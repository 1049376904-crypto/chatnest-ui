# 和机的另一个家——前端

这是一个与Claude聊天 Web App 的前端。手机优先，iOS 上「添加到主屏幕」当独立 App 用。

打开就能点——`index.html` 里带了一层演示数据，所有 `/api/*` 都被拦下来用假数据回答，不需要后端、API key 或模型账号。

```bash
python3 -m http.server 8080
```

然后开 `http://127.0.0.1:8080/`。也可以直接双击 `index.html`。

## 出处

基于小莺老师 [ugui3u/chatnest](https://github.com/ugui3u/chatnest) 二次开发，沿用它的 Non-Commercial 许可。原项目的记忆系统来自糖糖老师，在这里感谢两位老师的开源分享

这一份改了挺多：界面从 2000 行长到 5900 行，砍掉了跟我个人生活绑死的那些功能，留下对话、内置时钟、记忆，日记心情记录以及聊天内容定向搜索。另外加了记忆的近似去重和检索的新鲜度排序（以下介绍）

## 这个包里有什么

只放了四件事，其他功能没有写进来。

**对话** — 流式回复、思考链（一行摘要，点开看全文）、工具调用卡片、模型切换与 effort 档位、图片和文件上传、消息长按（复制 / 编辑 / 重新生成）、编辑后从那一条重开、微信式连发气泡、会话侧栏（Starred / Recents、重命名、星标、删除）、全库搜聊天记录并跳回原文、HTML/SVG artifact 预览、KaTeX 公式、本地 IndexedDB 消息缓存（只缓存最近一窗，长会话不会把手机撑爆）。

**内置时钟** — 小机不用调工具自动知道时间，这一件在服务端，见下面。

**Memories** — Profile 页：名字、Saved memories（可逐条开关）、Preferences（自定义指令）、Memory summary（从最近几个对话框整理出来的长期印象，可看可改可关）。带近似去重，见下面。

**Diary** — 按月分组的日记列表 + 全文搜索；Calendar 视图按年铺开，每天记机和人的心情以及重要事件，点开就能改。

## 内置时钟（服务端那半边）

时钟没法只放在前端——时间要跟着服务器的会话记录算。`server-clock.py` 就是那一个模块，无依赖，Python 3.11+：

```python
from clock import prompt_note
prompt += prompt_note(last_message_at)   # last_message_at = 上一条消息的 ISO 时间戳
```

拼出来的是这样一行：

```text
[现在] 2026年8月3日 周一 15:28（Asia/Singapore） · 距上次说话 3小时20分
```

环境变量：`APP_TIMEZONE`（默认 `Asia/Singapore`）、`CLOCK_ENABLED=0` 关掉、`CLOCK_GAP_MIN_SEC`（间隔小于这个秒数就不提，默认 300）。

⚠️ 一律 `datetime.now(ZoneInfo(APP_TIMEZONE))`，别用 naive 的 `datetime.now()`。naive 版给的是服务器所在时区的墙钟，机器和用户不在同一时区时会差好几个小时，模型就会在下午跟你说晚安。这个坑我踩过。

## 记忆去重（服务端那半边）

只比「一模一样」挡不住换个说法的同一条：「我住新加坡」和「我在新加坡住」会各存一条，攒几个月上下文里就全是近义句在占位置。

`server-memory.py` 是那套规则，无依赖，Python 3.11+：

```python
from server_memory import find_duplicate, MemoryRejected

clash = find_duplicate(new_content, profile["savedMemories"])
if clash is not None:
    raise MemoryRejected("duplicate", clash["content"])
```

五道判断：**全等 → 否定词守卫 → 包含 → 编辑距离 → 字符集重合**，任一命中即算重复。

否定词守卫是必需的：「喜欢香菜」和「不喜欢香菜」字符集重合 0.8，光看相似度必然误合。编辑距离的门槛按长度浮动——「周一要交报告」和「周五要交报告」相似度 0.83，放长句里是同一条，放 6 个字里是两件事，所以短句要 0.95 才算重复。

`index.html` 里有一份等价的 JS 实现（`isNearDuplicate`），**两边规则必须一致**。后端拦 `POST /api/profile/memory`（模型自己写记忆的入口），前端在 Saved memories 列表里把重复的那条标红给人看——前端只标不删，判重是启发式的，留不留由人定。

直接跑 `python3 server-memory.py` 会过一遍自带的用例。

环境变量：`MEMORY_DUP_RATIO`（0.82）、`MEMORY_DUP_JACCARD`（0.80）、`MEMORY_DUP_MIN_CHARS`（4）。调低会误杀新记忆，默认留得偏保守。

## 接自己的后端

把 `index.html` head 里的开关改掉：

```js
window.AGENT_APP_DEMO=false;
```

然后实现下面这些接口。演示层在 `index.html` 末尾，每个分支上面都写了对应的契约，照着回就行。除 `/api/auth` 外都带 `Authorization: Bearer <token>`。

| 接口 | 说明 |
| --- | --- |
| `POST /api/auth` | `{password}` → `{token}` |
| `GET /api/models` | `{models:[{id,label,desc,thinking,primary}]}`，`thinking` 为 `none`/`adaptive`/`extended` |
| `POST /api/chat` | SSE，事件：`conversation` / `thinking` / `tool_use` / `tool_result` / `delta` / `done` |
| `GET /api/sessions` | `{sessions:[{conv_id,title,starred,created_at,updated_at}]}` |
| `GET /api/sessions/{id}/messages` | `{messages:[...],has_more,next_before_id,has_newer,next_after_id}`，支持 `limit` / `before_id` / `after_id` / `around_id` |
| `PATCH /api/sessions/{id}/star`<br>`PATCH /api/sessions/{id}/title`<br>`DELETE /api/sessions/{id}` | 星标 / 重命名 / 删除 |
| `GET /api/search?q=&limit=` | `{results:[{conv_id,conv_title,message_id,role,snippet,time_text,starred}]}` |
| `GET/PUT /api/profile` | `{profile:{fullName,nickname,savedMemories[],preferences}}` |
| `POST /api/profile/memory` | 模型自己写记忆的入口。重了返回 `{saved:false, reason:"duplicate", detail:"跟哪条重了"}`，满了 `reason:"limit"` |
| `GET/PUT/DELETE /api/memory-summary`<br>`POST /api/memory-summary/generate` | 长期印象摘要，后台生成期间 `running:true`，前端会轮询 |
| `GET /api/diary` | `{entries:[{date,text}]}` |
| `GET /api/calendar?year=` | `{year,startMonth,days:{'YYYY-MM-DD':{me:{mood,event},partner:{mood,event}}}}` |
| `GET/PUT /api/calendar/{date}` | 单天读写 |
| `POST /api/upload` | multipart → `{conversation_id,attachments:[...]}` |
| `GET/PUT /api/avatars` | `{me:{url},ai:{url}}` |
| `POST /api/thinking-summary` | `{thinking}` → `{summary}`，思考链那一行摘要 |
| `POST /api/tool-caption` | `{tool_name,tool_input,tool_output}` → `{caption}` |
| `GET /api/splash` | `{line}`，空会话上方那句招呼 |
| `POST /api/warmup` | 预热模型进程，可以直接返回 `{ok:true}` |

## 素材

公开版不带图片素材：壁纸、开屏图、图标、字体都拿掉了，logo 和状态动画是通用占位符。界面因此是纯色底，不会开天窗。
当然想要素材可以私信我！

想换成自己的图，把 CSS 变量 `--chat-wallpaper` 设成 `url(...)` 即可（head 里那段脚本就是设它的地方）。替换 logo / 图标 / 字体见 `BRANDING.md`。请只使用你自己拥有或已获授权的素材。

## 许可证

非商用。允许非商业复制、修改和再发布；禁止商业使用；二传二改带上我的名字好嘛（卑微）
以上为小猫二次编辑，有bug随时反馈给我
