"""小红书 WEB：独立笔记 TOP N、原生视口证据、专属手动登录。

capture 用独立子进程包住驱动和截图操作，取消/总预算覆盖整个任务。
capture_from_page 供常驻连接 worker 复用当前 Page，不接管浏览器生命周期。
本模块导入不启动浏览器；open_login 仅供 GUI 显式调用。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unicodedata
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from uuid import uuid4

from .web_results_xiaohongshu import (
    RANK_POLICY, RANK_POLICY_DETAIL, SCROLL_JS, SCROLL_TOP_JS, SNAPSHOT_JS,
    analyse_frames, fingerprint, note_identity,
)
from .web_fullpage import FullPageNotReady, crop_top_n, file_sha256, fullpage_readiness
from .progress import phase

LOCAL_ROOT = Path(__file__).resolve().parents[2] / ".local" / "web_xiaohongshu"
PROFILE_MARKER = ".opinion-capture-exclusive"
PROFILE_OWNER = "xiaohongshu WEB dedicated profile\n"
SOURCE_HOSTS = {"www.xiaohongshu.com", "xiaohongshu.com"}
MAX_FRAMES = 36
MAX_PRIMARY_PIXELS = 60_000_000


def _now() -> str:
    """产生带时区的实际时间。"""
    return datetime.now().astimezone().isoformat()


def validate_request(request: dict) -> dict:
    """校验请求，不读用户浏览器会话；兼容 runner 的 180/1800 秒和取消文件。"""
    if not isinstance(request, dict):
        raise ValueError("request 必须是 dict")
    keyword = request.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip() or any(ord(c) < 32 for c in keyword):
        raise ValueError("keyword 必须是非空且无控制字符的字符串")
    kind = request.get("capture_kind")
    if kind not in ("results", "suggestions"):
        raise ValueError("capture_kind 必须为 results/suggestions")
    top_n = request.get("top_n", 20)
    if type(top_n) is not int or top_n < 1:
        raise ValueError("top_n 必须是正整数")
    timeout = request.get("timeout_seconds", 180)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 1800:
        raise ValueError("timeout_seconds 必须在 (0, 1800] 范围")
    fresh_search = request.get("fresh_search", False)
    if type(fresh_search) is not bool:
        raise ValueError("fresh_search 必须是 bool")
    result = dict(keyword=keyword.strip(), capture_kind=kind, top_n=top_n,
                  timeout_seconds=float(timeout), fresh_search=fresh_search)
    for key in ("output_dir", "user_data_dir", "cancel_file"):
        value = request.get(key)
        if key != "output_dir" and value is None:
            result[key] = None
            continue
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"{key} 必须是绝对路径字符串")
        result[key] = str(Path(value).resolve())
    return result


def classify_block(text: str, url: str = "", *, login_modal: bool = False,
                   http_status: int | None = None) -> str | None:
    """识别明确风险/认证提示；导航栏登录按钮不构成登录墙。"""
    parsed = urlparse(url)
    if parsed.path == "/website-login/error":
        code = parse_qs(parsed.query).get("error_code", [""])[0]
        return "access_restricted" if code == "300012" else "platform_access_error"
    if any(word in text for word in ("安全验证", "滑块验证", "拖动滑块", "请完成验证", "访问验证", "人机验证", "请完成下方验证")):
        return "captcha_required"
    if any(word in text for word in ("访问过于频繁", "操作频繁", "访问受限", "网络环境存在风险", "IP存在风险", "IP 存在风险")):
        return "access_restricted"
    if login_modal or parsed.path.startswith(("/login", "/auth/login")) or any(word in text for word in (
            "登录后查看", "登录后搜索", "请先登录", "登录探索更多内容", "登录后查看更多")):
        return "login_required"
    if http_status in (401, 403, 429):
        return f"http_{http_status}"
    return None


def _block(snapshot: dict) -> str | None:
    """读取页面摘要中的阻塞状态。"""
    return classify_block(snapshot.get("block_text", snapshot.get("text", "")), snapshot.get("url", ""),
                          login_modal=snapshot.get("login_modal", False), http_status=snapshot.get("http_status"))


def _source_problem(snapshot: dict, request: dict, *, require_input_visibility: bool = True) -> str | None:
    """绑定真实来源、输入关键词和结果页查询参数，避免截到旧搜索或其它 tab。"""
    parsed = urlparse(snapshot.get("url", ""))
    if parsed.scheme != "https" or parsed.hostname not in SOURCE_HOSTS:
        return "unexpected_source_url"
    if snapshot.get("keyword") != request["keyword"] or (require_input_visibility and snapshot.get("input_visible") is not True):
        return "keyword_not_verified"
    if request["capture_kind"] == "results":
        if parsed.path.rstrip("/") not in ("/search_result", "/search_result_ai"):
            return "not_search_results_page"
        query = parse_qs(parsed.query).get("keyword", [])
        # 官网从首页提交的URL会再编码一次；输入框仍须完整等于目标词。
        if len(query) != 1 or (query[0] != request["keyword"] and unquote(query[0]) != request["keyword"]):
            return "search_query_not_verified"
    if request["capture_kind"] == "suggestions" and snapshot.get("input_id") in ("search-input-ai", "search-input-in-feeds"):
        query = parse_qs(parsed.query).get("keyword", [])
        if parsed.path.rstrip("/") not in ("/search_result_ai", "/search_result") or len(query)!=1 or (query[0]!=request["keyword"] and unquote(query[0])!=request["keyword"]):
            return "suggestion_query_not_verified"
        normalize = lambda text: "".join(unicodedata.normalize("NFKC", text).casefold().split())
        highlights = [run for item in snapshot.get("suggestions", []) for run in item.get("matched_queries", []) if run]
        if highlights and not any(normalize(run)==normalize(request["keyword"]) for run in highlights):
            return "suggestion_query_not_verified"
    return None


def assess_snapshot(snapshot: dict, request: dict, *, stable: bool) -> tuple:
    """兼容旧单快照接口；结果排名需要 analyse_frames 的原图证据链。"""
    blocked = _block(snapshot)
    if blocked:
        return "blocked", blocked, None, False
    problem = _source_problem(snapshot, request)
    if problem:
        return "failed", problem, None, False
    if not stable:
        return "partial", "dom_changed_during_screenshot", None, False
    count = len(snapshot.get(request["capture_kind"], []))
    if request["capture_kind"] == "results":
        return "partial", "screenshot_rank_chain_required", count, False
    if not count:
        return "partial", "suggestions_not_observed", 0, False
    if snapshot.get("suggestion_pending_images", snapshot.get("pending_images", 0)):
        return "partial", "visible_images_incomplete", count, False
    if not snapshot.get("suggestion_panel_complete") or not snapshot.get("suggestion_panel_associated"):
        return "partial", "suggestion_panel_coverage_not_verified", count, False
    return "success", "visible_keyword_suggestions_verified", count, False


_fingerprint = fingerprint


class CaptureCancelled(Exception):
    """任务取消标记已出现。"""


class Budget:
    """共享 monotonic 截止时间；每一步同时检查取消。"""

    def __init__(self, seconds: float, cancel_file: str | None = None, *, deadline: float | None = None):
        self.deadline = deadline if deadline is not None else time.monotonic() + seconds
        self.cancel_file = Path(cancel_file) if cancel_file else None

    def check(self, cap_ms: int = 10_000) -> int:
        """返回单次操作剩余毫秒数，不给保存现场追加新预算。"""
        if self.cancel_file and self.cancel_file.exists():
            raise CaptureCancelled("cancel_file_detected")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("capture_total_deadline_exceeded")
        return max(1, min(cap_ms, int(remaining * 1000)))

    def pause(self, milliseconds: int = 150) -> None:
        """小步等待；取消检测间隔不超过 50ms。"""
        until = min(self.deadline, time.monotonic() + milliseconds / 1000)
        while time.monotonic() < until:
            self.check()
            time.sleep(min(.05, max(0, until-time.monotonic())))
        self.check()


def _base_result() -> dict:
    """旧 dict 字段保留，最终图与原始源帧独立声明。"""
    stamp = _now()
    return dict(platform="xiaohongshu", client="WEB", capture_kind=None, keyword=None,
                status="failed", reason="invalid_request", requested_top_n=20, observed_count=None,
                rank_verified=False, rank_policy=RANK_POLICY, outcome="incomplete", items=[],
                artifact_paths=[], primary_artifact_path=None, source_artifact_paths=[],
                captured_at=stamp, captured_at_range=[stamp, stamp], started_at=stamp, finished_at=stamp, source_url="")


def _write_json(path: Path, value: dict) -> None:
    """同目录临时文件原子替换；历史 run 目录不复用。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _checkpoint(run: Path, result: dict, evidence: dict) -> None:
    """每帧落盘清单，便于总超时强停后恢复已经存在的原图证据。"""
    path = str(run / "evidence.json")
    if path not in result["artifact_paths"]:
        result["artifact_paths"].append(path)
    result["finished_at"] = _now()
    # 身份/顺序不明优先保留证据，不因伴随超时或停滞而盲目重搜。
    issues = result.get("issues", [])
    scenes = [evidence.get("prepared_snapshot", {})]
    if not evidence.get("fresh_search_navigation"):
        scenes.append(evidence.get("initial_snapshot", {}))
    scenes.extend(frame.get("after", {}) for frame in evidence.get("frames", []))
    identity_uncertain = result.get("capture_kind") == "results" and any(
        item.get("identity_error") or not note_identity(item)
        for scene in scenes for item in scene.get("results", []) if not item.get("excluded"))
    transient = {"target_images_or_screenshot_coverage_incomplete", "dom_changed_during_screenshot"}
    reason = result.get("reason", "") + ";" + result.get("stop_reason", "")
    result["retryable"] = (result.get("status") in ("failed", "partial")
                           and not identity_uncertain and not any(issue not in transient for issue in issues)
                           and (result.get("outcome") == "timeout" or any(token in reason for token in (
                               "TimeoutError", "ConnectionError", "load_timeout", "scroll_stalled", "frame_budget_exceeded",
                               "suggestions_not_observed", "waiting_for_results_or_explicit_end"))))
    _write_json(Path(path), {"schema_version": 2, "result": result, "evidence": evidence})


