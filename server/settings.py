"""运行期配置。可视化界面要改的东西都在这里，不在 .env。

为什么分两层：`.env` 是开机就得有的东西（密码、签名密钥），
改它必然要重启；`data/settings.json` 是随时想改的东西（模型、MCP、
轮数、自定义 CSS），改完重读一下就生效。

settings.json 里留空的项会回退到 .env，所以你现有的 .env 继续有效，
不需要先把它搬干净。

api_key 读出来一律打码（`sk-1234…cdef`），写入时只有传了新值才覆盖。
界面上看不到完整 key，也就不会因为登录密码泄了连带把 key 送出去。
"""

import json
import os
import threading
import uuid
from typing import Any

from server import config

SETTINGS_PATH = config.DATA_DIR / "settings.json"

_lock = threading.Lock()
_cache: dict[str, Any] | None = None

# 这些键存在 settings.json 里；留空 / 缺失就用 .env 的值。
_TEXT_KEYS = ("openai_base_url", "openai_api_key", "chat_model", "summary_model")
_INT_KEYS = ("max_tokens", "history_turns", "request_timeout", "max_tool_rounds")
_BOOL_KEYS = ("tools_enabled", "debug_log")

# 自定义 CSS 单独处理：不修剪空白、限长得多。
MAX_CSS_CHARS = 200_000


def _blank() -> dict[str, Any]:
    return {
        "openai_base_url": "",
        "openai_api_key": "",
        "chat_model": "",
        "summary_model": "",
        "max_tokens": 0,
        "history_turns": 0,
        "request_timeout": 0,
        "max_tool_rounds": 8,
        "tools_enabled": True,
        "debug_log": False,
        "models": [],
        "mcp_servers": [],
        "custom_css": "",
        "custom_css_enabled": True,
    }


