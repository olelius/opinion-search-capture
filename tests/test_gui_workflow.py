"""GUI 完整操作流程离屏回归；调度和采集全部使用替身，不连接真实平台。"""

import copy
import json
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QTableView
from opinion_capture.gui import MainWindow, default_plan_data, run_core, OPTIONAL_FIELDS, validate_plan_data
from opinion_capture.gui_helpers import core_preflight, core_history, core_schedule, RecordModel, task_rows, status_text


class FakeService:
    """不会读取时钟/数据库的调度替身，停止接收不等于工作已结束。"""

    def __init__(self, data, *, progress_callback):
        self.data = copy.deepcopy(data)
        self.callback = progress_callback
        self.is_running = False
        self.started = threading.Event()
        self.stop_calls = []
        self.thread_ids = []
        self.last_error = None

    def start(self):
        self.thread_ids.append(threading.get_ident())
        self.is_running = True
        self.started.set()

    def stop(self, wait=False, cancel_running=False):
        self.thread_ids.append(threading.get_ident())
        self.stop_calls.append((wait, cancel_running))
        if cancel_running:
            self.is_running = False


class WorkflowTests(unittest.TestCase):
    def test_choose_adb_saves_local_selection_then_refreshes(self):
        from opinion_capture import android_discovery
        with tempfile.TemporaryDirectory() as directory:
            adb=Path(directory)/"adb.exe"
            adb.touch()
            record=Path(directory)/".local/android-tools.json"
            with patch.object(android_discovery,"_selection_file",return_value=record), \
                 patch("opinion_capture.gui.QFileDialog.getOpenFileName",return_value=(str(adb),"")), \
                 patch.object(self.window,"refresh_devices") as refresh, patch.dict(os.environ,{}):
                self.window.choose_adb()
                refresh.assert_called_once()
                self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["adb"],str(adb))
                self.assertEqual(os.environ["OPINION_CAPTURE_ADB"],str(adb))
                self.assertTrue(self.window.adb_select_button.isEnabled())

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.releases = []
        self.services = []
        self.runner = Mock(return_value={"status": "blocked", "reason": "隔离执行"})
        self.history = Mock(return_value=[])
        self.devices = Mock(return_value=[])
        self.preflight = Mock(return_value={"ok": True, "originals": [], "composites": [], "errors": [], "warnings": []})
        self.window = MainWindow(runner=self.runner, preflight=self.preflight,
            device_provider=self.devices, history_provider=self.history,
            service_factory=self.factory, autoload=False)
        self.data = default_plan_data()
        self.data.update(keywords=["A1", "A10"], platforms=["bilibili"], clients=["WEB"])
        self.window.set_form_data(self.data)

    def factory(self, data, **kwargs):
        service = FakeService(data, **kwargs)
        self.services.append(service)
        return service

    def until(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate(), "离屏替身应按预期结束/发出事件")

    def idle(self):
        self.until(lambda: not self.window._runtime_busy() and not self.window._reads)

    def tearDown(self):
        for event in self.releases:
            event.set()
        for service in self.services:
            service.is_running = False
        self.window._force_exit = True
        self.window.close()
        self.idle()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_no_implicit_execution_or_discovery(self):
        self.window.preview_config()
        self.runner.assert_not_called()
        self.history.assert_not_called()
        self.devices.assert_not_called()
        self.assertEqual(self.services, [])
        self.assertIn("首次使用", self.window.config_label.text())
        self.assertFalse(self.window.tray_check.isChecked())

    def test_startup_autoload_is_read_only_and_errors_guide(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plan.json"
            path.write_text(json.dumps(self.data), encoding="utf-8")
            original = path.read_bytes()
            window = MainWindow(runner=self.runner, service_factory=self.factory, config_path=path)
            try:
                self.assertEqual(window.form_data(), self.data)
                self.assertIn("自动加载", window.status_label.text())
                self.assertEqual(path.read_bytes(), original)
                self.runner.assert_not_called()
                self.assertEqual(self.services, [])
            finally:
                window.close()
                window.deleteLater()
            path.write_text("{broken", encoding="utf-8")
            window = MainWindow(config_path=path)
            try:
                self.assertIn("加载出错", window.status_label.text())
                self.assertEqual(window.keywords_edit.toPlainText(), "")
            finally:
                window.close()
                window.deleteLater()

    def test_save_same_file_confirmation_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "用户配置.json"
            self.window.save_config(path)
            initial = path.read_bytes()
            self.window.name_edit.setText("更改计划")
            with patch.object(self.window, "_confirm_overwrite", return_value=False):
                self.window.choose_save_current()
            self.assertEqual(path.read_bytes(), initial)
            with patch.object(self.window, "_confirm_overwrite", return_value=True):
                self.window.choose_save_current()
            changed = path.read_bytes()
            self.assertEqual(json.loads(changed)["name"], "更改计划")
            with patch("opinion_capture.gui.os.replace", side_effect=OSError("写入模拟失败")):
                with self.assertRaises(OSError):
                    self.window.save_config(path, overwrite=True)
            self.assertEqual(path.read_bytes(), changed)
            self.assertEqual([p.name for p in Path(folder).iterdir()], [path.name])

    def test_configuration_never_overwrites_image(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "原图.png"
            path.write_bytes(b"immutable-image")
            with self.assertRaises(ValueError):
                self.window.save_config(path, overwrite=True)
            self.assertEqual(path.read_bytes(), b"immutable-image")

    def test_named_field_insertion_preserves_template_selection(self):
        for field in ("directory_edit", "filename_edit", "composite_directory_edit", "composite_filename_edit"):
            edit = getattr(self.window, field)
            combo = self.window.field_combos[field]
            edit.setText("前缀_替换.png")
            edit.setSelection(3, 2)
            index = combo.findData("{month:02d}")
            self.assertGreater(index, 0)
            self.window.insert_field(edit, combo, index)
            self.assertEqual(edit.text(), "前缀_{month:02d}.png")
            self.assertEqual(combo.currentIndex(), 0)

    def test_optional_controls_match_core_and_preserve_fractional_delay(self):
        from opinion_capture.config import Plan
        self.window.optional_widgets["retry_delay_seconds"].setValue(0.125)
        self.window.layout_combo.setCurrentIndex(self.window.layout_combo.findData("horizontal"))
        data = self.window.form_data()
        self.assertEqual(Plan.from_dict(data).retry_delay_seconds, 0.125)
        self.window.set_form_data(data)
        self.assertEqual(self.window.form_data(), data)
        for field, (_, low, high, _) in OPTIONAL_FIELDS.items():
            invalid = {**data, field: high + 1}
            with self.assertRaises(ValueError, msg=field):
                self.window.set_form_data(invalid)
            self.assertEqual(self.window.form_data(), data)

    def test_loading_precise_retry_delay_does_not_silently_round_saved_value(self):
        data = {**self.data, "retry_delay_seconds": 0.123456789}
        self.window.set_form_data(data)
        self.assertEqual(self.window.form_data()["retry_delay_seconds"], 0.123456789)
        self.window.optional_widgets["retry_delay_seconds"].setValue(0.5)
        self.assertEqual(self.window.form_data()["retry_delay_seconds"], 0.5)

    def test_read_only_core_preflight_displays_real_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            data = {**self.data, "output_root": str(Path(folder) / "not-created"),
                    "directory_template": "{platform}/{client}/{keyword}",
                    "composite_directory_template": "汇总/{platform}/{client}"}
            self.window.set_form_data(data)
            self.window._preflight = core_preflight
            self.window.preview_paths()
            self.idle()
            self.assertIn("预检通过", self.window.preflight_label.text())
            self.assertEqual(self.window.path_model.rowCount(), 6)
            self.assertTrue(all(Path(row["path"]).is_absolute() for row in self.window.path_model.rows))
            self.assertEqual(list(Path(folder).iterdir()), [])
            self.runner.assert_not_called()
            self.assertIn("没有执行写入", self.window.preflight_details.toPlainText())

    def test_preflight_errors_and_stale_snapshot(self):
        release = threading.Event()
        self.releases.append(release)
        entered = threading.Event()
        thread_ids = []
        def preview(data):
            entered.set()
            thread_ids.append(threading.get_ident())
            release.wait(2)
            return {"ok": False, "errors": [{"code": "PATH_COLLISION", "message": "目标撞名"}], "warnings": []}
        self.window._preflight = preview
        self.window.preview_paths()
        self.until(entered.is_set)
        self.window.preview_paths()
        release.set()
        self.idle()
        self.assertIn("错误", self.window.preflight_label.text())
        self.assertIn("目标撞名", self.window.preflight_details.toPlainText())
        self.assertNotEqual(thread_ids[0], threading.get_ident())
        self.window.name_edit.setText("变更")
        self.assertEqual(self.window.path_model.rowCount(), 0)
        self.assertIn("重新预检", self.window.preflight_label.text())

    def test_stale_async_preflight_result_is_discarded(self):
        release = threading.Event()
        self.releases.append(release)
        self.window._preflight = lambda d: (release.wait(2) or True) and {"ok": True, "originals": ["example"]}
        self.window.preview_paths()
        self.window.name_edit.setText("读取期间变化")
        release.set()
        self.idle()
        self.assertEqual(self.window.path_model.rowCount(), 0)
        self.assertIn("重新预检", self.window.preflight_label.text())

    def test_devices_use_names_and_serial_data_and_keep_missing_selection(self):
        self.devices.return_value = [dict(name="测试模拟器", serial="SERIAL1", state="device"),
                                     dict(name="离线设备", serial="SERIAL2", state="offline")]
        self.window.refresh_devices()
        self.idle()
        self.assertIn("测试模拟器", self.window.device_combo.currentText())
        self.assertEqual(self.window.form_data()["android_serial"], "SERIAL1")
        self.assertFalse(self.window.device_combo.model().item(2).isEnabled())
        self.devices.return_value = [dict(name="其他设备", serial="SERIAL3", state="device")]
        self.window.refresh_devices()
        self.idle()
        self.assertEqual(self.window.form_data()["android_serial"], "SERIAL1")
        self.assertIn("未发现", self.window.device_combo.currentText())
        self.runner.assert_not_called()

    def test_tasks_use_model_view_and_exact_identity(self):
        data = {**self.data, "keywords": [f"词{i}" for i in range(1200)]}
        self.window.task_model.replace(task_rows(data))
        self.assertIsInstance(self.window.task_table, QTableView)
        self.assertEqual(self.window.task_model.rowCount(), 2400)
        row = dict(platform="bilibili", client="WEB", capture_kind="suggestions", keyword="词1", status="partial")
        self.window.task_model.update_task(row)
        matched = [r for r in self.window.task_model.rows if r["status"] == "partial"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["keyword"], "词1")
        self.assertEqual(status_text("partial"), "未完整验收")

    def test_cancel_request_not_false_finish_and_ui_progress(self):
        release = threading.Event()
        self.releases.append(release)
        started = threading.Event()
        received = []
        ui_threads = []
        self.window.task_model.dataChanged.connect(lambda *a: ui_threads.append(threading.get_ident()))
        def runner(data, *, cancel_event, progress_callback):
            received.append(cancel_event)
            progress_callback(dict(event="task_started", platform="bilibili", client="WEB",
                capture_kind="results", keyword="A1", task_id="T1", status="running", total=4, completed=0))
            started.set()
            release.wait(2)
            return {"status": "cancelled", "reason": "协作取消"}
        self.window._runner = runner
        self.window.execute_once()
        self.until(started.is_set)
        self.until(lambda: bool(ui_threads))
        self.window.cancel_once()
        self.assertTrue(received[0].is_set())
        self.assertIsNotNone(self.window._worker)
        self.assertFalse(self.window.run_button.isEnabled())
        self.assertTrue(all(t == threading.get_ident() for t in ui_threads))
        release.set()
        self.idle()
        self.assertIn("已取消", self.window.status_label.text())
        self.assertTrue(self.window.run_button.isEnabled())

    def test_close_waits_for_actual_worker_exit(self):
        release = threading.Event()
        self.releases.append(release)
        received = []
        def runner(data, **kwargs):
            received.append(kwargs["cancel_event"])
            release.wait(2)
            return {"status": "cancelled"}
        self.window._runner = runner
        self.window.show()
        self.window.execute_once()
        self.until(lambda: bool(received))
        self.assertFalse(self.window.close())
        self.assertTrue(self.window.isVisible())
        self.assertTrue(received[0].is_set())
        release.set()
        self.idle()
        self.until(lambda: not self.window.isVisible())

    def test_runner_typeerror_is_not_reexecuted(self):
        calls = []
        def bad(data, **kwargs):
            calls.append(data)
            raise TypeError("执行器内部错误")
        self.window._runner = bad
        self.window.execute_once()
        self.idle()
        self.assertEqual(len(calls), 1)
        self.assertIn("执行异常", self.window.status_label.text())

    def test_core_bridge_passes_cancel_progress_resume_exactly(self):
        fake = types.ModuleType("opinion_capture.runner")
        fake.run_plan = Mock(return_value={"status": "cancelled"})
        event, callback = threading.Event(), Mock()
        with patch.dict("sys.modules", {"opinion_capture.runner": fake}):
            result = run_core(self.data, cancel_event=event, progress_callback=callback, resume_batch_id="B1")
        self.assertEqual(result["status"], "cancelled")
        self.assertIs(fake.run_plan.call_args.kwargs["cancel_event"], event)
        self.assertIs(fake.run_plan.call_args.kwargs["progress_callback"], callback)
        self.assertEqual(fake.run_plan.call_args.kwargs["resume_batch_id"], "B1")
        self.assertNotIn("collector", fake.run_plan.call_args.kwargs)

    def test_real_runner_with_mock_collector_feeds_gui_and_history(self):
        """真实核心仅以注入采集替身联调；断言真实适配器从未调用。"""
        from datetime import datetime
        from opinion_capture.config import Plan
        from opinion_capture.runner import run_plan
        from opinion_capture.scheduling import TIMEZONE
        with tempfile.TemporaryDirectory() as folder:
            data = {**self.data, "output_root": str(Path(folder) / "exports"),
                    "directory_template": "{platform}/{client}/{keyword}",
                    "composite_directory_template": "汇总/{platform}/{client}"}
            self.window.set_form_data(data)
            def collector(request):
                return {**{key: request[key] for key in ("platform", "client", "capture_kind", "keyword")},
                        "status": "blocked", "reason": "隔离采集替身，未访问平台", "artifact_paths": [],
                        "captured_at": datetime.now(TIMEZONE).isoformat(), "rank_verified": False}
            self.window._runner = lambda snapshot, **kwargs: run_plan(Plan.from_dict(snapshot), collector=collector, **kwargs)
            self.window._history_provider = core_history
            with patch("opinion_capture.runner._adapter", side_effect=AssertionError("禁止实际采集")) as adapter:
                self.window.execute_once()
                self.idle()
                adapter.assert_not_called()
            self.assertEqual(self.window.task_model.rowCount(), 4)
            self.assertTrue(all(row["status"] == "blocked" for row in self.window.task_model.rows))
            self.assertEqual(self.window.history_model.rowCount(), 1)
            self.assertTrue(Path(self.window.history_model.rows[0]["report_path"]).is_file())
            self.assertEqual(self.window.progress_bar.value(), 4)
            self.assertIn("受阻", self.window.status_label.text())

    def test_schedule_stop_drain_then_cancel_and_mutual_exclusion(self):
        self.window.start_schedule()
        self.until(lambda: bool(self.services) and self.services[0].started.is_set())
        service = self.services[0]
        self.window.start_schedule()
        self.window.execute_once()
        self.assertEqual(len(self.services), 1)
        self.runner.assert_not_called()
        self.window.stop_schedule()
        self.until(lambda: bool(service.stop_calls))
        self.assertEqual(service.stop_calls[0], (False, False))
        self.assertIsNotNone(self.window._scheduler)
        self.assertTrue(service.is_running)
        self.assertFalse(self.window.run_button.isEnabled())
        self.window.stop_schedule(cancel_running=True)
        self.idle()
        self.assertEqual(service.stop_calls[-1], (False, True))
        self.assertTrue(all(i != threading.get_ident() for i in service.thread_ids))
        self.assertTrue(self.window.run_button.isEnabled())

    def test_schedule_close_uses_cancel_running(self):
        self.window.show()
        self.window.start_schedule()
        self.until(lambda: bool(self.services) and self.services[0].started.is_set())
        self.assertFalse(self.window.close())
        self.idle()
        self.assertEqual(self.services[0].stop_calls, [(False, True)])
        self.until(lambda: not self.window.isVisible())

    def test_schedule_exception_is_not_presented_as_normal_stop(self):
        self.window.start_schedule()
        self.until(lambda: bool(self.services) and self.services[0].started.is_set())
        self.services[0].last_error = "计划接口故障"
        self.services[0].is_running = False
        self.idle()
        self.assertIn("计划接口故障", self.window.schedule_label.text())
        self.assertIn("执行异常", self.window.schedule_label.text())

    def test_empty_schedule_does_not_start_service(self):
        self.window.remove_schedule_window()
        self.window.start_schedule()
        self.assertEqual(self.services, [])
        self.assertIn("请先设置", self.window.status_label.text())

    def test_core_schedule_factory_constructs_plan_without_start(self):
        fake = types.ModuleType("opinion_capture.service")
        fake.ScheduleService = Mock()
        callback = Mock()
        with patch.dict("sys.modules", {"opinion_capture.service": fake}):
            service = core_schedule(self.data, progress_callback=callback)
        self.assertEqual(fake.ScheduleService.call_args.args[0].keywords, ["A1", "A10"])
        self.assertIs(fake.ScheduleService.call_args.kwargs["progress_callback"], callback)
        service.start.assert_not_called()

    def test_schedule_buttons_click_and_early_close(self):
        self.window.start_button.click()
        self.until(lambda: bool(self.services) and self.services[0].started.is_set())
        self.window.stop_button.click()
        self.until(lambda: bool(self.services[0].stop_calls))
        self.window.stop_cancel_button.click()
        self.idle()
        self.assertEqual(self.services[0].stop_calls, [(False, False), (False, True)])

    def test_portable_default_paths_use_shared_paths_module(self):
        from opinion_capture.paths import application_root, default_config_path
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "portable.exe"
            with patch("sys.frozen", True, create=True), patch("sys.executable", str(exe)):
                self.assertEqual(application_root(), Path(folder))
                self.assertEqual(default_plan_data()["output_root"], str(Path(folder) / ".local" / "exports"))
                self.assertEqual(default_config_path(), Path(folder) / ".local" / "plan.json")

    def test_schedule_batch_result_refreshes_history_and_keeps_unknown_status(self):
        self.window.show_progress(dict(event="batch_finished", batch_id="B1", total=4,
            completed=4, status="partial", reason="示例最终状态", report_path=""))
        self.idle()
        self.history.assert_called_once()
        self.assertEqual(self.window.progress_bar.value(), 4)
        self.assertEqual(json.loads(self.window.preview_edit.toPlainText())["batch_id"], "B1")
        self.window.show_result({"status": "future-state"})
        self.assertIn("未知状态", self.window.status_label.text())

    def test_history_filter_sort_and_open_local_report(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.json"
            report.write_text("{}", encoding="utf-8")
            self.history.return_value = [dict(id="B1", status="partial", planned_at="2026-09-08", report_path=str(report), output_root=folder),
                                        dict(id="B2", status="success", planned_at="2026-09-09", output_root=folder)]
            self.window.refresh_history()
            self.idle()
            self.window.history_filter.setText("未完整验收")
            self.assertEqual(self.window.history_proxy.rowCount(), 1)
            self.window.history_table.selectRow(0)
            with patch("opinion_capture.gui.QDesktopServices.openUrl", return_value=True) as opener:
                self.window.open_history_output()
                self.window.open_history_report()
            self.assertEqual(len(opener.call_args_list), 2)
            self.assertTrue(all(c.args[0].isLocalFile() for c in opener.call_args_list))
            self.assertEqual(Path(opener.call_args.args[0].toLocalFile()), report)

    def test_real_history_empty_root_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "uncreated"
            self.assertEqual(core_history(str(root)), [])
            self.assertFalse(root.exists())

    def test_resume_uses_history_snapshot_not_current_form(self):
        history_config = {**self.data, "name": "历史快照"}
        self.history.return_value = [dict(id="OLD-BATCH", status="partial", config_json=json.dumps(history_config))]
        self.window.refresh_history()
        self.idle()
        self.window.name_edit.setText("当前编辑内容")
        self.window.history_table.selectRow(0)
        with patch("opinion_capture.gui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.window.resume_selected()
        self.idle()
        self.assertEqual(self.runner.call_args.args[0]["name"], "历史快照")
        self.assertEqual(self.runner.call_args.kwargs["resume_batch_id"], "OLD-BATCH")

    def test_unavailable_tray_remains_visible(self):
        self.window.show()
        with patch("opinion_capture.gui.QSystemTrayIcon.isSystemTrayAvailable", return_value=False):
            self.window.tray_check.setChecked(True)
        self.assertFalse(self.window.tray_check.isChecked())
        self.assertTrue(self.window.isVisible())

    def test_explicit_tray_close_hides_without_cancelling(self):
        tray = Mock()
        tray.isVisible.return_value = True
        self.window._tray = tray
        self.window.tray_check.blockSignals(True)
        self.window.tray_check.setChecked(True)
        self.window.tray_check.blockSignals(False)
        self.window.show()
        self.window.start_schedule()
        self.until(lambda: bool(self.services) and self.services[0].started.is_set())
        self.assertFalse(self.window.close())
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.services[0].stop_calls, [])
        self.window.request_exit()
        self.idle()
        self.assertEqual(self.services[0].stop_calls, [(False, True)])


if __name__ == "__main__":
    unittest.main()
