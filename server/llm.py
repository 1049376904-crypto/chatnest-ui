"""OpenAI 兼容的 chat completions 客户端，带工具调用循环。

中转站的字段各家不一样，所以 delta 里思考链那个 key 列了四种常见写法
（reasoning_content / reasoning / thinking / thought），命中哪个算哪个。
对方不吐思考链也无妨，前端那一栏不出现而已。

工具调用走 MCP：`tools` 字段带上工具清单，模型说要调哪个，
这边去 MCP server 执行，把结果拼回 messages 再问一遍，直到它不再要工具。
"""

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from server import settings
from server.mcp_client import registry
from server.uploads import IMAGE_EXTENSIONS, TEXT_EXTENSIONS, as_data_url, inline_text

logger = logging.getLogger("chatnest.llm")

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
    key = settings.api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
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
    放 system 里会被中转站的 prompt 缓存锁住，模型就永远停在首次那个时间。
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
        return "中转 API 报 401：检查 API key。"
    if status == 404:
        return f"中转 API 报 404：检查 base URL 和模型名。{detail}".strip()
    if status == 429:
        return "中转 API 报 429：额度或频率限制，稍后再试。"
    if status >= 500:
        return f"中转 API 服务端错误（{status}）。{detail}".strip()
    return f"中转 API 报错（{status}）：{detail or '无详细信息'}"


class _ToolCallAccumulator:
    """流式模式下 tool_calls 是分片来的，得按 index 攒起来。

    上游一般这么发：第一片给 index / id / function.name，后面若干片
    只给 function.arguments 的一小段字符串，要自己拼成完整 JSON。
    不累积的话拿到的是 `{"que` 这种半截东西。
    """

    def __init__(self) -> None:
        self._slots: dict[int, dict[str, Any]] = {}

    def feed(self, deltas: list[dict[str, Any]]) -> None:
        for item in deltas:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            index = int(index) if isinstance(index, int) else len(self._slots)
            slot = self._slots.setdefault(
                index, {"id": "", "name": "", "arguments": ""}
            )
            if item.get("id"):
                slot["id"] = str(item["id"])
            function = item.get("function") or {}
            if isinstance(function, dict):
                if function.get("name"):
                    slot["name"] = str(function["name"])
                chunk = function.get("arguments")
                if isinstance(chunk, str):
                    slot["arguments"] += chunk

    def finish(self) -> list[dict[str, Any]]:
        out = []
        for index in sorted(self._slots):
            slot = self._slots[index]
            if not slot["name"]:
                continue
            out.append({
                "id": slot["id"] or f"call_{index}",
                "name": slot["name"],
                "arguments": slot["arguments"],
            })
        return out


async def _stream_once(
    messages: list[dict[str, Any]],
    model: str,
    effort: str,
    extended: bool,
    tools: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """打一次上游。产出 thinking / delta，最后一帧是 round_done。"""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": settings.max_tokens(),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if extended:
        payload["reasoning_effort"] = EFFORT_MAP.get(effort, "medium")
        # 思考预算和 tools 一起送，有些中转站会 400；带工具时就不发这一项。
        if not tools:
            budget = THINKING_BUDGET.get(effort)
            if budget:
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}

    url = f"{settings.base_url()}/chat/completions"
    timeout = httpx.Timeout(settings.request_timeout(), connect=20.0)
    if settings.debug_log():
        logger.info(
            "upstream request model=%s messages=%d tools=%d\n%s",
            model, len(messages), len(tools),
            json.dumps(payload, ensure_ascii=False)[:4000],
        )

    accumulator = _ToolCallAccumulator()
    text_buffer = ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", url, headers=_headers(), json=payload
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")
                    logger.error(
                        "upstream error status=%s body=%s",
                        response.status_code, body[:1000],
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
                    tool_deltas = delta.get("tool_calls")
                    if isinstance(tool_deltas, list):
                        accumulator.feed(tool_deltas)
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        text_buffer += text
                        yield {"event": "delta", "text": text}
                    elif isinstance(text, list):
                        # 少数中转站把 content 包成 parts 数组
                        for part in text:
                            if isinstance(part, dict) and part.get("type") == "text":
                                piece = part.get("text") or ""
                                if piece:
                                    text_buffer += piece
                                    yield {"event": "delta", "text": piece}
    except httpx.TimeoutException as exc:
        raise UpstreamError("中转 API 超时了，稍后再试。") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"连不上中转 API：{exc}") from exc

    yield {
        "event": "round_done",
        "tool_calls": accumulator.finish(),
        "text": text_buffer,
    }


