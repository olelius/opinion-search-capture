"""字段精确归档、配置及时间语义的离线验证。"""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from opinion_capture.config import Plan, load_plan, save_plan
from opinion_capture.exporting import artifact_values, copy_immutable, export_path, keyword_id
from opinion_capture.scheduling import TIMEZONE, daily_slots, due_slots
from opinion_capture.storage import Store


class CoreTests(unittest.TestCase):
    def values(self, keyword="A1", client="APP", kind="results", month=9):
        return artifact_values(project="项目", platform="bilibili", client=client, capture_kind=kind,
            keyword=keyword, captured_at=datetime(2026,month,8,9,tzinfo=TIMEZONE), top_n=20,batch=1,artifact_id="x")

    def test_keyword_identity_exact(self):
        self.assertEqual(len({keyword_id(k) for k in ["A1", "A10", "a1", "A/1", "A:1"]}),5)
        self.assertNotEqual(self.values("A/1")["keyword"], self.values("A:1")["keyword"])

    def test_month_and_client_paths(self):
        with tempfile.TemporaryDirectory() as root:
            template="{month}/{month:02d}{day:02d}/{client}/{keyword}"
            paths=[export_path(root,template,"{artifact_id}.png",self.values(client=c,month=m))
                   for c in ("APP","WEB") for m in (9,10)]
            self.assertEqual(len(set(paths)),4)
            self.assertIn("09",str(paths[0]))

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for directory in ["../bad","/absolute","C:\\bad","ok/../../bad"]:
                with self.subTest(directory=directory), self.assertRaises(ValueError):
                    export_path(root,directory,"x.png",self.values())

    def test_template_expression_rejected(self):
        with self.assertRaises(ValueError):
            Plan(directory_template="{keyword.__class__}")
        with self.assertRaises(ValueError):
            Plan(filename_template="{month!r}.png")

    def test_copy_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            src,dst=Path(root)/"src",Path(root)/"dst"
            src.write_bytes(b"evidence")
            copy_immutable(src,dst)
            with self.assertRaises(FileExistsError): copy_immutable(src,dst)
            self.assertEqual(src.read_bytes(),dst.read_bytes())

    def test_plan_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"config.json"; plan=Plan(output_root=root)
            save_plan(plan,p)
            self.assertEqual(plan,load_plan(p))

    def test_bad_configuration(self):
        for args in ({"top_n":True},{"top_n":0},{"keywords":[]},{"keywords":["A1","A1"]},
                     {"clients":["mobile"]},{"output_root":"relative"}):
            with self.subTest(args=args),self.assertRaises(ValueError): Plan(**args)

    def test_thirteen_daily_slots(self):
        slots=daily_slots(date(2026,9,8),Plan().schedule)
        self.assertEqual(len(slots),13)
        self.assertEqual((slots[0].hour,slots[-1].hour),(9,21))

    def test_cross_month_window(self):
        slots=daily_slots(date(2026,9,30),{"windows":[{"start":"23:30","end":"00:30","interval_minutes":30}]})
        self.assertEqual([s.month for s in slots],[9,10,10])

    def test_overlapping_slots_deduplicate(self):
        schedule={"times":["09:00","09:00"],"windows":[{"start":"09:00","end":"10:00","interval_minutes":60}]}
        self.assertEqual(len(daily_slots(date(2026,9,8),schedule)),2)

    def test_past_slots_not_replayed(self):
        self.assertEqual(due_slots(datetime(2026,9,8,10,5,tzinfo=TIMEZONE),Plan().schedule),[])

    def test_batch_idempotence(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"db.sqlite3")
            try:
                now=datetime.now(TIMEZONE)
                one,fresh=store.create_batch("key",now,{})
                two,again=store.create_batch("key",now,{})
                self.assertEqual(one,two);self.assertTrue(fresh);self.assertFalse(again)
            finally: store.close()

    def test_same_display_type_different_client(self):
        a,b=self.values(kind="suggestions"),self.values(client="WEB",kind="suggestions")
        self.assertEqual(a["display_type"],b["display_type"])
        self.assertNotEqual(a["client"],b["client"])


if __name__ == "__main__": unittest.main()
