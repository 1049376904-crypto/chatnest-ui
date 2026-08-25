"""ChatNest 后端。接 OpenAI 兼容的中转 API，工具走 MCP。

接口分两组：
- `/api/*` 聊天那一套，对齐上游 README 那张表
- `/api/admin/*` 控制台用的，读写运行期配置

启动：
    uvicorn server.main:app --host 127.0.0.1 --port 8787

不监听 0.0.0.0：本服务只有应用内密码一层防护，直接暴露到公网等于把
你的 API 额度交给扫段的。让 nginx 反代 127.0.0.1，并在 nginx 那一层上 HTTPS。
"""

import asyncio
import hashlib
import json
import logging
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.formparsers import MultiPartParser

from server import (
    auth,
    clock,
    config,
    css_meta,
    llm,
    profile as profile_store,
    settings,
    store,
)
from server.mcp_client import registry
from server.store import ConversationNotFound
from server.uploads import (
    MAX_FILE_BYTES,
    remove_conversation_uploads,
    save_uploads,
    validated_attachments,
    validated_file,
)

# Starlette 默认单个 part 只收 1MB，不抬高的话超过 1MB 的附件会在
# uploads.py 那 12MB 的检查之前就先失败，报错还很难看懂。
MultiPartParser.max_part_size = MAX_FILE_BYTES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chatnest")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

store.initialize_store()

# 一次只跟一个回复——不是技术限制，是防自己手滑双击发送把额度花两份。
chat_lock = asyncio.Lock()
summary_lock = asyncio.Lock()

TRACE_CONTENT_CHARS = 20_000


def require_auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")


AUTHED = [Depends(require_auth)]


class AuthBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ChatBody(BaseModel):
    message: str = Field(default="", max_length=20_000)
    conversation_id: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=256)
    edit_message_id: int | None = Field(default=None, ge=1)
    retry_message_id: int | None = Field(default=None, ge=1)
    model: str = Field(default="", max_length=200)
    effort: str = Field(default="medium", max_length=16)
    extended: bool = True
    attachments: list[str] = Field(default_factory=list, max_length=10)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class StarBody(BaseModel):
    starred: bool


class MemoryBody(BaseModel):
    content: str = Field(default="", max_length=4000)


class ProfileBody(BaseModel):
    fullName: str = Field(default="", max_length=200)
    nickname: str = Field(default="", max_length=200)
    savedMemories: list[Any] = Field(default_factory=list, max_length=200)
    preferences: Any = None
    updatedAt: int | None = None


class SummaryBody(BaseModel):
    content: str | None = Field(default=None, max_length=20_000)
    enabled: bool | None = None


class ThinkingSummaryBody(BaseModel):
    thinking: str = Field(default="", max_length=50_000)


class ToolCaptionBody(BaseModel):
    tool_name: str = Field(default="", max_length=200)
    tool_input: Any = None
    tool_output: str = Field(default="", max_length=20_000)


class DiaryBody(BaseModel):
    date: str = Field(min_length=10, max_length=10)
    text: str = Field(default="", max_length=20_000)


