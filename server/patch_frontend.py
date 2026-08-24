#!/usr/bin/env python3
"""给 index.html 打两个补丁。幂等，重复跑不会重复改。

    python3 server/patch_frontend.py /var/www/chatnest-ui/index.html

为什么用脚本而不是直接改文件：那个 html 单文件 38 万字节，整份重写
风险太大；而且你以后从上游拉新版本之后，重跑一次就行。

补丁一：启动时恢复上次的会话。
localStorage 里一直存着 chat_conversation（前端自己写的），但没人读。
给 resetEmpty 包一层：首次进页面时如果存着 conv_id 就直接打开它，
后续主动新建会话照旧行为。

补丁二：历史里的工具卡片别默默隐藏。
源码 2621 行建完卡片就 display='none'，而能展开它的那个按钮只在
有 summary 条目时才创建——两个条件一错开，卡片就在 DOM 里永远打不开。
这里把那一句隐藏去掉。（后端也补上了 summary，两道保险。）
"""

import re
import shutil
import sys
from pathlib import Path

MARK = "/*chatnest-patched*/"

BOOTSTRAP = """
<script>%s
// 启动时恢复上次的会话。只管首次进页面这一下，
// 之后你主动点「新对话」依旧得到空白会话。
(function () {
  "use strict";
  var done = false;
  function boot() {
    if (done) return;
    done = true;
    var id = null;
    try { id = localStorage.getItem("chat_conversation"); } catch (e) { return; }
    if (!id) return;
    if (typeof openSession !== "function") return;
    // 标题这一刷先留空，openSession 会自己拉回真正的标题。
    try { openSession({ conv_id: id, title: "" }); } catch (e) {}
  }
  // resetEmpty 是启动流程里最后一步，包住它比猜启动时机可靠。
  var timer = setInterval(function () {
    if (typeof resetEmpty !== "function" || typeof openSession !== "function") return;
    clearInterval(timer);
    var original = resetEmpty;
    window.resetEmpty = resetEmpty = function () {
      var result = original.apply(this, arguments);
      if (!done) {
        // 等 resetEmpty 把空白页画完再接上历史，否则会被它覆盖。
        Promise.resolve(result).then(boot, boot);
      }
      return result;
    };
    // 若 resetEmpty 已经跑过了（补丁插得比启动流程晚），就自己补一次。
    setTimeout(function () { if (!document.getElementById("empty")) return; boot(); }, 1200);
  }, 60);
  setTimeout(function () { clearInterval(timer); }, 20000);
})();
</script>
""" % MARK


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


def patch_bootstrap(text: str) -> tuple[str, str]:
    if MARK in text:
        return text, "会话恢复：已打过，跳过"
    match = re.search(r"</body\s*>", text, re.IGNORECASE)
    if not match:
        return text, "会话恢复：没找到 </body>，未改"
    index = match.start()
    return text[:index] + BOOTSTRAP + text[index:], "会话恢复：已插入"


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

    text, note1 = patch_traces(text)
    text, note2 = patch_bootstrap(text)
    path.write_text(text, encoding="utf-8")

    print(note1)
    print(note2)
    print("完了。手机上硬刷新一下页面（或者换个无痕模式标签页）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
