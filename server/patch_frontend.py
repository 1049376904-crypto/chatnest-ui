#!/usr/bin/env python3
"""给 index.html 打两个补丁。幂等，重复跑不会重复改。

    python3 server/patch_frontend.py /var/www/chatnest-ui/index.html

为什么用脚本而不是直接改文件：那个 html 单文件 38 万字节，整份重写
风险太大；而且你以后从上游拉新版本之后，重跑一次就行。

补丁一：启动时恢复上次的会话。
启动那一句长这样（约 5044 行）：

    if(state.token)showChat();else loadMsgAvatars();resetEmpty();loadModels();…

`resetEmpty()` 头一件事就是 removeItem('chat_conversation')，所以在它
之后做任何补救都来不及——conv_id 已经没了。这里把那一句替换成先查
localStorage：有 conv_id 就 openSession(它)，没有才 resetEmpty()。

补丁二：历史里的工具卡片别默默隐藏。
源码建完卡片就 display='none'，而能展开它的按钮只在 traces 里有
summary 条目时才创建——两个条件一错开，卡片就在 DOM 里永远打不开。
"""

import re
import shutil
import sys
from pathlib import Path

MARK = "/*chatnest-patched*/"

# 启动那一句的原文。整句替换，不做正则拼接——这一句里有分号有函数调用，
# 正则改写太容易改出语法错。
BOOT_NEEDLE = (
    "if(state.token)showChat();else loadMsgAvatars();"
    "resetEmpty();loadModels();updateSessionHeader();updateSendButton();"
)

BOOT_REPLACEMENT = (
    "if(state.token)showChat();else loadMsgAvatars();"
    "(function(){" + MARK + "\n"
    "// 有上次的会话就接着上次那个，没有才开空白。\n"
    "// 注意 resetEmpty() 会清掉 chat_conversation，所以必须先读再决定。\n"
    "var id=null;try{id=localStorage.getItem('chat_conversation')}catch(e){}\n"
    "if(id&&state.token){\n"
    "  // 标题先留空，openSession 会自己拉回真正的标题。\n"
    "  Promise.resolve().then(function(){return openSession({conv_id:id,title:''})})\n"
    "    .catch(function(){return resetEmpty()});\n"
    "}else{resetEmpty()}\n"
    "})();loadModels();updateSessionHeader();updateSendButton();"
)

# 上一版补丁：整段 script 插在 </body> 之前。认出来就撤掉。
OLD_PATCH_RE = re.compile(
    r"\n<script>/\*chatnest-patched\*/.*?</script>\n",
    re.DOTALL,
)


def strip_old(text: str) -> tuple[str, str]:
    """撤掉上一版那段无效补丁。"""
    cleaned, count = OLD_PATCH_RE.subn("", text)
    if count:
        return cleaned, f"旧补丁：已撤掉 {count} 段"
    return text, ""


def patch_boot(text: str) -> tuple[str, str]:
    if MARK in text:
        return text, "会话恢复：已打过，跳过"
    if BOOT_NEEDLE not in text:
        return text, "会话恢复：没找到启动那一句（上游可能改过），未改"
    return text.replace(BOOT_NEEDLE, BOOT_REPLACEMENT, 1), "会话恢复：已改启动逻辑"


def patch_traces(text: str) -> tuple[str, str]:
    """把没有 summary 时那句 tr.style.display='none' 去掉。"""
    needle = (
        "else{const tr=_buildTraceRowFromHistory(toolTraces,'');"
        "if(tr){tr.style.display='none';col.append(tr)}}"
    )
    replacement = (
        "else{const tr=_buildTraceRowFromHistory(toolTraces,'');"
        "if(tr){col.append(tr)}}"
    )
    if replacement in text:
        return text, "工具卡片：已打过，跳过"
    if needle not in text:
        return text, "工具卡片：没找到目标代码（上游可能改过），未改"
    return text.replace(needle, replacement, 1), "工具卡片：已取消隐藏"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"文件不存在：{path}")
        return 1

    text = path.read_text(encoding="utf-8")
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"备份：{backup}")

    notes = []
    text, note = strip_old(text)
    if note:
        notes.append(note)
    text, note = patch_traces(text)
    notes.append(note)
    text, note = patch_boot(text)
    notes.append(note)

    path.write_text(text, encoding="utf-8")
    for line in notes:
        print(line)
    print("完了。手机上硬刷新一下页面（或者换个无痕标签页）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
