"""Saved memories 近似去重。规则与根目录 server-memory.py 一致。

前端 index.html 里的 isNearDuplicate 是同一套规则的 JS 版，两边必须一致：
后端拦 POST /api/profile/memory（模型自己写记忆的入口），
前端只在列表里把重复那条标红给人看，不删。
"""

import os
import re
from difflib import SequenceMatcher
from typing import Any

_DUP_RATIO = float(os.environ.get("MEMORY_DUP_RATIO", "0.82"))
_DUP_JACCARD = float(os.environ.get("MEMORY_DUP_JACCARD", "0.80"))
_DUP_MIN_CHARS = int(os.environ.get("MEMORY_DUP_MIN_CHARS", "4"))
_DUP_MAX_LEN_RATIO = 1.8

_PUNCT_RE = re.compile(
    r"[\s，。、；：！？「」『』（）《》【】…—~,.;:!?\"'()\[\]{}<>/\\|`*_-]+"
)
_NEGATION_RE = re.compile(
    r"不|没|无|非|未|别|勿|拒绝|讨厌|\bnot\b|n't|\bnever\b|\bno\b"
)


class MemoryRejected(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _dup_key(value: str) -> str:
    return _PUNCT_RE.sub("", re.sub(r"\s+", " ", str(value or "")).strip()).lower()


def _char_jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _ratio_bar(length: int) -> float:
    if length < 8:
        return 0.95
    if length < 20:
        return 0.88
    return _DUP_RATIO


def is_near_duplicate(a: str, b: str) -> bool:
    ka, kb = _dup_key(a), _dup_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if bool(_NEGATION_RE.search(ka)) != bool(_NEGATION_RE.search(kb)):
        return False
    short, long = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if len(short) < _DUP_MIN_CHARS:
        return False
    if short in long:
        return True
    if len(long) / len(short) > _DUP_MAX_LEN_RATIO:
        return False
    if SequenceMatcher(None, ka, kb).ratio() >= _ratio_bar(len(short)):
        return True
    return _char_jaccard(ka, kb) >= _DUP_JACCARD


def find_duplicate(
    content: str,
    memories: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for item in memories:
        existing = item.get("content") or ""
        if existing and is_near_duplicate(content, existing):
            return item
    return None
