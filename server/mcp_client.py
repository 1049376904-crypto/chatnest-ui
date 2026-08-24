"""MCP 客户端（streamable HTTP）。

一个地址后面挂多少个工具都无所谓：`tools/list` 拿回来多少就全部交给模型，
这边不做筛选。

三个要命的细节：

1. 响应体可能是 `application/json`，也可能是 `text/event-stream`（同一个
   接口两种写法都合规）。两种都得能解。
2. 握手完成后服务端可能给一个 `Mcp-Session-Id`，后续请求必须带上。
   它会过期，过期后上游报 404/400，得重新握一次而不是直接报错。
3. OpenAI 对工具名的要求是 `^[a-zA-Z0-9_-]{1,64}$`。MCP 那边名字里带
   中文、点、斜杠都很常见，不洗一遗上游直接 400。洗完要留一张映射表
   回查真名，调用时用真名。
"""

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from server import settings

logger = logging.getLogger("chatnest.mcp")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "chatnest", "version": "1.0.0"}
HANDSHAKE_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_RESULT_CHARS = 30_000
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]+")


class MCPError(RuntimeError):
    pass


def _parse_body(response: httpx.Response) -> dict[str, Any]:
    """JSON 或 SSE 都收。SSE 里只关心带 id 的那一帧（即响应，非通知）。"""
    text = response.text or ""
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/event-stream" in content_type or text.lstrip().startswith("event:"):
        last: dict[str, Any] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                frame = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict) and ("result" in frame or "error" in frame):
                last = frame
        if last:
            return last
        raise MCPError("SSE 响应里没有可用的 JSON-RPC 帧")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPError(f"响应不是 JSON：{text[:200]}") from exc
    if not isinstance(data, dict):
        raise MCPError("响应格式不对")
    return data


class MCPServer:
    """一个 MCP 端点。session id 缓存复用，失效就重握一次。"""

    def __init__(self, config_item: dict[str, Any]) -> None:
        self.id = config_item["id"]
        self.name = config_item.get("name") or config_item["url"]
        self.url = config_item["url"]
        self.token = config_item.get("token") or ""
        self._session_id: str | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    def _headers(self, session_id: str | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    def _rpc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        session_id: str | None,
        expect_result: bool = True,
    ) -> tuple[dict[str, Any], str | None]:
        response = await client.post(
            self.url, headers=self._headers(session_id), json=payload
        )
        new_session = response.headers.get("mcp-session-id") or session_id
        if response.status_code in (400, 404) and session_id:
            # 很可能是 session 过期，交给上层重握。
            raise _SessionExpired()
        if response.status_code == 202 and not expect_result:
            return {}, new_session
        if response.status_code >= 400:
            raise MCPError(
                f"HTTP {response.status_code}：{(response.text or '')[:300]}"
            )
        if not expect_result:
            return {}, new_session
        data = _parse_body(response)
        if "error" in data:
            error = data["error"] or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise MCPError(f"MCP 报错：{message}")
        return data.get("result") or {}, new_session

    async def _handshake(self, client: httpx.AsyncClient) -> str | None:
        result, session_id = await self._send(
            client,
            {
                "jsonrpc": "2.0",
                "id": self._rpc_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            },
            None,
        )
        logger.debug("mcp initialize ok server=%s info=%s", self.name, result.get("serverInfo"))
        # initialized 是通知，没有回包；不发的话严格实现会拒后续请求。
        try:
            await self._send(
                client,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                session_id,
                expect_result=False,
            )
        except (MCPError, _SessionExpired):
            pass
        return session_id

    async def _call(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        async with self._lock:
            for attempt in (1, 2):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout, connect=15.0)
                ) as client:
                    try:
                        if self._session_id is None:
                            self._session_id = await self._handshake(client)
                        result, session_id = await self._send(
                            client,
                            {
                                "jsonrpc": "2.0",
                                "id": self._rpc_id(),
                                "method": method,
                                "params": params,
                            },
                            self._session_id,
                        )
                        self._session_id = session_id
                        return result
                    except _SessionExpired:
                        self._session_id = None
                        if attempt == 2:
                            raise MCPError("session 反复失效，握手不上")
                    except httpx.TimeoutException as exc:
                        raise MCPError(f"连接超时：{self.url}") from exc
                    except httpx.HTTPError as exc:
                        raise MCPError(f"连不上 {self.url}：{exc}") from exc
        raise MCPError("调用失败")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._call("tools/list", {}, HANDSHAKE_TIMEOUT)
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        result = await self._call(
            "tools/call",
            {"name": name, "arguments": arguments},
            CALL_TIMEOUT,
        )
        is_error = bool(result.get("isError"))
        chunks: list[str] = []
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                chunks.append(str(block.get("text") or ""))
            elif kind == "resource":
                resource = block.get("resource") or {}
                chunks.append(str(resource.get("text") or resource.get("uri") or ""))
            else:
                chunks.append(f"[{kind} 内容，无法转成文字]")
        text = "\n".join(part for part in chunks if part).strip()
        if not text:
            # 有些 server 只回 structuredContent
            structured = result.get("structuredContent")
            if structured is not None:
                text = json.dumps(structured, ensure_ascii=False)[:MAX_RESULT_CHARS]
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n\n[结果过长，已截断]"
        return text or "(空结果)", is_error


