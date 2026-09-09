"""小红书综合页只读 DOM 协议及跨帧证据算法；不发送平台请求。

排名使用综合页 feed 的独立笔记 DOM 序列，而非瀑布流的 y 坐标排序。
节点身份取笔记链接中的 note ID；原生 DOM 回收须有重叠身份桥接。
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from urllib.parse import urlparse


RANK_POLICY = "independent_content_cards"
RANK_POLICY_DETAIL = {
    "version": 1, "surface": "comprehensive_search",
    "order": "feed_dom_order_with_identity_overlap",
    "include": ["independent_notes", "promoted_notes"],
    "exclude": ["account_aggregates", "related_search", "other_aggregates"],
    "visual_order": "preserve_native_masonry_not_y_coordinate_sort",
}

# 仅查询布局和状态；不删除、改写、隐藏节点，不注入反检测代码。
DOM_HELPERS_JS = r"""
  const shown = e => {if(!e)return false; const r=e.getBoundingClientRect();
    if(r.width<=0||r.height<=0)return false;
    for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);
      if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;}return true;};
  const excluded = '.related-search,.related-searches,.search-recommend,.user-list,.user-container,'+
    '.user-item,.user-card,.account-aggregate,[data-module="users"],[data-module="related-search"],'+
    '[data-module="aggregate"]';
  const cards = [...document.querySelectorAll('section.note-item')].filter(e=>shown(e)&&!e.closest(excluded)
    &&!e.querySelector('.related-search,.related-searches,.user-list,.account-aggregate,[data-module="aggregate"],.query-note-wrapper,.xhs-live-note-item')
    &&!e.parentElement?.closest('section.note-item'));
  const roots = [...new Set(cards.map(e=>e.closest('.feeds-container')||e.parentElement))];
  const root = roots[0]||document.querySelector('.feeds-container');
  let host = root?.parentElement;
  while(host && host!==document.body && host!==document.documentElement){
    const s=getComputedStyle(host);if(/auto|scroll/.test(s.overflowY)&&host.scrollHeight>host.clientHeight+1)break;
    host=host.parentElement;
  }
  const docHost = !host||host===document.body||host===document.documentElement;
  host = docHost ? document.scrollingElement : host;
  const hr = docHost ? {top:0,left:0,bottom:innerHeight,right:innerWidth} : host.getBoundingClientRect();
  const rect = e => {const r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height].map(v=>Math.round(v*10)/10);};
  const clip = e => {const r=e.getBoundingClientRect();let l=Math.max(0,r.left),t=Math.max(0,r.top),
      b=Math.min(innerHeight,r.bottom),rr=Math.min(innerWidth,r.right);
    for(let p=e.parentElement;p;p=p.parentElement){const s=getComputedStyle(p),pr=p.getBoundingClientRect();
      if(/hidden|clip|auto|scroll/.test(s.overflowY)){t=Math.max(t,pr.top);b=Math.min(b,pr.bottom);}
      if(/hidden|clip|auto|scroll/.test(s.overflowX)){l=Math.max(l,pr.left);rr=Math.min(rr,pr.right);}}
    return {l,t,b,rr,w:Math.max(0,rr-l),h:Math.max(0,b-t)};};
  const uncovered = (e,c) => c.w>4&&c.h>4&&[.05,.5,.95].every(y=>[.05,.5,.95].every(x=>{
    const h=document.elementFromPoint(c.l+c.w*x,c.t+c.h*y);return h===e||e.contains(h);}));
  const exposedClip = e => {const c=clip(e);
    // 顶部/底部覆盖条只缩小可证明区间，不丢弃仍实际可见的半张卡。
    for(let pass=0;pass<6&&c.h>4;pass++){
      let changed=false;
      for(const edge of ['top','bottom'])for(const x of [.05,.5,.95]){
        const hit=document.elementFromPoint(c.l+c.w*x,edge==='top'?c.t+2:c.b-2);
        if(!hit||hit===e||e.contains(hit))continue;
        let blocker=hit;
        for(let p=hit;p&&p!==document.body;p=p.parentElement){
          if(/fixed|sticky/.test(getComputedStyle(p).position)){blocker=p;break;}}
        const br=blocker.getBoundingClientRect();
        if(edge==='top'&&br.bottom>c.t&&br.bottom<c.b){c.t=br.bottom;changed=true;}
        if(edge==='bottom'&&br.top<c.b&&br.top>c.t){c.b=br.top;changed=true;}
      }
      c.h=Math.max(0,c.b-c.t);if(!changed)break;
    }
    return c;};
  const full = e => {if(!shown(e))return false;const r=e.getBoundingClientRect(),c=clip(e);
    return c.w>=r.width-1&&c.h>=r.height-1&&uncovered(e,c);};
  const inputExposed = e => {if(!shown(e))return false;const r=e.getBoundingClientRect(),c=clip(e);
    // 搜索/清空图标覆盖右端属于正常布局，只检查实际输入文本区域。
    return c.w>=r.width-1&&c.h>=r.height-1&&[.15,.45,.65].every(x=>{
      const hit=document.elementFromPoint(r.left+r.width*x,r.top+r.height/2);
      return hit===e||e.contains(hit);});};
  const documentUnclipped = e => {const r=e.getBoundingClientRect();
    // full_page 扩展 document，不扩展内部滚动框或 overflow 裁切祖先。
    for(let p=e.parentElement;p&&p!==document.documentElement;p=p.parentElement){
      const s=getComputedStyle(p),pr=p.getBoundingClientRect();
      if(/hidden|clip|auto|scroll/.test(s.overflowY)&&(r.top<pr.top-1||r.bottom>pr.bottom+1))return false;
      if(/hidden|clip|auto|scroll/.test(s.overflowX)&&(r.left<pr.left-1||r.right>pr.right+1))return false;
    }return true;};
