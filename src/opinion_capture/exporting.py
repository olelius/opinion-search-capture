"""白名单字段路径生成与不覆盖导出；不从目录子串反推图片归属。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import string
import tempfile
from datetime import datetime
from pathlib import Path

from .scheduling import TIMEZONE

INTEGER_FIELDS = {"year", "month", "day", "top_n", "batch", "part"}
FIELDS = INTEGER_FIELDS | {"project", "platform", "client", "display_type", "keyword", "keyword_id", "content_label", "capture_stamp", "artifact_id"}
INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}
_WINDOWS = os.name == "nt"


def _windows_length(value: str) -> int:
    """Windows 路径上限按 UTF-16 单元计数，补充字符不按单个 Python 字符计。"""
    return len(value.encode("utf-16-le")) // 2


def path_identity(path: str | Path) -> str:
    """用保守的 Windows 大小写规则比较路径，不用于判断业务分组。"""
    return str(Path(path).absolute()).replace("\\", "/").casefold()


def exact_group_key(*, platform: str, client: str, capture_kind: str,
                    captured_at: datetime, keyword: str | None = None) -> tuple:
    """按实际本地日期和精确身份分组；省略 keyword 时表示全关键词汇总组。"""
    if captured_at.tzinfo is None:
        raise ValueError("实际截图时间缺少时区")
    local = captured_at.astimezone(TIMEZONE)
    group = (local.year, local.month, local.day, platform, client, capture_kind)
    return group if keyword is None else (*group, keyword_id(keyword))


def keyword_id(keyword: str) -> str:
    """对原始UTF-8关键词精确计算身份；A1、A10和a1互不相同。"""
    return hashlib.sha256(keyword.encode("utf-8")).hexdigest()


def safe_segment(value: str) -> str:
    """清理单个显示字段，不将清理后的文本用于实际搜索。"""
    clean = INVALID.sub("_", str(value)).strip().rstrip(". ")
    if not clean or clean in (".", ".."):
        clean = "_"
    if clean.split(".")[0].upper() in RESERVED:
        clean = "_" + clean
    return clean[:80].rstrip(". ") or "_"


def validate_template(template: str) -> None:
    """只允许命名字段和整数02d格式，不执行属性访问或用户表达式。"""
    if not isinstance(template, str):
        raise ValueError("模板应为文本")
    for _, name, spec, conversion in string.Formatter().parse(template):
        if name is None:
            continue
        if name not in FIELDS or conversion or (spec and not (name in INTEGER_FIELDS and spec == "02d")):
            raise ValueError(f"模板字段或格式无效：{name}:{spec}")


def artifact_values(*, project: str, platform: str, client: str, capture_kind: str,
                    keyword: str, captured_at: datetime, top_n: int, batch: int,
                    artifact_id: str, part: int = 1, partial: bool = False) -> dict:
    """从实际采集时间和任务元数据生成字段；批次计划时间不冒充截图时间。"""
    from .config import PLATFORMS
    if captured_at.tzinfo is None:
        raise ValueError("实际截图时间缺少时区")
    local = captured_at.astimezone(TIMEZONE)
    identity = keyword_id(keyword)
    label = f"TOP{top_n}" if capture_kind == "results" else "下拉词"
    if partial:
        label += "-未完整验证"
    return {
        "project": safe_segment(project), "platform": PLATFORMS[platform], "client": client,
        "display_type": "下拉词" if capture_kind == "suggestions" else ("移动端" if client == "APP" else "WEB端"),
        "keyword": f"{safe_segment(keyword)}--{identity[:12]}", "keyword_id": identity,
        "year": local.year, "month": local.month, "day": local.day, "top_n": top_n,
        "batch": batch, "part": part, "content_label": label,
        "capture_stamp": local.strftime("%Y%m%d-%H%M%S-%f"), "artifact_id": safe_segment(artifact_id),
    }


def export_path(root: str | Path, directory: str, filename: str, values: dict) -> Path:
    """生成根目录内的精确目标路径，校验穿越、非法文件名及长度。"""
    base = Path(root)
    if not base.is_absolute():
        raise ValueError("导出根目录应为绝对路径")
    validate_template(directory)
    validate_template(filename)
    folder, leaf = directory.format_map(values), filename.format_map(values)
    if folder.startswith(("/", "\\")):
        raise ValueError("目录模板应为根目录下的相对结构")
    parts = folder.replace("\\", "/").split("/") if folder else []
    for segment in [*parts, leaf]:
        if not segment or segment in (".", "..") or INVALID.search(segment) or segment.endswith((".", " ")):
            raise ValueError(f"路径片段无效：{segment}")
        if segment.split(".")[0].upper() in RESERVED or _windows_length(segment) > 240:
            raise ValueError(f"文件名保留字或片段过长：{segment}")
    raw_destination = base.joinpath(*parts, leaf)
    if raw_destination.is_symlink():
        raise ValueError("输出文件位置已为符号链接")
    destination = raw_destination.resolve()
    if not destination.is_relative_to(base.resolve()):
        raise ValueError("输出路径越出所选根目录")
    if _windows_length(str(destination)) > 245:
        raise ValueError("路径过长，请缩短根目录或模板")
    return destination


def publish_immutable(staged: str | Path, destination: str | Path) -> str:
    """将同目录、已写完并验证的暂存文件排他发布，返回发布机制。

    Windows 的 os.rename 使用不替换目标的同卷重命名，不要求 NTFS 硬链接；
    POSIX 使用排他硬链接。若文件系统不支持该原语，报错并保留暂存文件供
    调用方清理，绝不降级为先建最终文件再逐块写入，也不使用 os.replace。
    调用方负责 fsync、内容验证与 finally 清理暂存文件。
    """
    tmp = Path(staged).resolve(strict=True)
    raw_dst = Path(destination).absolute()
    if raw_dst.is_symlink() or raw_dst.exists():
        raise FileExistsError(f"目标文件已存在：{raw_dst}")
    dst = raw_dst.resolve()
    if not tmp.is_file() or tmp.parent != dst.parent or tmp == dst:
        raise ValueError("原子发布需要目标同目录内的独立暂存文件")
    if _WINDOWS:
        # Windows 即使在先前 exists 检查之后发生同名抢占，rename 也会失败。
        os.rename(tmp, dst)
        return "windows_rename_noreplace"
    try:
        os.link(tmp, dst)
    except FileExistsError:
        raise
    except OSError as exc:
        raise OSError("文件系统未完成排他硬链接发布；最终路径未降级为部分写入") from exc
    return "posix_hardlink_noreplace"


def copy_immutable(source: str | Path, destination: str | Path) -> dict:
    """完整复制、同步后排他发布；同名抢占或复制中断均不覆盖历史文件。"""
    raw_dst = Path(destination).absolute()
    if raw_dst.is_symlink():
        raise FileExistsError(f"目标文件已为符号链接：{raw_dst}")
    src, dst = Path(source).resolve(), raw_dst.resolve()
    if not src.is_file():
        raise ValueError("原始图片不存在")
    if src == dst or dst.exists():
        raise FileExistsError(f"目标文件已存在：{dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".capture-", suffix=".tmp", dir=dst.parent)
    tmp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as output, src.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        digest = hashlib.sha256()
        with tmp.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        size = tmp.stat().st_size
        publication = publish_immutable(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": str(dst), "sha256": digest.hexdigest(), "bytes": size,
            "publication": publication}