class _SessionExpired(Exception):
    pass


def _slug(value: str) -> str:
    cleaned = _NAME_OK.sub("_", value).strip("_")
    return cleaned[:40] or "mcp"


class ToolRegistry:
    """把所有 server 的工具汇总成一张 OpenAI 格式的清单。"""

    def __init__(self) -> None:
        self.schemas: list[dict[str, Any]] = []
        self.mapping: dict[str, tuple[MCPServer, str]] = {}
        self.errors: list[dict[str, str]] = []
        self.by_server: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def refresh(self) -> dict[str, Any]:
        async with self._lock:
            schemas: list[dict[str, Any]] = []
            mapping: dict[str, tuple[MCPServer, str]] = {}
            errors: list[dict[str, str]] = []
            by_server: dict[str, list[str]] = {}
            used: set[str] = set()
            for item in settings.mcp_servers(only_enabled=True):
                server = MCPServer(item)
                try:
                    tools = await server.list_tools()
                except MCPError as exc:
                    logger.warning("mcp list_tools failed server=%s: %s", server.name, exc)
                    errors.append({"server": server.name, "error": str(exc)})
                    continue
                names: list[str] = []
                for tool in tools:
                    if not isinstance(tool, dict):
                        continue
                    real_name = str(tool.get("name") or "").strip()
                    if not real_name:
                        continue
                    exposed = _slug(real_name)
                    if exposed in used:
                        exposed = f"{_slug(server.name)}__{exposed}"[:64]
                    suffix = 2
                    while exposed in used:
                        exposed = f"{exposed[:60]}_{suffix}"
                        suffix += 1
                    used.add(exposed)
                    schema = tool.get("inputSchema")
                    if not isinstance(schema, dict) or not schema:
                        schema = {"type": "object", "properties": {}}
                    schemas.append({
                        "type": "function",
                        "function": {
                            "name": exposed,
                            "description": str(tool.get("description") or real_name)[:1000],
                            "parameters": schema,
                        },
                    })
                    mapping[exposed] = (server, real_name)
                    names.append(real_name)
                by_server[server.name] = names
            self.schemas = schemas
            self.mapping = mapping
            self.errors = errors
            self.by_server = by_server
            self._loaded = True
            logger.info(
                "mcp tools refreshed: %d 个工具 / %d 个 server，%d 个失败",
                len(schemas), len(by_server), len(errors),
            )
            return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self._loaded,
            "tool_count": len(self.schemas),
            "tools": [
                {"exposed": name, "real": real, "server": server.name}
                for name, (server, real) in self.mapping.items()
            ],
            "by_server": self.by_server,
            "errors": self.errors,
        }

    async def ensure_loaded(self) -> None:
        if not self._loaded and settings.mcp_servers(only_enabled=True):
            await self.refresh()

    def tool_schemas(self) -> list[dict[str, Any]]:
        return self.schemas

    async def call(self, exposed_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        entry = self.mapping.get(exposed_name)
        if entry is None:
            return f"没有这个工具：{exposed_name}", True
        server, real_name = entry
        try:
            return await server.call_tool(real_name, arguments)
        except MCPError as exc:
            logger.warning("tool call failed %s: %s", exposed_name, exc)
            return f"工具调用失败：{exc}", True


registry = ToolRegistry()


async def probe(url: str, token: str = "") -> dict[str, Any]:
    """界面上那个「测试连接」按钮。把握手结果直接说清楚，不让你去翻日志。"""
    server = MCPServer({"id": "probe", "name": url, "url": url, "token": token})
    try:
        tools = await server.list_tools()
    except MCPError as exc:
        return {"ok": False, "error": str(exc), "tools": []}
    return {
        "ok": True,
        "error": "",
        "tools": [
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or "")[:200],
            }
            for tool in tools
            if isinstance(tool, dict)
        ],
    }
