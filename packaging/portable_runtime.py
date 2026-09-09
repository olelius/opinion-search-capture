"""便携分发约定：只读资源、外部依赖发现和受限 worker 路由。

此模块由 launcher 与校验器共享；不导入采集适配器、不连接设备或网络。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

APP_NAME = "OpinionCapture-Portable"
GUI_EXE = "OpinionCapture.exe"
CLI_EXE = "OpinionCapture-CLI.exe"
ADAPTERS = ("android", "web_bilibili", "web_xiaohongshu")
WEB_MODULES = {"bilibili": "opinion_capture.web_bilibili",
               "xiaohongshu": "opinion_capture.web_xiaohongshu"}
DEPENDENCY_ENV = {"browser": "OPINION_CAPTURE_BROWSER", "adb": "OPINION_CAPTURE_ADB",
                  "mumu_home": "OPINION_CAPTURE_MUMU_HOME"}


def resource_root() -> Path:
    """返回只读资源目录；与EXE旁可写的.local目录严格分离。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS).resolve() / "portable_resources"
    return Path(__file__).resolve().parent / "resources"


def load_defaults() -> dict:
    """只读取随包提供的通用示例，绝不读取开发者.local配置。"""
    data = json.loads((resource_root() / "portable-defaults.json").read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("plan"), dict):
        raise ValueError("便携示例资源格式错误")
    return data["plan"]


def worker_route(argv: list[str]) -> tuple[str, str] | None:
    """识别固定内部入口；普通CLI返回None，畸形worker参数明确报错。

Android约定 --ui-worker JSON；WEB支持规范参数及既有模块调用的白名单兼容。
这里不执行传入Python代码，也不接受任意模块名称。
"""
    if argv[:1] == ["--existing-browser-worker"]:
        if len(argv) != 1:
            raise ValueError("--existing-browser-worker 不接受额外参数")
        return "existing-browser", ""
    if argv[:1] == ["--ui-worker"]:
        if len(argv) != 2 or not isinstance(json.loads(argv[1]), dict):
            raise ValueError("--ui-worker 需要一个JSON对象参数")
        return "android", argv[1]
    if argv[:1] == ["--web-worker"]:
        if len(argv) != 3 or argv[1] not in WEB_MODULES:
            raise ValueError("--web-worker 需要 bilibili|xiaohongshu JOB_JSON")
        return argv[1], argv[2]
    if argv[:1] in (["-B"], ["-m"], ["-c"]):
        legacy = argv[1:] if argv[0] == "-B" else argv
        if (len(legacy) == 4 and legacy[0] == "-m" and legacy[2] == "--worker"
                and legacy[1] in WEB_MODULES.values()):
            key = next(k for k, value in WEB_MODULES.items() if value == legacy[1])
            return key, legacy[3]
        raise ValueError("冻结入口仅支持显式worker协议，不是通用Python解释器")
    return None


def discover_dependencies(config_path: Path | None = None, *, environ=None) -> dict:
    """仅检查文件存在性，优先环境变量、JSON配置，再查PATH/常见安装位置。

显式配置失效时保留invalid，不偷偷降级。相对JSON路径相对于JSON文件目录。
发现不等于可启动、版本兼容、设备在线或已有平台登录。
"""
    env = dict(os.environ if environ is None else environ)
    config = {}
    if config_path is not None and config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict) or set(config) - set(DEPENDENCY_ENV):
            raise ValueError("依赖配置只接受 browser、adb、mumu_home")
    result = {}
    for name, variable in DEPENDENCY_ENV.items():
        explicit = env.get(variable) or config.get(name)
        if explicit is not None and not isinstance(explicit, str):
            raise ValueError(f"{name}依赖路径应为字符串")
        candidates: list[tuple[Path, str]] = []
        if explicit:
            path = Path(explicit).expanduser()
            if not path.is_absolute():
                if env.get(variable):
                    raise ValueError(f"{variable}应使用绝对路径")
                path = config_path.parent / path
            candidates.append((path, "environment" if env.get(variable) else "config"))
        else:
            for program in ({"browser": ("chrome.exe", "msedge.exe"), "adb": ("adb.exe",),
                             "mumu_home": ()}[name]):
                found = shutil.which(program, path=env.get("PATH", ""))
                if found:
                    candidates.append((Path(found), "PATH"))
            if name == "browser":
                for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                    if env.get(key):
                        for suffix in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
                            candidates.append((Path(env[key]) / suffix, "known-location"))
            if name == "adb":
                home = env.get(DEPENDENCY_ENV["mumu_home"]) or config.get("mumu_home")
                if home:
                    root = Path(home)
                    if not root.is_absolute() and config_path:
                        root = config_path.parent / root
                    for suffix in ("nx_main/adb.exe", "shell/adb.exe", "adb.exe"):
                        candidates.append((root / suffix, "mumu-config"))
        found = next(((p.resolve(), source) for p, source in candidates
                      if (p.is_dir() if name == "mumu_home" else p.is_file())), None)
        result[name] = {"status": "found" if found else ("invalid" if explicit else "missing"),
                        "path": str(found[0]) if found else (str(candidates[0][0]) if explicit else None),
                        "source": found[1] if found else (candidates[0][1] if explicit else None),
                        "runtime_verified": False}
    return result


def apply_dependencies(report: dict) -> None:
    """为本进程及其子进程设置发现结果；不更改系统环境或用户profile。"""
    for key, item in report.items():
        if item["status"] == "invalid":
            raise ValueError(f"显式依赖路径不存在：{DEPENDENCY_ENV[key]}")
        if item["status"] == "found":
            os.environ[DEPENDENCY_ENV[key]] = item["path"]
    adb = report["adb"]
    if adb["status"] == "found":
        os.environ["ADBUTILS_ADB_PATH"] = adb["path"]
        os.environ["PATH"] = str(Path(adb["path"]).parent) + os.pathsep + os.environ.get("PATH", "")


def source_python_command(script: Path, *args: str) -> tuple[list[str], dict]:
    """Windows使用base解释器绕过venv redirector，并保留当前依赖搜索路径。

仅用于源码离线子进程验证；冻结模式必须直接启动CLI EXE，不传-c/-m。
"""
    if getattr(sys, "frozen", False):
        raise RuntimeError("冻结进程应使用同目录CLI EXE")
    executable = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
    paths = [p for p in sys.path if p and Path(p).is_dir()]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(paths), PYTHONDONTWRITEBYTECODE="1",
               PYTHONIOENCODING="utf-8")
    return [executable, "-B", str(script.resolve()), *args], env
