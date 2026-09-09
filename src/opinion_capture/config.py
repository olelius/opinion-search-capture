"""采集计划配置；显示名与平台/采集端的内部标识分离。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from .paths import default_output_root

PLATFORMS = {"xiaohongshu": "小红书", "bilibili": "B站"}
CLIENTS = ("APP", "WEB")
KINDS = ("results", "suggestions")
DEFAULT_DIRECTORY = "{project}-{year}-{month}月/{platform}/{month:02d}{day:02d}-{display_type}-{client}/{keyword}"
DEFAULT_FILENAME = "{keyword}_{content_label}_{batch:02d}_{capture_stamp}_{artifact_id}.png"


def validate_browser_connection(mode: str, endpoint: str) -> None:
    """校验模式并复用后端的纯端点校验；不连接浏览器或访问其资料。"""
    if not isinstance(mode, str) or mode not in ("dedicated", "existing_chrome"):
        raise ValueError("浏览器连接模式应为 dedicated 或 existing_chrome")
    from .existing_browser import validate_browser_endpoint
    validate_browser_endpoint(endpoint)


@dataclass(frozen=True)
class Plan:
    """一个计划的可序列化快照；keywords保留完整原文和用户顺序。"""

    name: str = "日常关键词截图"
    project: str = "零跑-SEO"
    keywords: list[str] = field(default_factory=lambda: ["零跑"])
    platforms: list[str] = field(default_factory=lambda: list(PLATFORMS))
    clients: list[str] = field(default_factory=lambda: list(CLIENTS))
    capture_kinds: list[str] = field(default_factory=lambda: list(KINDS))
    top_n: int = 20
    output_root: str = field(default_factory=lambda: str(default_output_root()))
    directory_template: str = DEFAULT_DIRECTORY
    filename_template: str = DEFAULT_FILENAME
    composite_directory_template: str = "{project}-{year}-{month}月/{platform}/{month:02d}{day:02d}-{display_type}-{client}/汇总"
    composite_filename_template: str = "{content_label}_{batch:02d}_汇总_{part:02d}_{artifact_id}.png"
    composite_columns: int = 4
    composite_cell_width: int = 1080
    composite_layout: str = "grid"
    # 已弃用：仅兼容旧 JSON 并保留快照原值，不作为汇总图的有效上限。
    composite_max_pixels: int | None = None
    composite_max_height: int | None = None
    capture_timeout_seconds: int = 180
    max_retries: int = 1
    retry_delay_seconds: float = 2.0
    schedule_grace_seconds: int = 120
    max_queue_delay_seconds: int = 900
    rank_policy: str = "independent_content_cards"
    capture_mode: str = "visual_scrollshot"
    schedule: dict = field(default_factory=lambda: {
        "windows": [{"start": "09:00", "end": "21:00", "interval_minutes": 60}], "times": []
    })
    android_serial: str | None = None
    browser_profile_root: str | None = None
    browser_connection_mode: str = "dedicated"
    browser_endpoint: str = "auto"
    # 只控制独立网页采集；复用浏览器保留原方式，登录始终可见。
    browser_headless: bool = False

    def __post_init__(self) -> None:
        """在生成任务前校验配置，阻止空关键词、错误枚举和相对导出目录。"""
        for label, value in (("计划名称", self.name), ("项目名称", self.project)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label}应为非空文本")
        if not isinstance(self.keywords, list) or not self.keywords:
            raise ValueError("至少配置一个关键词")
        if any(not isinstance(k, str) or not k.strip() or any(ord(c) < 32 for c in k) for k in self.keywords):
            raise ValueError("关键词应为非空文本，且不含控制字符")
        if len(set(self.keywords)) != len(self.keywords):
            raise ValueError("关键词有完全相同的重复项")
        for label, selected, allowed in (
            ("平台", self.platforms, PLATFORMS), ("采集端", self.clients, CLIENTS),
            ("截图种类", self.capture_kinds, KINDS),
        ):
            if not isinstance(selected, list) or not selected or any(v not in allowed for v in selected):
                raise ValueError(f"{label}选项无效")
            if len(set(selected)) != len(selected):
                raise ValueError(f"{label}存在重复选项")
        if type(self.top_n) is not int or not 1 <= self.top_n <= 1000:
            raise ValueError("TOP N应为1至1000的整数")
        if not isinstance(self.output_root, str) or not Path(self.output_root).is_absolute():
            raise ValueError("导出根目录应为绝对路径")
        if not isinstance(self.schedule, dict):
            raise ValueError("时间计划应为对象")
        from .scheduling import daily_slots
        from datetime import date
        daily_slots(date(2026, 9, 8), self.schedule)
        from .exporting import validate_template
        validate_template(self.directory_template)
        validate_template(self.filename_template)
        validate_template(self.composite_directory_template)
        validate_template(self.composite_filename_template)
        if not self.filename_template or "/" in self.filename_template or "\\" in self.filename_template:
            raise ValueError("文件名模板应为单个文件名")
        if not self.filename_template.lower().endswith(".png") or not self.composite_filename_template.lower().endswith(".png"):
            raise ValueError("首版原图与派生图导出使用PNG，请保留.png后缀")
        if any(c in self.composite_filename_template for c in "/\\"):
            raise ValueError("拼图文件名模板应为单个文件名")
        if type(self.composite_columns) is not int or not 1 <= self.composite_columns <= 100:
            raise ValueError("拼图列数应为1至100的整数")
        for name, minimum, maximum in (
            ("composite_cell_width", 64, 4096), ("capture_timeout_seconds", 1, 1800),
            ("max_retries", 0, 5), ("schedule_grace_seconds", 0, 3600),
            ("max_queue_delay_seconds", 1, 86400),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name}应为{minimum}至{maximum}的整数")
        for name in ("composite_max_pixels", "composite_max_height"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"已弃用字段{name}仅兼容非负整数或null，其值不限制汇总图尺寸")
        if isinstance(self.retry_delay_seconds, bool) or not isinstance(self.retry_delay_seconds, (int, float)) or not 0 <= self.retry_delay_seconds <= 60:
            raise ValueError("重试间隔应为0至60秒")
        if self.composite_layout not in ("grid", "horizontal"):
            raise ValueError("拼图布局应为grid或horizontal")
        if self.rank_policy != "independent_content_cards":
            raise ValueError("计数口径应为独立视频/笔记结果")
        if self.capture_mode not in ("visual_scrollshot", "strict_evidence"):
            raise ValueError("截图模式应为visual_scrollshot或strict_evidence")
        if self.android_serial is not None and (not isinstance(self.android_serial, str) or not self.android_serial.strip()):
            raise ValueError("设备串号应为空或非空字符串")
        validate_browser_connection(self.browser_connection_mode, self.browser_endpoint)
        if type(self.browser_headless) is not bool:
            raise ValueError("后台无头采集 browser_headless 应为布尔值 true 或 false")
        if self.browser_profile_root is not None and (not isinstance(self.browser_profile_root, str) or not Path(self.browser_profile_root).is_absolute()):
            raise ValueError("WEB专属会话目录应为空或绝对路径")
        if "WEB" in self.clients and self.browser_connection_mode == "dedicated":
            from .sessions import profile_directory
            for platform in self.platforms:
                profile_directory(self, platform)

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        """从JSON对象构造计划；深复制避免运行中修改输入对象改变快照。"""
        if not isinstance(data, dict):
            raise ValueError("配置文件顶层应为对象")
        try:
            return cls(**json.loads(json.dumps(data, ensure_ascii=False)))
        except TypeError as exc:
            raise ValueError(f"配置字段错误：{exc}") from exc

    def to_dict(self) -> dict:
        """返回独立副本供GUI显示或批次留档。"""
        return asdict(self)


def load_plan(path: str | Path) -> Plan:
    """读取UTF-8/UTF-8 BOM配置。"""
    return Plan.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def save_plan(plan: Plan, path: str | Path) -> None:
    """保存用户明确选择的配置文件；采集图片不使用此覆盖写入接口。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
