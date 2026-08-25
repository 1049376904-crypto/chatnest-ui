"""内置时钟。规则与仓库根目录的 server-clock.py 一致，只是挪进包里好 import。

一律 datetime.now(ZoneInfo(APP_TIMEZONE))，不要用 naive datetime.now()——
服务器在美国、人在东八区的话，模型会在下午跟你说晚安。
库里时间戳按 UTC 存，只拿来相减。
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
GAP_MIN_SEC = 300


def timezone_name() -> str:
    return os.environ.get("APP_TIMEZONE", "Asia/Shanghai")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def enabled() -> bool:
    raw = (os.environ.get("CLOCK_ENABLED", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def now_local() -> datetime:
    return datetime.now(ZoneInfo(timezone_name()))


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_gap(seconds: float) -> str:
    total = int(max(0, seconds))
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"
    days, hours = divmod(hours, 24)
    if days < 30:
        return f"{days}天{hours}小时" if hours else f"{days}天"
    return f"{days}天"


def clock_line(last_seen: str | None = None) -> str:
    local = now_local()
    line = (
        f"{local.year}年{local.month}月{local.day}日 "
        f"{_WEEKDAY_ZH[local.weekday()]} {local.strftime('%H:%M')}"
        f"（{timezone_name()}）"
    )
    last_ts = parse_ts(last_seen)
    if last_ts is not None:
        gap = (datetime.now(timezone.utc) - last_ts).total_seconds()
        if gap >= _env_int("CLOCK_GAP_MIN_SEC", GAP_MIN_SEC):
            line += f" · 距上次说话 {format_gap(gap)}"
    return line


def prompt_note(last_seen: str | None = None) -> str:
    if not enabled():
        return ""
    try:
        return f"\n\n[现在] {clock_line(last_seen)}"
    except Exception:
        return ""
