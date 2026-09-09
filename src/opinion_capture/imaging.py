"""Pillow 派生图处理；输入顺序由调用方冻结，不推断平台、关键词或排名。"""

from __future__ import annotations

import math
import os
import tempfile
import struct
import zlib
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Callable, Iterable, Mapping

from PIL import Image, ImageChops, ImageStat, PngImagePlugin

from .exporting import export_path, path_identity, publish_immutable

DEFAULT_MAX_PIXELS = 50_000_000


class ImagingError(ValueError):
    """参数、资源预算或连续性证据不满足要求。"""


class PageCompositionError(ImagingError):
    """分页中断时携带已完整发布页，避免文件留存但批次清单丢失。"""

    def __init__(self, message, published_pages, failed_part, planned_pages):
        super().__init__(message)
        self.published_pages = list(published_pages)
        self.failed_part = failed_part
        self.planned_pages = planned_pages


class OverlapError(ImagingError):
    """重叠缺失、重复帧、低纹理或多个候选存在歧义。"""


def _integer(name: str, value: int, minimum: int = 1) -> None:
    """校验整数参数，避免布尔值和隐式小数截断。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ImagingError(f"{name} 应为 >= {minimum} 的整数")


def _paths(image_paths: Iterable[str | Path], output_path: str | Path):
    """固定输入清单并校验新输出路径；原图及已有派生图均禁止覆盖。"""
    if isinstance(image_paths, (str, Path)):
        raise ImagingError("image_paths 应为路径清单")
    paths = [Path(p).resolve(strict=True) for p in image_paths]
    if not paths:
        raise ImagingError("图片清单为空")
    raw_output = Path(output_path).absolute()
    if raw_output.is_symlink():
        raise FileExistsError(f"输出路径已为符号链接：{raw_output}")
    output = raw_output.resolve()
    if output in paths or output.exists():
        raise FileExistsError(f"输出路径已有文件或指向原图：{output}")
    if output.suffix.lower() != ".png":
        raise ImagingError("派生输出扩展名须为 .png")
    return paths, output


def _budget(width: int, height: int, limit: int) -> None:
    """在分配画布或解码原图前校验像素预算。"""
    if width * height > limit:
        raise ImagingError(f"像素预算超限：{width}×{height} > {limit}")


def _open_source(path: Path, *, unrestricted_png: bool = False):
    """汇总原生PNG按文件格式读取，不修改Pillow全局像素阈值影响并行采集。"""
    if unrestricted_png and path.suffix.lower() == ".png":
        return PngImagePlugin.PngImageFile(path)
    return Image.open(path)


def _size(path: Path, limit: int | None) -> tuple[int, int]:
    """只读文件头；limit为None时不使用业务像素阈值，多帧输入仍须先拆帧。"""
    with _open_source(path, unrestricted_png=limit is None) as im:
        if limit is not None:
            _budget(im.width, im.height, limit)
        if getattr(im, "n_frames", 1) != 1:
            raise ImagingError(f"输入须为单帧图片：{path}")
        return im.size


def _read(path: Path, *, unrestricted_png: bool = False) -> Image.Image:
    """读取为独立 RGB 像素，透明区域铺白，绝不写回原文件。"""
    with _open_source(path, unrestricted_png=unrestricted_png) as im:
        if im.mode in ("RGBA", "LA") or "transparency" in im.info:
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, "white")
            return Image.alpha_composite(bg, rgba).convert("RGB")
        return im.convert("RGB")


def _save(image: Image.Image, output: Path) -> dict:
    """先同步并验证临时 PNG，再排他发布；最终路径始终对应完整文件。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".compose-", suffix=".tmp", dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            image.save(stream, format="PNG")
            stream.flush()
            os.fsync(stream.fileno())
        with Image.open(temporary) as check:
            check.verify()
        publication = publish_immutable(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(output), "width": image.width, "height": image.height,
            "pixels": image.width * image.height, "format": "PNG", "publication": publication}


