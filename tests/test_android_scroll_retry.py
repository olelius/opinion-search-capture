"""滚动/加载等待的定点回归：虚拟时钟、设备与ledger，不连接真实设备。"""
from copy import deepcopy
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from opinion_capture import android
from opinion_capture.android_results import EvidenceError, RankLedger


class Clock:
    """让等待即时推进，并在指定虚拟时刻创建取消标记。"""

    def __init__(self, cancel_at=None, cancel_file=None):
        self.now = 0.0
        self.cancel_at, self.cancel_file = cancel_at, cancel_file

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.cancel_at is not None and self.now >= self.cancel_at:
            self.cancel_file.touch()


class Ledger:
    """仅模拟原子收帧/静止拒绝，用来验证等待控制流而不是冒充像素验收。"""

    def __init__(self, top_n, strict=False):
        self.top_n = top_n
        self.items, self.frames, self.links, self.excluded, self.unknown = [], [], [], [], []

    def add(self, frame, check):
        check()
        if self.frames and len(frame["page"]["items"]) == len(self.items):
            raise EvidenceError("no_scroll_progress")
        if self.frames:
            self.links.append(dict(shift=686, overlap=1024))
        frame.update(accepted=True, offset=0)
        self.frames.append(frame)
        self.items = deepcopy(frame["page"]["items"])

    def target_ready(self):
        return len(self.items) >= self.top_n

    def boundary_clear(self, count):
        return True

    def unresolved_regions(self, count):
        return []


