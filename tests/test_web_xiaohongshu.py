"""默认离线 unittest；不启动 Playwright、不访问平台。"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from opinion_capture.web_xiaohongshu import assess_snapshot, capture, classify_block, validate_request


class RequestTests(unittest.TestCase):
    def base(self, **changes):
        return dict(dict(keyword="零跑", capture_kind="results", output_dir=str(Path.cwd())), **changes)

    def test_defaults(self):
        req = validate_request(self.base())
        self.assertEqual(req["top_n"], 20)
        self.assertEqual(req["timeout_seconds"], 180)

    def test_invalid_requests(self):
        invalid = [None, {}, self.base(keyword=" "), self.base(capture_kind="APP"),
                   self.base(top_n=True), self.base(top_n=0), self.base(top_n=1.5),
                   self.base(timeout_seconds=True), self.base(timeout_seconds=float("nan")),
                   self.base(timeout_seconds=float("inf")), self.base(timeout_seconds=0),
                   self.base(output_dir="relative"), self.base(user_data_dir="relative")]
        for req in invalid:
            with self.subTest(req=req), self.assertRaises(ValueError):
                validate_request(req)

    def test_invalid_capture_serializable(self):
        result = capture({})
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["observed_count"])
        self.assertEqual(result["artifact_paths"], [])
        json.dumps(result, allow_nan=False)

    def test_input_is_not_modified(self):
        original = self.base(keyword=" 零跑 ")
        normalized = validate_request(original)
        self.assertEqual(original["keyword"], " 零跑 ")
        self.assertEqual(normalized["keyword"], "零跑")

    def test_output_failure_still_returns_dict(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("offline test")):
            result = capture(self.base())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["artifact_paths"], [])

    def test_results_contains_contract_and_timezone(self):
        from datetime import datetime
        result = capture({})
        expected = {"platform", "client", "capture_kind", "keyword", "status", "reason",
                    "requested_top_n", "observed_count", "rank_verified", "artifact_paths",
                    "captured_at", "source_url"}
        self.assertTrue(expected <= set(result))
        self.assertTrue({"primary_artifact_path", "source_artifact_paths", "items", "rank_policy",
                         "outcome", "captured_at_range", "started_at", "finished_at"} <= set(result))
        self.assertIsNotNone(datetime.fromisoformat(result["captured_at"]).utcoffset())


class EvidenceTests(unittest.TestCase):
    def test_navigation_login_is_not_wall(self):
        self.assertIsNone(classify_block("首页 登录 发布 搜索"))

    def test_blocks(self):
        for text, expected in [("请完成验证", "captcha_required"), ("登录后搜索", "login_required"),
                               ("网络环境存在风险", "access_restricted")]:
            self.assertEqual(classify_block(text), expected)
        self.assertEqual(classify_block("", login_modal=True), "login_required")
        self.assertEqual(classify_block("", http_status=429), "http_429")

    def test_observed_risk_redirect_before_text_render(self):
        url = "https://www.xiaohongshu.com/website-login/error?error_code=300012"
        self.assertEqual(classify_block("", url), "access_restricted")
        self.assertEqual(classify_block("安全限制\nIP存在风险，请切换可靠网络环境后重试\n300012"), "access_restricted")

    def test_unknown_platform_error_not_target(self):
        self.assertEqual(classify_block("", "https://www.xiaohongshu.com/website-login/error?error_code=999"), "platform_access_error")

    def test_login_url(self):
        self.assertEqual(classify_block("", "https://www.xiaohongshu.com/login"), "login_required")

    def state(self, **changes):
        return dict(dict(url="https://www.xiaohongshu.com/search_result?keyword=零跑", keyword="零跑", input_visible=True,
                         text="零跑", results=[{"id": "1"}, {"id": "2"}, {"id": "3"}],
                         suggestions=[{"text": "零跑汽车"}], pending_images=0, suggestion_panel_complete=True,
                         suggestion_panel_associated=True), **changes)

    def assess(self, state=None, kind="results", stable=True):
        return assess_snapshot(state or self.state(), dict(keyword="零跑", capture_kind=kind, top_n=3), stable=stable)

    def test_top_n_never_implies_rank(self):
        status, reason, count, verified = self.assess()
        self.assertEqual((status, count, verified), ("partial", 3, False))

    def test_suggestions_conditions(self):
        self.assertEqual(self.assess(kind="suggestions")[0], "success")
        self.assertEqual(self.assess(self.state(keyword="其他"), kind="suggestions")[0], "failed")
        self.assertEqual(self.assess(self.state(suggestions=[]), kind="suggestions")[0], "partial")
        self.assertEqual(self.assess(self.state(pending_images=1), kind="suggestions")[0], "partial")

    def test_suggestion_partial_panel_not_success(self):
        self.assertEqual(self.assess(self.state(suggestion_panel_complete=False), kind="suggestions")[0], "partial")

    def test_source_validation(self):
        for url in ("https://www.xiaohongshu.com.evil.test/search_result", "about:blank", "http://www.xiaohongshu.com/search_result"):
            self.assertEqual(self.assess(self.state(url=url))[0], "failed")
        self.assertEqual(self.assess(self.state(url="https://www.xiaohongshu.com/explore"))[0], "failed")

    def test_dom_changed_has_unknown_count(self):
        result = self.assess(stable=False)
        self.assertEqual((result[0], result[2]), ("partial", None))

    def test_blocked_overrides_target(self):
        result = self.assess(self.state(login_modal=True))
        self.assertEqual(result, ("blocked", "login_required", None, False))


if __name__ == "__main__":
    unittest.main()
