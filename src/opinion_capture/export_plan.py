"""批次导出预检：只读快照预览，不创建批次、不虚构实际截图时间。"""

from __future__ import annotations

import hashlib
import os
import string
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from .exporting import FIELDS, artifact_values, export_path, keyword_id, path_identity, validate_template
from .scheduling import TIMEZONE


def _get(plan, name: str, default=None):
    """兼容 Plan、SimpleNamespace 与普通字典，不要求具体配置类。"""
    return plan.get(name, default) if isinstance(plan, Mapping) else getattr(plan, name, default)


def _issue(code: str, message: str, **context) -> dict:
    """给 GUI 保留可显示文案及可定位字段。"""
    return {"code": code, "message": message, **context}


def _template_fields(template: str) -> set[str]:
    """在白名单校验之后提取字段，不支持属性访问或格式表达式。"""
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


def _directory_check(directory: Path, probe: bool) -> dict:
    """检查到最近存在父目录；可选只在该目录写入、清理一个排他探针。

    不创建计划目录；未生成子目录的继承 ACL、未来磁盘容量及竞争仍属执行边界。
    os.access 只是静态迹象，probe=False 不将其成功视为实际写入已验证。
    """
    ancestor = directory
    while not ancestor.exists():
        if ancestor.is_symlink():
            raise OSError(f"父路径含失效符号链接：{ancestor}")
        if ancestor == ancestor.parent:
            raise OSError("导出根路径缺少存在的父目录")
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise NotADirectoryError(f"路径被普通文件占用：{ancestor}")
    result = {"directory": str(directory), "checked_ancestor": str(ancestor),
              "probe_writable": probe, "writable": None}
    if not os.access(ancestor, os.W_OK | os.X_OK):
        raise PermissionError(f"父目录当前缺少写入/访问权限：{ancestor}")
    if probe:
        fd, name = tempfile.mkstemp(prefix=".export-probe-", suffix=".tmp", dir=ancestor)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"export-preflight\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            Path(name).unlink(missing_ok=True)
        result["writable"] = True
    return result


