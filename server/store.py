"""会话与消息的 SQLite 存储。

跟上游 full-stack/app/store.py 最大的区别：那边靠 Claude CLI 的 session
文件续接上下文，这边没有 session 概念——每次请求自己从库里拼历史发给中转 API。
所以少了 session_aliases / latest_session_id 那一整套，多了 messages 的
全文搜索。

编辑和重新生成沿用上游的做法：把被砍掉的尾巴存进 message_branches，
模型这一轮要是炸了就 restore_branch 放回去，不至于把用户消息弄丢。
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from server.config import DB_PATH


class ConversationNotFound(LookupError):
    pass


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """一次查询一个连接：进来开、出去提交并关掉。

    注意 sqlite3 的 `with connection` 只负责提交/回滚，**不关连接**。
    直接 `with sqlite3.connect(...) as db` 会一路漏文件描述符，
    跑几千个请求之后就报 too many open files。
    """
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    # 这个 pragma 是 per-connection 的，每条连接都得重新开，
    # 不开的话 conversations 删掉后 messages 会变成孤儿行。
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_store() -> None:
    with _db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conv_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                starred INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id TEXT NOT NULL REFERENCES conversations(conv_id)
                    ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                text TEXT NOT NULL DEFAULT '',
                thinking TEXT NOT NULL DEFAULT '',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                traces_json TEXT NOT NULL DEFAULT '[]',
                edited INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_conv_id
                ON messages(conv_id, id);
            CREATE TABLE IF NOT EXISTS message_branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id TEXT NOT NULL REFERENCES conversations(conv_id)
                    ON DELETE CASCADE,
                base_message_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('edit', 'retry')),
                tail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS message_branches_conv_base
                ON message_branches(conv_id, base_message_id);
            """
        )


def _message_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key, target in (("attachments_json", "attachments"),
                        ("traces_json", "traces")):
        try:
            item[target] = json.loads(item.pop(key))
        except (json.JSONDecodeError, TypeError, KeyError):
            item[target] = []
    item["edited"] = bool(item.get("edited", 0))
    item["branch_count"] = int(item.get("branch_count", 0) or 0)
    return item


