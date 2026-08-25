# 后端（OpenAI 兼容中转 + MCP 工具）

把 `index.html` 里的 `/api/*` 接上真实服务。模型走 OpenAI 兼容的 chat
completions 接口——中转站、本地 ollama、官方 API 都行，只要它接
`POST {base_url}/chat/completions` 并支持 `stream: true`。工具走 MCP
（streamable HTTP）。

不需要 Claude Code CLI，不需要登录浏览器，不读 `ANTHROPIC_API_KEY`。

## 装

```bash
cd /path/to/chatnest-ui
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
cp server/env.example server/.env
python3 -c "import secrets;print('CHAT_SECRET=' + secrets.token_urlsafe(32))"
```

`server/.env` 里只有两项是必填的：

```ini
CHAT_PASSWORD=你自己的登录密码
CHAT_SECRET=刚才生成的那串
```

其余（base URL、API key、模型、MCP）在控制台页面上填，见下面。
你也可以先写在 `.env` 里当默认值，控制台里留空就会回退用它。

然后先跑自检：

```bash
python3 -m server.selftest
```

它不联网、不碰 MCP、不花额度，只验存储层、去重、时钟、配置读写、
工具名洗洗、tool_calls 分片累积和路由鉴权。全绿再往下走，否则后面报错
你分不清是代码问题还是配置问题。

❗ 自检全绿不代表你的 key 或 MCP 地址是对的——它根本不联网。

## 跑

```bash
./server/run.sh
```

开发时直接访 `http://127.0.0.1:8787/health`，返回 `{"ok":true}` 就是起来了。

生产用 systemd（`/etc/systemd/system/chatnest.service`）：

```ini
[Unit]
Description=ChatNest backend
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/chatnest-ui
ExecStart=/path/to/chatnest-ui/.venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8787
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now chatnest
journalctl -u chatnest -f
```

## 接上前端

三步。

**一、关演示模式。** 改 `index.html` head 里那一行：

```js
window.AGENT_APP_DEMO=false;
```

**二、打前端补丁。**

```bash
python3 server/patch_frontend.py /var/www/chatnest-ui/index.html
```

两个改动：刷新后回到上次那个会话（原本每次刷新都开空白会话）；
历史里的工具卡片不再被默默隐藏。幂等，从上游拉了新版本之后重跑一次就行；
自动备份成 `index.html.bak`，想还原就拷回去。

**三、nginx 反代。** 前端所有请求都是相对路径 `/api/*`，反代不需要改
任何前端代码，也不会有 CORS：

```nginx
server {
    listen 443 ssl;
    server_name chat.example.com;

    root /var/www/chatnest-ui;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # 控制台页面由后端提供，不在静态目录里
    location = /dashboard {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 这两行是 SSE 的命。proxy_buffering 不关的话，nginx 会把流式
        # 回复攒着，等模型说完一次吐给你——看上去就像“卡十几秒，然后
        # 突然出一大段”。工具跑得久时尤其明显。
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
    }
}
```

改完 `nginx -t && systemctl reload nginx`。

## 控制台

`https://你的域名/dashboard`，用聊天页那个密码登录。里面四块：

**模型清单** —— 聊天页模型菜单里显示的就是这些。`id` 要跟中转站的名字
一字不差（包括 `[前缀]` 这种），`label` 是给你自己看的。

**MCP 服务** —— 地址写到 `/mcp` 为止，token 走 `Authorization: Bearer`。
工具清单是连上去实时拉的，不用手填。旅边那个「测试连接」点一下就能看到
握手结果和工具名，不用去翻日志。一个地址后面挂多少个工具都行，
`tools/list` 回什么就全部交给模型，这边不筛选。

**模型接口** —— base URL、API key、主模型、摘要模型、token 上限、
历史条数、超时。留空就回退到 `.env` 里的值。

**开关** —— 工具调用总开关、详细日志、工具最多连续几轮。

顶栏两个按钮：「保存」写盘并立即生效，「热更新」重读配置并重拉 MCP 工具。
两者都不需要重启进程。

配置存在 `server/data/settings.json`。API key 和 MCP token 在页面上只显示
打码后的形态（`sk-abc…wxyz`），输入框留空即沿用旧值。

**详细日志那个开关要注意：**开着会把发给模型的完整请求（含系统提示词、
聊天历史）和工具参数写进系统日志。排错时开，完事关掉。

## 跟上游不一样的地方

**工具走 MCP，不是本地执行。** 上游靠 Claude CLI 直接读写硬盘，这边把
`tools/list` 拿到的工具交给模型，它说要调就去 MCP server 执行，
结果拼回对话再问一遍，最多 `max_tool_rounds` 轮。模型能不能写记忆取决于
你接的 MCP 有没有那个工具。

**上传限 12MB。** 上游允许 60MB，因为那边模型能用 Read 工具自己看硬盘上的
文件。这边附件得 base64 塞进请求体，图片 8MB 就能把中转站的体积上限顶穿。
图片转 data URL，文本直接贴进正文，PDF 和 HEIC 存得下但模型读不了。

**没有向量检索。** 上游那套 ChromaDB + jieba/BM25 没搬过来（它得多拉三个
重依赖）。profile 里的 Saved memories 和长期印象摘要是全文拼进 system prompt 的，
一两百条无所谢，再多就要想办法了。想要检索，接个带检索工具的 MCP 更直接。

