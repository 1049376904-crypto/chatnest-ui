"""从 index.html 里抽出真存在的类名，给控制台做类名体检。

为什么需要这个：从别处拄来的 CSS 类名往往对不上（比如
`.message-sent` 是别的项目的），而浏览器对此一声不响——规则写了但永远
不命中。写完保存刷新发现没反应，还得猜是哪环错了。

提取方式是扫描三种写法：
1. HTML 里的 `class="a b c"`
2. JS 里的 `className='a b'`（这份前端大量类名是动态创建的）
3. CSS 选择器里的 `.a`

第三种会抄到一些并不存在于 DOM 的类（比如上游写了样式但后来删了元素），
但宁可多收不可少收——少收会把对的类名报成红的，那比不体检更坏。
"""

import re
from pathlib import Path
from typing import Any

from server import settings

# 前端文件位置。默认猜几个常见路径，也可用环境变量 FRONTEND_HTML 指定。
_CANDIDATES = (
    "/var/www/chatnest-ui/index.html",
    "/var/www/html/index.html",
)

_CLASS_ATTR = re.compile(r"""class\s*=\s*["']([^"']+)["']""")
_CLASS_NAME = re.compile(r"""className\s*=\s*["']([^"']+)["']""")
_CLASS_LIST = re.compile(r"""classList\.(?:add|toggle|remove)\(([^)]*)\)""")
_QUOTED = re.compile(r"""["']([A-Za-z0-9_-]+)["']""")
_CSS_CLASS = re.compile(r"\.(-?[A-Za-z_][A-Za-z0-9_-]*)")

_cache: dict[str, Any] | None = None


