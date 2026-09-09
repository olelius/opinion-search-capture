"""执行核心独立评审：仅临时目录、确定性图片和本测试创建的进程。

ReviewRegressionTests 断言应满足的业务不变量，当前缺陷会直接显示 FAIL，
不是 expectedFailure，也不把缺陷现状固化成通过标准。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from opinion_capture.config import Plan
from opinion_capture.runner import run_plan, trigger_key
from opinion_capture.scheduling import TIMEZONE
from opinion_capture.service import ScheduleService
from opinion_capture.storage import Store


NOW = datetime(2026, 9, 8, 9, 0, 2, tzinfo=TIMEZONE)


def fixture(request):
    """每个精确关键词生成固定颜色 PNG，不导入或调用平台适配器。"""
    path = Path(request["output_dir"]) / "primary.png"
    color = tuple(hashlib.sha256(request["keyword"].encode()).digest()[:3])
    with Image.new("RGB", (40, 80), color) as picture:
        picture.save(path)
    return {**{k: request[k] for k in ("platform", "client", "capture_kind", "keyword")},
            "status": "success", "reason": "local_review_fixture", "rank_verified": True,
            "observed_count": request["top_n"], "requested_top_n": request["top_n"],
            "artifact_paths": [str(path)], "primary_artifact_path": str(path),
            "captured_at": NOW.isoformat()}


class ReviewBase(unittest.TestCase):
    """把输出、数据库、OS锁文件和进程握手文件都限制在专用临时目录。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oc-lifecycle-review-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        lock_root = patch("opinion_capture.locks.tempfile.gettempdir", return_value=str(self.root))
        lock_root.start()
        self.addCleanup(lock_root.stop)

    def plan(self, **changes):
        values = dict(output_root=str(self.root / "out"), keywords=["A1"],
                      platforms=["bilibili"], clients=["WEB"], capture_kinds=["suggestions"],
                      retry_delay_seconds=0, composite_cell_width=64)
        return Plan(**(values | changes))

    def store(self, plan):
        return Store(Path(plan.output_root) / ".capture-state" / "history.sqlite3")

    @contextmanager
    def owned_process_lock(self, key):
        """真实 CPython 子进程只持指定锁；通过自身句柄清理，不按名称找进程。"""
        marker = self.root / "child-lock-ready"
        code = ("import sys,time,tempfile;from pathlib import Path;"
                "tempfile.tempdir=sys.argv[2];"
                "from opinion_capture.locks import ResourceLock;"
                "lock=ResourceLock(sys.argv[1]);lock.acquire();"
                "Path(sys.argv[3]).write_text('locked');time.sleep(60)")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        process = subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-B", "-c", code,
             key, str(self.root), str(marker)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), "评审自身子进程未在期限内取得OS锁")
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stderr.close()
            marker.unlink(missing_ok=True)

    def seeded_batch(self, plan):
        with self.store(plan) as store:
            batch_id, _ = store.create_batch(trigger_key(plan, NOW), NOW, plan.to_dict(), kind="scheduled")
            return batch_id, store.batch(batch_id), "batch:" + str(store.path).casefold() + ":" + batch_id