**上下文靠自己拼。** 上游靠 Claude CLI 的 session 文件续接，这边每次请求从
库里拉最近 `history_turns` 条发过去。调大更连贯，但每次的 input token 也跟着涨。

## 坑

**思考链不一定有。** 中转站把思考内容放哪个字段各家不同，`llm.py` 里试了四种
常见写法（`reasoning_content` / `reasoning` / `thinking` / `thought`）。都不命中
就是没有，前端思考那一栏不出现而已。

**带工具时不发思考预算。** `thinking.budget_tokens` 和 `tools` 一起送，有些中转站
会 400，所以带工具那几轮只发 `reasoning_effort`。要是你的中转站连这个也不认，
把 `llm.py` 里 `EFFORT_MAP` 那一行注掉。

**工具名会被重写。** OpenAI 只收 `^[a-zA-Z0-9_-]{1,64}$`，所以带中文、点、
斜杠的工具名会被洗成下划线，内部留了一张映射表回查真名。你在控制台看到的
是真名，模型看到的是洗过的。

**时区必须设。** `APP_TIMEZONE` 默认 `Asia/Shanghai`。VPS 在国外的话这一项不能不改，
不然模型会在你的下午跟你说晚安。

**别监听 0.0.0.0。** 本服务只有应用内密码一层防护，没有频率限制、没有封禁。
直接摆到公网等于把你的 API 额度交给扫段的。让 nginx 反代 127.0.0.1，
HTTPS 在 nginx 那一层上。

**token 不过期。** 和上游一样，token 是 `CHAT_SECRET` 的确定性 HMAC，没有有效期，
也无法单独吐回某一个。想让所有已登录设备下线：改 `CHAT_SECRET`，重启。

**一次只跟一个回复。** 上一条还在回的时候发新消息会被拒。不是技术限制，
是防手滑双击发送把额度花两份。

## 数据在哪

默认 `server/data/`（已 gitignore），可用 `DATA_DIR` 改。

```
server/data/
  conversations.db      会话和消息
  settings.json         控制台改的那些（含 API key、MCP token）
  profile.json          名字、Saved memories、自定义指令
  memory_summary.json   长期印象摘要
  diary.json            日记
  calendar.json         日历心情
  avatars.json          头像
  uploads/<conv_id>/    附件
```

备份就是打包这一个目录。里面有密钥，别提交进仓库、别丢到公开地方。
JSON 写入走临时文件 + `os.replace`，写到一半断电不会把文件弄成碎的。

## 接口

上游 README 那张表里的全部已实现，外加控制台那四条。

| 接口 | 说明 |
| --- | --- |
| `POST /api/auth` | 密码 → token |
| `GET /api/models` | 读控制台配的模型清单 |
| `POST /api/chat` | SSE：`conversation` / `thinking` / `delta` / `tool_use` / `tool_result` / `trace_summary` / `done` / `error` |
| `GET /api/sessions` | 会话列表 |
| `GET /api/sessions/{id}/messages` | `limit` / `before_id` / `after_id` / `around_id` |
| `PATCH .../star`、`PATCH .../title`、`DELETE ...` | 星标 / 重命名 / 删除 |
| `GET /api/search` | 全库搜聊天记录（SQL LIKE）|
| `GET/PUT /api/profile` | 资料与记忆 |
| `POST /api/profile/memory` | 写记忆，带近似去重 |
| `GET/PUT/DELETE /api/memory-summary` + `/generate` | 后台生成，`running` 轮询 |
| `GET/PUT /api/diary` | 日记 |
| `GET /api/calendar`、`GET/PUT /api/calendar/{date}` | 日历 |
| `POST /api/upload` | 附件，12MB |
| `GET/PUT /api/avatars` | 头像 |
| `POST /api/thinking-summary` | 思考链那一行摘要 |
| `POST /api/tool-caption` | 工具说明 |
| `GET /api/splash` | 空会话那句招呼，本地句子不过模型 |
| `POST /api/warmup` | 直接 `{ok:true}` |
| `GET /dashboard` | 控制台页面 |
| `GET/PUT /api/admin/settings` | 读写配置 |
| `POST /api/admin/reload` | 热更新 |
| `POST /api/admin/mcp/refresh`、`/probe` | 重拉工具、测单个地址 |

除 `/api/auth`、`/api/models`、`/api/splash`、`/health`、`/dashboard` 外全部要
`Authorization: Bearer <token>`。（`/dashboard` 只是个页面壳子，里面每个请求都鉴权。）

❗ `/api/memory-summary`、`/api/avatars`、`/api/calendar` 这三组的字段名是按
上游 README 的描述写的，没能逐字比对 `index.html` 末尾的演示层（单文件
384KB）。要是前端哪一栏不显示，开浏览器 Network 看那个请求的响应，
对不上的键名在 `server/profile.py` 里改一下就行。

## 系统提示词

两种写法，`SYSTEM_PROMPT` 环境变量优先，否则读 `server/prompt.txt`
（已 gitignore，很适合放个人化的那些东西）。

❗ 长提示词一定要放 `prompt.txt`，不要往 `.env` 里塞。`.env` 是一行一个
`KEY=VALUE`，多行文本会从第二行开始被丢掉，而且不报错——只会在启动时
吐几条 `python-dotenv could not parse statement`，很容易当成无关警告忽略掉。

profile 里的名字、Saved memories、自定义指令和长期印象摘要会自动拼在它后面。
