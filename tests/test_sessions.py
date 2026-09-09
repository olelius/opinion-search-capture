"""仅验证新增会话接口；浏览器全部 mock，真实进程仅为本地睡眠替身。"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from opinion_capture import sessions, web_bilibili, web_xiaohongshu
from opinion_capture.locks import ResourceBusy, ResourceLock


ROOT = Path(__file__).resolve().parents[1] / ".local" / "sessions-tests"
CLOSED = {"status": "success", "reason": "manual_window_closed_authentication_not_asserted",
          "outcome": "manual_session_closed"}


def _tree_worker(gate, payload, *, orphan=False):
    """spawn 的本地替身：父/孙实际 PID 落盘，晚写入探针证明停止后无后代活动。"""
    if os.name != "nt":
        os.setsid()
    gate.wait()
    run = Path(payload["run"])
    (run / "parent.pid").write_text(str(os.getpid()), encoding="ascii")
    code = ("import os,time;from pathlib import Path;"
            f"Path({str(run / 'child.pid')!r}).write_text(str(os.getpid()));"
            "time.sleep(2);"
            f"Path({str(run / 'late-marker')!r}).write_text('still alive');time.sleep(60)")
    # 故意使用 sys.executable，覆盖 Windows venvredirector 再生的真实孙进程。
    subprocess.Popen([sys.executable, "-B", "-c", code], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    deadline = time.monotonic() + 5
    while not (run / "child.pid").is_file():
        if time.monotonic() > deadline:
            raise RuntimeError("local_child_not_ready")
        time.sleep(.01)
    if orphan:
        sessions._write_json(run / "worker-result.json", CLOSED)
        return
    time.sleep(60)


def _orphan_worker(gate, payload):
    """模拟驱动已返回但留有后代，检查正常返回同样回收 Job。"""
    _tree_worker(gate, payload, orphan=True)


class SessionFixture(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="sessions-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = SimpleNamespace(output_root=str(self.root / "exports"), browser_profile_root=None)
        self.app_root = patch.object(sessions.paths, "application_root", return_value=self.root)
        self.app_root.start()
        self.addCleanup(self.app_root.stop)

    def payload(self):
        """所有实际文件仅在 .local/sessions-tests 的本次子目录中。"""
        run = self.root / "run"
        run.mkdir()
        return dict(run=str(run), platform="bilibili", profile_dir=str(self.root / "profile"),
                    cancel_file=str(run / "cancel.request"), timeout_seconds=5,
                    deadline=time.monotonic() + 5)


class PathTests(SessionFixture):
    def test_default_is_pure_and_independent_of_export_root(self):
        profile = sessions.profile_directory(self.plan, "bilibili")
        self.assertEqual(profile, self.root / ".local/browser-profiles/bilibili")
        self.plan.output_root = str(self.root / "other-exports")
        self.assertEqual(profile, sessions.profile_directory(self.plan, "bilibili"))
        del self.plan.browser_profile_root
        self.assertEqual(profile, sessions.profile_directory(self.plan, "bilibili"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_custom_root_and_platform_are_separate(self):
        self.plan.browser_profile_root = str(self.root / "private")
        for platform in sessions.PLATFORMS:
            self.assertEqual(sessions.profile_directory(self.plan, platform), self.root / "private" / platform)
        self.assertFalse((self.root / "private").exists())

    def test_rejects_traversal_relative_general_browser_and_export_overlap(self):
        for platform in ("../bilibili", "B站", "", None):
            with self.assertRaises(ValueError):
                sessions.profile_directory(self.plan, platform)
        for root in ("relative", str(self.root / "Google/Chrome/User Data"),
                     str(self.root / "Microsoft/Edge/User Data"), str(self.root / "exports")):
            self.plan.browser_profile_root = root
            with self.subTest(root=root), self.assertRaises(ValueError):
                sessions.profile_directory(self.plan, "bilibili")
        self.plan.browser_profile_root = str(self.root / "private")
        self.plan.output_root = str(self.root / "private/bilibili/images")
        with self.assertRaises(ValueError):
            sessions.profile_directory(self.plan, "bilibili")
        self.assertEqual(list(self.root.iterdir()), [])


class InterfaceTests(SessionFixture):
    def test_pre_cancel_and_bad_timeout_do_not_launch_or_write(self):
        event = threading.Event()
        event.set()
        with patch.object(sessions, "_run_login") as run:
            self.assertEqual(sessions.open_web_login(self.plan, "bilibili", cancel_event=event)["status"], "cancelled")
            for timeout in (0, -1, True, float("nan"), float("inf"), 1801, "20"):
                self.assertEqual(sessions.open_web_login(self.plan, "bilibili", timeout_seconds=timeout)["status"], "failed")
            run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_busy_uses_runner_key_immediately_and_releases_after_call(self):
        profile = sessions.profile_directory(self.plan, "bilibili")
        key = "browser-profile:" + str(profile).casefold()
        started = time.monotonic()
        with ResourceLock(key), patch.object(sessions, "_run_login") as run:
            result = sessions.open_web_login(self.plan, "bilibili")
            run.assert_not_called()
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual((result["status"], result["outcome"], result["busy"]), ("blocked", "busy", True))
        self.assertFalse(profile.exists())
        self.assertIsNone(result["log_path"])

    def test_interface_filters_cookie_fields_and_keeps_diagnostics_local(self):
        def fake(payload, event):
            key = "browser-profile:" + payload["profile_dir"].casefold()
            with self.assertRaises(ResourceBusy):
                with ResourceLock(key):
                    pass
            return CLOSED | {"cookies": "never returned", "artifact_paths": ["not exported"]}
        with patch.object(sessions, "_run_login", side_effect=fake):
            result = sessions.open_web_login(self.plan, "bilibili")
        self.assertFalse(result["authentication_verified"])
        self.assertEqual(result["outcome"], "manual_session_closed")
        self.assertNotIn("cookies", result)
        self.assertNotIn("artifact_paths", result)
        self.assertFalse(Path(self.plan.output_root).exists())
        self.assertFalse(Path(result["profile_dir"]).exists())
        log = Path(result["log_path"])
        self.assertTrue(log.is_relative_to(self.root / ".local/session-log"))
        self.assertEqual(json.loads(log.read_text(encoding="utf-8")), result)
        with ResourceLock("browser-profile:" + result["profile_dir"].casefold()):
            pass

    def test_failure_releases_lock_without_touching_existing_profile(self):
        profile = sessions.profile_directory(self.plan, "bilibili")
        profile.mkdir(parents=True)
        marker = profile / "existing-session"
        marker.write_bytes(b"preserve-this")
        with patch.object(sessions, "_run_login", side_effect=RuntimeError("fixture startup failure")):
            result = sessions.open_web_login(self.plan, "bilibili")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(marker.read_bytes(), b"preserve-this")
        with ResourceLock("browser-profile:" + str(profile).casefold()):
            pass

    def test_log_export_overlap_is_rejected_before_launch(self):
        self.plan.output_root = str(self.root / ".local/session-log/exported")
        with patch.object(sessions, "_run_login") as run:
            result = sessions.open_web_login(self.plan, "bilibili")
            run.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(list(self.root.iterdir()), [])


class XiaohongshuTests(SessionFixture):
    def test_public_open_login_receives_same_profile_cancel_file_and_remaining_budget(self):
        with patch.object(web_xiaohongshu, "open_login", return_value=CLOSED) as login, \
                patch.object(sessions, "_supervise_login") as second_supervisor:
            result = sessions.open_web_login(self.plan, "xiaohongshu", timeout_seconds=20)
        second_supervisor.assert_not_called()
        args, kwargs = login.call_args
        self.assertEqual(args, (result["profile_dir"],))
        self.assertEqual(Path(kwargs["output_dir"]), Path(result["diagnostic_dir"]))
        self.assertEqual(Path(kwargs["cancel_file"]), Path(result["diagnostic_dir"]) / "cancel.request")
        self.assertGreater(kwargs["timeout_seconds"], 0)
        self.assertLessEqual(kwargs["timeout_seconds"], 20)
        self.assertFalse(any(t.name == "web-session-cancel-bridge" for t in threading.enumerate()))

    def test_cancel_bridge_is_joined_and_manual_close_never_overrides_cancel(self):
        event = threading.Event()
        def fake(_profile, **kwargs):
            event.set()
            until = time.monotonic() + 2
            while not Path(kwargs["cancel_file"]).exists() and time.monotonic() < until:
                time.sleep(.01)
            self.assertTrue(Path(kwargs["cancel_file"]).exists())
            return CLOSED
        with patch.object(web_xiaohongshu, "open_login", side_effect=fake):
            result = sessions.open_web_login(self.plan, "xiaohongshu", cancel_event=event)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(any(t.name == "web-session-cancel-bridge" for t in threading.enumerate()))

    def test_timeout_bridge_and_exception_both_leave_no_watcher(self):
        def fake(_profile, **kwargs):
            until = time.monotonic() + 2
            while not Path(kwargs["cancel_file"]).exists() and time.monotonic() < until:
                time.sleep(.01)
            self.assertTrue(Path(kwargs["cancel_file"]).exists())
            return {"status": "cancelled", "outcome": "cancelled"}
        with patch.object(web_xiaohongshu, "open_login", side_effect=fake):
            result = sessions.open_web_login(self.plan, "xiaohongshu", timeout_seconds=.1)
        self.assertEqual((result["status"], result["outcome"]), ("failed", "timeout"))
        with patch.object(web_xiaohongshu, "open_login", side_effect=RuntimeError("fixture")):
            self.assertEqual(sessions.open_web_login(self.plan, "xiaohongshu")["status"], "failed")
        self.assertFalse(any(t.name == "web-session-cancel-bridge" for t in threading.enumerate()))


class BilibiliTests(SessionFixture):
    @contextmanager
    def browser_fixture(self):
        """所有驱动对象均为 mock，页面等待只触发假 close 回调。"""
        payload = self.payload()
        playwright = MagicMock()
        context = playwright.chromium.launch_persistent_context.return_value
        page = MagicMock()
        context.pages = [page]
        def close(*_):
            context.on.call_args.args[1]()
        page.wait_for_timeout.side_effect = close
        with patch("playwright.sync_api.sync_playwright") as factory, \
                patch.object(web_xiaohongshu, "_browser_paths", return_value=[Path(sys.executable)]), \
                patch.dict(os.environ, {"OPINION_CAPTURE_BROWSER": sys.executable}):
            factory.return_value.__enter__.return_value = playwright
            yield payload, playwright, context, page, close

    def test_headful_marked_profile_and_manual_close_without_auth_assertion(self):
        with self.browser_fixture() as (payload, playwright, context, page, _):
            result = sessions._bilibili_login(payload)
            marker = Path(payload["profile_dir"]) / web_bilibili.PROFILE_MARKER
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["owner"], "opinion_capture.web_bilibili")
            self.assertFalse(playwright.chromium.launch_persistent_context.call_args.kwargs["headless"])
            self.assertFalse(playwright.chromium.launch_persistent_context.call_args.kwargs["accept_downloads"])
            self.assertEqual(result, CLOSED)
            self.assertEqual(page.goto.call_count, 1)
            page.fill.assert_not_called()
            context.cookies.assert_not_called()
            context.storage_state.assert_not_called()
            context.close.assert_called_once()

    def test_close_during_navigation_is_manual_close_not_login(self):
        with self.browser_fixture() as (payload, _, context, page, close):
            def navigation(*args, **kwargs):
                close()
                raise RuntimeError("fixture target closed")
            page.goto.side_effect = navigation
            self.assertEqual(sessions._bilibili_login(payload), CLOSED)
            context.close.assert_called_once()

    def test_existing_unmarked_profile_is_preserved_before_browser_start(self):
        with self.browser_fixture() as (payload, playwright, _, _, _):
            profile = Path(payload["profile_dir"])
            profile.mkdir()
            (profile / "Preferences").write_bytes(b"unchanged-fixture")
            with self.assertRaises(ValueError):
                sessions._bilibili_login(payload)
            playwright.chromium.launch_persistent_context.assert_not_called()
            self.assertEqual((profile / "Preferences").read_bytes(), b"unchanged-fixture")
            self.assertEqual(len(list(profile.iterdir())), 1)

    def test_explicit_invalid_browser_does_not_fall_back(self):
        with self.browser_fixture() as (payload, playwright, _, _, _):
            for value in ("", "relative-browser.exe", str(self.root / "missing.exe")):
                with self.subTest(value=value), patch.dict(os.environ, {"OPINION_CAPTURE_BROWSER": value}), \
                        self.assertRaises(ValueError):
                    sessions._bilibili_login(payload)
            playwright.chromium.launch_persistent_context.assert_not_called()


class ProcessTests(SessionFixture):
    def _assert_dead(self, pid):
        """Windows 使用真实进程句柄检查，不仅相信外层 redirector 的 poll。"""
        if os.name == "nt":
            import ctypes as c
            from ctypes import wintypes as w
            api = c.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes, api.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
            api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
            api.CloseHandle.argtypes = [w.HANDLE]
            handle = api.OpenProcess(0x00100000, False, pid)
            if handle:
                try:
                    self.assertEqual(api.WaitForSingleObject(handle, 0), 0, f"PID {pid} still active")
                finally:
                    api.CloseHandle(handle)
        else:
            status = Path(f"/proc/{pid}/stat")
            self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z")

    def _exercise(self, mode):
        event = threading.Event()
        stop = threading.Event()
        def cancel_when_child_started():
            while not stop.wait(.01):
                if list(self.root.glob(".local/session-log/*/child.pid")):
                    event.set()
                    return
        watcher = threading.Thread(target=cancel_when_child_started) if mode == "cancel" else None
        if watcher:
            watcher.start()
        try:
            with patch.object(sessions, "_worker_entry", _orphan_worker if mode == "orphan" else _tree_worker):
                result = sessions.open_web_login(self.plan, "bilibili", cancel_event=event,
                                                 timeout_seconds=.9 if mode == "timeout" else 8)
        finally:
            stop.set()
            if watcher:
                watcher.join()
        run = Path(result["diagnostic_dir"])
        for filename in ("parent.pid", "child.pid"):
            self.assertTrue((run / filename).is_file(), result)
            self._assert_dead(int((run / filename).read_text()))
        time.sleep(2.1)
        self.assertFalse((run / "late-marker").exists(), result)
        self.assertFalse(Path(result["profile_dir"]).exists())
        self.assertFalse(Path(self.plan.output_root).exists())
        with ResourceLock("browser-profile:" + result["profile_dir"].casefold()):
            pass
        return result

    def test_timeout_reaps_real_local_child_tree(self):
        result = self._exercise("timeout")
        self.assertEqual((result["status"], result["outcome"]), ("failed", "timeout"))

    def test_cancel_reaps_real_local_child_tree(self):
        result = self._exercise("cancel")
        self.assertEqual((result["status"], result["outcome"]), ("cancelled", "cancelled"))

    def test_normal_exit_reaps_orphan_descendant(self):
        self.assertEqual(self._exercise("orphan")["outcome"], "manual_session_closed")


if __name__ == "__main__":
    unittest.main()
