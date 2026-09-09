"""便携规范纯测试：替身验证入口与路径，不运行平台、GUI或正式构建。"""

import ast
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "src"))
import build_support
import portable_launcher as launcher
import portable_runtime as runtime
from portable_check import adapter_probe, offline_guard


class PackagingTests(unittest.TestCase):
    """所有测试产物留在.local/packaging，不清理或改写其他开发中的文件。"""

    @classmethod
    def setUpClass(cls):
        cls.base = ROOT / ".local" / "packaging" / ("pure-tests-" + uuid.uuid4().hex)
        cls.base.mkdir(parents=True, exist_ok=False)

    def new_dir(self):
        directory = self.base / uuid.uuid4().hex
        directory.mkdir()
        return directory

    def setUp(self):
        """隔离launcher的进程环境设置，避免影响其他并行开发测试。"""
        guard = patch.dict(os.environ, {"PATH": ""}, clear=True)
        guard.start()
        self.addCleanup(guard.stop)

    def test_no_arguments_routes_only_gui(self):
        with patch.object(launcher, "run_gui", return_value=0) as gui, patch.object(launcher, "run_worker") as worker:
            self.assertEqual(launcher.main([]), 0)
        from opinion_capture.paths import default_config_path
        gui.assert_called_once_with(default_config_path())
        worker.assert_not_called()

    def test_gui_receives_explicit_config(self):
        config = self.new_dir() / "中文配置.json"
        with patch.object(launcher, "run_gui", return_value=0) as gui:
            launcher.main(["gui", "--config", str(config)])
        gui.assert_called_once_with(config.resolve())

    def test_unknown_argument_never_falls_into_gui(self):
        with patch.object(launcher, "run_gui") as gui, patch("sys.stderr"), self.assertRaises(SystemExit):
            launcher.main(["--typo"])
        gui.assert_not_called()

    def test_ui_worker_routed_before_gui_and_dependencies(self):
        payload = '{"serial":"probe","action":"dump"}'
        with patch.object(launcher, "run_worker", return_value=0) as worker, patch.object(launcher, "run_gui") as gui, patch.object(launcher, "discover_dependencies") as deps:
            self.assertEqual(launcher.main(["--ui-worker", payload]), 0)
        worker.assert_called_once_with(("android", payload))
        gui.assert_not_called()
        deps.assert_not_called()

    def test_ui_worker_forwards_json_to_internal_function(self):
        module = Mock()
        with patch.object(launcher.importlib, "import_module", return_value=module):
            self.assertEqual(launcher.run_worker(("android", '{"action":"dump"}')), 0)
        module._ui_worker.assert_called_once_with({"action": "dump"})

    def test_ui_worker_exception_propagates(self):
        module = Mock()
        module._ui_worker.side_effect = RuntimeError("worker failed")
        with patch.object(launcher.importlib, "import_module", return_value=module), self.assertRaises(RuntimeError):
            launcher.run_worker(("android", "{}"))

    def test_worker_json_requires_exactly_one_object(self):
        for args in (["--ui-worker"], ["--ui-worker", "[]"], ["--ui-worker", "{}", "extra"], ["--ui-worker", "oops"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                runtime.worker_route(args)

    def test_web_worker_canonical_and_legacy_whitelist(self):
        for name in ("bilibili", "xiaohongshu"):
            expected = (name, "job.json")
            self.assertEqual(runtime.worker_route(["--web-worker", name, "job.json"]), expected)
            self.assertEqual(runtime.worker_route(["-B", "-m", "opinion_capture.web_" + name, "--worker", "job.json"]), expected)

    def test_generic_python_execution_not_accepted(self):
        for args in (["-c", "print(1)"], ["-m", "arbitrary", "--worker", "job.json"], ["--web-worker", "arbitrary", "job.json"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                runtime.worker_route(args)

    def test_web_worker_restores_argv(self):
        previous = sys.argv
        with patch.object(launcher.runpy, "run_module", side_effect=RuntimeError), self.assertRaises(RuntimeError):
            launcher.run_worker(("xiaohongshu", "job.json"))
        self.assertIs(sys.argv, previous)

    def test_freeze_support_precedes_routing(self):
        calls = []
        with patch.object(launcher.multiprocessing, "freeze_support", side_effect=lambda: calls.append("freeze")), patch.object(launcher, "main", side_effect=lambda: calls.append("main") or 0):
            self.assertEqual(launcher.entrypoint(), 0)
        self.assertEqual(calls, ["freeze", "main"])

    def test_worker_failure_returns_nonzero(self):
        with patch.object(launcher.multiprocessing, "freeze_support"), patch.object(launcher, "main", side_effect=RuntimeError("probe")), patch.object(launcher.traceback, "print_exc"):
            self.assertEqual(launcher.entrypoint(), 2)

    def test_cli_passes_resume_and_config_preserves_argv(self):
        from opinion_capture import __main__ as core
        previous = sys.argv
        captured = []
        path = self.new_dir() / "plan.json"
        with patch.object(core, "main", side_effect=lambda: captured.extend(sys.argv) or 0):
            self.assertEqual(launcher.main(["run", "--config", str(path), "--resume-batch", "batch-id"]), 0)
        self.assertEqual(captured[1:], ["run", "--config", str(path), "--resume-batch", "batch-id"])
        self.assertIs(sys.argv, previous)

    def test_init_config_uses_neutral_resource_and_no_overwrite(self):
        path = self.new_dir() / "plan.json"
        with patch("builtins.print"):
            self.assertEqual(launcher.main(["init-config", "--config", str(path)]), 0)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["keywords"], ["示例关键词"])
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            launcher.main(["init-config", "--config", str(path)])
        self.assertEqual(path.read_bytes(), before)

    def test_resource_root_distinct_from_exe_directory(self):
        directory = self.new_dir()
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(directory / "_internal"), create=True), patch.object(sys, "executable", str(directory / runtime.GUI_EXE)):
            self.assertEqual(runtime.resource_root(), directory / "_internal" / "portable_resources")

    def test_defaults_no_machine_specific_output_or_profiles(self):
        data = runtime.load_defaults()
        self.assertNotIn("output_root", data)
        text = json.dumps(data).lower()
        self.assertNotIn("profile", text)
        self.assertNotIn("cookie", text)

    def test_dependency_environment_overrides_config(self):
        directory = self.new_dir()
        env_exe, configured = directory / "custom.exe", directory / "configured.exe"
        env_exe.touch()
        configured.touch()
        config = directory / "deps.json"
        config.write_text(json.dumps({"browser": str(configured)}), encoding="utf-8")
        result = runtime.discover_dependencies(config, environ={"OPINION_CAPTURE_BROWSER": str(env_exe)})
        self.assertEqual(result["browser"]["path"], str(env_exe))
        self.assertEqual(result["browser"]["source"], "environment")

    def test_relative_dependency_path_uses_config_directory(self):
        directory = self.new_dir()
        (directory / "adb.exe").touch()
        config = directory / "deps.json"
        config.write_text('{"adb":"adb.exe"}', encoding="utf-8")
        result = runtime.discover_dependencies(config, environ={})
        self.assertEqual(result["adb"]["path"], str(directory / "adb.exe"))

    def test_missing_explicit_dependency_does_not_fall_back(self):
        result = runtime.discover_dependencies(environ={"OPINION_CAPTURE_ADB": str(self.base / "missing.exe")})
        self.assertEqual(result["adb"]["status"], "invalid")
        with self.assertRaises(ValueError):
            runtime.apply_dependencies(result)

    def test_discovery_never_launches_external_program(self):
        with offline_guard():
            result = runtime.discover_dependencies(environ={})
        self.assertTrue(all(item["status"] == "missing" for item in result.values()))
        self.assertTrue(all(not item["runtime_verified"] for item in result.values()))

    def test_adb_configuration_sets_both_adbutils_and_path(self):
        directory = self.new_dir()
        adb = directory / "adb.exe"
        adb.touch()
        with patch.dict(os.environ, {"PATH": "sentinel"}, clear=True):
            result = runtime.discover_dependencies(environ={"OPINION_CAPTURE_ADB": str(adb)})
            runtime.apply_dependencies(result)
            self.assertEqual(os.environ["ADBUTILS_ADB_PATH"], str(adb))
            self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], str(directory))

    def test_source_subprocess_uses_base_not_venv_redirector(self):
        with patch.object(sys, "executable", "venv-redirector.exe"), patch.object(sys, "_base_executable", "base-python.exe", create=True):
            command, env = runtime.source_python_command(ROOT / "packaging/portable_launcher.py", "--worker-check", "android")
        self.assertEqual(command[0], "base-python.exe" if os.name == "nt" else "venv-redirector.exe")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_frozen_never_uses_source_python_subprocess(self):
        with patch.object(sys, "frozen", True, create=True), self.assertRaises(RuntimeError):
            runtime.source_python_command(ROOT / "packaging/portable_launcher.py")

    def test_adapter_probe_imports_but_never_captures(self):
        module = Mock(__name__="opinion_capture.android", __file__="probe.py")
        with patch("portable_check.importlib.import_module", return_value=module):
            self.assertFalse(adapter_probe("android")["capture_called"])
        module.capture.assert_not_called()

    def test_fingerprint_ignores_profiles_and_docs(self):
        directory = self.new_dir()
        (directory / "src").mkdir()
        code = directory / "src/module.py"
        code.write_text("x=1", encoding="utf-8")
        before = build_support.source_fingerprint(directory)
        (directory / "docs").mkdir()
        (directory / "docs/private.jpg").write_bytes(b"do not include")
        self.assertEqual(before, build_support.source_fingerprint(directory))
        code.write_text("x=2", encoding="utf-8")
        self.assertNotEqual(before["sha256"], build_support.source_fingerprint(directory)["sha256"])

    def test_payload_policy_excludes_private_and_external_binaries(self):
        for name in ("docs/source.jpg", "_internal/adbutils/binaries/adb.exe", ".local/plan.json", "profiles/Cookies", "state.sqlite3"):
            with self.subTest(name=name):
                self.assertTrue(build_support.forbidden_member(name))
        self.assertFalse(build_support.forbidden_member("_internal/uiautomator2/assets/u2.jar"))
        self.assertFalse(build_support.forbidden_member("_internal/portable_resources/portable-defaults.json"))

    def test_dist_boundary_rejects_root_and_traversal(self):
        directory = self.new_dir()
        dist = directory / "dist"
        dist.mkdir()
        for target in (directory, dist, dist / "../outside"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                build_support.ensure_dist_child(target, directory)
        self.assertEqual(build_support.ensure_dist_child(dist / "candidate/bundle", directory), dist / "candidate/bundle")

    def test_spec_has_two_entrypoints_and_dynamic_modules(self):
        text = (ROOT / "packaging/opinion_capture.spec").read_text(encoding="utf-8")
        ast.parse(text)
        for name in ("opinion_capture.android_results", "opinion_capture.web_results_bilibili", "opinion_capture.web_results_xiaohongshu"):
            self.assertIn(name, text)
        self.assertIn('console=False', text)
        self.assertIn('console=True', text)
        self.assertIn('COLLECT(gui, cli', text)

    def test_build_requires_core_stable_and_structural_checks_only(self):
        text = (ROOT / "scripts/build-portable.ps1").read_text(encoding="utf-8-sig")
        for token in ("$CoreStable", "'audit'", "source-before.json", "source-after.json", "Resolve-Path"):
            self.assertIn(token, text)
        self.assertNotIn("& $cli", text)
        self.assertIn('frozenRuntimeVerified = $false', text)
        self.assertNotIn("Remove-Item", text)
        self.assertNotIn("Move-Item", text)
        self.assertNotIn("--noconfirm", text)


if __name__ == "__main__":
    unittest.main()
