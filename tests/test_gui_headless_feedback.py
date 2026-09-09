"""无头字段、登录反馈与阶段展示的独立定点；不连接平台、不采集或截图。"""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from opinion_capture.config import Plan, load_plan, save_plan
from opinion_capture.gui import MainWindow, default_plan_data, validate_plan_data
from opinion_capture.gui_helpers import task_rows


class HeadlessPlanTests(unittest.TestCase):
    """正式配置入口保证旧 JSON 缺省和严格布尔类型，两种连接方式均保留值。"""

    def test_legacy_default_and_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text('{"keywords": ["配置定点"]}', encoding="utf-8-sig")
            legacy = load_plan(path)
            self.assertIs(legacy.browser_headless, False)
            for mode in ("dedicated", "existing_chrome"):
                for value in (False, True):
                    with self.subTest(mode=mode, value=value):
                        data = dict(legacy.to_dict(), browser_connection_mode=mode, browser_headless=value)
                        plan = Plan.from_dict(data)
                        save_plan(plan, path)
                        self.assertIs(json.loads(path.read_text(encoding="utf-8"))["browser_headless"], value)
                        self.assertEqual(load_plan(path), plan)
                        self.assertIs(validate_plan_data(data)["browser_headless"], value)

    def test_headless_rejects_non_boolean_in_plan_and_form(self):
        data = default_plan_data()
        data["keywords"] = ["配置定点"]
        for mode in ("dedicated", "existing_chrome"):
            for value in (None, "false", "true", 0, 1, [], {}):
                with self.subTest(mode=mode, value=value):
                    changed = dict(data, browser_headless=value, browser_connection_mode=mode)
                    for validator in (Plan.from_dict, validate_plan_data):
                        with self.assertRaisesRegex(ValueError, "browser_headless"):
                            validator(changed)


