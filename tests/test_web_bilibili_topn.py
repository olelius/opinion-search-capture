"""严格TOP N受影响路径回归：纯决策、隔离监督及明确标注的离线浏览器夹具。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opinion_capture.web_bilibili import (
    LOCAL_ROOT, CaptureCancelled, _Budget, _dismiss_ordinary_tip_once, _snapshot,
    assess_capture, build_source_url, capture, classify_page, validate_request,
)
from opinion_capture.web_results_bilibili import (
    PageState, compose_primary, crop_plan, explicit_end, layout_fingerprint,
    normalize_snapshot, png_details, rank_items, stable_identity, collect_results,
)

TEST_ROOT = LOCAL_ROOT / "goal-offline-topn"


def req(n=3, **extra):
    """与runner兼容的独立输入；离线产物全部限制在goal目录。"""
    return validate_request(dict(keyword="零跑", capture_kind="results", top_n=n,
                                 timeout_seconds=180, output_dir=str(TEST_ROOT), **extra))


def video(number, x=0, y=200, height=150, **extra):
    """结构化独立卡夹具，非平台实测。"""
    return dict(id=f"BVfixture{number}", href=f"https://www.bilibili.com/video/BVfixture{number}",
                title=f"视频{number}", dom_index=number, images_loaded=True,
                rect=dict(x=x, y=y, width=100, height=height), promoted=False, **extra)


class PolicyTests(unittest.TestCase):
    def test_runner_timeout_and_absolute_cancel_contract(self):
        for timeout in (180, 1800):
            self.assertEqual(validate_request({**req(), "timeout_seconds": timeout})["timeout_seconds"], timeout)
        self.assertEqual(req(cancel_file=str(TEST_ROOT / "cancel"))["cancel_file"], str((TEST_ROOT / "cancel").resolve()))
        with self.assertRaises(ValueError):
            req(cancel_file="relative")

    def test_stable_video_and_promotion_target_without_ad_request(self):
        self.assertEqual(stable_identity(dict(href="https://www.bilibili.com/video/BVabc?spm=123")), "BVabc")
        self.assertEqual(stable_identity(dict(href="https://cm.bilibili.com/cm/api/fees?msg=random",
                                             target_url="https://www.bilibili.com/video/BVtarget?creative_id=99", promoted=True)), "BVtarget")
        self.assertEqual(stable_identity(dict(href="https://cm.bilibili.com/?creative_id=99&ts=21", promoted=True)), "promotion:creative_id:99")
        self.assertIsNone(stable_identity(dict(href="https://live.bilibili.com/12")))
        self.assertIsNone(stable_identity(dict(href="https://evil.example/video/BVabc")))
        self.assertIsNone(stable_identity(dict(href="javascript:alert(1)")))

    def test_order_duplicates_and_missing_identity_preserved(self):
        seen = {"BVfixture1": 1}
        raw = {"url": build_source_url("零跑", "results"), "items": [video(1), video(2), video(2), {**video(3), "id": None}]}
        fresh, occurrences = rank_items(raw, seen, 2)
        self.assertEqual([i["rank"] for i in fresh], [2])
        self.assertEqual([i["rank"] for i in occurrences], [1, 2, 2, None])
        self.assertEqual(occurrences[0]["duplicate_of"], 1)
        self.assertEqual(seen, {"BVfixture1": 1})

    def test_masonry_neighbor_ranks_and_complete_target_boundary(self):
        s = dict(document_size=dict(width=400, height=1000), input_rect=dict(y=10, height=40))
        items = [{**video(1, x=0, y=200, height=350), "rank": 1},
                 {**video(2, x=110, y=200, height=100), "rank": 2},
                 {**video(3, x=110, y=320, height=100), "rank": 3},
                 {**video(4, x=110, y=450, height=200), "rank": 4}]
        plan = crop_plan(s, items, 1, entire_page=False)
        self.assertEqual(plan["effective_rank_range"], [1, 3])
        self.assertEqual(plan["incidental_ranks"], [2, 3])
        self.assertEqual(plan["partially_visible_ranks"], [4])
        self.assertTrue(plan["targets_contained"])

    def test_no_growth_or_missing_next_does_not_prove_end(self):
        self.assertFalse(explicit_end(dict(items=[video(1)], next_present=False)))
        self.assertFalse(explicit_end(dict(items=[], next_present=False)))
        self.assertTrue(explicit_end(dict(next_present=True, next_disabled=True)))
        self.assertTrue(explicit_end(dict(items=[], no_results=["没有找到相关结果"])))

    def test_success_requires_each_piece_of_coverage(self):
        evidence = dict(url=build_source_url("零跑", "results"), title="零跑-bilibili", body="",
                        input_value="零跑", input_unobscured=True,
                        items=[{**video(i), "rank": i} for i in range(1, 4)],
                        coverage=dict(pages_verified=True, verified_ranks=[1, 2, 3],
                                      identity_order_verified=True, pixel_integrity_verified=True, end_verified=False))
        assess = lambda s, stable=True: assess_capture(req(), s, screenshot_saved=True, dom_stable=stable)
        self.assertEqual(assess(evidence)["status"], "success")
        for key in ("pages_verified", "identity_order_verified", "pixel_integrity_verified"):
            altered = copy.deepcopy(evidence)
            altered["coverage"][key] = False
            self.assertFalse(assess(altered)["rank_verified"], key)
        altered = copy.deepcopy(evidence)
        altered["coverage"]["verified_ranks"] = [1, 3]
        self.assertEqual(assess(altered)["status"], "partial")
        self.assertEqual(assess(evidence, False)["status"], "partial")
        altered = copy.deepcopy(evidence)
        altered["items"][0]["images_loaded"] = False
        self.assertEqual(assess(altered)["status"], "partial")
        altered["challenge_visible"] = True
        self.assertEqual(assess(altered)["status"], "blocked")

    def test_layout_fingerprint_tracks_order_and_media_not_scroll(self):
        s = dict(items=[video(1), video(2)], scroll_y=0)
        changed = copy.deepcopy(s)
        changed["scroll_y"] = 400
        changed["items"][0]["unobscured"] = True
        self.assertEqual(layout_fingerprint(s), layout_fingerprint(changed))
        changed["items"].reverse()
        self.assertNotEqual(layout_fingerprint(s), layout_fingerprint(changed))


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)

    def test_cancel_before_launch_starts_no_browser(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            flag = Path(folder) / "cancel"
            flag.touch()
            with patch("opinion_capture.web_bilibili.subprocess.Popen") as launch:
                result = capture(req(cancel_file=str(flag)))
            self.assertEqual(result["status"], "cancelled")
            launch.assert_not_called()
            self.assertTrue(flag.exists())

    def test_budget_cancellation_and_total_deadline(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            flag = Path(folder) / "cancel"
            budget = _Budget(time.monotonic() + 1, str(flag))
            self.assertLessEqual(budget(5000), 1000)
            flag.touch()
            with self.assertRaises(CaptureCancelled):
                budget()
            with self.assertRaises(TimeoutError):
                _Budget(time.monotonic() - 1, None)()

    def test_hung_worker_is_terminated_by_total_budget(self):
        real_popen = subprocess.Popen
        def sleeping_worker(command, **options):
            if "--worker" in command:
                return real_popen([sys.executable, "-B", "-c", "import time;time.sleep(60)"], **options)
            return real_popen(command, **options)
        with patch("opinion_capture.web_bilibili.subprocess.Popen", side_effect=sleeping_worker):
            before = time.monotonic()
            result = capture({**req(), "timeout_seconds": .5})
        self.assertEqual(result["reason"], "capture_deadline_exceeded")
        self.assertEqual(result["outcome"], "timeout")
        self.assertFalse(result["rank_verified"])
        self.assertLess(time.monotonic() - before, 5)

    def test_inflight_cancel_stops_worker_and_preserves_flag(self):
        import threading
        real_popen = subprocess.Popen
        processes = []
        def sleeping_worker(command, **options):
            if "--worker" in command:
                process = real_popen([sys.executable, "-B", "-c", "import time;time.sleep(60)"], **options)
                processes.append(process)
                return process
            return real_popen(command, **options)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            flag = Path(folder) / "cancel"
            timer = threading.Timer(.4, flag.touch)
            timer.start()
            try:
                with patch("opinion_capture.web_bilibili.subprocess.Popen", side_effect=sleeping_worker):
                    before = time.monotonic()
                    result = capture(req(cancel_file=str(flag)))
            finally:
                timer.cancel()
            self.assertEqual(result["status"], "cancelled")
            self.assertTrue(flag.exists())
            self.assertLess(time.monotonic() - before, 5)
            self.assertTrue(all(p.poll() is not None for p in processes))


_COVER = "data:image/svg+xml," + quote('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="150"><rect width="320" height="150" fill="#789acc"/><circle cx="85" cy="70" r="30" fill="#ff8844"/></svg>')


def card(number: int, *, ad=False, live=False, broken=False):
    """构造离线结果组件；网络由route完全拦截，图像为内联数据。"""
    href = f"https://www.bilibili.com/video/BVfixture{number}"
    if live:
        href = "https://live.bilibili.com/123"
    attributes = f' data-target-url="{href}"' if ad else ""
    if ad:
        href = "https://cm.bilibili.com/cm/api/fees?msg=fixture"
    image = "" if broken else _COVER
    return f'<div class="slot"><div class="bili-video-card"><a href="{href}"{attributes}><picture><img src="{image}" loading="lazy"></picture><h3 class="bili-video-card__info--tit">离线视频{number}</h3></a>{"<span class=bili-video-card__stats--ad>推广</span>" if ad else ""}</div></div>'


def fixture(first: str, *, second: str | None = None, next_disabled=False, empty=False, overlays=""):
    """完整离线页面，显式标注FIXTURE；翻页是夹具自身原生按钮行为。"""
    pager = ""
    if second is not None or next_disabled:
        pager = f'<div class="vui_pagenation"><button class="vui_button--active">1</button><button id="next" {"disabled" if next_disabled else ""}>下一页</button></div>'
    script = ""
    if second is not None:
        script = f'''<script>document.getElementById('next').onclick=()=>{{
        document.querySelector('.video-list').innerHTML={json.dumps(second)};
        document.querySelector('.vui_button--active').textContent='2';
        document.getElementById('next').disabled=true;window.scrollTo(0,0);}};</script>'''
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>零跑-离线FIXTURE</title>
    <style>body{{margin:0;background:#fafafa;font-family:Arial}} .heading{{height:100px;padding:20px;box-sizing:border-box;background:#dceaf3}}
    input{{height:40px;width:400px}} .search-page{{margin:20px}} .video-list{{display:grid;grid-template-columns:repeat(3,1fr);gap:30px}}
    .bili-video-card{{height:220px;background:#f1f3f5}} img{{width:100%;height:150px}} h3{{margin:8px}} .vui_pagenation{{padding:20px}} .bili-video-card__stats--ad{{display:block}}</style></head>
    <body><div class="heading">OFFLINE FIXTURE <input class="search-input-el" value="零跑"></div>
    <div class="search-page"><div class="video search-all-list"><div class="video-list">{first}</div></div>
    {'<div class="search-nodata">没有找到相关结果</div>' if empty else ''}{pager}</div>{overlays}{script}</body></html>'''


class OfflineBrowserTests(unittest.TestCase):
    """真实Chrome只渲染离线夹具，覆盖DOM提取、滚动、截图像素和原生翻页。"""
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.chrome = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"
        if not cls.chrome.is_file():
            raise unittest.SkipTest("本测试需要本机Chrome")
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        cls.root = Path(tempfile.mkdtemp(prefix="browser-fixture-", dir=TEST_ROOT))
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=str(cls.chrome), headless=True,
                                            downloads_path=str(cls.root))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1000, "height": 650})
        self.page = self.context.new_page()
        self.run = self.root / self._testMethodName
        self.run.mkdir()
        self.budget = _Budget(time.monotonic() + 90, None)

    def tearDown(self):
        self.context.close()

    def load(self, html):
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
                           if route.request.resource_type == "document" else route.abort())
        self.page.goto(build_source_url("零跑", "results"))
        self.page.wait_for_timeout(120)

    def collect(self, n):
        result = collect_results(self.page, req(n), self.run, self.budget,
                                 lambda: _snapshot(self.page, "results", 200), classify_page, lambda r: None)
        assessment = assess_capture(req(n), {**result["scene"], "items": result["items"], "coverage": result["coverage"]},
                                    screenshot_saved=bool(result["primary"]), dom_stable=all(p["dom_stable"] for p in result["pages"]))
        (self.run / "fixture-evidence.json").write_text(json.dumps({"type": "OFFLINE_FIXTURE", "assessment": assessment,
                                                                    "collection": result}, ensure_ascii=False, indent=2), encoding="utf-8")
        return result, assessment

    def test_aggregate_related_live_exclusion_and_ad_inclusion(self):
        html = fixture(card(1) + card(2, ad=True) + card(3, live=True) + card(4))
        extra = '<div class="user search-all-list"><div class="video-list">' + card(91) + '</div></div><div class="related-search search-all-list">' + card(92) + '</div>'
        html = html.replace('<div class="video search-all-list">', extra + '<div class="video search-all-list">')
        self.load(html)
        scene = _snapshot(self.page, "results", 200)
        self.assertEqual([i["id"] for i in scene["items"]], ["BVfixture1", "BVfixture2", "BVfixture4"])
        self.assertTrue(scene["items"][1]["promoted"])
        self.assertEqual(len(scene["excluded"]), 3)
        result, assessment = self.collect(3)
        self.assertEqual(assessment["status"], "success")
        self.assertEqual(result["coverage"]["verified_ranks"], [1, 2, 3])

    def test_lazy_multi_page_duplicates_and_original_pixel_composition(self):
        self.load(fixture(''.join(card(i) for i in range(1, 10)), second=card(9) + card(10) + card(11) + card(12)))
        result, assessment = self.collect(11)
        self.assertEqual(assessment["status"], "success")
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual(len(result["transitions"]), 1)
        self.assertEqual([i["id"] for i in result["items"]], [f"BVfixture{i}" for i in range(1, 13)])
        self.assertTrue(result["pages"][1]["occurrences"][0]["duplicate_of"])
        self.assertTrue(result["primary"]["pixel_integrity_verified"])
        before_hashes = [p["source"]["sha256"] for p in result["pages"]]
        with self.assertRaises(FileExistsError):
            compose_primary(result["pages"], Path(result["primary"]["path"]), 11, self.budget)
        self.assertEqual(before_hashes, [hashlib.sha256(Path(p["source"]["path"]).read_bytes()).hexdigest() for p in result["pages"]])

    def test_explicit_end_allows_insufficient_results(self):
        self.load(fixture(card(1) + card(2), next_disabled=True))
        _, assessment = self.collect(5)
        self.assertEqual((assessment["status"], assessment["outcome"]), ("success", "insufficient_results"))
        self.assertTrue(assessment["rank_verified"])

    def test_zero_results_needs_explicit_empty_state(self):
        self.load(fixture("", empty=True))
        _, assessment = self.collect(5)
        self.assertEqual((assessment["status"], assessment["observed_count"]), ("success", 0))
        self.assertEqual(assessment["outcome"], "insufficient_results")

    def test_no_next_without_end_is_partial(self):
        self.load(fixture(card(1) + card(2)))
        _, assessment = self.collect(5)
        self.assertEqual(assessment["status"], "partial")
        self.assertFalse(assessment["rank_verified"])

    def test_occlusion_does_not_pass_with_stable_dom(self):
        self.load(fixture(card(1) + card(2), overlays='<div style="position:fixed;top:140px;left:20px;width:350px;height:200px;background:#333;z-index:99">OFFLINE OVERLAY</div>'))
        result, assessment = self.collect(2)
        self.assertTrue(result["pages"][0]["dom_stable"])
        self.assertFalse(result["pages"][0]["target_visibility_verified"])
        self.assertEqual(assessment["status"], "partial")

    def test_partially_visible_card_occlusion_at_capture_is_rejected(self):
        self.load(fixture(''.join(card(i) for i in range(1, 10)), overlays='<div style="position:fixed;top:620px;left:20px;width:320px;height:25px;background:#333;z-index:99">PARTIAL VIEWPORT OVERLAY</div>'))
        scene = _snapshot(self.page, "results", 200)
        self.assertFalse(scene["items"][6]["unobscured"])
        result, assessment = self.collect(7)
        self.assertFalse(result["pages"][0]["target_visibility_verified"])
        self.assertEqual(assessment["status"], "partial")

    def test_dom_changes_during_capture_preserve_versions_and_fail_proof(self):
        self.load(fixture(card(1) + card(2)))
        original = self.page.screenshot
        def changing_screenshot(**kwargs):
            raw = original(**kwargs)
            self.page.evaluate("document.querySelector('h3').textContent += 'changed'")
            return raw
        with patch.object(self.page, "screenshot", side_effect=changing_screenshot):
            result, assessment = self.collect(2)
        self.assertEqual(len(list(self.run.glob('page-001-original-v*.png'))), 2)
        self.assertFalse(result["pages"][0]["dom_stable"])
        self.assertFalse(assessment["rank_verified"])

    def test_lazy_append_is_collected_without_fabricated_end(self):
        html = fixture(card(1) + card(2))
        appended = ''.join(card(i) for i in range(3, 9))
        html += '<script>setTimeout(()=>document.querySelector(".video-list").insertAdjacentHTML("beforeend",' + json.dumps(appended) + '),400)</script>'
        self.load(html)
        result, assessment = self.collect(7)
        self.assertEqual(assessment["status"], "success")
        self.assertEqual(result["coverage"]["verified_ranks"], list(range(1, 8)))

    def test_ordinary_close_is_native_and_at_most_once_per_type(self):
        tip = '<div class="login-tip" style="position:fixed;right:10px;bottom:20px;background:#00afff"><button class="close" onclick="this.parentElement.remove()">关闭</button>普通提示</div>'
        self.load(fixture(card(1), overlays=tip))
        evidence = {}
        _dismiss_ordinary_tip_once(self.page, self.budget, evidence)
        self.assertEqual(self.page.locator('.login-tip').count(), 0)
        self.page.evaluate('html=>document.body.insertAdjacentHTML("beforeend",html)', tip)
        _dismiss_ordinary_tip_once(self.page, self.budget, evidence)
        self.assertEqual(self.page.locator('.login-tip').count(), 1)
        self.assertEqual(len(evidence["ordinary_tip_interactions"]), 1)

    def test_pagination_without_page_and_identity_transition_stays_partial(self):
        self.load(fixture(card(1) + card(2), second=card(3)))
        self.page.evaluate("document.getElementById('next').onclick=()=>{}")
        result, assessment = self.collect(5)
        self.assertEqual(result["stopped_reason"], "pagination_not_confirmed")
        self.assertEqual(assessment["status"], "partial")
        self.assertEqual(result["transitions"], [])

    def test_missing_image_is_partial(self):
        self.load(fixture(card(1) + card(2, broken=True)))
        _, assessment = self.collect(2)
        self.assertEqual(assessment["status"], "partial")
        self.assertIn("target_images_incomplete", assessment["reason"])

    def test_challenge_stops_before_screenshot_or_navigation(self):
        self.load(fixture(card(1), overlays='<div class="geetest_panel">离线验证码夹具</div>'))
        with self.assertRaises(PageState) as exc:
            self.collect(1)
        self.assertEqual(exc.exception.status, "blocked")
        self.assertEqual(list(self.run.glob("*.png")), [])

    def test_suggestions_ten_contract_not_truncated_by_top_n(self):
        suggestions = '<div class="search-panel"><div class="suggestions">' + ''.join(f'<div class="suggest-item">零跑{i}</div>' for i in range(10)) + '</div></div>'
        self.load(fixture("") + suggestions)
        scene = _snapshot(self.page, "suggestions", 200)
        assessment = assess_capture({**req(3), "capture_kind": "suggestions"}, scene, screenshot_saved=True, dom_stable=True)
        self.assertEqual((assessment["status"], assessment["observed_count"]), ("success", 10))
        self.assertFalse(assessment["rank_verified"])


if __name__ == "__main__":
    unittest.main()
