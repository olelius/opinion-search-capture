# -*- mode: python ; coding: utf-8 -*-
"""onedir双入口：GUI不弹控制台，CLI/Android worker保留标准输入输出。"""

import os
from pathlib import Path
import sys

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(root / "packaging"))
from build_support import forbidden_member
from portable_runtime import APP_NAME

# 不复制repo目录、docs原图、用户配置、profiles、浏览器或ADB安装包。
datas = [(str(p), "portable_resources") for p in (root / "packaging/resources").glob("*.json")]
for package in ("playwright", "uiautomator2", "tzdata"):
    datas += collect_data_files(package, excludes=["**/.local-browsers/**", "**/__pycache__/**"])
for package in ("Pillow", "playwright", "uiautomator2", "adbutils", "APScheduler", "PySide6", "tzdata"):
    datas += copy_metadata(package)
hidden = collect_submodules("opinion_capture")
hidden += ["opinion_capture.android", "opinion_capture.android_results",
           "opinion_capture.web_bilibili", "opinion_capture.web_results_bilibili",
           "opinion_capture.web_xiaohongshu", "opinion_capture.web_results_xiaohongshu",
           "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PIL.Image",
           "PIL.PngImagePlugin", "sqlite3", "zoneinfo", "tzdata", "uiautomator2", "adbutils",
           "playwright.sync_api", "multiprocessing.spawn", "multiprocessing.popen_spawn_win32"]
a = Analysis([str(root / "packaging/portable_launcher.py")],
             pathex=[str(root / "src"), str(root / "packaging")],
             binaries=[], datas=datas, hiddenimports=hidden,
             hookspath=[], runtime_hooks=[], excludes=["PyQt5", "PyQt6", "PySide2"],
             noarchive=False)

# 第三方hooks也须服从发布白名单边界。ADB由用户显式配置，不随adbutils捎带。
def accepted(entry):
    """只放行当前构建工作目录的已知生成文件，其他用户资料保持拒绝。"""
    destination, source, kind = entry
    parts = destination.replace("\\", "/").lower().split("/")
    if "adbutils" in parts and "binaries" in parts:
        return False
    if forbidden_member(destination):
        raise ValueError("拒绝收集发布范围外文件：" + destination)
    resolved = Path(source).resolve()
    work_root = Path(CONF["workpath"]).resolve()
    # Analysis生成的标准库归档不是用户资料。严格匹配目标名、类型和当前CONF路径；
    # 不放行整个workpath，更不放行整个.local；符号链接逃逸也不满足等式。
    if (destination == "base_library.zip" and kind == "DATA"
            and work_root.is_relative_to((root / ".local" / "packaging").resolve())
            and resolved == work_root / "base_library.zip" and resolved.is_file()):
        return True
    for private in (root / ".local", root / "docs"):
        if resolved.is_relative_to(private.resolve()):
            raise ValueError("拒绝从用户数据/原始截图收集载荷：" + destination)
    if {part.lower() for part in resolved.parts} & {"profiles", "browser-profiles", "sessions", "session-log", "user data"}:
        raise ValueError("拒绝从浏览器会话来源收集载荷：" + destination)
    return True

a.datas = [entry for entry in a.datas if accepted(entry)]
a.binaries = [entry for entry in a.binaries if accepted(entry)]
pyz = PYZ(a.pure)
common = dict(exclude_binaries=True, debug=False, bootloader_ignore_signals=False,
              strip=False, upx=False, contents_directory="_internal")
gui = EXE(pyz, a.scripts, [], name="OpinionCapture", console=False, **common)
cli = EXE(pyz, a.scripts, [], name="OpinionCapture-CLI", console=True, **common)
collection = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