def _browser_paths(playwright) -> list[Path]:
    """只查本机可执行文件，不读取通用浏览器 profile，不下载浏览器。"""
    configured = os.environ.get("OPINION_CAPTURE_BROWSER")
    if configured:
        executable = Path(configured)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("OPINION_CAPTURE_BROWSER应指向有效的绝对浏览器路径")
        return [executable]
    # 手动登录与后续采集优先使用同一本机浏览器，避免新Chrome保存的profile
    # 随后被较旧的Playwright内置Chromium打开。显式配置仍保持最高优先级。
    candidates = []
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if root:
            candidates.extend([Path(root) / "Google/Chrome/Application/chrome.exe",
                               Path(root) / "Microsoft/Edge/Application/msedge.exe"])
    candidates.append(Path(playwright.chromium.executable_path))
    return list(dict.fromkeys(p for p in candidates if p.is_file()))[:2]


@contextmanager
def _local_playwright():
    """专属临时目录仅传给驱动启动过程；退出后还原环境。"""
    from playwright.sync_api import sync_playwright
    temporary = LOCAL_ROOT / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    previous = {key: os.environ.get(key) for key in ("TMP", "TEMP", "TMPDIR")}
    try:
        for key in previous:
            os.environ[key] = str(temporary)
        playwright = sync_playwright().start()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    try:
        yield playwright
    finally:
        playwright.stop()


@contextmanager
def _dedicated_profile(profile: Path):
    """验证专属标记并取 OS 文件锁；进程强停后由 OS 释放，不遗留排他锁状态。"""
    profile = profile.resolve()
    lowered = str(profile).replace("\\", "/").lower()
    if any(part in lowered for part in ("/google/chrome/user data", "/microsoft/edge/user data", "/mozilla/firefox/profiles")):
        raise ValueError("profile_not_dedicated")
    profile.mkdir(parents=True, exist_ok=True)
    marker = profile / PROFILE_MARKER
    if marker.exists():
        if marker.read_text(encoding="utf-8") != PROFILE_OWNER:
            raise ValueError("profile_owner_mismatch")
    elif any(profile.iterdir()):
        raise ValueError("profile_not_dedicated")
    else:
        try:
            with marker.open("x", encoding="utf-8") as stream:
                stream.write(PROFILE_OWNER)
        except FileExistsError:
            if marker.read_text(encoding="utf-8") != PROFILE_OWNER:
                raise ValueError("profile_owner_mismatch")
    lock = (profile / ".capture.lock").open("a+b")
    acquired = False
    try:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield profile
    finally:
        if acquired:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _snapshot(page, budget: Budget, http_status=None) -> dict:
    """evaluate 的驱动卡死由外层子进程总预算兜底。"""
    budget.check()
    state = page.evaluate(SNAPSHOT_JS)
    state["http_status"] = http_status
    budget.check()
    return state


