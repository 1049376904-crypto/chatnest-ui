"""OpenAI 兼容的 chat completions 客户端。

中转站的字段各家不一样，所以 delta 里思考链那个 key 列了四种常见写法
（reasoning_content / reasoning / thinking / thought），命中哪个算哪个。
对方不吐思考链也无妨，前端那一栏不出现而已。

工具调用（tool_use / tool_result 事件）这边不接——中转 API 背后没有
执行环境，没东西可调。前端那两个事件分支就永远不会触发。
"""

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from server import config
from server.uploads import IMAGE_EXTENSIONS, TEXT_EXTENSIONS, as_data_url, inline_text

logger = logging.getLogger(__name__)

THINKING_KEYS = ("reasoning_content", "reasoning", "thinking", "thought")

# effort 档位 → 思考预算。中转站对这两个字段的支持很不一致，所以两个都带上：
# OpenAI 系认 reasoning_effort，Anthropic 系认 thinking.budget_tokens。
# 不认识的一方一般直接忽略；若中转站严格校验未知字段并报 400，
# 把 THINKING_BUDGET 里对应档位改成 None 即可。
EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high"}
THINKING_BUDGET = {"low": 2048, "medium": 8192, "high": 24576}


class UpstreamError(RuntimeError):
    """上游报的错，带上能给人看的一句。"""


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {config.OPENAI_API_KEY}"
    return headers


def _attachment_parts(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """附件 → content parts。图片转 data URL，文本直接贴正文。"""
    parts: list[dict[str, Any]] = []
    for item in attachments or []:
        raw_path = item.get("path")
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            try:
                url = as_data_url(path, item.get("mime") or "image/png")
            except OSError:
                continue
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif suffix in TEXT_EXTENSIONS:
            text = inline_text(path)
            if text:
                parts.append({
                    "type": "text",
                    "text": f"[附件 {item.get('name') or path.name}]\n{text}",
                })
        else:
            parts.append({
                "type": "text",
                "text": f"[用户上传了文件 {item.get('name') or path.name}，当前无法读取其内容]",
            })
    return parts


def build_messages(
    history: list[dict[str, Any]],
    system: str,
    clock_note: str = "",
) -> list[dict[str, Any]]:
    """库里的历史 → chat completions 的 messages。

    时钟那一行拼在最后一条用户消息尾巴上，而不是 system 里——
    放 system 里会被中转站的 prompt 缓存镀住，模型就永远停在首次那个时间。
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    last_user_index = -1
    for index, item in enumerate(history):
        if item["role"] == "user":
            last_user_index = index
    for index, item in enumerate(history):
        text = item.get("text") or ""
        if index == last_user_index and clock_note:
            text = f"{text}{clock_note}"
        parts = _attachment_parts(item.get("attachments") or [])
        if parts:
            content: Any = ([{"type": "text", "text": text}] if text else []) + parts
        else:
            content = text
        if not content:
            continue
        messages.append({"role": item["role"], "content": content})
    return messages


def _delta_thinking(delta: dict[str, Any]) -> str:
    for key in THINKING_KEYS:
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _error_message(status: int, body: str) -> str:
    """上游错误 → 一句人话。中转站的错误体格式也不统一，尽量挖。"""
    detail = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif isinstance(error, str):
                detail = error
            detail = detail or str(data.get("message") or "")
    except (json.JSONDecodeError, TypeError):
        detail = body[:200]
    if status == 401:
        return "中转 API 报 401：检查 OPENAI_API_KEY。"
    if status == 404:
        return f"中转 API 报 404：检查 OPENAI_BASE_URL 和模型名。{detail}".strip()
    if status == 429:
        return "中转 API 报 429：额度或频率限制，稍后再试。"
    if status >= 500:
        return f"中转 API 服务端错误（{status}）。{detail}".strip()
    return f"中转 API 报错（{status}）：{detail or '无详细信息'}"


async def stream_chat(
    messages: list[dict[str, Any]],
    model: str,
    effort: str = "medium",
    extended: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """流式拉回复。产出 {'event': 'thinking'|'delta'|'done'} 三种。

    不在这里写库也不在这里拼 SSE，那是 main.py 的事。
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": config.MAX_TOKENS,
    }
    if extended:
        level = EFFORT_MAP.get(effort, "medium")
        payload["reasoning_effort"] = level
        budget = THINKING_BUDGET.get(effort)
        if budget:
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}

    url = f"{config.OPENAI_BASE_URL}/chat/completions"
    timeout = httpx.Timeout(config.REQUEST_TIMEOUT, connect=20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", url, headers=_headers(), json=payload
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")
                    logger.error(
                        "upstream error status=%s body=%s",
                        response.status_code,
                        body[:500],
                    )
                    raise UpstreamError(_error_message(response.status_code, body))
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    thinking = _delta_thinking(delta)
                    if thinking:
                        yield {"event": "thinking", "text": thinking}
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        yield {"event": "delta", "text": text}
                    elif isinstance(text, list):
                        # 少数中转站把 content 包成 parts 数组
                        for part in text:
                            if isinstance(part, dict) and part.get("type") == "text":
                                piece = part.get("text") or ""
                                if piece:
                                    yield {"event": "delta", "text": piece}
    except httpx.TimeoutException as exc:
        raise UpstreamError("中转 API 超时了，稍后再试。") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"连不上中转 API：{exc}") from exc
    yield {"event": "done"}


async def complete(
    messages: list[dict[str, Any]],
    model: str | None = None,
    max_tokens: int = 300,
) -> str:
    """非流式一句话。给两条摘要接口用，默认走便宜模型。"""
    payload = {
        "model": model or config.SUMMARY_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    url = f"{config.OPENAI_BASE_URL}/chat/completions"
    timeout = httpx.Timeout(60.0, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=_headers(), json=payload)
    if response.status_code >= 400:
        raise UpstreamError(_error_message(response.status_code, response.text))
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
        ).strip()
    return (content or "").strip()


async def summarize_thinking(thinking: str) -> str:
    if not thinking.strip():
        return ""
    return await complete(
        [
            {
                "role": "system",
                "content": (
                    "用一句 15-20 字的中文概括这段思考在干什么。"
                    "只输出那一句，不要引号、不要前缀。"
                ),
            },
            {"role": "user", "content": thinking[:8000]},
        ],
        max_tokens=80,
    )


async def summarize_tool_use(name: str, tool_input: Any, tool_output: str) -> str:
    detail = json.dumps(tool_input, ensure_ascii=False)[:1500]
    return await complete(
        [
            {
                "role": "system",
                "content": (
                    "用一句 15-20 字的中文说明这次工具调用做了什么。"
                    "只输出那一句。"
                ),
            },
            {
                "role": "user",
                "content": f"工具：{name}\n参数：{detail}\n结果：{tool_output[:1500]}",
            },
        ],
        max_tokens=80,
    )


async def generate_memory_summary(transcript: str) -> str:
    """从最近几个对话里整理长期印象。用主模型，这一条质量重于价钱。"""
    if not transcript.strip():
        return ""
    return await complete(
        [
            {
                "role": "system",
                "content": (
                    "你在整理对一个人的长期印象。读完下面的对话节选，"
                    "写 200-400 字的中文摘要：他是怎样的人、在意什么、"
                    "习惯怎么说话。只写看得出来的，不要编。不要分条，"
                    "写成连贯的段落。"
                ),
            },
            {"role": "user", "content": transcript[:40_000]},
        ],
        model=config.CHAT_MODEL,
        max_tokens=1200,
    )
