"""预算/取消/手动入口的本机进程验证；替身子进程不启动浏览器、不联网。"""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from opinion_capture import web_xiaohongshu as adapter


ROOT = Path(__file__).resolve().parents[1]/".local/web_xiaohongshu/goal-20260908-complete"


class SupervisorTests(unittest.TestCase):
    def run_helper(self, *, cancel=False):
        """仅将内部 worker 命令换成睡眠替身，其它清理命令仍真实执行。"""
        directory = ROOT/("control-"+uuid4().hex[:10])
        directory.mkdir(parents=True)
        flag = directory/"cancel"
        real_popen = subprocess.Popen
        processes = []

        def launch(args, **kwargs):
            if "--worker" in args:
                args = [sys.executable, "-c", "import time; time.sleep(60)"]
                process = real_popen(args, **kwargs)
                processes.append(process)
                return process
            return real_popen(args, **kwargs)
        timer = threading.Timer(.5, flag.touch) if cancel else None
        if timer:
            timer.start()
        start = time.monotonic()
        try:
            with patch.object(adapter.subprocess, "Popen", side_effect=launch):
                result = adapter.capture(dict(keyword="零跑", capture_kind="results", output_dir=str(directory),
                    timeout_seconds=10 if cancel else .5, cancel_file=str(flag)))
        finally:
            if timer:
                timer.cancel()
                timer.join()
        elapsed = time.monotonic()-start
        self.assertLess(elapsed, 4)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        manifest = next(Path(p) for p in result["artifact_paths"] if Path(p).name == 'evidence.json')
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["result"], result)
        (directory/"verification.json").write_text(json.dumps(dict(
            evidence_source="offline_sleep_process_not_browser", elapsed_seconds=elapsed,
            process_reaped=True, result=result), ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def test_total_deadline_interrupts_stalled_driver(self):
        result = self.run_helper()
        self.assertEqual((result["status"], result["outcome"]), ("partial", "timeout"))
        self.assertFalse(result["rank_verified"])

    def test_cancel_interrupts_stalled_driver(self):
        result = self.run_helper(cancel=True)
        self.assertEqual((result["status"], result["outcome"]), ("cancelled", "cancelled"))

    def test_real_worker_entry_rejects_non_dedicated_profile_before_browser(self):
        directory = ROOT/("worker-entry-"+uuid4().hex[:10])
        profile = directory/"ordinary-profile"
        profile.mkdir(parents=True)
        (profile/"Preferences").write_text("offline fixture, not a real browser profile", encoding="utf-8")
        result = adapter.capture(dict(keyword="零跑", capture_kind="results", output_dir=str(directory),
                                      user_data_dir=str(profile), timeout_seconds=5))
        self.assertEqual(result["status"], "failed")
        self.assertIn("profile_not_dedicated", result["reason"])
        self.assertEqual(result["source_artifact_paths"], [])


class WorkerControlTests(unittest.TestCase):
    @contextmanager
    def worker_fixture(self):
        """为启动/导航协议提供替身；不创建真实浏览器。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            request = adapter.validate_request(dict(keyword="零跑", capture_kind="results", output_dir=tmp,
                                                   user_data_dir=str(run/"profile"), timeout_seconds=5))
            pw = MagicMock()
            context = pw.chromium.launch_persistent_context.return_value
            page = MagicMock()
            context.pages = [page]
            page.goto.return_value.status = 200
            page.url = "https://www.xiaohongshu.com/explore"
            with patch.object(adapter, "_local_playwright") as factory, patch.object(adapter, "_browser_paths", return_value=[Path(__file__)]):
                factory.return_value.__enter__.return_value = pw
                yield request, run, pw, page

    def test_manual_worker_visible_one_navigation_no_auth_claim(self):
        with self.worker_fixture() as (request, run, pw, page):
            page.is_closed.side_effect = [False, True]
            state = dict(url=page.url, text="扫码登录", login_modal=True)
            with patch.object(adapter, "_snapshot", return_value=state), patch.object(adapter.Budget, "pause"):
                result = adapter._worker(request, run, adapter._base_result(), mode="login")
            self.assertFalse(pw.chromium.launch_persistent_context.call_args.kwargs["headless"])
            self.assertEqual(page.goto.call_count, 1)
            page.screenshot.assert_not_called()
            self.assertEqual(result["outcome"], "manual_session_closed")
            self.assertIn("authentication_not_asserted", result["reason"])

    def test_manual_navigation_keeps_window_and_does_not_repeat_goto(self):
        """手动登录跳转后等新DOM；窗口实际关闭前不中止，不以跳转断言已登录。"""
        with self.worker_fixture() as (request, run, pw, page):
            page.is_closed.side_effect = [False, False, False, True]
            states = [RuntimeError("Execution context was destroyed, most likely because of a navigation"),
                      dict(url=page.url, text="扫码登录", login_modal=True)]
            with patch.object(adapter, "_snapshot", side_effect=states), patch.object(adapter.Budget, "pause"):
                result = adapter._worker(request, run, adapter._base_result(), mode="login")
            self.assertEqual(page.goto.call_count, 1)
            self.assertEqual(result["outcome"], "manual_session_closed")
            self.assertIn("authentication_not_asserted", result["reason"])

    def test_manual_already_closed_context_keeps_completion(self):
        """用户关闭窗口后重复close不覆盖已记录结果，也不宣称认证成功。"""
        with self.worker_fixture() as (request, run, pw, page):
            page.is_closed.side_effect = [False, True]
            context = pw.chromium.launch_persistent_context.return_value
            context.close.side_effect = RuntimeError("Target page, context or browser has been closed")
            with patch.object(adapter, "_snapshot", return_value=dict(url=page.url, text="扫码登录", login_modal=True)), patch.object(adapter.Budget, "pause"):
                result = adapter._worker(request, run, adapter._base_result(), mode="login")
            self.assertEqual(result["outcome"], "manual_session_closed")
            self.assertEqual(result["status"], "success")
            self.assertFalse(result["rank_verified"])

    def test_block_stays_blocked_if_diagnostic_screenshot_times_out(self):
        with self.worker_fixture() as (request, run, pw, page):
            state = dict(url="https://www.xiaohongshu.com/website-login/error?error_code=300012", text="", input_visible=False)
            page.screenshot.side_effect = TimeoutError("offline fixture deadline")
            with patch.object(adapter, "_snapshot", return_value=state):
                result = adapter._worker(request, run, adapter._base_result())
            self.assertEqual((result["status"], result["reason"]), ("blocked", "access_restricted"))
            self.assertEqual(page.goto.call_count, 1)
            self.assertIsNone(result["primary_artifact_path"])

    def test_manual_risk_does_not_wait_for_user_or_retry(self):
        with self.worker_fixture() as (request, run, pw, page):
            page.is_closed.return_value = False
            state = dict(url=page.url, text="请完成验证")
            with patch.object(adapter, "_snapshot", return_value=state), patch.object(adapter.Budget, "pause") as pause:
                result = adapter._worker(request, run, adapter._base_result(), mode="login")
            pause.assert_not_called()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(page.goto.call_count, 1)


if __name__ == "__main__":
    unittest.main()
