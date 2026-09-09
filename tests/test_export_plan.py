"""批次预检的路径、冻结分组和执行边界；全部临时产物位于 .local/export-tests。"""

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from opinion_capture.config import Plan
from opinion_capture.export_plan import preflight_plan
from opinion_capture.exporting import artifact_values, exact_group_key, export_path
from opinion_capture.scheduling import TIMEZONE

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 8, 23, 59, tzinfo=TIMEZONE)


class ExportPlanTests(unittest.TestCase):
    """不借助未发生的截图时间产生虚假唯一性结论。"""

    def setUp(self):
        base = ROOT / ".local/export-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, **changes):
        """返回字典，验证预检不依赖 Plan 构造时的验证副作用。"""
        values = Plan(output_root=str(self.root / "out"), keywords=["A1", "A10", "a1", "A/1", "A:1"],
                      platforms=["bilibili"], clients=["WEB"], capture_kinds=["suggestions"]).to_dict()
        return {**values, **changes}

    def check(self, **changes):
        return preflight_plan(self.plan(**changes), planned_at=NOW)

    def test_default_pure_deterministic_and_ducktype(self):
        before = sorted(self.root.rglob("*"))
        plan = SimpleNamespace(**self.plan())
        one = preflight_plan(plan, planned_at=NOW)
        two = preflight_plan(plan, planned_at=NOW)
        self.assertTrue(one["ok"], one["errors"])
        self.assertEqual(one, two)
        self.assertEqual(before, sorted(self.root.rglob("*")))
        self.assertEqual(len(one["raw_paths"]), 5)
        self.assertEqual(one["raw_paths"], one["originals"])
        self.assertEqual(one["composite_paths"][0]["members"][1]["keyword"], "A10")
        self.assertEqual(one["composition"]["composite_cell_width"], 1080)
        self.assertEqual(one["composition"]["composite_max_height"], 24000)
        json.dumps(one, ensure_ascii=False)

    def test_keywords_case_and_illegal_character_disambiguation(self):
        r = self.check(directory_template="档案-{year}-{month:02d}/{keyword}",
                       filename_template="任意文字-{keyword_id}.png")
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(len({item["path"].casefold() for item in r["raw_paths"]}), 5)
        self.assertEqual(len({item["keyword_id"] for item in r["raw_paths"]}), 5)

    def test_no_invented_capture_time_uniqueness(self):
        r = self.check(directory_template="raw", filename_template="{capture_stamp}.png")
        self.assertFalse(r["ok"])
        self.assertTrue(any(e["code"] == "PATH_COLLISION" for e in r["errors"]))
        self.assertEqual(len({p["values"]["capture_stamp"] for p in r["raw_paths"]}), 1)
        self.assertTrue(any(w["code"] == "UNKNOWN_CAPTURE_TIME" for w in r["warnings"]))

    def test_batch_cross_type_and_case_collision(self):
        r = self.check(keywords=["A1"], directory_template="raw", filename_template="SAME.png",
                       composite_directory_template="RAW", composite_filename_template="same.PNG")
        self.assertFalse(r["ok"])
        self.assertTrue(any(e["code"] == "PATH_COLLISION" for e in r["errors"]))

    def test_old_template_needs_per_page_id_or_part(self):
        r = self.check(composite_filename_template="{artifact_id}.png")
        self.assertTrue(r["ok"], r["errors"])
        self.assertTrue(r["composite_paths"][0]["requires_unique_id_per_part"])
        r = self.check(composite_filename_template="汇总.png")
        self.assertTrue(any(w["code"] == "MULTIPART_COLLISION" for w in r["warnings"]))

    def test_cross_year_and_exact_grouping(self):
        r = self.check(directory_template="{month:02d}/{keyword}", filename_template="固定.png")
        self.assertTrue(any(w["code"] == "CROSS_YEAR_COLLISION" for w in r["warnings"]))
        values = dict(project="项目", platform="bilibili", client="APP", capture_kind="results",
                      keyword="A1", top_n=20, batch=1, artifact_id="same")
        one = artifact_values(**values, captured_at=NOW)
        two = artifact_values(**values, captured_at=NOW.replace(year=2027))
        self.assertNotEqual(export_path(self.root, "{year}/{month}/{keyword}", "a.png", one),
                            export_path(self.root, "{year}/{month}/{keyword}", "a.png", two))
        common = dict(platform="bilibili", client="APP", capture_kind="suggestions")
        groups = [exact_group_key(**common, captured_at=NOW.replace(year=y), keyword=k)
                  for y in (2026, 2027) for k in ("A1", "A10", "a1", "A/1", "A:1")]
        self.assertEqual(len(set(groups)), 10)
        self.assertEqual(exact_group_key(**common, captured_at=NOW),
                         exact_group_key(**common, captured_at=NOW.astimezone(timezone.utc)))
        self.assertNotEqual(exact_group_key(**common, captured_at=NOW),
                            exact_group_key(**(common | {"client": "WEB"}), captured_at=NOW))

    def test_custom_templates_remain_independent(self):
        r = self.check(directory_template="原图/自定义-{year}-{month:02d}/{keyword}",
                       filename_template="证据-{artifact_id}.png", composite_directory_template="总图/{year}",
                       composite_filename_template="全部-{artifact_id}-{part:02d}.png")
        self.assertTrue(r["ok"], r["errors"])
        self.assertIn("原图", r["raw_paths"][0]["path"])
        self.assertIn("总图", r["composite_paths"][0]["path"])

    def test_missing_field_and_unsafe_templates(self):
        for changes in ({"project": None}, {"keywords": []}, {"platforms": ["bad"]},
                        {"clients": ["WEB", "WEB"]}, {"top_n": True}, {"directory_template": "{unknown}"},
                        {"filename_template": "{keyword.__class__}.png"}, {"filename_template": "{keyword!r}.png"},
                        {"filename_template": "{}.png"}, {"filename_template": "{month:99999d}.png"},
                        {"directory_template": "../out"}, {"filename_template": "CON.png"},
                        {"filename_template": "bad?.png"}, {"composite_layout": "diagonal"},
                        {"composite_max_height": 0}):
            with self.subTest(changes=changes):
                r = self.check(**changes)
                self.assertFalse(r["ok"])
                self.assertTrue(r["errors"])

    def test_bad_root_and_time(self):
        self.assertFalse(self.check(output_root="relative")["ok"])
        self.assertFalse(preflight_plan(self.plan(), planned_at=datetime(2026, 9, 8))["ok"])

    def test_long_path_and_utf16_limit(self):
        for name in ("中" * 241 + ".png", "😀" * 121 + ".png"):
            r = self.check(filename_template=name)
            self.assertFalse(r["ok"])
            self.assertTrue(any(e["code"] == "INVALID_PATH" for e in r["errors"]))

    def test_existing_file_and_parent_obstruction(self):
        r = self.check()
        p = Path(r["raw_paths"][0]["path"])
        p.parent.mkdir(parents=True)
        p.write_bytes(b"old evidence")
        r = self.check()
        self.assertFalse(r["ok"])
        self.assertTrue(any(e["code"] == "PATH_EXISTS" for e in r["errors"]))
        blocker = self.root / "file"
        blocker.write_bytes(b"keep")
        r = self.check(output_root=str(blocker / "subdir"))
        self.assertFalse(r["ok"])
        self.assertTrue(any(e["code"] == "NOT_WRITABLE" for e in r["errors"]))

    def test_optional_probe_and_denied_permissions(self):
        before = list(self.root.rglob("*"))
        r = preflight_plan(self.plan(), planned_at=NOW, probe_writable=True)
        self.assertTrue(r["ok"], r["errors"])
        self.assertTrue(any(c["writable"] for c in r["writable_checks"]))
        self.assertEqual(before, list(self.root.rglob("*")))
        with patch("opinion_capture.export_plan.os.access", return_value=False):
            r = self.check()
        self.assertFalse(r["ok"])
        self.assertTrue(any(e["code"] == "NOT_WRITABLE" for e in r["errors"]))
        with patch("opinion_capture.export_plan.tempfile.mkstemp", side_effect=PermissionError("denied")):
            r = preflight_plan(self.plan(), planned_at=NOW, probe_writable=True)
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