def _frontend_path() -> Path | None:
    import os

    configured = (os.environ.get("FRONTEND_HTML") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    for candidate in _CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def known_classes(refresh: bool = False) -> dict[str, Any]:
    """返回 {"classes": set, "path": str|None}。结果缓存，文件不小。"""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    path = _frontend_path()
    classes: set[str] = set()
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for pattern in (_CLASS_ATTR, _CLASS_NAME):
            for match in pattern.finditer(text):
                for name in match.group(1).split():
                    # 模版字符串里的 ${...} 抽不出确定类名，跳过
                    if "$" in name or "{" in name:
                        continue
                    classes.add(name)
        for match in _CLASS_LIST.finditer(text):
            for name in _QUOTED.findall(match.group(1)):
                classes.add(name)
        for match in _CSS_CLASS.finditer(text):
            classes.add(match.group(1))
    _cache = {"classes": classes, "path": str(path) if path else None}
    return _cache


# 常见的“别的项目类名 → 这边的类名”对应。
# 都是从实际踩过的坑里收的，不是猜的。
_SUGGEST = {
    "message-sent": ".bubble",
    "message-received": ".ai-bubble",
    "msg-row": ".msg-user / .msg-claude",
    "wechat-bubble": ".msg-layer（里面是 .ai-bubble）",
    "message-user": ".msg-user",
    "message-assistant": ".msg-claude",
    "chat-bubble": ".bubble / .ai-bubble",
    "user-bubble": ".bubble",
    "ai-message": ".msg-claude",
    "input-box": ".composer-box",
    "chat-input": "#input",
    "send-button": "#send",
    "avatar": ".msg-avatar",
    "timestamp": "用 .msg-user::after 配 attr(data-timestamp)",
}

# 写 CSS 时最用得上的那些，带一句说明。控制台拿这个当参考表。
KEY_CLASSES = [
    (".msg-user", "我发的那一行（带 data-timestamp）"),
    (".msg-claude", "模型回复那一行（带 data-timestamp）"),
    (".bubble", "我方气泡"),
    (".ai-bubble", "模型气泡"),
    (".msg-layer", "连发的第二条起，内层还是 .ai-bubble"),
    (".msg-avatar", "头像"),
    (".msg-body", "气泡列（头像旁边那一列）"),
    (".md", "气泡里的正文"),
    (".thinking-summary", "思考链那一行摘要"),
    (".tool-row", "工具卡片"),
    (".composer-box", "输入条容器"),
    ("#input", "输入框本体"),
    ("#send", "发送按钮"),
    (".composer-icon", "输入条上的小图标"),
    (".model-capsule", "模型选择胶囊"),
]


def _selectors(css: str) -> list[str]:
    """粗粗拆出选择器。不写完整 CSS parser，够用就行。"""
    stripped = re.sub(r"/\*[\s\S]*?\*/", " ", css)
    # 去掉 @media / @keyframes 的头，但保留里面的规则体
    out = []
    depth = 0
    buffer = ""
    for ch in stripped:
        if ch == "{":
            depth += 1
            if depth == 1 or buffer.strip():
                head = buffer.strip()
                if head and not head.startswith("@"):
                    out.append(head)
            buffer = ""
        elif ch == "}":
            depth = max(0, depth - 1)
            buffer = ""
        else:
            buffer += ch
    return out


def checkup(css: str) -> dict[str, Any]:
    """类名体检：每个类名在真页面里存不存在。

    还附一个花括号配对检查——括号不对 CSS 不报错，只默默丢掉一整段，
    那是“明明写了却没生效”里最难查的一种。
    """
    meta = known_classes()
    known: set[str] = meta["classes"]

    seen: dict[str, dict[str, Any]] = {}
    for selector in _selectors(css):
        for name in _CSS_CLASS.findall(selector):
            if name in seen:
                continue
            hit = name in known
            entry: dict[str, Any] = {"name": name, "known": hit}
            if not hit and name in _SUGGEST:
                entry["suggest"] = _SUGGEST[name]
            seen[name] = entry

    classes = sorted(seen.values(), key=lambda item: (item["known"], item["name"]))
    unknown = [item for item in classes if not item["known"]]

    # 花括号配对
    depth = 0
    brace_error = ""
    plain = re.sub(r"/\*[\s\S]*?\*/", " ", css)
    for index, ch in enumerate(plain):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                brace_error = f"多了一个右花括号（第 {index + 1} 个字符附近）"
                break
    if not brace_error and depth > 0:
        brace_error = f"有 {depth} 个左花括号没关上"

    return {
        "source": meta["path"],
        "source_class_count": len(known),
        "classes": classes,
        "unknown_count": len(unknown),
        "brace_error": brace_error,
        "length": len(css),
    }


def reference() -> dict[str, Any]:
    """给控制台的参考表：关键类名 + 可用变量。"""
    meta = known_classes()
    known: set[str] = meta["classes"]
    items = []
    for selector, note in KEY_CLASSES:
        name = selector.lstrip(".#")
        exists = selector.startswith("#") or name in known
        items.append({"selector": selector, "note": note, "exists": exists})
    return {"source": meta["path"], "key_classes": items}


def snippets() -> list[dict[str, str]]:
    """片段库。类名都是核对过的真类名，直接能用。"""
    return [
        {
            "id": "bubble-shape",
            "name": "气泡形状",
            "note": "圆角、尾部小角、包裹文字不占满行",
            "css": """/* 气泡形状 */
.bubble, .ai-bubble {
  width: fit-content !important;
  max-width: 80% !important;
  padding: 8px 14px !important;
  line-height: 1.45 !important;
}
/* 我方：右下收一个小角 */
.bubble { border-radius: 20px 20px 5px 20px !important; }
/* 模型：左下收一个小角，连发的后几条一起管 */
.ai-bubble, .msg-layer .ai-bubble {
  border-radius: 20px 20px 20px 5px !important;
}""",
        },
        {
            "id": "glass",
            "name": "毛玻璃气泡",
            "note": "背后有壁纸或渐变才看得出模糊，纯色底效果很平",
            "css": """/* 毛玻璃气泡 */
.bubble, .ai-bubble {
  background: linear-gradient(135deg,
              rgba(255,255,255,.40),
              rgba(255,255,255,.10)) !important;
  backdrop-filter: blur(10px) !important;
  -webkit-backdrop-filter: blur(10px) !important;
  border: 1px solid rgba(255,255,255,.40) !important;
  border-top-color: rgba(255,255,255,.60) !important;
  border-left-color: rgba(255,255,255,.60) !important;
  box-shadow: 0 8px 32px rgba(31,38,135,.10) !important;
  /* 用变量而不写死颜色，暗色模式下才不会黑底黑字 */
  color: var(--text-primary) !important;
}""",
        },
        {
            "id": "timestamp",
            "name": "消息时间",
            "note": "靠 data-timestamp 属性显示；开关里能改成短格式",
            "css": """/* 消息时间。开关里开了“短时间”的话这里显示的就是 01:55 这种 */
.msg-user::after, .msg-claude::after {
  content: attr(data-timestamp);
  display: block;
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-faint);
  /* 这一行别删：不加会挡住消息长按菜单 */
  pointer-events: none;
}
.msg-user::after { text-align: right; }""",
        },
        {
            "id": "typography",
            "name": "字号与行距",
            "note": "改变量比覆盖具体规则安全",
            "css": """/* 字号、行高、消息间距 */
:root {
  --text-base: 16px;
  --text-read: 16px;
  --leading: 1.65;
  --msg-gap: 24px;
}""",
        },
        {
            "id": "colors",
            "name": "配色",
            "note": "强调色、气泡底色、页面底色",
            "css": """/* 配色。改这几个变量比直接改规则安全，上游更新也不容易冲掘 */
:root {
  --accent: #DA7756;        /* 按钮、选中态 */
  --bubble-user: #EEEEEC;   /* 我发的气泡底色 */
  --bg-primary: #F8F8F6;    /* 页面底色 */
  --bg-surface: #F6F6F4;    /* 模型气泡底色 */
  --text-primary: #1F1E1D;
  --text-secondary: #6E6D66;
}""",
        },
        {
            "id": "rise",
            "name": "气泡渐入",
            "note": "新消息出现时往上浮一点，别调太慢",
            "css": """/* 气泡渐入 */
.msg-user, .msg-claude {
  animation: cn-rise 240ms cubic-bezier(.22,.61,.36,1) both;
}
@keyframes cn-rise {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: none; }
}""",
        },
        {
            "id": "composer",
            "name": "输入条",
            "note": "底色、圆角、边框",
            "css": """/* 输入条 */
.composer-box {
  background: rgba(255,255,255,.55) !important;
  border-radius: 24px !important;
  box-shadow: inset 0 0 0 1px rgba(0,0,0,.06) !important;
  backdrop-filter: blur(12px) !important;
  -webkit-backdrop-filter: blur(12px) !important;
}""",
        },
        {
            "id": "wallpaper",
            "name": "背景渐变",
            "note": "毛玻璃要有东西可糊才好看，先给页面铺个底",
            "css": """/* 背景渐变。毛玻璃气泡配这个才看得出效果。
   换图片：background: url("https://…") center/cover no-repeat fixed; */
body {
  background: linear-gradient(160deg, #EDE7DF 0%, #E4E9EC 55%, #E9E4EC 100%)
              no-repeat fixed !important;
}""",
        },
    ]
