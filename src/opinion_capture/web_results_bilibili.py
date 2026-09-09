"""B站综合页独立视频 TOP N：只读 DOM，原生滚动/翻页，不删改结果布局。"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .progress import phase


RANK_POLICY = {
    "version": "bilibili-comprehensive-independent-video-v1",
    "scope": "综合搜索；当前会话、关键词及采集时段，不代表跨会话固定排名",
    "included": ["独立视频卡", "推广独立内容卡（保留原网页标识及promoted字段）"],
    "excluded": ["账号聚合及其内嵌视频", "相关搜索", "直播", "课堂课程合集", "非视频模块"],
    "order": "页码递增，每页可见独立卡DOM顺序；相同稳定身份只计首次出现",
    "boundary": "完整宽度自页顶截至目标卡底部；邻卡保留，另记完整/局部有效rank范围",
    "composition": "原图不可覆盖；逐页原像素矩形裁切后直接纵向拼接，证据说明只保存在JSON",
}

# 不写样式、不隐藏节点。屏幕外的 unobscured 为 null，必须滚动到现场检查。
RESULT_SNAPSHOT_JS = r"""() => {
  const visible = e => {
    if(!e || !e.getClientRects().length) return false;
    for(let n=e;n;n=n.parentElement) {
      const s=getComputedStyle(n);
      if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
    }
    return true;
  };
  const nodes=s=>[...document.querySelectorAll(s)].filter(visible);
  const rect=e=>{const r=e.getBoundingClientRect();return {
    x:Math.round((r.x+scrollX)*100)/100,y:Math.round((r.y+scrollY)*100)/100,
    width:Math.round(r.width*100)/100,height:Math.round(r.height*100)/100};};
  const clear=e=>{
    if(!visible(e)) return false;
    const r=e.getBoundingClientRect();
    const l=Math.max(0,r.left),t=Math.max(0,r.top),rr=Math.min(innerWidth,r.right),b=Math.min(innerHeight,r.bottom);
    if(rr<=l||b<=t) return null;
    const clearPart=[.04,.25,.5,.75,.96].every(x=>[.08,.35,.65,.92].every(y=>{
      const t=document.elementFromPoint(l+(rr-l)*x,Math.max(0,r.top)+(b-Math.max(0,r.top))*y);
      return !!t&&(e===t||e.contains(t));
    }));
    if(!clearPart) return false;
    return r.left<0||r.top<0||r.right>innerWidth||r.bottom>innerHeight ? null : true;
  };
  const excluded=[], unknown=[];
  const items=nodes('.search-page .bili-video-card').flatMap((e,index)=>{
    const module=e.closest('.search-all-list');
    const aggregate=e.closest('.user-list,.user,.user-card,.bili-user-card,.up-user,.related-search,.search-recommend,.live-list');
    const info={dom_index:index+1,module:module?module.className:'',rect:rect(e)};
    if(aggregate || (module && !module.matches('.video,.brand-ad-list'))) {
      excluded.push({...info,reason:'aggregate_or_non_video_module'}); return [];
    }
    if(!e.closest('.video-list') && !module?.matches('.brand-ad-list')) {
      unknown.push({...info,reason:'unknown_card_container'}); return [];
    }
    const title=e.querySelector('.bili-video-card__info--tit');
    const a=e.querySelector('a[href*="/video/"]') || title?.closest('a') || e.querySelector('a[href]');
    if(a && new URL(a.href).hostname==='live.bilibili.com') {
      excluded.push({...info,href:a.href,reason:'live_room_not_video'}); return [];
    }
    // 不根据标题里出现“推广”二字判定广告。
    const marker=[...e.querySelectorAll('[class*="ad-creative"],[class*="ad-tag"],[class*="ad-mark"],.bili-video-card__stats--ad')].filter(visible);
    const imgs=[...e.querySelectorAll('picture img,.bili-video-card__image img')].filter(visible);
    return [{...info,href:a?a.href:'',target_url:a?.getAttribute('data-target-url')||'',title:title?title.textContent.trim():'',
      promoted:marker.length>0 || (!!a && new URL(a.href).hostname==='cm.bilibili.com'), promotion_labels:marker.map(n=>n.textContent.trim()||n.getAttribute('class')),
      images_loaded:imgs.length>0&&imgs.every(i=>i.complete&&i.naturalWidth>0),
      images:imgs.map(i=>({src:i.currentSrc||i.src,width:i.naturalWidth,height:i.naturalHeight})),
      unobscured:clear(e),css_order:getComputedStyle(e.parentElement).order,
      card_css_order:getComputedStyle(e).order}];
  });
  const input=nodes('input.search-input-el, input#search-input')[0];
  const pager=nodes('.vui_pagenation')[0];
  const next=pager&&[...pager.querySelectorAll('button,a')].find(e=>e.textContent.trim()==='下一页');
  const active=pager?.querySelector('.vui_button--active,[aria-current="page"]');
  const noResults=nodes('.search-nodata,.search-empty,.empty-content,.vui_empty,.search-result-empty')
    .map(e=>e.innerText.trim()).filter(t=>/没有|未找到|暂无|无搜索/.test(t));
  const endings=nodes('.no-more,.load-end,.no-more-text').map(e=>e.innerText.trim())
    .filter(t=>/没有更多|已经到底|已到底|全部加载/.test(t));
  return {url:location.href,title:document.title,body:document.body.innerText.slice(0,20000),
    input_value:input?input.value:null,input_rect:input?rect(input):null,input_unobscured:clear(input)===true,
    challenge_visible:nodes('.geetest_panel,.geetest_window,.geetest_holder,[class*="captcha-container"],iframe[src*="captcha"]').length>0,
    login_required_visible:nodes('.bili-mini-mask,.bili-mini-login,.login-dialog').length>0,
    items,excluded,unknown,fonts_loaded:!document.fonts||document.fonts.status==='loaded',
    ordinary_overlays:nodes('.login-tip,.login-panel-popover').map(e=>({class_name:e.className,rect:rect(e)})),
    next_present:!!next,next_disabled:!!next&&(next.disabled||next.getAttribute('aria-disabled')==='true'||next.classList.contains('vui_button--disabled')),
    page_number:active?Number(active.textContent.trim()):null,no_results:noResults,end_markers:endings,
    viewport:{width:innerWidth,height:innerHeight},scroll_y:scrollY,
    document_size:{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight}};
}"""


def stable_identity(item: dict) -> str | None:
    """视频身份优先取BV/av；推广无视频链接时取DOM链接中的creative_id，不拿位置当身份。"""
    href = item.get("href", "")
    parsed = urlsplit(href)
    if parsed.scheme not in ("http", "https"):
        return None
    candidates = [item.get("target_url", ""), href]
    query = parse_qs(parsed.query)
    for key in ("url", "target_url", "jump_url", "redirect_url"):
        candidates.extend(query.get(key, []))
    for candidate in candidates:
        target = urlsplit(unquote(candidate))
        if target.hostname in ("www.bilibili.com", "m.bilibili.com", "bilibili.com"):
            match = re.search(r"/video/(BV[0-9A-Za-z]+|av[0-9]+)(?:/|$)", target.path)
            if match:
                return match[1]
    if item.get("promoted"):
        for candidate in candidates:
            target = urlsplit(candidate)
            if target.hostname not in ("cm.bilibili.com", "www.bilibili.com"):
                continue
            for key in ("creative_id", "ad_id"):
                value = parse_qs(target.query).get(key, [""])[0]
                if re.fullmatch(r"[0-9]+", value):
                    return f"promotion:{key}:{value}"
    return None


def normalize_snapshot(scene: dict) -> dict:
    """保留全部独立卡出现位置；身份缺失显式阻止排名确认，不静默漏卡。"""
    independent = []
    for item in scene["items"]:
        target = urlsplit(item.get("href", ""))
        if (target.hostname in ("www.bilibili.com", "m.bilibili.com", "bilibili.com")
                and re.fullmatch(r"/cheese/play/ss[0-9]+/?", target.path)):
            # 课堂ss链接是课程合集入口，不是独立BV/av视频。保留原画面和排除明细。
            scene.setdefault("excluded", []).append({**item, "reason":"course_collection_not_independent_video"})
            continue
        item["id"] = stable_identity(item)
        item["identity_kind"] = "promotion" if str(item["id"]).startswith("promotion:") else "video"
        independent.append(item)
    scene["items"] = independent
    return scene


def layout_fingerprint(scene: dict, target_indices: set[int] | None = None) -> str:
    """全列表身份/顺序及目标布局指纹；无关页尾尺寸、排除模块几何不参与。"""
    keys = ("url", "input_value", "challenge_visible", "login_required_visible", "viewport",
            "page_number", "next_present", "next_disabled", "no_results", "end_markers", "fonts_loaded")
    value = {key: scene.get(key) for key in keys}
    value["unknown"] = [{k: i.get(k) for k in ("dom_index", "reason", "module")} for i in scene.get("unknown", [])]
    value["items"] = []
    for item in scene.get("items", []):
        fields = ("dom_index", "id", "title", "href", "target_url", "css_order", "card_css_order", "promoted")
        if target_indices is None or item.get("dom_index") in target_indices:
            fields += ("rect", "images", "images_loaded")
        value["items"].append({k: item.get(k) for k in fields})
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def identity_order_failures(scene: dict) -> list[str]:
    """列出阻止排名证明的具体卡/容器，不静默丢弃未知条目或重排内容。"""
    failures = [f"unknown_card:{i.get('dom_index')}:{i.get('reason', 'unknown')}:{i.get('module', '')}"
                for i in scene.get("unknown", [])]
    for item in scene.get("items", []):
        index = item.get("dom_index")
        for field in ("id", "title"):
            if not item.get(field):
                failures.append(f"card_{field}_missing:{index}")
        for field in ("css_order", "card_css_order"):
            if str(item.get(field, "0")) != "0":
                failures.append(f"card_css_order_unverified:{index}:{field}={item.get(field)}")
    return failures


_TILE_VISIBILITY_JS = r"""({indices, top, bottom, width}) => {
  const visible=e=>{
    if(!e.getClientRects().length) return false;
    for(let n=e;n;n=n.parentElement) {
      const s=getComputedStyle(n);
      if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
    }
    return true;
  };
  const nodes=[...document.querySelectorAll('.search-page .bili-video-card')].filter(visible);
  return indices.filter(index=>{
    const e=nodes[index-1]; if(!e) return true;
    const r=e.getBoundingClientRect(), t=Math.max(top,r.top), b=Math.min(bottom,r.bottom);
    if(b<=t) return false;
    if(r.left<0||r.right>width) return true;
    return ![.04,.25,.5,.75,.96].every(x=>[.04,.25,.5,.75,.96].every(y=>{
      const hit=document.elementFromPoint(r.left+r.width*x,t+(b-t)*y);
      return !!hit&&(hit===e||e.contains(hit));
    }));
  });
}"""


def capture_viewport_region(page, scene: dict, crop: dict, targets: list[dict], run: Path,
                            page_index: int, attempt: int, budget, read) -> tuple[dict, list[dict], list[str]]:
    """只截原视口并以原像素组装页顶目标区域，避免 full_page 改变原生滚动条布局。

    每帧保存不可变原PNG、前后DOM和连续裁切区；实际导出区单独做遮挡检查，
    不把滚动时位于丢弃重叠区的固定页头误当目标遮挡，也不把遮挡像素拼入成品。
    """
    from PIL import Image
    width, bottom = crop["box"][2:]
    if width > scene["viewport"]["width"] or width * bottom > 80_000_000:
        raise ValueError("capture_region_exceeds_viewport_width_or_pixel_budget")
    indices = {i["dom_index"] for i in targets}
    signature = layout_fingerprint(scene, indices)
    frames, failures, covered = [], [], 0
    canvas = Image.new("RGB", (width, bottom), "white")
    try:
        for number in range(1, 121):
            budget()
            y = 0 if not covered else max(0, covered - scene["viewport"]["height"] * .45)
            page.evaluate("y => window.scrollTo(0,y)", y)
            budget.pause(page, 150)
            before = read()
            scroll = round(before["scroll_y"])
            end = min(bottom, scroll + before["viewport"]["height"])
            if scroll > covered or end <= covered:
                raise ValueError(f"viewport_strip_gap_or_no_progress:{covered}:{scroll}:{end}")
            box = [0, covered - scroll, width, end - scroll]
            args = {"indices": sorted(indices), "top": box[1], "bottom": box[3], "width": width}
            hidden_before = page.evaluate(_TILE_VISIBILITY_JS, args)
            # 首帧沿用旧版本化原图名；后续帧单列，不覆盖旧图或冒充整页原图。
            name = (f"page-{page_index:03d}-original-v{attempt}.png" if number == 1 else
                    f"page-{page_index:03d}-f{number:03d}-v{attempt}.png")
            path = run / name
            phase("screenshot", "B站：保存目标区域原始视口帧")
            png = page.screenshot(full_page=False, scale="css", timeout=budget(6000))
            with path.open("xb") as stream:
                stream.write(png)
            after = read()
            phase("validate_capture", "B站：核验视口帧身份、布局与遮挡")
            hidden_after = page.evaluate(_TILE_VISIBILITY_JS, args)
            source = png_details(path, (before["viewport"]["width"], before["viewport"]["height"]))
            frame_failures = []
            if signature != layout_fingerprint(before, indices) or signature != layout_fingerprint(after, indices):
                frame_failures.append(f"frame_{number}:target_identity_order_or_layout_changed")
            if before["scroll_y"] != after["scroll_y"]:
                frame_failures.append(f"frame_{number}:scroll_changed_during_capture")
            for index in sorted(set(hidden_before + hidden_after)):
                frame_failures.append(f"frame_{number}:target_partly_obscured:{index}")
            if number == 1 and (not before.get("input_unobscured") or not after.get("input_unobscured")):
                frame_failures.append("frame_1:keyword_input_partly_obscured")
            with Image.open(path) as original:
                region = original.convert("RGB").crop(box)
                canvas.paste(region, (0, covered))
                pixel_hash = hashlib.sha256(region.tobytes()).hexdigest()
            frames.append(dict(source=source, before=before, after=after, source_box=box,
                               destination_box=[0, covered, width, end], pixel_sha256=pixel_hash,
                               verification_failures=frame_failures))
            failures.extend(frame_failures)
            covered = end
            if covered >= bottom:
                break
        if covered != bottom:
            raise ValueError("viewport_frame_budget_exceeded")
        destination = run / f"page-{page_index:03d}-region-v{attempt}.png"
        with destination.open("xb") as stream:
            canvas.save(stream, format="PNG")
    finally:
        canvas.close()
    source = png_details(destination, (width, bottom))
    with Image.open(destination) as derived:
        for frame in frames:
            budget()
            if (hashlib.sha256(derived.crop(frame["destination_box"]).tobytes()).hexdigest() != frame["pixel_sha256"]
                    or hashlib.sha256(Path(frame["source"]["path"]).read_bytes()).hexdigest() != frame["source"]["sha256"]):
                raise ValueError("viewport_region_pixels_or_source_changed")
    source.update(role="derived_viewport_region", pixel_integrity_verified=True)
    return source, frames, failures


def rank_items(scene: dict, seen: dict[str, int], page_index: int) -> tuple[list[dict], list[dict]]:
    """返回新身份和全部出现；不修改传入seen，跨页重复仍保留截图位置与duplicate_of。"""
    ranks = dict(seen)
    fresh, occurrences = [], []
    for raw in scene.get("items", []):
        item = {**raw, "page_index": page_index, "source_url": scene["url"]}
        identity = item.get("id")
        if not identity or not item.get("title"):
            item["rank"] = None
        elif identity in ranks:
            item.update(rank=ranks[identity], duplicate_of=ranks[identity])
        else:
            item["rank"] = ranks[identity] = len(ranks) + 1
            fresh.append(item)
        occurrences.append(item)
    return fresh, occurrences


def explicit_end(scene: dict) -> bool:
    """停止增长/缺下一页都不是到底；仅原生禁用下一页或明确到底/空结果文案有效。"""
    return bool((scene.get("next_present") and scene.get("next_disabled"))
                or scene.get("end_markers") or (not scene.get("items") and scene.get("no_results")))


def crop_plan(scene: dict, occurrences: list[dict], top_n: int, *, entire_page: bool) -> dict:
    """整宽矩形保留页顶搜索框及前N完整卡；记录瀑布流邻卡完整/局部覆盖。"""
    size = scene["document_size"]
    targets = [i for i in occurrences if i.get("rank") and i["rank"] <= top_n and not i.get("duplicate_of")]
    bottom = size["height"] if entire_page or not targets else math.ceil(max(i["rect"]["y"] + i["rect"]["height"] for i in targets)) + 8
    bottom = min(size["height"], max(bottom, math.ceil((scene.get("input_rect") or {}).get("y", 0) + (scene.get("input_rect") or {}).get("height", 0)) + 8))
    full, partial = [], []
    for item in occurrences:
        r = item["rect"]
        complete = r["y"] >= 0 and r["y"] + r["height"] <= bottom and r["x"] >= 0 and r["x"] + r["width"] <= size["width"]
        if item.get("rank"):
            if complete:
                full.append(item["rank"])
            elif r["y"] < bottom and r["y"] + r["height"] > 0:
                partial.append(item["rank"])
    full = sorted(set(full))
    return dict(box=[0, 0, size["width"], bottom], fully_visible_ranks=full,
                effective_rank_range=[min(full), max(full)] if full else None,
                incidental_ranks=[r for r in full if r > top_n], partially_visible_ranks=sorted(set(partial)),
                target_ranks=[i["rank"] for i in targets],
                targets_contained=all(i["rank"] in full for i in targets))


def png_details(path: Path, expected_size: tuple[int, int] | None = None) -> dict:
    """解码并验证PNG尺寸和像素非空白；哈希用于不可变原图与派生像素追溯。"""
    from PIL import Image, ImageStat
    with Image.open(path) as image:
        if image.format != "PNG":
            raise ValueError("not_png")
        image.verify()
    with Image.open(path) as image:
        image.load()
        if expected_size and image.size != expected_size:
            raise ValueError(f"png_dimensions_mismatch:{image.size}:{expected_size}")
        if max(ImageStat.Stat(image.convert("RGB")).stddev) < 1:
            raise ValueError("blank_screenshot")
        size = list(image.size)
    return {"path": str(path), "size": size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def compose_primary(pages: list[dict], destination: Path, top_n: int, check) -> dict:
    """逐页原像素裁切后直接拼接；不添加标题/色条，证据说明仅存于元数据。"""
    from PIL import Image
    check()
    width = max(page["crop"]["box"][2] - page["crop"]["box"][0] for page in pages)
    height = sum(page["crop"]["box"][3] - page["crop"]["box"][1] for page in pages)
    if width * height > 80_000_000:
        raise ValueError("primary_pixel_budget_exceeded:80000000")
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    segments = []
    for page in pages:
        check()
        crop = page["crop"]
        with Image.open(page["source"]["path"]) as original:
            region = original.convert("RGB").crop(crop["box"])
            canvas.paste(region, (0, y))
            segments.append({"page_index": page["page_index"], "source_path": page["source"]["path"],
                             "source_box": crop["box"], "destination_box": [0, y, region.width, y + region.height],
                             "pixel_sha256": hashlib.sha256(region.tobytes()).hexdigest()})
            y += region.height
    check()
    with destination.open("xb") as stream:
        canvas.save(stream, format="PNG")
    canvas.close()
    details = png_details(destination, (width, height))
    with Image.open(destination) as final:
        for segment, page in zip(segments, pages):
            check()
            if hashlib.sha256(final.crop(segment["destination_box"]).tobytes()).hexdigest() != segment["pixel_sha256"]:
                raise ValueError("derived_pixels_mismatch")
            if hashlib.sha256(Path(segment["source_path"]).read_bytes()).hexdigest() != page["source"]["sha256"]:
                raise ValueError("source_changed_after_capture")
    return {**details, "segments": segments, "requested_top_n": top_n,
            "pixel_integrity_verified": True, "added_annotations": False}


def assess_results(request: dict, scene: dict, *, screenshot_saved: bool, dom_stable: bool) -> dict:
    """从可追溯分页证据验证前N；输入缺失/图片不完整/未到底均不放行。"""
    items = scene.get("items", [])
    count = len({item["id"] for item in items if item.get("id") and item.get("title")})
    reasons = []
    proof = scene.get("coverage", {})
    if not dom_stable:
        reasons.append("dom_changed_during_capture")
    if not scene.get("input_unobscured"):
        reasons.append("keyword_input_partly_obscured")
    if any(not i.get("images_loaded") for i in items[:request["top_n"]]):
        reasons.append("target_images_incomplete")
    needed = min(count, request["top_n"])
    expected = list(range(1, needed + 1))
    if (not proof.get("pages_verified") or proof.get("verified_ranks") != expected
            or [i.get("rank") for i in items[:needed]] != expected
            or not proof.get("identity_order_verified") or not proof.get("pixel_integrity_verified")
            or not screenshot_saved):
        reasons.append("rank_unverified:coverage_or_identity_or_pixels")
    for field in ("pages_verified", "identity_order_verified", "pixel_integrity_verified"):
        if not proof.get(field):
            reasons.append(f"{field}:false")
    reasons.extend(proof.get("verification_failures", []))
    reasons = list(dict.fromkeys(reasons))
    ended = bool(proof.get("end_verified"))
    if count < request["top_n"] and not ended:
        reasons.append("observed_below_requested:end_not_confirmed")
    complete = not reasons
    return dict(status="success" if complete else ("partial" if count or ended else "failed"),
                reason="strict_top_n_verified" if complete and count >= request["top_n"] else
                       "insufficient_results_end_verified" if complete else ";".join(reasons),
                observed_count=count, rank_verified=complete, verification_failures=reasons,
                outcome="insufficient_results" if complete and count < request["top_n"] else "complete" if complete else "incomplete")


def collect_results(page, request: dict, run: Path, budget, snapshot, classify, checkpoint, settle_ui=None) -> dict:
    """按当前综合页顺序加载、验证、原生翻页；仅稳定、完整覆盖的页参与成功证明。"""
    pages, seen, all_items, transitions = [], {}, [], []
    ended, stopped_reason = False, ""

    def read():
        budget()
        if settle_ui:
            settle_ui()
        scene = snapshot()
        state, reason = classify(scene)
        if state:
            raise PageState(state, reason, scene)
        if scene.get("input_value") != request["keyword"] or parse_qs(urlsplit(scene["url"]).query).get("keyword") != [request["keyword"]]:
            raise ValueError("search_context_mismatch")
        return scene

    def pause(ms=200):
        budget.pause(page, ms)

    page_index = 1
    while True:
        phase("waiting_content", "B站：等待独立结果卡及目标图片稳定")
        scene = read()
        # 首次/翻页后的异步加载有界等待；没有条目但未明确到底不判零结果成功。
        for _ in range(30):
            if scene["items"] or explicit_end(scene):
                break
            pause()
            scene = read()
        if not scene["items"] and not explicit_end(scene):
            stopped_reason = "results_not_loaded"
            break
        # 从页顶逐屏经过目标区域，既触发懒加载，也核验完整卡的遮挡状态。
        visibility = {}
        previous, stable_rounds = None, 0
        for _ in range(120):
            fresh, occurrences = rank_items(scene, seen, page_index)
            target = [i for i in occurrences if i.get("rank") and i["rank"] <= request["top_n"]]
            enough = len(seen) + len(fresh) >= request["top_n"]
            limit = max((i["rect"]["y"] + i["rect"]["height"] for i in target), default=0) if enough else scene["document_size"]["height"]
            for item in target:
                if item.get("unobscured") is True and item.get("images_loaded"):
                    visibility[item["dom_index"]] = item_signature(item)
            signature = layout_fingerprint(scene, {i["dom_index"] for i in target})
            reached = scene["scroll_y"] + scene["viewport"]["height"] >= limit - 2
            stable_rounds = stable_rounds + 1 if signature == previous and reached else 0
            if stable_rounds >= 3:
                break
            previous = signature
            if not reached:
                page.evaluate("y => window.scrollTo(0,y)", min(scene["scroll_y"] + scene["viewport"]["height"] * .55, limit))
            pause(250)
            scene = read()
        fresh, occurrences = rank_items(scene, seen, page_index)
        enough = len(seen) + len(fresh) >= request["top_n"]
        # 每张目标卡对齐至视口中部，补足跨屏边缘和上方固定导航的遮挡验证。
        for item in occurrences:
            if not item.get("rank") or item["rank"] > request["top_n"]:
                continue
            if visibility.get(item["dom_index"]) == item_signature(item):
                continue
            y = max(0, item["rect"]["y"] - (scene["viewport"]["height"] - item["rect"]["height"]) / 2)
            page.evaluate("y => window.scrollTo(0,y)", y)
            pause(180)
            current = read()
            match = next((i for i in current["items"] if i["dom_index"] == item["dom_index"]), {})
            if match.get("unobscured") is True and match.get("images_loaded"):
                visibility[item["dom_index"]] = item_signature(match)
        page.evaluate("window.scrollTo(0,0)")
        page.mouse.move(0, 0)
        pause(300)
        before = read()
        pause(300)
        settled = read()
        # 截图前后都读取；变化时最多追加一个版本，原图从不覆盖。
        record = None
        for attempt in range(1, 3):
            fresh, occurrences = rank_items(settled, seen, page_index)
            enough = len(seen) + len(fresh) >= request["top_n"]
            crop = crop_plan(settled, occurrences, request["top_n"], entire_page=not enough)
            html_path = run / f"page-{page_index:03d}-dom-v{attempt}.html"
            with html_path.open("x", encoding="utf-8") as stream:
                stream.write(page.content())
            started = budget.timestamp()
            targets = [i for i in occurrences if i.get("rank") and i["rank"] <= request["top_n"]]
            source, frames, frame_failures = capture_viewport_region(
                page, settled, crop, targets, run, page_index, attempt, budget, read)
            finished = budget.timestamp()
            page.evaluate("window.scrollTo(0,0)")
            pause(150)
            after = read()
            indices = {i["dom_index"] for i in targets}
            stable = (layout_fingerprint(before, indices) == layout_fingerprint(settled, indices)
                      == layout_fingerprint(after, indices)
                      and not any("changed" in f for f in frame_failures))
            after_by_index = {i["dom_index"]: i for i in after["items"]}
            visible = all(visibility.get(i["dom_index"]) == item_signature(i)
                          and i.get("unobscured") is not False
                          and after_by_index.get(i["dom_index"], {}).get("unobscured") is not False for i in targets)
            visible = visible and not any("obscured" in f for f in frame_failures)
            identity_failures = identity_order_failures(settled) + identity_order_failures(after)
            sequence = lambda s: [(i.get("dom_index"), i.get("id"), i.get("title")) for i in s.get("items", [])]
            for frame in frames:
                for state in (frame["before"], frame["after"]):
                    identity_failures.extend(identity_order_failures(state))
                    if sequence(state) != sequence(settled):
                        identity_failures.append("card_identity_or_order_changed_during_capture")
            if sequence(after) != sequence(settled):
                identity_failures.append("card_identity_or_order_changed_after_capture")
            identities = not identity_failures
            failures = list(dict.fromkeys(identity_failures + frame_failures))
            if not stable:
                failures.append("target_dom_changed_during_capture")
            if not visible:
                failures.extend(f"target_visibility_unverified:{i['dom_index']}" for i in targets
                                if visibility.get(i["dom_index"]) != item_signature(i))
                failures.append("target_visibility_unverified")
            if not crop["targets_contained"]:
                failures.append("targets_not_contained")
            if not settled.get("input_unobscured") or not after.get("input_unobscured"):
                failures.append("keyword_input_partly_obscured")
            if not settled.get("fonts_loaded"):
                failures.append("fonts_not_loaded")
            failures.extend(f"target_images_incomplete:{i['dom_index']}" for i in targets if not i.get("images_loaded"))
            record = dict(page_index=page_index, source=source, html_path=str(html_path),
                          before=settled, after=after, dom_stable=stable, crop=crop,
                          screenshot_mode="viewport_regions", viewport_frames=frames,
                          verification_failures=list(dict.fromkeys(failures)),
                          occurrences=occurrences, identity_order_verified=bool(identities),
                          target_visibility_verified=visible, capture_started_at=started, capture_finished_at=finished)
            record["verified"] = not record["verification_failures"]
            checkpoint(record)
            if stable:
                break
            before = after
            pause(350)
            settled = read()
        pages.append(record)
        for item in fresh:
            seen[item["id"]] = item["rank"]
            item["source_artifact_path"] = record["source"]["path"]
            all_items.append(item)
        scene = record["after"]
        if not record["verified"]:
            stopped_reason = "page_evidence_unverified"
            break
        if enough:
            break
        if explicit_end(scene):
            ended = True
            break
        if not scene.get("next_present"):
            stopped_reason = "no_growth_without_explicit_end_or_next"
            break
        old_number = scene.get("page_number")
        old_ids = [i.get("id") for i in scene["items"]]
        next_button = page.locator('.vui_pagenation button, .vui_pagenation a').filter(has_text=re.compile(r"^下一页$"))
        next_button.click(timeout=budget(3000))
        changed = False
        for _ in range(35):
            pause(200)
            new = read()
            ids = [i.get("id") for i in new["items"]]
            if new.get("page_number") == (old_number or page_index) + 1 and ids != old_ids and (ids or explicit_end(new)):
                changed = True
                transitions.append({"from_page": page_index, "to_page": page_index + 1,
                                    "old_ids": old_ids, "new_ids": ids, "url": new["url"]})
                break
        if not changed:
            stopped_reason = "pagination_not_confirmed"
            break
        page_index += 1
        if not fresh:
            stopped_reason = "pagination_no_new_identity"
            break
    if not pages:
        return dict(items=[], pages=[], transitions=transitions, stopped_reason=stopped_reason,
                    coverage={}, primary=None, scene=scene)
    primary = compose_primary(pages, run / "primary-results-v1.png", request["top_n"], budget)
    needed = min(len(all_items), request["top_n"])
    verified_ranks = sorted({r for p in pages if p["verified"] for r in p["crop"]["target_ranks"] if r <= needed})
    coverage = dict(pages_verified=all(p["verified"] for p in pages), verified_ranks=verified_ranks,
                    identity_order_verified=all(p["identity_order_verified"] for p in pages),
                    pixel_integrity_verified=primary["pixel_integrity_verified"], end_verified=ended,
                    verification_failures=[f"page_{p['page_index']}:{f}" for p in pages for f in p["verification_failures"]])
    offsets = {s["page_index"]: s["destination_box"][1] for s in primary["segments"]}
    for item in all_items:
        item["in_requested_top_n"] = item["rank"] <= request["top_n"]
        item["rank_verified"] = item["rank"] in verified_ranks
        rect = item["rect"]
        item["primary_rect"] = {**rect, "y": rect["y"] + offsets[item["page_index"]]} if item["rank"] in pages[item["page_index"] - 1]["crop"]["fully_visible_ranks"] else None
    return dict(items=all_items, pages=pages, transitions=transitions, stopped_reason=stopped_reason,
                coverage=coverage, primary=primary, scene=scene)


def item_signature(item: dict) -> str:
    """屏幕遮挡校验绑定实际内容、图片和坐标，滚动后替换内容不复用旧证明。"""
    return json.dumps({k: item.get(k) for k in ("id", "title", "rect", "images", "images_loaded", "promoted")}, sort_keys=True, ensure_ascii=False)


class PageState(RuntimeError):
    """只传递平台阻塞/错误现场，不执行验证、登录或网络重试。"""
    def __init__(self, status: str, reason: str, scene: dict):
        super().__init__(reason)
        self.status, self.scene = status, scene
