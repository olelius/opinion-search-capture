"""原生搜索证据解析、独立卡片排名和 XML 锚点约束的无缩放长图。

模块不连接设备。平台标识缺失时只声明可见内容指纹，不伪造 BV/笔记 ID。
未识别模块、断序、图像不连续均保留为异常，由采集器返回 partial。
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
import hashlib
import re
from pathlib import Path
import statistics
import xml.etree.ElementTree as ET

from PIL import Image, ImageChops, ImageStat


PACKAGES = {"bilibili": "tv.danmaku.bili", "xiaohongshu": "com.xingin.xhs"}
RANK_POLICY = "independent_content_cards"
END_TEXTS = {"没有更多了", "没有更多内容了", "已经到底啦", "到底了", "没有找到相关内容，换个词试试吧", "没有找到相关视频", "暂无搜索结果"}
PROMOTION_TEXTS = {"广告", "赞助", "推广", "品牌合作"}
VIDEO_DURATION = re.compile(r"\d+:[0-5]\d(?::[0-5]\d)?")


class EvidenceError(ValueError):
    """证据不足以支持排名或连续拼接。"""


def bounds(node) -> list[int]:
    """读取 Android bounds；缺失和空矩形返回零矩形。"""
    values = [int(x) for x in re.findall(r"-?\d+", node.get("bounds", ""))]
    return values if len(values) == 4 else [0, 0, 0, 0]


def _rid(node) -> str:
    return node.get("resource-id", "").split("/")[-1]


def _text(node) -> str:
    return " ".join(node.get("text", "").split())


def _find(node, *ids):
    return next((n for n in node.iter("node") if _rid(n) in ids), None)


def _value(node, *ids) -> str:
    found = _find(node, *ids)
    return _text(found) if found is not None else ""


def _fingerprint(*parts) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _visible(box, viewport) -> bool:
    return box[2] > box[0] and box[3] > box[1] and box[3] > viewport[1] and box[1] < viewport[3]


def _item(platform, title, author, duration, node, cover, viewport, promoted=False):
    box = bounds(node)
    return dict(identity=_fingerprint(platform, title, author, duration),
                identity_kind="visible_title_author_duration", platform_content_id=None,
                title=title, author=author, duration=duration, promoted=promoted,
                bounds=box, cover_bounds=bounds(cover),
                # UI树会把被裁掉的卡片外框钳制到viewport顶边；等于边界不证明完整。
                complete=box[1] > viewport[1] and box[3] < viewport[3] and bounds(cover)[1] > viewport[1],
                content_kind="video" if platform == "bilibili" else "note")


def _note_structure(row, screen_w):
    """返回半宽笔记的正文容器及大封面；解析与页尾判断共用此结构证据。"""
    box = bounds(row)
    if not 0 < box[2] - box[0] < screen_w * .6:
        return None, None
    for body in row.iter("node"):
        covers = [n for n in body if n.get("class", "").endswith("ImageView") and
                  bounds(n)[2] - bounds(n)[0] > screen_w * .35]
        if covers:
            return body, covers[0]
    return None, None


def _note_text_fields(body, cover, author) -> tuple[str, str, bool]:
    """按几何分离正文、封面时长和独立促销标签；结构歧义返回空标题供 unknown 留证。"""
    fields = [n for n in body if n.get("class", "").endswith("TextView") and _text(n)]
    cb, ab = bounds(cover), bounds(author)
    titles = [n for n in fields if _text(n) not in PROMOTION_TEXTS and
              cb[0] <= bounds(n)[0] < bounds(n)[2] <= cb[2] and cb[3] <= bounds(n)[1] < bounds(n)[3] <= ab[1]]
    durations = {_text(n) for n in fields if VIDEO_DURATION.fullmatch(_text(n)) and
                 cb[0] <= bounds(n)[0] < bounds(n)[2] <= cb[2] and cb[1] <= bounds(n)[1] < bounds(n)[3] <= cb[3]}
    promoted = any(_text(n) in PROMOTION_TEXTS for n in fields)
    if len(titles) != 1 or len(durations) > 1:
        return "", "", promoted
    return _text(titles[0]), next(iter(durations), ""), promoted


def _shortcut_occlusions(nodes, container, viewport) -> list[dict]:
    """只识别列表外、底部紧凑可点击容器内的完整快捷方式提示，不以正文关键词猜浮层。"""
    content_nodes = set(container.iter("node"))
    width, height = viewport[2] - viewport[0], viewport[3] - viewport[1]
    found = []
    for node in nodes:
        box = bounds(node)
        if (node in content_nodes or not node.get("class", "").endswith("FrameLayout") or
                node.get("clickable") != "true" or box[2] - box[0] < width * .9 or
                not viewport[1] + height * .5 <= box[1] < viewport[3] or
                not 0 < box[3] - box[1] <= height * .25):
            continue
        fields = [n for n in node.iter("node") if n.get("class", "").endswith("TextView") and _text(n)]
        texts = [_text(n) for n in fields]
        if ("搜索一键直达" in texts and any(t.startswith("把小组件放进你的桌面") for t in texts) and
                any(_text(n) == "去添加" and n.get("clickable") == "true" for n in fields) and
                not any(o["bounds"] == box for o in found)):
            found.append(dict(reason="search_shortcut_prompt", bounds=box, texts=texts))
    return sorted(found, key=lambda o: (o["bounds"][1], o["bounds"][0]))


def xhs_query_nodes(root) -> list:
    """定位已知原生查询行，使用明确控件关系与有效APP视口，不依赖固定顶栏像素。

    只接受与返回图标、搜索按钮同父级且同高度的宽文本框；普通笔记文字不作入口。
    """
    nodes = [n for n in root.iter("node") if n.get("package") == PACKAGES["xiaohongshu"]]
    if not nodes:
        return []
    app_top = min(bounds(n)[1] for n in nodes)
    app_bottom = max(bounds(n)[3] for n in nodes)
    found = []
    for row in nodes:
        children = [n for n in row if n.get("package") == PACKAGES["xiaohongshu"]]
        back = [n for n in children if n.get("content-desc") == "返回"]
        search = [n for n in children if _text(n) == "搜索" and n.get("clickable") == "true"]
        if len(back) != 1 or len(search) != 1:
            continue
        rb, sb, bb = bounds(row), bounds(search[0]), bounds(back[0])
        if not (0 < rb[3] - rb[1] <= (app_bottom - app_top) * .2 and
                rb[1] < app_top + (app_bottom - app_top) * .2):
            continue
        for node in children:
            box = bounds(node)
            if (node.get("class") == "android.widget.TextView" and node.get("clickable") == "true"
                    and _text(node) not in ("", "搜索") and box[0] >= bb[2] and box[2] <= sb[0]
                    and box[2] - box[0] > (rb[2] - rb[0]) * .3
                    and max(box[1], sb[1]) < min(box[3], sb[3])):
                found.append(node)
    return found if len(found) == 1 else []


def parse_page(xml: str, platform: str, kind: str, keyword: str) -> dict:
    """解析一份原始 XML，输出条目、排除项、未知区域与明确结束证据。

    B站只识别综合结果容器的直接卡片，绝不递归把 UP 主聚合视频计数。
    小红书下拉仅计普通词行；账号/商品行保留 excluded，混淆资源名不参与语义。
    小红书结果缺少登录后真实结构基线时失败闭合，不用泛用节点数冒充笔记数。
    """
    if platform not in PACKAGES or kind not in ("results", "suggestions"):
        raise ValueError("invalid_parser_scope")
    root = ET.fromstring(xml)
    nodes = [n for n in root.iter("node") if n.get("package") == PACKAGES[platform]]
    screen_w = max((bounds(n)[2] for n in nodes), default=0)
    screen_h = max((bounds(n)[3] for n in nodes), default=0)
    out = dict(items=[], excluded=[], unknown=[], anchors=[], occlusions=[], start_verified=None, viewport=[0, 0, screen_w, screen_h],
               recognized=False, keyword_verified=False, scope_verified=False,
               end_marker=None, complete=False, platform=platform, capture_kind=kind)
    search_nodes = [n for n in nodes if n.get("class", "").endswith("EditText") or
                    _rid(n) in ("search_fake_text", "search_src_text")]
    if platform == "xiaohongshu" and not search_nodes:
        search_nodes = xhs_query_nodes(root)
    out["keyword_verified"] = any(_text(n).removeprefix("搜索, ") == keyword for n in search_nodes)
    container = None
    if platform == "bilibili":
        if kind == "suggestions":
            container = next((n for n in nodes if _rid(n) == "suggest_list"), None)
        else:
            holder = next((n for n in nodes if _rid(n) == "result_container"), None)
            if holder is not None:
                container = _find(holder, "recycler_view")
            tabs = [n for n in nodes if _rid(n) == "tab_title"]
            out["scope_verified"] = any(_text(n) == "综合" and n.get("selected") == "true" for n in tabs) if tabs else None
            overlay = next((n for n in nodes if _rid(n) == "suggest_list" and len(n)), None)
            if overlay is not None:
                out["unknown"].append(dict(reason="suggestion_overlay", bounds=bounds(overlay)))
    elif kind == "suggestions":
        candidates = [n for n in nodes if n.get("class", "").endswith("RecyclerView") and
                      any(c.get("class", "").endswith("Button") for c in n)]
        if len(candidates) == 1:
            container = candidates[0]
    else:
        candidates = [n for n in nodes if n.get("class", "").endswith("RecyclerView") and
                      bounds(n)[2] - bounds(n)[0] > screen_w * .9 and bounds(n)[3] - bounds(n)[1] > screen_h * .5]
        if len(candidates) == 1:
            container = candidates[0]
        general = any(_text(n) == "综合" and 130 <= bounds(n)[1] < 300 for n in nodes)
        all_tab = any(_text(n) == "全部" and 130 <= bounds(n)[1] < 210 for n in nodes)
        out["scope_verified"] = True if general and all_tab else (None if all_tab else False)
        out["reading_order"] = "visible_top_y_then_x"
    # 不以登录门后的空页当成成功的零条搜索结果。
    if container is None:
        marker = next((n for n in nodes if _text(n) in END_TEXTS), None)
        if kind == "results" and platform == "bilibili" and marker is not None and out["scope_verified"]:
            out.update(recognized=True, end_marker=dict(text=_text(marker), bounds=bounds(marker)))
            return out
        out["unknown"].append(dict(reason="result_structure_unverified", bounds=out["viewport"]))
        return out
    out["recognized"] = True
    out["viewport"] = bounds(container)
    if platform == "xiaohongshu" and kind == "results":
        # 结果RecyclerView延伸到状态栏背后；从原生固定页头/筛选栏推导可见内容上沿。
        headers = [bounds(n)[3] for n in nodes if _text(n) in ("全部", "问一问") and bounds(n)[1] < 210]
        bars = [bounds(n)[3] for n in nodes if n.get("class", "").endswith("RecyclerView") and
                130 <= bounds(n)[1] < 300 and bounds(n)[2] - bounds(n)[0] > screen_w * .9 and bounds(n)[3] - bounds(n)[1] < 120]
        out["viewport"][1] = max([210] + headers + bars)
        out["occlusions"] = _shortcut_occlusions(nodes, container, out["viewport"])
        if out["occlusions"]:
            # 只收缩证据/手势有效区域；源 XML、PNG 和卡片原始矩形保持原样。
            out["viewport"][3] = min(o["bounds"][1] for o in out["occlusions"])
    viewport = out["viewport"]
    for n in root:
        box = bounds(n)
        package = n.get("package", "")
        if package and package != PACKAGES[platform] and "inputmethod" in package and _visible(box, viewport):
            out["unknown"].append(dict(reason="keyboard_overlay", bounds=box))
    for node in container.iter("node"):
        box = bounds(node)
        if _text(node) and box[1] > viewport[1] + 2 and box[3] < viewport[3] - 2:
            out["anchors"].append(dict(identity=_fingerprint(_rid(node), _text(node)), bounds=box))
    rows = sorted(list(container), key=lambda n: (bounds(n)[1], bounds(n)[0]))
    if platform == "xiaohongshu" and kind == "results":
        # 原生列表头占位仅在列表起点出现；综合筛选栏本身悬浮，不能单独作为回顶成功证据。
        out["start_verified"] = any(bounds(n)[0] == 0 and bounds(n)[2] == screen_w and
                                     bounds(n)[1] <= 42 and bounds(n)[3] <= viewport[1] for n in rows)
    for native_order, row in enumerate(rows):
        box = bounds(row)
        if not _visible(box, viewport):
            continue
        texts = [_text(n) for n in row.iter("node") if _text(n)]
        marker = next((t for t in texts if t in END_TEXTS), None)
        body, cover = _note_structure(row, screen_w) if platform == "xiaohongshu" and kind == "results" else (None, None)
        has_content_structure = cover is not None if platform == "xiaohongshu" and kind == "results" else any(
            _rid(n) in ("cover", "title") for n in row.iter("node"))
        if marker and not has_content_structure:
            out["end_marker"] = dict(text=marker, bounds=box)
            continue
        if kind == "suggestions":
            if any("粉丝" in t or "笔记 " in t for t in texts):
                out["excluded"].append(dict(reason="account_suggestion", texts=texts, bounds=box))
                continue
            if any("件商品" in t for t in texts):
                out["excluded"].append(dict(reason="product_suggestion", texts=texts, bounds=box))
                continue
            if texts == ["反馈"]:
                out["end_marker"] = dict(text="反馈", bounds=box)
                continue
            title = _value(row, "title") if platform == "bilibili" else (texts[0] if len(texts) == 1 else "")
            if title:
                out["items"].append(dict(identity=_fingerprint(platform, "suggestion", title),
                    identity_kind="suggestion_text", title=title, bounds=box,
                    complete=box[1] >= viewport[1] and box[3] < viewport[3], promoted=False))
            elif texts:
                out["unknown"].append(dict(reason="unknown_suggestion", texts=texts, bounds=box))
            continue
        ids = {_rid(n) for n in row.iter("node")}
        if platform == "xiaohongshu":
            if "avatarLayout" in ids and any("粉丝" in t for t in texts):
                out["excluded"].append(dict(reason="account_aggregate", texts=texts, bounds=box))
                continue
            if any(t in ("大家都在搜", "相关搜索", "大家还在搜") for t in texts):
                out["excluded"].append(dict(reason="related_search", texts=texts, bounds=box))
                continue
            if "直播中" in texts:
                out["excluded"].append(dict(reason="live_room_not_note", texts=texts, bounds=box))
                continue
            # 9.31.2真实结构：直接可点击半宽FrameLayout→RelativeLayout→封面、正文、作者栏。
            author_nodes = [n for n in row.iter("node") if n.get("class", "").endswith("TextView") and n.get("clickable") == "true" and _text(n)]
            if not author_nodes and body is not None:
                for line in body:
                    fields = [n for n in line if n.get("class", "").endswith("TextView") and _text(n)]
                    if len(fields) == 2 and re.fullmatch(r"(?:\d{2}-\d{2}|\d{4}-\d{2}-\d{2}|\d+(?:分钟|小时|天|周|月|年)前|昨天|前天|刚刚)", _text(fields[1])):
                        author_nodes = [fields[0]]
                        break
            title, duration, promoted = _note_text_fields(body, cover, author_nodes[0]) if cover is not None and len(author_nodes) == 1 else ("", "", False)
            if title:
                author = _text(author_nodes[0])
                item = _item(platform, title, author, duration, row, cover, viewport, promoted)
                # 时长可能因封面裁切而缺席，促销标签也不属于笔记身份；延续原四段指纹格式。
                item.update(identity=_fingerprint(platform, title, author, ""),
                            identity_kind="visible_title_author", native_order=native_order)
                out["items"].append(item)
            elif box[3] <= viewport[1]:
                continue
            else:
                out["unknown"].append(dict(reason="unidentified_note_card", texts=texts, bounds=box,
                    cover_bounds=bounds(cover) if cover is not None else None,
                    native_order=native_order, edge=box[1] <= viewport[1] or box[3] >= viewport[3]))
            continue
        if "user_info" in ids or "up_title" in ids or "more_video" in ids:
            out["excluded"].append(dict(reason="account_aggregate", texts=texts, bounds=box))
            continue
        if any(t in ("相关搜索", "大家还在搜", "大家都在搜", "相关搜索词") for t in texts):
            out["excluded"].append(dict(reason="related_search", texts=texts, bounds=box))
            continue
        if "live_badge" in ids or "live_badge_title" in ids:
            out["excluded"].append(dict(reason="live_room_not_video", texts=texts, bounds=box))
            continue
        if "price_layout" in ids and "extra_tv" in ids and not ids & {"upuser", "upper", "duration"}:
            out["excluded"].append(dict(reason="product_ad_not_content", texts=texts, bounds=box, promoted=True))
            continue
        title = _value(row, "title")
        author = _value(row, "upuser", "upper")
        cover = _find(row, "cover")
        duration = _value(row, "duration")
        if _value(row, "cover_badge") in ("动态", "专栏") and not duration:
            out["excluded"].append(dict(reason="non_video_dynamic" if _value(row, "cover_badge") == "动态" else "non_video_article", texts=texts, bounds=box))
            continue
        promoted = bool(ids & {"ad_tag", "ad_tint_frame"}) or any(t in ("广告", "推广") for t in texts)
        if title and author and cover is not None and (duration or promoted):
            out["items"].append(_item(platform, title, author, duration, row, cover, viewport, promoted))
        else:
            out["unknown"].append(dict(reason="unidentified_content_card", texts=texts, bounds=box,
                                      edge=box[1] <= viewport[1] or box[3] >= viewport[3]))
    out["items"].sort(key=lambda i: (i["bounds"][1], i["bounds"][0]))
    if kind == "results":
        for item in out["items"]:
            if not item["complete"]:
                out["unknown"].append(dict(reason="incomplete_content_card", bounds=item["bounds"],
                                           identity=item["identity"], native_order=item.get("native_order"), edge=True))
    if kind == "suggestions":
        out["scope_verified"] = out["keyword_verified"]
        # 非可滚动列表且末行下有实际空白，或小红书明确反馈尾标；仅稳定双快照后用于完成判定。
        bottom = max((bounds(n)[3] for n in rows), default=viewport[3])
        room = viewport[3] - bottom >= 32
        tail = bool(out["end_marker"]) or (container.get("scrollable") == "false" and room)
        out["complete"] = bool(out["items"]) and tail and not out["unknown"] and all(i["complete"] for i in out["items"])
    return out


def page_signature(page: dict) -> tuple:
    """XML-截图-XML配对检查签名；忽略时钟等非内容节点。"""
    return (page["keyword_verified"], page["scope_verified"], tuple(page["viewport"]),
            tuple((i["identity"], tuple(i["bounds"]), i["complete"], i.get("duration", ""), i.get("promoted", False)) for i in page["items"]),
            repr(page["excluded"]), repr(page["unknown"]), repr(page["end_marker"]), repr(page.get("occlusions", [])))


def _mae(a, b) -> float:
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / len(a.getbands())


def match_frames(previous: dict, current: dict, check=lambda: None) -> dict:
    """用共同条目几何确定唯一位移，并逐带检验原分辨率 RGB 重叠。

    不依赖猜测手势位移，不缩放输出，不把滚动条/固定头部参与位移搜索。
    至少两个共同锚点；每个重叠横带限制像素误差，防白底稀释错位文字。
    """
    check()
    a, b = previous["page"], current["page"]
    a_counts, b_counts = Counter(i["identity"] for i in a["items"]), Counter(i["identity"] for i in b["items"])
    def interior(item, page):
        # 同时保护历史JSON中曾误记complete=True的贴边卡；只用未裁切锚点拟合位移。
        box, viewport = item["bounds"], page["viewport"]
        return item["complete"] and box[1] > viewport[1] and box[3] < viewport[3]
    amap = {i["identity"]: i for i in a["items"] if interior(i, a) and a_counts[i["identity"]] == 1}
    pairs = [(amap[i["identity"]], i) for i in b["items"] if interior(i, b) and b_counts[i["identity"]] == 1 and i["identity"] in amap]
    aa, bb = a.get("anchors", []), b.get("anchors", [])
    unique_a = {i["identity"]: i for i in aa if sum(j["identity"] == i["identity"] for j in aa) == 1}
    unique_b = [i for i in bb if sum(j["identity"] == i["identity"] for j in bb) == 1]
    native_pairs = [(unique_a[i["identity"]], i) for i in unique_b if i["identity"] in unique_a]
    stationary_pairs = pairs + native_pairs
    anchor_kind = "content_cards"
    if len(pairs) < 2:
        # 大聚合/相关搜索占屏时允许其原生文字充当几何锚点，但仍不参与内容计数。
        pairs = native_pairs
        anchor_kind = "unique_native_text_nodes"
        if len(pairs) < 2 or max(y["bounds"][1] for _, y in pairs) - min(y["bounds"][1] for _, y in pairs) < 80:
            raise EvidenceError("insufficient_shared_card_anchors")
    shifts = [x["bounds"][1] - y["bounds"][1] for x, y in pairs]
    shift = int(statistics.median(shifts))
    discarded_anchors = []
    if anchor_kind == "unique_native_text_nodes":
        # 同名作者/播放量可能恰在两个窗口各出现一次。采用多数位移共识，仍由整幅像素复核兜底。
        inliers = [(x, y) for x, y in pairs if abs(x["bounds"][1] - y["bounds"][1] - shift) <= 1]
        discarded_anchors = [x["identity"] for x, y in pairs if (x, y) not in inliers]
        if len(inliers) < 3 or len(inliers) / len(pairs) < .75:
            raise EvidenceError("ambiguous_native_text_anchors")
        pairs = inliers
        shifts = [x["bounds"][1] - y["bounds"][1] for x, y in pairs]
    # 所有共同证据严格静止才归类为未滚动；多数为零、横向重排或尺寸变化仍走原异常。
    # 在像素拼接前退出：提示消失可改变截图，但零位移既不是有效链接，也不是页尾。
    if all(x["bounds"] == y["bounds"] for x, y in stationary_pairs):
        raise EvidenceError("no_scroll_progress")
    if max(shifts) - min(shifts) > 1 or shift <= 0:
        raise EvidenceError("stationary_or_reordered_frames")
    with Image.open(previous["png_path"]) as ai, Image.open(current["png_path"]) as bi:
        if ai.size != bi.size:
            raise EvidenceError("frame_size_changed")
        top = max(a["viewport"][1], b["viewport"][1] + shift)
        bottom = min(a["viewport"][3], b["viewport"][3] + shift)
        if bottom - top < 96:
            raise EvidenceError("overlap_too_small")
        # 外沿12px仅用于误差计算时去除滚动条；实际输出保留原始全宽。
        left, right = 12, ai.width - 12
        rgb_a, rgb_b = ai.convert("RGB"), bi.convert("RGB")
        first = rgb_a.crop((left, top, right, bottom))
        second = rgb_b.crop((left, top - shift, right, bottom - shift))
        mean = _mae(first, second)
        bands = []
        for y in range(0, first.height, 48):
            check()
            box = (0, y, first.width, min(y + 48, first.height))
            bands.append(_mae(first.crop(box), second.crop(box)))
        texture = max(ImageStat.Stat(first).stddev)
        if mean > 1.5 or max(bands) > 6.0 or texture < 8:
            raise EvidenceError(f"pixel_overlap_mismatch: mean={mean:.3f}, band={max(bands):.3f}")
        # 锚点相邻1像素的错位也必须显著更差，防重复背景造成假连续。
        alternatives = []
        for delta in (-1, 1):
            if 0 <= top - shift + delta and bottom - shift + delta <= bi.height:
                other = rgb_b.crop((left, top - shift + delta, right, bottom - shift + delta))
                alternatives.append(_mae(first, other))
        margin = min(alternatives, default=mean) - mean
        if margin < .15:
            raise EvidenceError("ambiguous_pixel_shift")
    return dict(shift=shift, overlap=bottom - top, mean_error=mean, max_band_error=max(bands),
                neighbor_margin=margin, anchor_kind=anchor_kind, anchor_ids=[x["identity"] for x, _ in pairs],
                discarded_anchors=discarded_anchors)


def match_visual_frames(previous: dict, current: dict, check=lambda: None) -> dict:
    """视觉模式仅用滚动区域像素估算位移，不要求卡片身份或无障碍节点稳定。

    这是普通滚动截屏的几何对齐，不承担排名证明；找不到唯一重叠时保守报错。
    """
    check()
    with Image.open(previous["png_path"]) as first, Image.open(current["png_path"]) as second:
        if first.size != second.size:
            raise EvidenceError("frame_size_changed")
        pa, pb = previous["page"]["viewport"], current["page"]["viewport"]
        top_a, bottom_a = max(0, pa[1]), min(pa[3], first.height)
        top_b, bottom_b = max(0, pb[1]), min(pb[3], second.height)
        left, right = max(12, pa[0], pb[0]), min(first.width - 12, pa[2], pb[2])
        # 正常手势80%约留20%；页头收起及实际手势偏差另由唯一像素重叠验证。
        # 10%只限定搜索下界，不把预期位移直接当作实际位移，保留96px纹理门槛。
        min_overlap = max(96, min(bottom_a - top_a, bottom_b - top_b) // 10)
        max_shift = bottom_a - top_b - min_overlap
        if right <= left or min(bottom_a - top_a, bottom_b - top_b) < min_overlap or max_shift < 0:
            raise EvidenceError("visual_overlap_too_small")
        # 只压缩横轴，纵轴保留原始像素。分别把不同高度视口缩成128行会改变比例，
        # 且多个实际位移落在同一整数行，造成虚假的“同分歧义”。页头收起时仍按
        # 屏幕绝对坐标求交集：上一帧y对应下一帧y-shift，不按裁剪后相对坐标匹配。
        sample_width = min(80, right - left)
        a = first.convert("L").crop((left, 0, right, first.height)).resize((sample_width, first.height), Image.Resampling.BOX)
        b = second.convert("L").crop((left, 0, right, second.height)).resize((sample_width, second.height), Image.Resampling.BOX)
        scores = []
        for shift in range(max_shift + 1):
            check()
            top, bottom = max(top_a, top_b + shift), min(bottom_a, bottom_b + shift)
            if bottom - top < min_overlap:
                continue
            scores.append((_mae(a.crop((0, top, sample_width, bottom)),
                                b.crop((0, top - shift, sample_width, bottom - shift))), shift))
        scores.sort()
        if not scores:
            raise EvidenceError("visual_overlap_not_found")
        error, shift = scores[0]
        runner_up = next((score for score, candidate in scores[1:] if abs(candidate - shift) >= 8), None)
        neighbor = min((score for score, candidate in scores if abs(candidate - shift) == 1), default=None)
        actual_top = max(top_a, top_b + shift)
        actual_bottom = min(bottom_a, bottom_b + shift)
        overlap_a = a.crop((0, actual_top, sample_width, actual_bottom))
        overlap_b = b.crop((0, actual_top - shift, sample_width, actual_bottom - shift))
        if (error > 24 or ImageStat.Stat(overlap_a).stddev[0] < 8
                or (runner_up is not None and runner_up - error < 0.15)
                or (neighbor is not None and neighbor - error <= 1e-6)):
            raise EvidenceError("visual_overlap_ambiguous")
        if shift == 0:
            raise EvidenceError("no_scroll_progress")
        bands = []
        for y in range(0, overlap_a.height, 48):
            check()
            box = (0, y, sample_width, min(y + 48, overlap_a.height))
            bands.append(_mae(overlap_a.crop(box), overlap_b.crop(box)))
        return dict(shift=shift, overlap=actual_bottom - actual_top, mean_error=error,
                    max_band_error=max(bands), neighbor_margin=None if neighbor is None else neighbor - error,
                    anchor_kind="visual_pixels",
                    anchor_ids=[], discarded_anchors=[])


class RankLedger:
    """累积独立完整卡片；记录顺序、重复出现和源帧映射，不给未知区域放行。"""

    def __init__(self, top_n: int, *, strict: bool = True):
        self.top_n = top_n
        self.strict = strict
        self.items = []
        self.excluded = []
        self.unknown = []
        self.frames = []
        self.links = []

    @staticmethod
    def _cover_hash(visible: dict, frame: dict) -> str | None:
        """完整封面像素作为可见指纹的辅助身份凭据；不以截图文件名代替内容身份。"""
        box = visible.get("cover_bounds")
        if not box or not Path(frame["png_path"]).is_file():
            return None
        with Image.open(frame["png_path"]) as source:
            if not (0 <= box[0] < box[2] <= source.width and 0 <= box[1] < box[3] <= source.height):
                raise EvidenceError("invalid_cover_bounds")
            return hashlib.sha256(source.crop(tuple(box)).convert("RGB").tobytes()).hexdigest()

    def add(self, frame: dict, check=lambda: None):
        """整帧原子纳入排名；任何一张卡失败都不泄漏该帧先处理的条目或链接。"""
        previous = (self.items, self.excluded, self.unknown, self.frames, self.links)
        frame_state = {key: (key in frame, frame.get(key)) for key in ("offset", "accepted")}
        self.items = deepcopy(self.items)
        self.excluded, self.unknown = list(self.excluded), list(self.unknown)
        self.frames, self.links = list(self.frames), list(self.links)
        try:
            self._add_validated(frame, check)
        except BaseException:
            self.items, self.excluded, self.unknown, self.frames, self.links = previous
            for key, (existed, value) in frame_state.items():
                if existed:
                    frame[key] = value
                else:
                    frame.pop(key, None)
            raise

    def _linked_note_coverage(self, visible: dict, frame: dict, check) -> dict | None:
        """视觉模式补证跨帧笔记：首部结构、尾部身份及同列原像素重叠必须同时成立。

        仅处理相邻两帧中被上下边裁切的同一笔记，不推测缺失身份，不跨未接受帧补图。
        返回完整内容矩形及两个真实可见源矩形；原始page/items保持原样。
        """
        if self.strict or not self.frames or frame["page"].get("platform") != "xiaohongshu":
            return None
        previous = self.frames[-1]
        vp, old_vp = frame["page"]["viewport"], previous["page"]["viewport"]
        box, cover = visible["bounds"], visible.get("cover_bounds")
        if (not cover or not visible.get("title") or not visible.get("author") or
                box[1] > vp[1] or not vp[1] < box[3] < vp[3] or cover[3] <= vp[1]):
            return None
        offset, old_offset = frame["offset"], previous["offset"]
        candidates = []
        for row in previous["page"]["unknown"]:
            rb, cb = row["bounds"], row.get("cover_bounds")
            if row["reason"] == "incomplete_content_card":
                # 前帧已读到身份但卡片尾部被裁切：只有同一身份、同一原生矩形才补证。
                matches = [item for item in previous["page"]["items"]
                           if item["identity"] == visible["identity"] == row.get("identity") and item["bounds"] == rb]
                cb = matches[0].get("cover_bounds") if len(matches) == 1 else None
            if (row["reason"] not in ("unidentified_note_card", "incomplete_content_card") or not cb or
                    (rb[0], rb[2]) != (box[0], box[2]) or
                    (cb[0], cb[2]) != (cover[0], cover[2]) or
                    not old_vp[1] < rb[1] <= cb[1] < old_vp[3] <= rb[3]):
                continue
            start = max(rb[1] + old_offset, vp[1] + offset)
            end = min(old_vp[3] + old_offset, cover[3] + offset)
            if end - start >= 96 and rb[1] + old_offset < vp[1] + offset:
                candidates.append((row, start, end))
        if len(candidates) != 1:
            return None
        row, start, end = candidates[0]
        check()
        # 全屏位移正确之外，还要复核本卡片重叠，防另一列的相似画面替该卡片作证。
        with Image.open(previous["png_path"]) as first, Image.open(frame["png_path"]) as second:
            left, right = cover[0], cover[2]
            a = first.crop((left, start - old_offset, right, end - old_offset)).convert("RGB")
            b = second.crop((left, start - offset, right, end - offset)).convert("RGB")
            error = _mae(a, b)
            bands = [_mae(a.crop((0, y, a.width, min(y + 48, a.height))),
                          b.crop((0, y, b.width, min(y + 48, b.height)))) for y in range(0, a.height, 48)]
            if error > 1.5 or max(bands) > 6 or max(ImageStat.Stat(a).stddev) < 8:
                raise EvidenceError("partial_note_overlap_unverified")
        mappings = []
        for source, rect, sequence in ((previous, row["bounds"], len(self.frames) - 1),
                                       (frame, box, len(self.frames))):
            source_vp = source["page"]["viewport"]
            clipped = [rect[0], max(rect[1], source_vp[1]), rect[2], min(rect[3], source_vp[3])]
            mappings.append(dict(frame_index=source.get("source_index", sequence), sequence_index=sequence,
                png_path=source["png_path"], xml_path=source["xml_path"], bounds=clipped,
                content_bounds=[clipped[0], clipped[1] + source["offset"], clipped[2], clipped[3] + source["offset"]]))
        return dict(kind="linked_native_note_fragments", content_bounds=[box[0], row["bounds"][1] + old_offset, box[2], box[3] + offset],
                    source_mappings=mappings, overlap=end - start, mean_error=error, max_band_error=max(bands))

    def _add_validated(self, frame: dict, check=lambda: None):
        """内部暂存状态推进；由add负责提交/回滚，不能直接从采集器调用。"""
        page = frame["page"]
        scope_ok = page["scope_verified"] is True or (bool(self.frames) and page["scope_verified"] is None)
        if not page["recognized"] or not page["keyword_verified"] or not scope_ok:
            raise EvidenceError("search_scope_or_keyword_unverified")
        if not self.frames and any(i["bounds"][1] < page["viewport"][1] for i in page["items"]):
            raise EvidenceError("initial_search_not_at_top")
        if not self.frames and page.get("start_verified") is False:
            raise EvidenceError("initial_search_start_marker_missing")
        offset = 0
        if self.frames:
            link = match_frames(self.frames[-1], frame, check) if self.strict else match_visual_frames(self.frames[-1], frame, check)
            offset = self.frames[-1]["offset"] + link["shift"]
            self.links.append(dict(link, previous_frame=self.frames[-1].get("source_index", len(self.frames) - 1),
                                   current_frame=frame.get("source_index", len(self.frames))))
        frame["offset"] = offset
        index = len(self.frames)
        by_id = {i["identity"]: i for i in self.items}
        previous_rank = 0
        candidates = [(visible, self._linked_note_coverage(visible, frame, check)
                       if not visible["complete"] and visible["identity"] not in by_id else None)
                      for visible in page["items"]]
        candidates.sort(key=lambda pair: ((pair[1]["content_bounds"][1] - offset) if pair[1] else pair[0]["bounds"][1], pair[0]["bounds"][0]))
        for visible, coverage in candidates:
            if not visible["complete"] and not coverage:
                continue
            if page.get("reading_order") == "visible_top_y_then_x":
                barriers = [r for r in page["unknown"] if r["bounds"][3] >= page["viewport"][3] and r.get("identity") not in by_id]
                if any((r["bounds"][1], r["bounds"][0]) < (visible["bounds"][1], visible["bounds"][0]) for r in barriers):
                    break
            identity = visible["identity"]
            item = by_id.get(identity)
            cover_hash = self._cover_hash(visible, frame) if self.strict else None
            mapping = dict(frame_index=frame.get("source_index", index), sequence_index=index,
                           png_path=frame["png_path"], xml_path=frame["xml_path"],
                           bounds=visible["bounds"], content_bounds=[visible["bounds"][0], visible["bounds"][1] + offset,
                           visible["bounds"][2], visible["bounds"][3] + offset])
            content_bounds = coverage["content_bounds"] if coverage else mapping["content_bounds"]
            if item is not None:
                at_new_position = all(abs(m["content_bounds"][1] - mapping["content_bounds"][1]) > 1 for m in item["source_mappings"])
                if at_new_position:
                    if self.strict and (not cover_hash or cover_hash != item.get("cover_sha256")):
                        raise EvidenceError("identity_collision_or_unverified_duplicate")
                    mapping["duplicate_occurrence"] = True
                    item["source_mappings"].append(mapping)
                    # 重复卡只增加出现记录，排名和独立条数保持首次出现位置。
                    continue
                if item["rank"] < previous_rank:
                    if not any(m.get("duplicate_occurrence") and abs(m["content_bounds"][1] - mapping["content_bounds"][1]) <= 1 for m in item["source_mappings"]):
                        raise EvidenceError("rank_order_changed")
                previous_rank = item["rank"]
                item["source_mappings"].append(mapping)
                continue
            if self.items and content_bounds[1] < self.items[-1]["content_bounds"][1]:
                raise EvidenceError("new_item_inserted_before_prior_rank")
            item = deepcopy(visible)
            item.update(rank=len(self.items) + 1, source_mappings=coverage["source_mappings"] if coverage else [mapping],
                        content_bounds=content_bounds, cover_sha256=cover_hash)
            if coverage:
                item["coverage_evidence"] = coverage
            self.items.append(item)
            by_id[identity] = item
            previous_rank = item["rank"]
        for key in ("excluded", "unknown"):
            for row in page[key]:
                getattr(self, key).append(dict(row, frame_index=frame.get("source_index", index), offset=offset))
        frame["accepted"] = True
        self.frames.append(frame)

    def target_ready(self) -> bool:
        """恰好前N条完整可见且目标边界前没有未识别内容。"""
        return len(self.items) >= self.top_n and self.boundary_clear(self.top_n)

    def boundary_clear(self, count: int) -> bool:
        """已存在连续帧且前N边界没有未解释区域。"""
        return bool(self.frames) and not self.unresolved_regions(count)

    def unresolved_regions(self, count: int) -> list[dict]:
        """返回仍缺证的原生区域，供完成校验及已滚过缺口的及时停止共用。"""
        if not self.frames:
            return []
        # 两种模式都保留TOP N几何边界，视觉拼接成功不等于未知卡片可被跳过。
        visual = self.frames[0]["page"].get("reading_order") == "visible_top_y_then_x"
        cutoff = (self.items[count - 1]["content_bounds"][1] if visual else max(i["content_bounds"][3] for i in self.items[:count])) if count else float("inf")
        unresolved = []
        for row in self.unknown:
            top, bottom = row["bounds"][1] + row["offset"], row["bounds"][3] + row["offset"]
            if top > cutoff or (top == cutoff and (not count or not visual or row["bounds"][0] > self.items[count - 1]["content_bounds"][0])):
                continue
            # 被屏幕裁切的未知片段仅在另一完整卡片或已分类模块覆盖时被解释。
            classified = [m["content_bounds"] for i in self.items for m in i["source_mappings"]]
            classified += [i["content_bounds"] for i in self.items if i.get("coverage_evidence")]
            classified += [[e["bounds"][0], e["bounds"][1] + e["offset"], e["bounds"][2], e["bounds"][3] + e["offset"]] for e in self.excluded]
            if row.get("edge") and any(b[0] <= row["bounds"][0] and b[2] >= row["bounds"][2] and b[1] <= top + 1 and b[3] >= bottom - 1 for b in classified):
                continue
            unresolved.append(row)
        return unresolved


def render_long(ledger: RankLedger, output: Path, count: int, check=lambda: None) -> dict:
    """按已验证位移拼接到第N条底边，保留首屏页头与聚合模块；源帧保持原样。

    每段记录源矩形和目标矩形，items中的目标坐标可直接映射导出图。
    只裁切严格N边界后内容，不重排/抠出卡片，不在原截图上盖编号。
    """
    check()
    if not ledger.boundary_clear(count):
        raise EvidenceError("unknown_card_before_top_n_boundary")
    frames = ledger.frames
    if not frames:
        raise EvidenceError("no_frames")
    width = frames[0]["page"]["viewport"][2]
    height = max(i["content_bounds"][3] for i in ledger.items[:count]) if count else frames[-1]["page"]["viewport"][3] + frames[-1]["offset"]
    if count < ledger.top_n and frames[-1]["page"].get("end_marker"):
        height = max(height if count else 0, frames[-1]["page"]["end_marker"]["bounds"][3] + frames[-1]["offset"])
    if height <= 0 or width * height > 100_000_000:
        raise EvidenceError("long_image_pixel_budget_exceeded")
    placements = []
    with Image.new("RGB", (width, height)) as canvas:
        filled = 0
        for index, frame in enumerate(frames):
            check()
            offset = frame["offset"]
            start = 0 if index == 0 else max(frame["page"]["viewport"][1], filled - offset)
            end = min(frame["page"]["viewport"][3], height - offset)
            if end <= start:
                continue
            if offset + start != filled:
                raise EvidenceError("uncovered_stitch_region")
            with Image.open(frame["png_path"]) as source:
                if source.format != "PNG" or source.width != width:
                    raise EvidenceError("invalid_source_png")
                fragment = source.crop((0, start, width, end)).convert("RGB")
                canvas.paste(fragment, (0, offset + start))
            placements.append(dict(frame_index=frame.get("source_index", index), sequence_index=index, source_path=frame["png_path"],
                source_box=[0, start, width, end], output_box=[0, offset + start, width, offset + end]))
            filled = offset + end
        if filled != height:
            raise EvidenceError("incomplete_output_boundary")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            canvas.save(stream, format="PNG")
    with Image.open(output) as verify:
        verify.verify()
    check()
    incidental = [i["rank"] for i in ledger.items[count:] if i["content_bounds"][3] <= height]
    partial = [i["rank"] for i in ledger.items[count:] if i["content_bounds"][1] < height < i["content_bounds"][3]]
    fragments = []
    for row in ledger.unknown:
        top, bottom = row["bounds"][1] + row["offset"], row["bounds"][3] + row["offset"]
        if top < height < bottom and not any(i["identity"] == row.get("identity") for i in ledger.items):
            fragments.append(dict(rank=None, identity=row.get("identity"), frame_index=row["frame_index"],
                                  bounds=[row["bounds"][0], top, row["bounds"][2], bottom], reason="incidental_identity_or_rank_unverified"))
    duplicates = [dict(rank=i["rank"], **m) for i in ledger.items for m in i["source_mappings"]
                  if m.get("duplicate_occurrence") and m["content_bounds"][1] < height]
    return dict(path=str(output.resolve()), width=width, height=height, placements=placements,
                sha256=hashlib.sha256(output.read_bytes()).hexdigest(), links=ledger.links,
                target_ranks=list(range(1, count + 1)), incidental_ranks=incidental,
                partially_visible_ranks=partial, partially_visible_fragments=fragments,
                duplicate_occurrences=duplicates,
                layout="continuous_native_rectangle", rank_order="visible_top_y_then_x",
                boundary=dict(count=count, bottom=height, policy=RANK_POLICY))
