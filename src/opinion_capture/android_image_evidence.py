"""Android 截图中封面可见性的保守判定。

这里的结果是“是否值得等待/重试”的证据，不是图像内容分类器：均匀背景和渐变
占位只会得到 ``blank_or_unverified``，不会被当作确定的空图，也不会改写输入文件。
"""

from __future__ import annotations

import hashlib
import math
from numbers import Real
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageChops, ImageFilter, ImageStat


def _bounds(value: Sequence[int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    if len(value) != 4:
        raise ValueError("cover_bounds must contain left, top, right, bottom")
    if any(isinstance(x, bool) or not isinstance(x, int) for x in value):
        raise TypeError("cover_bounds values must be integers")
    box = tuple(value)
    if not (0 <= box[0] < box[2] <= size[0] and 0 <= box[1] < box[3] <= size[1]):
        raise ValueError("cover_bounds is outside the image")
    return box


def _mean_abs_difference(image: Image.Image) -> float:
    """返回有效相邻像素的平均差，避免人工补黑边。"""
    gray = image.convert("L")
    width, height = gray.size
    if width < 2 or height < 2:
        return 0.0
    horizontal = ImageChops.difference(gray.crop((0, 0, width - 1, height)), gray.crop((1, 0, width, height)))
    vertical = ImageChops.difference(gray.crop((0, 0, width, height - 1)), gray.crop((0, 1, width, height)))
    return (ImageStat.Stat(horizontal).mean[0] + ImageStat.Stat(vertical).mean[0]) / (2 * 255.0)

def _edge_energy(image: Image.Image) -> float:
    """以 Pillow 滤波器计算归一化梯度；不依赖单一全图标准差。"""
    gray = image.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    if edges.width > 2 and edges.height > 2:
        edges = edges.crop((1, 1, edges.width - 1, edges.height - 1))
    return ImageStat.Stat(edges).mean[0] / 255.0


def _block_variances(image: Image.Image, grid: tuple[int, int]) -> list[float]:
    gray = image.convert("L")
    cols, rows = grid
    values: list[float] = []
    for row in range(rows):
        for col in range(cols):
            left = col * gray.width // cols
            top = row * gray.height // rows
            right = (col + 1) * gray.width // cols
            bottom = (row + 1) * gray.height // rows
            values.append(ImageStat.Stat(gray.crop((left, top, right, bottom))).var[0])
    return values


def assess_cover(
    path: str | Path,
    cover_bounds: Sequence[int],
    *,
    fully_visible: bool = True,
    grid: tuple[int, int] = (3, 3),
    block_variance_threshold: float = 8.0,
    edge_energy_threshold: float = 0.025,
    near_solid_range: float = 12.0,
    minimum_content_blocks: int = 2,
    center_margin: float = 0.15,
) -> dict:
    """评估完整截图中的原始封面区域。

    ``cover_sha256`` 是完整分辨率 RGB crop 的原始像素字节 hash，不是缩略图 hash。
    采用避开四角控件的中心区域块方差 + 边缘能量 + 近纯色范围的联合界限；默认边缘界限为0.025，阈值是待确认加载启发式，
    不代表准确的空图/有图分类。只有 ``fully_visible`` 且结果未确认时才建议重试。
    """
    if len(grid) != 2 or any(isinstance(x, bool) or not isinstance(x, int) for x in grid):
        raise TypeError("grid values must be integers")
    if grid[0] < 1 or grid[1] < 1:
        raise ValueError("grid must contain positive column and row counts")
    if isinstance(minimum_content_blocks, bool) or not isinstance(minimum_content_blocks, int):
        raise TypeError("minimum_content_blocks must be an integer")
    if not 1 <= minimum_content_blocks <= grid[0] * grid[1]:
        raise ValueError("minimum_content_blocks is outside the grid")
    if isinstance(center_margin, bool) or not isinstance(center_margin, Real) or not math.isfinite(center_margin) or not 0 <= center_margin < 0.5:
        raise ValueError("center_margin must be in [0, 0.5)")
    for name, value in (("block_variance_threshold", block_variance_threshold), ("edge_energy_threshold", edge_energy_threshold), ("near_solid_range", near_solid_range)):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number")
    image_path = Path(path)
    raw_file = image_path.read_bytes()
    with Image.open(image_path) as source:
        box = _bounds(cover_bounds, source.size)
        crop = source.crop(box).convert("RGB")
        raw_cover_bytes = crop.tobytes()
        gray = crop.convert("L")
        low, high = gray.getextrema()
        margin_x = int(crop.width * center_margin)
        margin_y = int(crop.height * center_margin)
        analysis_box = (margin_x, margin_y, crop.width - margin_x, crop.height - margin_y)
        analysis_crop = crop.crop(analysis_box)
        block_variances = _block_variances(analysis_crop, grid)
        content_blocks = sum(v >= block_variance_threshold for v in block_variances)
        edge = _edge_energy(analysis_crop)
        # 同一照片在高分辨率封面中边缘像素占比更低；增加统一尺度分析，阈值不随失败下调。
        # 仅缩放分析副本，原始封面/hash/导出尺寸保持不变；纯色与轻微渐变仍无有效纹理。
        normalized = analysis_crop.copy()
        normalized.thumbnail((64, 64), Image.Resampling.BOX)
        normalized_edge = _edge_energy(normalized)
        neighbor_edge = _mean_abs_difference(analysis_crop)
        global_variance = ImageStat.Stat(gray).var[0]

    near_solid = (high - low) <= near_solid_range
    high_variance_blocks = sum(v >= max(25.0, block_variance_threshold * 4) for v in block_variances)
    content_visible = (high_variance_blocks >= minimum_content_blocks and max(edge, normalized_edge) >= edge_energy_threshold and (not near_solid or high_variance_blocks >= minimum_content_blocks + 1))
    status = "content_visible" if content_visible else "blank_or_unverified"
    return {
        "status": status,
        "retry_recommended": bool(fully_visible and status == "blank_or_unverified"),
        "fully_visible": bool(fully_visible),
        "cover_bounds": list(box),
        "cover_size": [box[2] - box[0], box[3] - box[1]],
        "analysis_bounds": [box[0] + analysis_box[0], box[1] + analysis_box[1], box[0] + analysis_box[2], box[1] + analysis_box[3]],
        "cover_sha256": hashlib.sha256(raw_cover_bytes).hexdigest(),
        "source_sha256": hashlib.sha256(raw_file).hexdigest(),
        "raw_cover_bytes": len(raw_cover_bytes),
        "grid": list(grid),
        "block_variances": [round(v, 6) for v in block_variances],
        "content_blocks": content_blocks,
        "high_variance_blocks": high_variance_blocks,
        "global_variance": round(global_variance, 6),
        "range": high - low,
        "edge_energy": round(edge, 6),
        "normalized_edge_energy": round(normalized_edge, 6),
        "normalized_analysis_size": list(normalized.size),
        "neighbor_edge_energy": round(neighbor_edge, 6),
        "thresholds": {
            "block_variance": block_variance_threshold,
            "edge_energy": edge_energy_threshold,
            "near_solid_range": near_solid_range,
            "minimum_content_blocks": minimum_content_blocks,
            "center_margin": center_margin,
        },
        "classification_note": "待确认加载启发式；均匀背景/渐变不等于确定空图。",
    }


__all__ = ["assess_cover"]




