"""桌面界面的只读桥接、表格模型和后台工作线程；不在此实现采集业务。"""

from __future__ import annotations

import copy
import inspect
import math
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QThread, Qt, Signal


EXISTING_CHROME_GUIDANCE = (
    "Chrome 144 及以上：请在已打开的 Chrome 中手动进入 "
    "chrome://inspect/#remote-debugging 开启远程调试，并在 Chrome 自身弹窗中授权连接。"
    "此处仅保存连接配置，不修改 Chrome 设置、不访问 profile/cookies、不启动浏览器，也不探测连接；"
    "实际连接由执行后端处理。"
)


STATUS_LABELS = {
    "pending": "待执行", "running": "执行中", "success": "成功", "partial": "未完整验收",
    "blocked": "受阻，需处理", "failed": "失败", "error": "执行异常", "cancelled": "已取消",
    "canceled": "已取消", "cancelling": "正在取消", "stopping": "正在停止", "stopped": "已停止",
    "scheduled": "等待计划时刻", "idle": "空闲", "skipped": "已跳过", "interrupted": "执行中断",
    "missing_status": "未返回状态", "completed": "已完成", "retrying": "正在重试",
    "queued": "等待执行", "missed": "已错过计划", "waiting": "等待中", "busy": "资源正在使用",
}

PARTIAL_EXPLANATION = (
    "未完整验收不等于截图成功，请核对最终图片和批次报告。"
    "无最终图片由核心归为失败；旧 partial 记录不保证已有图片。"
)


def phase_progress_text(event: dict) -> str:
    """分别显示本次采集累计和当前阶段耗时；不计作跨重试或整个批次时长。"""
    phase = event.get("phase_text") or event.get("phase_code") or "等待阶段说明"
    timings = []
    for field, label in (("elapsed_seconds", "本次采集累计"), ("phase_elapsed_seconds", "当前阶段")):
        elapsed = event.get(field)
        timings.append(f"{label}耗时 {elapsed:.1f} 秒"
                       if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0
                       else f"{label}耗时待上报")
    keyword = f" · 关键词：{event['keyword']}" if event.get("keyword") else ""
    return f"当前阶段：{phase} · {' · '.join(timings)}（核心上报）{keyword}"


def result_detail_text(result: dict) -> str:
    """合并核心的成品说明和原因，仅做展示，不由成品标记反推或覆盖任务状态。"""
    details = []
    if result.get("completion_detail"):
        details.append(str(result["completion_detail"]))
    if result.get("has_deliverable") is True:
        details.append("已有最终截图，完整性以任务状态和报告为准")
    elif result.get("has_deliverable") is False and not details:
        details.append("未产生最终截图")
    if result.get("reason") and str(result["reason"]) not in details:
        details.append(str(result["reason"]))
    return "；".join(details)


def login_result_text(result: dict) -> str:
    """分别解释认证与会话持久化；正常关闭不推定登录成功，也不推定会话丢失。"""
    code = result.get("status", "missing_status")
    verified = result.get("authentication_verified") is True
    persisted = result.get("session_persisted")
    if verified:
        title = "平台登录已确认；" + (
            "浏览器会话已保存，可开始截图" if persisted is True else
            "浏览器会话保存未成功，请查看核心说明" if persisted is False else
            "浏览器会话保存结果待确认（核心未返回持久化标记）"
        )
    elif result.get("outcome") == "manual_session_closed" or code in (
        "success", "completed", "manual_session_closed", "closed"
    ):
        title = "浏览器会话已保留，登录未确认；关闭窗口不代表登录成功"
    elif code in ("cancelled", "canceled"):
        title = "网页登录已取消，登录未确认"
    else:
        title = f"网页登录返回 · {status_text(code)}；登录未确认"
    if not verified and persisted is True:
        title += "；核心已确认浏览器会话保存，但尚未确认平台认证"
    reason = result.get("reason")
    # 旧核心的此原因只代表关闭窗口；结构化认证标记优先，原始原因仍保留在 JSON。
    if reason and reason != "manual_window_closed_authentication_not_asserted":
        title += f"\n核心说明：{reason}"
    return title + "\n网页登录不自动保存采集计划配置；如需保留表单修改，请点击“保存配置”。"


