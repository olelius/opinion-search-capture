"""非阻塞计划服务：持久化触发、FIFO工作队列、错过记录与可控停止。"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timedelta, time
from pathlib import Path

from .config import Plan
from .locks import ResourceLock, ResourceBusy
from .runner import plan_fingerprint, run_plan, trigger_key
from .scheduling import TIMEZONE, daily_slots
from .storage import Store

LOGGER = logging.getLogger(__name__)


def slots_between(start: datetime, end: datetime, schedule: dict) -> list[datetime]:
    """展开(start,end]触发，包含跨午夜窗口，时钟倒退时不补发。"""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("调度时间需要时区")
    start, end = start.astimezone(TIMEZONE), end.astimezone(TIMEZONE)
    if end <= start:
        return []
    day = start.date() - timedelta(days=1)
    result = set()
    while day <= end.date():
        result.update(t for t in daily_slots(day, schedule) if start < t <= end)
        day += timedelta(days=1)
    return sorted(result)


class ScheduleService:
    """一个冻结计划一个服务，OS锁防双开；停止接收和取消当前采集分开。

    start非阻塞；stop默认等待当前批次自然结束，但未执行的队列会标记取消。
    只有调度线程与工作线程均退出后is_running才为False。
    """

    def __init__(self, plan: Plan, *, run_callback=None, progress_callback=None,
                 tick_seconds: float = 5, clock=None):
        if tick_seconds <= 0:
            raise ValueError("检查间隔需要大于0")
        self.plan = Plan.from_dict(plan.to_dict())
        self.run_callback = run_callback or run_plan
        self.progress_callback = progress_callback
        self.tick_seconds = tick_seconds
        self.clock = clock or (lambda: datetime.now(TIMEZONE))
        self.state = Path(self.plan.output_root).resolve() / ".capture-state"
        self.key = plan_fingerprint(self.plan)
        self._thread = self._worker = None
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._queue = queue.Queue()
        self._deferred = {}
        self._deferred_lock = threading.Lock()
        self._service_lock = None
        self.last_error = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _notify(self, event, **data):
        if self.progress_callback:
            try:
                self.progress_callback({"event":event,"plan_name":self.plan.name,**data})
            except Exception:
                LOGGER.exception("计划状态观察者异常")

    def _store(self):
        with ResourceLock("database-init:" + str(self.state).casefold(), timeout=30):
            return Store(self.state / "history.sqlite3")

    def start(self):
        """检查同计划活动锁再启动；不把遗留锁文件当作活动任务。"""
        if self.is_running:
            return
        self._service_lock = ResourceLock("schedule:" + str(self.state).casefold() + ":" + self.key)
        self._service_lock.acquire()
        self._stop.clear()
        self._cancel.clear()
        self._queue = queue.Queue()
        self.last_error = None
        self._thread = threading.Thread(target=self._loop, name="capture-scheduler", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._service_lock.release()
            raise

    def stop(self, wait: bool = False, cancel_running: bool = False):
        """停止未来触发；可选取消在途批次，GUI不在主线程阻塞等待。"""
        if cancel_running:
            self._cancel.set()
        self._stop.set()
        if wait and self._thread and threading.current_thread() not in (self._thread, self._worker):
            self._thread.join()

    def _restore(self):
        """恢复已持久化的未完成批次；是否仍有执行者由runner的OS锁判定。"""
        with self._store() as store:
            for row in store.recoverable_batches():
                snapshot = Plan.from_dict(json.loads(row["config_json"]))
                if plan_fingerprint(snapshot) == self.key:
                    self._queue.put(row)
                    self._notify("schedule_restored", batch_id=row["id"], status=row["status"])

    def _scan(self, now):
        with self._deferred_lock:
            ready = [key for key, row in self._deferred.items() if datetime.fromisoformat(row["planned_at"]) <= now]
            for key in ready:
                self._queue.put(self._deferred.pop(key))
        with self._store() as store:
            start = store.get_cursor(self.key)
            if start is None:
                start = datetime.combine(now.astimezone(TIMEZONE).date(), time.min, TIMEZONE) - timedelta(microseconds=1)
            if now < start:
                self._notify("clock_reversed", status="waiting", reason="系统时间早于已记录游标，等待时钟追上")
                return
            for slot in slots_between(start, now, self.plan.schedule):
                expired = (now - slot).total_seconds() > self.plan.schedule_grace_seconds
                batch_id, fresh = store.create_batch(trigger_key(self.plan, slot), slot, self.plan.to_dict(),
                                                      kind="scheduled", status="missed" if expired else "queued")
                if not fresh:
                    continue
                if expired:
                    store.finish_batch(batch_id, "missed", reason="missed_scheduled_time")
                    self._notify("schedule_missed", batch_id=batch_id, status="missed", planned_at=slot.isoformat())
                else:
                    self._queue.put(store.batch(batch_id))
                    self._notify("schedule_queued", batch_id=batch_id, status="queued", planned_at=slot.isoformat())
            store.set_cursor(self.key, now)

    def _finish_unowned(self, row, status, reason, allowed=("queued",)):
        """持有批次OS锁并重读当前状态，才允许终结未派发任务。"""
        key = "batch:" + str((self.state / "history.sqlite3").resolve()).casefold() + ":" + row["id"]
        try:
            with ResourceLock(key):
                with self._store() as store:
                    current = store.batch(row["id"])
                    if current["status"] not in allowed:
                        return False
                    store.finish_batch(row["id"], status, reason=reason)
                    return True
        except ResourceBusy:
            self._notify("schedule_owned_elsewhere", batch_id=row["id"], status="running")
            return False

    def _work(self):
        while True:
            row = self._queue.get()
            if row is None:
                self._queue.task_done()
                return
            try:
                with self._store() as store:
                    row = store.batch(row["id"])
                if row["status"] not in ("queued", "running"):
                    continue
                if self._stop.is_set():
                    self._finish_unowned(row, "cancelled", "schedule_stopped_before_dispatch")
                    continue
                slot = datetime.fromisoformat(row["planned_at"])
                if self.clock() < slot:
                    with self._deferred_lock:
                        self._deferred[row["id"]] = row
                    self._notify("schedule_waiting_clock", batch_id=row["id"], status="queued")
                    continue
                # 已开始而中断的批次恢复未完成任务；从未开始的过期排队批次不补造截图。
                if row["status"] == "queued" and (self.clock() - slot).total_seconds() > self.plan.max_queue_delay_seconds:
                    if self._finish_unowned(row, "missed", "queue_delay_exceeded"):
                        self._notify("schedule_missed", batch_id=row["id"], status="missed", reason="queue_delay_exceeded")
                    continue
                result = self.run_callback(self.plan, scheduled_at=slot, resume_batch_id=row["id"],
                    cancel_event=self._cancel, progress_callback=self.progress_callback)
                if not isinstance(result, dict) or result.get("status") not in ("success","partial","blocked","failed","cancelled","running"):
                    raise ValueError("批次执行器返回状态无效")
                with self._store() as store:
                    current = store.batch(row["id"])
                    if current["status"] in ("queued", "running") and result["status"] != "running":
                        store.finish_batch(row["id"], result["status"], result.get("report_path"), result.get("reason"))
                self._notify("schedule_result", batch_id=row["id"], status=result["status"], report_path=result.get("report_path"))
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                try:
                    self._finish_unowned(row, "failed", self.last_error, allowed=("queued", "running"))
                except Exception:
                    LOGGER.exception("记录调度失败状态时发生异常")
                self._notify("schedule_error", status="failed", batch_id=row["id"], reason=self.last_error)
            finally:
                self._queue.task_done()

    def _loop(self):
        try:
            self._restore()
            self._worker = threading.Thread(target=self._work, name="capture-schedule-worker", daemon=True)
            self._worker.start()
            self._notify("schedule_started", status="running")
            while not self._stop.is_set():
                self._scan(self.clock().astimezone(TIMEZONE))
                self._stop.wait(self.tick_seconds)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._notify("schedule_error", status="failed", reason=self.last_error)
            self._stop.set()
        finally:
            with self._deferred_lock:
                deferred = list(self._deferred.values())
                self._deferred.clear()
            for row in deferred:
                self._finish_unowned(row, "cancelled", "schedule_stopped_before_dispatch")
            self._queue.put(None)
            if self._worker:
                self._worker.join()
            if self._service_lock:
                self._service_lock.release()
            self._notify("schedule_stopped", status="stopped", reason=self.last_error)
