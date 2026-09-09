"""Android 适配器离线测试：不连接模拟器，不触发业务 UI 操作。"""
import io
import json
from pathlib import Path
import tempfile
import time
import subprocess
import sys
import os
import threading
import unittest
from unittest.mock import patch, MagicMock
from PIL import Image

from opinion_capture.android import AdbClient, AndroidBlocked, AndroidCancelled, capture, detect_blocker, parse_devices, select_device, discover_devices
from opinion_capture.android import _run_bounded, _worker_runtime


class AndroidTests(unittest.TestCase):
    def test_actual_worker_runtime_imports_venv_and_not_redirector(self):
        executable, env = _worker_runtime()
        if os.name == "nt":
            self.assertEqual(executable, sys._base_executable)
            self.assertIn(str(Path(sys.prefix) / "Lib" / "site-packages"), env["PYTHONPATH"])
        result = _run_bounded([executable, "-B", "-c", "import PIL,uiautomator2,opinion_capture.android;print('runtime_ok')"],
                              time.monotonic() + 8, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"runtime_ok", result.stdout)

    def test_actual_timeout_and_cancel_kill_delayed_device_action(self):
        # 真实子进程用延迟写文件替代设备动作：若只杀venv launcher，测试会写出标记并失败。
        base = Path(__file__).resolve().parents[1] / ".local/android/goal-tests"
        base.mkdir(parents=True, exist_ok=True)
        executable, env = _worker_runtime()
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory(dir=base) as tmp:
                action = Path(tmp) / "unexpected-device-action"
                ready = Path(tmp) / "ready"
                marker = Path(tmp) / "cancel"
                code = "from pathlib import Path;import time,sys;Path(sys.argv[1]).touch();time.sleep(.8);Path(sys.argv[2]).touch()"
                command = [executable, "-B", "-c", code, str(ready), str(action)]
                timer = None
                if cancel:
                    timer = threading.Timer(.25, marker.touch)
                    timer.start()
                try:
                    with self.assertRaises((AndroidCancelled, TimeoutError, subprocess.TimeoutExpired)):
                        _run_bounded(command, time.monotonic() + (5 if cancel else .25), str(marker) if cancel else None, env)
                finally:
                    if timer:
                        timer.join()
                self.assertTrue(ready.exists(), "确保实际worker曾开始执行，不把启动失败当停止成功")
                time.sleep(.9)
                self.assertFalse(action.exists(), "停止后仍执行了延迟设备动作")

    def test_gui_discovery_background_short_deadline(self):
        with patch("opinion_capture.android.AdbClient") as cls:
            cls.return_value.list_devices.return_value = [{"serial":"a","state":"device","model":"MuMu_15"}, {"serial":"b","state":"offline"}]
            result = discover_devices()
            self.assertEqual(result, [dict(name="MuMu_15", serial="a", state="device"), dict(name="b", serial="b", state="offline")])
            cls.return_value.list_devices.assert_called_once_with(refresh=True)
            self.assertLessEqual(cls.call_args.args[0] - time.monotonic(), 8)
            cls.return_value.discover.assert_not_called()

    def test_cancel_file_prevents_all_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "cancel"
            marker.touch()
            client = AdbClient(time.monotonic() + 1800, executable="adb", cancel_file=str(marker))
            with patch("opinion_capture.android.subprocess.run") as run:
                with self.assertRaises(AndroidCancelled):
                    client.run("devices", device=False)
                run.assert_not_called()
            with patch("opinion_capture.android.AdbClient") as adb:
                result = capture(dict(platform="bilibili", keyword="零跑", capture_kind="results", output_dir=tmp,
                                      timeout_seconds=1800, cancel_file=str(marker)))
                self.assertEqual(result["status"], "cancelled")
                self.assertIsNone(result["primary_artifact_path"])
                adb.assert_not_called()

    def test_cancel_path_must_be_absolute(self):
        result = capture(dict(platform="bilibili", keyword="词", capture_kind="results", cancel_file="relative"))
        self.assertIn("cancel_file_must_be_absolute", result["reason"])

    def test_discovery_and_explicit_serial(self):
        devices = parse_devices("List of devices attached\na device model:M\nb offline\n")
        self.assertEqual(select_device(devices, "a")["model"], "M")
        with self.assertRaisesRegex(AndroidBlocked, "multiple_devices"):
            select_device(devices)
        with self.assertRaisesRegex(AndroidBlocked, "offline"):
            select_device(devices, "b")

    def test_no_device_and_wrong_serial(self):
        with self.assertRaisesRegex(AndroidBlocked, "no_device"):
            select_device([])
        with self.assertRaisesRegex(AndroidBlocked, "serial_not_found"):
            select_device([{"serial": "a", "state": "device"}], "x")

    def test_single_unauthorized(self):
        with self.assertRaisesRegex(AndroidBlocked, "unauthorized"):
            select_device(parse_devices("a unauthorized"))

    def test_package_match_is_exact(self):
        client = AdbClient(time.monotonic() + 5, executable="adb")
        with patch.object(client, "text", return_value="package:tv.danmaku.bili.other"):
            self.assertFalse(client.installed("tv.danmaku.bili"))
        with patch.object(client, "text", return_value="package:tv.danmaku.bili"):
            self.assertTrue(client.installed("tv.danmaku.bili"))

    def test_deadline_prevents_command(self):
        client = AdbClient(time.monotonic() - 1, executable="adb")
        with patch("opinion_capture.android.subprocess.run") as run:
            with self.assertRaises(TimeoutError):
                client.run("devices", device=False)
            run.assert_not_called()

    def test_xhs_actual_login_gate(self):
        self.assertEqual(detect_blocker('<hierarchy><node text="登录查看更多精彩内容"/></hierarchy>'), "login_required")

    def test_png_original_bytes(self):
        buf = io.BytesIO()
        Image.new("RGB", (3, 4), "red").save(buf, format="PNG")
        client = AdbClient(time.monotonic() + 5, executable="adb")
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "run", return_value=buf.getvalue()) as run:
            path = Path(tmp) / "screen.png"
            client.screenshot(path)
            self.assertEqual(path.read_bytes(), buf.getvalue())
            run.assert_called_once_with("exec-out", "screencap", "-p")
            with self.assertRaises(FileExistsError):
                client.screenshot(path)

    def test_jpeg_rejected(self):
        client = AdbClient(time.monotonic() + 5, executable="adb")
        with patch.object(client, "run", return_value=b"\xff\xd8bad"):
            with self.assertRaisesRegex(ValueError, "not_png"):
                client.screenshot(Path("unused.png"))

    def test_blocker_is_not_login_link(self):
        self.assertIsNone(detect_blocker('<hierarchy><node text="登录"/></hierarchy>'))
        self.assertEqual(detect_blocker('<hierarchy><node text="请先登录"/></hierarchy>'), "login_required")
        self.assertEqual(detect_blocker('<hierarchy><node text="安全验证"/></hierarchy>'), "verification_required")

    def test_invalid_request_no_adb(self):
        with patch("opinion_capture.android.AdbClient") as adb:
            result = capture({"platform": "other"})
            self.assertEqual(result["status"], "failed")
            adb.assert_not_called()

    def test_missing_package_retains_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, patch("opinion_capture.android.AdbClient") as cls, patch("opinion_capture.android._ui_call") as ui:
            adb = cls.return_value
            adb.serial = "fixture"
            adb.deadline = time.monotonic() + 10
            adb.discover.return_value = {"serial": "fixture"}
            adb.installed.return_value = False
            adb.screenshot.side_effect = lambda path: path.write_bytes(b"fixture")
            ui.return_value = "<hierarchy/>"
            request = {"platform": "bilibili", "keyword": "零跑", "capture_kind": "results", "top_n": 3, "output_dir": tmp}
            result = capture(request)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("tv.danmaku.bili", result["reason"])
            self.assertIsNone(result["observed_count"])
            self.assertFalse(result["rank_verified"])
            self.assertEqual(len(result["artifact_paths"]), 3)
            for artifact in result["artifact_paths"]:
                self.assertTrue(Path(artifact).is_absolute())
                self.assertTrue(Path(artifact).is_file())
            self.assertEqual(json.loads(Path(result["artifact_paths"][-1]).read_text(encoding="utf-8")), result)
            self.assertTrue(all(call.args[2] == "dump" for call in ui.call_args_list))


if __name__ == "__main__":
    unittest.main()
