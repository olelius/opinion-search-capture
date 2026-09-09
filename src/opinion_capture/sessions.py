"""显式 WEB 手动会话：独立存储、与采集共锁，以及拥有进程树的同步回收。

导入和 profile_directory 都不创建目录或启动浏览器。open_web_login 是阻塞接口，
仅由 GUI 的显式用户操作在工作线程调用；这里不依赖 runner，避免循环导入。
"""

from __future__ import annotations

from datetime import datetime
import json
import math
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import threading
import time
from uuid import uuid4

from . import paths
from .locks import ResourceBusy, ResourceLock

PLATFORMS = ("bilibili", "xiaohongshu")
_POLL_SECONDS = .05
_CLOSE_GRACE_SECONDS = .5
_REAP_SECONDS = 5


def _disjoint(left: Path, right: Path) -> bool:
    """目录及其解析后的链接目标互不包含，避免导出根打包时带走会话。"""
    return not (left.is_relative_to(right) or right.is_relative_to(left))


def profile_directory(plan, platform: str) -> Path:
    """计算平台专属绝对路径，不创建/搬移/扫描会话；非法配置抛 ValueError。

    plan.browser_profile_root 可缺失或为 None；默认 EXE/仓库旁 .local/browser-profiles。
    平台内部标识固定为 bilibili/xiaohongshu，根目录与图片输出分别配置。
    """
    if platform not in PLATFORMS:
        raise ValueError("platform 必须为 bilibili 或 xiaohongshu")
    configured = getattr(plan, "browser_profile_root", None)
    root = Path(configured) if configured is not None else paths.application_root() / ".local" / "browser-profiles"
    if not root.is_absolute():
        raise ValueError("browser_profile_root 应为绝对路径")
    profile = (root / platform).resolve()
    lowered = profile.as_posix().casefold()
    if any(part in lowered for part in ("/google/chrome/user data", "/microsoft/edge/user data",
                                        "/mozilla/firefox/profiles")):
        raise ValueError("profile_not_dedicated")
    output = Path(plan.output_root).resolve()
    if not _disjoint(profile, output):
        raise ValueError("WEB 会话目录与图片导出目录应互不包含")
    return profile


def _stamp() -> str:
    """本地诊断采用带时区时间，持续时间另用 monotonic。"""
    return datetime.now().astimezone().isoformat()


