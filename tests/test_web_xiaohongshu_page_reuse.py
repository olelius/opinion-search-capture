"""本轮新增定点验证：保存实拍的本地裁切与 Page 调用契约，不连接浏览器/平台。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image, ImageChops

from opinion_capture import web_xiaohongshu as adapter
from opinion_capture.web_fullpage import FullPageNotReady, crop_top_n, fullpage_readiness, plan_fullpage


ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / ".local/completion/existing-chrome-web"


def page_state(keyword="零跑", *, kind="results"):
    """小尺寸内存页面替身；不把这些像素作为平台采集证据。"""
    identity = "a" * 24
    return dict(schema_version=2, url="https://www.xiaohongshu.com/search_result?keyword=" + keyword,
                keyword=keyword, input_visible=True, search_tab="综合", feed_count=1,
                login_modal=False, block_text="", loading=False, pending_images=0,
                viewport=dict(width=80, height=60), document=dict(width=80, height=120, scroll_x=0, scroll_y=0),
                scroll=dict(host="document", top=0, height=120, at_top=True, viewport_height=60),
                results=[dict(id=identity, href="https://www.xiaohongshu.com/explore/"+identity,
                              dom_index=0, rect=[5, 15, 40, 60], document_rect=[5, 15, 40, 60],
                              images_loaded=True, cover_present=True, images=[dict(loaded=True)],
                              document_unclipped=True, document_uncovered=True)],
                suggestions=[dict(text=keyword+"汽车", dom_index=0)] if kind == "suggestions" else [],
                suggestion_panel_rect=[5, 5, 60, 35] if kind == "suggestions" else None,
                suggestion_panel_associated=kind == "suggestions", suggestion_panel_complete=kind == "suggestions",
                suggestion_pending_images=0)


class PageDouble:
    """只有调用契约所需接口；访问浏览器/context/profile/关闭对象会直接失败。"""
    def __init__(self, state):
        self.state = state
        self.calls = []
        self.after_screenshot = None

    def evaluate(self, expression, *args):
        self.calls.append(("evaluate",))
        if expression == adapter.SCROLL_TOP_JS:
            before = self.state["scroll"]["top"]
            self.state["scroll"].update(top=0, at_top=True)
            self.state["document"]["scroll_y"] = 0
            return {"before": before, "after": 0}
        return deepcopy(self.state)

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return self

    @property
    def first(self):
        return self

    def fill(self, value, **kwargs):
        self.calls.append(("fill", value))
        self.state.update(keyword=value, suggestions=[dict(text=value+"汽车", dom_index=0)],
                          suggestion_panel_rect=[5, 5, 60, 35], suggestion_panel_associated=True,
                          suggestion_panel_complete=True)

    def click(self, **kwargs):
        self.calls.append(("click", kwargs))
        self.state.update(suggestion_panel_rect=[5, 5, 60, 35], suggestion_panel_associated=True,
                          suggestion_panel_complete=True,
                          suggestions=[dict(text=self.state["keyword"]+"汽车", dom_index=0)])

    def focus(self, **kwargs):
        self.calls.append(("focus", kwargs))
        self.state.update(suggestion_panel_rect=[5,5,60,35],suggestion_panel_associated=True,
                          suggestion_panel_complete=True,suggestions=[dict(text=self.state["keyword"]+"汽车",dom_index=0)])

    def press(self, key, **kwargs):
        self.calls.append(("press", key))
        if key == "Enter":
            self.state = page_state(self.state["keyword"])
            # 模拟新请求更新结果身份，避免把输入框变了但旧卡片未更新当成就绪。
            self.state["results"][0]["id"] = hashlib.sha256(self.state["keyword"].encode()).hexdigest()[:24]
            self.state["results"][0]["href"] = "https://www.xiaohongshu.com/explore/" + self.state["results"][0]["id"]
        self.state.update(suggestion_panel_rect=None, suggestion_panel_associated=False, suggestions=[])

    def goto(self, url, **kwargs):
        from urllib.parse import parse_qs, urlparse, unquote
        self.calls.append(("goto", url))
        self.state = page_state(unquote(parse_qs(urlparse(url).query).get("keyword", [""])[0]))
        self.state["url"] = url
        return None

    def screenshot(self, *, path, full_page, **kwargs):
        self.calls.append(("screenshot", full_page, kwargs))
        with Image.new("RGB", (80, 120 if full_page else 60), "white") as png:
            png.save(path)
        if self.after_screenshot:
            self.after_screenshot()

    def content(self):
        self.calls.append(("content",))
        return "<html>本地页面替身</html>"


class PageReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.request = dict(keyword="零跑", capture_kind="results", top_n=1,
                            output_dir=self.temp.name, timeout_seconds=5)

    def run_page(self, page, **overrides):
        # 去掉必要稳定轮次之间的真实等待，但保留快照前后的总预算检查。
        with patch.object(adapter.Budget, "pause", lambda budget, *args: budget.check()):
            return adapter.capture_from_page(page, dict(self.request, **overrides))

    def test_same_keyword_fullpage_and_unique_runs_without_browser_ownership(self):
        page = PageDouble(page_state())
        # 保留官网原生双重编码 URL；入口不得重发导航修复编码。
        page.state["url"] = "https://www.xiaohongshu.com/search_result?keyword=%25E9%259B%25B6%25E8%25B7%2591"
        with patch.object(adapter, "_collect", side_effect=AssertionError("fullpage 应优先")), \
             patch.object(adapter, "_local_playwright", side_effect=AssertionError("禁止新建驱动")), \
             patch.object(adapter, "_dedicated_profile", side_effect=AssertionError("禁止读取 profile")):
            first = self.run_page(page, user_data_dir=object())
            second = self.run_page(page)
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        self.assertNotEqual(first["output_dir"], second["output_dir"])
        self.assertFalse({"goto", "fill", "click", "press"} & {call[0] for call in page.calls})
        self.assertEqual([call[1] for call in page.calls if call[0] == "screenshot"], [True, True])
        self.assertTrue(first["rank_verified"])
        with Image.open(first["primary_artifact_path"]) as png:
            self.assertEqual(png.size, (80, 75))
        self.assertNotIn(first["primary_artifact_path"], first["source_artifact_paths"])
        manifest = json.loads((Path(first["output_dir"])/"evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["result"], first)

    def test_changed_keyword_or_homepage_needs_exactly_one_navigation(self):
        changed = PageDouble(page_state("旧词"))
        result = self.run_page(changed)
        self.assertEqual(result["status"], "success")
        self.assertEqual(sum(call[0] == "goto" for call in changed.calls), 0)
        self.assertEqual(sum(call[0] == "fill" for call in changed.calls), 1)
        self.assertEqual(sum(call == ("press", "Enter") for call in changed.calls), 1)
        mismatch = PageDouble(page_state())
        mismatch.state["url"] = "https://www.xiaohongshu.com/explore"
        result = self.run_page(mismatch)
        self.assertEqual(result["status"], "success")
        self.assertEqual(sum(call[0] == "goto" for call in mismatch.calls), 1)
        self.assertEqual(sum(call == ("press", "Enter") for call in mismatch.calls), 0)
        blank = PageDouble(page_state())
        blank.state["url"] = "about:blank"
        self.assertEqual(self.run_page(blank)["status"], "success")
        self.assertEqual(sum(call[0] == "goto" for call in blank.calls), 2)

    def test_reused_page_restores_top_and_dismisses_panel_without_navigation(self):
        state = page_state(kind="suggestions")
        state["scroll"].update(top=100, at_top=False)
        state["document"]["scroll_y"] = 100
        page = PageDouble(state)
        result = self.run_page(page)
        self.assertEqual(result["status"], "success")
        self.assertIn(("press", "Tab"), page.calls)
        self.assertFalse(any(call[0] == "goto" for call in page.calls))
        self.assertEqual(page.state["scroll"]["top"], 0)

    def test_suggestions_same_word_only_expand_changed_word_fill_once(self):
        for same, already_open in ((True, False), (True, True), (False, False)):
            with self.subTest(same=same, already_open=already_open):
                page = PageDouble(page_state("零跑" if same else "旧词",
                                             kind="suggestions" if already_open else "results"))
                result = self.run_page(page, capture_kind="suggestions")
                self.assertEqual(result["status"], "success")
                self.assertEqual(sum(call[0] == "fill" for call in page.calls), int(not same))
                self.assertEqual(sum(call[0] == "focus" for call in page.calls), int(same and not already_open))
                self.assertFalse(any(call[0] == "click" for call in page.calls))
                self.assertFalse(any(call[0] in ("goto", "press") for call in page.calls))

    def test_failed_fullpage_preconditions_keep_real_partial_from_scroll(self):
        page = PageDouble(page_state())
        page.state["results"][0]["images_loaded"] = False

        def incomplete(_page, _request, _run, result, evidence, _budget):
            result.update(status="partial", reason="target_images_or_screenshot_coverage_incomplete")

        with patch.object(adapter, "_collect", side_effect=incomplete) as fallback:
            result = self.run_page(page)
        self.assertEqual(fallback.call_count, 1)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["rank_verified"])
        self.assertFalse(any(call[0] in ("screenshot", "goto") for call in page.calls))

    def test_unstable_fullpage_preserved_and_falls_back_without_second_fullpage(self):
        page = PageDouble(page_state())
        page.after_screenshot = lambda: page.state["results"][0].update(text="截图时内容改变")
        with patch.object(adapter, "_collect") as fallback:
            result = self.run_page(page)
        self.assertEqual(fallback.call_count, 1)
        self.assertEqual(len(result["source_artifact_paths"]), 1)
        self.assertEqual(sum(call[0] == "screenshot" for call in page.calls), 1)
        self.assertIsNone(result["primary_artifact_path"])
        frame = json.loads((Path(result["output_dir"])/"source-fullpage.json").read_text(encoding="utf-8"))
        self.assertFalse(frame["stable"])
        self.assertTrue(Path(frame["path"]).is_file())

    def test_cancellation_and_budget_apply_per_call_without_late_page_actions(self):
        flag = Path(self.temp.name)/"cancel"
        flag.touch()
        page = PageDouble(page_state())
        result = self.run_page(page, cancel_file=str(flag))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(page.calls, [])
        flag.unlink()
        page.after_screenshot = flag.touch
        result = self.run_page(page, cancel_file=str(flag))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(page.calls[-1][0], "screenshot")
        self.assertEqual(len(result["source_artifact_paths"]), 1)
        self.assertTrue(Path(result["source_artifact_paths"][0]).is_file())
        self.assertIsNone(result["primary_artifact_path"])
        slow = PageDouble(page_state())

        def delayed(expression):
            slow.calls.append(("evaluate",))
            time.sleep(.03)
            return deepcopy(slow.state)

        slow.evaluate = delayed
        result = self.run_page(slow, timeout_seconds=.01)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(slow.calls, [("evaluate",)])
        # 第二次调用获得新预算，而不是沿用上次超时的 deadline。
        self.assertEqual(self.run_page(PageDouble(page_state()))["status"], "success")

    def test_invalid_and_blocked_requests_stop_before_navigation(self):
        page = PageDouble(page_state())
        self.assertEqual(self.run_page(page, top_n=0)["status"], "failed")
        self.assertEqual(page.calls, [])
        page.state.update(login_modal=True)
        result = self.run_page(page)
        self.assertEqual((result["status"], result["reason"]), ("blocked", "login_required"))
        self.assertEqual(page.calls, [("evaluate",)])

    def test_cancel_wins_over_driver_error_and_document_scroll_requires_head(self):
        flag = Path(self.temp.name)/"cancel-during-call"
        page = PageDouble(page_state())

        def interrupted():
            flag.touch()
            raise RuntimeError("本地模拟驱动在截图写入后断开")

        page.after_screenshot = interrupted
        result = self.run_page(page, cancel_file=str(flag))
        self.assertEqual((result["status"], result["outcome"]), ("cancelled", "cancelled"))
        self.assertEqual(page.calls[-1][0], "screenshot")
        self.assertEqual(len(result["source_artifact_paths"]), 1)
        self.assertTrue(Path(result["source_artifact_paths"][0]).is_file())
        state = page_state()
        state["document"]["scroll_y"] = 100
        self.assertEqual(fullpage_readiness(state, 1), "feed_head_not_observed")

    def test_geometry_rejects_missing_proof_and_supports_pixel_scale(self):
        state = page_state()
        plan = plan_fullpage(state, state, [160, 240], 1)
        self.assertEqual(plan["crop_box"], [0, 0, 160, 150])
        for change in ("internal_scroll", "not_at_top", "duplicate", "unloaded", "clipped"):
            with self.subTest(change=change):
                bad = deepcopy(state)
                if change == "internal_scroll":
                    bad["scroll"]["host"] = "feed_ancestor"
                elif change == "not_at_top":
                    bad["scroll"]["at_top"] = False
                elif change == "duplicate":
                    bad["results"].append(dict(bad["results"][0], dom_index=1))
                elif change == "unloaded":
                    bad["results"][0]["images_loaded"] = False
                else:
                    bad["results"][0]["document_unclipped"] = False
                self.assertIsNotNone(fullpage_readiness(bad, 1))


@unittest.skipUnless((SAVED/"results-full-page.json").is_file() and (SAVED/"results-full-page.png").is_file(),
                     "仅在保存实拍存在的本机运行；不补采或下载")
class SavedEvidenceCropTests(unittest.TestCase):
    def test_saved_fullpage_exact_top20_without_overwriting_any_input(self):
        inputs = [SAVED/name for name in ("results-full-page.json", "results-full-page.png",
                                         "xiaohongshu-WEB-TOP20.png", "suggestions-validated.json", "suggestions.png")]
        hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
        metadata = json.loads(inputs[0].read_text(encoding="utf-8"))
        self.assertEqual(metadata["before"], metadata["after"])
        self.assertEqual(len(metadata["before"]["results"]), 26)
        run = ROOT / ".local/completion" / ("web-page-reuse-local-" + uuid4().hex)
        run.mkdir()
        output = run/"xiaohongshu-WEB-TOP20.png"
        layout = crop_top_n(inputs[1], metadata, output, 20)
        self.assertEqual(layout["dimensions"], [2560, 2480])
        self.assertAlmostEqual(layout["css_bottom"], 2479.4)
        self.assertEqual(layout["captured_count"], 20)
        with Image.open(output) as actual, Image.open(inputs[2]) as accepted, Image.open(inputs[1]) as raw:
            self.assertIsNone(ImageChops.difference(actual.convert("RGB"), accepted.convert("RGB")).getbbox())
            self.assertIsNone(ImageChops.difference(actual.convert("RGB"), raw.crop((0, 0, 2560, 2480)).convert("RGB")).getbbox())
        with self.assertRaises(FileExistsError):
            crop_top_n(inputs[1], metadata, output, 20)
        with self.assertRaises(FileExistsError):
            crop_top_n(inputs[1], metadata, inputs[1], 20)
        bad = dict(metadata, sha256="0"*64)
        with self.assertRaises(FullPageNotReady):
            crop_top_n(inputs[1], bad, run/"bad.png", 20)
        self.assertFalse((run/"bad.png").exists())
        self.assertEqual(hashes, {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs})
        suggestions = json.loads((SAVED/"suggestions-validated.json").read_text(encoding="utf-8"))
        self.assertEqual(len(suggestions["suggestions"]), 10)
        (run/"verification.json").write_text(json.dumps(dict(
            verification="local_saved_evidence_crop_only_no_browser_or_platform", layout=layout,
            source_files_unchanged=hashes, accepted_top20_pixels_equal=True, saved_suggestions_count=10,
            note="旧输入可见性标志不用于新的业务成功断言"), ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nLOCAL_VERIFICATION=" + str(run/"verification.json"))


if __name__ == "__main__":
    unittest.main()
