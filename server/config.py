"""环境变量集中在这里读，别处不再直接碰 os.environ。

除 CHAT_PASSWORD / CHAT_SECRET 外都有默认值——缺了也起得来，
只是发消息那一刻会失败（上游 401）。这两个没有默认值是故意的：
没密码的聊天服务挂在公网上等于不设防。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(
            f"缺少环境变量 {name}。把 server/env.example 复制成 server/.env 再填。"
        )
    return value


CHAT_PASSWORD = _required("CHAT_PASSWORD")
CHAT_SECRET = _required("CHAT_SECRET")

# 中转站给的地址常见三种写法：带 /v1、不带、或带全路径。
# 统一成不带尾斜杠的 base，请求时拼 /chat/completions。
_raw_base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
_raw_base = _raw_base.rstrip("/")
if _raw_base.endswith("/chat/completions"):
    _raw_base = _raw_base[: -len("/chat/completions")]
OPENAI_BASE_URL = _raw_base
OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()

CHAT_MODEL = (os.environ.get("CHAT_MODEL") or "claude-sonnet-4-6").strip()
SUMMARY_MODEL = (os.environ.get("SUMMARY_MODEL") or "claude-haiku-4-5").strip()

MAX_TOKENS = _int("MAX_TOKENS", 8192)
HISTORY_TURNS = _int("HISTORY_TURNS", 30)
REQUEST_TIMEOUT = _int("REQUEST_TIMEOUT", 300)

DATA_DIR = Path(
    (os.environ.get("DATA_DIR") or "").strip() or (ROOT / "data")
).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "conversations.db"
PROFILE_PATH = DATA_DIR / "profile.json"
SUMMARY_PATH = DATA_DIR / "memory_summary.json"
DIARY_PATH = DATA_DIR / "diary.json"
CALENDAR_PATH = DATA_DIR / "calendar.json"
AVATAR_PATH = DATA_DIR / "avatars.json"
UPLOAD_ROOT = DATA_DIR / "uploads"

PORT = _int("PORT", 8787)


def system_prompt() -> str:
    """优先 SYSTEM_PROMPT 环境变量，其次 server/prompt.txt，都没有就空。"""
    inline = (os.environ.get("SYSTEM_PROMPT") or "").strip()
    if inline:
        return inline
    try:
        return (ROOT / "prompt.txt").read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""