def _settled(page, request: dict, budget: Budget, http_status=None) -> dict:
    """等待本次页面的 DOM 和可见图片稳定；不导航重试、不再次提交搜索。"""
    phase("waiting_content", "等待目标内容与图片稳定")
    previous = None
    until = min(budget.deadline, time.monotonic() + 12)
    while True:
        state = _snapshot(page, budget, http_status)
        if _block(state):
            return state
        present = bool(state.get(request["capture_kind"])) or (request["capture_kind"] == "results" and state.get("explicit_end"))
        pending = state.get("suggestion_pending_images", 0) if request["capture_kind"] == "suggestions" else state.get("pending_images")
        bound = request["capture_kind"] != "suggestions" or not _source_problem(state, request)
        if request["capture_kind"] == "suggestions" and request.get("fresh_search"):
            normalize = lambda value: "".join(unicodedata.normalize("NFKC", value).casefold().split())
            bound = (bound and state.get("suggestion_panel_associated") and state.get("suggestion_panel_rect")
                     and any(normalize(request["keyword"]) in normalize(item.get("text", ""))
                             for item in state.get("suggestions", [])))
        if present and bound and not state.get("loading") and not pending and previous == fingerprint(state):
            return state
        if time.monotonic() >= until:
            state["wait_reason"] = "content_load_timeout"
            return state
        previous = fingerprint(state)
        budget.pause(60)


def _save_frame(page, run: Path, frames: list, request: dict, budget: Budget, http_status=None) -> dict:
    """保存原生视口 PNG、截图前后摘要及 HTML，不遮盖或重绘真实内容。"""
    from PIL import Image
    index = len(frames)
    before = _snapshot(page, budget, http_status)
    path = run / f"frame-{index:03d}.png"
    started = _now()
    page.screenshot(path=str(path), full_page=False, animations="allow", caret="initial", timeout=budget.check())
    finished = _now()
    with Image.open(path) as png:
        dimensions = list(png.size)
        if png.format != "PNG":
            raise ValueError("source_not_png")
        png.verify()
    # 原图一旦完成即登记；之后驱动失联也保留这张诊断源帧。
    frame = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), dimensions=dimensions,
                 captured_at_range=[started, finished], before=before, after={}, stable=False)
    frames.append(frame)
    after = _snapshot(page, budget, http_status)
    frame.update(after=after, stable=fingerprint(before) == fingerprint(after))
    html = run / f"frame-{index:03d}.html"
    budget.check()
    html.write_text(page.content(), encoding="utf-8")
    frame.update(html_path=str(html), html_sha256=hashlib.sha256(html.read_bytes()).hexdigest())
    return frame


def _evaluate(frames: list, request: dict, stop_reason: str = "") -> dict:
    """阻塞优先；任何帧来源异常都不升级成业务成功。"""
    for index, frame in enumerate(frames):
        for state in (frame["before"], frame["after"]):
            blocked = _block(state)
            if blocked:
                return dict(status="blocked", reason=blocked, observed_count=None, rank_verified=False,
                            items=[], outcome="blocked")
            problem = _source_problem(state, request, require_input_visibility=index == 0 or request["capture_kind"] == "suggestions")
            if problem:
                return dict(status="partial", reason=problem, observed_count=None, rank_verified=False,
                            items=[], outcome="incomplete")
    if request["capture_kind"] == "results":
        return analyse_frames(frames, request["top_n"], stop_reason=stop_reason)
    if not frames:
        return dict(status="partial", reason=stop_reason or "suggestions_not_observed", rank_verified=False, items=[])
    frame = frames[-1]
    status, reason, count, rank = assess_snapshot(frame["after"], request, stable=frame["stable"])
    return dict(status=status, reason=reason, observed_count=count, rank_verified=rank,
                outcome="suggestions" if status == "success" else "incomplete",
                items=[dict(item, rank=index+1, counted=True, screenshot_refs=[{"path": frame["path"], "frame_index": len(frames)-1}])
                       for index, item in enumerate(frame["after"].get("suggestions", []))])


def _compose_primary(frames: list, run: Path, budget: Budget, analysis: dict | None = None) -> tuple[str, list, str]:
    """逐帧原尺寸无损纵接，保留重复页头/重叠内容；不是伪造的无缝页面长图。"""
    from PIL import Image
    phase("screenshot", "保存当前截图")
    budget.check()
    if analysis and analysis.get("rank_verified") and any(item.get("counted") for item in analysis.get("items", [])):
        from .web_scrollshot import compose_scrollshot
        image = compose_scrollshot(frames, analysis, run / "primary.png", budget)
        return image["path"], image["placements"], image["sha256"]
    width = max(frame["dimensions"][0] for frame in frames)
    height = sum(frame["dimensions"][1] for frame in frames)
    if width * height > MAX_PRIMARY_PIXELS:
        raise ValueError("primary_pixel_budget_exceeded")
    primary = run / "primary.png"
    layout = []
    with Image.new("RGB", (width, height), "white") as canvas:
        y = 0
        for index, frame in enumerate(frames):
            budget.check()
            with Image.open(frame["path"]) as source:
                canvas.paste(source.convert("RGB"), (0, y))
            layout.append(dict(frame_index=index, source_path=frame["path"], x=0, y=y,
                               width=frame["dimensions"][0], height=frame["dimensions"][1]))
            y += frame["dimensions"][1]
        canvas.save(primary, format="PNG")
    budget.check()
    with Image.open(primary) as check:
        check.verify()
    return str(primary), layout, hashlib.sha256(primary.read_bytes()).hexdigest()


def _next_scroll_step(state: dict, analysis: dict, top_n: int) -> float:
    """正常保留重叠；接近第N条时只滚到缺失底边，避免触发多余的下一页加载。"""
    scroll = state.get("scroll", {})
    visible_height = scroll.get("viewport_height", state.get("viewport", {}).get("height", 1080))
    step = max(1, visible_height * .8)
    targets = [item for item in analysis.get("items", []) if item.get("counted")]
    if len(targets) < top_n:
        return step
    pending = {item["id"] for item in targets if not item.get("screenshot_covered")}
    current = {item["id"]:item for item in state.get("results", [])}
    if pending and pending.issubset(current):
        rect = scroll.get("rect", [0, 0, 0, visible_height])
        bottom = min(state.get("viewport", {}).get("height", rect[1]+rect[3]), rect[1]+rect[3])
        missing = max(current[key]["rect"][1]+current[key]["rect"][3] for key in pending)-bottom
        if missing <= 0:
            return 0  # 已在画面内，先等图片/遮挡状态，不滚走缺失内容。
        step = min(step, missing+min(96,visible_height*.1))
    return step