def _legacy_models() -> list[dict[str, Any]]:
    """首次运行时把旧的 server/models.json 搬进来，省得你重填一遍。"""
    try:
        data = json.loads((config.ROOT / "models.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return _clean_models(data)


def _read_file() -> dict[str, Any]:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    merged = _blank()
    merged.update({key: value for key, value in data.items() if key in merged})
    merged["models"] = _clean_models(merged.get("models"))
    merged["mcp_servers"] = _clean_servers(merged.get("mcp_servers"))
    merged["custom_css"] = str(merged.get("custom_css") or "")[:MAX_CSS_CHARS]
    if not merged["models"]:
        merged["models"] = _legacy_models()
    return merged


def _write_file(data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, SETTINGS_PATH)


def current() -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _read_file()
        return dict(_cache)


def reload() -> dict[str, Any]:
    """热重载：丢掉缓存重读文件。界面上那个按钮调的就是这里。"""
    global _cache
    with _lock:
        _cache = _read_file()
        return dict(_cache)


def _str(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _clean_models(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:100]:
        if not isinstance(item, dict):
            continue
        model_id = _str(item.get("id"), 200)
        if not model_id:
            continue
        thinking = _str(item.get("thinking"), 20) or "none"
        if thinking not in {"none", "adaptive", "extended"}:
            thinking = "none"
        out.append(
            {
                "id": model_id,
                "label": _str(item.get("label"), 60) or model_id,
                "desc": _str(item.get("desc"), 120),
                "thinking": thinking,
                "primary": bool(item.get("primary", True)),
            }
        )
    return out


def _clean_servers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:50]:
        if not isinstance(item, dict):
            continue
        url = _str(item.get("url"), 500)
        if not url:
            continue
        out.append(
            {
                "id": _str(item.get("id"), 40) or uuid.uuid4().hex[:12],
                "name": _str(item.get("name"), 60) or url,
                "url": url,
                "token": _str(item.get("token"), 2000),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return out


# ---------- 生效值（settings.json 优先，否则 .env） ----------

def base_url() -> str:
    raw = _str(current().get("openai_base_url")) or config.OPENAI_BASE_URL
    raw = raw.rstrip("/")
    if raw.endswith("/chat/completions"):
        raw = raw[: -len("/chat/completions")]
    return raw


def api_key() -> str:
    return _str(current().get("openai_api_key"), 2000) or config.OPENAI_API_KEY


def chat_model() -> str:
    return _str(current().get("chat_model"), 200) or config.CHAT_MODEL


def summary_model() -> str:
    return _str(current().get("summary_model"), 200) or config.SUMMARY_MODEL


def _positive(key: str, fallback: int) -> int:
    try:
        value = int(current().get(key) or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else fallback


def max_tokens() -> int:
    return _positive("max_tokens", config.MAX_TOKENS)


def history_turns() -> int:
    return _positive("history_turns", config.HISTORY_TURNS)


def request_timeout() -> int:
    return _positive("request_timeout", config.REQUEST_TIMEOUT)


def max_tool_rounds() -> int:
    return max(1, min(_positive("max_tool_rounds", 8), 30))


def tools_enabled() -> bool:
    return bool(current().get("tools_enabled", True))


def debug_log() -> bool:
    return bool(current().get("debug_log", False))


def custom_css() -> str:
    """关掉开关时返回空串，但正文还存着——方便你一键关掉看原样。"""
    data = current()
    if not data.get("custom_css_enabled", True):
        return ""
    return str(data.get("custom_css") or "")


def models() -> list[dict[str, Any]]:
    listed = current().get("models") or []
    if listed:
        return listed
    # 一个都没配时至少报默认模型，不然前端菜单是空的。
    name = chat_model()
    return [{
        "id": name,
        "label": name,
        "desc": "默认模型",
        "thinking": "adaptive",
        "primary": True,
    }]


def mcp_servers(only_enabled: bool = False) -> list[dict[str, Any]]:
    listed = current().get("mcp_servers") or []
    if only_enabled:
        return [item for item in listed if item.get("enabled", True)]
    return listed


# ---------- 写入 ----------

def mask_key(value: str) -> str:
    """sk-abcd…wxyz。太短的就全遮，别把短 key 露出大半。"""
    if not value:
        return ""
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:6]}\u2026{value[-4:]}"


def public_view() -> dict[str, Any]:
    """给界面看的版本：token 和 key 全部打码。"""
    data = current()
    servers = []
    for item in data.get("mcp_servers") or []:
        servers.append({
            "id": item["id"],
            "name": item["name"],
            "url": item["url"],
            "enabled": item.get("enabled", True),
            "has_token": bool(item.get("token")),
            "token_masked": mask_key(item.get("token", "")),
        })
    return {
        "openai_base_url": data.get("openai_base_url") or "",
        "openai_base_url_effective": base_url(),
        "openai_api_key_masked": mask_key(api_key()),
        "openai_api_key_from_env": not _str(data.get("openai_api_key"), 2000),
        "chat_model": data.get("chat_model") or "",
        "chat_model_effective": chat_model(),
        "summary_model": data.get("summary_model") or "",
        "summary_model_effective": summary_model(),
        "max_tokens": data.get("max_tokens") or 0,
        "max_tokens_effective": max_tokens(),
        "history_turns": data.get("history_turns") or 0,
        "history_turns_effective": history_turns(),
        "request_timeout": data.get("request_timeout") or 0,
        "request_timeout_effective": request_timeout(),
        "max_tool_rounds": max_tool_rounds(),
        "tools_enabled": tools_enabled(),
        "debug_log": debug_log(),
        "models": models(),
        "mcp_servers": servers,
        "custom_css": str(data.get("custom_css") or ""),
        "custom_css_enabled": bool(data.get("custom_css_enabled", True)),
    }


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """局部更新。没传的键不动，openai_api_key 传空串也不动。

    key 那一项特殊对待是必需的：界面上显示的是打码值，若原样收下
    会把真 key 覆盖成 `sk-123…cdef` 这种垃圾。想清空传 "__clear__"。
    """
    global _cache
    with _lock:
        data = _read_file()
        for key in _TEXT_KEYS:
            if key not in patch:
                continue
            value = _str(patch.get(key), 2000)
            if key == "openai_api_key":
                if value == "__clear__":
                    data[key] = ""
                elif value and "\u2026" not in value and "*" not in value:
                    data[key] = value
                continue
            data[key] = value
        for key in _INT_KEYS:
            if key in patch:
                try:
                    data[key] = max(0, int(patch[key] or 0))
                except (TypeError, ValueError):
                    pass
        for key in _BOOL_KEYS:
            if key in patch:
                data[key] = bool(patch[key])
        if "custom_css" in patch:
            # 不做 strip：CSS 里的缩进和换行得原样留着。
            data["custom_css"] = str(patch["custom_css"] or "")[:MAX_CSS_CHARS]
        if "custom_css_enabled" in patch:
            data["custom_css_enabled"] = bool(patch["custom_css_enabled"])
        if "models" in patch:
            data["models"] = _clean_models(patch["models"])
        if "mcp_servers" in patch:
            data["mcp_servers"] = _merge_servers(
                data.get("mcp_servers") or [], patch["mcp_servers"]
            )
        _write_file(data)
        _cache = data
        return dict(data)


def _merge_servers(existing: list[dict[str, Any]], incoming: Any) -> list[dict[str, Any]]:
    """界面传回来的 token 是打码的，所以没填新 token 就沿用旧的。"""
    by_id = {item["id"]: item for item in existing}
    cleaned = _clean_servers(incoming)
    out = []
    for item in cleaned:
        token = item.get("token", "")
        if not token or "\u2026" in token or set(token) == {"*"}:
            token = by_id.get(item["id"], {}).get("token", "")
        item["token"] = token
        out.append(item)
    return out