def status_text(value: object) -> str:
    """未知状态不猜测成功，保留内部代码便于定位。"""
    code = str(value or "missing_status")
    return STATUS_LABELS.get(code, f"未知状态（{code}）")


def invoke_supported(function: Callable, *args, **kwargs):
    """按签名传递扩展参数；不捕获后重试 TypeError，避免执行器被重复调用。"""
    signature = inspect.signature(function)
    variadic = any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values())
    accepted = kwargs if variadic else {k: v for k, v in kwargs.items() if k in signature.parameters}
    return function(*args, **accepted)


def core_preflight(data: dict) -> dict:
    """仅调用核心导出预检，预检不得创建目录、图片或占用采集设备。"""
    from .config import Plan
    from .export_plan import preflight_plan
    return preflight_plan(Plan.from_dict(data))


def core_schedule(data: dict, *, progress_callback=None):
    """在调度工作线程内构建服务；GUI 不拥有定时计算或补跑策略。"""
    from .config import Plan
    from .service import ScheduleService
    return ScheduleService(Plan.from_dict(data), progress_callback=progress_callback)


def core_devices() -> list[dict]:
    """只枚举已有 Android 设备；由核心封装实际设备发现。"""
    from .android import discover_devices
    return discover_devices()


def core_login(data: dict, platform: str, *, cancel_event=None, timeout_seconds=1800) -> dict:
    """专属模式才导入登录会话层；复用模式仅返回 Chrome 手动授权说明。"""
    from .config import Plan
    plan = Plan.from_dict(data)
    if plan.browser_connection_mode == "existing_chrome":
        return {"status": "blocked", "reason": EXISTING_CHROME_GUIDANCE}
    from .sessions import open_web_login
    return open_web_login(plan, platform, cancel_event=cancel_event,
                          timeout_seconds=timeout_seconds)


def core_history(output_root: str) -> list[dict]:
    """通过只读核心接口读取历史；未产生数据库时返回空列表，不新建数据库。"""
    from .storage import list_batches
    return list_batches(output_root, limit=200)


def task_rows(data: dict) -> list[dict]:
    """构造冻结计划的任务显示元数据，不分配任何逐行控件。"""
    return [dict(platform=p, client=c, capture_kind=k, keyword=w, status="pending")
            for p in data["platforms"] for c in data["clients"]
            for w in data["keywords"] for k in data["capture_kinds"]]


