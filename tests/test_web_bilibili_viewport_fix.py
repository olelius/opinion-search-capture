"""B站WEB本次修复的离线定点：原生滚动条、视口原图、严格排名和可见导航。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from opinion_capture import web_bilibili as web
from opinion_capture import web_results_bilibili as results
from opinion_capture.progress import phase_scope
from test_web_bilibili_topn import card, fixture, _COVER


class EvidenceTests(unittest.TestCase):
    def test_customer_uniform_shift_is_not_missing_or_obscured(self):
        """复用客户已有JSON，仅检验规则，不回写历史partial或推断原图通过。"""
        path = ROOT / ".local/customer-evidence/bilibili-12a2e35a/worker-evidence.json"
        if not path.is_file():
            self.skipTest("客户私有证据不随代码发布")
        evidence = json.loads(path.read_text(encoding="utf-8"))
        before, after = evidence["before"], evidence["after"]
        self.assertEqual(after["items"][0]["rect"]["x"] - before["items"][0]["rect"]["x"], 7)
        self.assertTrue(web.suggestion_stability(before, after)["verified"])
        for mutation in ("missing", "order", "hidden", "input", "relative"):
            changed = copy.deepcopy(after)
            if mutation == "missing":
                changed["items"].pop()
            elif mutation == "order":
                changed["items"].reverse()
            elif mutation == "hidden":
                changed["items"][0]["unobscured"] = False
            elif mutation == "input":
                changed["input_unobscured"] = False
            else:
                changed["items"][0]["rect"]["y"] += 20
            proof = web.suggestion_stability(before, changed)
            self.assertFalse(proof["verified"], mutation)
            self.assertTrue(proof["failures"], mutation)

    def test_identity_failure_is_specific_and_not_overridden_by_pixels(self):
        scene = {"items": [{"dom_index": 4, "id": None, "title": "", "css_order": "2"}],
                 "unknown": [{"dom_index": 3, "reason": "unknown_card_container", "module": "custom"}]}
        failures = results.identity_order_failures(scene)
        self.assertIn("unknown_card:3:unknown_card_container:custom", failures)
        self.assertIn("card_id_missing:4", failures)
        self.assertIn("card_title_missing:4", failures)
        self.assertIn("card_css_order_unverified:4:css_order=2", failures)
        report = results.assess_results({"top_n": 1}, {
            "items": [{"id": "BVfixture1", "title": "正常", "rank": 1, "images_loaded": True}],
            "input_unobscured": True,
            "coverage": {"pages_verified": False, "verified_ranks": [], "identity_order_verified": False,
                         "pixel_integrity_verified": True, "verification_failures": failures}},
            screenshot_saved=True, dom_stable=True)
        self.assertFalse(report["rank_verified"])
        self.assertIn("card_id_missing:4", report["reason"])


def suggestions_html():
    """显式本地夹具；十条下拉及页尾使原生竖滚动条出现。"""
    rows = ''.join(f'<div class="suggest-item">零跑词{i}</div>' for i in range(1, 11))
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>零跑 OFFLINE FIXTURE</title>
    <style>body{{margin:0;background:#eff5fa}}input{{display:block;width:636px;box-sizing:border-box;height:40px;margin:100px auto 20px}}
    .search-panel{{width:636px;margin:auto;background:white}}.suggest-item{{height:32px;box-sizing:border-box;padding:6px 16px}}
    #tail{{height:2800px;background:linear-gradient(#abc,#789)}}</style></head><body>
    <input id="search-input" class="search-input-el" value="零跑"><div class="search-panel"><div class="suggestions">{rows}</div></div>
    <div id="tail">OFFLINE FIXTURE</div></body></html>'''


class OfflineDOMTests(unittest.TestCase):
    """只启动并关闭本类自建无账号浏览器；所有页面请求本地fulfill或abort。"""
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        chrome = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"
        if not chrome.is_file():
            raise unittest.SkipTest("需要已安装Chrome渲染离线DOM")
        parent = ROOT / ".local/web_bilibili"
        parent.mkdir(parents=True, exist_ok=True)
        cls.root = Path(tempfile.mkdtemp(prefix="viewport-fix-", dir=parent))
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=str(chrome), headless=True,
                                            ignore_default_args=["--hide-scrollbars"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 889}, service_workers="block")
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.run = self.root / self._testMethodName
        self.run.mkdir()
        self.requests = []

    def tearDown(self):
        self.context.close()

    def load(self, html, url=None):
        """仅给主文档返回夹具，其他资源全部终止；从不请求平台服务器。"""
        def route_local(route):
            self.requests.append((route.request.url, route.request.resource_type))
            if route.request.resource_type == "document":
                route.fulfill(status=200, content_type="text/html", body=html)
            else:
                route.abort()
        self.context.unroute("**/*")
        self.context.route("**/*", route_local)
        self.page.goto(url or web.build_source_url("零跑", "results"))
        self.page.wait_for_timeout(100)

    def collect(self, top_n):
        budget = web._Budget(time.monotonic() + 50, None)
        records = []
        collected = results.collect_results(self.page, {"keyword": "零跑", "top_n": top_n}, self.run,
                    budget, lambda: web._snapshot(self.page, "results", 200), web.classify_page, records.append)
        return collected, records

    def test_classic_scrollbar_reproduces_fullpage_shift_but_viewport_is_stable(self):
        self.load(suggestions_html())
        before = web._snapshot(self.page, "suggestions", 200)
        self.assertLess(before["document_size"]["width"], before["viewport"]["width"])
        self.page.screenshot(full_page=True, path=str(self.run / "legacy-fullpage.png"))
        after = web._snapshot(self.page, "suggestions", 200)
        self.assertNotEqual(before["items"][0]["rect"]["x"], after["items"][0]["rect"]["x"])
        self.load(suggestions_html())
        before = web._snapshot(self.page, "suggestions", 200)
        self.page.screenshot(full_page=False, path=str(self.run / "viewport.png"))
        after = web._snapshot(self.page, "suggestions", 200)
        self.assertEqual(before["document_size"], after["document_size"])
        self.assertEqual(before["items"], after["items"])

    def test_suggestions_real_worker_preserves_ten_rows_and_emits_phases(self):
        self.load(suggestions_html())
        with phase_scope({}) as recorder:
            result = web.capture_from_page(self.page, {"keyword": "零跑", "capture_kind": "suggestions",
                                            "top_n": 1, "output_dir": str(self.run), "timeout_seconds": 30})
        self.assertEqual(result["status"], "success", result["reason"])
        self.assertEqual([i["text"] for i in result["items"]], [f"零跑词{i}" for i in range(1, 11)])
        source = Path(result["source_artifact_paths"][0])
        self.assertEqual(results.png_details(source)["size"], [1440, 889])
        codes = {item["phase_code"] for item in recorder.timings}
        self.assertTrue({"prepare_search", "waiting_content", "screenshot", "validate_capture"} <= codes)
        self.assertFalse(self.page.is_closed())

    def test_suggestions_overlay_and_lost_row_during_shot_remain_partial(self):
        self.load(suggestions_html())
        original = self.page.screenshot
        def changed(**kwargs):
            data = original(**kwargs)
            self.page.evaluate("document.querySelector('.suggest-item').remove()")
            return data
        with patch.object(self.page, "screenshot", side_effect=changed):
            result = web.capture_from_page(self.page, {"keyword": "零跑", "capture_kind": "suggestions",
                                          "output_dir": str(self.run), "timeout_seconds": 30})
        self.assertEqual(result["status"], "partial")
        self.assertIn("suggestion_text_or_order_changed", result["reason"])
        self.assertTrue(Path(result["source_artifact_paths"][0]).is_file())

    def test_suggestions_obscured_word_is_not_success(self):
        self.load(suggestions_html() + '<div style="position:fixed;left:0;right:0;top:165px;height:20px;background:black;z-index:99">OVERLAY</div>')
        result = web.capture_from_page(self.page, {"keyword": "零跑", "capture_kind": "suggestions",
                                      "output_dir": str(self.run), "timeout_seconds": 30})
        self.assertEqual(result["status"], "partial")
        self.assertIn("suggestion_partly_obscured:1", result["reason"])

    def test_two_page_results_keep_identity_order_and_original_frame_pixels(self):
        self.page.set_viewport_size({"width": 1000, "height": 650})
        self.load(fixture(''.join(card(i) for i in range(1, 10)), second=card(9) + card(10) + card(11) + card(12)))
        result, records = self.collect(11)
        self.assertTrue(result["coverage"]["pages_verified"], result["coverage"])
        self.assertTrue(result["coverage"]["identity_order_verified"])
        self.assertEqual(result["coverage"]["verified_ranks"], list(range(1, 12)))
        self.assertEqual([i["id"] for i in result["items"]], [f"BVfixture{i}" for i in range(1, 13)])
        self.assertEqual(len(result["transitions"]), 1)
        self.assertGreater(len(records[0]["viewport_frames"]), 1)
        from PIL import Image
        for record in records:
            self.assertEqual(record["before"]["document_size"]["width"], record["after"]["document_size"]["width"])
            with Image.open(record["source"]["path"]) as derived:
                for frame in record["viewport_frames"]:
                    source = Path(frame["source"]["path"])
                    self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), frame["source"]["sha256"])
                    with Image.open(source) as original:
                        self.assertEqual(original.convert("RGB").crop(frame["source_box"]).tobytes(),
                                         derived.crop(frame["destination_box"]).tobytes())

    def test_unrelated_footer_growth_does_not_fail_result_targets(self):
        html = fixture(card(1) + card(2)) + '<div id="tail" style="height:2000px">OFFLINE TAIL</div>'
        self.load(html)
        original = self.page.screenshot
        def grow(**kwargs):
            self.page.evaluate("document.getElementById('tail').style.height=(2000+Math.random()*300)+'px'")
            self.assertIs(kwargs.get("full_page"), False)
            return original(**kwargs)
        with patch.object(self.page, "screenshot", side_effect=grow):
            result, _ = self.collect(2)
        self.assertTrue(result["coverage"]["pages_verified"], result["coverage"])

    def test_results_worker_lists_native_frames_separate_from_derived_region(self):
        self.load(fixture(card(1) + card(2)))
        result = web.capture_from_page(self.page, {"keyword": "零跑", "capture_kind": "results", "top_n": 2,
                                      "output_dir": str(self.run), "timeout_seconds": 30})
        self.assertEqual(result["status"], "success", result["reason"])
        self.assertTrue(result["source_artifact_paths"])
        self.assertTrue(all("-original-" in path and "-region-" not in path for path in result["source_artifact_paths"]))
        self.assertTrue(any("-region-" in path for path in result["artifact_paths"]))
        self.assertTrue(all(Path(path).is_file() for path in result["source_artifact_paths"] + result["artifact_paths"]))
        self.assertFalse(self.page.is_closed())

    def test_unknown_card_keeps_identity_false_with_specific_failure(self):
        self.load(fixture(card(1)) + '<div class="search-page">' + card(88) + '</div>')
        result, _ = self.collect(1)
        self.assertFalse(result["coverage"]["identity_order_verified"])
        self.assertTrue(result["coverage"]["pixel_integrity_verified"])
        self.assertTrue(any("unknown_card:" in f for f in result["coverage"]["verification_failures"]))

    def test_order_changes_at_capture_keep_versions_and_fail_rank(self):
        self.load(fixture(card(1) + card(2)))
        original = self.page.screenshot
        def reorder(**kwargs):
            png = original(**kwargs)
            self.page.evaluate("const e=document.querySelector('.video-list');e.append(e.firstElementChild)")
            return png
        with patch.object(self.page, "screenshot", side_effect=reorder):
            result, records = self.collect(2)
        self.assertFalse(result["coverage"]["pages_verified"])
        self.assertFalse(result["coverage"]["identity_order_verified"])
        self.assertEqual(len(records), 2)
        self.assertEqual(len(list(self.run.glob("page-001-original-v*.png"))), 2)
        self.assertTrue(any("order_changed" in f for f in result["coverage"]["verification_failures"]))

    def test_css_pixel_capture_at_device_scale_two(self):
        self.context.close()
        self.context = self.browser.new_context(viewport={"width": 1000, "height": 650},
                                                device_scale_factor=2, service_workers="block")
        self.page = self.context.new_page()
        self.load(fixture(card(1) + card(2)))
        result, records = self.collect(2)
        self.assertTrue(result["coverage"]["pages_verified"], result["coverage"])
        self.assertEqual(records[0]["viewport_frames"][0]["source"]["size"], [1000, 650])

    def test_result_overlay_is_not_masked_by_pixel_integrity(self):
        self.load(fixture(card(1), overlays='<div style="position:fixed;left:20px;top:140px;width:300px;height:150px;background:#333;z-index:999">OFFLINE OVERLAY</div>'))
        result, _ = self.collect(1)
        self.assertFalse(result["coverage"]["pages_verified"])
        self.assertTrue(result["coverage"]["pixel_integrity_verified"])
        self.assertTrue(any("obscured" in f for f in result["coverage"]["verification_failures"]))

    def test_visible_avatar_navigation_only_no_storage_or_navigation(self):
        avatar = f'<a class="header-avatar-wrap" href="https://space.bilibili.com/12345"><img src="{_COVER}" width="40" height="40"></a>'
        cases = [('<header class="bili-header">' + avatar + '</header>', True),
                 (avatar, False),
                 ('<header class="bili-header"><a href="https://passport.bilibili.com/login">登录</a></header>', False),
                 ('<header class="bili-header" style="display:none">' + avatar + '</header>', False),
                 ('<header class="bili-header">' + avatar + '</header><div class="geetest_panel">验证</div>', False)]
        for html, expected in cases:
            self.load(html, "https://www.bilibili.com/")
            before = len(self.requests)
            self.assertEqual(web.authenticated_navigation(self.page), expected)
            self.assertEqual(len(self.requests), before)
        self.load(cases[0][0], "https://not-bilibili.invalid/")
        self.assertFalse(web.authenticated_navigation(self.page))


if __name__ == "__main__":
    unittest.main()
