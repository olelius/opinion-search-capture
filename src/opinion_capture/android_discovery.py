"""定位运行中的MuMu工具与实例；不扫描磁盘、不修改系统PATH、不启动模拟器。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

_cache_lock = threading.Lock()
_cache_at = 0.0
_cache: list[dict] = []


def _selection_file() -> Path:
    """用户选择属于本机私有数据，不随便携包分发。"""
    from .paths import application_root
    return application_root() / ".local" / "android-tools.json"


def saved_adb() -> str | None:
    """复用本机选择；搬家后旧路径失效则继续自动查找。"""
    try:
        value = json.loads(_selection_file().read_text(encoding="utf-8-sig")).get("adb")
        return value if isinstance(value, str) and Path(value).is_file() else None
    except (OSError, ValueError, AttributeError):
        return None


def remember_adb(filename: str) -> str:
    """保存用户明确选中的adb.exe；不修改系统环境变量。"""
    path = Path(filename).resolve()
    if path.name.casefold() != "adb.exe" or not path.is_file():
        raise ValueError("请选择模拟器目录内真实存在的adb.exe文件")
    target = _selection_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"adb":str(path)}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return str(path)


def _registry_paths(deadline: float) -> list[str]:
    """进程路径不可见时，仅用MuMu安装项定位；注册表只读，不执行卸载命令。"""
    if os.name != "nt":
        return []
    import winreg
    paths = []
    key_name = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            if time.monotonic() >= deadline:
                return paths
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as parent:
                    index = 0
                    while time.monotonic() < deadline:
                        try:
                            name = winreg.EnumKey(parent, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(parent, name) as key:
                                display = str(winreg.QueryValueEx(key, "DisplayName")[0])
                                if "mumu" not in display.casefold():
                                    continue
                                for field in ("InstallLocation", "DisplayIcon"):
                                    try:
                                        value = os.path.expandvars(str(winreg.QueryValueEx(key, field)[0])).strip()
                                    except OSError:
                                        continue
                                    if not value:
                                        continue
                                    if field == "InstallLocation":
                                        paths.append(str(Path(value.strip('"')) / "adb.exe"))
                                    else:
                                        icon = value.split('"')[1] if value.startswith('"') else value.rsplit(",", 1)[0]
                                        if Path(icon).is_absolute():
                                            paths.append(icon)
                        except OSError:
                            continue
            except OSError:
                continue
    return list(dict.fromkeys(paths))


def _process_paths(deadline: float) -> list[str]:
    """只读取MuMu相关进程的可执行路径，缺少读取权限时保留空结果。"""
    if os.name != "nt" or time.monotonic() >= deadline:
        return []
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
              "Get-CimInstance Win32_Process -Filter \"Name LIKE 'MuMu%' OR Name = 'NemuPlayer.exe'\" "
              "| Select-Object -ExpandProperty ExecutablePath | ConvertTo-Json -Compress")
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, timeout=min(3.0, max(.01, deadline-time.monotonic())),
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or not result.stdout.strip():
            return []
        data = json.loads(result.stdout.decode("utf-8-sig"))
        return [p for p in (data if isinstance(data, list) else [data]) if isinstance(p, str) and p]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def _tools_near(executable: str | Path) -> list[dict]:
    """仅检查已知进程附近三层的常见工具目录，不猜盘符。"""
    parent = Path(executable).resolve().parent
    found, seen = [], set()
    for root in (parent, *list(parent.parents)[:2]):
        if root == root.parent:
            continue
        for directory in (root, root / "nx_main", root / "shell"):
            key = str(directory).casefold()
            if key in seen:
                continue
            seen.add(key)
            adb, manager = directory / "adb.exe", directory / "MuMuManager.exe"
            if adb.is_file() or manager.is_file():
                found.append({"adb": str(adb) if adb.is_file() else None,
                              "manager": str(manager) if manager.is_file() else None})
    return found


def running_mumu_tools(deadline: float) -> list[dict]:
    """进程优先、安装信息兜底；成功和空结果均最多缓存5秒。"""
    global _cache_at, _cache
    with _cache_lock:
        if time.monotonic() - _cache_at < 5:
            return [dict(row) for row in _cache]
        rows, seen = [], set()
        for executable in _process_paths(deadline):
            for row in _tools_near(executable):
                key = (row["adb"], row["manager"])
                if key not in seen:
                    rows.append(row)
                    seen.add(key)
        if not any(row["adb"] for row in rows):
            for executable in _registry_paths(deadline):
                for row in _tools_near(executable):
                    key = (row["adb"], row["manager"])
                    if key not in seen:
                        rows.append(row)
                        seen.add(key)
        _cache_at, _cache = time.monotonic(), rows
        return [dict(row) for row in rows]


def find_adb(deadline: float, *, environ=None) -> str | None:
    """显式配置/PATH优先，再从运行中的MuMu定位；不依赖开发机目录。"""
    env = os.environ if environ is None else environ
    explicit = env.get("OPINION_CAPTURE_ADB")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    selected = saved_adb()
    if selected:
        return selected
    home = env.get("OPINION_CAPTURE_MUMU_HOME")
    if home:
        for suffix in ("nx_main/adb.exe", "shell/adb.exe", "adb.exe"):
            candidate = Path(home) / suffix
            if candidate.is_file():
                return str(candidate.resolve())
    on_path = shutil.which("adb", path=env.get("PATH", ""))
    if on_path:
        return on_path
    return next((row["adb"] for row in running_mumu_tools(deadline)
                 if row["adb"] and Path(row["adb"]).is_file()), None)


def running_instances(adb: str, deadline: float, run) -> list[dict]:
    """读取官方管理器info中的运行实例，仅接受真实返回的本机ADB端口。"""
    tools = _tools_near(adb)
    if not any(row["manager"] for row in tools):
        tools += running_mumu_tools(deadline)
    instances, managers, serials = [], set(), set()
    for tool in tools:
        manager = tool["manager"]
        if not manager or manager.casefold() in managers:
            continue
        managers.add(manager.casefold())
        result = run([manager, "info", "--vmindex", "all"])
        try:
            data = json.loads(result.stdout.decode("utf-8-sig")) if result.returncode == 0 else {}
        except (UnicodeError, ValueError):
            continue
        rows = data.values() if isinstance(data, dict) else data if isinstance(data, list) else []
        for row in rows:
            if not isinstance(row, dict) or row.get("is_android_started") is not True or row.get("error_code", 0) != 0:
                continue
            port = row.get("adb_port")
            if isinstance(port, str) and port.isdecimal():
                port = int(port)
            if type(port) is not int or not 1 <= port <= 65535:
                continue
            serial = f"127.0.0.1:{port}"
            if serial not in serials:
                instances.append({"serial": serial, "name": row.get("name") or "MuMu设备"})
                serials.add(serial)
    return instances
