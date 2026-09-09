"""可在源码及冻结包中运行的离线验收；从不触发平台或ADB访问。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import importlib
from importlib import resources
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from unittest.mock import patch
import uuid

from portable_runtime import ADAPTERS, CLI_EXE, discover_dependencies, load_defaults, resource_root, source_python_command


@contextmanager
def offline_guard():
    """阻断Python网络入口和外部进程启动；只在明确本地子进程探针外启用。"""
    def forbidden(*args, **kwargs):
        raise RuntimeError("portable-check禁止网络、浏览器、ADB及其他外部进程调用")
    with ExitStack() as stack:
        for obj, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                          (socket.socket, "sendto"), (socket, "getaddrinfo"),
                          (subprocess, "Popen"), (os, "system")):
            stack.enter_context(patch.object(obj, name, forbidden))
        yield


def require(condition: bool, message: str) -> None:
    """不依赖可被优化关闭的assert；失败进入JSON报告并导致非零退出。"""
    if not condition:
        raise RuntimeError(message)


def adapter_probe(name: str) -> dict:
    """按runner相同方式动态导入，检查capture接口但绝不调用。"""
    if name not in ADAPTERS:
        raise ValueError("未知适配器")
    module = importlib.import_module("opinion_capture." + name)
    require(callable(getattr(module, "capture", None)), f"{name}.capture缺失")
    return {"module": module.__name__, "origin": str(module.__file__), "capture_called": False}


def check_config(directory: Path) -> dict:
    """构造通用计划并UTF-8写回读取，不读取真实用户计划。"""
    from opinion_capture.config import Plan, load_plan, save_plan
    plan = Plan.from_dict(dict(load_defaults(), output_root=str(directory / "exports")))
    target = directory / "plan.json"
    save_plan(plan, target)
    require(load_plan(target).to_dict() == plan.to_dict(), "配置往返不一致")
    return {"path": str(target), "top_n": plan.top_n}


def check_sqlite(directory: Path) -> dict:
    """真实SQLite建表、中文写入、关闭重开；只使用全新探针库。"""
    from opinion_capture.storage import Store
    from opinion_capture.config import load_plan
    path = directory / "portable-check.sqlite3"
    store = Store(path)
    try:
        plan = load_plan(directory / "plan.json")
        batch, fresh = store.create_batch("portable-check", datetime.now(timezone.utc), plan.to_dict())
        require(fresh, "自检应创建独立批次")
        duplicate, fresh = store.create_batch("portable-check", datetime.now(timezone.utc), plan.to_dict())
        require(duplicate == batch and not fresh, "SQLite防重失败")
        require(store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite完整性失败")
    finally:
        store.db.close()
    store = Store(path)
    try:
        require(json.loads(store.batch(batch)["config_json"])["keywords"] == ["示例关键词"], "SQLite重开中文数据不一致")
        version = store.db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        store.db.close()
    return {"path": str(path), "schema_version": version, "reopen": True}


def check_image(directory: Path) -> dict:
    """PNG写入、解码和像素验证；不处理或打包docs原图。"""
    from PIL import Image
    path = directory / "pillow-probe.png"
    with Image.new("RGB", (32, 24), (31, 98, 176)) as im:
        im.save(path)
    with Image.open(path) as im:
        im.load()
        require(im.size == (32, 24) and im.getpixel((3, 3)) == (31, 98, 176), "PNG解码不一致")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def check_resources() -> dict:
    """验证时区、Playwright驱动文件和uiautomator2资源存在，不启动它们。"""
    from zoneinfo import ZoneInfo
    from playwright._impl._driver import compute_driver_executable
    result = {"portable": str(resource_root())}
    require((resource_root() / "portable-defaults.json").is_file(), "便携资源缺失")
    require(resources.files("tzdata").joinpath("zoneinfo/Asia/Shanghai").is_file(), "tzdata资源缺失")
    require(datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")).utcoffset().total_seconds() == 28800, "时区解析失败")
    node, cli = map(Path, compute_driver_executable())
    require(node.is_file() and cli.is_file(), "Playwright驱动node/cli.js未收集")
    result["playwright_driver"] = [str(node), str(cli)]
    for asset in ("u2.jar", "app-uiautomator.apk"):
        path = resources.files("uiautomator2").joinpath("assets", asset)
        require(path.is_file(), f"uiautomator2资源缺失：{asset}")
    return result


def check_paths() -> dict:
    """验证源码/冻结路径契约；改变工作目录也不改变默认计划或输出位置。"""
    from opinion_capture.paths import application_root, default_config_path, default_output_root
    from opinion_capture import gui
    expected = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    require(application_root() == expected, "application_root错误")
    require(default_config_path() == expected / ".local" / "plan.json", "默认配置位置错误")
    require(default_output_root() == expected / ".local" / "exports", "默认输出位置错误")
    require(Path(gui.default_plan_data()["output_root"]) == default_output_root(), "GUI未接入paths默认输出位置")
    previous = Path.cwd()
    try:
        os.chdir(resource_root())
        require(default_config_path() == expected / ".local" / "plan.json", "路径仍依赖CWD")
    finally:
        os.chdir(previous)
    return {"application_root": str(expected), "resource_root": str(resource_root()),
            "config": str(default_config_path()), "output": str(default_output_root())}


def check_qt(directory: Path) -> dict:
    """真正实例化主窗体并离屏渲染；所有业务回调替换为报错哨兵。"""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import __version__
    from PySide6.QtCore import QLibraryInfo
    from PySide6.QtWidgets import QApplication
    from opinion_capture.gui import MainWindow
    def forbidden(*args, **kwargs):
        raise RuntimeError("自检窗口意外发起业务操作")
    app = QApplication.instance() or QApplication(["portable-check", "-platform", "offscreen"])
    window = MainWindow(runner=forbidden, preflight=forbidden, service_factory=forbidden,
                        device_provider=forbidden, history_provider=forbidden, autoload=False)
    try:
        window.resize(1100, 780)
        window.show()
        app.processEvents()
        path = directory / "qt-window.png"
        require(window.grab().save(str(path), "PNG"), "Qt离屏PNG保存失败")
        require(window._worker is None and window._scheduler is None and not window._reads,
                "主窗口启动产生后台业务任务")
        plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
        if os.name == "nt":
            require((plugins / "platforms" / "qwindows.dll").is_file(), "Windows Qt平台插件缺失")
        return {"version": __version__, "platform": app.platformName(), "plugins": str(plugins),
                "image": str(path), "automatic_collection": False}
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def local_command(*args: str) -> tuple[list[str], dict]:
    """子进程只使用本项目源码launcher或同目录冻结CLI，不依赖venv redirector。"""
    if getattr(sys, "frozen", False):
        cli = Path(sys.executable).resolve().with_name(CLI_EXE)
        require(cli.is_file(), "冻结CLI companion缺失")
        env = dict(os.environ)
        # 避免开发机环境掩盖资源遗漏；不调用浏览器安装或驱动启动。
        for key in ("PYTHONPATH", "PYTHONHOME", "PLAYWRIGHT_NODEJS_PATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            env.pop(key, None)
        return [str(cli), *args], env
    return source_python_command(Path(__file__).with_name("portable_launcher.py"), *args)


def multiprocessing_probe(connection) -> None:
    """可被spawn导入的顶层探针；只经Pipe回传PID及动态模块导入结果。"""
    try:
        with offline_guard():
            detail = adapter_probe("web_bilibili")
        connection.send({"ok": True, "pid": os.getpid(), "detail": detail})
    finally:
        connection.close()


def check_multiprocessing() -> list[dict]:
    """冻结时分别经GUI/CLI bootloader spawn，验证freeze_support而非只看导入。"""
    import multiprocessing.spawn
    from portable_runtime import GUI_EXE
    context = multiprocessing.get_context("spawn")
    original = multiprocessing.spawn.get_executable()
    if getattr(sys, "frozen", False):
        executables = [Path(sys.executable).with_name(name) for name in (CLI_EXE, GUI_EXE)]
    else:
        executables = [Path(getattr(sys, "_base_executable", sys.executable))]
    results = []
    try:
        for executable in executables:
            require(executable.is_file(), "multiprocessing解释器/冻结入口缺失")
            multiprocessing.set_executable(str(executable))
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=multiprocessing_probe, args=(sender,))
            try:
                process.start()
                sender.close()
                require(receiver.poll(30), "multiprocessing未返回；检查freeze_support入口")
                data = receiver.recv()
                process.join(timeout=10)
                require(not process.is_alive() and process.exitcode == 0, "multiprocessing子进程退出异常")
                require(data["ok"] and data["pid"] == process.pid, "multiprocessing探针PID错误")
                results.append({"executable": str(executable), "pid": process.pid, "ok": True})
            finally:
                sender.close()
                receiver.close()
                if process.pid is not None:
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=10)
                    process.close()
    finally:
        multiprocessing.set_executable(original)
    return results


def check_children(directory: Path) -> dict:
    """验证冻结分派、三适配器导入及真实子进程终止；仅延迟文件写入替代设备动作。"""
    records = []
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for name in ("android", "bilibili", "xiaohongshu"):
        command, env = local_command("--worker-check", name)
        completed = subprocess.run(command, env=env, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=30, **options)
        require(completed.returncode == 0, f"{name}子进程失败：{completed.stderr[-1500:]}")
        data = json.loads(completed.stdout)
        require(data.get("ok") and data.get("platform_access") is False, "子进程报告错误")
        records.append(data)
    ready, late = directory / "child.ready", directory / "unexpected-action"
    command, env = local_command("--probe-child", str(ready), "--late-marker", str(late))
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    try:
        deadline = time.monotonic() + 15
        ready_pid = None
        while process.poll() is None and time.monotonic() < deadline:
            # exists早于write_text完成：等待完整PID，避免把空文件视作就绪。
            if ready.is_file():
                value = ready.read_text(encoding="ascii").strip()
                if value.isdigit():
                    ready_pid = int(value)
                    break
            time.sleep(.02)
        require(ready_pid is not None, "子进程未实际就绪，终止验证无效")
        require(ready_pid == process.pid, "实际工作PID与被终止PID不一致（redirector）")
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    time.sleep(1.15)
    require(not late.exists(), "结束后子进程仍执行延迟动作")
    return {"adapters": records, "multiprocessing": check_multiprocessing(),
            "kill_probe_pid": process.pid, "late_action": False,
            "scope": "local process only; not live browser/ADB process-tree cancellation"}


def run_checks(base: Path, *, children: bool = True) -> dict:
    """每次使用新目录并保留证据；单项失败仍输出其余检查结果。"""
    directory = base.resolve() / ("check-" + uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "frozen": bool(getattr(sys, "frozen", False)),
              "executable": sys.executable, "platform_access": False, "directory": str(directory),
              "checks": [], "external_dependencies": {}, "ok": False,
              "not_verified": ["真实浏览器/模拟器访问", "真实采集与完整取消链", "其他Windows系统兼容性", "签名和SmartScreen"]}
    for name, callback in (("config", lambda: check_config(directory)), ("sqlite", lambda: check_sqlite(directory)),
                           ("image", lambda: check_image(directory)), ("resources", check_resources),
                           ("paths", check_paths), ("adapters", lambda: [adapter_probe(n) for n in ADAPTERS]),
                           ("qt", lambda: check_qt(directory))):
        try:
            with offline_guard():
                detail = callback()
            report["checks"].append({"name": name, "ok": True, "detail": detail})
        except Exception as exc:
            report["checks"].append({"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    if children:
        try:
            detail = check_children(directory)
            report["checks"].append({"name": "children", "ok": True, "detail": detail})
        except Exception as exc:
            report["checks"].append({"name": "children", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    else:
        report["not_verified"].append("child-process checks skipped")
    try:
        from opinion_capture.paths import application_root
        report["external_dependencies"] = discover_dependencies(application_root() / "portable-dependencies.json")
    except Exception as exc:
        report["checks"].append({"name": "dependency_config", "ok": False, "error": str(exc)})
    report["ok"] = all(item["ok"] for item in report["checks"])
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None) -> int:
    """返回0代表所选离线检查通过，2代表检查失败；不代表平台功能已验收。"""
    from opinion_capture.paths import application_root
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-dir", type=Path, default=application_root() / ".local" / "packaging")
    parser.add_argument("--skip-children", action="store_true", help="只用于局部诊断，正式验收不使用")
    options = parser.parse_args(argv)
    report = run_checks(options.check_dir, children=not options.skip_children)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2
