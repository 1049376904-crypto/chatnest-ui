# -*- coding: utf-8 -*-
"""内置时钟：给模型的每条消息尾巴上挂一行「现在几点 + 对方多久没说话了」。

这是「对话 / 内置时钟 / memories / diary」四件里唯一没法放进前端的一件——
时间要跟着服务器的会话记录算，前端只是把结果当普通正文显示。

接法（FastAPI 为例，在组 prompt 的地方加一行）：

    from clock import prompt_note
    prompt += prompt_note(last_message_at)   # last_message_at = 上一条消息的 ISO 时间戳

⚠️ 时区：一律 datetime.now(ZoneInfo(APP_TIMEZONE))，**不要用 naive datetime.now()**。
naive 版给的是服务器所在时区的墙钟，机器和用户不在同一个时区时会差出好几个小时，
模型就会在下午说晚安。库里的时间戳按 UTC 存，只拿来相减。

环境变量：
    APP_TIMEZONE        默认 Asia/Singapore
    CLOCK_ENABLED       0 / false / no / off 关掉这一行
    CLOCK_GAP_MIN_SEC   间隔小于这个秒数就不提「距上次说话多久」，默认 300
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Asia/Singapore")
_WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 距上次说话不到这个秒数就不提——对方正连着说话，报「距上次 2 分钟」是废话。
GAP_MIN_SEC = 300


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def enabled() -> bool:
    """CLOCK_ENABLED=0 关掉这行（模型就退回只能自己调时间工具）。"""
    return (os.environ.get("CLOCK_ENABLED", "1") or "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def now_local() -> datetime:
    """用户那边的本地时间，与服务器墙钟无关。"""
    return datetime.now(ZoneInfo(APP_TIMEZONE))


def parse_ts(value: str | None) -> datetime | None:
    """库里的 ISO 时间戳 → aware datetime；裸时间戳按 UTC 认。"""
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
    """3720 → "1小时2分"；粗到能说人话就行，不报秒。"""
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
    """一行现在时间；对方离开够久就带上离开了多久。"""
    local = now_local()
    line = (
        f"{local.year}年{local.month}月{local.day}日 "
        f"{_WEEKDAY_ZH[local.weekday()]} {local.strftime('%H:%M')}"
        f"（{APP_TIMEZONE}）"
    )
    last_ts = parse_ts(last_seen)
    if last_ts is not None:
        gap = (datetime.now(timezone.utc) - last_ts).total_seconds()
        if gap >= _env_int("CLOCK_GAP_MIN_SEC", GAP_MIN_SEC):
            line += f" · 距上次说话 {format_gap(gap)}"
    return line


def prompt_note(last_seen: str | None = None) -> str:
    """给 /api/chat 用：拼在消息末尾的一行，关掉或出错时返回空串。"""
    if not enabled():
        return ""
    try:
        return f"\n\n[现在] {clock_line(last_seen)}"
    except Exception:
        return ""


if __name__ == "__main__":
    print(prompt_note().strip() or "(clock disabled)")
