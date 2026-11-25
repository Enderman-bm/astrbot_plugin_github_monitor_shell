from datetime import datetime
from typing import List, Optional, Set, Tuple
from zoneinfo import ZoneInfo


def format_commit_datetime(
    date_str: str,
    time_zone: str,
    time_format: str,
) -> Optional[str]:
    try:
        normalized = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        target_tz = ZoneInfo(time_zone)
        return dt.astimezone(target_tz).strftime(time_format)
    except Exception:
        return None


_WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _parse_time_range(range_str) -> Optional[Tuple[int, int]]:
    """解析单个 "HH:MM-HH:MM" 时段为 (开始分钟, 结束分钟)；非法返回 None

    结束小于开始时视为跨天时段（如 22:00-07:00 表示 22 点到次日 7 点）。
    """
    try:
        start_str, end_str = str(range_str).split("-", 1)
        sh, sm = start_str.strip().split(":")
        eh, em = end_str.strip().split(":")
        start = int(sh) * 60 + int(sm)
        end = int(eh) * 60 + int(em)
    except (ValueError, AttributeError):
        return None
    if not (0 <= start < 24 * 60 and 0 <= end < 24 * 60):
        return None
    return start, end


def parse_time_ranges(ranges) -> List[Tuple[int, int]]:
    """解析时段列表，返回合法项 [(start_minutes, end_minutes)]（支持跨天）"""
    parsed: List[Tuple[int, int]] = []
    for item in ranges or []:
        parsed_range = _parse_time_range(item)
        if parsed_range is not None:
            parsed.append(parsed_range)
    return parsed


def is_in_time_ranges(now: datetime, parsed_ranges) -> bool:
    """判断 now 是否落在任一已解析的时段内（含起点、不含终点）"""
    if not parsed_ranges:
        return False
    minutes = now.hour * 60 + now.minute
    for start, end in parsed_ranges:
        if start <= end:
            if start <= minutes < end:
                return True
        # 跨天时段：当天起点之后或次日终点之前均命中
        elif minutes >= start or minutes < end:
            return True
    return False


def _parse_weekdays(value) -> Optional[Set[int]]:
    """解析星期限制为 {0(周一)..6(周日)}；未配置或全部无法识别时返回 None（不限制）

    支持数字（"0"-"6"）与英文缩写（mon/tue/...）。
    """
    if not value:
        return None
    if isinstance(value, (str, int)):
        value = [value]
    days: Set[int] = set()
    for item in value:
        text = str(item).strip().lower()
        if text in _WEEKDAY_ALIASES:
            days.add(_WEEKDAY_ALIASES[text])
            continue
        try:
            day = int(text)
        except ValueError:
            continue
        if 0 <= day <= 6:
            days.add(day)
    return days or None


def should_monitor_now(schedule: dict, time_zone: str,
                       now: Optional[datetime] = None) -> bool:
    """按 monitor_schedule 配置判断当前是否进行检测

    - blacklist：落在任一时段内 → 暂停检测；时段外正常检测
    - whitelist：只在时段内检测
    - 未配置合法时段时视为时段控制不生效（避免 whitelist 空列表把监控永久关掉）
    - 时间基准使用 time_zone（IANA 时区），解析失败时回退系统本地时间
    """
    if not isinstance(schedule, dict) or not schedule.get("enabled"):
        return True

    if now is None:
        try:
            now = datetime.now(ZoneInfo(time_zone))
        except Exception:
            now = datetime.now()

    parsed_ranges = parse_time_ranges(schedule.get("ranges"))
    if not parsed_ranges:
        return True

    weekdays = _parse_weekdays(schedule.get("weekdays"))
    if weekdays is not None and now.weekday() not in weekdays:
        # 今天不在生效星期内：whitelist 没有任何有效时段；blacklist 没有需要暂停的时段
        return False if str(schedule.get("mode", "blacklist")).lower() == "whitelist" else True

    in_range = is_in_time_ranges(now, parsed_ranges)
    if str(schedule.get("mode", "blacklist")).lower() == "whitelist":
        return in_range
    return not in_range
