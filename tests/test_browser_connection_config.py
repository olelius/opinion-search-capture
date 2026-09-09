"""仅验证新增浏览器配置及离屏表单往返；不显示窗口、不运行采集或连接浏览器。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication

from opinion_capture.config import Plan, load_plan, save_plan
from opinion_capture.gui import MainWindow, default_plan_data, validate_plan_data
from opinion_capture.gui_helpers import EXISTING_CHROME_GUIDANCE, core_login


class BrowserConnectionConfigTests(unittest.TestCase):
    """旧配置兼容、端点约束和 JSON 往返均走正式配置入口。"""

    def test_old_plan_defaults_and_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(json.dumps({"keywords": ["配置验证"]}), encoding="utf-8")
            plan = load_plan(path)
            self.assertEqual((plan.browser_connection_mode, plan.browser_endpoint), ("dedicated", "auto"))
            save_plan(plan, path)
            self.assertEqual(load_plan(path), plan)
            old_form = default_plan_data()
            old_form.update(keywords=["配置验证"])
            old_form.pop("browser_connection_mode")
            old_form.pop("browser_endpoint")
            checked = validate_plan_data(old_form)
            self.assertEqual(checked["browser_connection_mode"], "dedicated")
            self.assertEqual(checked["browser_endpoint"], "auto")

    def test_loopback_endpoints_roundtrip_without_profile_lookup(self):
        endpoints = ("auto", "http://localhost:9222", "http://127.0.0.1:9222",
                     "ws://[::1]:9222/devtools/browser/config-test")
        with patch("opinion_capture.sessions.profile_directory") as profile:
            for endpoint in endpoints:
                with self.subTest(endpoint=endpoint):
                    plan = Plan(browser_connection_mode="existing_chrome", browser_endpoint=endpoint)
                    self.assertEqual(Plan.from_dict(plan.to_dict()), plan)
                    self.assertEqual(validate_plan_data(plan.to_dict())["browser_endpoint"], endpoint)
            profile.assert_not_called()

    def test_invalid_browser_fields_rejected_by_plan_and_form(self):
        invalid = [("browser_connection_mode", value) for value in ("", "chrome", None, 1)]
        invalid += [("browser_endpoint", value) for value in (
            None, 123, "", "https://127.0.0.1:9222", "file:///tmp/chrome",
            "http://example.com:9222", "http://192.168.1.2:9222", "http://0.0.0.0:9222",
            "ws://[::]:9222/devtools/browser/id", "http://localhost.example.com:9222",
            "http://user:password@127.0.0.1:9222", "http://@localhost:9222",
            "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:bad",
        )]
        data = default_plan_data()
        data.update(keywords=["配置验证"], browser_connection_mode="existing_chrome")
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                changed = dict(data, **{field: value})
                with self.assertRaises(ValueError):
                    Plan.from_dict(changed)
                with self.assertRaises(ValueError):
                    validate_plan_data(changed)

    def test_existing_login_bridge_does_not_open_dedicated_session(self):
        with patch("opinion_capture.sessions.open_web_login") as login, \
                patch("opinion_capture.sessions.profile_directory") as profile:
            result = core_login({"browser_connection_mode": "existing_chrome"}, "bilibili")
            self.assertEqual(result, {"status": "blocked", "reason": EXISTING_CHROME_GUIDANCE})
            login.assert_not_called()
            profile.assert_not_called()


class BrowserConnectionFormTests(unittest.TestCase):
    """只操作新增控件与保存/加载方法；所有后台入口都是禁止调用的替身。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.provider = Mock(side_effect=AssertionError("配置往返不应调用后台入口"))
        self.window = MainWindow(
            runner=self.provider, preflight=self.provider, service_factory=self.provider,
            device_provider=self.provider, login_provider=self.provider,
            history_provider=self.provider, autoload=False,
        )

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.provider.assert_not_called()

    def test_form_save_load_preserves_modes_endpoints_and_hidden_profile(self):
        window = self.window
        self.assertEqual(window.browser_connection_mode_combo.currentText(), "独立网页会话")
        self.assertEqual(window.browser_endpoint_edit.text(), "自动发现")
        self.assertTrue(window.login_button.isEnabled())
        self.assertFalse(window.dedicated_browser_controls.isHidden())
        data = default_plan_data()
        data.update(keywords=["配置验证"])
        with tempfile.TemporaryDirectory() as temporary:
            data["browser_profile_root"] = str(Path(temporary) / "dedicated")
            for index, (mode, endpoint) in enumerate((
                ("existing_chrome", "auto"), ("existing_chrome", "http://127.0.0.1:9222"),
                ("existing_chrome", "ws://[::1]:9222/devtools/browser/config-test"),
                ("dedicated", "http://localhost:9222"),
            )):
                with self.subTest(mode=mode, endpoint=endpoint):
                    expected = dict(data, browser_connection_mode=mode, browser_endpoint=endpoint)
                    window.set_form_data(expected)
                    self.assertEqual(window.form_data(), expected)
                    self.assertEqual(window.dedicated_browser_controls.isHidden(), mode == "existing_chrome")
                    self.assertEqual(window.login_button.isEnabled(), mode == "dedicated")
                    path = Path(temporary) / f"plan-{index}.json"
                    window.save_config(path)
                    window.browser_endpoint_edit.setText("自动发现")
                    window.load_config(path)
                    self.assertEqual(window.form_data(), expected)
                    self.assertEqual(load_plan(path).browser_endpoint, endpoint)
            self.assertFalse((Path(temporary) / "dedicated").exists())

    def test_existing_mode_changes_only_configuration_and_blocks_old_login(self):
        window = self.window
        window.browser_connection_mode_combo.setCurrentIndex(
            window.browser_connection_mode_combo.findData("existing_chrome")
        )
        self.assertTrue(window.dedicated_browser_controls.isHidden())
        self.assertFalse(window.login_button.isEnabled())
        self.assertFalse(window.existing_chrome_note.isHidden())
        self.assertTrue(window.browser_endpoint_edit.isEnabled())
        with patch("opinion_capture.gui.ExecutionThread") as worker, \
                patch("opinion_capture.gui.QFileDialog.getExistingDirectory") as chooser:
            window.login_button.click()
            window.open_web_login()
            window.choose_profile_directory()
            worker.assert_not_called()
            chooser.assert_not_called()
        self.assertIn("chrome://inspect/#remote-debugging", window.status_label.toolTip())
        self.assertIsNone(window._worker)
        window.browser_connection_mode_combo.setCurrentIndex(
            window.browser_connection_mode_combo.findData("dedicated")
        )
        self.assertTrue(window.login_button.isEnabled())
        self.assertFalse(window.dedicated_browser_controls.isHidden())


if __name__ == "__main__":
    unittest.main()
