"""WEB滚动/重搜定点：纯Page替身与合成像素，不启动浏览器或访问网站。"""
from copy import deepcopy
import io
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import patch

from PIL import Image

from opinion_capture import web_bilibili as bili
from opinion_capture import web_results_bilibili as results
from opinion_capture import web_xiaohongshu as xhs


class FakeBudget:
    """记录稳定对间隔，不进行真实等待；保留预算接口。"""
    deadline = float("inf")

    def __init__(self):
        self.pauses = []

    def __call__(self, cap=30000):
        return cap

    check = __call__

    def pause(self, *args):
        self.pauses.append(args[-1])

    @staticmethod
    def timestamp():
        return "2026-09-09T00:00:00+08:00"


class FakePage:
    """输入、单次文档导航和原生滚动替身；无context及网络接口。"""
    def __init__(self, platform, kind="results"):
        self.platform, self.kind = platform, kind
        self.keyword, self.generation, self.top = "测试词", 0, 0
        self.url = (bili.build_source_url(self.keyword, "results") if platform == "bili" else
                    "https://www.xiaohongshu.com/search_result?keyword=" + self.keyword)
        self.panel = kind == "suggestions"
        self.calls, self.blocked, self.unknown = [], False, False
        self.keep_scroll = False
        self.mouse = self

    @property
    def first(self):
        return self

    def locator(self, selector):
        return self

    def wait_for(self, **kwargs):
        pass

    def input_value(self, **kwargs):
        return self.keyword

    def focus(self, **kwargs):
        self.calls.append(("focus",))
        self.panel = bool(self.keyword)

    click = focus

    def fill(self, value, **kwargs):
        self.calls.append(("fill", value))
        self.keyword, self.panel = value, bool(value)

    def press(self, key, **kwargs):
        self.calls.append(("press", key))
        if key == "Enter":
            raise AssertionError("同词重试不依赖可能被忽略的Enter")
        self.panel = False

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))
        self.url = url
        self.keyword = unquote(parse_qs(urlsplit(url).query).get("keyword", [""])[0])
        self.generation += 1
        self.panel = False
        self.top = 0 if not self.keep_scroll else self.top
        return None

    def on(self, *args):
        pass

    def remove_listener(self, *args):
        pass

    def move(self, *args):
        pass

    def scene(self):
        if self.platform == "xhs":
            identity = str(self.generation + 1) * 24
            return dict(url=self.url, keyword=self.keyword, input_id="search-input", input_visible=True,
                        search_tab="综合",
                        login_modal=self.blocked, loading=False, pending_images=0,
                        scroll=dict(top=self.top, at_top=self.top == 0, viewport_height=100, height=400),
                        document=dict(scroll_y=self.top), viewport=dict(width=100, height=100),
                        results=[dict(id=identity, href="https://www.xiaohongshu.com/explore/" + identity)],
                        suggestions=[dict(text=self.keyword + "列表")] if self.panel else [],
                        suggestion_panel_rect=[0, 20, 80, 30] if self.panel else None,
                        suggestion_panel_associated=self.panel, suggestion_panel_complete=self.panel)
        cards = [dict(dom_index=i, id=f"BVfake{self.generation}{i}",
                      href=f"https://www.bilibili.com/video/BVfake{self.generation}{i}", title=f"标题{i}",
                      rect=dict(x=5, y=y, width=60, height=40), images_loaded=True,
                      images=[dict(src="fixture")], unobscured=True if self.top <= y and y+40 <= self.top+100 else None)
                 for i, y in enumerate((20, 110, 200), 1)]
        suggestions = [dict(dom_index=1, text=self.keyword + "列表", unobscured=True,
                            rect=dict(x=5, y=30, width=60, height=30))] if self.panel else []
        return dict(url=self.url, title=self.keyword, input_value=self.keyword,
                    items=cards if self.kind == "results" else suggestions,
                    unknown=[dict(dom_index=4, reason="unknown_card_container")] if self.unknown else [],
                    input_rect=dict(x=5, y=0, width=60, height=10), input_unobscured=self.top == 0,
                    challenge_visible=self.blocked, page_number=1, fonts_loaded=True, scroll_y=self.top,
                    viewport=dict(width=100, height=100), document_size=dict(width=100, height=400))

    def evaluate(self, script, *args):
        if script == xhs.SCROLL_TOP_JS or script == "window.scrollTo(0,0)":
            self.calls.append(("scroll", 0))
            self.top = 0
            return {}
        if script == "y => window.scrollTo(0,y)":
            self.top = min(300, max(0, args[0]))
            self.calls.append(("scroll", self.top))
            return None
        if script == results._TILE_VISIBILITY_JS:
            return []
        return deepcopy(self.scene())

    def content(self):
        return "<html>仅单元测试</html>"

    def screenshot(self, **kwargs):
        self.calls.append(("screenshot", kwargs))
        if self.kind == "suggestions":
            raise RuntimeError("fixture_stop_before_screenshot")
        self.assert_viewport(kwargs)
        with Image.new("RGB", (100, 100)) as png:
            png.putdata([(x, (y + round(self.top)) % 256, 90) for y in range(100) for x in range(100)])
            stream = io.BytesIO()
            png.save(stream, format="PNG")
            return stream.getvalue()

    @staticmethod
    def assert_viewport(kwargs):
        if kwargs.get("full_page") is not False:
            raise AssertionError("仅原生视口，不改列数或布局")


class WebScrollRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.request = dict(keyword="测试词", capture_kind="results", top_n=3,
                            output_dir=self.temp.name, timeout_seconds=10)

    def bili_capture(self, page, **overrides):
        """到采集器边界即停；只检查调用/重试字段，不伪称平台采集成功。"""
        observed = []

        def collect(page, req, run, budget, snapshot, *args):
            observed.append((page.generation, snapshot()["scroll_y"], str(run)))
            raise RuntimeError("fixture_collector_boundary")

        with patch.object(bili, "_dismiss_ordinary_tip_once"), \
                patch.object(bili._Budget, "pause", lambda *args: None), \
                patch.object(bili, "collect_results", side_effect=collect):
            report = bili.capture_from_page(page, dict(self.request, **overrides))
        return report, observed

    def test_validate_preserves_only_real_bool(self):
        for adapter in (xhs, bili):
            self.assertIs(adapter.validate_request(self.request)["fresh_search"], False)
            for value in (True, False):
                self.assertIs(adapter.validate_request(dict(self.request, fresh_search=value))["fresh_search"], value)
            for value in (1, 0, "true", None):
                with self.assertRaises(ValueError):
                    adapter.validate_request(dict(self.request, fresh_search=value))

    def test_same_keyword_reuses_then_retry_navigates_once_and_resets_top(self):
        page = FakePage("xhs")
        first = xhs._prepare_current_page(page, self.request, {}, FakeBudget())
        self.assertFalse(any(call[0] == "goto" for call in page.calls))
        page.top, page.keep_scroll = 160, True
        evidence = {}
        fresh = xhs._prepare_current_page(page, dict(self.request, fresh_search=True), evidence, FakeBudget())
        self.assertEqual(sum(call[0] == "goto" for call in page.calls), 1)
        self.assertNotEqual(first["results"][0]["id"], fresh["results"][0]["id"])
        self.assertEqual(fresh["scroll"]["top"], 0)
        self.assertTrue(evidence["fresh_search_navigation"])
        self.assertNotIn("preparation_problem", evidence)
        page = FakePage("bili")
        _, first = self.bili_capture(page)
        self.assertFalse(any(call[0] == "goto" for call in page.calls))
        page.top, page.keep_scroll = 160, True
        _, fresh = self.bili_capture(page, fresh_search=True)
        self.assertEqual(sum(call[0] == "goto" for call in page.calls), 1)
        self.assertEqual(first[0][:2], (0, 0))
        self.assertEqual(fresh[0][:2], (1, 0))
        self.assertNotEqual(first[0][2], fresh[0][2])

    def test_suggestions_retry_refocus_clear_refill_without_results_search(self):
        for platform in ("xhs", "bili"):
            for fresh in (False, True):
                with self.subTest(platform=platform, fresh=fresh):
                    page = FakePage(platform, "suggestions")
                    req = dict(self.request, capture_kind="suggestions", fresh_search=fresh)
                    if platform == "xhs":
                        state = xhs._prepare_current_page(page, req, {}, FakeBudget())
                        self.assertEqual(state["suggestions"][0]["text"], "测试词列表")
                    else:
                        report, observed = self.bili_capture(page, capture_kind="suggestions", fresh_search=fresh)
                        self.assertEqual(observed, [])
                        self.assertIn("fixture_stop_before_screenshot", report["reason"])
                    self.assertEqual([call[1] for call in page.calls if call[0] == "fill"],
                                     ["", "测试词"] if fresh else [])
                    self.assertTrue(page.panel)
                    self.assertFalse(any(call[0] in ("goto", "press") for call in page.calls))
        # 清空后旧列表仍挂着时，不能把同词旧内容当作新展开成功。
        for platform in ("xhs", "bili"):
            page = FakePage(platform, "suggestions")
            original_fill = page.fill

            def stale_fill(value, **kwargs):
                original_fill(value, **kwargs)
                page.panel = True

            page.fill = stale_fill
            if platform == "xhs":
                evidence = {}
                xhs._prepare_current_page(page, dict(self.request, capture_kind="suggestions", fresh_search=True),
                                          evidence, FakeBudget())
                self.assertEqual(evidence["preparation_problem"], "suggestion_reset_not_confirmed")
            else:
                report, observed = self.bili_capture(page, capture_kind="suggestions", fresh_search=True)
                self.assertEqual(report["reason"], "suggestion_reset_not_confirmed")
                self.assertFalse(report["retryable"])
                self.assertEqual(observed, [])
            self.assertEqual([call for call in page.calls if call[0] == "fill"], [("fill", "")])

    def test_blank_rebuilt_session_suggestions_retry_initializes_once_without_results(self):
        """worker重建后的空Page允许一次入口初始化，然后清空重填，不先采TOP20。"""
        for platform in ("xhs", "bili"):
            with self.subTest(platform=platform):
                page = FakePage(platform, "suggestions")
                page.url, page.keyword, page.panel = "about:blank", "", False
                if platform == "xhs":
                    evidence = {}
                    with patch.object(xhs, "_collect", side_effect=AssertionError("禁止先采TOP20")), \
                            patch.object(xhs, "_collect_fullpage", side_effect=AssertionError("禁止先采全页结果")):
                        state = xhs._prepare_current_page(
                            page, dict(self.request, capture_kind="suggestions", fresh_search=True),
                            evidence, FakeBudget())
                    self.assertNotIn("preparation_problem", evidence)
                    self.assertEqual(evidence["navigation_attempts"], 1)
                    self.assertEqual(state["suggestions"][0]["text"], "测试词列表")
                else:
                    report, observed = self.bili_capture(page, capture_kind="suggestions", fresh_search=True)
                    self.assertEqual(observed, [])
                    self.assertIn("fixture_stop_before_screenshot", report["reason"])
                self.assertEqual(sum(call[0] == "goto" for call in page.calls), 1)
                self.assertEqual([call[1] for call in page.calls if call[0] == "fill"], ["", "测试词"])
                self.assertTrue(page.panel)
                self.assertFalse(any(call[0] == "press" for call in page.calls))

    def test_blocked_retry_stops_before_navigation_or_input(self):
        for platform in ("xhs", "bili"):
            page = FakePage(platform)
            page.blocked = True
            if platform == "xhs":
                state = xhs._prepare_current_page(page, dict(self.request, fresh_search=True), {}, FakeBudget())
                self.assertEqual(xhs._block(state), "login_required")
            else:
                report, observed = self.bili_capture(page, fresh_search=True)
                self.assertEqual(report["status"], "blocked")
                self.assertIs(report["retryable"], False)
                self.assertEqual(observed, [])
            self.assertEqual(page.calls, [])

    def test_80_percent_steps_and_tail_limit(self):
        state = dict(viewport=dict(height=1000), scroll=dict(viewport_height=1000),
                     results=[dict(id="tail", rect=[0, 990, 200, 30])])
        self.assertEqual(xhs._next_scroll_step(state, {}, 20), 800)
        pending = dict(items=[dict(id="tail", counted=True, screenshot_covered=False)])
        self.assertEqual(xhs._next_scroll_step(state, pending, 1), 116)
        state["results"][0]["rect"][1] = 900
        self.assertEqual(xhs._next_scroll_step(state, pending, 1), 0)
        self.assertIn("*.8", xhs.SCROLL_JS)
        self.assertNotIn("*.55", xhs.SCROLL_JS)
        scene = dict(scroll_y=0, viewport=dict(height=1000))
        self.assertEqual(results._next_scroll_y(scene, 5000), 800)
        self.assertEqual(results._next_scroll_y(scene, 1100), 100)

    def test_bili_ready_collection_keeps_native_pixels_and_short_stable_pairs(self):
        page, budget, records = FakePage("bili"), FakeBudget(), []
        with tempfile.TemporaryDirectory() as run:
            result = results.collect_results(page, self.request, Path(run), budget, page.scene,
                                             bili.classify_page, records.append)
            self.assertTrue(result["coverage"]["pages_verified"], result["coverage"])
            self.assertTrue(result["coverage"]["pixel_integrity_verified"])
            self.assertEqual(len(records), 1)
            frames = records[0]["viewport_frames"]
            self.assertEqual([frame["before"]["scroll_y"] for frame in frames], [0, 80, 148])
            self.assertLessEqual(max(call[1] for call in page.calls if call[0] == "scroll"), 148)
            with Image.open(records[0]["source"]["path"]) as composed:
                for frame in frames:
                    with Image.open(frame["source"]["path"]) as original:
                        self.assertEqual(original.crop(frame["source_box"]).tobytes(),
                                         composed.crop(frame["destination_box"]).tobytes())
        self.assertTrue(budget.pauses)
        self.assertEqual(set(budget.pauses), {60})

    def test_retryability_is_transient_only_and_identity_wins(self):
        for unknown in (False, True):
            page = FakePage("bili")
            page.unknown = unknown
            with patch.object(bili, "_dismiss_ordinary_tip_once"), \
                    patch.object(bili._Budget, "pause", lambda *args: None), \
                    patch.object(bili, "collect_results", side_effect=results.PageState(
                        "partial", "content_load_timeout", page.scene())):
                report = bili.capture_from_page(page, self.request)
            self.assertIs(report["retryable"], not unknown)
        for issues, expected in (([], True), (["ambiguous_note_identity"], False),
                                 (["rank_order_changed"], False)):
            report = xhs._base_result()
            report.update(status="partial", capture_kind="results", reason="scroll_stalled_without_complete_evidence",
                          issues=issues)
            xhs._checkpoint(Path(self.temp.name), report, {})
            self.assertIs(report["retryable"], expected)


if __name__ == "__main__":
    unittest.main()
