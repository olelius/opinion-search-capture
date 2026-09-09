"""源码与便携EXE共用的默认数据位置；不受启动时工作目录影响。"""

import sys
from pathlib import Path


def application_root() -> Path:
    """便携模式使用EXE所在目录，开发模式使用仓库根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return application_root() / ".local" / "plan.json"


def default_output_root() -> Path:
    return application_root() / ".local" / "exports"
