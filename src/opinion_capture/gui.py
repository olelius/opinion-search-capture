"""中文桌面配置界面：表单/JSON 独立，采集核心仅在手动执行时延迟导入。"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTime, Qt, Slot, QUrl, QTimer, QSortFilterProxyModel
from PySide6.QtGui import QCloseEvent, QFont, QFontDatabase, QFontMetrics, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QDoubleSpinBox, QTableWidget,
    QTimeEdit, QVBoxLayout, QWidget, QComboBox, QTabWidget, QTableView,
    QProgressBar, QSystemTrayIcon, QMenu, QStyle, QAbstractItemView,
)

from .gui_helpers import (
    ExecutionThread, ScheduleThread, ReadThread, RecordModel, core_preflight,
    core_schedule, core_history, core_devices, core_login, invoke_supported, status_text, task_rows,
    EXISTING_CHROME_GUIDANCE, PARTIAL_EXPLANATION, login_result_text, phase_progress_text, result_detail_text,
)
from .paths import application_root, default_config_path, default_output_root


PROJECT_ROOT = application_root()
OPTIONS = {
    "platforms": [("xiaohongshu", "小红书"), ("bilibili", "哔哩哔哩")],
    "clients": [("APP", "APP"), ("WEB", "WEB")],
    "capture_kinds": [("results", "搜索结果"), ("suggestions", "下拉词")],
}


def ensure_application_font() -> None:
    """Windows offscreen 插件可能不枚举系统字体；缺失时显式加载本机中文字体。"""
    app = QApplication.instance()
    if app is None or QFontMetrics(app.font()).inFontUcs4(ord("中")):
        return
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    if font_path.is_file():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont(families[0], 10))


def default_plan_data() -> dict:
    """返回独立的初始表单配置；根目录固定在项目内，不创建任何采集产物。"""
    return {
        "name": "默认采集计划", "project": "舆情监测", "keywords": [],
        "platforms": ["xiaohongshu", "bilibili"], "clients": ["APP", "WEB"],
        "capture_kinds": ["results", "suggestions"], "top_n": 20,
        "output_root": str(default_output_root()),
        "directory_template": "{project}/{platform}/{client}/{keyword}",
        "filename_template": "{keyword}_{content_label}_{batch:02d}_{capture_stamp}_{artifact_id}.png",
        "composite_directory_template": "{project}-{year}-{month}月/{platform}/{month:02d}{day:02d}-{display_type}-{client}/汇总",
        "composite_filename_template": "{content_label}_{batch:02d}_汇总_{artifact_id}.png",
        "composite_columns": 4,
        "browser_connection_mode": "dedicated", "browser_endpoint": "auto",
        "browser_headless": False,
        "schedule": {"windows": [{"start": "09:00", "end": "21:00", "interval_minutes": 60}], "times": []},
    }


def validate_plan_data(data: dict) -> dict:
    """校验基础表单结构并深拷贝；模板字段语义与调度冲突留给核心校验。"""
    if not isinstance(data, dict):
        raise ValueError("配置 JSON 顶层须为对象")
    result = copy.deepcopy(data)
    # 旧配置补齐拼图和浏览器连接默认值；已保存的自定义值不被覆盖。
    defaults = default_plan_data()
    for field in ("composite_directory_template", "composite_filename_template", "composite_columns",
                  "browser_connection_mode", "browser_endpoint", "browser_headless"):
        result.setdefault(field, defaults[field])
    from .config import validate_browser_connection
    validate_browser_connection(result["browser_connection_mode"], result["browser_endpoint"])
    if type(result["browser_headless"]) is not bool:
        raise ValueError("后台无头采集 browser_headless 须为布尔值 true 或 false")
    for field, label in (("name", "计划名称"), ("project", "项目名称"),
                         ("output_root", "导出根目录"), ("directory_template", "目录模板"),
                         ("filename_template", "原图文件名模板"),
                         ("composite_directory_template", "拼图目录模板"),
                         ("composite_filename_template", "拼图文件名模板")):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise ValueError(f"{label}须填写文本")
    for field in ("filename_template", "composite_filename_template"):
        if not result[field].lower().endswith(".png") or any(char in result[field] for char in "/\\"):
            raise ValueError("原图和拼图文件名模板均须为单个 .png 文件名")
    columns = result["composite_columns"]
    if type(columns) is not int or not 1 <= columns <= 100:
        raise ValueError("每行关键词数须为 1 至 100 的整数")
    if not Path(result["output_root"]).is_absolute():
        raise ValueError("导出根目录须为绝对路径")
    profile_root = result.get("browser_profile_root")
    if profile_root is not None and (not isinstance(profile_root, str) or not Path(profile_root).is_absolute()):
        raise ValueError("专属浏览器会话目录须为绝对路径，或留空使用默认值")
    keywords = result.get("keywords")
    if not isinstance(keywords, list) or not keywords or any(
        not isinstance(word, str) or not word.strip() for word in keywords
    ):
        raise ValueError("请填写至少一个关键词，每行一个")
    if any(any(ord(char) < 32 for char in word) for word in keywords):
        raise ValueError("关键词中请勿嵌入控制字符")
    if len(set(keywords)) != len(keywords):
        raise ValueError("关键词存在完全相同的重复项")
    for field, choices in OPTIONS.items():
        values = result.get(field)
        allowed = {value for value, _ in choices}
        if (not isinstance(values, list) or not values
                or any(not isinstance(value, str) or value not in allowed for value in values)
                or len(values) != len(set(values))):
            raise ValueError(f"{field} 须至少选择一项，且仅包含支持的非重复值")
    top_n = result.get("top_n")
    if type(top_n) is not int or not 1 <= top_n <= 1000:
        raise ValueError("TOP N 须为 1 至 1000 的整数")
    schedule = result.get("schedule")
    if not isinstance(schedule, dict):
        raise ValueError("schedule 须为对象")
    windows, times = schedule.get("windows"), schedule.get("times")
    if not isinstance(windows, list) or not isinstance(times, list):
        raise ValueError("计划窗口和固定时刻须为列表")

    def check_time(value: object) -> None:
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("时间格式须为 HH:mm，例如 09:00")

    for window in windows:
        if not isinstance(window, dict):
            raise ValueError("每个计划窗口须为对象")
        check_time(window.get("start"))
        check_time(window.get("end"))
        interval = window.get("interval_minutes")
        if type(interval) is not int or not 1 <= interval <= 1440:
            raise ValueError("间隔分钟须为 1 至 1440 的整数")
    for field, (_, low, high, _) in OPTIONAL_FIELDS.items():
        if field in result:
            value = result[field]
            valid_type = type(value) in (int, float) if field == "retry_delay_seconds" else type(value) is int
            if not valid_type or not low <= value <= high:
                raise ValueError(f"{OPTIONAL_FIELDS[field][0]}须在 {low} 至 {high} 范围内")
    if "composite_layout" in result and result["composite_layout"] not in ("grid", "horizontal"):
        raise ValueError("拼图布局须为网格或横向排列")
    for time in times:
        check_time(time)
    return result


def run_core(data: dict, *, cancel_event=None, progress_callback=None, resume_batch_id=None) -> dict:
    """仅执行线程将快照转换为 Plan；不重复调用报错执行器。"""
    try:
        from .config import Plan
        from .runner import run_plan
    except ImportError as exc:
        raise RuntimeError(f"核心执行接口尚未就绪：{exc}") from exc
    options = {k: v for k, v in dict(cancel_event=cancel_event, progress_callback=progress_callback,
                                   resume_batch_id=resume_batch_id).items() if v is not None}
    return invoke_supported(run_plan, Plan.from_dict(data), **options)


OPTIONAL_FIELDS = {
    "capture_timeout_seconds": ("单任务捕获超时 / 秒", 1, 1800, 180),
    "max_retries": ("失败后最多重试次数", 0, 5, 1),
    "retry_delay_seconds": ("重试等待 / 秒", 0, 60, 2),
    "schedule_grace_seconds": ("计划触发宽限 / 秒", 0, 3600, 120),
    "max_queue_delay_seconds": ("队列最长等待 / 秒", 1, 86400, 900),
    "composite_cell_width": ("拼图单元宽度 / 像素", 64, 4096, 1080),
}
FIELD_LABELS = {
    "project": "项目名称", "platform": "平台名称", "client": "APP / WEB", "keyword": "关键词（含身份后缀）",
    "keyword_id": "关键词唯一身份", "year": "年份", "month": "月份", "day": "日期（日）",
    "display_type": "展示类型", "top_n": "TOP N", "batch": "批次序号", "part": "图片序号（汇总固定1）",
    "content_label": "内容标签", "capture_stamp": "实际截图时间", "artifact_id": "图片唯一身份",
}


class MainWindow(QMainWindow):
    """中文操作台；只读查询、采集和调度均可注入替身，启动无采集副作用。"""

    def __init__(self, runner=None, *, preflight=None, service_factory=None, device_provider=None, login_provider=None,
                 history_provider=None, autoload=True, config_path=None):
        super().__init__()
        ensure_application_font()
        self._runner = runner or run_core
        self._preflight = preflight or core_preflight
        self._service_factory = service_factory or core_schedule
        self._device_provider = device_provider or core_devices
        self._login_provider = login_provider or core_login
        self._history_provider = history_provider or core_history
        self._worker = None
        self._job_kind = "capture"
        self._scheduler = None
        self._reads = {}
        self._config_path = None
        self._closing = self._force_exit = False
        self._active_root = self._history_root = ""
        self._preview_snapshot = self._last_result = self._last_batch = None
        self._tray = None
        self.optional_widgets = {}
        try:
            from .config import Plan
            self._core_fields = set(Plan.__dataclass_fields__)
        except ImportError:
            self._core_fields = set()
        self.setWindowTitle("舆情搜索截图 · 计划与任务")
        self.resize(1180, 840)
        self.setMinimumSize(900, 620)
        self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("舆情搜索截图")
        title.setObjectName("title")
        layout.addWidget(title)
        self.hint_label = QLabel("加载、保存、预检均不采集；执行与计划须手动启动。关闭窗口会停止计划并取消当前任务，等待退出。")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)
        self.config_label = QLabel("首次使用：填写并保存采集计划配置，或加载已有 JSON；这与浏览器会话保存相互独立。")
        self.config_label.setWordWrap(True)
        layout.addWidget(self.config_label)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_capture_tab()
        self._build_output_tab()
        self._build_schedule_tab()
        self._build_tasks_tab()
        self.preview_edit = QPlainTextEdit()
        self.preview_edit.setReadOnly(True)
        self.tabs.addTab(self.preview_edit, "配置 / 原始结果")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("尚未执行")
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel("待配置 · 没有后台采集或调度任务")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        actions = QHBoxLayout()
        self.load_button = self._button("加载配置", self.choose_load, actions)
        self.save_button = self._button("保存配置", self.choose_save_current, actions)
        self.save_as_button = self._button("另存配置", self.choose_save, actions)
        self.preview_button = self._button("配置预览", self.preview_config, actions)
        actions.addStretch()
        self.run_button = self._button("执行一次", self.execute_once, actions)
        self.run_button.setObjectName("primary")
        self.cancel_button = self._button("取消本次", self.cancel_once, actions)
        layout.addLayout(actions)
        self.setStyleSheet('''
            QWidget { font-size: 13px; }
            QLabel#title { font-size: 23px; font-weight: 600; }
            QGroupBox { border: 1px solid #ced5dc; border-radius: 4px; margin-top: 12px; padding: 12px 10px 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; }
            QLineEdit, QPlainTextEdit, QSpinBox, QTimeEdit, QComboBox { padding: 4px; background: white; color: #243447; }
            QPushButton { padding: 6px 10px; min-height: 20px; }
            QPushButton#primary { background: #245ba6; color: white; border-radius: 4px; }
            QPushButton#primary:disabled { background: #dce1e7; color: #697482; }
            QTableView { background: white; alternate-background-color: #f2f5f8; color: #243447; }
            QHeaderView::section { padding: 5px; }
        ''')
        self.set_form_data(default_plan_data(), validate=False)
        startup = Path(config_path) if config_path else default_config_path()
        if autoload and startup.is_file():
            try:
                self.load_config(startup)
                self._status("已自动加载配置；请检查设置，再手动执行或启动计划")
            except Exception as exc:
                self._status(f"初始配置加载出错，请手动加载或填写：{exc}")
        elif autoload and config_path:
            self._status(f"配置文件不存在，请手动加载或填写：{startup}")
        self._connect_preview_invalidation()
        self._sync_buttons()

    @staticmethod
    def _button(text, callback, layout):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _scroll_tab(self, title):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(12, 10, 12, 10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, title)
        return page, box

    def _optional(self, form, fields):
        """尚未落盘的核心字段禁用，不将未知参数传入旧版本 Plan。"""
        for field in fields:
            label, low, high, default = OPTIONAL_FIELDS[field]
            spin = QDoubleSpinBox() if field == "retry_delay_seconds" else QSpinBox()
            if isinstance(spin, QDoubleSpinBox):
                spin.setDecimals(3)
            spin.setRange(low, high)
            spin.setValue(default)
            spin.setEnabled(field in self._core_fields)
            spin.setToolTip(label if field in self._core_fields else "核心字段尚未就绪，沿用默认值；接入后重开窗口启用")
            self.optional_widgets[field] = spin
            form.addRow(label, spin)

    def _build_capture_tab(self):
        self.capture_page, box = self._scroll_tab("采集设置")
        group = QGroupBox("计划与关键词")
        form = QFormLayout(group)
        self.name_edit, self.project_edit = QLineEdit(), QLineEdit()
        self.keywords_edit = QPlainTextEdit()
        self.keywords_edit.setPlaceholderText("每行一个关键词，保留输入顺序，例如：新能源汽车")
        self.keywords_edit.setMinimumHeight(100)
        self.keywords_edit.setMaximumHeight(120)
        self.top_n_spin = QSpinBox()
        self.top_n_spin.setRange(1, 1000)
        for label, widget in (("计划名称", self.name_edit), ("项目名称", self.project_edit),
                              ("关键词", self.keywords_edit), ("结果 TOP N", self.top_n_spin)):
            form.addRow(label, widget)
        self.checks = {}
        for field, label in (("platforms", "采集平台"), ("clients", "采集端"), ("capture_kinds", "采集内容")):
            row = QHBoxLayout()
            self.checks[field] = {}
            for value, text in OPTIONS[field]:
                check = QCheckBox(text)
                self.checks[field][value] = check
                row.addWidget(check)
            row.addStretch()
            form.addRow(label, row)
        row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.addItem("未指定设备（APP 执行前请刷新并选择）", None)
        row.addWidget(self.device_combo, 1)
        self.device_button = self._button("刷新设备", self.refresh_devices, row)
        self.adb_select_button = self._button("选择ADB文件", self.choose_adb, row)
        form.addRow("Android 设备", row)
        note = QLabel("启动MuMu后点刷新；自动发现失败可选一次adb.exe，本机记住路径。无需系统PATH，不启动模拟器或采集；WEB不需要设备。")
        note.setWordWrap(True)
        form.addRow(note)
        box.addWidget(group)
        group = QGroupBox("WEB 浏览器连接")
        form = QFormLayout(group)
        self.browser_connection_mode_combo = QComboBox()
        self.browser_connection_mode_combo.addItem("独立网页会话", "dedicated")
        self.browser_connection_mode_combo.addItem("复用已打开的Chrome", "existing_chrome")
        form.addRow("连接方式", self.browser_connection_mode_combo)
        self.browser_endpoint_edit = QLineEdit("自动发现")
        self.browser_endpoint_edit.setPlaceholderText("自动发现")
        self.browser_endpoint_edit.setToolTip(
            "自动发现保存为 auto；也可填写本机 http/ws 调试端点，例如 http://127.0.0.1:9222 "
            "或 ws://[::1]:9222/devtools/browser/标识。仅接受 loopback 地址，不含用户名密码。"
        )
        form.addRow("调试端点", self.browser_endpoint_edit)
        self.existing_chrome_note = QLabel(EXISTING_CHROME_GUIDANCE)
        self.existing_chrome_note.setWordWrap(True)
        self.existing_chrome_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.existing_chrome_note)
        self.browser_headless_check = QCheckBox("后台无头采集（登录仍打开窗口）")
        form.addRow(self.browser_headless_check)
        self.browser_headless_note = QLabel()
        self.browser_headless_note.setWordWrap(True)
        form.addRow(self.browser_headless_note)
        self.dedicated_browser_controls = QWidget()
        form.addRow(self.dedicated_browser_controls)
        form = QFormLayout(self.dedicated_browser_controls)
        form.setContentsMargins(0, 0, 0, 0)
        self.profile_root_edit = QLineEdit()
        self.profile_root_edit.setPlaceholderText("留空使用应用目录下 .local/browser-profiles；与图片目录分离")
        self.profile_root_edit.setEnabled("browser_profile_root" in self._core_fields)
        row = QHBoxLayout()
        row.addWidget(self.profile_root_edit, 1)
        self.profile_root_button = self._button("选择会话目录", self.choose_profile_directory, row)
        self.profile_root_button.setEnabled("browser_profile_root" in self._core_fields)
        form.addRow("会话根目录", row)
        row = QHBoxLayout()
        self.login_platform_combo = QComboBox()
        for platform, label in OPTIONS["platforms"]:
            self.login_platform_combo.addItem(label, platform)
        row.addWidget(self.login_platform_combo)
        self.login_button = self._button("打开网页登录", self.open_web_login, row)
        row.addStretch()
        form.addRow("登录平台", row)
        note = QLabel("登录始终打开可见窗口，与独立采集沿用同一会话目录；关闭窗口不代表登录已确认。浏览器会话与采集计划配置分别保存，登录不自动保存整份计划。活跃批次时禁用此操作。")
        note.setWordWrap(True)
        form.addRow(note)
        box.addWidget(group)
        group = QGroupBox("捕获与重试")
        self._optional(QFormLayout(group), ("capture_timeout_seconds", "max_retries", "retry_delay_seconds"))
        box.addWidget(group)
        box.addStretch()

    def _template_row(self, field, label, form):
        edit = QLineEdit()
        edit.setMinimumWidth(260)
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        combo = QComboBox()
        combo.setMinimumWidth(165)
        combo.addItem("选择字段插入…", None)
        for key, text in FIELD_LABELS.items():
            combo.addItem(text, "{" + key + "}")
            if key in ("month", "day", "batch", "part"):
                combo.addItem(text + "（两位数字）", "{" + key + ":02d}")
        combo.activated.connect(lambda index, e=edit, c=combo: self.insert_field(e, c, index))
        row.addWidget(combo)
        form.addRow(label, row)
        setattr(self, field, edit)
        self.field_combos[field] = combo

    @staticmethod
    def insert_field(edit, combo, index):
        """向对应模板光标/选区插入中文名称所映射的字段。"""
        value = combo.itemData(index)
        if value:
            edit.insert(value)
            edit.setFocus()
        combo.setCurrentIndex(0)

    def _build_output_tab(self):
        self.output_page, box = self._scroll_tab("导出与预检")
        group = QGroupBox("导出路径与原图 / 拼图模板")
        form = QFormLayout(group)
        row = QHBoxLayout()
        self.root_edit = QLineEdit()
        row.addWidget(self.root_edit, 1)
        self._button("选择目录", self.choose_directory, row)
        form.addRow("导出根目录", row)
        self.field_combos = {}
        for field, label in (("directory_edit", "原图目录模板"), ("filename_edit", "原图文件名"),
                             ("composite_directory_edit", "拼图目录模板"), ("composite_filename_edit", "拼图文件名")):
            self._template_row(field, label, form)
        self.composite_columns_spin = QSpinBox()
        self.composite_columns_spin.setRange(1, 100)
        self.composite_columns_spin.setToolTip("网格按此关键词数换行；横排保持单行，不使用此值换行。")
        form.addRow("每行关键词数", self.composite_columns_spin)
        self.layout_combo = QComboBox()
        self.layout_combo.addItem("网格（按列数换行，单张 PNG）", "grid")
        self.layout_combo.addItem("横排（单行 PNG）", "horizontal")
        self.layout_combo.setEnabled("composite_layout" in self._core_fields)
        form.addRow("拼图布局", self.layout_combo)
        self._optional(form, ("composite_cell_width",))
        note = QLabel("每组仅一张 PNG，无人工像素或高度上限；网格按每行关键词数换行，横排始终单行。"
                      "图片永不覆盖；唯一身份区分新产物，预检不代表已经生成图片。")
        note.setWordWrap(True)
        form.addRow(note)
        box.addWidget(group)
        actions = QHBoxLayout()
        self.preflight_button = self._button("预检真实导出路径", self.preview_paths, actions)
        self.preflight_label = QLabel("尚未预检；请填写关键词和模板。")
        self.preflight_label.setWordWrap(True)
        actions.addWidget(self.preflight_label, 1)
        box.addLayout(actions)
        self.path_model = RecordModel([("kind", "产物类型"), ("platform", "平台"), ("client", "采集端"),
                                       ("capture_kind", "内容"), ("keyword", "关键词"), ("path", "核心预检目标路径")], self)
        self.path_table = self._table(self.path_model)
        self.path_table.setMinimumHeight(190)
        for column, width in enumerate((75, 85, 70, 95, 130)):
            self.path_table.setColumnWidth(column, width)
        self.path_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.path_table.doubleClicked.connect(self.show_path_detail)
        box.addWidget(self.path_table)
        self.preflight_details = QPlainTextEdit()
        self.preflight_details.setReadOnly(True)
        self.preflight_details.setMinimumHeight(110)
        self.preflight_details.setMaximumHeight(180)
        self.preflight_details.setPlaceholderText("核心检查错误、警告及预检边界；双击路径可查看并复制全文。")
        box.addWidget(self.preflight_details)

    def _build_schedule_tab(self):
        self.schedule_page, box = self._scroll_tab("计划时间")
        group = QGroupBox("每日计划窗口与固定时刻")
        self.schedule_group = group
        form = QVBoxLayout(group)
        self.windows_table = QTableWidget(0, 3)
        self.windows_table.setHorizontalHeaderLabels(["开始", "结束", "间隔 / 分钟"])
        self.windows_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.windows_table.verticalHeader().setVisible(False)
        self.windows_table.setMinimumHeight(130)
        self.windows_table.setMaximumHeight(230)
        form.addWidget(self.windows_table)
        row = QHBoxLayout()
        self._button("添加窗口", lambda: self.add_schedule_window(), row)
        self._button("删除选中窗口", self.remove_schedule_window, row)
        row.addStretch()
        form.addLayout(row)
        times = QFormLayout()
        self.times_edit = QLineEdit()
        self.times_edit.setPlaceholderText("例如 09:00, 14:30；仅执行一次时可不设时间")
        times.addRow("固定时刻", self.times_edit)
        self._optional(times, ("schedule_grace_seconds", "max_queue_delay_seconds"))
        form.addLayout(times)
        note = QLabel("结束早于开始表示跨午夜至次日；时区由核心确定。保存不启动计划，启动后使用配置快照。")
        note.setWordWrap(True)
        form.addWidget(note)
        box.addWidget(group)
        controls = QHBoxLayout()
        self.start_button = self._button("启动计划", self.start_schedule, controls)
        self.stop_button = self._button("停止计划，等待当前完成", self.stop_schedule, controls)
        self.stop_cancel_button = self._button("停止并取消当前", lambda: self.stop_schedule(cancel_running=True), controls)
        controls.addStretch()
        box.addLayout(controls)
        self.schedule_label = QLabel("计划未启动")
        self.schedule_label.setWordWrap(True)
        box.addWidget(self.schedule_label)
        self.tray_check = QCheckBox("显式启用托盘后台：最小化或关闭窗口后，计划继续运行")
        self.tray_check.toggled.connect(self.toggle_tray)
        box.addWidget(self.tray_check)
        note = QLabel("默认关闭会停止计划并取消本次。启用托盘后，双击图标恢复窗口；托盘“停止并退出”仍会等任务真正结束。系统关机/睡眠期间不采集。")
        note.setWordWrap(True)
        box.addWidget(note)
        box.addStretch()

    @staticmethod
    def _table(model):
        table = QTableView()
        table.setModel(model)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setDefaultSectionSize(30)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _build_tasks_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("本次任务（按完整关键词身份分行；完成数量不等于成功数量）"))
        partial_note = QLabel(PARTIAL_EXPLANATION)
        partial_note.setWordWrap(True)
        layout.addWidget(partial_note)
        self.phase_label = QLabel("当前阶段：尚未开始")
        self.phase_label.setWordWrap(True)
        self.phase_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.phase_label)
        self.task_model = RecordModel([("platform", "平台"), ("client", "端"), ("capture_kind", "内容"),
            ("keyword", "关键词"), ("status", "状态"), ("reason", "说明")], self)
        self.task_table = self._table(self.task_model)
        for column, width in enumerate((90, 65, 100, 160, 120)):
            self.task_table.setColumnWidth(column, width)
        self.task_table.setMinimumHeight(120)
        layout.addWidget(self.task_table, 3)
        self.event_log = QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.document().setMaximumBlockCount(300)
        self.event_log.setMaximumHeight(76)
        layout.addWidget(self.event_log)
        row = QHBoxLayout()
        row.addWidget(QLabel("历史批次"))
        self.history_filter = QLineEdit()
        self.history_filter.setPlaceholderText("按批次 / 时间 / 状态筛选")
        row.addWidget(self.history_filter, 1)
        self.history_button = self._button("刷新历史", self.refresh_history, row)
        layout.addLayout(row)
        self.history_model = RecordModel([("batch_id", "批次"), ("planned_at", "计划时间"),
            ("status", "最终 / 当前状态"), ("completed", "已处理"), ("total", "总任务"), ("finished_at", "结束时间")], self)
        self.history_proxy = QSortFilterProxyModel(self)
        self.history_proxy.setSourceModel(self.history_model)
        self.history_proxy.setFilterKeyColumn(-1)
        self.history_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.history_filter.textChanged.connect(self.history_proxy.setFilterFixedString)
        self.history_table = self._table(self.history_proxy)
        for column, width in enumerate((180, 180, 135, 75, 75)):
            self.history_table.setColumnWidth(column, width)
        self.history_table.setSortingEnabled(True)
        self.history_table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        self.history_table.setMinimumHeight(110)
        layout.addWidget(self.history_table, 3)
        self.history_label = QLabel("历史尚未读取；读取不会生成数据库。")
        self.history_label.setWordWrap(True)
        layout.addWidget(self.history_label)
        row = QHBoxLayout()
        self.open_output_button = self._button("打开输出目录", self.open_history_output, row)
        self.open_report_button = self._button("打开批次报告", self.open_history_report, row)
        self.resume_button = self._button("恢复选中未完成批次", self.resume_selected, row)
        row.addStretch()
        layout.addLayout(row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, "任务与历史")

    def _text_fields(self):
        return ((self.name_edit, "name"), (self.project_edit, "project"), (self.root_edit, "output_root"),
                (self.directory_edit, "directory_template"), (self.filename_edit, "filename_template"),
                (self.composite_directory_edit, "composite_directory_template"),
                (self.composite_filename_edit, "composite_filename_template"))

    def add_schedule_window(self, data=None):
        """少量时间窗口保留时间控件；大量采集任务另用 Model/View。"""
        data = data or {"start": "09:00", "end": "21:00", "interval_minutes": 60}
        row = self.windows_table.rowCount()
        self.windows_table.insertRow(row)
        self.windows_table.setRowHeight(row, 36)
        for column, field in enumerate(("start", "end")):
            edit = QTimeEdit(QTime.fromString(data[field], "HH:mm"))
            edit.setDisplayFormat("HH:mm")
            self.windows_table.setCellWidget(row, column, edit)
        interval = QSpinBox()
        interval.setRange(1, 1440)
        interval.setValue(data["interval_minutes"])
        self.windows_table.setCellWidget(row, 2, interval)
        self.windows_table.setCurrentCell(row, 0)
        self.windows_table.setFixedHeight(min(210, max(106, 42 + 36 * self.windows_table.rowCount())))

    def remove_schedule_window(self):
        row = self.windows_table.currentRow()
        if row >= 0:
            self.windows_table.removeRow(row)
            self.windows_table.setFixedHeight(min(210, max(106, 42 + 36 * self.windows_table.rowCount())))

    def set_form_data(self, data: dict, *, validate: bool = True):
        """基础校验通过才更新；未知扩展字段及已保存值不丢弃。"""
        data = validate_plan_data(data) if validate else copy.deepcopy(data)
        self._loaded_data = data
        for widget, field in self._text_fields():
            widget.setText(data[field])
        self.keywords_edit.setPlainText("\n".join(data["keywords"]))
        self.top_n_spin.setValue(data["top_n"])
        self.composite_columns_spin.setValue(data["composite_columns"])
        for field, checks in self.checks.items():
            for value, check in checks.items():
                check.setChecked(value in data[field])
        self.windows_table.setRowCount(0)
        for window in data["schedule"]["windows"]:
            self.add_schedule_window(window)
        self.times_edit.setText(", ".join(data["schedule"]["times"]))
        serial = data.get("android_serial")
        index = self.device_combo.findData(serial)
        if index < 0:
            self.device_combo.addItem(f"已保存设备（待刷新名称） · {serial}", serial)
            index = self.device_combo.count() - 1
        self.device_combo.setCurrentIndex(index)
        self.profile_root_edit.setText(data.get("browser_profile_root") or "")
        self.browser_connection_mode_combo.setCurrentIndex(
            self.browser_connection_mode_combo.findData(data.get("browser_connection_mode", "dedicated"))
        )
        endpoint = data.get("browser_endpoint", "auto")
        self.browser_endpoint_edit.setText("自动发现" if endpoint == "auto" else endpoint)
        self.browser_headless_check.setChecked(data.get("browser_headless", False))
        self._sync_browser_controls()
        for field, widget in self.optional_widgets.items():
            value = data.get(field, OPTIONAL_FIELDS[field][3])
            if type(value) in (int, float):
                widget.setValue(value)
        self._optional_display_values = {field: widget.value() for field, widget in self.optional_widgets.items()}
        index = self.layout_combo.findData(data.get("composite_layout", "grid"))
        if index < 0:
            self.layout_combo.addItem(f"已保存布局：{data['composite_layout']}", data["composite_layout"])
            index = self.layout_combo.count() - 1
        self.layout_combo.setCurrentIndex(index)

    def form_data(self) -> dict:
        """读取完整快照；未支持的扩展字段保留原文，不从禁用控件推定配置。"""
        data = copy.deepcopy(self._loaded_data)
        for widget, field in self._text_fields():
            data[field] = widget.text()
        data["keywords"] = [line for line in self.keywords_edit.toPlainText().splitlines() if line.strip()]
        data["top_n"] = self.top_n_spin.value()
        data["composite_columns"] = self.composite_columns_spin.value()
        for field, checks in self.checks.items():
            selected = {v for v, c in checks.items() if c.isChecked()}
            data[field] = [v for v in data[field] if v in selected]
            data[field] += [v for v in checks if v in selected and v not in data[field]]
        data["schedule"]["windows"] = [
            {"start": self.windows_table.cellWidget(row, 0).time().toString("HH:mm"),
             "end": self.windows_table.cellWidget(row, 1).time().toString("HH:mm"),
             "interval_minutes": self.windows_table.cellWidget(row, 2).value()}
            for row in range(self.windows_table.rowCount())]
        data["schedule"]["times"] = [v for v in re.split(r"[,，\s]+", self.times_edit.text().strip()) if v]
        serial = self.device_combo.currentData()
        data["browser_connection_mode"] = self.browser_connection_mode_combo.currentData()
        data["browser_headless"] = self.browser_headless_check.isChecked()
        endpoint = self.browser_endpoint_edit.text()
        data["browser_endpoint"] = "auto" if endpoint in ("", "自动发现") else endpoint
        if serial is not None or "android_serial" in data:
            data["android_serial"] = serial
        if "browser_profile_root" in self._core_fields:
            root = self.profile_root_edit.text().strip() or None
            if root is not None or "browser_profile_root" in data:
                data["browser_profile_root"] = root
        for field, widget in self.optional_widgets.items():
            if field in self._core_fields and (field in data or widget.value() != OPTIONAL_FIELDS[field][3]):
                if field not in data or widget.value() != self._optional_display_values.get(field):
                    data[field] = widget.value()
        if "composite_layout" in self._core_fields and ("composite_layout" in data or self.layout_combo.currentData() != "grid"):
            data["composite_layout"] = self.layout_combo.currentData()
        return validate_plan_data(data)

    def save_config(self, path: str | Path, *, overwrite: bool = False):
        """仅保存 JSON；显式覆盖使用原子替换，图片从不进入此路径。"""
        target = Path(path).resolve()
        if target.suffix.lower() != ".json":
            raise ValueError("配置须保存为 .json 文件；图片保持原样")
        content = json.dumps(self.form_data(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        if overwrite:
            import tempfile
            fd, temporary = tempfile.mkstemp(prefix=".plan-", suffix=".tmp", dir=target.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        else:
            with target.open("x", encoding="utf-8") as stream:
                stream.write(content)
        self._config_path = target
        self._show_config_path()

    def load_config(self, path: str | Path):
        """只读加载；坏文件保留原表单和原配置路径。"""
        if self._runtime_busy():
            raise RuntimeError("任务或计划运行中，请先停止后加载另一配置")
        target = Path(path).resolve()
        self.set_form_data(json.loads(target.read_text(encoding="utf-8-sig")))
        self._config_path = target
        self._show_config_path()

    def _show_config_path(self):
        self.config_label.setText(f"当前采集计划配置：{self._config_path}（保存会询问覆盖；与浏览器会话保存相互独立）")

    def _confirm_overwrite(self, path):
        return QMessageBox.question(self, "覆盖配置 JSON", f"是否覆盖该配置文件？\n{path}\n此操作仅保存配置，图片保持不变。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    @Slot()
    def choose_save_current(self):
        if self._config_path is None:
            self.choose_save()
            return
        if self._config_path.exists() and not self._confirm_overwrite(self._config_path):
            return
        try:
            self.save_config(self._config_path, overwrite=True)
            self._status("配置已保存到同一用户文件；未触发采集或计划")
        except Exception as exc:
            self._status(f"保存出错：{exc}")

    @Slot()
    def choose_save(self):
        suggested = self._config_path or default_config_path()
        path, _ = QFileDialog.getSaveFileName(self, "另存配置", str(suggested), "JSON 配置 (*.json)",
                                             options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            existing = Path(path).exists()
            if existing and not self._confirm_overwrite(path):
                return
            try:
                self.save_config(path, overwrite=existing)
                self._status("配置已保存；未触发采集或计划")
            except Exception as exc:
                self._status(f"保存出错：{exc}")

    @Slot()
    def choose_load(self):
        if self._runtime_busy():
            return
        path, _ = QFileDialog.getOpenFileName(self, "加载配置", str(PROJECT_ROOT / ".local"), "JSON 配置 (*.json)")
        if path:
            try:
                self.load_config(path)
                self._status("配置已加载；未触发采集或计划")
            except Exception as exc:
                self._status(f"加载出错，原表单保留：{exc}")

    @Slot()
    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "选择导出根目录", self.root_edit.text())
        if path:
            self.root_edit.setText(path)

    @Slot()
    def choose_profile_directory(self):
        """仅填写会话根目录；不创建目录或打开浏览器。"""
        if self.browser_connection_mode_combo.currentData() == "existing_chrome":
            return
        path = QFileDialog.getExistingDirectory(self, "选择专属浏览器会话根目录", self.profile_root_edit.text() or str(application_root()))
        if path:
            self.profile_root_edit.setText(path)

    @Slot()
    def preview_config(self):
        try:
            self.preview_edit.setPlainText(json.dumps(self.form_data(), ensure_ascii=False, indent=2, sort_keys=True))
            self.tabs.setCurrentWidget(self.preview_edit)
            self._status("配置 JSON 已更新；真实路径请到“导出与预检”点击预检")
        except Exception as exc:
            self._status(f"配置检查：{exc}")

    def _status(self, text):
        """限制状态区长度，完整错误保留在悬停和有界日志中。"""
        value = str(text)
        self.status_label.setText(value[:220] + ("…（完整说明见任务日志）" if len(value) > 220 else ""))
        self.status_label.setToolTip(value)
        self.statusBar().showMessage(value.splitlines()[0][:140])
        self.event_log.appendPlainText(f"{datetime.now():%H:%M:%S}  {value}")

    def _runtime_busy(self):
        return self._worker is not None or self._scheduler is not None

    @Slot()
    def _sync_browser_controls(self):
        """模式切换仅更新表单状态；隐藏旧登录入口并保留两种模式的已填配置。"""
        existing = self.browser_connection_mode_combo.currentData() == "existing_chrome"
        self.browser_endpoint_edit.setEnabled(existing)
        self.existing_chrome_note.setVisible(existing)
        self.dedicated_browser_controls.setVisible(not existing)
        self.dedicated_browser_controls.setEnabled(not existing)
        busy = self._runtime_busy() or self._closing or bool(set(self._reads) & {"preflight", "devices"})
        self.browser_headless_check.setEnabled(not existing and not busy)
        note = ("复用已打开的Chrome时此项禁用；保留勾选值，但不改变已有浏览器的窗口或运行方式。"
                if existing else "仅独立网页会话采集生效；登录始终打开窗口，并沿用同一会话目录。")
        self.browser_headless_note.setText(note)
        self.browser_headless_check.setToolTip(note)
        self.login_button.setEnabled(not existing and not busy)

    def _sync_buttons(self):
        busy = self._runtime_busy() or self._closing or bool(set(self._reads) & {"preflight", "devices"})
        self.run_button.setEnabled(not busy)
        self.cancel_button.setEnabled(self._worker is not None and not self._worker.cancel_event.is_set())
        self.start_button.setEnabled(not busy)
        self._sync_browser_controls()
        self.stop_button.setEnabled(self._scheduler is not None and not self._scheduler.stop_event.is_set())
        self.stop_cancel_button.setEnabled(self._scheduler is not None and not self._scheduler.cancel_running.is_set())
        self.load_button.setEnabled(not busy)
        self.resume_button.setEnabled(not busy)
        self.capture_page.setEnabled(not busy)
        self.output_page.setEnabled(not busy)
        self.windows_table.setEnabled(not busy)
        self.times_edit.setEnabled(not busy)
        self.schedule_group.setEnabled(not busy)
        self.preflight_button.setEnabled(not busy and "preflight" not in self._reads)
        self.device_button.setEnabled(not busy and "devices" not in self._reads)
        self.adb_select_button.setEnabled(not busy and "devices" not in self._reads)
        self.history_button.setEnabled(not self._closing and "history" not in self._reads)

    def _read(self, name, callback, handler):
        """每种只读请求至多一个；退出时同样等待读线程结束。"""
        if name in self._reads or self._closing:
            return
        worker = ReadThread(callback, self)
        self._reads[name] = worker
        worker.result_ready.connect(handler)
        worker.finished.connect(self._read_finished)
        self._sync_buttons()
        worker.start()

    @Slot()
    def _read_finished(self):
        worker = self.sender()
        for name, active in list(self._reads.items()):
            if active is worker:
                del self._reads[name]
                break
        worker.deleteLater()
        self._sync_buttons()
        self._finish_close_if_ready()

    def _connect_preview_invalidation(self):
        for widget, _ in self._text_fields():
            widget.textChanged.connect(self._invalidate_preview)
        self.keywords_edit.textChanged.connect(self._invalidate_preview)
        self.top_n_spin.valueChanged.connect(self._invalidate_preview)
        self.composite_columns_spin.valueChanged.connect(self._invalidate_preview)
        self.layout_combo.currentIndexChanged.connect(self._invalidate_preview)
        self.browser_connection_mode_combo.currentIndexChanged.connect(self._sync_browser_controls)
        self.browser_connection_mode_combo.currentIndexChanged.connect(self._invalidate_preview)
        self.browser_endpoint_edit.textChanged.connect(self._invalidate_preview)
        self.browser_headless_check.toggled.connect(self._invalidate_preview)
        for controls in self.checks.values():
            for check in controls.values():
                check.toggled.connect(self._invalidate_preview)
        for spin in self.optional_widgets.values():
            spin.valueChanged.connect(self._invalidate_preview)

    def _invalidate_preview(self, *args):
        self._preview_snapshot = None
        self.path_model.replace([])
        self.preflight_label.setText("配置已变更，请重新预检；旧路径不再作为当前结果。")
        self.preflight_details.clear()

    @Slot()
    def preview_paths(self):
        if "preflight" in self._reads:
            return
        try:
            snapshot = self.form_data()
        except Exception as exc:
            self._status(f"预检前配置检查：{exc}")
            return
        self._preview_request = copy.deepcopy(snapshot)
        self.preflight_label.setText("正在调用核心导出预检；不生成图片…")
        self._read("preflight", lambda: self._preflight(snapshot), self._paths_ready)

    @Slot(object)
    def _paths_ready(self, response):
        if not response["ok"]:
            self.preflight_label.setText("预检失败；没有有效路径预览")
            self.path_model.replace([])
            self.preflight_details.setPlainText(response["reason"])
            return
        try:
            if self.form_data() != self._preview_request:
                self._invalidate_preview()
                return
            result = response["value"]
            if not isinstance(result, dict) or "ok" not in result:
                raise ValueError("核心预检应返回包含 ok/errors/warnings/originals/composites 的字典")
            rows = []
            for field, label in (("originals", "原图"), ("composites", "拼图")):
                for item in result.get(field, []):
                    row = dict(item) if isinstance(item, dict) else {"path": str(item)}
                    row["kind"] = label
                    row["path"] = row.get("path") or row.get("output_path") or row.get("destination") or ""
                    rows.append(row)
            self.path_model.replace(rows)
            lines = []
            for field, title in (("errors", "错误"), ("warnings", "警告")):
                for item in result.get(field, []):
                    value = f"{item.get('code', '')}：{item.get('message', '')}" if isinstance(item, dict) else str(item)
                    lines.append(f"{title} · {value}")
            lines.append("预检边界：" + json.dumps(result.get("boundaries", {}), ensure_ascii=False, default=str))
            lines.append("统计：" + json.dumps(result.get("counts", {}), ensure_ascii=False, default=str))
            self.preflight_details.setPlainText("\n".join(lines))
            self._preview_snapshot = copy.deepcopy(self._preview_request)
            passed = bool(result["ok"]) and not result.get("errors")
            self.preflight_label.setText(f"{'预检通过' if passed else '预检发现错误'} · {len(rows)} 条路径；每组单张 PNG，实际日期、身份及尺寸以采集时为准")
        except Exception as exc:
            self.path_model.replace([])
            self.preflight_label.setText("预检返回格式异常")
            self.preflight_details.setPlainText(str(exc))

    @Slot(object)
    def show_path_detail(self, index):
        if index.isValid():
            self.preflight_details.appendPlainText("\n完整路径：" + str(self.path_model.rows[index.row()].get("path", "")))

    @Slot()
    def choose_adb(self):
        """失败兜底只选本机文件，保存后沿用现有后台设备刷新流程。"""
        if self._runtime_busy() or self._closing or "devices" in self._reads:
            return
        from .android_discovery import remember_adb, saved_adb
        previous = saved_adb()
        filename, _ = QFileDialog.getOpenFileName(self, "选择MuMu目录中的adb.exe",
            str(Path(previous).parent) if previous else "", "ADB程序 (adb.exe)")
        if not filename:
            return
        try:
            selected = remember_adb(filename)
            # 仅覆盖当前程序的选择，子进程继承；不调用setx或更改全局PATH。
            os.environ["OPINION_CAPTURE_ADB"] = selected
            self._status("ADB路径已保存到本机，正在刷新设备")
            self.refresh_devices()
        except (OSError, ValueError) as exc:
            self._status(f"ADB文件选择失败：{exc}")

    @Slot()
    def refresh_devices(self):
        if self._runtime_busy():
            return
        self._status("正在后台发现设备；不会启动平台采集")
        self._read("devices", self._device_provider, self._devices_ready)

    @Slot(object)
    def _devices_ready(self, response):
        if not response["ok"]:
            reason = response["reason"]
            if "adb_not_found" in reason:
                reason = "未找到ADB：请点击“选择ADB文件”，选择MuMu目录中的adb.exe；本机记住路径，无需修改系统PATH。"
            self._status(f"设备发现失败，原选择保留：{reason}")
            return
        devices = response["value"]
        if not isinstance(devices, list) or any(not isinstance(d, dict) or not d.get("serial") for d in devices):
            self._status("设备发现返回格式异常，原选择保留")
            return
        saved = self.device_combo.currentData()
        self.device_combo.clear()
        self.device_combo.addItem("未指定设备（请选择名称）", None)
        valid = []
        for item in devices:
            state = item.get("state", item.get("status", "device"))
            serial = item["serial"]
            name = item.get("name") or item.get("model") or "Android 设备"
            ready = state in ("device", "online", "ready")
            label = f"{name} · {serial}" + ("" if ready else f"（{'离线' if state == 'offline' else '待确认连接'}）")
            self.device_combo.addItem(label, serial)
            if ready:
                valid.append(serial)
            else:
                self.device_combo.model().item(self.device_combo.count() - 1).setEnabled(False)
        index = self.device_combo.findData(saved)
        if saved and index < 0:
            self.device_combo.addItem(f"已保存设备未发现 · {saved}", saved)
            index = self.device_combo.count() - 1
        self.device_combo.setCurrentIndex(index if index >= 0 else 0)
        if saved is None and len(valid) == 1:
            self.device_combo.setCurrentIndex(self.device_combo.findData(valid[0]))
        self._status(f"发现 {len(devices)} 台设备，{len(valid)} 台可用；配置只保存所选串号")

    @Slot()
    def execute_once(self):
        self._begin_execution()

    def _begin_execution(self, *, resume_batch_id=None, data=None):
        if self._runtime_busy() or self._closing or bool(set(self._reads) & {"preflight", "devices"}):
            return
        try:
            data = data or self.form_data()
        except Exception as exc:
            self._status(f"执行前配置检查：{exc}")
            return
        self._active_root = data["output_root"]
        self._job_kind = "capture"
        self.cancel_button.setText("取消本次")
        self._last_batch = None
        self._last_result = None
        self.phase_label.setText("当前阶段：等待核心上报")
        self.phase_label.setToolTip("")
        self.task_model.replace(task_rows(data))
        self.progress_bar.setRange(0, self.task_model.rowCount())
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("已处理 %v / %m（含失败与受阻）")
        callback = self._runner
        if resume_batch_id:
            runner = callback
            callback = lambda snapshot, **kw: runner(snapshot, resume_batch_id=resume_batch_id, **kw)
        self._worker = ExecutionThread(data, callback, self)
        self._worker.result_ready.connect(self.show_result)
        self._worker.progress.connect(self.show_progress)
        self._worker.finished.connect(self.execution_finished)
        self._sync_buttons()
        self.tabs.setCurrentIndex(3)
        self._status("执行中 · 使用配置快照；取消请求会交给核心，等待任务实际退出")
        self._worker.start()

    @Slot()
    def cancel_once(self):
        if self._worker is not None:
            self._worker.cancel()
            self._status("正在关闭网页登录：已请求取消，等待浏览器会话实际退出；登录状态未验证" if self._job_kind == "login" else "正在取消本次：请求已发送，等待核心保存状态并退出；不是立即终止")
            self._sync_buttons()

    @Slot(object)
    def show_progress(self, event):
        """阶段仅更新展示；任务状态、计数及结果仍由原有生命周期事件决定。"""
        if not isinstance(event, dict):
            self._status(f"收到非结构化进度：{event}")
            return
        row = dict(event)
        for key in ("task", "result"):
            if isinstance(event.get(key), dict):
                row.update(event[key])
        batch_id = event.get("batch_id")
        if event.get("event") in ("phase", "task_phase"):
            # 晚到的阶段不覆盖终态展示，也不插入任务或采用事件中的 status/completed。
            if self._last_result is not None or (batch_id and self._last_batch and batch_id != self._last_batch):
                return
            identity = RecordModel.identity(row)
            current = next((task for task in self.task_model.rows
                            if (all(part is not None for part in identity) and RecordModel.identity(task) == identity)
                            or (row.get("task_id") is not None and row["task_id"] == task.get("task_id"))), None)
            if current and current.get("status") in (
                "success", "partial", "failed", "error", "blocked", "cancelled", "canceled", "skipped", "interrupted"
            ):
                return
            text = phase_progress_text(row)
            self.phase_label.setText(text)
            self.phase_label.setToolTip(text + f"\n阶段代码：{row.get('phase_code') or '未上报'}")
            self.event_log.appendPlainText(f"{datetime.now():%H:%M:%S}  {text}")
            return
        if batch_id and self._last_batch and batch_id != self._last_batch and self._scheduler is not None:
            self.task_model.replace(task_rows(self._scheduler.data))
            self.progress_bar.setValue(0)
        if batch_id:
            self._last_batch = batch_id
        if event.get("event") == "batch_started":
            self._last_result = None
        if event.get("event") in ("batch_started", "task_started"):
            self.phase_label.setText("当前阶段：等待核心上报")
            self.phase_label.setToolTip("")
        elif event.get("event") in ("task_finished", "batch_finished", "schedule_result"):
            self.phase_label.setText(f"阶段进度：{status_text(row.get('status'))}；最近阶段及耗时见任务日志")
            self.phase_label.setToolTip("")
        self.task_model.update_task(row)
        total, completed = event.get("total"), event.get("completed")
        if isinstance(total, int) and total > 0:
            self.progress_bar.setRange(0, total)
        if isinstance(completed, int):
            self.progress_bar.setValue(min(completed, self.progress_bar.maximum()))
            self.progress_bar.setFormat("已处理 %v / %m（含失败与受阻）")
        if event.get("status") or event.get("reason"):
            detail = result_detail_text(row) or row.get("keyword") or "核心进度已更新"
            self._status(f"{status_text(event.get('status', 'running'))} · {detail}")
        result = event.get("result")
        if isinstance(result, dict) and ("results" in result or "report_path" in result):
            self.show_result(result)
        elif event.get("event") in ("batch_finished", "schedule_result"):
            self._last_result = dict(event)
            self.preview_edit.setPlainText(json.dumps(event, ensure_ascii=False, indent=2, default=str))
            self.refresh_history()

    @Slot(object)
    def show_result(self, result):
        """业务状态与线程退出分开；原始结果可追溯，任务列表中文显示。"""
        self._last_result = copy.deepcopy(result)
        code = result.get("status", "missing_status")
        reason = result_detail_text(result) or "请查看任务明细及批次报告"
        if code == "partial":
            reason += "\n" + PARTIAL_EXPLANATION
        self._status(f"执行返回 · {status_text(code)}（{code}）\n{reason}")
        self.phase_label.setText(f"阶段进度：本次执行已返回（{status_text(code)}）；耗时见任务日志")
        self.phase_label.setToolTip("")
        for row in result.get("results", []):
            if isinstance(row, dict):
                self.task_model.update_task(row)
        total = result.get("total", self.task_model.rowCount())
        completed = result.get("completed", len(result.get("results", [])))
        if isinstance(total, int) and total > 0:
            self.progress_bar.setRange(0, total)
        if isinstance(completed, int):
            self.progress_bar.setValue(min(completed, self.progress_bar.maximum()))
        self.progress_bar.setFormat("已处理 %v / %m（含失败与受阻）")
        value = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        self.preview_edit.setPlainText(value[:200000] + ("\n（内容较大，其余请打开批次报告）" if len(value) > 200000 else ""))

    @Slot()
    def execution_finished(self):
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        was_login = self._job_kind == "login"
        self._job_kind = "capture"
        self.cancel_button.setText("取消本次")
        self._sync_buttons()
        if not was_login and not self._closing and self._last_result and self._last_result.get("batch_id"):
            self.refresh_history()
        self._finish_close_if_ready()

    @Slot()
    def open_web_login(self):
        """显式用户入口；网页登录复用可取消执行线程，不启动计划或 collector。"""
        if self.browser_connection_mode_combo.currentData() == "existing_chrome":
            self._status(EXISTING_CHROME_GUIDANCE)
            return
        if self._runtime_busy() or self._closing or bool(set(self._reads) & {"preflight", "devices"}):
            return
        try:
            data = self.form_data()
        except Exception as exc:
            self._status(f"会话配置检查：{exc}")
            return
        platform = self.login_platform_combo.currentData()
        self._job_kind = "login"
        self._last_result = None
        self.cancel_button.setText("关闭网页登录")
        callback = lambda snapshot, cancel_event=None, **kwargs: self._login_provider(snapshot, platform, cancel_event=cancel_event)
        self._worker = ExecutionThread(data, callback, self)
        self._worker.result_ready.connect(self._login_result)
        self._worker.finished.connect(self.execution_finished)
        self._sync_buttons()
        self._status("正在打开专属网页会话，请在浏览器中手动登录后关闭；此操作不采集，关闭不等于已登录")
        self._worker.start()

    @Slot(object)
    def _login_result(self, result):
        """只展示会话层返回的认证/持久化事实；不触发整份采集计划保存。"""
        self._status(login_result_text(result))
        self.preview_edit.setPlainText(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    @Slot()
    def start_schedule(self):
        if self._runtime_busy() or self._closing or bool(set(self._reads) & {"preflight", "devices"}):
            return
        try:
            data = self.form_data()
            if not data["schedule"]["windows"] and not data["schedule"]["times"]:
                raise ValueError("请先设置计划窗口或固定时刻；只需一次任务时使用“执行一次”")
        except Exception as exc:
            self._status(f"启动前配置检查：{exc}")
            return
        self._active_root = data["output_root"]
        self.task_model.replace(task_rows(data))
        self._last_batch = None
        self._scheduler = ScheduleThread(data, self._service_factory, self)
        self._scheduler.progress.connect(self.show_progress)
        self._scheduler.result_ready.connect(self._schedule_result)
        self._scheduler.service_started.connect(self._schedule_started)
        self._scheduler.finished.connect(self._schedule_finished)
        self.schedule_label.setText("计划服务正在启动…")
        self._sync_buttons()
        self._scheduler.start()

    @Slot()
    def _schedule_started(self):
        if self._scheduler and not self._scheduler.stop_event.is_set():
            self.schedule_label.setText("计划服务已启动，等待核心触发时刻；表单已锁定为运行快照")
            self._status("计划已启动；可停止接收新计划，或停止并取消当前任务")

    @Slot()
    def stop_schedule(self, *, cancel_running=False):
        if self._scheduler is not None:
            self._scheduler.stop(cancel_running=cancel_running)
            self.schedule_label.setText("正在停止并取消当前任务，等待保存状态及退出" if cancel_running else "已请求停止新计划，等待当前任务自然完成；此时服务仍可能运行")
            self._status(self.schedule_label.text())
            self._sync_buttons()

    @Slot(object)
    def _schedule_result(self, result):
        self.schedule_label.setText(f"{status_text(result.get('status'))} · {result.get('reason', '')}")
        self._status(self.schedule_label.text())

    @Slot()
    def _schedule_finished(self):
        if self._scheduler is not None:
            self._scheduler.deleteLater()
            self._scheduler = None
        self._sync_buttons()
        if not self._closing:
            self.refresh_history()
        self._finish_close_if_ready()

    @Slot()
    def refresh_history(self):
        if "history" in self._reads:
            return
        root = self._active_root if self._runtime_busy() else self.root_edit.text()
        if not Path(root).is_absolute():
            self.history_label.setText("请先选择绝对导出目录")
            return
        self._history_request_root = root
        self.history_label.setText("正在后台读取历史批次…")
        self._read("history", lambda: self._history_provider(root), self._history_ready)

    @Slot(object)
    def _history_ready(self, response):
        if not response["ok"]:
            self.history_label.setText(f"历史读取失败：{response['reason']}")
            return
        rows = response["value"]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            self.history_label.setText("历史接口返回格式异常")
            return
        normalized = []
        for row in rows:
            row = dict(row)
            row.setdefault("batch_id", row.get("id"))
            row.setdefault("planned_at", row.get("scheduled_at"))
            row.setdefault("output_root", self._history_request_root)
            normalized.append(row)
        self.history_model.replace(normalized)
        self._history_root = self._history_request_root
        self.history_label.setText(f"{len(rows)} 个历史批次 · 来源：{self._history_root}")
        if rows:
            self.history_table.selectRow(0)

    def _selected_history(self):
        index = self.history_table.currentIndex()
        if not index.isValid():
            self._status("请先刷新历史并选择一个批次")
            return None
        return self.history_model.rows[self.history_proxy.mapToSource(index).row()]

    def _open_path(self, path, *, directory=False):
        """只打开已存在本地绝对路径，不经 shell 解释历史内容。"""
        if not path:
            self._status("该批次尚无报告或输出路径")
            return
        target = Path(path)
        if not target.is_absolute() or not (target.is_dir() if directory else target.is_file()):
            self._status(f"路径尚不存在或类型不符：{target}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            self._status(f"打开失败，请复制路径手动打开：{target}")

    @Slot()
    def open_history_output(self):
        row = self._selected_history()
        if row:
            self._open_path(row.get("output_root"), directory=True)

    @Slot()
    def open_history_report(self):
        row = self._selected_history()
        if row:
            self._open_path(row.get("report_path"))

    @Slot()
    def resume_selected(self):
        """恢复必须使用历史快照，避免当前编辑内容改变旧批次。"""
        if self._runtime_busy():
            return
        row = self._selected_history()
        if not row:
            return
        if row.get("status") in ("success", "running", "pending"):
            self._status("请选择已结束的未完成批次；运行中批次不重复恢复")
            return
        data = row.get("config")
        if not isinstance(data, dict) and isinstance(row.get("config_json"), str):
            try:
                data = json.loads(row["config_json"])
            except (ValueError, TypeError):
                pass
        if not isinstance(data, dict):
            self._status("历史记录未附配置快照；等待核心提供 config，避免用当前表单误改旧批次")
            return
        try:
            data = validate_plan_data(data)
        except Exception as exc:
            self._status(f"历史配置校验失败：{exc}")
            return
        answer = QMessageBox.question(self, "恢复未完成批次", f"使用批次 {row['batch_id']} 的历史配置继续执行？\n已有图片与旧尝试报告保留，不覆盖。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._begin_execution(resume_batch_id=row["batch_id"], data=data)

    @Slot(bool)
    def toggle_tray(self, enabled):
        if enabled and not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_check.blockSignals(True)
            self.tray_check.setChecked(False)
            self.tray_check.blockSignals(False)
            self._status("当前环境没有可用托盘；窗口保持可见，关闭仍停止并取消")
            return
        if enabled:
            if self._tray is None:
                self._tray = QSystemTrayIcon(self.windowIcon(), self)
                self._tray.setToolTip("舆情搜索截图 · 后台计划已显式启用")
                menu = QMenu(self)
                menu.addAction("恢复窗口").triggered.connect(self.restore_window)
                menu.addAction("停止计划、取消任务并退出").triggered.connect(self.request_exit)
                self._tray.setContextMenu(menu)
                self._tray.activated.connect(self._tray_activated)
            self._tray.show()
            self.hint_label.setText("托盘后台已启用：最小化 / 关闭窗口后计划继续；退出请用托盘菜单“停止计划、取消任务并退出”。")
        else:
            if self._tray:
                self._tray.hide()
            self.restore_window()
            self.hint_label.setText("关闭窗口会停止计划并取消当前任务；等线程真正退出后关闭。加载、保存、预检均不采集。")

    @Slot(object)
    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick, QSystemTrayIcon.ActivationReason.Trigger):
            self.restore_window()

    @Slot()
    def restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot()
    def request_exit(self):
        self._force_exit = True
        self.restore_window()
        self.close()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, "tray_check") and self.isMinimized() and self.tray_check.isChecked() and self._tray and self._tray.isVisible():
            QTimer.singleShot(0, self.hide)

    def _finish_close_if_ready(self):
        if self._closing and not self._runtime_busy() and not self._reads:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent):
        """默认停止并取消后异步等待；只有显式启用托盘才隐藏继续。"""
        if not self._force_exit and self.tray_check.isChecked() and self._tray and self._tray.isVisible():
            event.ignore()
            self.hide()
            return
        if self._runtime_busy() or self._reads:
            event.ignore()
            self._closing = True
            if self._worker:
                self._worker.cancel()
            if self._scheduler:
                self._scheduler.stop(cancel_running=True)
            self._sync_buttons()
            self._status("正在退出：已停止新计划并请求取消当前任务，等待所有工作线程实际结束；请勿强制结束进程")
            return
        if self._tray:
            self._tray.hide()
        event.accept()


def main(config_path=None) -> int:
    """加载已有默认配置或显示首次引导；不自动开始计划或采集。"""
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(config_path=config_path)
    screen = app.primaryScreen()
    if screen:
        available = screen.availableGeometry()
        window.resize(min(1180, available.width() - 40), min(840, available.height() - 60))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())


