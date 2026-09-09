"""已加载 document 全页原图的本地 TOP N 裁切；不访问页面、不拼接或覆盖源图。

这里验证截图、DOM 顺序和几何边界；来源 URL、关键词及输入框可见性由调用方
另行验证。旧实拍元数据可用于几何回放，不因此被升级为新的业务成功证据。
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path

from .web_results_xiaohongshu import fingerprint, note_identity


MAX_FULLPAGE_PIXELS = 60_000_000


class FullPageNotReady(ValueError):
    """全页证明条件不足；调用方可继续原有滚动证据链。"""


def _check(budget) -> None:
    """可选的任务总预算，不在派生图处理时重置期限。"""
    if budget is not None:
        budget.check()


def file_sha256(path: Path, budget=None) -> str:
    """分块读取本地证据，期间检查取消及剩余预算。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            _check(budget)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def plan_fullpage(before: dict, after: dict, dimensions, top_n: int) -> dict:
    """仅计算稳定全页中前 N 个独立笔记的原生整宽裁切框。

    排名沿用 feed DOM 顺序，底边取前 N 条卡片的最大底边，不按瀑布流 y 排序。
    同行后续笔记和原生页面上下文保留，返回数量不将这些背景笔记计入 TOP N。
    支持等比例设备像素；内层滚动容器、非顶部、缺图、重复身份均保留不足状态。
    """
    if type(top_n) is not int or top_n < 1:
        raise ValueError("top_n 必须是正整数")
    if fingerprint(before) != fingerprint(after):
        raise FullPageNotReady("dom_changed_during_fullpage_screenshot")
    state = after
    if state.get("schema_version") != 2 or state.get("feed_count") != 1:
        raise FullPageNotReady("feed_structure_not_verified")
    if state.get("search_tab") != "综合":
        raise FullPageNotReady("comprehensive_tab_not_verified")
    scroll = state.get("scroll", {})
    document = state.get("document", {})
    if scroll.get("host") != "document":
        raise FullPageNotReady("fullpage_requires_document_scroller")
    if (not scroll.get("at_top") or abs(scroll.get("top", 0)) > 1
            or abs(document.get("scroll_y", 0)) > 1 or document.get("scroll_x", 0) != 0):
        raise FullPageNotReady("feed_head_not_observed")
    if state.get("loading") or state.get("login_modal") or state.get("suggestion_panel_rect"):
        raise FullPageNotReady("fullpage_scene_not_ready")
    css_width = document.get("width", state.get("viewport", {}).get("width", 0))
    css_height = document.get("height", scroll.get("height", 0))
    values = [css_width, css_height, *dimensions]
    if len(dimensions) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
        raise FullPageNotReady("invalid_fullpage_dimensions")
    width, height = dimensions
    if width * height > MAX_FULLPAGE_PIXELS:
        raise FullPageNotReady("fullpage_pixel_budget_exceeded")
    scale = width / css_width
    if abs(css_height * scale - height) > 1:
        raise FullPageNotReady("fullpage_dimension_mismatch")
    notes = state.get("results", [])
    if len(notes) < top_n:
        raise FullPageNotReady("loaded_document_has_fewer_than_top_n")
    identities = []
    for index, note in enumerate(notes):
        identity = note_identity(note)
        if not identity or note.get("identity_error") or note.get("excluded"):
            raise FullPageNotReady("ambiguous_note_identity")
        if identity in identities:
            raise FullPageNotReady("duplicate_note_identity")
        if note.get("dom_index") != index:
            raise FullPageNotReady("feed_dom_order_not_verified")
        identities.append(identity)
    bottom = 0.0
    targets = []
    for rank, note in enumerate(notes[:top_n], 1):
        rect = note.get("document_rect", note.get("rect", []))
        if len(rect) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in rect):
            raise FullPageNotReady("invalid_card_geometry")
        x, y, w, h = rect
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > css_width+0.1 or y+h > css_height+0.1:
            raise FullPageNotReady("card_outside_fullpage")
        if note.get("images_loaded") is not True or note.get("cover_present") is not True:
            raise FullPageNotReady("target_images_incomplete")
        if not note.get("images") or any(image.get("loaded") is not True for image in note["images"]):
            raise FullPageNotReady("target_images_incomplete")
        bottom = max(bottom, y+h)
        targets.append(dict(id=identities[rank-1], href=note["href"], title=note.get("title", ""),
                            text=note.get("text", ""), promoted=bool(note.get("promoted")),
                            rank=rank, counted=True, images_loaded=True, document_rect=deepcopy(rect)))
    return dict(crop_box=[0, 0, width, min(height, math.ceil(bottom * scale))],
                css_bottom=bottom, scale=scale, observed_count=len(notes), captured_count=top_n, items=targets)


def fullpage_readiness(state: dict, top_n: int) -> str | None:
    """新实时入口的严格预检：额外要求 document 裁切及可见遮挡证明。

    历史快照缺少这些字段时只支持本地几何回放，不进入实时全页成功分支。
    """
    document = state.get("document", {})
    try:
        plan_fullpage(state, state, [document.get("width", 0), document.get("height", 0)], top_n)
    except FullPageNotReady as exc:
        return str(exc)
    if any(note.get("document_unclipped") is not True or note.get("document_uncovered") is not True
           for note in state["results"][:top_n]):
        return "fullpage_card_visibility_not_verified"
    return None


def crop_top_n(source_path: str | Path, metadata: dict, output_path: str | Path,
               top_n: int = 20, *, budget=None) -> dict:
    """从已保存且哈希匹配的全页原图生成独立派生 PNG，目标存在即报错。

    metadata 接收 before/after/sha256，与既有 results-full-page.json 同形。
    本函数不宣称业务 status/rank_verified；调用方负责补齐实时来源与可见性证明。
    """
    from PIL import Image

    _check(budget)
    source, output = Path(source_path).resolve(), Path(output_path).resolve()
    if source == output or output.exists():
        raise FileExistsError("derived_path_must_be_new_and_separate")
    digest = file_sha256(source, budget)
    if digest != metadata.get("sha256"):
        raise FullPageNotReady("fullpage_source_sha256_mismatch")
    with Image.open(source) as png:
        # 历史浏览器工具实拍有 .png 扩展名但实际 JPEG 的原图；按文件签名识别，
        # 不重编码或覆盖原始字节。只有派生产物统一编码为 PNG。
        source_format = png.format
        if source_format not in ("PNG", "JPEG"):
            raise FullPageNotReady("unsupported_source_image_format")
        plan = plan_fullpage(metadata["before"], metadata["after"], png.size, top_n)
        source_dimensions = list(png.size)
        png.verify()
    _check(budget)
    with Image.open(source) as png, png.crop(tuple(plan["crop_box"])) as derived:
        _check(budget)
        with output.open("xb") as stream:
            derived.save(stream, format="PNG")
    _check(budget)
    with Image.open(output) as check:
        dimensions = list(check.size)
        check.verify()
    for item in plan["items"]:
        item.update(screenshot_covered=True, screenshot_refs=[dict(path=str(source),
                    document_rect=item["document_rect"], coverage=[0, item["document_rect"][3]])])
    return dict(plan, primary_artifact_path=str(output), primary_sha256=file_sha256(output, budget),
                dimensions=dimensions, source_path=str(source), source_sha256=digest,
                source_dimensions=source_dimensions, source_format=source_format,
                composition="document_fullpage_top_n_dom_crop",
                contains_context_beyond_top_n=True)