def _write_json(path: Path, data: dict) -> None:
    """仅替换本次唯一诊断目录内的 JSON，避免父进程读取半份结果。"""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class _WindowsJob:
    """Windows Job 保留进程树所有权；根进程提前退出后仍可回收浏览器后代。

    先将 spawn 工作进程加入 Job 再放行门闩。只操作自身句柄，不扫描进程名，
    KILL_ON_JOB_CLOSE 还覆盖监督进程意外退出。
    """

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class BasicLimits(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
                        ("min_working_set", c.c_size_t), ("max_working_set", c.c_size_t),
                        ("process_limit", w.DWORD), ("affinity", c.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IOCounters(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ("read_ops", "write_ops", "other_ops",
                                                       "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(c.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IOCounters),
                        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                        ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t)]

        class Accounting(c.Structure):
            _fields_ = [(name, c.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, w.DWORD) for name in ("page_faults", "total", "active", "terminated")]

        self.c, self.accounting_type = c, Accounting
        self.api = c.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [c.c_void_p, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
            ("QueryInformationJobObject", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            ("CloseHandle", [w.HANDLE], w.BOOL),
        ):
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        try:
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self._check(self.api.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)))
        except BaseException:
            self.api.CloseHandle(self.handle)
            raise

    def _check(self, ok):
        """原样报告系统调用失败；没有成功归属就不打开启动门闩。"""
        if not ok:
            raise self.c.WinError(self.c.get_last_error())

    def attach(self, process):
        """multiprocessing 的 Windows sentinel 就是该直接子进程的句柄。"""
        self._check(self.api.AssignProcessToJobObject(self.handle, process.sentinel))

    def assign(self, pid: int) -> None:
        """将已创建、尚未放行启动门闩的 PID 加入 Job；失败抛 OSError。"""
        handle = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        self._check(handle)
        try:
            self._check(self.api.AssignProcessToJobObject(self.handle, handle))
        finally:
            self.api.CloseHandle(handle)

    def terminate(self) -> None:
        """同步终止全部成员并验证 active=0；保留 Job 句柄，重复调用有效。"""
        if not self.handle:
            return
        handles = self._member_handles()
        try:
            self._check(self.api.TerminateJobObject(self.handle, 1))
            deadline = time.monotonic() + _REAP_SECONDS
            # active=0 可能先于进程句柄 signaled；同步等待已拥有成员的真实退出。
            for handle in handles:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                if self.api.WaitForSingleObject(handle, remaining_ms) != 0:
                    raise TimeoutError("session_job_member_cleanup_timeout")
            while True:
                info = self.accounting_type()
                self._check(self.api.QueryInformationJobObject(
                    self.handle, 1, self.c.byref(info), self.c.sizeof(info), None))
                if not info.active:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("session_job_cleanup_timeout")
                time.sleep(.01)
        finally:
            for handle in handles:
                self.api.CloseHandle(handle)

    def _member_handles(self) -> list:
        """仅枚举本 Job 的成员并保留等待句柄，绝不按浏览器名称扫描系统。"""
        count, handles = 32, []
        try:
            while True:
                buffer = self.c.create_string_buffer(8 + self.c.sizeof(self.c.c_size_t) * count)
                if self.api.QueryInformationJobObject(self.handle, 3, buffer, len(buffer), None):
                    break
                if self.c.get_last_error() != 234:  # ERROR_MORE_DATA
                    self._check(False)
                count = max(count * 2, self.c.c_uint32.from_buffer(buffer).value)
            listed = self.c.c_uint32.from_buffer(buffer, 4).value
            for index in range(listed):
                pid = self.c.c_size_t.from_buffer(buffer, 8 + index * self.c.sizeof(self.c.c_size_t)).value
                handle = self.api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                if handle:
                    handles.append(handle)
                elif self.c.get_last_error() != 87:  # 成员已退出，ERROR_INVALID_PARAMETER
                    self._check(False)
            return handles
        except BaseException:
            for handle in handles:
                self.api.CloseHandle(handle)
            raise

    def close(self) -> None:
        """幂等收口：先 terminate 确认回收，再关闭句柄；任何返回路径都应调用。"""
        if not self.handle:
            return
        try:
            self.terminate()
        finally:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _stop_owned(process, job) -> None:
    """同步回收自己的工作进程及进程树，返回时才允许释放 profile 锁。"""
    try:
        if job is not None:
            job.close()
        elif process.pid is not None and os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # 尚未 setsid，或该进程组已正常消失。
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.kill()
            process.join(_REAP_SECONDS)
            if process.is_alive():
                raise TimeoutError("session_worker_cleanup_timeout")
            process.close()


def _supervise_login(payload: dict, cancel_event) -> dict:
    """总预算覆盖启动/页面/关闭；事件在本循环直接桥接文件，无额外守护线程。

    使用标准库 spawn 而非 sys.executable -m：Windows venv 由 CPython 绕过
    redirector；冻结入口需在 GUI 初始化前调用 multiprocessing.freeze_support()。
    """
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    process = context.Process(target=_worker_entry, args=(gate, payload), name="web-manual-session")
    job = None
    interruption = None
    try:
        job = _WindowsJob() if os.name == "nt" else None
        process.start()
        if job is not None:
            job.assign(process.pid)
        gate.set()
        while process.is_alive():
            if cancel_event.is_set():
                interruption = "cancelled"
            elif time.monotonic() >= payload["deadline"]:
                interruption = "timeout"
            if interruption:
                Path(payload["cancel_file"]).touch(exist_ok=True)
                # 给正常 context.close 一个短窗口；卡住仍由 Job/进程组硬回收。
                process.join(_CLOSE_GRACE_SECONDS)
                break
            process.join(min(_POLL_SECONDS, max(0, payload["deadline"] - time.monotonic())))
        if cancel_event.is_set():
            interruption = "cancelled"
        elif time.monotonic() >= payload["deadline"]:
            interruption = interruption or "timeout"
        exitcode = process.exitcode
    finally:
        _stop_owned(process, job)
    if interruption:
        return {"status": "cancelled" if interruption == "cancelled" else "failed", "outcome": interruption,
                "reason": "manual_session_cancelled" if interruption == "cancelled" else "manual_session_timeout"}
    result_file = Path(payload["run"]) / "worker-result.json"
    if exitcode != 0 or not result_file.is_file():
        return {"status": "failed", "outcome": "incomplete", "reason": "manual_session_worker_exited_without_completion"}
    return json.loads(result_file.read_text(encoding="utf-8"))


def _bilibili_login(payload: dict) -> dict:
    """只打开真实首页供手动操作；不填凭据、不检查 Cookie，不据关闭判断认证。"""
    from . import web_bilibili as adapter
    from . import web_xiaohongshu as common
    from playwright.sync_api import sync_playwright

    profile = Path(payload["profile_dir"])
    budget = common.Budget(payload["timeout_seconds"], payload["cancel_file"], deadline=payload["deadline"])
    budget.check()
    adapter._prepare_profile(profile)
    with sync_playwright() as playwright:
        explicit = os.environ.get("OPINION_CAPTURE_BROWSER")
        if explicit is not None and (not Path(explicit).is_absolute() or not Path(explicit).is_file()):
            raise ValueError("OPINION_CAPTURE_BROWSER 应为有效绝对文件路径")
        candidates = [Path(explicit)] if explicit is not None else common._browser_paths(playwright)
        if not candidates or not candidates[0].is_file():
            raise RuntimeError("browser_not_installed")
        context = playwright.chromium.launch_persistent_context(
            str(profile), executable_path=str(candidates[0]), headless=False,
            viewport={"width": 1440, "height": 1000}, locale="zh-CN", timezone_id="Asia/Shanghai",
            accept_downloads=False, downloads_path=str(Path(payload["run"]) / "downloads"),
            timeout=budget.check())
        closed = threading.Event()
        context.on("close", lambda *_: closed.set())
        verified = False
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=budget.check())
            while not closed.is_set() and context.pages:
                budget.check()
                # Playwright 短周期等待负责分发 close 事件；普通 time.sleep 不分发驱动事件。
                page = context.pages[0]
                try:
                    if adapter.authenticated_navigation(page):
                        verified = True
                        break
                    page.wait_for_timeout(min(100, budget.check()))
                except Exception as exc:
                    if "Execution context was destroyed" in str(exc) or "Cannot find context with specified id" in str(exc):
                        continue
                    if not closed.is_set() and not page.is_closed():
                        raise
            return {"status":"success", "outcome":"authenticated_session_ready" if verified else "manual_session_closed",
                    "reason":"authenticated_navigation_visible" if verified else "manual_window_closed_authentication_not_asserted",
                    "authentication_verified":verified, "session_persisted":verified}
        except Exception:
            if closed.is_set() or not context.pages:
                return {"status": "success", "outcome": "manual_session_closed",
                        "reason": "manual_window_closed_authentication_not_asserted"}
            raise
        finally:
            try:
                context.close()
            except Exception as exc:
                if not closed.is_set() and "Target page, context or browser has been closed" not in str(exc):
                    raise