"""

SNAPSHOT_JS = "() => {" + DOM_HELPERS_JS + r"""
  // 新版首页采用多行搜索框，结果页仍是input；不能把可见textarea漏成“未登录”。
  const inputs=[...document.querySelectorAll('input#search-input,input[placeholder*="搜索"],input[type="search"],textarea')].filter(shown);
  const input=inputs.find(inputExposed)||inputs[0];
  const notes=cards.map((e,index)=>{
    const links=[...e.querySelectorAll('a[href]')].map(a=>a.href);
    const ids=[...new Set(links.map(h=>{try{const u=new URL(h);return /^(www\.)?xiaohongshu\.com$/.test(u.hostname)
      ?u.pathname.match(/^\/(?:explore|search_result)\/([a-f0-9]{24})(?:\/|$)/i)?.[1]?.toLowerCase():null;}catch{return null;}}).filter(Boolean))];
    const data=e.getAttribute('data-note-id')||e.getAttribute('data-id')||'';
    const conflict=/^[a-f0-9]{24}$/i.test(data)&&ids.length===1&&data.toLowerCase()!==ids[0];
    const id=ids.length===1&&!conflict?ids[0]:'';
    const r=e.getBoundingClientRect(),c=exposedClip(e),imgs=[...e.querySelectorAll('img')];
    const cover=e.querySelector('a.cover img,.cover img,img.note-cover')||imgs.find(i=>!i.closest('.author-wrapper,.avatar,.user-info'));
    const imageRecords=imgs.map(i=>({src:i.currentSrc||i.src||'',loaded:i.complete&&i.naturalWidth>0,
      width:i.naturalWidth,height:i.naturalHeight,rect:rect(i)}));
    const exposed=shown(e)&&uncovered(e,c)&&c.w>=r.width-1;
    const promo=[...e.querySelectorAll('.advertisement,.ad-tag,.ads-tag,.promotion-tag,[data-ad]')]
      .some(n=>shown(n)&&/广告|推广|赞助/.test(n.innerText||n.getAttribute('data-ad')||''))
      || /(?:^|\n)\s*(广告|推广|赞助)\s*(?:\n|$)/.test(e.innerText||'');
    return {id,href:id?links.find(h=>h.toLowerCase().includes('/'+id))||'':'',dom_index:index,
      text:(e.innerText||'').trim(),title:(e.querySelector('.title')?.innerText||'').trim(),
      promoted:promo,identity_error:id?null:(conflict?'conflicting_note_identity':ids.length>1?'ambiguous_note_card':'missing_note_identity'),
      rect:rect(e),document_rect:[r.x+scrollX,r.y+scrollY,r.width,r.height].map(v=>Math.round(v*10)/10),
      document_unclipped:documentUnclipped(e),
      document_uncovered:(c.w<=0||c.h<=0)||uncovered(e,clip(e)),
      images:imageRecords,images_loaded:!!cover&&imageRecords.every(i=>i.loaded),
      cover_present:!!cover,intersects:c.w>0&&c.h>0,fully_visible:full(e),
      coverage:exposed?[Math.max(0,c.t-r.top),Math.min(r.height,c.b-r.top)]:null};
  });
  const panels=[...document.querySelectorAll('.sug-container,.suggestion-container,.ai-sug-container')].filter(shown);
  const panel=panels.length===1?panels[0]:null;
  const all=panel?[...panel.querySelectorAll('.sug-item,.suggestion-item')].filter(e=>shown(e)&&(e.innerText||'').trim()):[];
  const suggestions=all.map((e,index)=>({e,index})).filter(({e})=>full(e))
    .map(({e,index})=>{const label=e.querySelector('.sug-text-wrapper,.sug-text')||e;
      const runs=[];let run='';
      for(const node of label.children){if(node.classList.contains('highlight'))run+=node.textContent||'';
        else if(run){runs.push(run);run='';}}
      if(run)runs.push(run);
      return {text:(label.textContent||e.innerText||'').trim(),matched_queries:runs,rect:rect(e),dom_index:index};});
  const panelAssociated=!!input&&inputExposed(input)&&!!panel&&(document.activeElement===input||input.getAttribute('aria-controls')===panel.id&&!!panel.id);
  const excludedSuggestions=!!panel&&!!panel.querySelector('.history,.search-history,.hot-search,.guess-search');
  const markers=[...document.querySelectorAll('.end-container,.no-more,.no-more-text,.feeds-end,.empty-container,.search-empty,[data-testid="no-more"]')]
    .filter(e=>shown(e)&&clip(e).h>0&&!e.closest(excluded)&&!!root
      &&(root.contains(e)||e.parentElement===root.parentElement));
  const end=markers.find(e=>/^(没有更多(了|内容)?|没有更多笔记(了)?|已(经)?到底(了)?|到底了|暂无(搜索)?结果|没有找到相关(笔记|结果)|未找到相关(笔记|结果))\s*[~～!！。.]*$/.test((e.innerText||'').trim()));
  const loading=[...document.querySelectorAll('.loading,.loading-container,[aria-busy="true"]')].some(e=>shown(e)&&clip(e).h>0);
  const tab=[...document.querySelectorAll('.search-tabs .active,.search-tabs [aria-selected="true"],.tabs-container .active,[role="tab"][aria-selected="true"],.content-container button.tab.active')]
    .find(shown);
  const login=[...document.querySelectorAll('.login-container,.login-modal,[class*="login-modal"],[role="dialog"]')]
    .some(e=>shown(e)&&clip(e).h>0&&/扫码登录|手机号登录|登录探索|验证码登录|登录后/.test(e.innerText||''));
  const blockText=cards.length?[...document.querySelectorAll('p,span,h1,h2,div')]
    .filter(e=>e.childElementCount===0&&shown(e)&&clip(e).h>0&&!e.closest('section.note-item,.sug-container,.suggestion-container,.ai-sug-container'))
    .map(e=>e.innerText||'').join('\n'):(document.body?.innerText||'');
  const authenticated=[...document.querySelectorAll('.user.side-bar-component a.link-wrapper')]
    .some(e=>shown(e)&&clip(e).w>0&&clip(e).h>0&&(e.innerText||'').trim()==='我'
      &&/\/user\/profile\/[a-f0-9]+/i.test(e.getAttribute('href')||''));
  return {schema_version:2,url:location.href,title:document.title,keyword:input?.value||'',input_id:input?.id||'',input_visible:!!input&&inputExposed(input),authenticated,
    text:document.body?.innerText||'',block_text:blockText,login_modal:login,results:notes,suggestions,
    search_tab:(tab?.innerText||'').trim(),feed_count:roots.length||(root?1:0),
    suggestion_panel_complete:!!panel&&full(panel)&&all.length===suggestions.length&&panel.scrollHeight<=panel.clientHeight+1
      &&panelAssociated&&!excludedSuggestions,
    suggestion_panel_rect:panel?rect(panel):null,suggestion_input_rect:input?rect(input):null,
    suggestion_total:all.length,suggestion_panel_associated:panelAssociated,
    pending_images:notes.filter(n=>n.intersects&&!n.images_loaded).length,
    suggestion_pending_images:panel?[...panel.querySelectorAll('img')].filter(i=>!i.complete||i.naturalWidth===0).length:0,
    loading,explicit_end:!!end,end_text:(end?.innerText||'').trim(),end_rect:end?rect(end):null,
    scroll:{top:host?.scrollTop||0,height:host?.scrollHeight||0,viewport_height:docHost?innerHeight:host.clientHeight,
      at_top:(host?.scrollTop||0)<=1,at_bottom:(host?.scrollTop||0)+(docHost?innerHeight:host.clientHeight)>=(host?.scrollHeight||0)-2,
      host:docHost?'document':'feed_ancestor',rect:[hr.left,hr.top,hr.right-hr.left,hr.bottom-hr.top]},
    document:{width:Math.max(document.documentElement.scrollWidth,document.body?.scrollWidth||0,innerWidth),
      height:Math.max(document.documentElement.scrollHeight,document.body?.scrollHeight||0,innerHeight),
      scroll_x:scrollX,scroll_y:scrollY},
    viewport:{width:innerWidth,height:innerHeight},scope:'rendered_independent_notes_and_visible_suggestions'};
}"""

SCROLL_JS = "step => {" + DOM_HELPERS_JS + r"""
  const previous=host.scrollTop;
  host.scrollBy({top:Math.max(1,Math.min(step,(docHost?innerHeight:host.clientHeight)*.8)),behavior:'instant'});
  return {before:previous,after:host.scrollTop};
}"""


SCROLL_TOP_JS = "() => {" + DOM_HELPERS_JS + r"""
  const previous=host.scrollTop;
  host.scrollTo({top:0,behavior:'instant'});
  return {before:previous,after:host.scrollTop};
}"""


def note_identity(item: dict) -> str | None:
    """从规范笔记链接和声明 ID 交叉确认身份；跟踪参数不参与去重。"""
    href = item.get("href", "")
    if not isinstance(href, str):
        return None
    parsed = urlparse(href)
    match = re.fullmatch(r"/(?:explore|search_result)/([a-fA-F0-9]{24})/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname not in ("www.xiaohongshu.com", "xiaohongshu.com") or not match:
        return None
    identity = match[1].lower()
    declared = item.get("id")
    return identity if not declared or declared == identity else None


def fingerprint(snapshot: dict) -> str:
    """忽略页面杂项时钟，但覆盖排名、图片、位置、下拉边界和阻塞状态。"""
    fields = ("url", "keyword", "input_visible", "results", "suggestions", "login_modal", "http_status", "block_text",
              "pending_images", "suggestion_pending_images", "suggestion_panel_complete", "suggestion_panel_rect", "suggestion_input_rect",
              "suggestion_total", "suggestion_panel_associated", "search_tab", "feed_count", "loading",
              "explicit_end", "end_text", "end_rect", "scroll", "viewport", "document")
    return json.dumps({k: snapshot.get(k) for k in fields}, ensure_ascii=False, sort_keys=True)


def interval_covered(intervals: list, height: float) -> bool:
    """验证同一笔记卡垂直区间无缺口；每个区间已要求整宽未遮挡。"""
    if height <= 0 or not intervals:
        return False
    end = 0.0
    for start, stop in sorted(intervals):
        if start > end + 1:
            return False
        end = max(end, stop)
    return end >= height - 1


def analyse_frames(frames: list[dict], top_n: int, *, stop_reason: str = "") -> dict:
    """纯函数：按截图前后 DOM 及原图元信息证明 TOP N 或明确末页不足。

    返回 items 是已观测去重笔记序列；counted 标出 TOP N，screenshot_refs 给出
    原图及卡片相对可见区间。未桥接回收、身份歧义、顺序逆转均保留 partial。
    """
    order, records, intervals, heights = [], {}, {}, {}
    issues, valid_frames = [], []
    head = False
    end_confirmations = 0
    last_end_key = None
    for index, frame in enumerate(frames):
        before, state = frame.get("before", {}), frame.get("after", {})
        if not frame.get("path") or not re.fullmatch(r"[a-f0-9]{64}", frame.get("sha256", "")):
            issues.append("screenshot_evidence_missing")
            continue
        if not frame.get("stable") or fingerprint(before) != fingerprint(state):
            issues.append("dom_changed_during_screenshot")
            continue
        valid_frames.append(frame)
        viewport = state.get("viewport", {})
        if frame.get("dimensions") != [viewport.get("width"), viewport.get("height")]:
            issues.append("screenshot_viewport_dimension_mismatch")
        if state.get("schema_version") != 2 or state.get("feed_count") != 1:
            issues.append("feed_structure_not_verified")
        if state.get("search_tab") != "综合":
            issues.append("comprehensive_tab_not_verified")
        sequence, current = [], {}
        for item in state.get("results", []):
            if item.get("excluded"):
                continue
            identity = note_identity(item)
            if not identity or item.get("identity_error"):
                issues.append("ambiguous_note_identity")
                continue
            if identity not in current:
                sequence.append(identity)
                current[identity] = item
            elif current[identity].get("text") != item.get("text"):
                issues.append("conflicting_duplicate_identity")
        if not order:
            head = bool(state.get("scroll", {}).get("at_top"))
            order.extend(sequence)
        elif sequence:
            old_positions = [order.index(key) for key in sequence if key in order]
            if old_positions != sorted(old_positions):
                issues.append("rank_order_changed")
            new = [key for key in sequence if key not in order]
            if new:
                if not old_positions or order[-1] not in sequence:
                    issues.append("unbridged_dom_recycling")
                elif sequence.index(order[-1]) > sequence.index(new[0]):
                    issues.append("rank_insertion_before_tail")
                order.extend(new)
        for identity in sequence:
            item = current[identity]
            record = records.setdefault(identity, dict(id=identity, href=item["href"],
                title=item.get("title", ""), text=item.get("text", ""), promoted=bool(item.get("promoted")),
                screenshot_refs=[], images_loaded=False))
            signature = (item.get("title", ""), item.get("text", ""), bool(item.get("promoted")))
            if (record["title"], record["text"], record["promoted"]) != signature:
                issues.append("note_content_changed_between_frames")
            rect = item.get("rect", [])
            if len(rect) != 4 or rect[2] <= 0 or rect[3] <= 0:
                issues.append("invalid_card_geometry")
                continue
            coverage = item.get("coverage")
            loaded = item.get("images_loaded") is True and item.get("cover_present") is True
            if coverage and loaded and len(coverage) == 2 and 0 <= coverage[0] < coverage[1] <= rect[3]+1:
                if identity in heights and abs(heights[identity]-rect[3]) > 1:
                    issues.append("card_layout_changed_between_frames")
                heights[identity] = rect[3]
                intervals.setdefault(identity, []).append(coverage)
                record["images_loaded"] = True
                record["screenshot_refs"].append({"frame_index": index, "path": frame["path"],
                    "rect": deepcopy(rect), "coverage": deepcopy(coverage)})
        end_key = (tuple(order), state.get("end_text"), state.get("scroll", {}).get("height"))
        explicit_end = state.get("explicit_end") and state.get("scroll", {}).get("at_bottom") and not state.get("loading")
        if explicit_end:
            end_confirmations = end_confirmations + 1 if last_end_key == end_key else 1
            last_end_key = end_key
        else:
            end_confirmations, last_end_key = 0, None
    if not head:
        issues.append("feed_head_not_observed")
    items = []
    for rank, identity in enumerate(order, 1):
        record = deepcopy(records[identity])
        record.update(rank=rank, counted=rank <= top_n,
                      screenshot_covered=interval_covered(intervals.get(identity, []), heights.get(identity, 0)))
        items.append(record)
    target = items[:top_n]
    enough = len(items) >= top_n
    ended = end_confirmations >= 2
    covered = all(item["screenshot_covered"] for item in target)
    if not covered:
        issues.append("target_images_or_screenshot_coverage_incomplete")
    complete = bool(valid_frames) and not issues and covered and (enough or ended)
    status = "success" if complete else "partial"
    count_proven = not any(issue != "target_images_or_screenshot_coverage_incomplete" for issue in issues)
    outcome = "insufficient_results" if ended and count_proven and len(items) < top_n else "top_n" if enough else "incomplete"
    reason = ("top_n_rank_and_screenshots_verified" if enough else "explicit_end_insufficient_results") if complete else (
        issues[0] if issues else stop_reason or "waiting_for_results_or_explicit_end")
    return dict(status=status, reason=reason, observed_count=len(items), captured_count=sum(i["screenshot_covered"] for i in target),
                rank_verified=complete, rank_policy=RANK_POLICY, outcome=outcome, items=items,
                issues=list(dict.fromkeys(issues)), end_verified=ended, stop_reason=stop_reason)