class GuiHeadlessFeedbackTests(unittest.TestCase):
    """只驱动指定表单及 GUI 槽；所有后台入口均设置为调用即失败的哨兵。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.provider = Mock(side_effect=AssertionError("GUI 定点不调用后台入口"))
        self.window = MainWindow(
            runner=self.provider, preflight=self.provider, service_factory=self.provider,
            device_provider=self.provider, login_provider=self.provider,
            history_provider=self.provider, autoload=False,
        )
        self.data = default_plan_data()
        self.data.update(keywords=["配置定点"], platforms=["bilibili"], clients=["WEB"])
        self.window.set_form_data(self.data)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.provider.assert_not_called()

    def test_headless_form_roundtrip_and_existing_mode_retains_disabled_value(self):
        window = self.window
        self.assertFalse(window.browser_headless_check.isChecked())
        self.assertTrue(window.browser_headless_check.isEnabled())
        self.assertEqual(window.browser_headless_check.text(), "后台无头采集（登录仍打开窗口）")
        with tempfile.TemporaryDirectory() as directory:
            profile = str(Path(directory) / "profile")
            for index, (mode, value) in enumerate((
                ("dedicated", True), ("existing_chrome", True),
                ("existing_chrome", False), ("dedicated", False),
            )):
                with self.subTest(mode=mode, value=value):
                    expected = dict(self.data, browser_connection_mode=mode,
                                    browser_headless=value, browser_profile_root=profile)
                    window.set_form_data(expected)
                    self.assertEqual(window.form_data(), expected)
                    self.assertFalse(window.browser_headless_check.isHidden())
                    self.assertEqual(window.browser_headless_check.isEnabled(), mode == "dedicated")
                    if mode == "existing_chrome":
                        self.assertIn("不改变已有浏览器", window.browser_headless_note.text())
                    path = Path(directory) / f"plan-{index}.json"
                    window.save_config(path)
                    window.browser_headless_check.setChecked(not value)
                    window.load_config(path)
                    self.assertEqual(window.form_data(), expected)
                    self.assertIs(load_plan(path).browser_headless, value)
            self.assertFalse(Path(profile).exists())
        # 已勾选后再加载旧文件，缺省值应恢复 False，而不是继承上份配置。
        window.browser_headless_check.setChecked(True)
        legacy = dict(self.data)
        legacy.pop("browser_headless")
        window.set_form_data(legacy)
        self.assertFalse(window.browser_headless_check.isChecked())
        self.assertIs(window.form_data()["browser_headless"], False)

    def test_headless_invalidates_preview_and_respects_busy_state(self):
        window = self.window
        window._preview_snapshot = copy.deepcopy(self.data)
        window.browser_headless_check.setChecked(True)
        self.assertIsNone(window._preview_snapshot)
        self.assertIs(window.form_data()["browser_headless"], True)
        try:
            window._reads["preflight"] = object()
            window._sync_buttons()
            self.assertFalse(window.browser_headless_check.isEnabled())
        finally:
            window._reads.clear()
            window._sync_buttons()
        self.assertTrue(window.browser_headless_check.isEnabled())
        window.browser_connection_mode_combo.setCurrentIndex(
            window.browser_connection_mode_combo.findData("existing_chrome"))
        window._sync_buttons()
        self.assertFalse(window.browser_headless_check.isEnabled())
        self.assertIs(window.form_data()["browser_headless"], True)
        window.browser_connection_mode_combo.setCurrentIndex(
            window.browser_connection_mode_combo.findData("dedicated"))
        self.assertTrue(window.browser_headless_check.isEnabled())
        self.assertTrue(window.browser_headless_check.isChecked())

    def test_verified_and_persisted_login_does_not_save_edited_plan(self):
        window = self.window
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            window.save_config(path)
            saved = path.read_bytes()
            window.name_edit.setText("尚未确认保存的计划修改")
            pending = window.form_data()
            label = window.config_label.text()
            result = dict(status="success", authentication_verified=True, session_persisted=True,
                          outcome="manual_session_closed",
                          reason="manual_window_closed_authentication_not_asserted")
            with patch.object(window, "save_config") as save, \
                    patch.object(window, "choose_save_current") as choose:
                window._login_result(result)
                save.assert_not_called()
                choose.assert_not_called()
            message = window.status_label.toolTip()
            self.assertIn("平台登录已确认", message)
            self.assertIn("浏览器会话已保存", message)
            self.assertIn("采集计划配置", message)
            self.assertNotIn("登录未确认", message)
            self.assertNotIn("登录状态未验证", message)
            self.assertEqual(window.form_data(), pending)
            self.assertEqual(window.config_label.text(), label)
            self.assertEqual(window._config_path, path.resolve())
            self.assertEqual(path.read_bytes(), saved)
            self.assertEqual(json.loads(window.preview_edit.toPlainText()), result)

    def test_verified_login_checks_persistence_marker_separately(self):
        for extra, expected in (({}, "保存结果待确认"), ({"session_persisted": False}, "保存未成功")):
            with self.subTest(extra=extra):
                self.window._login_result(dict(status="success", authentication_verified=True, **extra))
                message = self.window.status_label.toolTip()
                self.assertIn("平台登录已确认", message)
                self.assertIn(expected, message)
                self.assertNotIn("浏览器会话已保存", message)

    def test_manual_close_preserves_session_but_does_not_confirm_login(self):
        for extra in ({}, {"authentication_verified": False, "session_persisted": False}):
            with self.subTest(extra=extra):
                result = dict(status="success", outcome="manual_session_closed",
                              reason="manual_window_closed_authentication_not_asserted", **extra)
                self.window._login_result(result)
                message = self.window.status_label.toolTip()
                self.assertIn("浏览器会话已保留，登录未确认", message)
                self.assertNotIn("未保存", message)
                self.assertNotIn("平台登录已确认", message)
                self.assertNotIn("会话已保存", message)
                self.assertEqual(json.loads(self.window.preview_edit.toPlainText()), result)
                self.assertIsNone(self.window._config_path)

    def test_phase_updates_display_without_task_status_counts_or_result_changes(self):
        window = self.window
        row = dict(task_rows(self.data)[0], status="running", reason="原任务说明", task_id="TASK")
        window.task_model.replace([row])
        window._last_batch = "BATCH"
        window.progress_bar.setRange(0, 5)
        window.progress_bar.setValue(2)
        original_status = window.status_label.text()
        for event_name in ("phase", "task_phase"):
            with self.subTest(event=event_name):
                event = dict(event=event_name, batch_id="BATCH", task_id="TASK",
                             phase_code="capture", phase_text="正在截图", elapsed_seconds=12.25,
                             status="success", total=100, completed=99,
                             task=dict(row, status="failed"))
                window.show_progress(event)
                self.assertIn("正在截图", window.phase_label.text())
                self.assertIn("12.2 秒", window.phase_label.text())
                self.assertIn("capture", window.phase_label.toolTip())
                self.assertEqual(window.task_model.rows, [row])
                self.assertEqual((window.progress_bar.value(), window.progress_bar.maximum()), (2, 5))
                self.assertEqual(window.status_label.text(), original_status)
                self.assertIsNone(window._last_result)
        # 对齐 runner 的完整身份 + running 事件：同阶段每两秒刷新及换阶段均只更新标签。
        with patch.object(window.task_model, "replace", wraps=window.task_model.replace) as replace, \
                patch.object(window.task_model, "update_task", wraps=window.task_model.update_task) as update:
            for code, text, elapsed, phase_elapsed in (
                ("capture", "正在截图", 12.0, 2.0),
                ("capture", "正在截图", 14.0, 4.0),
                ("validate", "正在校验", 14.1, 0.0),
                ("validate", "正在校验", 16.1, 2.0),
            ):
                window.show_progress(dict(row, event="phase", batch_id="BATCH", phase_code=code,
                                          phase_text=text, elapsed_seconds=elapsed,
                                          phase_elapsed_seconds=phase_elapsed))
                self.assertIn(text, window.phase_label.text())
                self.assertIn(f"本次采集累计耗时 {elapsed:.1f} 秒", window.phase_label.text())
                self.assertIn(f"当前阶段耗时 {phase_elapsed:.1f} 秒", window.phase_label.text())
                self.assertEqual(window.task_model.rows, [row])
                self.assertEqual((window.progress_bar.value(), window.progress_bar.maximum()), (2, 5))
            replace.assert_not_called()
            update.assert_not_called()

    def test_result_fields_show_deliverables_and_keep_original_stage_timings(self):
        window = self.window
        rows = task_rows(self.data)
        window.task_model.replace(rows)
        timings = [{"phase_code": "capture", "phase_text": "正在截图", "seconds": 4.321}]
        rows[0].update(status="failed", has_deliverable=False, stage_timings=timings,
                       reason="capture_incomplete", completion_detail="未产生最终截图，保留诊断证据")
        rows[1].update(status="partial", has_deliverable=True, stage_timings=timings,
                       reason="图片完整性待验收")
        result = dict(status="partial", results=rows, total=2, completed=2)
        window.show_result(result)
        for index, row in enumerate(rows):
            detail = window.task_model.data(window.task_model.index(index, 5))
            self.assertIn(row["reason"], detail)
            self.assertEqual(window.task_model.rows[index]["status"], row["status"])
            self.assertEqual(window.task_model.rows[index]["stage_timings"], timings)
        self.assertIn("未产生最终截图，保留诊断证据",
                      window.task_model.data(window.task_model.index(0, 5)))
        self.assertIn("已有最终截图", window.task_model.data(window.task_model.index(1, 5)))
        self.assertEqual(window.task_model.data(window.task_model.index(0, 4)), "失败")
        self.assertEqual(window.task_model.data(window.task_model.index(1, 4)), "未完整验收")
        self.assertEqual(json.loads(window.preview_edit.toPlainText()), result)

    def test_phase_fallback_and_late_phase_keep_partial_terminal_state(self):
        window = self.window
        row = dict(task_rows(self.data)[0], status="running", task_id="TASK")
        window.task_model.replace([row])
        for elapsed in (None, -1, "3", True, float("nan"), float("inf")):
            with self.subTest(elapsed=elapsed):
                window.show_progress(dict(event="phase", phase_code="waiting", elapsed_seconds=elapsed))
                self.assertIn("waiting", window.phase_label.text())
                self.assertIn("耗时待上报", window.phase_label.text())
        final_row = dict(row, status="partial", reason="已有图片，完整性待验收")
        window.show_progress(dict(event="task_finished", **final_row))
        finished_display = window.phase_label.text()
        window.show_progress(dict(event="task_phase", task_id="TASK", phase_text="任务结束后的晚到阶段",
                                  elapsed_seconds=17, status="running"))
        self.assertEqual(window.phase_label.text(), finished_display)
        self.assertEqual(window.task_model.rows[0]["status"], "partial")
        result = dict(status="partial", results=[final_row], total=1, completed=1)
        window.show_result(result)
        final_model = copy.deepcopy(window.task_model.rows)
        display = window.phase_label.text()
        message = window.status_label.text()
        window.show_progress(dict(event="phase", task_id="TASK", phase_text="晚到截图阶段",
                                  elapsed_seconds=18, status="running", completed=0))
        self.assertEqual(window.phase_label.text(), display)
        self.assertEqual(window.status_label.text(), message)
        self.assertEqual(window.task_model.rows, final_model)
        self.assertEqual(window._last_result, result)
        self.assertEqual(window.progress_bar.value(), 1)
        index = window.task_model.index(0, 4)
        self.assertEqual(window.task_model.data(index), "未完整验收")
        self.assertIn("不等于截图成功", window.task_model.data(index, Qt.ItemDataRole.ToolTipRole))
        # 下个批次开始后重新接收阶段，前一批次的终态护栏不阻断新任务。
        window.show_progress(dict(event="batch_started", batch_id="NEXT", status="running"))
        window.show_progress(dict(event="task_started", batch_id="NEXT", **row))
        window.show_progress(dict(event="phase", batch_id="NEXT", task_id="TASK",
                                  phase_text="新批次连接", elapsed_seconds=0))
        self.assertIn("新批次连接", window.phase_label.text())
        self.assertIn("0.0 秒", window.phase_label.text())


if __name__ == "__main__":
    unittest.main()
