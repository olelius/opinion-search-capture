"""真实OS锁与批次恢复、重试、取消的本地集成，不访问平台。"""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from opinion_capture.config import Plan
from opinion_capture.locks import ResourceBusy, ResourceLock
from opinion_capture.runner import run_plan, trigger_key
from opinion_capture.scheduling import TIMEZONE
from opinion_capture.service import ScheduleService, slots_between
from opinion_capture.storage import Store, list_history
from tests.test_runner import fixture_collector


class LifecycleTests(unittest.TestCase):
    def plan(self, root, **kwargs):
        return Plan(output_root=root,platforms=["bilibili"],clients=["WEB"],capture_kinds=["suggestions"],retry_delay_seconds=0,**kwargs)

    def test_retry_keeps_attempts(self):
        calls=[]
        def collect(request):
            calls.append(request)
            r=fixture_collector(request)
            if len(calls)==1: r.update(status="failed",reason="temporary_connection_error")
            return r
        with tempfile.TemporaryDirectory() as root:
            result=run_plan(self.plan(root),collector=collect)
            self.assertEqual(result["status"],"success")
            self.assertEqual(len(calls),2)
            self.assertNotEqual(calls[0]["output_dir"],calls[1]["output_dir"])
            with Store(Path(root)/".capture-state/history.sqlite3") as store:
                self.assertEqual([r[0] for r in store.db.execute("SELECT status FROM attempts ORDER BY number")],["failed","success"])

    def test_explicit_missing_primary_never_exports_first_frame(self):
        def collect(request):
            result=fixture_collector(request)
            result.update(status="partial",primary_artifact_path=None,reason="unverified_long_image")
            return result
        with tempfile.TemporaryDirectory() as root:
            result=run_plan(self.plan(root),collector=collect)
            self.assertEqual(result["status"],"partial")
            self.assertEqual(result["results"][0]["exports"],[])
            self.assertEqual(result["composites"],[])

    def test_cancel_then_resume_reuses_success(self):
        event=threading.Event(); calls=[]
        def progress(data):
            if data["event"]=="task_finished" and data["completed"]==1: event.set()
        def collect(request):
            calls.append(request["keyword"]);return fixture_collector(request)
        with tempfile.TemporaryDirectory() as root:
            plan=self.plan(root,keywords=["A1","A10"])
            first=run_plan(plan,collector=collect,cancel_event=event,progress_callback=progress)
            self.assertEqual(first["status"],"cancelled")
            first_export=first["results"][0]["exports"][0]
            second=run_plan(plan,collector=collect,resume_batch_id=first["batch_id"])
            self.assertEqual(second["status"],"success")
            self.assertEqual(calls,["A1","A10"])
            self.assertEqual(second["results"][0]["exports"][0],first_export)
            self.assertNotEqual(first["report_path"],second["report_path"])
            self.assertTrue(Path(first["report_path"]).is_file())

    def test_cancel_file_bridge(self):
        event=threading.Event()
        def collect(request):
            event.set()
            deadline=time.monotonic()+2
            while not Path(request["cancel_file"]).exists() and time.monotonic()<deadline: time.sleep(.02)
            self.assertTrue(Path(request["cancel_file"]).exists())
            return fixture_collector(request)
        with tempfile.TemporaryDirectory() as root:
            result=run_plan(self.plan(root),collector=collect,cancel_event=event)
            self.assertEqual(result["status"],"cancelled")
            self.assertEqual(result["results"][0].get("exports"),[])

    def test_interrupted_attempt_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            plan=self.plan(root)
            with Store(Path(root)/".capture-state/history.sqlite3") as store:
                at=datetime.now(TIMEZONE);bid,_=store.create_batch(trigger_key(plan,at),at,plan.to_dict())
                store.start_batch(bid)
                from opinion_capture.exporting import keyword_id
                tid=store.add_task(bid,"bilibili","WEB","suggestions","零跑",keyword_id("零跑"))
                store.begin_attempt(tid,str(Path(root)/"aborted"))
            result=run_plan(plan,resume_batch_id=bid,collector=fixture_collector)
            self.assertEqual(result["status"],"success")
            with Store(Path(root)/".capture-state/history.sqlite3") as store:
                statuses=[r[0] for r in store.db.execute("SELECT status FROM attempts ORDER BY number")]
                self.assertEqual(statuses,["interrupted","success"])

    def test_live_os_lock_blocks_second_process(self):
        key="test-"+uuid.uuid4().hex
        code="from opinion_capture.locks import ResourceLock; import time; l=ResourceLock("+repr(key)+");l.acquire();print('LOCKED',flush=True);time.sleep(30)"
        # Windows venv启动器会产生额外子进程；锁测试直接启动真实CPython。
        process=subprocess.Popen([getattr(sys,"_base_executable",sys.executable),"-c",code],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),"LOCKED")
            with self.assertRaises(ResourceBusy):
                with ResourceLock(key,timeout=.05): pass
        finally:
            process.terminate();process.wait(timeout=5);process.stdout.close()
        with ResourceLock(key): pass

    def test_history_read_does_not_create_root(self):
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/"absent"
            self.assertEqual(list_history(target),[])
            self.assertFalse(target.exists())