class ReviewRegressionTests(ReviewBase):
    """真实缺陷回归；每项最终断言均要求修复后的正确行为。"""

    def test_success_attempt_commit_is_reused_after_interruption(self):
        """在尝试已成功提交、任务尚未提交处注入退出，不应重采同一关键词。"""
        plan, calls = self.plan(), []

        def collect(request):
            calls.append(request["keyword"])
            return fixture(request)

        with patch.object(Store, "finish_task", side_effect=SystemExit("review crash boundary")):
            with self.assertRaises(SystemExit):
                run_plan(plan, scheduled_at=NOW, collector=collect)
        with self.store(plan) as store:
            batch = store.recoverable_batches()[0]
            task = store.tasks(batch["id"])[0]
            attempt = dict(store.db.execute("SELECT * FROM attempts").fetchone())
            # 新实现同事务提交；注入退出发生在后续任务冗余更新处。
            self.assertEqual((task["status"], attempt["status"]), ("success", "success"))
            saved = json.loads(attempt["result_json"])["exports"][0]
            old_bytes = Path(saved["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(old_bytes).hexdigest(), saved["sha256"])
        result = run_plan(plan, resume_batch_id=batch["id"], collector=collect)
        self.assertEqual(result["status"], "success")
        self.assertEqual(Path(saved["path"]).read_bytes(), old_bytes)
        self.assertEqual(calls, ["A1"], "成功attempt和哈希原图已落库，恢复仍第二次调用collector")

    def _queue_must_respect_live_owner(self, stop):
        plan = self.plan(max_queue_delay_seconds=1)
        batch_id, stale_row, key = self.seeded_batch(plan)
        service = ScheduleService(plan, clock=lambda: NOW + timedelta(seconds=5))
        with self.owned_process_lock(key) as owner:
            with self.store(plan) as store:
                store.start_batch(batch_id)
            guarded = run_plan(plan, resume_batch_id=batch_id, collector=fixture)
            self.assertTrue(guarded["busy"])
            service._queue.put(stale_row)
            service._queue.put(None)
            if stop:
                service._stop.set()
            service._work()
            self.assertIsNone(owner.poll(), "活锁持有者应仍在运行")
            with self.store(plan) as store:
                current = store.batch(batch_id)
            self.assertEqual(current["status"], "running",
                             f"调度器跳过活锁保护覆盖状态: {current['status']} / {current['reason']}")

    def test_expired_queue_does_not_overwrite_live_batch(self):
        self._queue_must_respect_live_owner(stop=False)

    def test_stopped_queue_does_not_overwrite_live_batch(self):
        self._queue_must_respect_live_owner(stop=True)

    def test_cancel_before_resource_acquire_does_not_call_collector(self):
        """取消发生于task_started回调后、获取资源前，不应再派发采集。"""
        event, calls = threading.Event(), []

        def progress(data):
            if data["event"] == "task_started":
                event.set()

        def collect(request):
            calls.append(request["keyword"])
            return fixture(request)

        result = run_plan(self.plan(), collector=collect, cancel_event=event, progress_callback=progress)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(calls, [], "取消已生效仍调用collector；最终cancelled掩盖了实际派发")

    def test_configured_profile_root_is_forwarded(self):
        calls = []
        custom = self.root / "chosen-profiles"

        def collect(request):
            calls.append(request)
            return fixture(request)

        result = run_plan(self.plan(browser_profile_root=str(custom)), collector=collect)
        self.assertEqual(result["status"], "success")
        self.assertEqual(Path(calls[0]["user_data_dir"]).parent, custom,
                         "配置保存了专属会话根目录，但runner仍使用图片目录下的另一份profile")

    def test_clock_reversal_holds_already_queued_future_slot(self):
        """扫描声称等待时钟追上时，已入队的未来时点也应暂停派发。"""
        calls, events = [], []
        clock = [NOW]
        plan = self.plan(schedule={"times": ["09:00"]})
        service = ScheduleService(plan, clock=lambda: clock[0], progress_callback=events.append,
                                  run_callback=lambda *args, **kwargs: calls.append(kwargs) or {"status": "success"})
        service._scan(clock[0])
        self.assertEqual(service._queue.qsize(), 1)
        clock[0] -= timedelta(hours=1)
        service._scan(clock[0])
        self.assertEqual(events[-1]["event"], "clock_reversed")
        service._queue.put(None)
        service._work()
        self.assertEqual(calls, [], "08:00已派发09:00批次，与clock_reversed等待状态相反")

    def test_resume_preflight_does_not_reject_verified_existing_success(self):
        """使用被支持的按批次命名模板恢复时，应跳过成功原图而非预检拒绝。"""
        event, calls = threading.Event(), []
        plan = self.plan(keywords=["A1", "A10"], directory_template="raw/{keyword}",
                         filename_template="{keyword}_{batch:02d}.png")

        def collect(request):
            calls.append(request["keyword"])
            return fixture(request)

        def progress(data):
            if data["event"] == "task_finished" and data["completed"] == 1:
                event.set()

        first = run_plan(plan, scheduled_at=NOW, collector=collect,
                         cancel_event=event, progress_callback=progress)
        self.assertEqual(first["status"], "cancelled")
        self.assertTrue(Path(first["results"][0]["exports"][0]["path"]).is_file())
        resumed = run_plan(plan, resume_batch_id=first["batch_id"], collector=collect)
        self.assertEqual(resumed["status"], "success", resumed.get("reason"))
        self.assertEqual(calls, ["A1", "A10"])

    def test_second_page_error_reaches_batch_reason(self):
        """第二页写入失败应出现在批次原因，不止藏在composites嵌套记录中。"""
        from opinion_capture import imaging
        save = imaging._save
        published = []

        def fail_second(image, output):
            if published:
                raise OSError("review_second_page_write_failed")
            result = save(image, output)
            published.append(Path(output))
            return result

        plan = self.plan(keywords=["A1", "A10"], composite_columns=1, composite_max_height=256)
        with patch.object(imaging, "_save", side_effect=fail_second):
            result = run_plan(plan, collector=fixture)
        self.assertEqual(len(published), 1)
        self.assertTrue(published[0].is_file())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["composites"][0]["reason"], "review_second_page_write_failed")
        with self.store(plan) as store:
            stored = store.batch(result["batch_id"])
        self.assertIn("review_second_page_write_failed", result["reason"] + (stored["reason"] or ""),
                      "批次/历史reason为空，虽然有一页留存且下一页失败")


if __name__ == "__main__":
    unittest.main()
