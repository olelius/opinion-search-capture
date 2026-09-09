"""批次离线集成：注入本地图片采集器，不访问真实平台。"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from opinion_capture.config import Plan
from opinion_capture.runner import run_plan
from opinion_capture.scheduling import TIMEZONE


def fixture_collector(request):
    """测试替身只用于证明调度/导出逻辑，不代表真实平台通过。"""
    root=Path(request["output_dir"]);root.mkdir(parents=True,exist_ok=True)
    image=root/"original.png"
    Image.new("RGB",(40,80),"blue").save(image)
    return {**{k:request[k] for k in ("platform","client","capture_kind","keyword")},
            "status":"success","reason":"offline_test_fixture","rank_verified":True,
            "observed_count":request["top_n"],"requested_top_n":request["top_n"],
            "artifact_paths":[str(image)],"captured_at":"2026-09-08T09:00:02+08:00"}


class RunnerTests(unittest.TestCase):
    def test_session_block_suppresses_keywords_not_other_platform_and_resume(self):
        """同会话4任务仅1次实际派发；其他平台继续，明确恢复才重新尝试。"""
        calls = []
        def collector(request):
            calls.append((request["platform"], request["capture_kind"], request["keyword"]))
            result = fixture_collector(request)
            if request["platform"] == "xiaohongshu":
                result.update(status="blocked", reason="login_required", rank_verified=False)
            return result
        with tempfile.TemporaryDirectory() as root:
            plan = Plan(output_root=root, keywords=["A1", "A10"], clients=["WEB"],
                        platforms=["xiaohongshu", "bilibili"], max_retries=0)
            result = run_plan(plan, collector=collector)
            self.assertEqual(len([c for c in calls if c[0] == "xiaohongshu"]), 1)
            self.assertEqual(len([c for c in calls if c[0] == "bilibili"]), 4)
            self.assertEqual(sum(bool(r.get("request_suppressed")) for r in result["results"]), 3)
            calls.clear()
            def restored(request):
                calls.append(request["platform"])
                return fixture_collector(request)
            resumed = run_plan(plan, collector=restored, resume_batch_id=result["batch_id"])
            self.assertEqual(calls, ["xiaohongshu"] * 4)
            self.assertEqual(resumed["status"], "success")

    def test_exact_keywords_and_clients(self):
        with tempfile.TemporaryDirectory() as root:
            plan=Plan(output_root=root,keywords=["A1","A10"],platforms=["bilibili"],capture_kinds=["suggestions"])
            result=run_plan(plan,collector=fixture_collector)
            self.assertEqual(result["status"],"success")
            self.assertEqual(result["counts"],{"success":4})
            self.assertEqual(len(result["composites"]),2)
            paths=[r["exports"][0]["path"] for r in result["results"]]
            self.assertEqual(len(set(paths)),4)
            self.assertTrue(all(Path(p).is_file() for p in paths))

    def test_duplicate_trigger_is_not_reexecuted(self):
        with tempfile.TemporaryDirectory() as root:
            plan=Plan(output_root=root,platforms=["bilibili"],clients=["WEB"],capture_kinds=["suggestions"])
            now=datetime(2026,9,8,9,tzinfo=TIMEZONE)
            first=run_plan(plan,collector=fixture_collector,scheduled_at=now)
            second=run_plan(plan,collector=fixture_collector,scheduled_at=now)
            self.assertTrue(second["skipped_duplicate"])
            self.assertEqual(first["batch_id"],second["batch_id"])

    def test_blocker_never_exported_as_result(self):
        def blocked(request):
            r=fixture_collector(request);r.update(status="blocked",reason="login_required");return r
        with tempfile.TemporaryDirectory() as root:
            result=run_plan(Plan(output_root=root,platforms=["bilibili"],clients=["WEB"]),collector=blocked)
            self.assertEqual(result["status"],"blocked")
            self.assertFalse(result["composites"])
            self.assertTrue(all(not r["exports"] for r in result["results"]))

    def test_timezone_representation_does_not_duplicate_batch(self):
        with tempfile.TemporaryDirectory() as root:
            plan=Plan(output_root=root,platforms=["bilibili"],clients=["WEB"],capture_kinds=["suggestions"])
            local=datetime(2026,9,8,9,tzinfo=TIMEZONE)
            first=run_plan(plan,collector=fixture_collector,scheduled_at=local)
            second=run_plan(plan,collector=fixture_collector,scheduled_at=local.astimezone(timezone.utc))
            self.assertEqual(first["batch_id"],second["batch_id"])
            self.assertTrue(second["skipped_duplicate"])

    def test_unverified_top_n_is_partial(self):
        def unverified(request):
            r=fixture_collector(request);r["rank_verified"]=False;return r
        with tempfile.TemporaryDirectory() as root:
            result=run_plan(Plan(output_root=root,platforms=["bilibili"],clients=["WEB"],capture_kinds=["results"]),collector=unverified)
            self.assertEqual(result["status"],"partial")
            self.assertIn("未完整验证",result["results"][0]["exports"][0]["path"])

    def test_wrong_keyword_rejected(self):
        def wrong(request):
            r=fixture_collector(request);r["keyword"]="wrong";return r
        with tempfile.TemporaryDirectory() as root:
            result=run_plan(Plan(output_root=root,clients=["WEB"]),collector=wrong)
            self.assertEqual(result["status"],"failed")
            self.assertFalse(result["composites"])


if __name__ == "__main__": unittest.main()
