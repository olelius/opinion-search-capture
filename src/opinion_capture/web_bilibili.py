"""B站 WEB 原页面采集：严格独立视频TOP N、下拉词、总预算和可取消隔离进程。

模块导入/纯函数不启动浏览器；最终图和不可变源图分列返回，不修改共享模块。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .progress import phase

from .web_results_bilibili import (
    RANK_POLICY, RESULT_SNAPSHOT_JS, PageState, assess_results, collect_results,
    normalize_snapshot, png_details,
)


LOCAL_ROOT = Path(__file__).resolve().parents[2] / ".local" / "web_bilibili"
PROFILE_MARKER = ".opinion-capture-bilibili-profile.json"
CARD_SELECTOR = ".search-page .video-list .bili-video-card"
SUGGESTION_SELECTOR = ".search-panel .suggestions .suggest-item"


def validate_request(request: dict) -> dict:
    """验证类型、绝对路径和有限正数；不读写文件、不修改调用方字典。"""
    if not isinstance(request, dict):
        raise ValueError("request 必须是 dict")
    keyword = request.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip() or keyword != keyword.strip():
        raise ValueError("keyword 必须是非空且无首尾空白的字符串")
    if any(ord(char) < 32 for char in keyword):
        raise ValueError("keyword 含控制字符")
    kind = request.get("capture_kind")
    if kind not in ("results", "suggestions"):
        raise ValueError("capture_kind 必须是 results 或 suggestions")
    top_n = request.get("top_n", 20)
    if type(top_n) is not int or top_n <= 0:
        raise ValueError("top_n 必须是正整数")
    timeout = request.get("timeout_seconds", 30)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds 必须是有限正数")
    result = dict(keyword=keyword, capture_kind=kind, top_n=top_n,
                  timeout_seconds=float(timeout))
    for field in ("output_dir", "user_data_dir", "cancel_file"):
        value = request.get(field)
        if field in ("user_data_dir", "cancel_file") and value is None:
            result[field] = None
            continue
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError(f"{field} 必须是绝对路径字符串")
        result[field] = str(Path(value).resolve())
    return result


def build_source_url(keyword: str, capture_kind: str) -> str:
    """使用平台真实搜索页面；下拉词在空搜索页输入，不提交搜索。"""
    base = "https://search.bilibili.com/all"
    return base + "?" + urlencode({"keyword": keyword}) if capture_kind == "results" else base


def classify_page(evidence: dict) -> tuple[str | None, str]:
    """基于可见控件及页面状态分类，普通导航栏的登录入口不算阻塞。"""
    if evidence.get("challenge_visible"):
        return "blocked", "captcha_or_security_challenge"
    if evidence.get("login_required_visible"):
        return "blocked", "login_required"
    code = evidence.get("http_status")
    if code in (401, 403, 412, 429):
        return "blocked", f"http_{code}"
    if isinstance(code, int) and code >= 400:
        return "failed", f"http_{code}"
    url = urlsplit(evidence.get("url", ""))
    if url.hostname == "passport.bilibili.com":
        return "blocked", "login_redirect"
    body = evidence.get("body", "")
    # 仅对没有目标条目的短错误页使用正文规则，避免误判视频标题和页脚。
    if not evidence.get("items") and len(body) < 2500:
        if re.search(r"访问过于频繁|请求过于频繁|请完成.{0,8}验证|安全验证|人机验证|验证后继续|异常访问|风险验证", body):
            return "blocked", "security_or_rate_limit_page"
        if re.search(r"请先登录|登录后.{0,8}(?:搜索|查看|访问)|登录才能", body):
            return "blocked", "login_required"
        if re.search(r"网络错误|网络异常|加载失败|服务异常|页面不存在", body):
            return "failed", "platform_error_page"
    if url.scheme != "https" or url.hostname != "search.bilibili.com":
        return "failed", "unexpected_source_url"
    return None, ""


def assess_capture(request: dict, evidence: dict, *, screenshot_saved: bool,
                   dom_stable: bool) -> dict:
    """纯函数判定产物；observed_count 是同页去重条数，不是已验排名数。"""
    state, reason = classify_page(evidence)
    base = {"status": state or "failed", "reason": reason,
            "observed_count": None, "rank_verified": False}
    if state:
        return base
    if evidence.get("input_value") != request["keyword"]:
        return {**base, "reason": "keyword_mismatch"}
    if not screenshot_saved:
        return {**base, "reason": "screenshot_missing"}
    items = evidence.get("items", [])
    if request["capture_kind"] == "results":
        query = parse_qs(urlsplit(evidence["url"]).query)
        if query.get("keyword") != [request["keyword"]] or request["keyword"] not in evidence.get("title", ""):
            return {**base, "reason": "search_context_mismatch"}
        return assess_results(request, evidence, screenshot_saved=screenshot_saved, dom_stable=dom_stable)
    count = len(items)
    if not count:
        return {**base, "status": "partial", "reason": "suggestions_not_observed",
                "observed_count": 0}
    failures = list(evidence.get("stability_failures", []))
    if not dom_stable and not failures:
        failures.append("suggestions_changed_during_capture")
    if evidence.get("input_unobscured") is not True:
        failures.append("keyword_input_partly_obscured")
    for index, item in enumerate(items, 1):
        if not item.get("text"):
            failures.append(f"suggestion_text_missing:{index}")
        if item.get("unobscured") is not True:
            failures.append(f"suggestion_partly_obscured:{index}")
    if len({item.get("text") for item in items}) != count:
        failures.append("suggestion_duplicate_text")
    failures = list(dict.fromkeys(failures))
    complete = not failures
    return {**base, "status": "success" if complete else "partial",
            "reason": "visible_suggestions_complete" if complete else ";".join(failures),
            "observed_count": count, "verification_failures": failures}


# 只读真实页面 DOM；不替换页面、不隐藏弹窗、不合成平台画面。
_SNAPSHOT_JS = r"""({kind, cards, suggestions}) => {
  const visible = e => {
    if(!e || !e.getClientRects().length) return false;
    for(let n=e;n;n=n.parentElement) {
      const s=getComputedStyle(n);
      if(s.visibility==='hidden' || s.opacity==='0' || s.display==='none') return false;
    }
    return true;
  };
  const rect = e => { const r=e.getBoundingClientRect(); return {
    x:Math.round(r.x+scrollX), y:Math.round(r.y+scrollY),
    width:Math.round(r.width), height:Math.round(r.height)}; };
  const clear = e => {
    if (!visible(e)) return false;
    const r=e.getBoundingClientRect();
    if(r.left<0 || r.top<0 || r.right>innerWidth || r.bottom>innerHeight) return false;
    return [[.1,.5],[.5,.5],[.9,.5]].every(([x,y])=>{
      const t=document.elementFromPoint(r.left+r.width*x,r.top+r.height*y);
      return !!t && (e===t || e.contains(t));
    });
  };
  const nodes = selector => [...document.querySelectorAll(selector)].filter(visible);
  const input=nodes('input.search-input-el, input#search-input')[0];
  let items;
  if(kind==='results') {
    items=nodes(cards).map((e,index)=>{
      const a=e.querySelector('a[href*="/video/"]');
      const title=e.querySelector('.bili-video-card__info--tit');
      const id=a && a.href.match(/\/video\/(BV[0-9A-Za-z]+|av[0-9]+)/);
      const imgs=[...e.querySelectorAll('picture img')];
      return {dom_index:index+1,id:id ? id[1] : null,href:a ? a.href : '',
        title:title ? title.innerText.trim() : '',rect:rect(e),
        images_loaded:imgs.length>0 && imgs.every(i=>i.complete && i.naturalWidth>0)};
    }).filter(i=>i.id && i.title);
    const seen=new Set(); items=items.filter(i=>!seen.has(i.id) && seen.add(i.id));
  } else {
    items=nodes(suggestions).map((e,index)=>({dom_index:index+1,
      text:e.innerText.trim(),rect:rect(e),unobscured:clear(e)}));
  }
  return {url:location.href,title:document.title,body:document.body.innerText.slice(0,20000),
    input_value:input ? input.value : null,input_rect:input ? rect(input) : null,input_unobscured:clear(input),
    challenge_visible:nodes('.geetest_panel, .geetest_window, .geetest_holder, [class*="captcha-container"], iframe[src*="captcha"]').length>0,
    login_required_visible:nodes('.bili-mini-mask, .bili-mini-login, .login-dialog').length>0,
    items,viewport:{width:innerWidth,height:innerHeight},scroll_x:scrollX,scroll_y:scrollY,
    document_size:{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight}};
}"""


def _snapshot(page, kind: str, http_status: int | None) -> dict:
    """读取与截图同一个 Page 的目标条目、坐标、可见性及阻塞标志。"""
    if kind == "results":
        result = normalize_snapshot(page.evaluate(RESULT_SNAPSHOT_JS))
    else:
        result = page.evaluate(_SNAPSHOT_JS, {"kind": kind, "cards": CARD_SELECTOR,
                                             "suggestions": SUGGESTION_SELECTOR})
    result["http_status"] = http_status
    return result


def _fingerprint(evidence: dict) -> str:
    """仅比较下拉内容、顺序、可见性和组内布局，不把整页尺寸当作任务证据。"""
    keys = ("url", "input_value", "input_unobscured", "challenge_visible",
            "login_required_visible", "viewport", "scroll_x", "scroll_y")
    value = {key: evidence.get(key) for key in keys}
    items = evidence.get("items", [])
    anchor = evidence.get("input_rect") or (items[0].get("rect", {}) if items else {})
    value["items"] = [{**{k: i.get(k) for k in ("text", "dom_index", "unobscured")},
                       "rect": {**i.get("rect", {}),
                                "x": i.get("rect", {}).get("x", 0) - anchor.get("x", 0),
                                "y": i.get("rect", {}).get("y", 0) - anchor.get("y", 0)}} for i in items]
    value["input_size"] = {k: (evidence.get("input_rect") or {}).get(k) for k in ("width", "height")}
    return hashlib.sha256(json.dumps(value,
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def suggestion_stability(*scenes: dict) -> dict:
    """报告逐项失败及整体平移；完整列表/组内布局/可见性仍严格比较。"""
    failures, translations = [], []
    first = scenes[0]
    for index, scene in enumerate(scenes):
        label = f"snapshot_{index}"
        for key in ("url", "input_value", "challenge_visible", "login_required_visible", "viewport", "scroll_x", "scroll_y"):
            if scene.get(key) != first.get(key):
                failures.append(f"{label}:{key}_changed")
        if scene.get("input_unobscured") is not True:
            failures.append(f"{label}:keyword_input_partly_obscured")
        items = scene.get("items", [])
        if not items:
            failures.append(f"{label}:suggestions_not_observed")
        if [i.get("text") for i in items] != [i.get("text") for i in first.get("items", [])]:
            failures.append(f"{label}:suggestion_text_or_order_changed")
        for rank, item in enumerate(items, 1):
            if not item.get("text") or item.get("unobscured") is not True:
                failures.append(f"{label}:suggestion_text_or_visibility_unverified:{rank}")
        if _fingerprint(first) != _fingerprint(scene) and not any(f.startswith(label + ":") for f in failures):
            failures.append(f"{label}:suggestion_relative_layout_or_order_changed")
        if items and first.get("items"):
            a, b = first["items"][0]["rect"], items[0]["rect"]
            translations.append({"snapshot": index, "dx": b["x"] - a["x"], "dy": b["y"] - a["y"]})
    return {"verified": not failures, "failures": failures, "group_translations": translations,
            "scope": "keyword_order_visibility_relative_layout_viewport", "document_size_compared": False}


_AUTHENTICATED_NAVIGATION_JS = r"""() => {
  if(location.protocol !== 'https:' || !['www.bilibili.com','search.bilibili.com'].includes(location.hostname)) return false;
  const clear = e => {
    if(!e || !e.getClientRects().length) return false;
    for(let n=e;n;n=n.parentElement) {
      const s=getComputedStyle(n);
      if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
    }
    const r=e.getBoundingClientRect();
    if(r.width<=0||r.height<=0||r.left<0||r.top<0||r.right>innerWidth||r.bottom>innerHeight) return false;
    const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    return !!hit && (hit===e||e.contains(hit));
  };
  const shown=e=>!!e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden'
    && getComputedStyle(e).display!=='none' && getComputedStyle(e).opacity!=='0';
  if([...document.querySelectorAll('.bili-mini-mask,.bili-mini-login,.login-dialog,.geetest_panel,.geetest_window,.geetest_holder,[class*="captcha-container"],iframe[src*="captcha"]')].some(shown)) return false;
  // 只认导航账号头像/个人资料链接；正文UP主链接、普通“登录”入口均不构成认证。
  const navs=[...document.querySelectorAll('.bili-header,.bili-header__bar,.bili-header-m,header[role="banner"]')];
  return navs.some(nav=>[...nav.querySelectorAll('.header-avatar-wrap a[href],a.header-avatar-wrap[href],.header-avatar a[href],a.header-avatar[href],.header-entry-mini a[href],a.header-entry-mini[href],a[href][aria-label="个人中心"],a[href][aria-label="个人空间"]')].some(a=>{
    let u; try {u=new URL(a.href)} catch {return false}
    if(u.protocol!=='https:'||u.hostname!=='space.bilibili.com'||!/^\/[1-9]\d*\/?$/.test(u.pathname)||!clear(a)) return false;
    return [...a.querySelectorAll('img')].some(img=>clear(img)&&img.complete&&img.naturalWidth>0)
      || /^(个人中心|个人空间)$/.test(a.innerText.trim());
  }));
}"""


def authenticated_navigation(page) -> bool:
    """只读当前页面可见导航，供 sessions 的手动循环调用；不导航、不关页、不读存储。

    True 仅代表本次 DOM 明确展示本站账号导航；上下文关闭、持久化及总预算归调用方。
    页面切换中的执行上下文销毁异常交回调用方处理，不伪造 False 或成功。
    """
    return page.evaluate(_AUTHENTICATED_NAVIGATION_JS) is True


def _prepare_profile(profile: Path) -> None:
    """只使用本适配器标记的专属目录或新空目录，拒绝接管已有浏览器配置。"""
    if profile.exists() and not profile.is_dir():
        raise ValueError("user_data_dir 不是目录")
    marker = profile / PROFILE_MARKER
    if profile.exists() and any(profile.iterdir()) and not marker.is_file():
        raise ValueError("user_data_dir 必须是空目录或本适配器创建的专属 profile")
    if marker.is_file() and json.loads(marker.read_text(encoding="utf-8")).get("owner") != "opinion_capture.web_bilibili":
        raise ValueError("user_data_dir 的专属标记不匹配")
    profile.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text('{"owner":"opinion_capture.web_bilibili"}', encoding="utf-8")


def _launch(pw, profile: Path, temporary: Path, budget):
    """优先本机 Chrome/Edge，否则已有 Playwright Chromium；最多一次启动纠正。"""
    candidates = [Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
                  Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
                  Path(pw.chromium.executable_path)]
    configured_browser = os.environ.get("OPINION_CAPTURE_BROWSER")
    if configured_browser:
        configured_path = Path(configured_browser)
        if not configured_path.is_absolute() or not configured_path.is_file():
            raise ValueError("OPINION_CAPTURE_BROWSER应指向有效的绝对浏览器路径")
        candidates = [configured_path]
    available = list(dict.fromkeys(str(path) for path in candidates if path.is_file()))
    errors = []
    for executable in available[:2]:
        try:
            context = pw.chromium.launch_persistent_context(
                str(profile), executable_path=executable, headless=True,
                viewport={"width": 1440, "height": 1000}, locale="zh-CN",
                timezone_id="Asia/Shanghai", downloads_path=str(temporary),
                accept_downloads=False, timeout=budget(12000))
            return context, executable, errors
        except (CaptureCancelled, TimeoutError):
            raise
        except Exception as exc:
            errors.append(f"{executable}: {exc}")
    raise RuntimeError("browser_launch_failed: " + (" | ".join(errors) or "no_installed_chromium_browser"))


class CaptureCancelled(InterruptedError):
    """调用方取消标记；不删除标记，不将取消当平台阻塞。"""


class _Budget:
    """所有操作共用单调时钟截止点；外层进程监督涵盖无timeout参数的浏览器调用。"""
    def __init__(self, deadline: float, cancel_file: str | None):
        self.deadline, self.cancel_file = deadline, Path(cancel_file) if cancel_file else None

    def __call__(self, cap=30000):
        if self.cancel_file and self.cancel_file.exists():
            raise CaptureCancelled("capture_cancelled")
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError("capture_deadline_exceeded")
        return max(1, min(cap, remaining))

    def pause(self, page, milliseconds: int):
        """短步等待，让取消在自然静置期间也能及时生效。"""
        until = time.monotonic() + milliseconds / 1000
        while time.monotonic() < until:
            page.wait_for_timeout(min(self(100), max(1, (until - time.monotonic()) * 1000)))

    @staticmethod
    def timestamp():
        return datetime.now().astimezone().isoformat()


def _write_json(path: Path, value: dict) -> None:
    """排他写入清单/检查点；原图、旧版本和历史证据均不覆盖。"""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _base_result(raw: dict) -> dict:
    """所有退出路径返回同一JSON字段合同；captured_at保留旧调用方兼容性。"""
    now = _Budget.timestamp()
    return dict(platform="bilibili", client="WEB",
                capture_kind=raw.get("capture_kind") if isinstance(raw.get("capture_kind"), str) else "",
                keyword=raw.get("keyword") if isinstance(raw.get("keyword"), str) else "",
                requested_top_n=raw.get("top_n", 20) if type(raw.get("top_n", 20)) is int else 20,
                status="failed", reason="", outcome="incomplete", observed_count=None, rank_verified=False,
                artifact_paths=[], primary_artifact_path=None, source_artifact_paths=[], items=[],
                rank_policy="independent_content_cards", rank_policy_details=RANK_POLICY,
                captured_at=now, capture_started_at=now, capture_finished_at=now, source_url="")


def _dismiss_ordinary_tip_once(page, budget, evidence: dict) -> None:
    """每种已知普通提示只自然交互一次；不重复关同类弹层，验证码/强制登录不触碰。"""
    evidence.setdefault("ordinary_tip_interaction", {"attempted": False})
    interactions = evidence.setdefault("ordinary_tip_interactions", [])
    scene = _snapshot(page, "suggestions", 200)
    if classify_page(scene)[0]:
        return
    # 有关闭按钮则点按钮；无关闭按钮的hover提示以一次移入后移出触发原生mouseleave。
    selector = ".header-login-entry--close:visible, .login-tip .close:visible, .login-tip-content .close:visible, .login-panel-popover .close:visible"
    control = page.locator(selector).first
    if control.count() and not any(i.get("action") == "close_button" for i in interactions):
        evidence["ordinary_tip_interaction"] = {"attempted": True, "selector": selector,
                                                "action": "close_button", "at": budget.timestamp()}
        interactions.append(evidence["ordinary_tip_interaction"])
        control.click(timeout=budget(2000))
        page.mouse.move(0, 0)
        budget.pause(page, 200)
        evidence["ordinary_tip_interaction"]["closed"] = not control.count() or not control.is_visible()
    elif (page.locator('.login-panel-popover:visible').count()
          and not any(i.get("action") == "hover_then_leave" for i in interactions)):
        blank = page.evaluate("""() => {
          const e=document.elementFromPoint(24,180);
          return !!e && !e.closest('a,button,input,[role=button],.login-panel-popover');
        }""")
        if blank:
            evidence["ordinary_tip_interaction"] = {"attempted": True, "action": "hover_then_leave",
                                                    "point": [24, 180], "at": budget.timestamp()}
            interactions.append(evidence["ordinary_tip_interaction"])
            page.locator('.header-login-entry').first.hover(timeout=budget(2000))
            page.mouse.move(24, 180)
            budget.pause(page, 650)
            evidence["ordinary_tip_interaction"]["closed"] = not page.locator('.login-panel-popover:visible').count()


def _capture_worker(payload: dict, *, page=None) -> dict:
    """复用原采集流程；传入page时只借用，浏览器及连接的生命周期归调用方。"""
    req, run = payload["request"], Path(payload["run"])
    result = _base_result(req)
    result["source_url"] = build_source_url(req["keyword"], req["capture_kind"])
    budget = _Budget(payload["deadline"], req["cancel_file"])
    borrowed_page = page is not None
    context, pw = None, None
    response_listener = None
    evidence, checkpoints = {}, []
    code = {"status": None}
    if borrowed_page:
        result["source_url"] = ""
        evidence["page_ownership"] = "caller"

    def snapshot():
        if borrowed_page:
            budget()
        scene = _snapshot(page, req["capture_kind"], code["status"])
        if borrowed_page:
            evidence["last_scene"] = scene
            result["source_url"] = scene["url"]
            budget()
        return scene

    def checkpoint(record):
        checkpoints.append(record)
        _write_json(run / f"checkpoint-{len(checkpoints):03d}.json", record)
        source = record["source"]["path"]
        frames = record.get("viewport_frames", [])
        result["source_artifact_paths"].extend(f["source"]["path"] for f in frames)
        if frames:
            result["artifact_paths"].append(source)
        else:
            result["source_artifact_paths"].append(source)
        result["artifact_paths"].append(record["html_path"])
        if len(checkpoints) == 1:
            result["captured_at"] = result["capture_started_at"] = record["capture_started_at"]
        result["capture_finished_at"] = record["capture_finished_at"]

    try:
        budget()
        if not borrowed_page:
            temporary, profile = run / "temp", Path(req["user_data_dir"]) if req["user_data_dir"] else run / "profile"
            temporary.mkdir(exist_ok=False)
            _prepare_profile(profile)
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            context, executable, launch_errors = _launch(pw, profile, temporary, budget)
            evidence.update(browser_executable=executable, launch_errors=launch_errors)
            page = context.new_page()
            page.set_default_timeout(budget(4000))
        else:
            result["source_url"] = page.url

        def response_status(response):
            """被动记录当前页面搜索请求的HTTP限制；不重放接口、不读取其他会话。"""
            path = urlsplit(response.url).path
            if response.request.resource_type == "document" and response.frame == page.main_frame:
                code["status"] = response.status
            elif "/search/" in path and response.status in (401, 403, 412, 429):
                code["status"] = response.status

        page.on("response", response_status)
        response_listener = response_status
        navigate = True
        phase("prepare_search", "B站：准备搜索页及关键词")
        if borrowed_page:
            current_url = urlsplit(page.url)
            search_page = (current_url.scheme == "https" and current_url.hostname == "search.bilibili.com"
                           and current_url.path.rstrip("/") == "/all")
            if search_page:
                initial = snapshot()
                evidence["initial_scene"] = initial
                state, reason = classify_page(initial)
                if state:
                    raise PageState(state, reason, initial)
            same_keyword = parse_qs(current_url.query).get("keyword") == [req["keyword"]]
            return_to_first = False
            if search_page and same_keyword and req["capture_kind"] == "results":
                page_number = initial.get("page_number")
                page_numbers = parse_qs(current_url.query).get("page", [])
                # 上次跨页采集可能停在后续页；只在明确页码>1时补一次首页导航。
                return_to_first = ((type(page_number) is int and page_number > 1)
                                   or any(re.fullmatch(r"[0-9]+", value) and int(value) > 1
                                          for value in page_numbers))
            navigate = not search_page or (req["capture_kind"] == "results"
                                          and (not same_keyword or return_to_first))
            evidence["navigation"] = {"from_url": page.url, "action": "goto" if navigate else "reuse"}
            if return_to_first:
                evidence["navigation"]["reason"] = "return_to_first_page"
        if navigate:
            target_url = build_source_url(req["keyword"], req["capture_kind"])
            if borrowed_page:
                evidence["navigation"]["target_url"] = target_url
            code["status"] = None
            response = page.goto(target_url, wait_until="domcontentloaded", timeout=budget())
            if code["status"] is None:
                code["status"] = response.status if response else None
        first = snapshot()
        state, reason = classify_page(first)
        if state:
            raise PageState(state, reason, first)
        if borrowed_page and req["capture_kind"] == "results":
            # 一次导航后仍不是首页则保留真实状态，不再次导航或冒充全局TOP N。
            page_numbers = parse_qs(urlsplit(first["url"]).query).get("page", [])
            if (first.get("page_number") not in (None, 1)
                    or any(value != "1" for value in page_numbers)):
                raise PageState("partial", "search_not_first_page", first)
        box = page.locator("input.search-input-el:visible, input#search-input:visible").first
        box.wait_for(state="visible", timeout=budget(6000))
        if req["capture_kind"] == "suggestions":
            if borrowed_page and box.input_value(timeout=budget(3000)) == req["keyword"]:
                evidence["suggestion_input_action"] = "expand_same_value"
                box.click(timeout=budget(3000))
            else:
                if borrowed_page:
                    evidence["suggestion_input_action"] = "fill_changed_value_once"
                box.fill(req["keyword"], timeout=budget(3000))
        selector = CARD_SELECTOR if req["capture_kind"] == "results" else SUGGESTION_SELECTOR
        phase("waiting_content", "B站：等待结果卡或完整下拉列表")
        # 有条目或明确空结果即可继续；超时/拦截由实际现场分类，而非自动重试导航。
        for _ in range(25):
            budget()
            current = snapshot()
            state, reason = classify_page(current)
            if state:
                raise PageState(state, reason, current)
            if current["items"] or current.get("no_results"):
                break
            budget.pause(page, 200)
        budget.pause(page, 400)
        _dismiss_ordinary_tip_once(page, budget, evidence)
        if req["capture_kind"] == "results":
            collected = collect_results(page, req, run, budget, snapshot, classify_page, checkpoint,
                                        lambda: _dismiss_ordinary_tip_once(page, budget, evidence))
            evidence["collection"] = collected
            result["items"] = collected["items"]
            result["coverage"] = collected["coverage"]
            result["page_ranges"] = [{"page_index": p["page_index"], "source_artifact_path": p["source"]["path"],
                                      **p["crop"]} for p in collected["pages"]]
            if collected["primary"]:
                result["primary_artifact_path"] = collected["primary"]["path"]
                result["artifact_paths"].insert(0, collected["primary"]["path"])
            assessment = {**collected["scene"], "items": collected["items"], "coverage": collected["coverage"]}
            result.update(assess_capture(req, assessment, screenshot_saved=bool(collected["primary"]),
                                         dom_stable=all(p["dom_stable"] for p in collected["pages"])))
            if collected["stopped_reason"]:
                result["reason"] += ";" + collected["stopped_reason"]
            result["source_url"] = collected["scene"]["url"]
        else:
            # 关闭普通弹层若使下拉失焦，正常聚焦一次，不提交搜索、不截断10条合同。
            box.focus(timeout=budget(2000))
            budget.pause(page, 500)
            before = snapshot()
            budget.pause(page, 300)
            settled = snapshot()
            html = run / "suggestions-dom-v1.html"
            with html.open("x", encoding="utf-8") as stream:
                stream.write(page.content())
            result["artifact_paths"].append(str(html))
            raw_path = run / "suggestions-original-v1.png"
            result["captured_at"] = result["capture_started_at"] = budget.timestamp()
            phase("screenshot", "B站：保存当前视口下拉原图")
            png = page.screenshot(full_page=False, scale="css", timeout=budget(6000))
            with raw_path.open("xb") as stream:
                stream.write(png)
            result["capture_finished_at"] = budget.timestamp()
            result["source_artifact_paths"].append(str(raw_path))
            after = snapshot()
            phase("validate_capture", "B站：核验下拉文字、顺序和可见范围")
            source = png_details(raw_path, (settled["viewport"]["width"], settled["viewport"]["height"]))
            stability = suggestion_stability(before, settled, after)
            stable = stability["verified"]
            result["items"] = [{**i, "id": "suggestion:" + hashlib.sha256(i["text"].encode()).hexdigest()[:20],
                                "rank": index, "source_artifact_path": str(raw_path),
                                "primary_rect": {**i["rect"], "x": i["rect"]["x"] - settled.get("scroll_x", 0),
                                                 "y": i["rect"]["y"] - settled.get("scroll_y", 0)}}
                               for index, i in enumerate(settled["items"], 1)]
            result.update(assess_capture(req, {**after, "items": settled["items"],
                                              "stability_failures": stability["failures"]},
                                         screenshot_saved=True, dom_stable=stable))
            result["outcome"] = "complete" if result["status"] == "success" else "incomplete"
            result["rank_policy"] = "all_visible_suggestions"
            result["rank_policy_details"] = {"order": "原生下拉DOM顺序", "top_n_applies": False}
            result["primary_artifact_path"] = str(raw_path)
            result["artifact_paths"].insert(0, str(raw_path))
            evidence.update(before=settled, after=after, dom_stable=stable, source=source,
                            stability=stability, screenshot_mode="viewport", initial_stability_scene=before)
        if borrowed_page:
            budget()
    except Exception as exc:
        result.update(status="partial" if result["source_artifact_paths"] else "failed",
                      reason=f"{type(exc).__name__}: {exc}", rank_verified=False, outcome="incomplete")
        if isinstance(exc, CaptureCancelled):
            result.update(status="cancelled", outcome="cancelled", reason="capture_cancelled")
        elif borrowed_page and (isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError"):
            # Playwright TimeoutError与内置TimeoutError不同；都保留异常原文，不诊断重试。
            result["outcome"] = "timeout"
        elif isinstance(exc, PageState):
            result.update(status=exc.status, reason=str(exc), outcome="blocked" if exc.status == "blocked" else "incomplete")
            evidence["blocked_scene"] = exc.scene
        evidence["exception"] = str(exc)
        if borrowed_page:
            evidence["exception_type"] = type(exc).__name__
            # url是Page的当前属性；异常后不再调用DOM、截图或导航来补诊断。
            try:
                result["source_url"] = page.url
            except Exception:
                pass
        # 不扩展总预算；取消或预算结束后不再为诊断启动额外浏览器操作。
        if not borrowed_page and not isinstance(exc, (CaptureCancelled, TimeoutError)) and page is not None:
            try:
                budget()
                diagnostic = run / "diagnostic-v1.png"
                diagnostic_started = budget.timestamp()
                raw = page.screenshot(timeout=budget(1500))
                with diagnostic.open("xb") as stream:
                    stream.write(raw)
                if not result["source_artifact_paths"]:
                    result["captured_at"] = result["capture_started_at"] = diagnostic_started
                result["capture_finished_at"] = budget.timestamp()
                evidence["diagnostic"] = png_details(diagnostic)
                result["source_artifact_paths"].append(str(diagnostic))
            except Exception as diagnostic_error:
                evidence["diagnostic_error"] = str(diagnostic_error)
    finally:
        if borrowed_page and response_listener is not None:
            try:
                # 只移除本次监听，不清空调用方订阅，不修改其默认timeout。
                page.remove_listener("response", response_listener)
            except Exception as exc:
                evidence["listener_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        # 先落盘结果，外层仍监督资源关闭；卡住时只终止本次子进程树。
        if result["status"] != "success" and result["items"] == [] and checkpoints:
            gathered = {}
            for c in checkpoints:
                for i in c["occurrences"]:
                    if i.get("id") and i.get("rank"):
                        gathered.setdefault(i["id"], i)
            result["items"] = list(gathered.values())
            result["observed_count"] = len(gathered)
        # 即使截图后读DOM/PNG校验中断，已写出的源文件也保留在源清单，未验证者不升为primary。
        for pattern in ("page-*-original-v*.png", "suggestions-original-v*.png", "diagnostic-v*.png"):
            result["source_artifact_paths"].extend(str(p) for p in sorted(run.glob(pattern)))
        result["source_artifact_paths"] = list(dict.fromkeys(result["source_artifact_paths"]))
        if borrowed_page and result["status"] == "failed" and result["source_artifact_paths"]:
            result["status"] = "partial"
        evidence.update(result=result, request=req, policy=RANK_POLICY)
        manifest = run / "worker-evidence.json"
        result["artifact_paths"].append(str(manifest))
        _write_json(manifest, evidence)
        _write_json(run / "worker-result.json", result)
        for resource in (context, pw):
            if resource is not None:
                try:
                    resource.close() if resource is context else resource.stop()
                except Exception:
                    pass
    return result


def capture_from_page(page, request: dict) -> dict:
    """在调用方已连接的同步Playwright page上采集，返回与capture相同的结果字段。

    不启动/关闭浏览器、context或page，不读取profile/cookies，不改变默认timeout。
    request沿用capture字段；user_data_dir在此入口忽略。可选deadline是同一主机
    time.monotonic()绝对截止点，与timeout_seconds取更早者。cancel_file只读检查。
    每次有效请求使用独立子目录，源图排他写入，未验证截图不提升为primary。
    必须在page所属线程串行调用；无timeout参数的同步JS/DOM调用使用前后预算检查，
    挂起中的调用由常驻连接worker监督，本函数不通过关闭用户浏览器强制中断。
    """
    started = time.monotonic()
    result = _base_result(request if isinstance(request, dict) else {})
    try:
        req = validate_request({**request, "user_data_dir": None} if isinstance(request, dict) else request)
        if page is None:
            raise ValueError("page 必须是调用方已连接的同步Playwright page")
        deadline = request.get("deadline", started + req["timeout_seconds"])
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("deadline 必须是有限的time.monotonic绝对截止点")
        deadline = min(deadline, started + req["timeout_seconds"])
    except (ValueError, OSError) as exc:
        result["reason"] = f"invalid_request: {exc}"
        return result
    token = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:8]
    run = Path(req["output_dir"]) / (req["capture_kind"] + "-" + token)
    run_created = False
    try:
        run.mkdir(parents=True, exist_ok=False)
        run_created = True
        payload = {"request": req, "run": str(run), "deadline": deadline}
        _write_json(run / "worker-request.json", payload)
        result = _capture_worker(payload, page=page)
    except Exception as exc:
        result.update(status="failed", reason=f"{type(exc).__name__}: {exc}", rank_verified=False)
    result["finished_at"] = _Budget.timestamp()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if run_created:
        manifest = run / "evidence.json"
        result["artifact_paths"].append(str(manifest))
        _write_json(manifest, {"result": result, "request": req, "deadline": deadline,
                               "worker_evidence_path": str(run / "worker-evidence.json")})
    return result


def _stop_worker(process) -> None:
    """仅终止本次Popen创建的专属进程树；不按浏览器名称杀进程，不触碰其他任务。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=3, check=False)
    else:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=3)