class AndroidScrollRetryTests(unittest.TestCase):
    def flow(self, reason="spinner", ready_at=8, timeout=180, cancel_at=None, terminal=None):
        """在一次正常滚动后停住，按时加载、取消或出现明确终态。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = Clock(cancel_at, root / "cancel")
            adb = SimpleNamespace(deadline=timeout, cancel_file=str(root / "cancel"))
            result = dict(platform="bilibili", keyword="测试", requested_top_n=3, capture_kind="results",
                          frames=[], items=[], artifact_paths=[])
            base = dict(platform="bilibili", viewport=[0, 210, 1080, 1920], keyword_verified=True,
                        scope_verified=True, recognized=True, end_marker=None, excluded=[], unknown=[], occlusions=[])

            def page():
                state = deepcopy(base)
                ready = ready_at is not None and clock.now >= ready_at
                count = 3 if ready and terminal is None else 2
                state["items"] = [dict(identity=str(i), bounds=[0, 300 + i * 200, 1080, 480 + i * 200],
                                       complete=True, rank=i + 1, content_bounds=[0, 300, 1080, 480]) for i in range(count)]
                if ready and terminal == "end":
                    state["end_marker"] = dict(text="没有更多了", bounds=[0, 1750, 1080, 1800])
                return state

            def xml(*args):
                ready = ready_at is not None and clock.now >= ready_at
                text = "请先登录" if ready and terminal == "login" else "请完成验证" if ready and terminal == "verification" else "正在加载" if reason == "spinner" and not ready else ""
                return f'<hierarchy><node text="{text}"/></hierarchy>'

            def snapshot(*args):
                state = page()
                frame = dict(page=state, stable=True, accepted=False, source_index=len(result["frames"]),
                             loading_hint=None, png_path="virtual-not-read.png")
                if result["frames"]:
                    frame["loading_hint"] = android._loading_hint(xml(), state)
                result["frames"].append(frame)
                return frame

            def covers(frame, check):
                if reason == "cover" and frame["source_index"] > 0 and (ready_at is None or clock.now < ready_at):
                    raise EvidenceError("cover_content_unverified")

            error = None
            with patch.object(android.time, "monotonic", clock.monotonic), patch.object(android.time, "sleep", clock.sleep), \
                    patch.object(android, "_snapshot", side_effect=snapshot) as snapshots, \
                    patch.object(android, "_dump", side_effect=xml), \
                    patch.object(android, "_check_visible_covers", side_effect=covers), \
                    patch.object(android, "_swipe_forward", return_value={"target_px": 1368}) as swipe, \
                    patch.object(android, "phase") as phases, \
                    patch("opinion_capture.android_results.parse_page", side_effect=lambda *args: page()), \
                    patch("opinion_capture.android_results.RankLedger", Ledger), \
                    patch("opinion_capture.android_results.render_long", return_value={"path": str(root / "virtual.png")}):
                try:
                    android._collect(adb, root, result, {"max_frames": 2})
                except (android.AndroidLoadingTimeout, android.AndroidCancelled, android.AndroidBlocked, EvidenceError) as exc:
                    error = exc
            self.assertEqual(adb.deadline, timeout, "等待结束须恢复外层截止时间")
            self.assertEqual(swipe.call_count, 1, "停住后只读等待，零补滑")
            self.assertTrue(any(call.args[0] == "loading_wait" for call in phases.call_args_list))
            return result, clock.now, error, snapshots.call_count

    def test_fast_drag_keeps_80_percent_and_holds_before_up(self):
        for platform, cautious, remaining in (("xiaohongshu", False, 20), ("bilibili", True, 20), ("bilibili", False, 1)):
            with self.subTest(platform=platform, cautious=cautious, remaining=remaining):
                clock = Clock()
                def run(*command):
                    clock.sleep(sum(float(value) for value in re.findall(r"sleep ([\d.]+)", command[1])))
                    return b"gesture_completed\n"

                adb = SimpleNamespace(deadline=10, cancel_file=None, run=Mock(side_effect=run))
                adb.drag_hold = lambda gesture: android.AdbClient.drag_hold(adb, gesture)
                page = dict(platform=platform, viewport=[0, 210, 1080, 1920], items=[dict(complete=True)] * 5)
                with patch.object(android.time, "monotonic", clock.monotonic), patch.object(android.time, "sleep", clock.sleep):
                    action = android._swipe_forward(adb, page, cautious=cautious, remaining=remaining)
                self.assertEqual(action["normal_target_px"], 1368)
                self.assertEqual(action["target_px"], 274 if remaining == 1 else 1368)
                self.assertAlmostEqual(action["expected_overlap_ratio"], .84 if remaining == 1 else .2, delta=.001)
                self.assertEqual(action["dispatch_status"], "command_completed")
                self.assertAlmostEqual(clock.now, .58 if cautious else .5)
                adb.run.assert_called_once()
                self.assertEqual(adb.run.call_args.args[0], "shell")
                script = adb.run.call_args.args[1]
                events = re.findall(r"motionevent (DOWN|MOVE|UP) (\d+) (\d+)", script)
                self.assertEqual(events[0][0], "DOWN")
                self.assertEqual(events[-1][0], "UP")
                self.assertEqual(len(events), 13)
                self.assertTrue(all([int(event[1]), int(event[2])] == action["end"] for event in events[-5:]))
                self.assertIn("motionevent CANCEL", script)
                self.assertLess(script.index("motionevent_unsupported"), script.index("motionevent DOWN"))
                self.assertEqual(script.count("sleep 0.060"), 3)
                self.assertEqual(action["elapsed_seconds"], round(clock.now, 3))

    def test_slow_loading_recovers_without_extra_scroll(self):
        for reason in ("spinner", "stationary", "cover"):
            with self.subTest(reason=reason):
                result, elapsed, error, snapshots = self.flow(reason=reason)
                self.assertIsNone(error)
                self.assertEqual(result["status"], "success")
                self.assertFalse(result["retryable"])
                self.assertGreaterEqual(elapsed, 8)
                self.assertLess(elapsed, 8.36)
                self.assertEqual(result["loading_wait"]["episodes"][0]["status"], "ready")
                if reason != "cover":
                    self.assertEqual(snapshots, 3, "XML未就绪时不重复PNG")

    def test_loading_timeout_includes_global_deadline_and_cover(self):
        for reason, timeout, expected in (("spinner", 180, "loading_not_confirmed"),
                                           ("stationary", 180, "scroll_stalled"),
                                           ("cover", 180, "cover_loading_not_confirmed"),
                                           ("spinner", 4, "loading_not_confirmed")):
            with self.subTest(reason=reason, timeout=timeout):
                result, elapsed, error, _ = self.flow(reason=reason, ready_at=None, timeout=timeout)
                self.assertIsInstance(error, android.AndroidLoadingTimeout)
                self.assertIn(expected, str(error))
                self.assertAlmostEqual(elapsed, min(25, timeout))
                self.assertEqual(result["observed_count"], 2)
                self.assertEqual(result["loading_wait"]["episodes"][0]["status"], "timeout")

    def test_cancel_wait_and_interrupted_single_channel(self):
        result, elapsed, error, snapshots = self.flow(ready_at=None, cancel_at=1.1)
        self.assertIsInstance(error, android.AndroidCancelled)
        self.assertLessEqual(elapsed, 1.151)
        self.assertEqual(snapshots, 2)
        self.assertEqual(result["loading_wait"]["episodes"][0]["status"], "cancelled")
        clock = Clock()
        adb = SimpleNamespace(deadline=10, cancel_file=None, run=Mock(side_effect=android.AndroidCancelled("cancel_requested")))
        adb.drag_hold = lambda gesture: android.AdbClient.drag_hold(adb, gesture)
        record = {}
        with patch.object(android.time, "monotonic", clock.monotonic), self.assertRaises(android.AndroidCancelled):
            android._swipe_forward(adb, dict(platform="bilibili", viewport=[0, 210, 1080, 1920]), record=record)
        adb.run.assert_called_once()
        self.assertEqual(record["dispatch_status"], "interrupted_or_unknown")
        self.assertEqual(record["touch_cleanup"], "remote_trap_unconfirmed")

    def test_spinner_candidate_and_explicit_terminal_are_not_retry_signals(self):
        spinner = '<hierarchy><node class="android.widget.ImageView" text="" bounds="[501,1843][579,1885]"/></hierarchy>'
        page = dict(platform="xiaohongshu", viewport=[0, 210, 1080, 1920], items=[])
        self.assertEqual(android._loading_hint(spinner, page), "bottom_spinner_candidate")
        self.assertIsNone(android._loading_hint(spinner.replace('text=""', 'text="图标"'), page))
        page["items"] = [dict(bounds=[400, 1700, 600, 1900])]
        self.assertIsNone(android._loading_hint(spinner, page))
        for terminal in ("login", "verification", "end"):
            with self.subTest(terminal=terminal):
                result, elapsed, error, _ = self.flow(ready_at=1, terminal=terminal)
                self.assertIsNotNone(error)
                self.assertNotIsInstance(error, android.AndroidLoadingTimeout)
                self.assertLess(elapsed, 1.36)
                self.assertFalse(result["retryable"])

    def test_target_completes_with_persistent_footer_and_wait_accept_rolls_back(self):
        """验证首帧已够/等待中新增两种入口；使用真实RankLedger.add的原子包装。"""
        for ready_at in (0, .7):
            with self.subTest(ready_at=ready_at), tempfile.TemporaryDirectory() as directory:
                clock = Clock()
                ledger = RankLedger(20, strict=False)
                result = dict(platform="bilibili", keyword="测试", requested_top_n=20, capture_kind="results",
                              frames=[], items=[], artifact_paths=[])
                adb = SimpleNamespace(deadline=180, cancel_file=None)

                def page():
                    count = 20 if clock.now >= ready_at else 18 if not result["frames"] else 19
                    return dict(platform="bilibili", viewport=[0, 210, 1080, 1920], recognized=True,
                        keyword_verified=True, scope_verified=True, end_marker=None, unknown=[], excluded=[],
                        items=[dict(identity=str(i), complete=True, bounds=[0, 250 + i * 60, 1080, 300 + i * 60]) for i in range(count)])

                def snapshot(*args):
                    current = dict(page=page(), stable=True, accepted=False, source_index=len(result["frames"]),
                                   loading_hint="bottom_spinner_candidate" if ready_at == 0 or result["frames"] else None)
                    result["frames"].append(current)
                    return current

                def validated(frame, check):
                    # 只替换像素/业务计算；真实add仍复制状态并在异常时回滚。
                    # 等待帧19条会先成功add，再因目标不足+spinner由accept整体回滚。
                    ledger.items[:] = [dict(item, rank=i + 1, content_bounds=item["bounds"])
                                       for i, item in enumerate(frame["page"]["items"])]
                    if ledger.frames:
                        ledger.links.append(dict(shift=686, overlap=1024))
                    ledger.excluded.append(dict(bounds=[0, 1900, 100, 1920], offset=0))
                    ledger.unknown.append(dict(bounds=[0, 1900, 100, 1920], offset=0, edge=True))
                    frame.update(accepted=True, offset=0)
                    ledger.frames.append(frame)

                with patch.object(android.time, "monotonic", clock.monotonic), patch.object(android.time, "sleep", clock.sleep), \
                        patch.object(android, "_snapshot", side_effect=snapshot), \
                        patch.object(android, "_dump", return_value='<hierarchy><node text="正在加载数据"/></hierarchy>') as dump, \
                        patch.object(android, "_swipe_forward", return_value={}) as swipe, \
                        patch("opinion_capture.android_results.parse_page", side_effect=lambda *args: page()), \
                        patch("opinion_capture.android_results.RankLedger", return_value=ledger), \
                        patch.object(ledger, "_add_validated", side_effect=validated), \
                        patch("opinion_capture.android_results.render_long", return_value={"path": str(Path(directory) / "virtual.png")}):
                    android._collect(adb, Path(directory), result, {"max_frames": 2})
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["observed_count"], 20)
                self.assertFalse(result["retryable"])
                self.assertLess(clock.now, 1)
                accepted = 2 if ready_at else 1
                self.assertEqual(len(ledger.frames), accepted)
                self.assertEqual(len(ledger.excluded), accepted)
                self.assertEqual(len(ledger.unknown), accepted)
                self.assertEqual(len(ledger.links), accepted - 1)
                self.assertEqual(swipe.call_count, accepted - 1)
                if ready_at:
                    rejected = result["frames"][1]
                    self.assertFalse(rejected["accepted"])
                    self.assertNotIn("offset", rejected)
                    self.assertEqual(rejected["rejection_reason"], "loading_indicator_visible")
                    self.assertEqual([frame["source_index"] for frame in ledger.frames], [0, 2])
                    self.assertEqual(result["loading_wait"]["episodes"][0]["snapshots"], 1)
                else:
                    dump.assert_not_called()
                    self.assertNotIn("loading_wait", result)

    def test_capture_exposes_retryable_for_loading_and_ordinary_timeout(self):
        class FakeAdb:
            def __init__(self, deadline, cancel_file=None):
                self.deadline, self.cancel_file, self.serial = deadline, cancel_file, "virtual"

            def discover(self, serial):
                return {"serial": self.serial}

            def installed(self, package):
                return True

        for exception, status, retryable in ((android.AndroidLoadingTimeout("scroll_stalled_without_verified_end"), "failed", True),
                                             (TimeoutError("capture_deadline_exceeded"), "partial", True),
                                             (subprocess.TimeoutExpired("virtual-worker", 1), "partial", True),
                                             (android.AndroidBlocked("login_required"), "blocked", False),
                                             (android.AndroidCancelled("cancel_requested"), "cancelled", False),
                                             (None, "success", False)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                def collect(adb, output, result, request):
                    # 已有证据时不走诊断设备路径；此处只测结果分类。
                    result["source_artifact_paths"].append("virtual-evidence")
                    if exception:
                        raise exception
                    result.update(status="success", reason="explicit_end_of_results")

                with patch.object(android, "AdbClient", FakeAdb), patch.object(android, "_ui_call", return_value=""), \
                        patch.object(android, "_collect", side_effect=collect):
                    result = android.capture(dict(platform="bilibili", capture_kind="results", keyword="测试", output_dir=directory))
                self.assertEqual(result["status"], status)
                self.assertEqual(result["retryable"], retryable)


if __name__ == "__main__":
    unittest.main()