def _rows_to_messages(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [_message_from_row(row) for row in rows]


_MESSAGE_COLUMNS = """
    m.id, m.role, m.text, m.thinking, m.attachments_json,
    m.traces_json, m.edited, m.timestamp,
    (
        SELECT COUNT(*) FROM message_branches b
        WHERE b.conv_id = m.conv_id AND b.base_message_id = m.id
    ) AS branch_count
"""


def conversation_list() -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.execute(
            """
            SELECT conv_id, title, starred, created_at, updated_at
            FROM conversations
            ORDER BY starred DESC, updated_at DESC
            """
        ).fetchall()
    return [
        {
            "conv_id": row["conv_id"],
            "session_id": row["conv_id"],
            "title": row["title"],
            "starred": bool(row["starred"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_modified": row["updated_at"],
        }
        for row in rows
    ]


def resolve_conversation(identifier: str | None) -> str | None:
    if not identifier:
        return None
    with _db() as db:
        row = db.execute(
            "SELECT conv_id FROM conversations WHERE conv_id = ?",
            (identifier,),
        ).fetchone()
    return row["conv_id"] if row else None


def ensure_conversation(
    conversation_id: str | None = None,
    title: str = "新对话",
) -> str:
    conv_id = resolve_conversation(conversation_id)
    if conversation_id and not conv_id:
        raise ConversationNotFound("conversation not found")
    if conv_id:
        return conv_id
    conv_id = str(uuid.uuid4())
    now = _now()
    with _db() as db:
        db.execute(
            """
            INSERT INTO conversations
                (conv_id, title, starred, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (conv_id, title[:120] or "新对话", now, now),
        )
    return conv_id


def begin_turn(
    message: str,
    conversation_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[str, int]:
    """落一条 user 消息，返回 (conv_id, user_message_id)。"""
    conv_id = resolve_conversation(conversation_id)
    if conversation_id and not conv_id:
        raise ConversationNotFound("conversation not found")
    now = _now()
    with _db() as db:
        if not conv_id:
            conv_id = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO conversations
                    (conv_id, title, starred, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (conv_id, message.strip()[:120] or "新对话", now, now),
            )
        title_row = db.execute(
            "SELECT title FROM conversations WHERE conv_id = ?",
            (conv_id,),
        ).fetchone()
        if title_row and title_row["title"] == "新对话":
            title = message.strip()[:120]
            if not title and attachments:
                title = attachments[0].get("name", "附件")
            db.execute(
                "UPDATE conversations SET title = ? WHERE conv_id = ?",
                (title or "新对话", conv_id),
            )
        cursor = db.execute(
            """
            INSERT INTO messages(
                conv_id, role, text, thinking, attachments_json, timestamp
            )
            VALUES (?, 'user', ?, '', ?, ?)
            """,
            (conv_id, message, json.dumps(attachments or [], ensure_ascii=False), now),
        )
        user_message_id = int(cursor.lastrowid)
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE conv_id = ?",
            (now, conv_id),
        )
    return conv_id, user_message_id


def complete_turn(
    conv_id: str,
    text: str,
    thinking: str = "",
    traces: list | None = None,
) -> int:
    now = _now()
    with _db() as db:
        if not db.execute(
            "SELECT 1 FROM conversations WHERE conv_id = ?",
            (conv_id,),
        ).fetchone():
            raise ConversationNotFound("conversation not found")
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE conv_id = ?",
            (now, conv_id),
        )
        cursor = db.execute(
            """
            INSERT INTO messages(
                conv_id, role, text, thinking, attachments_json,
                traces_json, timestamp
            )
            VALUES (?, 'assistant', ?, ?, '[]', ?, ?)
            """,
            (conv_id, text, thinking,
             json.dumps(traces or [], ensure_ascii=False), now),
        )
        return int(cursor.lastrowid)


def history_messages(
    conv_id: str,
    through_id: int | None = None,
    turns: int = 30,
) -> list[dict[str, Any]]:
    """给模型的上下文：按 id 升序，最多末尾 turns 条。"""
    with _db() as db:
        params: list[Any] = [conv_id]
        clause = ""
        if through_id is not None:
            clause = "AND id <= ?"
            params.append(through_id)
        params.append(max(1, turns))
        rows = db.execute(
            f"""
            SELECT id, role, text, attachments_json
            FROM messages
            WHERE conv_id = ? {clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    items = []
    for row in reversed(rows):
        try:
            attachments = json.loads(row["attachments_json"])
        except (json.JSONDecodeError, TypeError):
            attachments = []
        items.append(
            {
                "id": row["id"],
                "role": row["role"],
                "text": row["text"],
                "attachments": attachments,
            }
        )
    return items


def last_message_time(conv_id: str, before_id: int | None = None) -> str | None:
    """上一条消息的时间戳，给时钟算「多久没说话」。"""
    with _db() as db:
        params: list[Any] = [conv_id]
        clause = ""
        if before_id is not None:
            clause = "AND id < ?"
            params.append(before_id)
        row = db.execute(
            f"""
            SELECT timestamp FROM messages
            WHERE conv_id = ? {clause}
            ORDER BY id DESC LIMIT 1
            """,
            params,
        ).fetchone()
    return row["timestamp"] if row else None


def _tail_snapshot(rows: list[sqlite3.Row]) -> str:
    return json.dumps(_rows_to_messages(rows), ensure_ascii=False)


def _snapshot_tail(
    db: sqlite3.Connection,
    conv_id: str,
    from_id: int,
    kind: str,
) -> int | None:
    rows = db.execute(
        f"""
        SELECT {_MESSAGE_COLUMNS}
        FROM messages m
        WHERE m.conv_id = ? AND m.id >= ?
        ORDER BY m.id
        """,
        (conv_id, from_id),
    ).fetchall()
    if not rows:
        return None
    cursor = db.execute(
        """
        INSERT INTO message_branches(
            conv_id, base_message_id, kind, tail_json, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (conv_id, from_id, kind, _tail_snapshot(rows), _now()),
    )
    return int(cursor.lastrowid)


def prepare_edit_turn(
    conversation_id: str,
    message_id: int,
    content: str,
) -> dict[str, Any]:
    resolved = resolve_conversation(conversation_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    content = content.strip()
    if not content:
        raise ValueError("消息不能为空")
    now = _now()
    with _db() as db:
        row = db.execute(
            "SELECT id, role, attachments_json FROM messages WHERE conv_id = ? AND id = ?",
            (resolved, message_id),
        ).fetchone()
        if not row:
            raise ConversationNotFound("message not found")
        if row["role"] != "user":
            raise ValueError("只有自己发的消息可以编辑")
        branch_id = _snapshot_tail(db, resolved, message_id, "edit")
        db.execute(
            "DELETE FROM messages WHERE conv_id = ? AND id > ?",
            (resolved, message_id),
        )
        db.execute(
            "UPDATE messages SET text = ?, edited = 1 WHERE conv_id = ? AND id = ?",
            (content, resolved, message_id),
        )
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE conv_id = ?",
            (now, resolved),
        )
        try:
            attachments = json.loads(row["attachments_json"])
        except (json.JSONDecodeError, TypeError):
            attachments = []
    return {
        "conv_id": resolved,
        "user_message_id": message_id,
        "message": content,
        "attachments": attachments,
        "branch_id": branch_id,
    }


def prepare_retry_turn(
    conversation_id: str,
    assistant_message_id: int,
) -> dict[str, Any]:
    resolved = resolve_conversation(conversation_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    now = _now()
    with _db() as db:
        row = db.execute(
            "SELECT id, role FROM messages WHERE conv_id = ? AND id = ?",
            (resolved, assistant_message_id),
        ).fetchone()
        if not row:
            raise ConversationNotFound("message not found")
        if row["role"] != "assistant":
            raise ValueError("只有回复可以重新生成")
        user_row = db.execute(
            """
            SELECT id, text, attachments_json
            FROM messages
            WHERE conv_id = ? AND id < ? AND role = 'user'
            ORDER BY id DESC LIMIT 1
            """,
            (resolved, assistant_message_id),
        ).fetchone()
        if not user_row:
            raise ConversationNotFound("source user message not found")
        branch_id = _snapshot_tail(db, resolved, assistant_message_id, "retry")
        db.execute(
            "DELETE FROM messages WHERE conv_id = ? AND id >= ?",
            (resolved, assistant_message_id),
        )
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE conv_id = ?",
            (now, resolved),
        )
        try:
            attachments = json.loads(user_row["attachments_json"])
        except (json.JSONDecodeError, TypeError):
            attachments = []
    return {
        "conv_id": resolved,
        "user_message_id": int(user_row["id"]),
        "message": user_row["text"],
        "attachments": attachments,
        "branch_id": branch_id,
    }


def restore_branch(branch_id: int | None) -> None:
    """模型这一轮失败时把砍掉的尾巴放回去。"""
    if not branch_id:
        return
    with _db() as db:
        branch = db.execute(
            "SELECT conv_id, tail_json FROM message_branches WHERE id = ?",
            (branch_id,),
        ).fetchone()
        if not branch:
            return
        try:
            tail = json.loads(branch["tail_json"])
        except (json.JSONDecodeError, TypeError):
            return
        if not tail:
            return
        ids = [int(item["id"]) for item in tail if item.get("id")]
        if not ids:
            return
        now = _now()
        db.execute(
            "DELETE FROM messages WHERE conv_id = ? AND id >= ?",
            (branch["conv_id"], min(ids)),
        )
        for item in tail:
            db.execute(
                """
                INSERT OR REPLACE INTO messages(
                    id, conv_id, role, text, thinking,
                    attachments_json, traces_json, edited, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(item["id"]),
                    branch["conv_id"],
                    item.get("role"),
                    item.get("text", ""),
                    item.get("thinking", ""),
                    json.dumps(item.get("attachments", []), ensure_ascii=False),
                    json.dumps(item.get("traces", []), ensure_ascii=False),
                    int(bool(item.get("edited", False))),
                    item.get("timestamp") or now,
                ),
            )
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE conv_id = ?",
            (now, branch["conv_id"]),
        )
        db.execute("DELETE FROM message_branches WHERE id = ?", (branch_id,))


def conversation_messages(
    conv_id: str,
    before_id: int | None = None,
    after_id: int | None = None,
    around_id: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """分页读消息。

    前端三种用法：进会话拿最后一窗（都不传）、上滑加载更早（before_id）、
    搜索跳转到某条（around_id，要求两侧都能继续翻）。
    """
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    page_size = max(1, min(limit or 40, 200))
    with _db() as db:
        def fetch(clause: str, params: list[Any], desc: bool, size: int):
            order = "DESC" if desc else "ASC"
            return db.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages m
                WHERE m.conv_id = ? {clause}
                ORDER BY m.id {order}
                LIMIT ?
                """,
                [resolved, *params, size + 1],
            ).fetchall()

        if around_id is not None:
            half = max(1, page_size // 2)
            older = fetch("AND m.id <= ?", [around_id], True, half)
            has_more = len(older) > half
            older = list(reversed(older[:half]))
            newer = fetch("AND m.id > ?", [around_id], False, half)
            has_newer = len(newer) > half
            newer = newer[:half]
            rows = older + newer
        elif after_id is not None:
            rows = fetch("AND m.id > ?", [after_id], False, page_size)
            has_newer = len(rows) > page_size
            rows = rows[:page_size]
            has_more = bool(
                db.execute(
                    "SELECT 1 FROM messages WHERE conv_id = ? AND id < ? LIMIT 1",
                    (resolved, rows[0]["id"] if rows else after_id),
                ).fetchone()
            )
        else:
            clause = "AND m.id < ?" if before_id is not None else ""
            params = [before_id] if before_id is not None else []
            rows = fetch(clause, params, True, page_size)
            has_more = len(rows) > page_size
            rows = list(reversed(rows[:page_size]))
            has_newer = before_id is not None
    messages = _rows_to_messages(rows)
    return {
        "messages": messages,
        "has_more": has_more,
        "next_before_id": messages[0]["id"] if has_more and messages else None,
        "has_newer": has_newer,
        "next_after_id": messages[-1]["id"] if has_newer and messages else None,
    }


def _like_pattern(value: str) -> str:
    """把用户输入转成 LIKE 的字面量模式。

    不转义的话搜一个 `%` 会命中全库，搜 `a_b` 会连 `axb` 一起捞出来。
    反斜杠要先转，否则会把后面刚加的转义符再转一遍。
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def search_messages(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """全库搜聊天记录。LIKE 够用——个人库量级不值得上 FTS5。"""
    query = query.strip()
    if not query:
        return []
    with _db() as db:
        rows = db.execute(
            r"""
            SELECT m.id AS message_id, m.conv_id, m.role, m.text, m.timestamp,
                   c.title AS conv_title, c.starred
            FROM messages m
            JOIN conversations c ON c.conv_id = m.conv_id
            WHERE m.text LIKE ? ESCAPE '\'
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (_like_pattern(query), max(1, min(limit, 200))),
        ).fetchall()
    results = []
    for row in rows:
        text = row["text"] or ""
        index = text.lower().find(query.lower())
        start = max(0, index - 30) if index >= 0 else 0
        snippet = text[start:start + 140].replace("\n", " ")
        if start > 0:
            snippet = "…" + snippet
        results.append(
            {
                "conv_id": row["conv_id"],
                "session_id": row["conv_id"],
                "conv_title": row["conv_title"],
                "message_id": row["message_id"],
                "role": row["role"],
                "snippet": snippet,
                "time_text": (row["timestamp"] or "")[:10],
                "timestamp": row["timestamp"],
                "starred": bool(row["starred"]),
            }
        )
    return results


def rename_conversation(conv_id: str, title: str) -> None:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    with _db() as db:
        db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE conv_id = ?",
            (title.strip()[:120], _now(), resolved),
        )


def star_conversation(conv_id: str, starred: bool) -> None:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    with _db() as db:
        db.execute(
            "UPDATE conversations SET starred = ? WHERE conv_id = ?",
            (int(starred), resolved),
        )


def delete_conversation(conv_id: str) -> None:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    with _db() as db:
        db.execute("DELETE FROM conversations WHERE conv_id = ?", (resolved,))
