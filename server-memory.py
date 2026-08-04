# -*- coding: utf-8 -*-
"""Saved memories 的近似去重：挡住换个说法的同一条记忆。

只比「一模一样」是不够的——「我住新加坡」和「我在新加坡住」会各存一条，
攒上几个月，上下文里就全是近义句在占位置。

前端 index.html 里有一份等价的 JS 实现（isNearDuplicate），两边规则必须一致：
后端负责拦 POST /api/profile/memory（模型自己写记忆的入口），
前端负责在 Saved memories 列表里把重复的那条标出来给人看。

无第三方依赖，Python 3.11+。用法：

    from server_memory import find_duplicate, MemoryRejected

    clash = find_duplicate(new_content, profile["savedMemories"])
    if clash is not None:
        raise MemoryRejected("duplicate", clash["content"])

环境变量（默认偏保守，调低会误杀新记忆）：
    MEMORY_DUP_RATIO       编辑距离门槛，默认 0.82
    MEMORY_DUP_JACCARD     字符集重合门槛，默认 0.80
    MEMORY_DUP_MIN_CHARS   短于这个长度只认全等，默认 4
"""

import os
import re
from difflib import SequenceMatcher
from typing import Any

_DUP_RATIO = float(os.environ.get("MEMORY_DUP_RATIO", "0.82"))
_DUP_JACCARD = float(os.environ.get("MEMORY_DUP_JACCARD", "0.80"))
_DUP_MIN_CHARS = int(os.environ.get("MEMORY_DUP_MIN_CHARS", "4"))
# 一长一短差太多就不是同一件事，是一条把另一条包在里面的长文
_DUP_MAX_LEN_RATIO = 1.8

_PUNCT_RE = re.compile(r"[\s，。、；：！？「」『』（）《》【】…—~,.;:!?\"'()\[\]{}<>/\\|`*_-]+")
# 否定词：有无之差会把意思整个翻过来，字面再像也不是同一条。
# 「喜欢香菜」和「不喜欢香菜」字符集重合 0.8，光看相似度必然误合。
_NEGATION_RE = re.compile(r"不|没|无|非|未|别|勿|拒绝|讨厌|\bnot\b|n't|\bnever\b|\bno\b")


class MemoryRejected(Exception):
    """写不进去，并且说得清为什么——让模型知道下一步是改写还是放弃。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _dup_key(value: str) -> str:
    """比对用的形态：去标点、去空白、字母转小写。"""
    return _PUNCT_RE.sub("", re.sub(r"\s+", " ", str(value or "")).strip()).lower()


def _char_jaccard(a: str, b: str) -> float:
    """字符集重合度。中文换语序（住新加坡 / 在新加坡住）编辑距离掉得厉害，这个还稳。"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _ratio_bar(length: int) -> float:
    """句子越短，一个字占的意思越重，门槛就要越高。

    「周一要交报告」和「周五要交报告」相似度 0.83——放在长句里这是同一条，
    放在 6 个字里这是两件事。
    """
    if length < 8:
        return 0.95
    if length < 20:
        return 0.88
    return _DUP_RATIO


def is_near_duplicate(a: str, b: str) -> bool:
    """全等 → 否定词守卫 → 包含 → 编辑距离 → 字符集重合，任一命中即算重复。"""
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


def find_duplicate(content: str, memories: list[dict[str, Any]]) -> dict[str, Any] | None:
    """返回撞上的那一条，让调用方能说清是跟哪条重了，而不是干巴巴一句『重复』。"""
    for item in memories:
        existing = item.get("content") or ""
        if existing and is_near_duplicate(content, existing):
            return item
    return None


if __name__ == "__main__":
    cases = [
        ("我住新加坡", "我在新加坡住", True),
        ("喜欢喝冰美式", "喜欢喝冰美式咖啡", True),
        ("他在新加坡读医学院", "在新加坡读医学院的是他", True),
        ("喜欢香菜", "不喜欢香菜", False),
        ("周一要交报告", "周五要交报告", False),
        ("他喜欢猫", "他讨厌猫", False),
    ]
    bad = 0
    for a, b, want in cases:
        got = is_near_duplicate(a, b)
        bad += got != want
        print(f"{'OK ' if got == want else 'FAIL'}  {'重复' if got else '不同'}  {a} ↔ {b}")
    raise SystemExit(bad)
