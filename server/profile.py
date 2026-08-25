"""profile / 长期印象摘要 / 日记 / 日历 / 头像的 JSON 落盘。

全部文件写入都过 _write_json：先写临时文件再 os.replace。
直接覆盖写的话，写到一半断电就得到一个碎的 profile.json，
里面是你几个月的记忆。
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from server.config import (
    AVATAR_PATH,
    CALENDAR_PATH,
    DIARY_PATH,
    PROFILE_PATH,
    SUMMARY_PATH,
)

MAX_MEMORY_CHARS = 4000
MAX_MEMORIES = 200
MAX_PROFILE_CHARS = 200_000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def _write_json(path: Path, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) > MAX_PROFILE_CHARS:
        raise ValueError("内容太大了")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def _trim(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# ---------- profile ----------

def empty_profile() -> dict[str, Any]:
    return {
        "fullName": "",
        "nickname": "",
        "savedMemories": [],
        "preferences": {"enabled": True, "content": ""},
        "updatedAt": _now_ms(),
    }


def _coerce_memory(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        content, raw = _trim(item, MAX_MEMORY_CHARS), {}
    elif isinstance(item, dict):
        content, raw = _trim(item.get("content"), MAX_MEMORY_CHARS), item
    else:
        return None
    if not content:
        return None
    now = _now_ms()
    source = _trim(raw.get("source"), 80) or "manual"
    if source not in {"manual", "auto", "migration"}:
        source = "manual"
    return {
        "id": _trim(raw.get("id"), 120) or uuid4().hex,
        "content": content,
        "enabled": bool(raw.get("enabled", True)),
        "source": source,
        "createdAt": _safe_int(raw.get("createdAt"), now),
        "updatedAt": _safe_int(raw.get("updatedAt"), now),
    }


def normalize_profile(data: Any) -> dict[str, Any]:
    base = empty_profile()
    if not isinstance(data, dict):
        return base
    base["fullName"] = _trim(data.get("fullName"), 200)
    base["nickname"] = _trim(data.get("nickname"), 200)

    memories: list[dict[str, Any]] = []
    for item in data.get("savedMemories") or []:
        memory = _coerce_memory(item)
        if memory is not None:
            memories.append(memory)
        if len(memories) >= MAX_MEMORIES:
            break
    base["savedMemories"] = memories

    preferences = data.get("preferences") or {}
    if isinstance(preferences, str):
        preferences = {"enabled": True, "content": preferences}
    if not isinstance(preferences, dict):
        preferences = {}
    base["preferences"] = {
        "enabled": bool(preferences.get("enabled", True)),
        "content": _trim(preferences.get("content"), 50_000),
    }
    base["updatedAt"] = _safe_int(data.get("updatedAt"), _now_ms())
    return base


def read_profile() -> dict[str, Any]:
    return normalize_profile(_read_json(PROFILE_PATH, None))


def write_profile(data: Any) -> dict[str, Any]:
    profile = normalize_profile(data)
    profile["updatedAt"] = _now_ms()
    _write_json(PROFILE_PATH, profile)
    return profile


def add_saved_memory(content: str) -> tuple[dict[str, Any] | None, str, str]:
    """模型写记忆的入口。返回 (memory, reason, detail)。

    重了要告诉模型是跟哪一条重，它才知道下一步是改写还是放弃。
    """
    from server.dedupe import find_duplicate

    content = _trim(content, MAX_MEMORY_CHARS)
    if not content:
        return None, "empty", ""
    profile = read_profile()
    memories = profile["savedMemories"]
    clash = find_duplicate(content, memories)
    if clash is not None:
        return None, "duplicate", clash.get("content", "")
    if len(memories) >= MAX_MEMORIES:
        return None, "limit", f"已经存了 {MAX_MEMORIES} 条"
    now = _now_ms()
    memory = {
        "id": uuid4().hex,
        "content": content,
        "enabled": True,
        "source": "auto",
        "createdAt": now,
        "updatedAt": now,
    }
    memories.insert(0, memory)
    profile["updatedAt"] = now
    _write_json(PROFILE_PATH, profile)
    return memory, "", ""


def build_profile_context(profile: dict[str, Any] | None = None) -> str:
    """拼进 system prompt 的那一段。关掉的记忆不进。"""
    profile = normalize_profile(profile if profile is not None else read_profile())
    lines: list[str] = []
    full_name = profile.get("fullName", "")
    nickname = profile.get("nickname", "")
    if full_name or nickname:
        lines.append("User profile:")
        if full_name:
            lines.append(f"- Full name: {full_name}")
        if nickname:
            lines.append(f"- Nickname: {nickname}")

    preferences = profile.get("preferences") or {}
    preference_text = _trim(preferences.get("content"), 50_000)
    if preferences.get("enabled", True) and preference_text:
        if lines:
            lines.append("")
        lines.append("User preferences / custom instructions:")
        lines.append(preference_text)

    enabled = [
        item["content"]
        for item in profile.get("savedMemories", [])
        if item.get("enabled", True) and item.get("content")
    ]
    if enabled:
        if lines:
            lines.append("")
        lines.append("Saved memories:")
        lines.extend(f"- {content}" for content in enabled)

    summary = read_summary()
    if summary.get("enabled", True) and summary.get("content"):
        if lines:
            lines.append("")
        lines.append("Long-term impression summary:")
        lines.append(summary["content"])

    return "\n".join(lines).strip()


# ---------- 长期印象摘要 ----------

def read_summary() -> dict[str, Any]:
    data = _read_json(SUMMARY_PATH, {})
    if not isinstance(data, dict):
        data = {}
    return {
        "content": _trim(data.get("content"), 20_000),
        "enabled": bool(data.get("enabled", True)),
        "updatedAt": _safe_int(data.get("updatedAt"), 0),
        "running": bool(data.get("running", False)),
    }


def write_summary(
    content: str | None = None,
    enabled: bool | None = None,
    running: bool | None = None,
) -> dict[str, Any]:
    current = read_summary()
    if content is not None:
        current["content"] = _trim(content, 20_000)
        current["updatedAt"] = _now_ms()
    if enabled is not None:
        current["enabled"] = bool(enabled)
    if running is not None:
        current["running"] = bool(running)
    _write_json(SUMMARY_PATH, current)
    return current


# ---------- 日记 ----------

def read_diary(query: str = "") -> list[dict[str, Any]]:
    data = _read_json(DIARY_PATH, [])
    if not isinstance(data, list):
        return []
    entries = [
        {
            "date": _trim(item.get("date"), 10),
            "text": _trim(item.get("text"), 20_000),
        }
        for item in data
        if isinstance(item, dict) and item.get("text")
    ]
    entries.sort(key=lambda item: item["date"], reverse=True)
    if query:
        needle = query.strip().lower()
        entries = [item for item in entries if needle in item["text"].lower()]
    return entries


def write_diary_entry(date: str, text: str) -> list[dict[str, Any]]:
    if not DATE_RE.match(date or ""):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    entries = read_diary()
    text = _trim(text, 20_000)
    entries = [item for item in entries if item["date"] != date]
    if text:
        entries.append({"date": date, "text": text})
    entries.sort(key=lambda item: item["date"], reverse=True)
    _write_json(DIARY_PATH, entries)
    return entries


# ---------- 日历 ----------

def _empty_day() -> dict[str, Any]:
    return {
        "me": {"mood": "", "event": ""},
        "partner": {"mood": "", "event": ""},
    }


def _coerce_side(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"mood": "", "event": ""}
    return {
        "mood": _trim(value.get("mood"), 40),
        "event": _trim(value.get("event"), 2000),
    }


def read_calendar() -> dict[str, Any]:
    data = _read_json(CALENDAR_PATH, {})
    return data if isinstance(data, dict) else {}


def calendar_year(year: int) -> dict[str, Any]:
    prefix = f"{year:04d}-"
    days = {
        key: {
            "me": _coerce_side(value.get("me")),
            "partner": _coerce_side(value.get("partner")),
        }
        for key, value in read_calendar().items()
        if key.startswith(prefix) and isinstance(value, dict)
    }
    return {"year": year, "startMonth": 1, "days": days}


def read_calendar_day(date: str) -> dict[str, Any]:
    if not DATE_RE.match(date or ""):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    value = read_calendar().get(date)
    if not isinstance(value, dict):
        return _empty_day()
    return {
        "me": _coerce_side(value.get("me")),
        "partner": _coerce_side(value.get("partner")),
    }


def write_calendar_day(date: str, payload: Any) -> dict[str, Any]:
    if not DATE_RE.match(date or ""):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    if not isinstance(payload, dict):
        payload = {}
    day = {
        "me": _coerce_side(payload.get("me")),
        "partner": _coerce_side(payload.get("partner")),
    }
    calendar = read_calendar()
    empty = day == _empty_day()
    if empty:
        calendar.pop(date, None)
    else:
        calendar[date] = day
    _write_json(CALENDAR_PATH, calendar)
    return day


# ---------- 头像 ----------

def read_avatars() -> dict[str, Any]:
    data = _read_json(AVATAR_PATH, {})
    if not isinstance(data, dict):
        data = {}
    return {
        "me": {"url": _trim((data.get("me") or {}).get("url"), 2000)},
        "ai": {"url": _trim((data.get("ai") or {}).get("url"), 2000)},
    }


def write_avatars(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    data = {
        "me": {"url": _trim((payload.get("me") or {}).get("url"), 2000)},
        "ai": {"url": _trim((payload.get("ai") or {}).get("url"), 2000)},
    }
    _write_json(AVATAR_PATH, data)
    return data