class ProbeBody(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    token: str = Field(default="", max_length=2000)
    id: str = Field(default="", max_length=40)


class CssBody(BaseModel):
    css: str = Field(default="", max_length=settings.MAX_CSS_CHARS)


def sse(event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


def trace_content(value: Any) -> str:
    text = str(value or "")
    if len(text) <= TRACE_CONTENT_CHARS:
        return text
    return text[:TRACE_CONTENT_CHARS] + "\n\n[output truncated]"


# ---------- 登录 ----------

@app.post("/api/auth")
async def login(body: AuthBody) -> dict:
    token = auth.issue_token(body.password)
    if token is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"token": token}


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


# ---------- 自定义 CSS ----------

@app.get("/api/custom.css")
async def custom_css(request: Request) -> Response:
    """前端 head 里那个 link 指到这里。

    不鉴权：`<link>` 标签带不了 Authorization 头，而且 CSS 里没有秘密。
    带 ETag 让浏览器复用缓存，改了内容 ETag 就变，所以热更新照样即时。
    """
    css = settings.custom_css()
    etag = '"%s"' % hashlib.sha256(css.encode("utf-8")).hexdigest()[:32]
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        css,
        media_type="text/css; charset=utf-8",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


# ---------- 控制台 ----------

@app.get("/dashboard")
async def dashboard() -> FileResponse:
    """控制台页面。页面本身不鉴权，里面每个接口都鉴权。"""
    return FileResponse(
        config.ROOT / "dashboard.html",
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/admin/settings", dependencies=AUTHED)
async def admin_settings() -> dict:
    return {"settings": settings.public_view(), "tools": registry.status()}


@app.put("/api/admin/settings", dependencies=AUTHED)
async def admin_settings_save(body: dict) -> dict:
    settings.update(body)
    # 配置一变，工具清单可能整个不一样了，重拉一次；拉不动也别让保存失败。
    try:
        await registry.refresh()
    except Exception:
        logger.exception("保存后重拉 MCP 工具失败")
    return {"settings": settings.public_view(), "tools": registry.status()}


@app.post("/api/admin/reload", dependencies=AUTHED)
async def admin_reload() -> dict:
    """热更新：重读配置文件并重拉工具，不重启进程。"""
    settings.reload()
    css_meta.known_classes(refresh=True)
    try:
        await registry.refresh()
    except Exception:
        logger.exception("热更新时重拉 MCP 工具失败")
    return {"settings": settings.public_view(), "tools": registry.status()}


@app.post("/api/admin/mcp/refresh", dependencies=AUTHED)
async def admin_mcp_refresh() -> dict:
    return await registry.refresh()


@app.post("/api/admin/mcp/probe", dependencies=AUTHED)
async def admin_mcp_probe(body: ProbeBody) -> dict:
    """测试单个地址。token 留空时沿用已存的那个，省得你重新贴一遍。"""
    from server.mcp_client import probe

    token = body.token
    if not token and body.id:
        for item in settings.mcp_servers():
            if item["id"] == body.id:
                token = item.get("token", "")
                break
    return await probe(body.url, token)


@app.post("/api/admin/css/checkup", dependencies=AUTHED)
async def admin_css_checkup(body: CssBody) -> dict:
    """类名体检：每个类名在真页面里存不存在。"""
    return css_meta.checkup(body.css)


@app.get("/api/admin/css/reference", dependencies=AUTHED)
async def admin_css_reference() -> dict:
    return css_meta.reference()


@app.get("/api/admin/css/snippets", dependencies=AUTHED)
async def admin_css_snippets() -> dict:
    return {"snippets": css_meta.snippets()}


# ---------- 模型 ----------

@app.get("/api/models")
async def models() -> dict:
    return {"models": settings.models()}


@app.post("/api/warmup", dependencies=AUTHED)
async def warmup() -> dict:
    # 中转 API 没有进程可预热，直接 ok。
    return {"ok": True}


@app.get("/api/splash")
async def splash() -> dict:
    """空会话上方那句招呼。按时段挑，不过模型——这句不值得花额度。"""
    import secrets

    hour = clock.now_local().hour
    if 5 <= hour < 11:
        pool = ["早。", "醒了？", "今天打算干点什么。"]
    elif 11 <= hour < 18:
        pool = ["在吗。", "下午好。", "进展如何。"]
    elif 18 <= hour < 23:
        pool = ["吃了吗。", "收工了？", "晚上好。"]
    else:
        pool = ["还没睡。", "夜深了。", "睁着眼呢。"]
    return {"line": secrets.choice(pool)}


# ---------- 聊天 ----------

def _system_prompt() -> str:
    base = config.system_prompt()
    context = profile_store.build_profile_context()
    return "\n\n".join(part for part in (base, context) if part)


@app.post("/api/chat", dependencies=AUTHED)
async def chat(body: ChatBody) -> StreamingResponse:
    is_branch = body.edit_message_id is not None or body.retry_message_id is not None
    if body.edit_message_id is not None and body.retry_message_id is not None:
        raise HTTPException(status_code=400, detail="一次只能执行一种分支操作")
    if not is_branch and not body.message.strip() and not body.attachments:
        raise HTTPException(status_code=400, detail="消息或附件不能为空")
    requested_conv_id = body.conversation_id or body.session_id
    if is_branch and not requested_conv_id:
        raise HTTPException(status_code=400, detail="分支操作缺少会话标识")
    if body.attachments and not requested_conv_id:
        raise HTTPException(status_code=400, detail="附件缺少会话标识")
    attachment_items = (
        validated_attachments(requested_conv_id, body.attachments)
        if body.attachments and requested_conv_id
        else []
    )

    async def stream():
        if chat_lock.locked():
            yield sse("error", {"message": "上一条消息仍在回复"})
            return
        await chat_lock.acquire()
        branch_id = None
        committed = False
        response_text = ""
        response_thinking = ""
        response_traces: list[dict] = []
        try:
            if body.edit_message_id is not None:
                prepared = store.prepare_edit_turn(
                    requested_conv_id or "",
                    body.edit_message_id,
                    body.message.strip(),
                )
            elif body.retry_message_id is not None:
                prepared = store.prepare_retry_turn(
                    requested_conv_id or "",
                    body.retry_message_id,
                )
            else:
                conv_id, user_message_id = store.begin_turn(
                    body.message.strip(),
                    requested_conv_id,
                    attachment_items,
                )
                prepared = {
                    "conv_id": conv_id,
                    "user_message_id": user_message_id,
                    "branch_id": None,
                }
            conv_id = prepared["conv_id"]
            user_message_id = prepared["user_message_id"]
            branch_id = prepared.get("branch_id")

            yield sse(
                "conversation",
                {"conversation_id": conv_id, "user_message_id": user_message_id},
            )

            history = store.history_messages(
                conv_id, user_message_id, settings.history_turns()
            )
            note = clock.prompt_note(
                store.last_message_time(conv_id, user_message_id)
            )
            messages = llm.build_messages(history, _system_prompt(), note)
            model = body.model.strip() or settings.chat_model()

            chunks = llm.stream_chat(
                messages, model, body.effort, body.extended
            ).__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(chunks.__anext__(), timeout=15)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    # 长时间无输出时吐个注释行，防 nginx / 浏览器把连接当死连接断掉。
                    # 工具跑得久的时候全靠这个撑着。
                    yield ": heartbeat\n\n"
                    continue
                event = chunk["event"]
                if event == "delta":
                    response_text += chunk.get("text", "")
                    yield sse("delta", {"text": chunk["text"]})
                elif event == "thinking":
                    response_thinking += chunk.get("text", "")
                    yield sse("thinking", {"text": chunk["text"]})
                elif event == "tool_use":
                    # text_offset 让前端知道这张卡片该插在正文哪个位置
                    response_traces.append({
                        "type": "tool_use",
                        "id": chunk.get("id"),
                        "name": chunk.get("name"),
                        "input": chunk.get("input"),
                        "text_offset": len(response_text.rstrip()),
                    })
                    yield sse("tool_use", {
                        "id": chunk.get("id"),
                        "name": chunk.get("name"),
                        "input": chunk.get("input"),
                    })
                elif event == "tool_result":
                    content = trace_content(chunk.get("content"))
                    response_traces.append({
                        "type": "tool_result",
                        "tool_use_id": chunk.get("tool_use_id"),
                        "content": content,
                        "is_error": chunk.get("is_error", False),
                    })
                    yield sse("tool_result", {
                        "tool_use_id": chunk.get("tool_use_id"),
                        "content": content,
                        "is_error": chunk.get("is_error", False),
                    })
                elif event == "done":
                    assistant_message_id = store.complete_turn(
                        conv_id, response_text, response_thinking, response_traces
                    )
                    committed = True
                    yield sse(
                        "done",
                        {
                            "conversation_id": conv_id,
                            "assistant_message_id": assistant_message_id,
                        },
                    )
        except ConversationNotFound:
            if branch_id and not committed:
                store.restore_branch(branch_id)
            yield sse("error", {"message": "会话不存在或已被删除"})
        except ValueError as exc:
            if branch_id and not committed:
                store.restore_branch(branch_id)
            yield sse("error", {"message": str(exc) or "这条消息不能这样操作"})
        except llm.UpstreamError as exc:
            if branch_id and not committed:
                store.restore_branch(branch_id)
            logger.warning("upstream failed: %s", exc)
            yield sse("error", {"message": str(exc)})
        except Exception:
            if branch_id and not committed:
                store.restore_branch(branch_id)
            logger.exception("chat failed")
            yield sse("error", {"message": "后端出错了，看一下服务日志。"})
        finally:
            chat_lock.release()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/thinking-summary", dependencies=AUTHED)
async def thinking_summary(body: ThinkingSummaryBody) -> dict:
    # 不再调模型。前端拿到空摘要就不折叠，完整思考链直接显示。
    return {"summary": ""}


@app.post("/api/tool-caption", dependencies=AUTHED)
async def tool_caption(body: ToolCaptionBody) -> dict:
    # 不再调模型。前端拿到空 caption 就保留默认的工具名显示。
    return {"caption": ""}


# ---------- 会话 ----------

@app.get("/api/sessions", dependencies=AUTHED)
async def sessions() -> dict:
    return {"sessions": store.conversation_list()}


@app.get("/api/sessions/{session_id}/messages", dependencies=AUTHED)
async def messages(
    session_id: str,
    before_id: int | None = Query(default=None, ge=1),
    after_id: int | None = Query(default=None, ge=1),
    around_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict:
    try:
        return store.conversation_messages(
            session_id, before_id, after_id, around_id, limit
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


@app.patch("/api/sessions/{session_id}/title", dependencies=AUTHED)
async def rename(session_id: str, body: RenameBody) -> dict:
    try:
        store.rename_conversation(session_id, body.title)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {"renamed": True}


@app.patch("/api/sessions/{session_id}/star", dependencies=AUTHED)
async def star(session_id: str, body: StarBody) -> dict:
    try:
        store.star_conversation(session_id, body.starred)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {"starred": body.starred}


@app.delete("/api/sessions/{session_id}", dependencies=AUTHED)
async def delete_session(session_id: str) -> dict:
    try:
        store.delete_conversation(session_id)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    remove_conversation_uploads(session_id)
    return {"deleted": True}


@app.get("/api/search", dependencies=AUTHED)
async def search(
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return {"results": store.search_messages(q, limit)}


# ---------- 上传 ----------

@app.post("/api/upload", dependencies=AUTHED)
async def upload(
    files: list[UploadFile] = File(...),
    conversation_id: str | None = Form(default=None),
) -> dict:
    try:
        conv_id = store.ensure_conversation(conversation_id)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    attachments = await save_uploads(conv_id, files)
    return {"conversation_id": conv_id, "attachments": attachments}


@app.get("/api/uploads/{conversation_id}/{filename}", dependencies=AUTHED)
async def uploaded_file(conversation_id: str, filename: str) -> FileResponse:
    return FileResponse(validated_file(conversation_id, filename))


# ---------- profile / 记忆 ----------

@app.get("/api/profile", dependencies=AUTHED)
async def get_profile() -> dict:
    return {"profile": profile_store.read_profile()}


@app.put("/api/profile", dependencies=AUTHED)
async def put_profile(body: ProfileBody) -> dict:
    saved = profile_store.write_profile(body.model_dump())
    return {"saved": True, "profile": saved}


@app.post("/api/profile/memory", dependencies=AUTHED)
async def post_memory(body: MemoryBody) -> dict:
    memory, reason, detail = profile_store.add_saved_memory(body.content)
    if memory is None:
        return {"saved": False, "reason": reason, "detail": detail}
    return {"saved": True, "memory": memory}


@app.get("/api/memory-summary", dependencies=AUTHED)
async def get_memory_summary() -> dict:
    return profile_store.read_summary()


@app.put("/api/memory-summary", dependencies=AUTHED)
async def put_memory_summary(body: SummaryBody) -> dict:
    return profile_store.write_summary(content=body.content, enabled=body.enabled)


@app.delete("/api/memory-summary", dependencies=AUTHED)
async def delete_memory_summary() -> dict:
    return profile_store.write_summary(content="", running=False)


async def _run_summary_generation() -> None:
    """后台生成长期印象。前端靠 running 字段轮询。

    finally 里一定要把 running 抹掉，不然炸一次就永远卡在「生成中」。
    """
    try:
        conversations = store.conversation_list()[:5]
        blocks = []
        for item in conversations:
            history = store.history_messages(item["conv_id"], None, 40)
            lines = [
                f"{'用户' if m['role'] == 'user' else '助手'}：{m['text']}"
                for m in history
                if (m.get("text") or "").strip()
            ]
            if lines:
                blocks.append(f"【{item['title']}】\n" + "\n".join(lines))
        summary = await llm.generate_memory_summary("\n\n".join(blocks))
        if summary:
            profile_store.write_summary(content=summary)
    except Exception:
        logger.exception("memory summary generation failed")
    finally:
        profile_store.write_summary(running=False)


@app.post("/api/memory-summary/generate", dependencies=AUTHED)
async def generate_memory_summary() -> dict:
    if summary_lock.locked():
        return {**profile_store.read_summary(), "running": True}

    async def guarded() -> None:
        async with summary_lock:
            await _run_summary_generation()

    profile_store.write_summary(running=True)
    asyncio.create_task(guarded())
    return {**profile_store.read_summary(), "running": True}


# ---------- 日记 / 日历 ----------

@app.get("/api/diary", dependencies=AUTHED)
async def get_diary(q: str = Query(default="", max_length=200)) -> dict:
    return {"entries": profile_store.read_diary(q)}


@app.put("/api/diary", dependencies=AUTHED)
async def put_diary(body: DiaryBody) -> dict:
    try:
        entries = profile_store.write_diary_entry(body.date, body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"saved": True, "entries": entries}


@app.get("/api/calendar", dependencies=AUTHED)
async def get_calendar(
    year: int | None = Query(default=None, ge=1970, le=2200),
) -> dict:
    # 按本地时区取当前年份。用 UTC 的话跨年那几个小时会翻错一年。
    target = year or clock.now_local().year
    return profile_store.calendar_year(target)


@app.get("/api/calendar/{date}", dependencies=AUTHED)
async def get_calendar_day(date: str) -> dict:
    try:
        return profile_store.read_calendar_day(date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/calendar/{date}", dependencies=AUTHED)
async def put_calendar_day(date: str, body: dict) -> dict:
    try:
        return profile_store.write_calendar_day(date, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- 头像 ----------

@app.get("/api/avatars", dependencies=AUTHED)
async def get_avatars() -> dict:
    return profile_store.read_avatars()


@app.put("/api/avatars", dependencies=AUTHED)
async def put_avatars(body: dict) -> dict:
    return profile_store.write_avatars(body)
