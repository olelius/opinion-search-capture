"""滚动加载故障的有限重搜与证据隔离；纯本地替身，不访问平台。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from opinion_capture.config import Plan
from opinion_capture.existing_browser import _Controller
from opinion_capture.runner import _retry_allowed, run_plan
from opinion_capture.storage import Store
from tests.test_runner import fixture_collector


class ScrollRetryRunnerTests(unittest.TestCase):
    def plan(self, root, **changes):
        values = dict(output_root=root, keywords=["A10"], platforms=["xiaohongshu"],
                      capture_kinds=["results"], retry_delay_seconds=0, composite_cell_width=64)
        return Plan(**(values | changes))

    def test_loading_partial_without_primary_restarts_and_isolates_frames(self):
        for client in ("APP", "WEB"):
            with self.subTest(client=client), tempfile.TemporaryDirectory() as root:
                calls = []
                def capture(request):
                    calls.append(dict(request))
                    result = fixture_collector(request)
                    if len(calls) == 1:
                        result.update(status="partial", primary_artifact_path=None,
                                      reason="loading_timeout", retryable=True,
                                      observed_count=18, rank_verified=False)
                    return result
                report = run_plan(self.plan(root, clients=[client]), collector=capture)
                self.assertEqual(report["status"], "success")
                self.assertEqual([r["fresh_search"] for r in calls], [False, True])
                self.assertEqual([r["keyword"] for r in calls], ["A10", "A10"])
                self.assertEqual([r["attempt_number"] for r in calls], [1, 2])
                self.assertNotEqual(calls[0]["output_dir"], calls[1]["output_dir"])
                self.assertTrue((Path(calls[0]["output_dir"]) / "original.png").exists())
                final = report["results"][0]
                self.assertTrue(Path(final["primary_artifact_path"]).is_relative_to(calls[1]["output_dir"]))
                self.assertEqual(final["observed_count"], 20)
                with Store(Path(root) / ".capture-state/history.sqlite3") as store:
                    rows = store.db.execute("SELECT result_json FROM attempts ORDER BY number").fetchall()
                first, second = [json.loads(row[0]) for row in rows]
                self.assertEqual([first["status"], second["status"]], ["failed", "success"])
                self.assertTrue(first["retry_scheduled"])
                self.assertEqual(first["exports"], [])
                self.assertFalse(second["retry_scheduled"])

    def test_loading_retries_stop_at_configured_limit(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            def capture(request):
                calls.append(request)
                result = fixture_collector(request)
                result.update(status="failed", reason="capture_timeout", primary_artifact_path=None)
                return result
            report = run_plan(self.plan(root, clients=["WEB"], max_retries=1), collector=capture)
            self.assertEqual(len(calls), 2)
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["results"][0]["retry_exhausted"])
            self.assertFalse(report["composites"])

    def test_blocked_cancelled_success_and_unknown_evidence_are_not_retried(self):
        for status in ("success", "blocked", "cancelled"):
            self.assertFalse(_retry_allowed(dict(status=status, reason="capture_timeout", retryable=True)))
        self.assertFalse(_retry_allowed(dict(status="failed", reason="loading_timeout", retryable=False)))
        self.assertFalse(_retry_allowed(dict(status="partial", reason="rank_identity_unverified")))
        self.assertFalse(_retry_allowed(dict(status="failed", reason="invalid_platform")))
        self.assertTrue(_retry_allowed(dict(status="partial", reason="TimeoutError: Locator.click timed out")))

    def test_resume_restarts_only_failed_keyword(self):
        with tempfile.TemporaryDirectory() as root:
            plan = self.plan(root, clients=["WEB"], max_retries=0, keywords=["A1", "A10"])
            def capture(request):
                result = fixture_collector(request)
                if request["keyword"] == "A10":
                    result.update(status="failed", reason="loading_timeout", primary_artifact_path=None)
                return result
            first = run_plan(plan, collector=capture)
            calls = []
            def recovered(request):
                calls.append(request)
                return fixture_collector(request)
            second = run_plan(plan, collector=recovered, resume_batch_id=first["batch_id"])
            self.assertEqual(second["status"], "success")
            self.assertEqual([r["keyword"] for r in calls], ["A10"])
            self.assertTrue(calls[0]["fresh_search"])
            self.assertEqual(calls[0]["attempt_number"], 2)
            self.assertEqual(second["results"][0]["exports"], first["results"][0]["exports"])

    def test_cancel_during_retry_does_not_dispatch_next_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            event, calls = threading.Event(), []
            def capture(request):
                calls.append(request)
                result = fixture_collector(request)
                result.update(status="failed", reason="loading_timeout")
                return result
            def progress(record):
                if record["event"] == "task_retry":
                    event.set()
            report = run_plan(self.plan(root, clients=["WEB"]), collector=capture,
                              cancel_event=event, progress_callback=progress)
            self.assertEqual(len(calls), 1)
            self.assertEqual(report["status"], "cancelled")

    def test_browser_worker_deadline_returns_retryable_timeout(self):
        controller = _Controller()
        controller.process = Mock()
        request = dict(platform="xiaohongshu", client="WEB", capture_kind="results",
                       keyword="A10", top_n=20, timeout_seconds=1)
        with patch.object(controller, "_start"), patch.object(controller, "_stop") as stop, \
             patch("opinion_capture.existing_browser.time.monotonic", side_effect=[0, 2]):
            result = controller.capture(request)
        stop.assert_called_once()
        self.assertEqual(result["reason"], "browser_response_timeout")
        self.assertEqual(result["outcome"], "timeout")
        self.assertTrue(_retry_allowed(result))


if __name__ == "__main__":
    unittest.main()
