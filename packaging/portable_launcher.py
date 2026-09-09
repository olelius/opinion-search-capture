"""GUI/CLI共用冻结入口；worker路由必须先于任何GUI或业务初始化。"""

from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing
from pathlib import Path
import runpy
import sys
import time
import traceback

# 源码调试只添加本仓库src；冻结运行绝不寻找源码盘或开发venv。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portable_runtime import WEB_MODULES, apply_dependencies, discover_dependencies, worker_route


def run_worker(route: tuple[str, str]) -> int:
    """执行固定worker接口；Android stdout专用于XML/blocked JSON。"""
    kind, payload = route
    if kind == "existing-browser":
        if sys.stdout is None:
            raise RuntimeError("现有浏览器工作进程应使用OpinionCapture-CLI.exe")
        from opinion_capture.existing_browser import worker_main
        return worker_main()
    if kind == "android":
        if sys.stdout is None:
            raise RuntimeError("Android worker请使用同目录OpinionCapture-CLI.exe以保留stdout管道")
        module = importlib.import_module("opinion_capture.android")
        module._ui_worker(json.loads(payload))
    else:
        previous = sys.argv
        try:
            sys.argv = [WEB_MODULES[kind], "--worker", str(Path(payload).resolve())]
            runpy.run_module(WEB_MODULES[kind], run_name="__main__")
        finally:
            sys.argv = previous
    return 0


def run_gui(config_path: Path) -> int:
    """只展示窗口并传递用户配置；不创建采集任务、不自动启动调度。"""
    from PySide6.QtWidgets import QApplication
    from opinion_capture.gui import MainWindow
    app = QApplication.instance() or QApplication([sys.argv[0]])
    window = MainWindow(config_path=config_path)
    screen = app.primaryScreen()
    if screen:
        area = screen.availableGeometry()
        window.resize(min(1180, area.width() - 40), min(840, area.height() - 60))
    window.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    """默认GUI；显式CLI/离线自检/内部worker互斥，不把未知参数当GUI。"""
    args = list(sys.argv[1:] if argv is None else argv)
    route = worker_route(args)
    if route is not None:
        return run_worker(route)
    if args[:1] == ["--portable-check"]:
        from portable_check import main as check_main
        return check_main(args[1:])
    if args[:1] == ["--worker-check"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--worker-check", choices=("android", "bilibili", "xiaohongshu"), required=True)
        option = parser.parse_args(args)
        from portable_check import adapter_probe, offline_guard
        with offline_guard():
            adapter_probe("android" if option.worker_check == "android" else "web_" + option.worker_check)
        print(json.dumps({"ok": True, "worker": option.worker_check, "platform_access": False}), flush=True)
        return 0
    if args[:1] == ["--probe-child"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--probe-child", type=Path, required=True)
        parser.add_argument("--late-marker", type=Path, required=True)
        option = parser.parse_args(args)
        option.probe_child.write_text(str(__import__("os").getpid()), encoding="ascii")
        time.sleep(1.0)
        option.late_marker.write_text("process survived termination", encoding="ascii")
        return 0

    from opinion_capture.paths import application_root, default_config_path
    parser = argparse.ArgumentParser(description="便携入口：无参数打开GUI；离线校验使用 --portable-check")
    parser.add_argument("action", nargs="?", default="gui",
                        choices=("gui", "preview", "run", "watch", "history", "init-config", "devices"))
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--resume-batch")
    parser.add_argument("--dependencies", type=Path, default=None,
                        help="外部依赖JSON；默认EXE旁portable-dependencies.json")
    option = parser.parse_args(args)
    if option.resume_batch and option.action != "run":
        parser.error("--resume-batch仅用于run")
    config = option.config.resolve() if option.config else default_config_path()
    dependencies = option.dependencies.resolve() if option.dependencies else application_root() / "portable-dependencies.json"
    if option.dependencies and not dependencies.is_file():
        parser.error("指定的依赖配置文件不存在")
    apply_dependencies(discover_dependencies(dependencies))
    if option.action == "gui":
        return run_gui(config)
    if option.action == "init-config":
        from opinion_capture.config import Plan
        from opinion_capture.paths import default_output_root
        from portable_runtime import load_defaults
        plan = Plan.from_dict(dict(load_defaults(), output_root=str(default_output_root())))
        config.parent.mkdir(parents=True, exist_ok=True)
        with config.open("x", encoding="utf-8") as stream:
            json.dump(plan.to_dict(), stream, ensure_ascii=False, indent=2)
        print(f"配置已生成：{config}")
        return 0
    previous = sys.argv
    try:
        sys.argv = [previous[0], option.action, "--config", str(config)]
        if option.resume_batch:
            sys.argv.extend(["--resume-batch", option.resume_batch])
        from opinion_capture.__main__ import main as core_main
        return core_main()
    finally:
        sys.argv = previous


def entrypoint() -> int:
    """冻结入口设置；windowed诊断写日志，CLI保留退出码与stdout。"""
    multiprocessing.freeze_support()
    if sys.stdout is None or sys.stderr is None:
        from opinion_capture.paths import application_root
        directory = application_root() / ".local" / "packaging"
        directory.mkdir(parents=True, exist_ok=True)
        stream = (directory / "launcher.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        return main()
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(entrypoint())
