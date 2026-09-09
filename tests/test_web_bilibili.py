"""B站 WEB 离线测试：只验证请求、页面分类和证据决策，不访问平台。"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opinion_capture.web_bilibili import (
    LOCAL_ROOT, PROFILE_MARKER, _fingerprint, _launch, _prepare_profile,
    assess_capture, build_source_url, capture, classify_page, validate_request,
)


def request(kind="results", **changes):
    """构造纯函数输入；此路径不会在纯函数测试中写入。"""
    return dict(keyword="零跑", capture_kind=kind,
                output_dir=str(LOCAL_ROOT / "test-output"), **changes)


def scene(kind="results", **changes):
    """结构化证据样本，仅用于离线分类，不作为平台页面或截图产物。"""
    result = dict(url=build_source_url("零跑", kind), title="零跑-哔哩哔哩_bilibili",
                  body="登录\n登录后你可以：免费看高清视频\n立即登录\n零跑",
                  http_status=200, input_value="零跑", input_unobscured=True,
                  challenge_visible=False, login_required_visible=False,
                  items=[dict(id="BVtest001", title="零跑试驾", images_loaded=True)])
    if kind == "suggestions":
        result["items"] = [dict(text="零跑a10", unobscured=True), dict(text="零跑c10", unobscured=True)]
    result.update(changes)
    return result


class RequestTests(unittest.TestCase):
    def test_defaults_and_no_mutation(self):
        original = request()
        normalized = validate_request(original)
        self.assertEqual(normalized["top_n"], 20)
        self.assertEqual(normalized["timeout_seconds"], 30)
        self.assertNotIn("top_n", original)

    def test_invalid_keywords(self):
        for keyword in (None, "", " ", " 零跑", "零跑\n", 17):
            with self.subTest(keyword=keyword), self.assertRaises(ValueError):
                validate_request({**request(), "keyword": keyword})

    def test_invalid_n(self):
        for n in (0, -1, True, "3", 2.5):
            with self.subTest(n=n), self.assertRaises(ValueError):
                validate_request(request(top_n=n))

    def test_invalid_timeout(self):
        for seconds in (0, -1, float("nan"), float("inf"), True, "30"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                validate_request(request(timeout_seconds=seconds))

    def test_paths_and_kind(self):
        for field, value in (("output_dir", "relative"), ("output_dir", None),
                             ("user_data_dir", "relative"), ("capture_kind", "other")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_request({**request(), field: value})

    def test_url_encoding(self):
        from urllib.parse import parse_qs, urlsplit
        keyword = "零跑 & A+1"
        self.assertEqual(parse_qs(urlsplit(build_source_url(keyword, "results")).query)["keyword"], [keyword])
        self.assertNotIn("?", build_source_url(keyword, "suggestions"))

    def test_invalid_capture_is_json_without_browser(self):
        for invalid in (None, {}, {"keyword": object()}, {**request(), "top_n": object()}):
            result = capture(invalid)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["artifact_paths"], [])
            json.dumps(result)


class ClassificationTests(unittest.TestCase):
    def test_normal_login_tip_is_not_blocked(self):
        self.assertEqual(classify_page(scene()), (None, ""))

    def test_challenge_and_login_modal(self):
        for flag in ("challenge_visible", "login_required_visible"):
            self.assertEqual(classify_page(scene(**{flag: True}))[0], "blocked")

    def test_http_blocking(self):
        for status in (401, 403, 412, 429):
            self.assertEqual(classify_page(scene(http_status=status))[0], "blocked")

    def test_http_error_not_success(self):
        self.assertEqual(classify_page(scene(http_status=503))[0], "failed")

    def test_short_blocking_pages(self):
        for body in ("安全验证", "访问过于频繁", "请先登录", "登录后才能搜索", "请完成滑动验证"):
            self.assertEqual(classify_page(scene(body=body, items=[]))[0], "blocked")

    def test_error_page(self):
        self.assertEqual(classify_page(scene(body="网络异常，请稍后重试", items=[]))[0], "failed")

    def test_video_title_not_challenge(self):
        self.assertEqual(classify_page(scene(body="零跑安全验证实测")), (None, ""))

    def test_wrong_source_and_login_redirect(self):
        for url in ("about:blank", "https://search.bilibili.com.example.com/all", "chrome-error://chromewebdata/"):
            self.assertEqual(classify_page(scene(url=url))[0], "failed")
        self.assertEqual(classify_page(scene(url="https://passport.bilibili.com/login"))[0], "blocked")


class AssessmentTests(unittest.TestCase):
    def assess(self, evidence=None, kind="results", **kwargs):
        return assess_capture(validate_request(request(kind, top_n=3)),
                              evidence or scene(kind), screenshot_saved=kwargs.get("png", True),
                              dom_stable=kwargs.get("stable", True))

    def test_results_without_coverage_do_not_claim_strict_rank(self):
        result = self.assess()
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["rank_verified"])
        self.assertIn("rank_unverified", result["reason"])
        self.assertIn("observed_below_requested", result["reason"])

    def test_result_ids_deduplicated(self):
        e = scene()
        e["items"] *= 4
        self.assertEqual(self.assess(e)["observed_count"], 1)

    def test_keyword_mismatch(self):
        self.assertEqual(self.assess(scene(input_value="其他"))["status"], "failed")

    def test_url_keyword_mismatch_precedes_partial(self):
        e = scene(url=build_source_url("其他", "results"), input_unobscured=False)
        self.assertEqual(self.assess(e)["reason"], "search_context_mismatch")

    def test_no_png_is_failed(self):
        self.assertEqual(self.assess(png=False)["status"], "failed")

    def test_no_results_is_failed(self):
        self.assertEqual(self.assess(scene(items=[]))["status"], "failed")

    def test_obscured_input_preserves_observed_count(self):
        result = self.assess(scene(input_unobscured=False))
        self.assertEqual(result["observed_count"], 1)
        self.assertIn("partly_obscured", result["reason"])

    def test_unloaded_images_and_changed_dom(self):
        e = scene()
        e["items"][0]["images_loaded"] = False
        result = self.assess(e, stable=False)
        self.assertIn("target_images_incomplete", result["reason"])
        self.assertIn("dom_changed", result["reason"])

    def test_suggestions_complete(self):
        result = self.assess(kind="suggestions")
        self.assertEqual((result["status"], result["observed_count"]), ("success", 2))
        self.assertFalse(result["rank_verified"])

    def test_suggestions_empty_is_not_success(self):
        result = self.assess(scene("suggestions", items=[]), "suggestions")
        self.assertEqual((result["status"], result["observed_count"]), ("partial", 0))

    def test_suggestions_changed_or_occluded(self):
        self.assertEqual(self.assess(kind="suggestions", stable=False)["status"], "partial")
        for e in (scene("suggestions", input_unobscured=False),
                  scene("suggestions", items=[dict(text="零跑", unobscured=False)])):
            self.assertEqual(self.assess(e, "suggestions")["status"], "partial")

    def test_duplicates_not_success(self):
        e = scene("suggestions")
        e["items"] *= 2
        self.assertEqual(self.assess(e, "suggestions")["status"], "partial")

    def test_blocked_never_passes_with_cards_and_png(self):
        self.assertEqual(self.assess(scene(challenge_visible=True))["status"], "blocked")

    def test_fingerprint_tracks_card_order_and_positions(self):
        before = scene()
        after = copy.deepcopy(before)
        after["items"][0]["rect"] = {"y": 300}
        self.assertNotEqual(_fingerprint(before), _fingerprint(after))


class IsolationTests(unittest.TestCase):
    def test_profile_guard_and_reuse(self):
        local = LOCAL_ROOT / "goal-offline-tests"
        local.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as directory:
            profile = Path(directory) / "profile"
            _prepare_profile(profile)
            self.assertTrue((profile / PROFILE_MARKER).is_file())
            _prepare_profile(profile)
            other = Path(directory) / "unrelated-profile"
            other.mkdir()
            (other / "user-file").write_text("preserve", encoding="utf-8")
            with self.assertRaises(ValueError):
                _prepare_profile(other)
            self.assertEqual((other / "user-file").read_text(), "preserve")

    def test_launch_max_two_attempts_without_network(self):
        from unittest.mock import patch
        pw = Mock()
        pw.chromium.executable_path = str(LOCAL_ROOT / "mock-chromium.exe")
        pw.chromium.launch_persistent_context.side_effect = RuntimeError("startup failed")
        with patch.object(Path, "is_file", return_value=True):
            with self.assertRaises(RuntimeError):
                _launch(pw, LOCAL_ROOT / "test-profile", LOCAL_ROOT / "test-temp", lambda cap: cap)
        self.assertEqual(pw.chromium.launch_persistent_context.call_count, 2)


if __name__ == "__main__":
    unittest.main()