class _CancelBridge:
    """仅桥接取消/总期限文件；退出时同步 join，本线程从不运行浏览器逻辑。"""

    def __init__(self, event, path: Path, deadline: float):
        self.event, self.path, self.deadline = event, path, deadline
        self.done = threading.Event()
        self.interruption = None
        self.error = None
        self.thread = threading.Thread(target=self._watch, name="web-session-cancel-bridge")

    def _watch(self):
        """使用已有的本次诊断目录，不创建/改动用户 profile。"""
        while not self.done.is_set():
            if self.event.is_set():
                self.interruption = "cancelled"
            elif time.monotonic() >= self.deadline:
                self.interruption = "timeout"
            if self.interruption:
                try:
                    self.path.touch(exist_ok=True)
                except OSError as exc:
                    self.error = exc
                return
            self.done.wait(min(_POLL_SECONDS, max(0, self.deadline - time.monotonic())))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.thread.join()


def _run_login(payload: dict, event) -> dict:
    """小红书只复用已有监督入口，B站使用本模块的专属进程树监督。"""
    if event.is_set():
        return {"status": "cancelled", "outcome": "cancelled", "reason": "manual_session_cancelled_before_start"}
    remaining = payload["deadline"] - time.monotonic()
    if remaining <= 0:
        return {"status": "failed", "outcome": "timeout", "reason": "manual_session_timeout"}
    if payload["platform"] == "bilibili":
        return _supervise_login(payload, event)
    from .web_xiaohongshu import open_login
    with _CancelBridge(event, Path(payload["cancel_file"]), payload["deadline"]) as bridge:
        response = open_login(payload["profile_dir"], timeout_seconds=remaining,
                              cancel_file=payload["cancel_file"], output_dir=payload["run"])
    interruption = "cancelled" if event.is_set() else bridge.interruption
    if not interruption and time.monotonic() >= payload["deadline"]:
        interruption = "timeout"
    if interruption:
        response = {"status": "cancelled" if interruption == "cancelled" else "failed",
                    "outcome": interruption, "reason": "manual_session_" + interruption}
    if bridge.error:
        raise OSError(f"session_cancel_bridge_failed: {bridge.error}")
    return response