async def stream_chat(
    messages: list[dict[str, Any]],
    model: str,
    effort: str = "medium",
    extended: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """流式拉回复，需要工具就自己调完再接着说。

    产出 thinking / delta / tool_use / tool_result / trace_summary / done。
    不在这里写库也不在这里拼 SSE，那是 main.py 的事。
    """
    tools: list[dict[str, Any]] = []
    if settings.tools_enabled():
        try:
            await registry.ensure_loaded()
            tools = registry.tool_schemas()
        except Exception:
            logger.exception("加载 MCP 工具失败，这轮不带工具")
            tools = []

    working = list(messages)
    max_rounds = settings.max_tool_rounds()
    hit_limit = True
    # 攒下这一轮调过哪些工具，最后给前端一句概括用。
    called: list[tuple[str, str, str]] = []
    saw_thinking = False

    for _ in range(max_rounds):
        pending: list[dict[str, Any]] = []
        assistant_text = ""
        async for chunk in _stream_once(working, model, effort, extended, tools):
            if chunk["event"] == "round_done":
                pending = chunk["tool_calls"]
                assistant_text = chunk["text"]
                continue
            if chunk["event"] == "thinking":
                saw_thinking = True
            yield chunk

        if not pending:
            hit_limit = False
            break

        # 把模型这一轮的发言（含工具调用意图）记进对话，
        # 否则下一轮它不知道自己刚才说要调工具。
        working.append({
            "role": "assistant",
            "content": assistant_text or None,
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call["arguments"] or "{}",
                    },
                }
                for call in pending
            ],
        })

        for call in pending:
            raw_args = call["arguments"] or "{}"
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}
                parse_error = ""
            except json.JSONDecodeError as exc:
                arguments = {}
                parse_error = f"参数不是合法 JSON：{exc}。原文：{raw_args[:500]}"

            yield {
                "event": "tool_use",
                "id": call["id"],
                "name": call["name"],
                "input": raw_args if parse_error else arguments,
            }

            if parse_error:
                # 不抛错，把问题喂回去让模型自己改——比整轮失败友好得多。
                output, is_error = parse_error, True
            else:
                if settings.debug_log():
                    logger.info(
                        "tool call %s args=%s",
                        call["name"],
                        json.dumps(arguments, ensure_ascii=False)[:2000],
                    )
                output, is_error = await registry.call(call["name"], arguments)
                if settings.debug_log():
                    logger.info(
                        "tool result %s error=%s len=%d\n%s",
                        call["name"], is_error, len(output), output[:2000],
                    )

            called.append((call["name"], raw_args, output))
            yield {
                "event": "tool_result",
                "tool_use_id": call["id"],
                "content": output,
                "is_error": is_error,
            }
            working.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": output,
            })

    if hit_limit:
        logger.warning("工具调用达到 %d 轮上限，停下了", max_rounds)
        yield {
            "event": "delta",
            "text": f"\n\n[连续调用工具 {max_rounds} 轮仍未结束，已停止]",
        }

    # 有工具调用、但模型没吐思考链时才补这一句：前端历史渲染靠它
    # 生成那个能展开工具卡片的按钮，没有的话卡片会被折起来打不开。
    if called and not saw_thinking:
        try:
            summary = await summarize_trace_batch(called)
            if summary:
                yield {"event": "trace_summary", "text": summary}
        except Exception:
            logger.exception("工具摘要失败，跳过")

    yield {"event": "done"}


async def complete(
    messages: list[dict[str, Any]],
    model: str | None = None,
    max_tokens: int = 300,
) -> str:
    """非流式一句话。给两条摘要接口用，默认走便宜模型。"""
    payload = {
        "model": model or settings.summary_model(),
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    url = f"{settings.base_url()}/chat/completions"
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


async def summarize_trace_batch(calls: list[tuple[str, str, str]]) -> str:
    """把这一轮调过的工具概括成一句话，给前端那个折叠按钮当标题。"""
    if not calls:
        return ""
    lines = []
    for name, arguments, output in calls[:10]:
        lines.append(
            f"工具 {name}\n参数：{arguments[:400]}\n结果：{output[:600]}"
        )
    return await complete(
        [
            {
                "role": "system",
                "content": (
                    "用一句 15-25 字的中文概括这一轮调用了什么工具、拿到了什么。"
                    "只输出那一句，不要引号、不要前缀、不要分条。"
                ),
            },
            {"role": "user", "content": "\n\n".join(lines)[:8000]},
        ],
        max_tokens=100,
    )


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
        model=settings.chat_model(),
        max_tokens=1200,
    )
