"""按网页实际滚动坐标拼接原始视口，不重复页头/重叠区域，不重绘内容卡片。"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
from io import BytesIO
import math
from pathlib import Path

from PIL import Image


def _raster_edge(value: float, *, upper: bool) -> int:
    """覆盖区间转像素边界；仅吸收 rect 一位小数造成的至多 0.05px 舍入误差。"""
    nearest = round(value)
    if abs(value-nearest) <= .051:
        return nearest
    return math.ceil(value) if upper else math.floor(value)


def _document_coverage(frames, items, offsets, width, viewport_height, height):
    """按每张卡的原帧覆盖区间建立逐行证据，不把排名成功等同于任意裁接都正确。

    valid[i][y] 表示帧 i 的该行没有目标卡落入未证明区间；cards 保留世界坐标、
    每帧已证明区间，用于最后逐卡核对输出映射。无目标卡的背景行不参与卡片计数。
    """
    cards = []
    for item in items:
        refs = item.get("screenshot_refs", [])
        if not refs or item.get("screenshot_covered") is not True:
            raise ValueError("document_scrollshot_target_proof_missing")
        intervals, geometry = {}, None
        for ref in refs:
            index = ref["frame_index"]
            if type(index) is not int or not 0 <= index < len(frames):
                raise ValueError("document_scrollshot_reference_invalid")
            x, y, w, h = ref["rect"]
            world = (x, y+offsets[index], w, h)
            if geometry is not None and any(abs(a-b) > .11 for a, b in zip(world, geometry)):
                raise ValueError("document_scrollshot_target_geometry_changed")
            geometry = geometry or world
            note = next((n for n in frames[index]["after"]["results"] if n.get("id") == item["id"]), None)
            if (not note or not note.get("images_loaded") or not note.get("cover_present")
                    or ref.get("path") != frames[index]["path"]
                    or ref.get("coverage") != note.get("coverage")
                    or ref["rect"] != note.get("rect")):
                raise ValueError("document_scrollshot_reference_not_bound")
            coverage = ref.get("coverage")
            if not coverage or not 0 <= coverage[0] < coverage[1] <= h+.051:
                raise ValueError("document_scrollshot_coverage_invalid")
            # 完整卡边界保留抗锯齿像素；内部裁切边界向已证明区间内收。
            start = (math.floor(world[1]) if coverage[0] == 0
                     else _raster_edge(world[1]+coverage[0], upper=True))
            end = (math.ceil(world[1]+h) if abs(coverage[1]-h) <= .051
                   else _raster_edge(world[1]+coverage[1], upper=False))
            intervals.setdefault(index, []).append((start, end))
        x, y, w, h = geometry
        box = [math.floor(x), math.floor(y), math.ceil(x+w), math.ceil(y+h)]
        if not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height:
            raise ValueError("document_scrollshot_target_outside_canvas")
        cards.append(dict(id=item["id"], rank=item.get("rank"), box=box, intervals=intervals))
    valid = []
    for index, offset in enumerate(offsets):
        rows = [offset+y < height for y in range(viewport_height)]
        for card in cards:
            intervals = card["intervals"].get(index, [])
            for world_y in range(max(offset, card["box"][1]), min(offset+viewport_height, card["box"][3])):
                if not any(start <= world_y < end for start, end in intervals):
                    rows[world_y-offset] = False
        valid.append(rows)
    return cards, valid


def _compose_document_scrollshot(frames, items, output, offsets, height, check):
    """文档滚动仅在原像素匹配带内接缝；先完成覆盖证明，再发布新图。

    首帧固定页头和两侧原像素各保留一次，后续只在目标卡列的原 x 坐标粘贴。
    侧栏下方为空白，不移动/重绘卡片，不读取或修改页面 CSS。
    """
    width, viewport_height = frames[0]["dimensions"]
    cards, valid = _document_coverage(frames, items, offsets, width, viewport_height, height)
    left = min(card["box"][0] for card in cards)
    right = max(card["box"][2] for card in cards)
    selected = list({offset: index for index, offset in enumerate(offsets)}.items())
    placements, seams, coverage = [], [], []
    with ExitStack() as stack:
        images = {}
        for offset, index in selected:
            check()
            frame = frames[index]
            scroll = frame["after"]["scroll"]
            if (frame["dimensions"] != [width, viewport_height] or scroll.get("host") != "document"
                    or scroll.get("rect") != [0, 0, width, viewport_height]
                    or scroll["top"] != offset):
                raise ValueError("document_scrollshot_viewport_changed")
            raw = Path(frame["path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != frame["sha256"]:
                raise ValueError("scrollshot_source_changed")
            with Image.open(BytesIO(raw)) as image:
                if image.size != (width, viewport_height) or image.format != "PNG":
                    raise ValueError("document_scrollshot_source_dimensions_changed")
                images[index] = image.convert("RGB")
                stack.callback(images[index].close)

        boundaries = [0]
        for (offset, index), (next_offset, next_index) in zip(selected, selected[1:]):
            check()
            start, end = next_offset, min(offset+viewport_height, height)
            if end-start < 2:
                raise ValueError("document_scrollshot_pixel_overlap_missing")
            before = images[index].crop((left, start-offset, right, end-offset)).tobytes()
            after = images[next_index].crop((left, start-next_offset, right, end-next_offset)).tobytes()
            stride = (right-left)*3
            matches = [before[y*stride:(y+1)*stride] == after[y*stride:(y+1)*stride]
                       for y in range(end-start)]
            bands, beginning = [], None
            for row, equal in enumerate(matches+[False]):
                if equal and beginning is None:
                    beginning = start+row
                elif not equal and beginning is not None:
                    bands.append((beginning, start+row))
                    beginning = None
            # 当前片段在第一个未证明行之前结束；下一片段也须从连续有效区间开始。
            limit = boundaries[-1]
            while limit < end and valid[index][limit-offset]:
                limit += 1
            reach = [next_offset+viewport_height] * (viewport_height+1)
            for row in range(viewport_height-1, -1, -1):
                reach[row] = reach[row+1] if valid[next_index][row] else next_offset+row
            choices = []
            for low, high in bands:
                for seam in range(max(low+1, boundaries[-1]+1), min(high-1, limit)+1):
                    if (valid[next_index][seam-next_offset]
                            and any(card["box"][1] < seam < card["box"][3] for card in cards)):
                        choices.append(((reach[seam-next_offset], high-low, -abs(seam-(low+high)/2)),
                                        seam, low, high))
            if not choices:
                raise ValueError("document_scrollshot_no_proven_pixel_seam")
            _, seam, low, high = max(choices)
            boundaries.append(seam)
            seams.append(dict(frame_indices=[index, next_index], world_y=seam,
                              matching_world_band=[low, high], matching_x=[left, right],
                              source_y=[seam-offset, seam-next_offset]))
        boundaries.append(height)
        for position, (offset, index) in enumerate(selected):
            start, end = boundaries[position:position+2]
            if (not offset <= start < end <= offset+viewport_height
                    or not all(valid[index][start-offset:end-offset])):
                raise ValueError("document_scrollshot_selected_target_coverage_gap")
            placements.append(dict(frame_index=index, source_path=frames[index]["path"],
                                   source_box=[0 if position == 0 else left, start-offset,
                                               width if position == 0 else right, end-offset],
                                   output_box=[0 if position == 0 else left, start,
                                               width if position == 0 else right, end]))
        # 首屏两侧补全至原视口底边，避免因内容接缝提前而裁掉底部菜单。
        first_index = selected[0][1]
        for x1, x2 in ((0, left), (right, width)):
            if x1 < x2 and boundaries[1] < min(height, viewport_height):
                box = [x1, boundaries[1], x2, min(height, viewport_height)]
                placements.append(dict(frame_index=first_index, source_path=frames[first_index]["path"],
                                       source_box=box, output_box=box.copy(), role="first_viewport_side_context"))
        # 输出逐卡区间清单：每个像素行必须恰由一个有效原帧区间提供。
        for card in cards:
            x1, top, x2, bottom = card["box"]
            segments, filled = [], top
            for position, (offset, index) in enumerate(selected):
                start, end = max(top, boundaries[position]), min(bottom, boundaries[position+1])
                if start < end:
                    if start != filled:
                        raise ValueError("document_scrollshot_target_output_gap")
                    segments.append(dict(frame_index=index, source_box=[x1, start-offset, x2, end-offset],
                                         output_box=[x1, start, x2, end]))
                    filled = end
            if filled != bottom:
                raise ValueError("document_scrollshot_target_output_gap")
            coverage.append(dict(id=card["id"], rank=card["rank"], output_box=card["box"], segments=segments))
        with Image.new("RGB", (width, height), "white") as canvas:
            for placement in placements:
                check()
                canvas.paste(images[placement["frame_index"]].crop(placement["source_box"]),
                             tuple(placement["output_box"][:2]))
            check()
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                canvas.save(stream, format="PNG")
    return dict(path=str(output.resolve()), width=width, height=height,
                sha256=hashlib.sha256(output.read_bytes()).hexdigest(), placements=placements,
                layout="continuous_native_scroll_column", captured_count=len(items),
                document_seams=seams, target_coverage=coverage, content_x=[left, right])


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
    if first["scroll"].get("host") == "document":
        return _compose_document_scrollshot(frames, items, output, offsets, height, check)
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