def capture(request: dict) -> dict:
    """隔离采集并监督总预算；cancel_file存在返回cancelled，预算结束不延长诊断窗口。

    兼容30秒旧默认及runner的180/1800秒请求。超时覆盖启动、导航、JS、截图、
    拼图和正常关闭；强制回收专属进程树可能有小量尾延迟。仅最终PNG进入旧
    artifact_paths；全部原始截图保留在source_artifact_paths及独立证据目录。
    """
    started = time.monotonic()
    result = _base_result(request if isinstance(request, dict) else {})
    try:
        req = validate_request(request)
    except (ValueError, OSError) as exc:
        result["reason"] = f"invalid_request: {exc}"
        return result
    result["source_url"] = build_source_url(req["keyword"], req["capture_kind"])
    if req["cancel_file"] and Path(req["cancel_file"]).exists():
        return {**result, "status": "cancelled", "reason": "capture_cancelled", "outcome": "cancelled"}
    token = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:8]
    run = Path(req["output_dir"]) / (req["capture_kind"] + "-" + token)
    deadline = started + req["timeout_seconds"]
    process, interruption = None, None
    try:
        run.mkdir(parents=True, exist_ok=False)
        (run / "driver-temp").mkdir()
        payload = {"request": req, "run": str(run), "deadline": deadline}
        payload_file = run / "worker-request.json"
        _write_json(payload_file, payload)
        env = dict(os.environ)
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", ""),
                   PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        for key in ("TEMP", "TMP", "TMPDIR"):
            env[key] = str(run / "driver-temp")
        command = [sys.executable, "-B", "-m", "opinion_capture.web_bilibili", "--worker", str(payload_file)]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with (run / "worker.log").open("xb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, env=env, **options)
            while process.poll() is None:
                if req["cancel_file"] and Path(req["cancel_file"]).exists():
                    interruption = "capture_cancelled"
                    break
                if time.monotonic() >= deadline:
                    interruption = "capture_deadline_exceeded"
                    break
                time.sleep(min(.1, max(.001, deadline - time.monotonic())))
            if interruption:
                _stop_worker(process)
        worker_result = run / "worker-result.json"
        if worker_result.is_file():
            result = json.loads(worker_result.read_text(encoding="utf-8"))
        else:
            records = []
            for file in sorted(run.glob("checkpoint-*.json")):
                try:
                    records.append(json.loads(file.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
            items = {}
            for record in records:
                frames = record.get("viewport_frames", [])
                if frames:
                    result["source_artifact_paths"].extend(f["source"]["path"] for f in frames)
                    result["artifact_paths"].append(record["source"]["path"])
                else:
                    result["source_artifact_paths"].append(record["source"]["path"])
                for item in record["occurrences"]:
                    if item.get("id") and item.get("rank"):
                        items.setdefault(item["id"], item)
            result["items"] = list(items.values())
            result["observed_count"] = len(items)
            if records:
                result["captured_at"] = result["capture_started_at"] = records[0]["capture_started_at"]
                result["capture_finished_at"] = records[-1]["capture_finished_at"]
            result["reason"] = f"worker_exit_without_result:{process.returncode}"
        if interruption:
            result.update(status="cancelled" if interruption == "capture_cancelled" else "partial" if result["source_artifact_paths"] else "failed",
                          reason=interruption, outcome="cancelled" if interruption == "capture_cancelled" else "timeout", rank_verified=False)
    except Exception as exc:
        result.update(status="failed", reason=f"{type(exc).__name__}: {exc}", rank_verified=False)
    finally:
        if process is not None and process.poll() is None:
            try:
                _stop_worker(process)
            except Exception as exc:
                result.update(status="failed", reason=f"worker_cleanup_failed:{exc}", rank_verified=False)
    if run.is_dir():
        for pattern in ("page-*-original-v*.png", "suggestions-original-v*.png", "diagnostic-v*.png"):
            result["source_artifact_paths"].extend(str(p) for p in sorted(run.glob(pattern)))
        result["source_artifact_paths"] = list(dict.fromkeys(result["source_artifact_paths"]))
    result["finished_at"] = _Budget.timestamp()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if run.is_dir():
        manifest = run / "evidence.json"
        result["artifact_paths"].append(str(manifest))
        _write_json(manifest, {"result": result, "request": req, "interruption": interruption,
                               "worker_evidence_path": str(run / "worker-evidence.json")})
    return result


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        _capture_worker(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))

