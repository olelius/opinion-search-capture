"""offscreen 表单与线程测试；全部执行器为替身，不接触浏览器和模拟器。"""

import copy
import json
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontMetrics
from opinion_capture.gui import MainWindow, PROJECT_ROOT, default_plan_data, run_core, validate_plan_data


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        self.window = MainWindow(runner=lambda data: self.calls.append(data) or {"status": "blocked", "reason": "测试替身"}, autoload=False)
        self.data = default_plan_data()
        self.data["keywords"] = ["新能源汽车", "A1", "A10"]
        self.window.set_form_data(self.data)

    def tearDown(self):
        self.wait_finished()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def wait_finished(self):
        deadline = time.monotonic() + 3
        while self.window._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.window._worker, "替身线程应及时退出")

    def test_create_defaults_and_no_automatic_execution(self):
        self.assertEqual(self.window.form_data(), self.data)
        self.assertEqual(Path(self.data["output_root"]), PROJECT_ROOT / ".local" / "exports")
        self.assertTrue(self.window.run_button.isEnabled())
        self.assertEqual(self.calls, [])

    def test_chinese_font_available(self):
        self.assertTrue(QFontMetrics(self.window.font()).inFontUcs4(ord("中")))

    def test_form_roundtrip_multiple_windows_and_fields(self):
        data = copy.deepcopy(self.data)
        data.update(name="夜间方案", project="测试项目", platforms=["bilibili", "xiaohongshu"],
                    clients=["WEB"], capture_kinds=["suggestions"], top_n=73,
                    directory_template="固定/{project}/{client}", filename_template="{keyword}.png", extension="保留")
        data["schedule"] = {"windows": [{"start": "08:15", "end": "10:45", "interval_minutes": 15},
                                        {"start": "16:00", "end": "19:00", "interval_minutes": 30}],
                            "times": ["07:45", "21:15"], "timezone": "Asia/Shanghai"}
        self.window.set_form_data(data)
        self.assertEqual(self.window.form_data(), data)

    def test_preview_deterministic_and_no_run(self):
        self.window.preview_config()
        before = self.window.preview_edit.toPlainText()
        self.window.preview_config()
        self.assertEqual(before, self.window.preview_edit.toPlainText())
        self.assertEqual(json.loads(before), self.data)
        self.assertEqual(self.window.preview_button.text(), "配置预览")
        self.assertEqual(self.calls, [])

    def test_save_load_tempdir_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "配置.json"
            self.window.save_config(path)
            original = path.read_bytes()
            self.window.name_edit.setText("已修改")
            with self.assertRaises(FileExistsError):
                self.window.save_config(path)
            self.assertEqual(path.read_bytes(), original)
            self.window.load_config(path)
            self.assertEqual(self.window.form_data(), self.data)
            self.assertEqual(self.calls, [])

    def test_invalid_load_retains_form(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text('{"keywords": []}', encoding="utf-8")
            with self.assertRaises(ValueError):
                self.window.load_config(path)
        self.assertEqual(self.window.form_data(), self.data)

    def test_composite_controls_save_load_and_core_roundtrip(self):
        """离屏控件/JSON 往返：旧上限没有输入框，列宽可编辑，布局不被隐式重写。"""
        from opinion_capture.config import Plan, load_plan

        for field in ("composite_max_pixels", "composite_max_height"):
            self.assertNotIn(field, self.window.optional_widgets)
        columns = self.window.composite_columns_spin
        self.assertEqual(columns.parentWidget().layout().labelForField(columns).text(), "每行关键词数")
        for layout, pixels, height in (("grid", 50_000_000, 24000), ("horizontal", 0, None)):
            with self.subTest(layout=layout):
                self.window.set_form_data({**self.data, "composite_layout": layout,
                                           "composite_max_pixels": pixels, "composite_max_height": height})
                self.assertEqual(self.window.layout_combo.currentData(), layout)
                self.assertIn("PNG", self.window.layout_combo.currentText())
                self.assertIn("按列数换行" if layout == "grid" else "单行", self.window.layout_combo.currentText())
                self.window.composite_directory_edit.setText("拼图/{project}/{platform}/{client}")
                self.window.composite_filename_edit.setText("总览_{artifact_id}.png")
                columns.setValue(7)
                self.window.optional_widgets["composite_cell_width"].setValue(720)
                expected = self.window.form_data()
                self.assertEqual(expected["composite_layout"], layout)
                self.assertEqual(expected["composite_columns"], 7)
                self.assertEqual(expected["composite_cell_width"], 720)
                self.assertEqual(expected["composite_max_pixels"], pixels)
                self.assertEqual(expected["composite_max_height"], height)
                self.assertEqual(expected["directory_template"], self.data["directory_template"])
                self.assertEqual(expected["filename_template"], self.data["filename_template"])
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "composite.json"
                    self.window.save_config(path)
                    self.assertEqual(load_plan(path).to_dict(), Plan.from_dict(expected).to_dict())
                    self.window.set_form_data(self.data)
                    self.window.load_config(path)
                    self.assertEqual(self.window.form_data(), expected)
                self.assertEqual(columns.value(), 7)
                self.assertEqual(self.window.layout_combo.currentData(), layout)
                self.assertEqual(self.window.composite_filename_edit.text(), "总览_{artifact_id}.png")
        self.assertEqual(self.calls, [])

    def test_legacy_config_gets_composite_defaults(self):
        legacy = copy.deepcopy(self.data)
        for key in ("composite_directory_template", "composite_filename_template", "composite_columns"):
            del legacy[key]
        self.window.set_form_data(legacy)
        self.assertEqual(self.window.form_data(), self.data)

    def test_composite_validation_retains_current_form(self):
        for field, value in (("composite_columns", 0), ("composite_columns", 101),
                             ("composite_columns", True), ("composite_directory_template", ""),
                             ("composite_filename_template", "{artifact_id}.jpg"),
                             ("composite_filename_template", "nested/{artifact_id}.png"),
                             ("filename_template", "{keyword}.jpg")):
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(self.data)
                data[field] = value
                with self.assertRaises(ValueError):
                    self.window.set_form_data(data)
                self.assertEqual(self.window.form_data(), self.data)

    def test_validation(self):
        for field, value in (("keywords", []), ("platforms", []), ("clients", ["OTHER"]),
                             ("capture_kinds", []), ("top_n", 0), ("top_n", 1001), ("top_n", True),
                             ("keywords", ["重复", "重复"]),
                             ("output_root", "relative"), ("filename_template", "")):
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(self.data)
                data[field] = value
                with self.assertRaises(ValueError):
                    validate_plan_data(data)
        for window in ({"start": "25:00", "end": "21:00", "interval_minutes": 60},
                       {"start": "09:00", "end": "21:00", "interval_minutes": 1441},
                       {"start": "09:00", "end": "21:00", "interval_minutes": 0}):
            data = copy.deepcopy(self.data)
            data["schedule"]["windows"] = [window]
            with self.assertRaises(ValueError):
                validate_plan_data(data)

    def test_cross_midnight_and_keyword_whitespace_roundtrip(self):
        data = copy.deepcopy(self.data)
        data["keywords"] = [" A1 ", "A10"]
        data["schedule"]["windows"][0].update(start="21:00", end="09:00")
        self.window.set_form_data(data)
        self.assertEqual(self.window.form_data(), data)

    def test_core_config_json_interoperability(self):
        try:
            from opinion_capture.config import Plan, load_plan, save_plan
        except ModuleNotFoundError as exc:
            if exc.name == "opinion_capture.config":
                self.skipTest("核心配置文件尚未落盘")
            raise
        self.window.composite_directory_edit.setText("拼图/{platform}/{client}")
        self.window.composite_filename_edit.setText("汇总_{artifact_id}.png")
        self.window.composite_columns_spin.setValue(6)
        plan = Plan.from_dict(self.window.form_data())
        with tempfile.TemporaryDirectory() as temp:
            gui_path = Path(temp) / "gui.json"
            core_path = Path(temp) / "core.json"
            self.window.save_config(gui_path)
            self.assertEqual(load_plan(gui_path).to_dict(), plan.to_dict())
            save_plan(plan, core_path)
            self.window.load_config(core_path)
            self.assertEqual(self.window.form_data(), plan.to_dict())

    def test_core_adapter_with_fake_runner_only(self):
        # 只导入配置类；真实 runner 模块被替身遮蔽，不触发平台接口。
        try:
            from opinion_capture.config import Plan
        except ModuleNotFoundError as exc:
            if exc.name == "opinion_capture.config":
                self.skipTest("核心配置文件尚未落盘")
            raise
        fake_module = types.ModuleType("opinion_capture.runner")
        received = []
        fake_module.run_plan = lambda plan: received.append(plan) or {"status": "blocked", "reason": "隔离测试"}
        with patch.dict("sys.modules", {"opinion_capture.runner": fake_module}):
            result = run_core(self.data)
        self.assertIsInstance(received[0], Plan)
        self.assertEqual(result, {"status": "blocked", "reason": "隔离测试"})

    def test_empty_windows_and_add_remove(self):
        self.window.remove_schedule_window()
        self.assertEqual(self.window.form_data()["schedule"]["windows"], [])
        self.window.add_schedule_window()
        self.assertEqual(self.window.form_data()["schedule"]["windows"], self.data["schedule"]["windows"])

    def test_async_snapshot_duplicate_click_and_close_guard(self):
        release = threading.Event()
        worker_thread_ids = []

        def fake_run(data):
            self.calls.append(data)
            worker_thread_ids.append(threading.get_ident())
            release.wait(2)
            return {"status": "partial", "reason": "仅得到 3 条结果"}

        self.window._runner = fake_run
        try:
            self.window.execute_once()
            self.assertFalse(self.window.run_button.isEnabled())
            self.window.execute_once()
            self.window.name_edit.setText("下一次计划")
            self.assertFalse(self.window.close())
            self.app.processEvents()
        finally:
            release.set()
        self.wait_finished()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["name"], self.data["name"])
        self.assertNotEqual(worker_thread_ids[0], threading.get_ident())
        self.assertIn("partial", self.window.status_label.text())
        self.assertIn("仅得到 3 条结果", self.window.status_label.text())
        self.assertNotIn("完成成功", self.window.status_label.text())
        self.assertTrue(self.window._closing)
        self.assertFalse(self.window.run_button.isEnabled(), "已请求关闭时不重新开放采集")

    def test_runner_exception_and_missing_status(self):
        def fake_run(data):
            raise RuntimeError("测试接口异常")
        self.window._runner = fake_run
        self.window.execute_once()
        self.wait_finished()
        self.assertIn("error", self.window.status_label.text())
        self.assertIn("测试接口异常", self.window.status_label.text())
        self.window.show_result({"reason": "未给出状态"})
        self.assertIn("missing_status", self.window.status_label.text())

    def test_missing_core_has_clear_error(self):
        with patch.dict("sys.modules", {"opinion_capture.config": None, "opinion_capture.runner": None}):
            independent = MainWindow(autoload=False)
            try:
                independent.set_form_data(self.data)
                independent.preview_config()
                self.assertEqual(json.loads(independent.preview_edit.toPlainText()), self.data)
            finally:
                independent.close()
                independent.deleteLater()
            with self.assertRaisesRegex(RuntimeError, "核心执行接口尚未就绪"):
                run_core(self.data)


if __name__ == "__main__":
    unittest.main()
