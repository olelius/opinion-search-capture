"""统一网页工作进程：专属有头/无头会话，或显式借用已有Chrome。

借用模式使用Chrome原生连接元数据，不读取Cookie/密码，不关闭用户浏览器；
专属模式仅管理本程序自己的持久会话，登录与采集共享目录，按任务串行复用。
"""
from __future__ import annotations

import atexit
import importlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse, unquote
from uuid import uuid4


PLATFORM_HOSTS = {
    "xiaohongshu": {"www.xiaohongshu.com", "xiaohongshu.com"},
    "bilibili": {"www.bilibili.com", "search.bilibili.com", "bilibili.com"},
}


def validate_browser_endpoint(value: str) -> str:
    """接受auto或显式本机CDP端点；拒绝外网、认证信息、查询串和任意路径。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("浏览器连接地址应为auto或本机调试地址")
    value = value.strip()
    if value == "auto":
        return value
    parsed = urlparse(value)
    try:
        local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        port = parsed.port
    except ValueError:
        local, port = False, None
    if (parsed.scheme not in ("http", "ws") or not local or port is None or not 1 <= port <= 65535
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("浏览器调试地址只允许明确端口的本机http/ws地址")
    if parsed.scheme == "http" and parsed.path not in ("", "/"):
        raise ValueError("HTTP调试地址只填写本机主机和端口")
    if parsed.scheme == "ws" and not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", parsed.path):
        raise ValueError("WebSocket调试地址应为/devtools/browser/连接标识")
    return value


def resolve_browser_endpoint(value: str) -> str:
    """按Chrome官方自动连接格式读取一个已知元数据文件；不枚举浏览器资料。"""
    value = validate_browser_endpoint(value)
    if value != "auto":
        return value
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise FileNotFoundError("chrome_connection_not_enabled")
    path = Path(root) / "Google/Chrome/User Data/DevToolsActivePort"
    try:
        with path.open("r", encoding="utf-8") as stream:
            text = stream.read(1025)
    except FileNotFoundError:
        raise FileNotFoundError("chrome_connection_not_enabled") from None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(text) > 1024 or len(lines) != 2 or not lines[0].isdigit():
        raise ValueError("chrome_connection_metadata_invalid")
    return validate_browser_endpoint(f"ws://127.0.0.1:{int(lines[0])}{lines[1]}")


def _result(request: dict, status: str, reason: str) -> dict:
    """无平台请求时仍返回明确业务状态，不伪造截图、时间或成功条数。"""
    from datetime import datetime
    return {**{key: request.get(key) for key in ("platform", "client", "capture_kind", "keyword")},
            "status": status, "reason": reason, "retryable": False, "rank_verified": False,
            "requested_top_n": request.get("top_n", 20), "observed_count": None,
            "artifact_paths": [], "source_artifact_paths": [], "primary_artifact_path": None,
            "captured_at": datetime.now().astimezone().isoformat(), "browser_connection_mode": request.get("browser_connection_mode", "existing_chrome")}


class _Session:
    """仅在子进程主线程使用Playwright，同一连接服务多个关键词及后续批次。"""

    def __init__(self):
        self.playwright = self.browser = None
        self.endpoint_key = None
        self.pages = {}
        self.dedicated = {}
        self.batch_keys = {}

    def disconnect(self):
        """停自己的驱动连接；禁止调用用户browser/context/page的close。"""
        for context, manager in self.dedicated.values():
            try:
                context.close()  # 仅本程序创建的专属会话，不是用户的既有Chrome。
            except Exception:
                pass
            finally:
                if manager is not None:
                    manager.__exit__(None, None, None)
        self.dedicated = {}
        playwright, self.playwright = self.playwright, None
        self.browser, self.endpoint_key, self.pages = None, None, {}
        self.batch_keys = {}
        if playwright is not None:
            playwright.stop()

    def page(self, request: dict):
        if request.get("browser_connection_mode") == "dedicated":
            return self._dedicated_page(request)
        key = validate_browser_endpoint(request.get("browser_endpoint", "auto"))
        if self.browser is None or self.endpoint_key != key or not self.browser.is_connected():
            self.disconnect()
            endpoint = resolve_browser_endpoint(key)
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            try:
                self.browser = self.playwright.chromium.connect_over_cdp(
                    endpoint, timeout=max(1, min(30000, int(request.get("timeout_seconds", 180) * 1000))))
            except Exception:
                self.disconnect()
                raise
            self.endpoint_key = key
        platform = request["platform"]
        hosts = PLATFORM_HOSTS[platform]
        current = self.pages.get(platform)
        if current is not None and not current.is_closed() and urlparse(current.url).hostname in hosts:
            return current
        candidates = [p for context in self.browser.contexts for p in context.pages
                      if not p.is_closed() and urlparse(p.url).hostname in hosts]
        if candidates:
            # 优先当前关键词的已有搜索页，不因获得连接而重新提交搜索。
            from urllib.parse import parse_qs
            keyword = request["keyword"]
            def same_query(page):
                values = parse_qs(urlparse(page.url).query).get("keyword", [])
                return len(values) == 1 and (values[0] == keyword or unquote(values[0]) == keyword)
            current = next((p for p in candidates if same_query(p)), candidates[0])
        else:
            # 不接管无关页面。仅在本批次明确请求的平台没有标签页时创建一个，
            # 后续关键词复用它，不新建浏览器/登录资料，也不关闭用户标签页。
            if not self.browser.contexts:
                raise RuntimeError("existing_browser_context_missing")
            current = self.browser.contexts[0].new_page()
        self.pages[platform] = current
        return current

    def _dedicated_page(self, request: dict):
        """复用程序自己的平台会话，保持登录与页面内存，不为每个词重启浏览器。"""
        from . import web_xiaohongshu as common
        platform = request["platform"]
        profile = Path(request["user_data_dir"]).resolve()
        headless = request.get("browser_headless", False)
        if type(headless) is not bool:
            raise ValueError("browser_headless 应为布尔值")
        key = (platform, str(profile).casefold(), headless)
        if self.browser is not None:  # 模式切换时只断开借用连接。
            self.disconnect()
        if self.playwright is None:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
        # 同一个profile切换有头/无头时先正常关闭自有上下文，避免目录争用和登录丢失。
        for old_key in list(self.dedicated):
            if old_key[:2] == key[:2] and old_key != key:
                old_context, old_manager = self.dedicated.pop(old_key)
                try:
                    old_context.close()
                finally:
                    if old_manager is not None:
                        old_manager.__exit__(None, None, None)
        if key in self.dedicated:
            context, manager = self.dedicated[key]
            if context.browser is not None and not context.browser.is_connected():
                if manager is not None:
                    manager.__exit__(None, None, None)
                del self.dedicated[key]
        if key not in self.dedicated:
            manager = None
            if platform == "xiaohongshu":
                manager = common._dedicated_profile(profile)
                manager.__enter__()
            else:
                from .web_bilibili import _prepare_profile
                _prepare_profile(profile)
            try:
                candidates = common._browser_paths(self.playwright)
                if not candidates:
                    raise FileNotFoundError("browser_not_installed")
                context = self.playwright.chromium.launch_persistent_context(
                    str(profile), executable_path=str(candidates[0]), headless=headless,
                    viewport={"width":1440,"height":1080}, locale="zh-CN",
                    timezone_id="Asia/Shanghai", device_scale_factor=1,
                    timeout=max(1,min(30000,int(request.get("timeout_seconds",180)*1000))))
                self.dedicated[key] = (context, manager)
            except Exception:
                if manager is not None:
                    manager.__exit__(None, None, None)
                raise
        context, _ = self.dedicated[key]
        candidates = [p for p in context.pages if not p.is_closed()]
        return candidates[0] if candidates else context.new_page()

    def capture(self, request: dict) -> dict:
        if request.get("client") != "WEB" or request.get("platform") not in PLATFORM_HOSTS:
            return _result(request, "failed", "existing_browser_request_invalid")
        flag = request.get("cancel_file")
        if flag and Path(flag).exists():
            return _result(request, "cancelled", "cancelled_before_browser_request")
        started = time.monotonic()
        from .progress import phase
        try:
            phase("browser_connection", "连接已有Chrome" if request.get("browser_connection_mode") == "existing_chrome"
                  else "准备无头浏览器" if request.get("browser_headless") else "准备专用浏览器")
            page = self.page(request)
            if request.get("browser_connection_mode") == "existing_chrome" or not request.get("browser_headless"):
                phase("activate_page", "激活目标网站标签页")
                page.bring_to_front()
            scope = (request["platform"], request.get("browser_connection_mode"), request.get("user_data_dir") or request.get("browser_endpoint"),
                     request.get("browser_headless", False))
            batch_id = request.get("batch_id")
            refreshed = False
            if batch_id and scope in self.batch_keys and self.batch_keys[scope] != batch_id:
                from urllib.parse import parse_qs
                url = urlparse(page.url)
                query = parse_qs(url.query).get("keyword", [])
                result_paths = {"/search_result", "/search_result_ai", "/all", "/video"}
                if (url.hostname in PLATFORM_HOSTS[request["platform"]] and url.path.rstrip("/") in result_paths
                        and len(query)==1 and (query[0]==request["keyword"] or unquote(query[0])==request["keyword"])):
                    # 新时段必须更新结果；批次内或同批次恢复不重复请求。
                    page.reload(wait_until="domcontentloaded", timeout=max(1,min(30000,int((request.get("timeout_seconds",180)-(time.monotonic()-started))*1000))))
                    refreshed = True
            if batch_id:
                self.batch_keys[scope] = batch_id
        except FileNotFoundError:
            return _result(request, "blocked", "browser_not_installed" if request.get("browser_connection_mode") == "dedicated" else "chrome_connection_not_enabled")
        except ValueError:
            return _result(request, "blocked", "chrome_connection_configuration_invalid")
        except Exception as error:
            return _result(request, "blocked", "chrome_connection_failed") | {"error_type": type(error).__name__}
        remaining = request.get("timeout_seconds", 180) - (time.monotonic() - started)
        if remaining <= 0:
            return _result(request, "partial", "browser_connection_deadline_exceeded")
        adapter = importlib.import_module("opinion_capture.web_" + request["platform"])
        phase("prepare_capture", "准备搜索与截图")
        if request["platform"] == "xiaohongshu":
            active = [page]
            result = adapter.capture_from_page(page, dict(request, timeout_seconds=remaining), page_ref=active)
            if active[0] is not page:
                if request.get("browser_connection_mode") == "dedicated":
                    # 只关闭程序自己的旧页面，保留本次新结果页供后续关键词/下拉复用。
                    page.close()
                else:
                    self.pages[request["platform"]] = active[0]
        else:
            result = adapter.capture_from_page(page, dict(request, timeout_seconds=remaining))
        result["browser_connection_mode"] = request.get("browser_connection_mode", "existing_chrome")
        result["browser_headless"] = bool(request.get("browser_headless")) if request.get("browser_connection_mode") == "dedicated" else False
        result["page_refreshed_for_new_batch"] = refreshed
        return result


def worker_main() -> int:
    """固定JSON行协议；先等父进程写首个请求，给Windows Job归属留下启动门闩。"""
    session = _Session()
    try:
        # 父进程固定发送UTF-8字节；冻结CLI的stdin可能采用Windows本地代码页。
        # 从二进制管道显式解码，避免中文路径的尾字节与反斜杠被错误组合。
        incoming = getattr(sys.stdin, "buffer", sys.stdin)
        for line in incoming:
            message = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
            request = message["request"]
            from .progress import phase_scope
            with phase_scope(request) as stages:
                try:
                    result = session.capture(request)
                except Exception as error:
                    result = _result(request, "failed", "existing_browser_capture_error: " + type(error).__name__)
            result["stage_timings"] = stages.timings
            print(json.dumps({"id": message["id"], "result": result}, ensure_ascii=False), flush=True)
    finally:
        session.disconnect()
    return 0


class _Controller:
    """可由GUI/调度线程复用的串行传输；超时仅结束本程序的工作进程树。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.process = self.job = self.log = None
        self.reader = None
        self.responses = queue.Queue()

    def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            try:
                process.stdin.close()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                if self.job is not None:
                    self.job.terminate()
                else:
                    process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # Job分配失败时也只回收已持有的自有根进程，不枚举/终止Chrome。
                    process.kill()
                    process.wait(timeout=3)
        if self.job is not None:
            self.job.close()
            self.job = None
        if self.reader is not None:
            self.reader.join(timeout=1)
            self.reader = None
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if self.log is not None:
            self.log.close()
            self.log = None

    def _start(self, request: dict):
        if self.process is not None and self.process.poll() is None:
            return
        self._stop()
        root = Path(request["output_dir"])
        root.mkdir(parents=True, exist_ok=True)
        self.log = (root / ("browser-worker-" + uuid4().hex[:8] + ".log")).open("ab")
        env = os.environ.copy()
        if getattr(sys, "frozen", False):
            command = [str(Path(sys.executable).with_name("OpinionCapture-CLI.exe")), "--existing-browser-worker"]
        else:
            executable = getattr(sys, "_base_executable", sys.executable)
            extra = [str(Path(__file__).resolve().parents[1]), str(Path(sys.prefix) / "Lib/site-packages")]
            if env.get("PYTHONPATH"):
                extra.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(extra)
            command = [executable, "-B", "-X", "utf8", "-m", "opinion_capture.existing_browser", "--worker"]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.log, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if os.name == "nt":
            from .sessions import _WindowsJob
            self.job = _WindowsJob()
            self.job.assign(self.process.pid)
        self.responses = queue.Queue()
        stream, responses = self.process.stdout, self.responses
        def receive():
            for line in iter(stream.readline, b""):
                responses.put(line)
            responses.put(None)
        self.reader = threading.Thread(target=receive, daemon=True, name="existing-chrome-results")
        self.reader.start()

    def capture(self, request: dict) -> dict:
        deadline = time.monotonic() + request.get("timeout_seconds", 180)
        flag = request.get("cancel_file")
        def interrupted():
            return bool(flag and Path(flag).exists())
        while not self.lock.acquire(timeout=.1):
            if interrupted() or time.monotonic() >= deadline:
                return _result(request, "cancelled" if interrupted() else "blocked", "browser_connection_busy")
        try:
            if interrupted():
                return _result(request, "cancelled", "cancelled_before_browser_request")
            self._start(request)
            identifier = uuid4().hex
            self.process.stdin.write((json.dumps({"id": identifier, "request": request}, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
            while True:
                if interrupted() or time.monotonic() >= deadline:
                    self._stop()
                    cancelled = interrupted()
                    result = _result(request, "cancelled" if cancelled else "partial",
                                     "cancel_requested" if cancelled else "browser_response_timeout")
                    result.update(retryable=not cancelled, outcome="cancelled" if cancelled else "timeout")
                    return result
                try:
                    line = self.responses.get(timeout=min(.1, max(.001, deadline-time.monotonic())))
                except queue.Empty:
                    continue
                if line is None:
                    raise RuntimeError("browser_worker_exited")
                response = json.loads(line)
                if response.get("id") != identifier or not isinstance(response.get("result"), dict):
                    raise RuntimeError("browser_worker_response_mismatch")
                return response["result"]
        except Exception as error:
            log_path = self.log.name if self.log is not None else None
            self._stop()
            result = _result(request, "failed", "browser_worker_error: " + type(error).__name__)
            result.update(worker_error=str(error), worker_log_path=log_path)
            if log_path:
                result["artifact_paths"].append(log_path)
            return result
        finally:
            self.lock.release()

    def close(self):
        with self.lock:
            self._stop()


_controller = None
_initialization_lock = threading.Lock()


def capture(request: dict) -> dict:
    """正式采集入口；仅首次任务创建后台连接，后续关键词和批次复用。"""
    global _controller
    with _initialization_lock:
        if _controller is None:
            _controller = _Controller()
            atexit.register(_controller.close)
    return _controller.capture(request)


def close_connection() -> None:
    """关闭程序自有连接，供退出/明确重新连接使用，不关闭Chrome。"""
    if _controller is not None:
        _controller.close()


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("仅支持内部 --worker 入口")
    raise SystemExit(worker_main())