class RecordModel(QAbstractTableModel):
    """轻量字典行表格；所有更新由 GUI 线程槽函数完成。"""

    def __init__(self, columns: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows: list[dict] = []
        self._index: dict[tuple, int] = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        field = self.columns[index.column()][0]
        value = row.get(field, "")
        if field == "reason":
            return result_detail_text(row)
        if field == "status":
            if role == Qt.ItemDataRole.ToolTipRole and value == "partial":
                return status_text(value) + "：" + PARTIAL_EXPLANATION
            return status_text(value)
        if field == "platform":
            return {"xiaohongshu": "小红书", "bilibili": "哔哩哔哩"}.get(value, str(value))
        if field == "capture_kind":
            return {"results": "搜索结果", "suggestions": "下拉词"}.get(value, str(value))
        return str(value) if value is not None else "—"

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section][1] if orientation == Qt.Orientation.Horizontal else str(section + 1)
        return None

    @staticmethod
    def identity(row: dict) -> tuple:
        """以平台/端/类型/关键词完整身份匹配，避免关键词前缀误归属。"""
        return tuple(row.get(k) for k in ("platform", "client", "capture_kind", "keyword"))

    def replace(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = copy.deepcopy(rows)
        self._index = {self.identity(row): i for i, row in enumerate(self.rows)}
        self.endResetModel()

    def update_task(self, row: dict) -> None:
        """单行增量更新；没有任务身份的批次消息不插入空白任务。"""
        key = self.identity(row)
        if any(part is None for part in key):
            task_id = row.get("task_id")
            index = next((i for i, r in enumerate(self.rows) if task_id and r.get("task_id") == task_id), None)
        else:
            index = self._index.get(key)
        if index is None:
            if any(part is None for part in key):
                return
            index = len(self.rows)
            self.beginInsertRows(QModelIndex(), index, index)
            self.rows.append(copy.deepcopy(row))
            self._index[key] = index
            self.endInsertRows()
        else:
            self.rows[index].update(copy.deepcopy(row))
            self.dataChanged.emit(self.index(index, 0), self.index(index, len(self.columns) - 1))


class ExecutionThread(QThread):
    """单次执行只接收快照；取消是协作事件，线程真正退出才发 finished。"""

    result_ready = Signal(object)
    progress = Signal(object)

    def __init__(self, data: dict, runner: Callable, parent=None):
        super().__init__(parent)
        self.data = copy.deepcopy(data)
        self.runner = runner
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            result = invoke_supported(self.runner, self.data, cancel_event=self.cancel_event,
                                      progress_callback=lambda event: self.progress.emit(copy.deepcopy(event)))
            if not isinstance(result, dict):
                raise TypeError("核心返回值须为字典，包含 status/reason")
        except Exception as exc:
            result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        self.result_ready.emit(copy.deepcopy(result))


class ReadThread(QThread):
    """预检/设备发现/历史查询使用短工作线程，避免 I/O 阻塞窗口。"""

    result_ready = Signal(object)

    def __init__(self, callback: Callable, parent=None):
        super().__init__(parent)
        self.callback = callback

    def run(self):
        try:
            result = {"ok": True, "value": self.callback()}
        except Exception as exc:
            result = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        self.result_ready.emit(result)


class ScheduleThread(QThread):
    """服务创建/start/stop 均在后台；退出前等待服务明确不再运行。"""

    progress = Signal(object)
    result_ready = Signal(object)
    service_started = Signal()

    def __init__(self, data: dict, factory: Callable, parent=None):
        super().__init__(parent)
        self.data = copy.deepcopy(data)
        self.factory = factory
        self.stop_event = threading.Event()
        self.cancel_running = threading.Event()

    def stop(self, *, cancel_running: bool = False) -> None:
        if cancel_running:
            self.cancel_running.set()
        self.stop_event.set()

    def run(self):
        service = None
        result = {"status": "stopped", "reason": "计划服务已退出"}
        try:
            service = self.factory(self.data, progress_callback=lambda e: self.progress.emit(copy.deepcopy(e)))
            if self.stop_event.is_set():
                return
            service.start()
            self.service_started.emit()
            requested = False
            cancelled = False
            while service.is_running:
                if self.stop_event.is_set() and (not requested or self.cancel_running.is_set() and not cancelled):
                    cancelled = self.cancel_running.is_set()
                    service.stop(wait=False, cancel_running=cancelled)
                    requested = True
                self.msleep(80)
            if getattr(service, "last_error", None):
                result = {"status": "error", "reason": f"计划因异常退出：{service.last_error}"}
        except Exception as exc:
            result = {"status": "error", "reason": f"计划服务异常：{type(exc).__name__}: {exc}"}
        finally:
            # start 部分成功或 stop 抛错时也不丢弃活跃服务，不销毁仍在工作的线程。
            if service is not None and service.is_running:
                try:
                    service.stop(wait=False, cancel_running=True)
                except Exception as exc:
                    self.progress.emit({"status": "error", "reason": f"停止服务出错，仍在等待退出：{exc}"})
                while service.is_running:
                    self.msleep(100)
            self.result_ready.emit(result)