def _collect(page, request: dict, run: Path, result: dict, evidence: dict, budget: Budget) -> None:
    """对已导航页面采集；离线 DOM 夹具执行同一算法，不重复访问入口。"""
    frames = evidence.setdefault("frames", [])
    stalls = 0
    previous_position = None
    stop_reason = ""
    for _ in range(MAX_FRAMES):
        settled = _settled(page, request, budget, evidence.get("http_status"))
        if _block(settled):
            result.update(status="blocked", reason=_block(settled), rank_verified=False, observed_count=None, outcome="blocked")
            evidence["blocked_snapshot"] = settled
            _checkpoint(run, result, evidence)
            break
        if settled.get("wait_reason"):
            stop_reason = settled["wait_reason"]
            break
        frame = _save_frame(page, run, frames, request, budget, evidence.get("http_status"))
        result["source_artifact_paths"].append(frame["path"])
        result["artifact_paths"].extend([frame["path"], frame["html_path"]])
        result.update(captured_at=frame["captured_at_range"][0], source_url=frame["before"].get("url", ""),
                      captured_at_range=[frames[0]["captured_at_range"][0], frame["captured_at_range"][1]])
        phase("validate_capture", "核对已捕获条目与截图覆盖")
        result.update(_evaluate(frames, request))
        _checkpoint(run, result, evidence)
        if result["status"] in ("blocked", "failed", "success"):
            break
        if request["capture_kind"] == "suggestions":
            stop_reason = "suggestion_panel_incomplete"
            break
        state = frame["after"]
        if _source_problem(state, request, require_input_visibility=len(frames) == 1):
            stop_reason = result["reason"]
            break
        position = (state.get("scroll", {}).get("top"), state.get("scroll", {}).get("height"),
                    tuple(item.get("id") for item in state.get("results", [])))
        stalls = stalls + 1 if position == previous_position else 0
        previous_position = position
        if stalls >= 3:
            stop_reason = "scroll_stalled_without_complete_evidence"
            break
        budget.check()
        step = _next_scroll_step(state, result, request["top_n"])
        frame["next_scroll_step"] = step
        if step > 0:
            page.evaluate(SCROLL_JS, step)
    else:
        stop_reason = "frame_budget_exceeded"
    if stop_reason:
        if frames:
            result.update(_evaluate(frames, request, stop_reason))
        else:
            result.update(status="partial", reason=stop_reason, outcome="incomplete", rank_verified=False)
        if stop_reason not in result["reason"]:
            result["reason"] += ";" + stop_reason
        result["stop_reason"] = stop_reason
        evidence["stop_reason"] = stop_reason
    target_scene = any(frame["after"].get(request["capture_kind"]) or
                       (request["capture_kind"] == "results" and frame["after"].get("explicit_end")) for frame in frames)
    sources_bound = all(not _source_problem(frame["after"], request, require_input_visibility=index == 0)
                        for index, frame in enumerate(frames))
    if frames and target_scene and sources_bound and result["status"] in ("success", "partial"):
        path, layout, digest = _compose_primary(frames, run, budget, result if request["capture_kind"] == "results" else None)
        result["primary_artifact_path"] = path
        result["artifact_paths"].append(path)
        evidence.update(primary_sha256=digest, primary_layout=layout,
                        composition="continuous_native_scroll_column" if request["capture_kind"] == "results" and result.get("rank_verified") else "native_viewport_frames",
                        contains_context_beyond_top_n=True)
    evidence["artifact_role"] = "target_observation" if result["primary_artifact_path"] else "diagnostic"
    _checkpoint(run, result, evidence)


def _worker(request: dict, run: Path, result: dict, *, mode: str = "capture", deadline: float | None = None) -> dict:
    """一次浏览器启动/导航；平台认证风险立即停止，不刷新或切网络。"""
    budget = Budget(request["timeout_seconds"], request.get("cancel_file"), deadline=deadline)
    profile = Path(request.get("user_data_dir") or LOCAL_ROOT / "profiles" / uuid4().hex)
    evidence = dict(frames=[], artifact_role="diagnostic", rank_policy=RANK_POLICY_DETAIL,
                    evidence_source="live_platform" if mode == "capture" else "explicit_manual_login",
                    profile=str(profile), keyword_input_completed=False, navigation_attempts=0)
    try:
        budget.check()
        with _dedicated_profile(profile), _local_playwright() as playwright:
            candidates = _browser_paths(playwright)
            if not candidates:
                raise RuntimeError("browser_not_installed")
            context = playwright.chromium.launch_persistent_context(str(profile), executable_path=str(candidates[0]),
                headless=False, viewport={"width": 1440, "height": 1080},
                locale="zh-CN", timezone_id="Asia/Shanghai", device_scale_factor=1, timeout=budget.check())
            try:
                page = context.pages[0] if context.pages else context.new_page()
                # 由平台输入框生成实际搜索路由，不猜测当前版本的URL编码/重定向。
                url = "https://www.xiaohongshu.com/explore"
                evidence.update(requested_url=url, navigation_attempts=1, browser=str(candidates[0]))
                _checkpoint(run, result, evidence)
                response = page.goto(url, wait_until="domcontentloaded", timeout=budget.check(30_000))
                evidence["http_status"] = response.status if response else None
                if mode == "login":
                    result.update(status="partial", reason="manual_login_window_open", source_url=page.url)
                    _checkpoint(run, result, evidence)
                    while not page.is_closed():
                        try:
                            state = _snapshot(page, budget, evidence["http_status"])
                        except Exception as exc:
                            if page.is_closed():
                                result.update(status="success", reason="manual_window_closed_authentication_not_asserted",
                                              outcome="manual_session_closed")
                                break
                            if ("Execution context was destroyed" in str(exc)
                                    or "Cannot find context with specified id" in str(exc)):
                                # 用户登录/切页会正常销毁旧DOM上下文。这里只等待新页面，
                                # 不重发导航、不关闭登录窗口、不把跳转本身当认证成功。
                                budget.pause(200)
                                continue
                            raise
                        block = _block(state)
                        if block and block != "login_required":
                            result.update(status="blocked", reason=block, outcome="blocked")
                            break
                        login_url = urlparse(state.get("url", ""))
                        if (state.get("authenticated") is True and not state.get("login_modal")
                                and login_url.scheme == "https" and login_url.hostname in SOURCE_HOSTS):
                            result.update(status="success", reason="authenticated_navigation_visible",
                                          outcome="authenticated_session_ready", authentication_verified=True)
                            break
                        if (request.get("capture_login_preview") is True and state.get("login_modal")
                                and not evidence.get("login_preview_path")):
                            # 仅显式启用时保存一次登录页面供扫码；不收集密码，预览只在私有目录。
                            preview = run / "login-preview.png"
                            page.screenshot(path=str(preview), full_page=False, timeout=budget.check())
                            evidence["login_preview_path"] = str(preview)
                            _checkpoint(run, result, evidence)
                        budget.pause(200)
                    else:
                        result.update(status="success", reason="manual_window_closed_authentication_not_asserted",
                                      outcome="manual_session_closed")
                else:
                    while True:
                        state = _snapshot(page, budget, evidence["http_status"])
                        if _block(state) or state.get("input_visible"):
                            break
                        budget.pause()
                    active_page = [page]
                    state = _prepare_current_page(page, request, evidence, budget, active_page)
                    page = active_page[0]
                    problem = evidence.get("preparation_problem") or _source_problem(state, request)
                    if _block(state):
                        _collect(page, request, run, result, evidence, budget)
                    elif problem:
                        result.update(status="partial", reason=problem)
                    elif request["capture_kind"] == "results" and _collect_fullpage(page, request, run, result, evidence, budget, state):
                        pass
                    else:
                        _collect(page, request, run, result, evidence, budget)
            finally:
                try:
                    context.close()
                except Exception as close_error:
                    # 用户已关闭可见浏览器时，Playwright的二次close可能报告TargetClosed；
                    # 它不应覆盖已经记录的手动窗口结束或原始业务结果。
                    if "Target page, context or browser has been closed" not in str(close_error):
                        raise
    except CaptureCancelled:
        result.update(status="cancelled", reason="cancel_file_detected", rank_verified=False, outcome="cancelled")
    except TimeoutError:
        if result["status"] != "blocked":
            result.update(status="partial", reason="capture_total_deadline_exceeded", rank_verified=False, outcome="timeout")
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        if result["status"] != "blocked":
            result.update(status="partial" if evidence["frames"] else "failed", reason=evidence["error"],
                          rank_verified=False, outcome="incomplete")
    finally:
        # 驱动在帧后摘要/HTML阶段失败时，已完成的原生 PNG 仍进入证据清单。
        for frame in evidence["frames"]:
            if frame["path"] not in result["source_artifact_paths"]:
                result["source_artifact_paths"].append(frame["path"])
            for key in ("path", "html_path"):
                if frame.get(key) and frame[key] not in result["artifact_paths"]:
                    result["artifact_paths"].append(frame[key])
        try:
            _checkpoint(run, result, evidence)
        except OSError as exc:
            result.update(status="failed", reason=f"evidence_write_failed: {exc}", rank_verified=False)
    return result