def compose_grid(image_paths: Iterable[str | Path], output_path: str | Path,
                 columns: int = 3, cell_width: int = 1080, gap: int = 16,
                 max_pixels: int = DEFAULT_MAX_PIXELS,
                 max_input_pixels: int = DEFAULT_MAX_PIXELS) -> dict:
    """按清单顺序逐行排版，列数设为图片数即可横排。

    各图等比例缩小至 cell_width 内（小图不放大），行高取该行最大值；
    顶部对齐、四周与单元间留 gap 白边，无裁切、无等高拉伸。
    max_pixels 限制输出画布；max_input_pixels 限制每张输入的解码尺寸。
    返回路径、尺寸、输入清单和 placements（原尺寸、位置、实际尺寸）。
    """
    for name, value in (("columns", columns), ("cell_width", cell_width),
                        ("max_pixels", max_pixels), ("max_input_pixels", max_input_pixels)):
        _integer(name, value)
    _integer("gap", gap, 0)
    paths, output = _paths(image_paths, output_path)
    sizes = [_size(p, max_input_pixels) for p in paths]
    scaled = [(min(cell_width, w), max(1, round(h * min(cell_width, w) / w)))
              for w, h in sizes]
    cols = min(columns, len(paths))
    rows = [max(h for _, h in scaled[i:i + cols]) for i in range(0, len(paths), cols)]
    width = cols * cell_width + (cols + 1) * gap
    height = sum(rows) + (len(rows) + 1) * gap
    _budget(width, height, max_pixels)
    placements = []
    with Image.new("RGB", (width, height), "white") as canvas:
        y = gap
        for index, (path, size) in enumerate(zip(paths, scaled)):
            row, col = divmod(index, cols)
            if index and col == 0:
                y += rows[row - 1] + gap
            x = gap + col * (cell_width + gap)
            with _read(path) as source:
                with source.resize(size, Image.Resampling.LANCZOS) as resized:
                    canvas.paste(resized, (x, y))
            placements.append({"index": index, "path": str(path), "source_size": list(sizes[index]),
                               "x": x, "y": y, "width": size[0], "height": size[1]})
        result = _save(canvas, output)
    return dict(result, inputs=[str(p) for p in paths], columns=cols, placements=placements)


def _png_chunk(stream, kind: bytes, payload: bytes) -> None:
    """写入带CRC的PNG数据块；每个IDAT块至多1MiB，不依赖完整画布。"""
    stream.write(struct.pack(">I", len(payload)) + kind)
    stream.write(payload)
    stream.write(struct.pack(">I", zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff))