def _worker_entry(gate, payload: dict) -> None:
    """spawn 顶层入口；门闩放行前不导入适配器、不创建浏览器或其后代。"""
    if os.name != "nt":
        os.setsid()
    gate.wait()
    run = Path(payload["run"])
    temporary = run / "driver-temp"
    temporary.mkdir()
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(temporary)  # 仅改变此私有子进程的环境。
    with (run / "worker.log").open("a", encoding="utf-8", buffering=1) as log:
        previous = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = log
        try:
            result = _bilibili_login(payload)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            result = {"status": "failed", "outcome": "incomplete", "reason": f"{type(exc).__name__}: {exc}"}
        finally:
            sys.stdout, sys.stderr = previous
        _write_json(run / "worker-result.json", result)


def open_web_login(plan, platform: str, *, cancel_event=None, timeout_seconds=1800) -> dict:
    """GUI 后台显式打开可见专属浏览器，阻塞至关闭/取消/期限或启动失败。

    返回 status/reason/outcome、profile_dir、diagnostic_dir/log_path、busy、
    authentication_verified/session_persisted 和起止/耗时；只按实际页面认证证据报告已登录。
    不输出 Cookie、storage_state 或图片导出清单；旧会话保持原位置和内容。
    """
    started = time.monotonic()
    event = cancel_event if cancel_event is not None else threading.Event()
    result = dict(platform=platform, client="WEB", status="failed", outcome="incomplete", reason="invalid_request",
                  busy=False, authentication_verified=False, session_persisted=False, profile_dir=None, diagnostic_dir=None, log_path=None,
                  started_at=_stamp())
    run = None
    try:
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 1800:
            raise ValueError("timeout_seconds 应在 (0, 1800] 范围")
        profile = profile_directory(plan, platform)
        result["profile_dir"] = str(profile)
        if event.is_set():
            result.update(status="cancelled", outcome="cancelled", reason="manual_session_cancelled_before_start")
        else:
            # 登录入口只在用户显式操作时调用；先释放程序自己的空闲采集会话，
            # 让同一profile可重新登录。借用Chrome只断开连接，不关闭用户浏览器。
            from .existing_browser import close_connection
            close_connection()
            log_root = (paths.application_root() / ".local" / "session-log").resolve()
            if not _disjoint(log_root, Path(plan.output_root).resolve()) or not _disjoint(log_root, profile):
                raise ValueError("会话诊断目录应与图片导出及 profile 目录互不包含")
            with ResourceLock("browser-profile:" + str(profile).casefold(), timeout=0, cancel_event=event):
                if event.is_set():
                    result.update(status="cancelled", outcome="cancelled", reason="manual_session_cancelled_before_start")
                else:
                    run = log_root / (platform + "-" + uuid4().hex)
                    run.mkdir(parents=True, exist_ok=False)
                    result.update(diagnostic_dir=str(run), log_path=str(run / "session.json"))
                    payload = dict(platform=platform, profile_dir=str(profile), run=str(run),
                                   cancel_file=str(run / "cancel.request"), timeout_seconds=float(timeout_seconds),
                                   deadline=started + timeout_seconds)
                    response = _run_login(payload, event)
                    # 只取生命周期字段，适配器的页面/诊断证据留在本地，不当作图片导出。
                    result.update({key: response[key] for key in ("status", "reason", "outcome") if key in response})
                    if response.get("authentication_verified") is True and response.get("status") == "success":
                        result["authentication_verified"] = True
                        # 正常worker返回前已关闭自有persistent context并落盘；与计划JSON是否保存无关。
                        result["session_persisted"] = response.get("session_persisted", response.get("outcome") == "authenticated_session_ready") is True
                    if response.get("outcome") == "manual_session_closed":
                        result.update(status="success", reason="manual_window_closed_authentication_not_asserted")
    except ResourceBusy:
        result.update(status="cancelled" if event.is_set() else "blocked", busy=not event.is_set(),
                      outcome="cancelled" if event.is_set() else "busy", reason="browser_profile_busy" if not event.is_set() else "manual_session_cancelled")
    except Exception as exc:
        result.update(status="failed", outcome="incomplete", reason=f"{type(exc).__name__}: {exc}")
    result.update(finished_at=_stamp(), elapsed_seconds=round(time.monotonic() - started, 3))
    if run is not None:
        try:
            _write_json(run / "session.json", result)
        except OSError as exc:
            result.update(status="failed", outcome="incomplete", reason=f"session_log_write_failed: {exc}")
    return result