def preflight_plan(plan, *, planned_at: datetime | None = None,
                   probe_writable: bool = False, batch_number: int = 1,
                   verified_existing_paths=()) -> dict:
    """预览一批代表性原图和各组单张汇总图，不写配置/图片/数据库。

    返回 ok/errors/warnings/raw_paths/composite_paths，以及 preview、originals、
    composites 别名；errors/warnings 元素为 code/message/context 字典。
    同批所有任务使用同一个示例 capture_stamp，避免以未知截图时刻证明唯一。
    artifact_id 是确定性预览代号；界面默认batch=1，执行器可提供实际编号。
    verified_existing_paths仅供已核对哈希的恢复路径；其他已有文件仍拒绝覆盖。
    汇总图 part 固定为 1；原图帧数、汇总尺寸、partial 状态及跨午夜实际目录在执行时重新校验。
    probe_writable=False 为只读检查；True 仅增加临时可写探针，无目录树生成。
    """
    from .config import DEFAULT_DIRECTORY, DEFAULT_FILENAME, PLATFORMS

    errors, warnings, raw_paths, composite_paths = [], [], [], []
    verified_paths = {path_identity(p) for p in verified_existing_paths}
    if type(batch_number) is not int or batch_number < 1:
        raise ValueError("批次编号应为正整数")
    report = {"ok": False, "errors": errors, "warnings": warnings,
              "raw_paths": raw_paths, "composite_paths": composite_paths,
              "originals": raw_paths, "composites": composite_paths, "preview": [],
              "counts": {"tasks": 0, "groups": 0}, "writable_checks": [],
              "composition": {"single_image": True, "composite_max_pixels": None,
                              "composite_max_height": None},
              "planned_at": None, "sample_batch": batch_number, "fields": sorted(FIELDS),
              "boundaries": [
                  "planned_at 仅为路径示例日期，不是 captured_at；全部任务共享示例时间戳。",
                  "预览 artifact_id 未预占，实际执行须分配唯一 ID 并对每张图片再次排他发布。",
                  "每组汇总为单张 PNG，网格按列数换行，横排保持单行；part 固定为 1，旧像素/高度上限不参与计算。",
                  "预检不证明未来截图时间戳唯一，不预知原图帧数、汇总尺寸、状态或跨午夜/跨月日期。",
                  "目录可写性、占用和磁盘容量可在预检后变化；probe=False 没有执行写入。",
                  "身份分组依据完整关键词及年/月/日/平台/端/种类元数据，不依据路径子串。",
              ]}
    now = planned_at if planned_at is not None else datetime.now(TIMEZONE)
    if type(probe_writable) is not bool:
        errors.append(_issue("INVALID_FIELD", "probe_writable 应为布尔值"))
        return report
    if not isinstance(now, datetime) or now.tzinfo is None:
        errors.append(_issue("INVALID_TIME", "planned_at 应为含时区的 datetime"))
        return report
    now = now.astimezone(TIMEZONE)
    report["planned_at"] = now.isoformat()
    root_value = _get(plan, "output_root")
    try:
        root = Path(root_value)
        if not root.is_absolute():
            raise ValueError("导出根目录应为绝对路径")
        # 保留逻辑路径供父目录验证，export_path 自身负责解析和越界检查。
        root = root.absolute()
    except (TypeError, ValueError, OSError) as exc:
        errors.append(_issue("INVALID_ROOT", str(exc)))
        return report
    project = _get(plan, "project")
    if not isinstance(project, str) or not project.strip():
        errors.append(_issue("MISSING_FIELD", "project 应为非空文本", field="project"))
    selected = {}
    for field, allowed in (("keywords", None), ("platforms", PLATFORMS),
                           ("clients", ("APP", "WEB")), ("capture_kinds", ("results", "suggestions"))):
        value = _get(plan, field)
        if not isinstance(value, (list, tuple)) or not value:
            errors.append(_issue("MISSING_FIELD", f"{field} 应为非空清单", field=field))
            continue
        if any(not isinstance(v, str) or not v.strip() or any(ord(c) < 32 for c in v) or
               (allowed is not None and v not in allowed) for v in value):
            errors.append(_issue("INVALID_FIELD", f"{field} 含无效成员", field=field))
        elif len(set(value)) != len(value):
            errors.append(_issue("DUPLICATE_FIELD", f"{field} 含完全相同的重复成员", field=field))
        selected[field] = list(value)
    top_n = _get(plan, "top_n", 20)
    if type(top_n) is not int or not 1 <= top_n <= 1000:
        errors.append(_issue("INVALID_FIELD", "top_n 应为 1 至 1000 的整数", field="top_n"))
    # 只读取有效布局参数；旧 cap 字段无论是否存在，都不参与预检及报告中的有效上限。
    for field, default in (("composite_columns", 4), ("composite_cell_width", 1080)):
        value = _get(plan, field, default)
        report["composition"][field] = value
        if type(value) is not int or value < 1:
            errors.append(_issue("INVALID_FIELD", f"{field} 应为正整数", field=field))
    layout = _get(plan, "composite_layout", "grid")
    report["composition"]["composite_layout"] = layout
    if layout not in ("horizontal", "grid"):
        errors.append(_issue("INVALID_FIELD", "composite_layout 应为 horizontal/grid"))
    templates = {
        "raw": (_get(plan, "directory_template", DEFAULT_DIRECTORY), _get(plan, "filename_template", DEFAULT_FILENAME)),
        "composite": (_get(plan, "composite_directory_template", "{project}-{year}-{month}月/{platform}/{client}/{display_type}/汇总"),
                      _get(plan, "composite_filename_template", "{content_label}_{batch:02d}_汇总_{artifact_id}_{part:02d}.png")),
    }
    fields_by_kind = {}
    for kind, (directory, filename) in templates.items():
        try:
            validate_template(directory)
            validate_template(filename)
            if not filename or "/" in filename or "\\" in filename or not filename.lower().endswith(".png"):
                raise ValueError("文件名模板应为单个 .png 文件名")
            used = _template_fields(directory) | _template_fields(filename)
            fields_by_kind[kind] = used
            if "capture_stamp" in used:
                warnings.append(_issue("UNKNOWN_CAPTURE_TIME", "capture_stamp 使用同一示例值；其实际值在采集结束后才有证据", kind=kind))
            if "year" not in used:
                warnings.append(_issue("YEAR_NOT_EXPLICIT", "模板未显式包含 year；月份目录本身不区分年份，身份仍须依元数据", kind=kind))
            if "artifact_id" not in used:
                warnings.append(_issue("NO_ARTIFACT_ID", "模板未含 artifact_id；重复执行需重新检验同名冲突", kind=kind))
            if kind == "raw" and not ({"part", "artifact_id"} & used):
                warnings.append(_issue("MULTIPART_COLLISION", "原图多帧共用时间戳时同名；请加入 part 或每张独立 artifact_id", kind=kind))
        except (ValueError, TypeError) as exc:
            errors.append(_issue("INVALID_TEMPLATE", str(exc), kind=kind))
    if errors:
        return report
    assert isinstance(project, str)
    destinations = {}
    directories = {root}

    def preview(kind, platform, client, capture_kind, keyword, index):
        """生成一项代表路径并做批内、当前文件系统和跨年复用检查。"""
        identity = hashlib.sha256(f"{kind}|{platform}|{client}|{capture_kind}|{keyword}".encode()).hexdigest()[:12]
        values = artifact_values(project=project, platform=platform, client=client, capture_kind=capture_kind,
                                 keyword=keyword, captured_at=now, top_n=top_n, batch=batch_number,
                                 artifact_id=identity, part=1)
        item = {"kind": kind, "index": index, "platform": platform, "client": client,
                "capture_kind": capture_kind, "keyword": keyword, "keyword_id": keyword_id(keyword),
                "part": 1, "values": values, "path": None, "estimated": True,
                "unresolved_fields": ["captured_at", "artifact_id", "batch", "status"],
                "group_key": [now.year, now.month, now.day, platform, client, capture_kind]}
        if kind == "raw":
            item["unresolved_fields"].append("part_count")
        else:
            item["single_image"] = True
        directory, filename = templates[kind]
        try:
            path = export_path(root, directory, filename, values)
            item["path"] = str(path)
            directories.add(path.parent)
            identity_path = path_identity(path)
            if identity_path in destinations:
                first = destinations[identity_path]
                errors.append(_issue("PATH_COLLISION", "批内目标撞名（含大小写折叠/字段清理）", path=str(path),
                                     kind=kind, index=index, conflicts_with=first))
            else:
                destinations[identity_path] = {"kind": kind, "index": index, "keyword": keyword}
            # 目录也算占用；即使示例 ID 未命中历史路径，也不暗示真正运行时无冲突。
            if path.exists() and identity_path not in verified_paths:
                errors.append(_issue("PATH_EXISTS", "示例目标已被文件或目录占用", path=str(path)))
            item["verified_existing"] = identity_path in verified_paths
            if "artifact_id" not in fields_by_kind[kind]:
                future = {**values, "year": now.year + 1,
                          "capture_stamp": f"{now.year + 1:04d}" + values["capture_stamp"][4:]}
                if path_identity(export_path(root, directory, filename, future)) == identity_path:
                    warnings.append(_issue("CROSS_YEAR_COLLISION", "相同月日的跨年任务将复用此路径", kind=kind, path=str(path)))
            if kind == "raw" and "part" not in fields_by_kind[kind] and "artifact_id" in fields_by_kind[kind]:
                item["requires_unique_id_per_part"] = True
            # partial 会改变 content_label，检查最长常见状态路径，避免仅成功示例通过长度验证。
            partial_values = {**values, "content_label": values["content_label"] + "-未完整验证"}
            item["partial_path"] = str(export_path(root, directory, filename, partial_values))
            directories.add(Path(item["partial_path"]).parent)
            if path_identity(item["partial_path"]) != identity_path and Path(item["partial_path"]).exists() and path_identity(item["partial_path"]) not in verified_paths:
                errors.append(_issue("PATH_EXISTS", "未完整状态的示例目标已被占用", path=item["partial_path"]))
        except (KeyError, ValueError, OSError) as exc:
            errors.append(_issue("INVALID_PATH", str(exc), kind=kind, index=index, keyword=keyword))
        return item

    for platform in selected["platforms"]:
        for client in selected["clients"]:
            for capture_kind in selected["capture_kinds"]:
                for keyword in selected["keywords"]:
                    raw_paths.append(preview("raw", platform, client, capture_kind, keyword, len(raw_paths)))
                group = preview("composite", platform, client, capture_kind, "全部关键词", len(composite_paths))
                group["members"] = [{"keyword": keyword, "keyword_id": keyword_id(keyword), "index": index}
                                    for index, keyword in enumerate(selected["keywords"])]
                composite_paths.append(group)
    seen_checks = set()
    for directory in sorted(directories, key=str):
        # 检查每条真实父路径，避免同一祖先的另一分支被文件挡住时漏检。
        try:
            check = _directory_check(directory, False)
            ancestor = check["checked_ancestor"]
            if probe_writable and ancestor not in seen_checks:
                check = _directory_check(directory, True)
            seen_checks.add(ancestor)
            report["writable_checks"].append(check)
        except (OSError, ValueError) as exc:
            errors.append(_issue("NOT_WRITABLE", str(exc), directory=str(directory)))
    if not probe_writable:
        warnings.append(_issue("WRITABILITY_NOT_PROBED", "本次仅检查父目录和权限迹象；未执行临时写入探针"))
    report["preview"] = [*raw_paths, *composite_paths]
    report["counts"] = {"tasks": len(raw_paths), "groups": len(composite_paths), "preview_paths": len(report["preview"])}
    report["ok"] = not errors
    # 同一种风险可能影响每个关键词；按完整内容去重，保留不同路径的定位信息。
    unique = []
    for item in warnings:
        if item not in unique:
            unique.append(item)
    report["warnings"] = unique
    return report
