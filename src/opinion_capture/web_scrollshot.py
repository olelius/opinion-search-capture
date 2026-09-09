"""按网页实际滚动坐标拼接原始视口，不重复页头/重叠区域，不重绘内容卡片。"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from PIL import Image


def compose_scrollshot(frames: list[dict], analysis: dict, output: Path, budget=None) -> dict:
    """将已验证前N条的源帧裁接为连续图；只处理本地原图，失败不覆盖旧文件。

    独立滚动列按其完整宽度裁切；首屏保留同列的查询标题，其他固定侧栏不反复拼入。
    analysis是analyse_frames的成功结果，源坐标和每段原图映射均记录在返回值中。
    """
    def check():
        if budget is not None:
            budget.check()
    check()
    if not frames or analysis.get("rank_verified") is not True:
        raise ValueError("scrollshot_requires_verified_frames")
    items = [item for item in analysis["items"] if item.get("counted")]
    if not items:
        raise ValueError("scrollshot_has_no_target_cards")
    first = frames[0]["after"]
    if not first["scroll"].get("at_top"):
        raise ValueError("scrollshot_head_missing")
    width, viewport_height = frames[0]["dimensions"]
    rect = first["scroll"]["rect"]
    left, right = max(0, math.floor(rect[0])), min(width, math.ceil(rect[0]+rect[2]))
    offsets = [round(frame["after"]["scroll"]["top"]) for frame in frames]
    if offsets != sorted(offsets) or offsets[0] != 0:
        raise ValueError("scrollshot_offsets_not_monotonic")
    for index in range(1, len(frames)):
        previous = {n["id"]:n for n in frames[index-1]["after"]["results"] if n.get("images_loaded")}
        shared = [(previous[n["id"]],n) for n in frames[index]["after"]["results"]
                  if n.get("images_loaded") and n["id"] in previous]
        if not shared or any(abs(a["rect"][1]+offsets[index-1]-b["rect"][1]-offsets[index])>1 for a,b in shared):
            raise ValueError("scrollshot_content_geometry_changed")
    # 全部目标底边都来自相同世界坐标，不按已截屏数猜测TOP N范围。
    bottoms = []
    for item in items:
        ref = item["screenshot_refs"][0]
        x, y, w, h = ref["rect"]
        if x < left-1 or x+w > right+1:
            raise ValueError("scrollshot_target_outside_column")
        bottoms.append(offsets[ref["frame_index"]]+y+h)
    height = math.ceil(max(bottoms))
    if not 0 < (right-left)*height <= 100_000_000:
        raise ValueError("scrollshot_pixel_budget_exceeded")
    # 同一位置的等图重拍使用最后一帧，避免取回图片未加载时的旧帧。
    latest = {offset: index for index, offset in enumerate(offsets)}
    placements = []
    filled = 0
    with Image.new("RGB", (right-left, height), "white") as canvas:
        selected = list(latest.items())
        for position, (offset, index) in enumerate(selected):
            check()
            frame = frames[index]
            state = frame["after"]
            host = state["scroll"]["rect"]
            if frame["dimensions"] != [width, viewport_height] or any(abs(a-b)>1 for a,b in zip(host,rect)):
                raise ValueError("scrollshot_viewport_changed")
            source = Path(frame["path"])
            if hashlib.sha256(source.read_bytes()).hexdigest() != frame["sha256"]:
                raise ValueError("scrollshot_source_changed")
            start = 0 if not filled else max(math.ceil(host[1]), filled-offset)
            end = min(viewport_height, math.floor(host[1]+host[3]), height-offset)
            for note in state["results"]:
                if note.get("intersects") and not note.get("images_loaded"):
                    end = min(end, math.floor(note["rect"][1]))
            if position+1 < len(selected):
                next_offset, next_index = selected[position+1]
                next_top = frames[next_index]["after"]["scroll"]["rect"][1]
                overlap_start = next_offset+next_top
                # 在有效重叠带中接缝，尽量避开每屏底部的固定悬浮按钮。
                overlap_end = offset+end-min(144, host[3]*.15)
                if overlap_start < overlap_end:
                    end = min(end, round((overlap_start+overlap_end)/2)-offset)
            if end <= start:
                continue
            if start+offset != filled:
                raise ValueError("scrollshot_uncovered_gap")
            with Image.open(source) as image:
                canvas.paste(image.crop((left,start,right,end)).convert("RGB"),(0,filled))
            placements.append({"frame_index":index,"source_path":str(source),
                               "source_box":[left,start,right,end],"output_box":[0,filled,right-left,end+offset]})
            filled = end+offset
        if filled != height:
            raise ValueError("scrollshot_target_not_fully_covered")
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open("xb") as stream:
            canvas.save(stream,format="PNG")
    return {"path":str(output.resolve()),"width":right-left,"height":height,
            "sha256":hashlib.sha256(output.read_bytes()).hexdigest(),"placements":placements,
            "layout":"continuous_native_scroll_column","captured_count":len(items)}
