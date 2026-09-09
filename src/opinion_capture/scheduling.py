"""时间窗口展开；本模块不创建系统自动化，也不将错过时点伪装成已采集。"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Asia/Shanghai")


def parse_time(value: str) -> time:
    """解析严格HH:MM格式，避免把范围误当执行频率。"""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("时间应为HH:MM")
    try:
        hour, minute = map(int, value.split(":"))
        return time(hour, minute)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"时间格式错误：{value}") from exc


def daily_slots(day: date, schedule: dict) -> list[datetime]:
    """展开业务起始日的时点，含落在间隔上的两端，跨午夜窗口落到次日。"""
    if not isinstance(schedule, dict) or set(schedule) - {"windows", "times"}:
        raise ValueError("时间计划仅支持windows和times")
    windows, explicit = schedule.get("windows", []), schedule.get("times", [])
    if not isinstance(windows, list) or not isinstance(explicit, list):
        raise ValueError("windows和times应为列表")
    result = {datetime.combine(day, parse_time(t), TIMEZONE) for t in explicit}
    for window in windows:
        if not isinstance(window, dict) or set(window) != {"start", "end", "interval_minutes"}:
            raise ValueError("时间窗口需要start/end/interval_minutes")
        interval = window["interval_minutes"]
        if type(interval) is not int or not 1 <= interval <= 1440:
            raise ValueError("执行间隔应为1至1440分钟")
        start = datetime.combine(day, parse_time(window["start"]), TIMEZONE)
        end = datetime.combine(day, parse_time(window["end"]), TIMEZONE)
        if end < start:
            end += timedelta(days=1)
        current = start
        while current <= end:
            result.add(current)
            current += timedelta(minutes=interval)
    return sorted(result)


def due_slots(now: datetime, schedule: dict, grace_seconds: int = 60) -> list[datetime]:
    """仅返回容许延迟内的计划时点；旧时点不补造历史截图。"""
    if now.tzinfo is None or grace_seconds < 0:
        raise ValueError("需要带时区的时间和非负延迟")
    local = now.astimezone(TIMEZONE)
    candidates = set(daily_slots(local.date(), schedule))
    candidates.update(daily_slots(local.date() - timedelta(days=1), schedule))
    return sorted(t for t in candidates if 0 <= (local - t).total_seconds() <= grace_seconds)