def _terminate_owned_process(process) -> None:
    """只终止本次创建的进程树，包含驱动和浏览器；不按进程名扫描用户浏览器。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def _supervise(request: dict, result: dict, *, mode: str = "capture") -> dict:
    """独立工作子进程提供驱动级总期限，50ms 轮询取消；不在当前线程启动浏览器。"""
    started = time.monotonic()
    run = None
    process = None
    owned_job = None
    interrupted = None
    evidence = {"frames": [], "rank_policy": RANK_POLICY_DETAIL}
    try:
        if request.get("cancel_file") and Path(request["cancel_file"]).exists():
            result.update(status="cancelled", reason="cancel_file_detected", outcome="cancelled")
            return result
        run = Path(request["output_dir"]) / ("goal-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10])
        run.mkdir(parents=True, exist_ok=False)
        _checkpoint(run, result, evidence)
        job = run / "request.json"
        start_gate = run / "worker.start"
        _write_json(job, {"request": request, "result": result, "mode": mode,
                          "deadline": started+request["timeout_seconds"], "start_gate": str(start_gate)})
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        import_roots = [str(Path(__file__).resolve().parents[1])]
        executable = sys.executable
        if os.name == "nt" and not getattr(sys, "frozen", False):
            # 避免venv redirector使父进程结束而实际Python继续运行。
            executable = getattr(sys, "_base_executable", sys.executable)
            import_roots.insert(0, str(Path(sys.prefix) / "Lib" / "site-packages"))
        env["PYTHONPATH"] = os.pathsep.join(import_roots + [env.get("PYTHONPATH", "")])
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with (run / "worker.log").open("wb") as log:
            process = subprocess.Popen([executable, "-B", "-m", "opinion_capture.web_xiaohongshu", "--worker", str(job)],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **options)
            if os.name == "nt":
                from .sessions import _WindowsJob
                owned_job = _WindowsJob()
                owned_job.assign(process.pid)
            # 先持有整棵进程树，才允许worker创建Playwright和浏览器后代。
            start_gate.touch(exist_ok=False)
            while process.poll() is None:
                if request.get("cancel_file") and Path(request["cancel_file"]).exists():
                    interrupted = "cancelled"
                    break
                if time.monotonic()-started >= request["timeout_seconds"]:
                    interrupted = "timeout"
                    break
                time.sleep(min(.05, max(0, started+request["timeout_seconds"]-time.monotonic())))
            if interrupted:
                if owned_job is not None:
                    owned_job.terminate()
                    process.wait(timeout=5)
                else:
                    _terminate_owned_process(process)
        payload = json.loads((run / "evidence.json").read_text(encoding="utf-8"))
        result = payload["result"]
        evidence = payload["evidence"]
        if interrupted:
            if interrupted == "cancelled" or result["status"] != "blocked":
                result.update(status="cancelled" if interrupted == "cancelled" else "partial",
                              reason="cancel_file_detected" if interrupted == "cancelled" else "capture_total_deadline_exceeded",
                              rank_verified=False, outcome=interrupted)
            evidence["supervisor_interruption"] = interrupted
        elif process.returncode or result["reason"] == "invalid_request":
            result.update(status="failed", reason="capture_worker_exited_without_completion", rank_verified=False)
        result["elapsed_seconds"] = round(time.monotonic()-started, 3)
    except Exception as exc:
        result.update(status="failed", reason=f"{type(exc).__name__}: {exc}", rank_verified=False)
        evidence["supervisor_error"] = result["reason"]
    finally:
        try:
            if owned_job is not None:
                owned_job.close()
                if process is not None:
                    process.wait(timeout=5)
            elif process is not None and process.poll() is None:
                _terminate_owned_process(process)
        except Exception as exc:
            result.update(status="failed", reason=f"worker_cleanup_failed: {exc}", rank_verified=False)
            evidence["cleanup_error"] = str(exc)
        result["finished_at"] = _now()
        if run is not None and run.is_dir():
            try:
                _checkpoint(run, result, evidence)
            except OSError as exc:
                result.update(status="failed", reason=f"evidence_write_failed: {exc}", rank_verified=False)
    return result


def capture(request: dict) -> dict:
    """一次采集；唯一最终业务图 primary_artifact_path，源帧保留但不直接参与导出。"""
    result = _base_result()
    try:
        req = validate_request(request)
    except (ValueError, OSError, RuntimeError) as exc:
        result["reason"] = f"invalid_request: {exc}"
        return result
    result.update(capture_kind=req["capture_kind"], keyword=req["keyword"], requested_top_n=req["top_n"])
    return _supervise(req, result)


def _collect_fullpage(page, request: dict, run: Path, result: dict, evidence: dict,
                      budget: Budget, before: dict) -> bool:
    """仅尝试一次已加载 document 原图；不足时不重拍，交还原滚动证明链。"""
    from PIL import Image

    problem = fullpage_readiness(before, request["top_n"])
    if problem:
        evidence["fullpage_fallback_reason"] = problem
        return False
    budget.check()
    path = run / "source-fullpage.png"
    frame = dict(path=str(path), before=before, after={}, stable=False,
                 full_page=True, captured_at_range=[_now(), _now()])
    # 先登记源图槽位；截图后取消或驱动失联仍会在 finally 保留已落盘 PNG。
    evidence.setdefault("fullpage_frames", []).append(frame)
    page.screenshot(path=str(path), full_page=True, animations="allow", caret="initial",
                    timeout=budget.check())
    frame["captured_at_range"][1] = _now()
    frame["after"] = _snapshot(page, budget, evidence.get("http_status"))
    frame["stable"] = fingerprint(before) == fingerprint(frame["after"])
    budget.check()
    with Image.open(path) as png:
        frame["dimensions"] = list(png.size)
        if png.format != "PNG":
            raise FullPageNotReady("source_not_png")
        png.verify()
    frame["sha256"] = file_sha256(path, budget)
    result["source_artifact_paths"].append(str(path))
    result["artifact_paths"].append(str(path))
    result.update(captured_at=frame["captured_at_range"][0], source_url=before.get("url", ""),
                  captured_at_range=frame["captured_at_range"][:])
    _checkpoint(run, result, evidence)
    after = frame["after"]
    if _block(after):
        result.update(status="blocked", reason=_block(after), outcome="blocked", rank_verified=False)
        return True
    problem = _source_problem(after, request)
    if problem:
        result.update(status="partial", reason=problem, outcome="incomplete", rank_verified=False)
        return True
    problem = fullpage_readiness(after, request["top_n"])
    if problem:
        evidence["fullpage_fallback_reason"] = problem
        return False
    derived_path = run / f"xiaohongshu-WEB-TOP{request['top_n']}.png"
    evidence["derived_candidate_path"] = str(derived_path)
    try:
        layout = crop_top_n(path, frame, derived_path, request["top_n"], budget=budget)
    except FullPageNotReady as exc:
        evidence["fullpage_fallback_reason"] = str(exc)
        return False
    budget.check()
    result.update(status="success", reason="top_n_fullpage_dom_and_screenshot_verified", outcome="top_n",
                  rank_verified=True, observed_count=layout["observed_count"], captured_count=request["top_n"],
                  items=layout["items"], primary_artifact_path=layout["primary_artifact_path"],
                  issues=[], end_verified=False)
    result["artifact_paths"].append(layout["primary_artifact_path"])
    evidence.update(primary_layout=layout, primary_sha256=layout["primary_sha256"],
                    composition=layout["composition"], contains_context_beyond_top_n=True,
                    artifact_role="target_observation")
    _checkpoint(run, result, evidence)
    return True


def _input_control(page, state):
    """操作与快照识别一致的输入框，避免新版多行框与旧占位框串位。"""
    if state.get("input_id") == "search-input-ai":
        return page.locator('textarea#search-input-ai:visible').first
    if state.get("input_id") == "search-input-in-feeds":
        return page.locator('textarea#search-input-in-feeds:visible').first
    if state.get("input_id") == "search-input":
        return page.locator('input#search-input:visible').first
    return page.locator('input#search-input:visible,input[placeholder*="搜索"]:visible,input[type="search"]:visible,textarea:visible').first


def _prepare_current_page(page, request: dict, evidence: dict, budget: Budget, page_ref=None) -> dict:
    """首轮同词零导航；结果重试仅一次传统URL导航，下拉重试只清空重填。"""
    phase("prepare_search", "定位搜索框与当前关键词")
    state = _snapshot(page, budget)
    evidence["initial_snapshot"] = state
    fresh = request.get("fresh_search", False)
    search_url = "https://www.xiaohongshu.com/search_result?" + urlencode({
        "keyword": quote(request["keyword"], safe=""), "source": "web_explore_feed"})
    if state.get("url") == "about:blank":
        # 连接层只会为缺失的平台新建空标签页；一次进入目标页，不另开浏览器。
        url = "https://www.xiaohongshu.com/explore"
        if request["capture_kind"] == "suggestions" or fresh:
            url = search_url
        evidence.update(navigation_attempts=1, requested_url=url)
        response = page.goto(url, wait_until="domcontentloaded", timeout=budget.check(30_000))
        budget.check()
        evidence["http_status"] = response.status if response else None
        until = min(budget.deadline, time.monotonic() + 20)
        while True:
            state = _snapshot(page, budget, evidence.get("http_status"))
            if _block(state) or state.get("input_visible") or time.monotonic() >= until:
                break
            budget.pause(200)
    if _block(state):
        return state
    parsed = urlparse(state.get("url", ""))
    if parsed.scheme != "https" or parsed.hostname not in SOURCE_HOSTS:
        evidence["preparation_problem"] = "unexpected_source_url"
        return state
    if request["capture_kind"] == "results":
        if fresh:
            # goto即使URL相同也重新载入文档；不依赖同路由可能被忽略的Enter提交。
            if not evidence.get("navigation_attempts"):
                response = page.goto(search_url, wait_until="domcontentloaded", timeout=budget.check(30_000))
                evidence.update(navigation_attempts=1, requested_url=search_url,
                                http_status=response.status if response else None)
            evidence["fresh_search_navigation"] = True
            state = _snapshot(page, budget, evidence.get("http_status"))
            if _block(state):
                return state
        # URL 与输入框都绑定本词才复用；二次 URL 编码沿用既有校验。
        problem = _source_problem(state, request, require_input_visibility=False)
        if fresh:
            # 后续只等本次导航内容就绪，不补提搜索或混入旧页面源帧。
            pass
        elif not problem:
            evidence["page_reused"] = True
            # 上次下拉仍展开时只收起，不重新提交搜索。
            if state.get("suggestion_panel_rect"):
                field = _input_control(page, state)
                # 当前官网Escape不会收起该面板；Tab使输入框失焦，已在原页确认。
                field.press("Tab", timeout=budget.check(2000))
                budget.check()
                evidence["suggestions_dismissed"] = True
        else:
            if not state.get("input_visible"):
                evidence["preparation_problem"] = "keyword_input_not_visible"
                return state
            field = _input_control(page, state)
            if state.get("keyword") != request["keyword"]:
                field.fill(request["keyword"], timeout=budget.check(2000))
                evidence["keyword_fill_attempts"] = 1
            # 新版首页可能在新标签页打开结果。只提交一次，再跟随该次操作的搜索页。
            budget.pause(250)
            popups = []
            def on_popup(popup):
                popups.append(popup)
            observes = hasattr(page, "on")
            origin_page = page
            if observes:
                origin_page.on("popup", on_popup)
            try:
                submit = page.locator('.submit-button-wrapper:visible,img.standard-search-icon:visible')
                if hasattr(submit, "count") and submit.count() == 1:
                    submit.click(timeout=budget.check(3000))
                    evidence["search_control"] = "platform_search_button"
                else:
                    field.press("Enter", timeout=budget.check(30_000))
                budget.check()
                evidence["search_submit_attempts"] = 1
                until = min(budget.deadline, time.monotonic()+10)
                while True:
                    for popup in popups:
                        if _source_problem({"url":popup.url,"keyword":request["keyword"]},request,require_input_visibility=False) is None:
                            page = popup
                            evidence["followed_search_popup"] = True
                            if page_ref is not None:
                                page_ref[:] = [page]
                            break
                    state_after = _snapshot(page, budget, evidence.get("http_status"))
                    if _block(state_after) or _source_problem(state_after,request,require_input_visibility=False) is None or time.monotonic()>=until:
                        break
                    budget.pause(150)
            finally:
                if observes:
                    origin_page.remove_listener("popup", on_popup)
        if state.get("scroll", {}).get("top", 0) > 1 or state.get("document", {}).get("scroll_y", 0) > 1:
            budget.check()
            evidence["scroll_to_top"] = page.evaluate(SCROLL_TOP_JS)
            budget.check()
    else:
        if state.get("input_id") in ("search-input-ai", "search-input-in-feeds"):
            # AI输入器混入个性化推荐，不能冒充关键词联想。使用用户实际浏览器
            # 已展示过的传统搜索路由和原生编码形式，一次导航后获取真正下拉面板。
            url = "https://www.xiaohongshu.com/search_result?" + urlencode({
                "keyword":quote(request["keyword"],safe=""),"source":"web_explore_feed"})
            response = page.goto(url,wait_until="domcontentloaded",timeout=budget.check(30_000))
            evidence["suggestion_surface_navigation"] = 1
            evidence["http_status"] = response.status if response else None
            until = min(budget.deadline,time.monotonic()+20)
            while True:
                state = _snapshot(page,budget,evidence.get("http_status"))
                if _block(state) or (state.get("input_visible") and state.get("input_id") == "search-input") or time.monotonic()>=until:
                    break
                budget.pause(200)
            if _block(state):
                return state
            if state.get("input_id") != "search-input":
                evidence["preparation_problem"] = "keyword_suggestion_surface_unavailable"
                return state
        # 不跳 explore，也不按 Enter；相同值只展开，异值仅一次 fill。
        if state.get("input_visible") is not True:
            evidence["preparation_problem"] = "keyword_input_not_visible"
            return state
        field = _input_control(page, state)
        if fresh:
            field.focus(timeout=budget.check(2000))
            field.fill("", timeout=budget.check(2000))
            for _ in range(25):
                cleared = _snapshot(page, budget, evidence.get("http_status"))
                if _block(cleared):
                    return cleared
                if cleared.get("keyword") == "" and not cleared.get("suggestions"):
                    break
                budget.pause(60)
            else:
                evidence["preparation_problem"] = "suggestion_reset_not_confirmed"
                return cleared
            field.fill(request["keyword"], timeout=budget.check(2000))
            evidence.update(suggestion_focus_attempts=1, keyword_fill_attempts=2,
                            keyword_input_completed=True, suggestion_input_action="clear_and_refill")
        elif state.get("keyword") == request["keyword"]:
            evidence["page_reused"] = True
            if not (state.get("suggestion_panel_associated") and state.get("suggestion_panel_rect")):
                # 聚焦即可展开的平台不需要坐标点击；仍检查页面阻塞与完整面板。
                phase("open_suggestions", "展开当前关键词下拉词")
                field.focus(timeout=budget.check(2000))
                evidence["suggestion_focus_attempts"] = 1
                until = min(budget.deadline, time.monotonic()+.75)
                while True:
                    focused = _snapshot(page, budget)
                    if _block(focused) or (focused.get("suggestion_panel_associated") and focused.get("suggestion_panel_rect")) or time.monotonic() >= until:
                        break
                    budget.pause(150)
                if _block(focused):
                    return focused
                if not (focused.get("suggestion_panel_associated") and focused.get("suggestion_panel_rect")):
                    # 对仅响应click的输入器使用正常中心点击，不force、不隐藏遮挡。
                    field.click(timeout=budget.check(3000))
                    evidence["suggestion_expand_attempts"] = 1
                budget.check()
        else:
            field.fill(request["keyword"], timeout=budget.check(2000))
            budget.check()
            evidence.update(keyword_input_completed=True, keyword_fill_attempts=1)
    ready = _settled(page, request, budget, evidence.get("http_status"))
    if ready.get("wait_reason"):
        evidence["preparation_problem"] = ready["wait_reason"]
    if request["capture_kind"] == "results" and not _block(ready) and not ready.get("wait_reason"):
        if (ready.get("scroll", {}).get("at_top") is not True
                or ready.get("scroll", {}).get("top", 0) > 1
                or ready.get("document", {}).get("scroll_y", 0) > 1):
            evidence["preparation_problem"] = "search_not_at_top"
    if request["capture_kind"] == "results" and ready.get("suggestion_panel_rect"):
        evidence["preparation_problem"] = "suggestion_panel_not_dismissed"
    return ready


def capture_from_page(page, request: dict, *, page_ref=None) -> dict:
    """同步采集调用方已选择的 Playwright Page，返回与 capture 同形的业务结果。

    必填 keyword/capture_kind/output_dir（绝对路径）；可选 top_n=20、
    timeout_seconds=180（最大1800）及 cancel_file。每次创建唯一子目录。
    此入口忽略 user_data_dir，不读取 profile/cookies，不创建或关闭任何浏览器对象，
    不改 viewport、超时默认值、页面样式或元素；应在拥有 page 的 worker 线程调用。

    每次使用独立总预算，所有动作前后及本地处理检查取消。Playwright evaluate/
    content 没有单次 timeout 参数；调用方常驻 worker 的外层监督负责驱动卡死时
    的硬截止和在途取消（仅断开自己连接，不关闭用户浏览器）。本函数不另起线程
    使用 page，也不以终止浏览器或重建会话实现取消。
    """
    started = time.monotonic()
    result = _base_result()
    try:
        # 不解析调用方普通 profile 路径，甚至不对该字段做 resolve/stat。
        req = validate_request({key: value for key, value in request.items() if key != "user_data_dir"}
                               if isinstance(request, dict) else request)
    except (ValueError, OSError, RuntimeError) as exc:
        result["reason"] = f"invalid_request: {exc}"
        return result
    result.update(capture_kind=req["capture_kind"], keyword=req["keyword"], requested_top_n=req["top_n"])
    budget = Budget(req["timeout_seconds"], req.get("cancel_file"), deadline=started+req["timeout_seconds"])
    evidence = dict(frames=[], fullpage_frames=[], artifact_role="diagnostic", rank_policy=RANK_POLICY_DETAIL,
                    evidence_source="caller_selected_page", navigation_attempts=0, keyword_fill_attempts=0,
                    suggestion_expand_attempts=0, keyword_input_completed=False, page_reused=False)
    run = None
    try:
        budget.check()
        run = Path(req["output_dir"]) / ("page-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex)
        run.mkdir(parents=True, exist_ok=False)
        result["output_dir"] = str(run)
        _checkpoint(run, result, evidence)
        active_page = page_ref if page_ref is not None else [page]
        state = _prepare_current_page(page, req, evidence, budget, active_page)
        page = active_page[0]
        evidence["prepared_snapshot"] = state
        budget.check()
        result["source_url"] = state.get("url", "")
        problem = evidence.get("preparation_problem") or _source_problem(state, req)
        if _block(state):
            result.update(status="blocked", reason=_block(state), outcome="blocked")
            evidence["blocked_snapshot"] = state
        elif problem:
            result.update(status="blocked" if problem == "keyword_input_not_visible" else "partial", reason=problem)
            diagnostic = run / "preparation-diagnostic.png"
            page.screenshot(path=str(diagnostic), full_page=False, timeout=budget.check(2000))
            result["source_artifact_paths"].append(str(diagnostic))
        elif req["capture_kind"] == "results" and _collect_fullpage(page, req, run, result, evidence, budget, state):
            pass
        else:
            # 全页未满足条件时才复用既有严格滚动算法，不将不足证明升级为成功。
            _collect(page, req, run, result, evidence, budget)
        budget.check()
    except CaptureCancelled:
        result.update(status="cancelled", reason="cancel_file_detected", rank_verified=False, outcome="cancelled")
    except TimeoutError:
        if result["status"] != "blocked":
            result.update(status="partial", reason="capture_total_deadline_exceeded", rank_verified=False, outcome="timeout")
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        if run is not None and time.monotonic() < budget.deadline and not (budget.cancel_file and budget.cancel_file.exists()):
            try:
                diagnostic = run / "failure-diagnostic.png"
                page.screenshot(path=str(diagnostic), full_page=False, timeout=budget.check(2000))
                result["source_artifact_paths"].append(str(diagnostic))
                evidence["failure_url"] = page.url
            except Exception:
                pass
        if budget.cancel_file and budget.cancel_file.exists():
            # 驱动抛错与用户取消同时发生时，仍优先报告本次取消，不继续页面动作。
            result.update(status="cancelled", reason="cancel_file_detected", rank_verified=False, outcome="cancelled")
        elif result["status"] != "blocked":
            # Playwright 自己的 TimeoutError 不继承内置 TimeoutError。
            timed_out = type(exc).__name__ == "TimeoutError" or time.monotonic() >= budget.deadline
            result.update(status="partial" if timed_out or evidence["frames"] or evidence["fullpage_frames"] else "failed",
                          reason="capture_total_deadline_exceeded" if time.monotonic() >= budget.deadline else evidence["error"],
                          rank_verified=False, outcome="timeout" if timed_out else "incomplete")
    finally:
        result["elapsed_seconds"] = round(time.monotonic()-started, 3)
        if run is not None and run.is_dir():
            try:
                # 只做本地登记，不在取消后重读页面或重拍；原图与派生图分别列出。
                for frame in evidence["fullpage_frames"] + evidence["frames"]:
                    if Path(frame["path"]).is_file():
                        if frame["path"] not in result["source_artifact_paths"]:
                            result["source_artifact_paths"].append(frame["path"])
                        for key in ("path", "html_path"):
                            if frame.get(key) and frame[key] not in result["artifact_paths"]:
                                result["artifact_paths"].append(frame[key])
                    if frame.get("full_page"):
                        metadata_path = run / "source-fullpage.json"
                        _write_json(metadata_path, frame)
                        result["artifact_paths"].append(str(metadata_path))
                candidate = evidence.get("derived_candidate_path")
                if candidate and Path(candidate).is_file() and candidate not in result["artifact_paths"]:
                    result["artifact_paths"].append(candidate)
                if result["status"] in ("cancelled", "failed") or result["outcome"] == "timeout":
                    result["primary_artifact_path"] = None
                    evidence["artifact_role"] = "diagnostic"
                _checkpoint(run, result, evidence)
            except OSError as exc:
                result.update(status="failed", reason=f"evidence_write_failed: {exc}", rank_verified=False)
        if run is None:
            result["finished_at"] = _now()
    return result


def open_login(profile_dir: str, *, timeout_seconds: float = 1800, cancel_file: str | None = None,
               output_dir: str | None = None, capture_preview: bool = False) -> dict:
    """显式打开可见专属持久会话，等待用户关闭；不自动输入凭据或宣称已登录。

    GUI 应在自己的工作线程调用，之后 capture 的 user_data_dir 使用同一个目录。
    IP 风险/验证码仍立即 blocked；关闭窗口只表示本次手动会话结束。
    """
    result = _base_result()
    try:
        if type(capture_preview) is not bool:
            raise ValueError("capture_preview 应为布尔值")
        req = validate_request(dict(keyword="手动登录", capture_kind="suggestions", top_n=1,
            user_data_dir=profile_dir, timeout_seconds=timeout_seconds, cancel_file=cancel_file,
            output_dir=output_dir or str(LOCAL_ROOT / "manual-login"), capture_login_preview=capture_preview))
        req["capture_login_preview"] = capture_preview
    except (ValueError, OSError, RuntimeError) as exc:
        result["reason"] = f"invalid_request: {exc}"
        return result
    result.update(capture_kind="login", keyword=None, outcome="manual_login")
    return _supervise(req, result, mode="login")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("此内部入口仅接受 --worker JOB_JSON")
    job_path = Path(sys.argv[2]).resolve()
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    gate = payload.get("start_gate")
    if gate:
        while not Path(gate).exists():
            if time.monotonic() >= payload["deadline"]:
                raise SystemExit("worker_start_gate_timeout")
            time.sleep(.025)
    _worker(payload["request"], job_path.parent, payload["result"], mode=payload["mode"], deadline=payload["deadline"])