def _composite_scanlines(rows, width: int, cell_width: int, gap: int, cache_root: Path):
    """每行仅暂存本行源图RGB到磁盘，再逐扫描线拼接；内存不随总画布高度增长。"""
    white = b"\xff" * (width * 3)
    for _ in range(gap):
        yield white
    for row in rows:
        with tempfile.TemporaryDirectory(prefix=".compose-cache-", dir=cache_root) as cache, ExitStack() as stack:
            streams = []
            for col, unit in enumerate(row):
                raw = Path(cache) / f"{col}.rgb"
                with _read(Path(unit["path"]), unrestricted_png=True) as source, raw.open("wb") as target:
                    if list(source.size) != unit["source_size"]:
                        raise ImagingError("输入尺寸在排版后变化，请冻结原图后重试")
                    resized = source if source.size == unit["size"] else source.resize(unit["size"], Image.Resampling.LANCZOS)
                    try:
                        # 阈值只用于细分内存块，不限制整图；避免tobytes复制完整长图。
                        strip_height = min(128, max(1, (Image.MAX_IMAGE_PIXELS or 4_194_304) // resized.width))
                        for top in range(0, resized.height, strip_height):
                            with resized.crop((0, top, resized.width, min(top + strip_height, resized.height))) as strip:
                                target.write(strip.tobytes())
                    finally:
                        if resized is not source:
                            resized.close()
                streams.append(stack.enter_context(raw.open("rb")))
            for y in range(max(u["card_height"] for u in row)):
                scanline = bytearray(white)
                for col, (unit, source) in enumerate(zip(row, streams)):
                    if y >= unit["size"][1]:
                        continue
                    count = unit["size"][0] * 3
                    pixels = source.read(count)
                    if len(pixels) != count:
                        raise ImagingError("图片像素缓存提前结束，未发布汇总图")
                    left = (gap + col * (cell_width + gap)) * 3
                    scanline[left:left + count] = pixels
                yield bytes(scanline)
        for _ in range(gap):
            yield white


def _save_scanlines(width: int, height: int, rows, output: Path) -> dict:
    """将所有扫描线写入同一PNG，完整校验后排他发布；失败不拆图、不留下半张成品。"""
    # PNG格式规定宽高使用正的31位整数；这是格式边界，而非业务分页阈值。
    if not (0 < width <= 0x7fffffff and 0 < height <= 0x7fffffff):
        raise ImagingError("尺寸超出PNG格式范围，未生成或拆分汇总图")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".compose-", suffix=".tmp", dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream, closing(rows):
            stream.write(b"\x89PNG\r\n\x1a\n")
            _png_chunk(stream, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            encoder = zlib.compressobj()
            count = 0
            for scanline in rows:
                if len(scanline) != width * 3 or count >= height:
                    raise ImagingError("汇总扫描线尺寸不匹配，未发布图片")
                compressed = encoder.compress(b"\0" + scanline)
                for index in range(0, len(compressed), 1024 * 1024):
                    _png_chunk(stream, b"IDAT", compressed[index:index + 1024 * 1024])
                count += 1
            if count != height:
                raise ImagingError("汇总扫描线数量不完整，未发布图片")
            compressed = encoder.flush()
            for index in range(0, len(compressed), 1024 * 1024):
                _png_chunk(stream, b"IDAT", compressed[index:index + 1024 * 1024])
            _png_chunk(stream, b"IEND", b"")
            stream.flush()
            os.fsync(stream.fileno())
        # 直接读取已知PNG，核对尺寸及所有块CRC，不改变全局解码配置。
        with PngImagePlugin.PngImageFile(temporary) as check:
            if check.size != (width, height):
                raise ImagingError("汇总PNG尺寸校验失败")
            check.verify()
        publication = publish_immutable(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(output), "width": width, "height": height, "pixels": width * height,
            "format": "PNG", "publication": publication, "encoding": "streamed_rgb_rows"}


def compose_pages(image_paths: Iterable[str | Path | None], output_dir: str | Path, *,
                  members: Iterable[Mapping] | None = None, layout: str = "grid",
                  columns: int = 4, cell_width: int = 1080, gap: int = 16,
                  max_pixels: int | None = None, max_height: int | None = None,
                  max_input_pixels: int | None = None,
                  filename_template: str = "page_{part:02d}.png",
                  output_path_factory: Callable[[int], str | Path] | None = None) -> dict:
    """按冻结顺序输出一张汇总图；兼容旧函数名和pages返回结构，part固定为1。

    image_paths 与 members 一一对应；只排版有效图片，不添加标题、状态条或
    文字占位卡。缺图/失败状态保存在members和omitted_members，不冒充成功内容。
    grid按columns换行，horizontal单行；只按用户cell_width等比例缩小，
    不为适配尺寸额外缩图。旧max_pixels/max_height/max_input_pixels参数不再生效。
    按行缓存、流式写入一个PNG；内存/磁盘/格式失败明确报错，不拆图。路径与
    不覆盖合同不变；没有有效输入时pages为空，否则只含一张完整成品。
    """
    for name, value in (("columns", columns), ("cell_width", cell_width)):
        _integer(name, value)
    _integer("gap", gap, 0)
    if layout not in ("grid", "horizontal"):
        raise ImagingError("layout 应为 grid 或 horizontal")
    if isinstance(image_paths, (str, Path)):
        raise ImagingError("image_paths 应为路径清单")
    raw_paths = list(image_paths)
    metadata = [dict(m) for m in members] if members is not None else [{} for _ in raw_paths]
    if not raw_paths or len(metadata) != len(raw_paths):
        raise ImagingError("图片清单应非空且与 members 一一对应，缺图请传 None")
    root = Path(output_dir).resolve()
    units, omitted = [], []
    for index, (raw, member) in enumerate(zip(raw_paths, metadata)):
        status = str(member.get("status", "success" if raw is not None else "missing"))
        path = Path(raw).resolve(strict=True) if raw is not None and status in ("success", "partial") else None
        if path is None and status in ("success", "partial"):
            status = "missing"
        if path is None:
            omitted.append({"index": index, "order": index + 1, "keyword": member.get("keyword"),
                            "status": status, "member": member, "reason": "no_usable_image"})
            continue
        size = _size(path, None)
        scaled = (min(cell_width, size[0]), max(1, round(size[1] * min(cell_width, size[0]) / size[0])))
        units.append({"index": index, "order": index + 1, "path": str(path) if path else None,
                      "keyword": member.get("keyword"), "status": status, "member": member,
                      "source_size": list(size) if size else None, "size": scaled,
                      "card_height": scaled[1]})

    if not units:
        return {"pages": [], "page_count": 0, "members": metadata, "omitted_members": omitted,
                "status": "partial", "reason": "no_usable_images", "layout": layout,
                "cell_width": cell_width, "order": list(range(len(raw_paths))),
                "rendered_order": [], "added_annotations": False, "single_image": True,
                "boundaries": ["没有有效图片时不生成文字占位图，缺失信息仅保存在清单。"]}

    def dimensions(rows):
        """把行结构换算为实际画布，空列不占据最终输出宽度。"""
        cols = max(len(row) for row in rows)
        return (cols * cell_width + (cols + 1) * gap,
                sum(max(u["card_height"] for u in row) for row in rows) + (len(rows) + 1) * gap)

    target_columns = len(units) if layout == "horizontal" else columns
    pages_rows = [[units[index:index + target_columns] for index in range(0, len(units), target_columns)]]

    # 沿用原路径合同；单张汇总的part固定1，先校验目标再开始写入。
    outputs, identities = [], set()
    source_ids = {path_identity(p) for p in raw_paths if p is not None}
    for part in range(1, len(pages_rows) + 1):
        if output_path_factory is not None:
            output = Path(output_path_factory(part)).absolute()
        else:
            import string
            for _, field, spec, conversion in string.Formatter().parse(filename_template):
                if field is not None and (field not in {"part", "page", "index"} or conversion or spec not in ("", "02d", "03d", "04d")):
                    raise ImagingError("分页文件模板只接受 part/page/index 和整数补零格式")
            leaf = filename_template.format(part=part, page=part, index=part)
            if not leaf or any(c in leaf for c in '/\\<>:"|?*') or any(ord(c) < 32 for c in leaf):
                raise ImagingError("分页文件模板应为合法的单个文件名")
            output = root / leaf
        if output.is_symlink():
            raise FileExistsError(f"输出路径已为符号链接：{output}")
        output = output.resolve()
        if not output.is_relative_to(root) or output.suffix.lower() != ".png":
            raise ImagingError("分页输出须为 output_dir 内的 PNG")
        # 回调路径也遵循原图导出的 Windows 片段/保留字/UTF-16 长度规则。
        relative = output.relative_to(root)
        try:
            export_path(root, "" if relative.parent == Path(".") else str(relative.parent).replace("{", "{{").replace("}", "}}"),
                        output.name.replace("{", "{{").replace("}", "}}"), {})
        except ValueError as exc:
            raise ImagingError(str(exc)) from exc
        identity = path_identity(output)
        if identity in identities or identity in source_ids or output.exists():
            raise FileExistsError(f"分页目标重复、已有文件或指向原图：{output}")
        identities.add(identity)
        outputs.append(output)

    pages = []
    for part, (page_rows, output) in enumerate(zip(pages_rows, outputs), 1):
        try:
            width, height = dimensions(page_rows)
            placements = []
            y = gap
            for row_number, row in enumerate(page_rows):
                for col, unit in enumerate(row):
                    x = gap + col * (cell_width + gap)
                    placements.append({k: unit[k] for k in ("index", "order", "path", "keyword", "status", "member", "source_size")}
                                      | {"page": 1, "part": 1, "row": row_number, "column": col,
                                         "x": x, "y": y, "width": unit["size"][0], "height": unit["size"][1],
                                         "card_box": [x, y, x + cell_width, y + unit["card_height"]],
                                         "label_box": None, "placeholder": False})
                y += max(u["card_height"] for u in row) + gap
            result = _save_scanlines(width, height, _composite_scanlines(page_rows, width, cell_width, gap, output.parent), output)
            pages.append(result | {"page": part, "part": part, "page_count": len(outputs),
                                   "columns": max(len(row) for row in page_rows), "placements": placements,
                                   "members": [p["member"] for p in placements], "order": [p["index"] for p in placements],
                                   "inputs": [p["path"] for p in placements if p["path"]],
                                   "omitted_members": omitted, "added_annotations": False, "single_image": True})
        except Exception as exc:
            raise PageCompositionError(str(exc), pages, part, len(outputs)) from exc
    return {"pages": pages, "page_count": len(pages), "members": metadata, "omitted_members": omitted,
            "status": "success" if not omitted and all(u["status"] == "success" for u in units) else "partial",
            "layout": layout, "cell_width": cell_width, "order": list(range(len(raw_paths))),
            "rendered_order": [u["index"] for u in units], "added_annotations": False, "single_image": True,
            "boundaries": ["每组一张PNG，按列数换行，不按像素/高度拆图。", "资源或PNG格式限制导致失败时不降清晰度、不发布半张图。"]}


def _mae(a: Image.Image, b: Image.Image) -> float:
    """计算 0—255 像素域的平均绝对误差，不将其解释为统计概率。"""
    with ImageChops.difference(a, b) as diff:
        values = ImageStat.Stat(diff).mean
        return sum(values) / len(values)


def _overlap(previous: Image.Image, current: Image.Image, min_overlap: int,
             max_overlap: int | None, compare_width: int, max_error: float,
             min_margin: float, min_texture: float) -> dict:
    """仅横向缩小灰度图、逐像素搜索竖直位移，再用原分辨率 RGB 复核。

    所有合法候选参与唯一性比较，包括相邻像素位移；宁可报歧义，
    不将多个近似匹配压成一个候选。每个重叠三分区还须有竖向纹理。
    """
    width = min(compare_width, previous.width)
    a = previous.convert("L").resize((width, previous.height), Image.Resampling.BOX)
    b = current.convert("L").resize((width, current.height), Image.Resampling.BOX)
    if a.size == b.size and _mae(a, b) <= max_error:
        raise OverlapError("重复帧或近似静止帧：缺少可信滚动增量")
    limit = min(a.height, b.height) - 1
    if max_overlap is not None:
        limit = min(limit, max_overlap)
    if limit < min_overlap:
        raise OverlapError("重叠搜索区间为空")
    scores = []
    for overlap in range(min_overlap, limit + 1):
        left = a.crop((0, a.height - overlap, width, a.height))
        right = b.crop((0, 0, width, overlap))
        scores.append((_mae(left, right), overlap))
    scores.sort()
    error, overlap = scores[0]
    runner_up = scores[1][0] if len(scores) > 1 else None
    if error > max_error:
        raise OverlapError(f"低置信重叠：最佳灰度误差 {error:.4f} > {max_error}")
    if runner_up is not None and runner_up - error < min_margin:
        raise OverlapError(f"重复纹理/位移歧义：候选误差差值 {runner_up - error:.4f} < {min_margin}")
    for i in range(3):
        start, end = i * overlap // 3, (i + 1) * overlap // 3
        patch = b.crop((0, start, width, end))
        gradient = _mae(patch.crop((0, 0, width, patch.height - 1)),
                        patch.crop((0, 1, width, patch.height)))
        if ImageStat.Stat(patch).stddev[0] < min_texture or gradient < 1.0:
            raise OverlapError("空白/低纹理重叠：竖向定位证据不足")
        original_a = previous.crop((0, previous.height - overlap + start, previous.width,
                                    previous.height - overlap + end))
        original_b = current.crop((0, start, current.width, end))
        if _mae(original_a, original_b) > max_error:
            raise OverlapError("低置信重叠：原分辨率 RGB 分区复核失败")
    return {"overlap": overlap, "gray_mae": error, "runner_up_mae": runner_up,
            "margin": None if runner_up is None else runner_up - error,
            "status": "accepted"}


def stitch_vertical(image_paths: Iterable[str | Path], output_path: str | Path,
                    content_top: int = 0, content_bottom: int | None = None,
                    min_overlap: int = 32, max_overlap: int | None = None,
                    compare_width: int = 48, max_error: float = 1.5,
                    min_margin: float = 0.75, min_texture: float = 8.0,
                    max_pixels: int = DEFAULT_MAX_PIXELS,
                    max_input_pixels: int = DEFAULT_MAX_PIXELS) -> dict:
    """按既定顺序拼接至少两张连续向下滚动、同宽且无缩放的帧。

    content_top/content_bottom 是每帧内容区的上下边界（底边不含）；
    固定栏由调用方明确裁掉，输出仅含内容区。重叠单位为原图像素。
    误差阈值单位为 0—255，不是概率；默认值尚未经真实 App 校准。
    重复、空白、歧义、低置信均抛 OverlapError，失败不输出猜测拼图。
    返回路径、尺寸、内容裁切和每对帧的候选误差、重叠高度。
    """
    for name, value in (("compare_width", compare_width), ("max_pixels", max_pixels),
                        ("max_input_pixels", max_input_pixels)):
        _integer(name, value)
    _integer("content_top", content_top, 0)
    _integer("min_overlap", min_overlap, 6)
    if content_bottom is not None:
        _integer("content_bottom", content_bottom)
    if max_overlap is not None:
        _integer("max_overlap", max_overlap, min_overlap)
    for name, value in (("max_error", max_error), ("min_margin", min_margin), ("min_texture", min_texture)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ImagingError(f"{name} 应为有限正数")
    paths, output = _paths(image_paths, output_path)
    if len(paths) < 2:
        raise ImagingError("连续拼接至少需要两帧")
    sizes = [_size(p, max_input_pixels) for p in paths]
    if len({w for w, _ in sizes}) != 1:
        raise ImagingError("连续帧内容宽度须一致，不自动拉伸对齐")
    boxes = [(0, content_top, w, h if content_bottom is None else content_bottom) for w, h in sizes]
    for box, (_, h) in zip(boxes, sizes):
        if not 0 <= box[1] < box[3] <= h:
            raise ImagingError("内容裁切边界超出帧范围")
    width, total = sizes[0][0], boxes[0][3] - content_top
    _budget(width, total, max_pixels)
    matches = []
    previous = None
    try:
        for index, (path, box) in enumerate(zip(paths, boxes)):
            with _read(path) as source:
                current = source.crop(box)
            if previous is not None:
                try:
                    match = _overlap(previous, current, min_overlap, max_overlap,
                                     compare_width, max_error, min_margin, min_texture)
                except Exception:
                    current.close()
                    raise
                total += current.height - match["overlap"]
                matches.append(dict(match, previous_index=index - 1, current_index=index))
                previous.close()
            previous = current
            _budget(width, total, max_pixels)
    finally:
        if previous is not None:
            previous.close()
    with Image.new("RGB", (width, total), "white") as canvas:
        y = 0
        for index, (path, box) in enumerate(zip(paths, boxes)):
            skip = 0 if index == 0 else matches[index - 1]["overlap"]
            with _read(path) as source:
                with source.crop((0, content_top + skip, width, box[3])) as fragment:
                    canvas.paste(fragment, (0, y))
                    y += fragment.height
        result = _save(canvas, output)
    return dict(result, inputs=[str(p) for p in paths], content_top=content_top,
                content_bottom=content_bottom, overlaps=matches)
