#!/usr/bin/env python3
"""给 index.html 打四个补丁。幂等，重复跑不会重复改。

    python3 server/patch_frontend.py /var/www/chatnest-ui/index.html

为什么用脚本而不是直接改文件：那个 html 单文件 38 万字节，整份重写
风险太大；而且你以后从上游拉新版本之后，重跑一次就行。

补丁一：启动时恢复上次的会话。
启动那一句里 `resetEmpty()` 头一件事就是 removeItem('chat_conversation')，
所以在它之后做任何补救都来不及——conv_id 已经没了。这里把那一句换成
先查 localStorage：有 conv_id 就 openSession(它)，没有才 resetEmpty()。

补丁二：历史里的工具卡片别默默隐藏。
源码建完卡片就 display='none'，而能展开它的按钮只在 traces 里有
summary 条目时才创建——两个条件一错开，卡片就在 DOM 里永远打不开。

补丁三：接上自定义 CSS。
在 </head> 前插一行 `<link href="/api/custom.css">`。CSS 正文存在控制台，
改完刷新页面即生效，index.html 不再动。

补丁四：思考链折叠改本地生成，不再调模型。
原本思考结束后会调一次 /api/thinking-summary 把本地预览换成模型精炼的
摘要再折进气泡。后端那个接口已经不调模型直接返回空，这里把折叠逻辑
换成同步用本地的 thoughtPreview 触发，不发请求也不等网络。
"""

import re
import shutil
import sys
from pathlib import Path

MARK = "/*chatnest-patched*/"
CSS_MARK = "chatnest-custom-css"

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

# 带上时间戳查询串会阻止缓存，但那样每次都重拉；这里靠后端发
# Cache-Control: no-cache，浏览器会带 ETag 来问，没改就 304。
CSS_LINK = (
    '<link rel="stylesheet" href="/api/custom.css" '
    f'data-{CSS_MARK}="1">\n'
)

# 思考链折叠：原文调 /api/thinking-summary 换摘要再折叠。
THINKING_FOLD_NEEDLE = (
    "function _maybeFetchThinkingSummary(row){"
    "const phases=_getPhases(row);"
    "const tp=phases.find(p=>p.type==='thinking'&&p.processText);"
    "if(!tp||tp._summaryFetched)return;"
    "tp._summaryFetched=true;"
    "_setPhases(row,phases);"
    "fetchThinkingSummary(tp.processText).then(s=>{"
    "if(!s)return;"
    "const ps=_getPhases(row);"
    "const p=ps.find(x=>x.id===tp.id);"
    "if(!p)return;"
    "const clean=dedupeSummaryText(s);"
    "p.summary=clean;p.title=clean;"
    "_setPhases(row,ps);"
    "const msgRow=row.closest('.msg-claude');"
    "if(!msgRow)return;"
    "row.style.display='none';"
    "foldThinkingIntoBubble(msgRow,tp.processText);"
    "const sum=msgRow.querySelector('.ai-bubble > .thinking-summary');"
    "if(sum){"
    "sum.classList.add('show');"
    "setThoughtSummary(sum,tp.processText,clean);"
    "const snap=tp.processText;"
    "sum.onclick=()=>{$('thoughtContent').textContent=snap;sheet('thought',true)};"
    "syncAiBubble(sum.closest('.ai-bubble'))"
    "}"
    "}).catch(()=>{})"
    "}"
)

THINKING_FOLD_REPLACEMENT = (
    "function _maybeFetchThinkingSummary(row){"
    "const phases=_getPhases(row);"
    "const tp=phases.find(p=>p.type==='thinking'&&p.processText);"
    "if(!tp||tp._summaryFetched)return;"
    "tp._summaryFetched=true;"
    "_setPhases(row,phases);"
    # 同步走本地预览，不再发网络请求；tp.summary 是流式时 thoughtPreview
    # 已经算好的那句，兜底再算一次防止是空的。
    "(function(){"
    "const s=tp.summary||thoughtPreview(tp.processText);"
    "if(!s)return;"
    "const ps=_getPhases(row);"
    "const p=ps.find(x=>x.id===tp.id);"
    "if(!p)return;"
    "const clean=s;"
    "p.summary=clean;p.title=clean;"
    "_setPhases(row,ps);"
    "const msgRow=row.closest('.msg-claude');"
    "if(!msgRow)return;"
    "row.style.display='none';"
    "foldThinkingIntoBubble(msgRow,tp.processText);"
    "const sum=msgRow.querySelector('.ai-bubble > .thinking-summary');"
    "if(sum){"
    "sum.classList.add('show');"
    "setThoughtSummary(sum,tp.processText,clean);"
    "const snap=tp.processText;"
    "sum.onclick=()=>{$('thoughtContent').textContent=snap;sheet('thought',true)};"
    "syncAiBubble(sum.closest('.ai-bubble'))"
    "}"
    "})()"
    "}"
)

# 上一版补丁：整段 script 插在 </body> 之前。认出来就撕掉。
OLD_PATCH_RE = re.compile(
    r"\n<script>/\*chatnest-patched\*/.*?</script>\n",
    re.DOTALL,
)


def strip_old(text: str) -> tuple[str, str]:
    """撕掉上一版那段无效补丁。"""
    cleaned, count = OLD_PATCH_RE.subn("", text)
    if count:
        return cleaned, f"旧补丁：已撕掉 {count} 段"
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


def patch_css_link(text: str) -> tuple[str, str]:
    """在 </head> 前插自定义 CSS 的 link。必须是最后一行，不然盖不住前面的规则。"""
    if CSS_MARK in text:
        return text, "自定义 CSS：已打过，跳过"
    match = re.search(r"</head\s*>", text, re.IGNORECASE)
    if not match:
        return text, "自定义 CSS：没找到 </head>，未改"
    index = match.start()
    return text[:index] + CSS_LINK + text[index:], "自定义 CSS：已接上"


def patch_thinking_fold(text: str) -> tuple[str, str]:
    """思考链折叠改本地生成，不再调 /api/thinking-summary。"""
    if THINKING_FOLD_REPLACEMENT in text:
        return text, "思考链折叠：已打过，跳过"
    if THINKING_FOLD_NEEDLE not in text:
        return text, "思考链折叠：没找到目标函数（上游可能改过），未改"
    return (
        text.replace(THINKING_FOLD_NEEDLE, THINKING_FOLD_REPLACEMENT, 1),
        "思考链折叠：已改成本地生成，不再调模型",
    )


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
    for patcher in (patch_traces, patch_boot, patch_css_link, patch_thinking_fold):
        text, note = patcher(text)
        notes.append(note)

    path.write_text(text, encoding="utf-8")
    for line in notes:
        print(line)
    print("完了。手机上硬刷新一下页面（或者换个无痕模式标签页）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