class ScheduleTests(unittest.TestCase):
    def test_long_batch_does_not_hide_next_trigger(self):
        clock=[datetime(2026,9,8,9,0,1,tzinfo=TIMEZONE)]
        started,release,second=threading.Event(),threading.Event(),threading.Event()
        calls=[]
        def run(plan,**kwargs):
            calls.append(kwargs["scheduled_at"])
            if len(calls)==1:
                started.set();release.wait(3)
            else: second.set()
            return {"status":"success"}
        with tempfile.TemporaryDirectory() as root:
            plan=Plan(output_root=root,schedule={"times":["09:00","09:01"]})
            service=ScheduleService(plan,run_callback=run,tick_seconds=.02,clock=lambda:clock[0])
            try:
                service.start();self.assertTrue(started.wait(2))
                clock[0]+=timedelta(minutes=1)
                deadline=time.monotonic()+2
                while time.monotonic()<deadline:
                    if len(list_history(root))==2: break
                    time.sleep(.02)
                self.assertEqual(len(list_history(root)),2)
                release.set();self.assertTrue(second.wait(2))
            finally: release.set();service.stop(wait=True)
            self.assertEqual([t.minute for t in calls],[0,1])

    def test_two_services_same_plan_are_excluded(self):
        with tempfile.TemporaryDirectory() as root:
            plan=Plan(output_root=root,schedule={"times":[]})
            a,b=ScheduleService(plan,tick_seconds=.02),ScheduleService(plan,tick_seconds=.02)
            try:
                a.start()
                with self.assertRaises(ResourceBusy): b.start()
            finally: a.stop(wait=True);b.stop(wait=True)

    def test_slots_cross_midnight_and_no_duplicate(self):
        start=datetime(2026,9,30,23,29,tzinfo=TIMEZONE)
        end=datetime(2026,10,1,0,31,tzinfo=TIMEZONE)
        schedule={"windows":[{"start":"23:30","end":"00:30","interval_minutes":30}],"times":["00:00"]}
        slots=slots_between(start,end,schedule)
        self.assertEqual(len(slots),3)
        self.assertEqual([s.month for s in slots],[9,10,10])

    def test_persistent_scheduler_and_missed(self):
        now=datetime(2026,9,8,9,0,2,tzinfo=TIMEZONE)
        with tempfile.TemporaryDirectory() as root:
            plan=Plan(output_root=root,schedule={"times":["08:00","09:00"]})
            called=threading.Event();calls=[]
            def run(plan,**kwargs):
                calls.append(kwargs["scheduled_at"]);called.set();return {"status":"success"}
            service=ScheduleService(plan,run_callback=run,tick_seconds=.03,clock=lambda:now)
            try:
                service.start();self.assertTrue(called.wait(3))
            finally: service.stop(wait=True)
            again=ScheduleService(plan,run_callback=run,tick_seconds=.03,clock=lambda:now)
            try:
                again.start();time.sleep(.15)
            finally: again.stop(wait=True)
            self.assertEqual(len(calls),1)
            rows=list_history(root)
            self.assertEqual({r["status"] for r in rows},{"success","missed"})

    def test_stop_reports_running_until_worker_exits(self):
        now=datetime(2026,9,8,9,0,2,tzinfo=TIMEZONE)
        started,release=threading.Event(),threading.Event()
        def run(plan,**kwargs):
            started.set();release.wait(3);return {"status":"success"}
        with tempfile.TemporaryDirectory() as root:
            service=ScheduleService(Plan(output_root=root),run_callback=run,tick_seconds=.03,clock=lambda:now)
            try:
                service.start();self.assertTrue(started.wait(2))
                service.stop(wait=False)
                self.assertTrue(service.is_running)
                release.set();service.stop(wait=True)
                self.assertFalse(service.is_running)
            finally: release.set();service.stop(wait=True,cancel_running=True)


if __name__=="__main__": unittest.main()
