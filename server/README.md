# 后端（OpenAI 兼容中转版）

把 `index.html` 里的 `/api/*` 接上真实服务。模型走 OpenAI 兼容的 chat
completions 接口——中转站、本地 ollama、官方 API 都行，只要它接
`POST {base_url}/chat/completions` 并支持 `stream: true`。

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

把那行 `CHAT_SECRET` 贴进 `server/.env`，再填这四个：

```ini
CHAT_PASSWORD=你自己的登录密码
OPENAI_BASE_URL=https://你的中转站/v1
OPENAI_API_KEY=sk-xxxx
CHAT_MODEL=claude-sonnet-4-6
```

`OPENAI_BASE_URL` 写到 `/v1` 为止，**后面不要带 `/chat/completions`**（带了
也会被自动抓掉，但别依赖这个）。

然后先跑自检：

```bash
python3 -m server.selftest
```

它不联网、不花额度，只验存储层、去重、时钟和 messages 组装。全绿再往下走，
否则后面报错你分不清是代码问题还是 key 问题。

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
User=你的用户名
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

两步。

**一、关演示模式。** 改 `index.html` head 里那一行：

```js
window.AGENT_APP_DEMO=false;
```

**二、nginx 把 `/api/` 反代到后端。** 前端所有请求都是相对路径 `/api/*`，
反代不需要改任何前端代码，也不会有 CORS：

```nginx
server {
    listen 443 ssl;
    server_name chat.example.com;

    # 前端静态文件
    root /var/www/chatnest-ui;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 这两行是 SSE 的命。proxy_buffering 不关的话，nginx 会把流式
        # 回复攒着，等模型说完一次吐给你——看上去就像“卡十几秒，然后
        # 突然出一大段”。
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
    }
}
```

改完 `nginx -t && systemctl reload nginx`。

## 跟上游不一样的地方

**没有工具调用。** `tool_use` / `tool_result` 两个 SSE 事件永远不会发——中转 API
背后没有执行环境，没东西可调。`/api/tool-caption` 接口还在，只是没人调它。
模型也因此不能自己写记忆（那本来是个工具），`POST /api/profile/memory` 得你
自己调，或者在前端 Saved memories 里手动加。

**上传限 12MB。** 上游允许 60MB，因为那边模型能用 Read 工具自己看硬盘上的
文件。这边附件得 base64 塞进请求体，图片 8MB 就能把中转站的体积上限顶穿。
图片转 data URL，文本直接贴进正文，PDF 和 HEIC 存得下但模型读不了。

**没有向量检索。** 上游那套 ChromaDB + jieba/BM25 没搬过来（它得多拉三个
重依赖）。profile 里的 Saved memories 和长期印象摘要是全文拼进 system prompt 的，
一两百条无所谢，再多就要想办法了。

**上下文靠自己拼。** 上游靠 Claude CLI 的 session 文件续接，这边每次请求从
库里拉最近 `HISTORY_TURNS` 条（默认 30）发过去。调大更连贯，但每次的
input token 也跟着涨。

## 坑

**思考链不一定有。** 中转站把思考内容放哪个字段各家不同，`llm.py` 里试了四种
常见写法（`reasoning_content` / `reasoning` / `thinking` / `thought`）。都不命中
就是没有，前端思考那一栏不出现而已，不影响聊天。

**effort 档位可能被忽略。** 请求里同时带了 `reasoning_effort`（OpenAI 系）和
`thinking.budget_tokens`（Anthropic 系），不认识的一方一般直接丢掉。要是你的中转站
严格校验未知字段并报 400，把 `llm.py` 里 `THINKING_BUDGET` 对应档位改成 `None`。

**时区必须设。** `APP_TIMEZONE` 默认 `Asia/Shanghai`。VPS 在国外的话这一项不能不改，
不然模型会在你的下午跟你说晚安。

**别监听 0.0.0.0。** 本服务只有应用内密码一层防护，没有频率限制、没有封禁。
直接摆到公网等于把你的 API 额度交给扫段的。让 nginx 反代 127.0.0.1，
HTTPS 在 nginx 那一层上。

**token 不过期。** 和上游一样，token 是 `CHAT_SECRET` 的确定性 HMAC，没有有效期，
也无法单独吐回某一个。想让所有已登录设备下线：改 `CHAT_SECRET`，重启。

## 数据在哪

默认 `server/data/`（已 gitignore），可用 `DATA_DIR` 改。

```
server/data/
  conversations.db      会话和消息
  profile.json          名字、Saved memories、自定义指令
  memory_summary.json   长期印象摘要
  diary.json            日记
  calendar.json         日历心情
  avatars.json          头像
  uploads/<conv_id>/    附件
```

备份就是打包这一个目录。JSON 写入走临时文件 + `os.replace`，写到一半断电不会
把 `profile.json` 弄成碎的。

## 接口对照

README 那张表里的全部已实现：

| 接口 | 状态 |
| --- | --- |
| `POST /api/auth` | ✓ |
| `GET /api/models` | ✓ 读 `server/models.json` |
| `POST /api/chat` | ✓ SSE：`conversation` / `thinking` / `delta` / `done` / `error` |
| `GET /api/sessions` | ✓ |
| `GET /api/sessions/{id}/messages` | ✓ `limit` / `before_id` / `after_id` / `around_id` |
| `PATCH .../star`、`PATCH .../title`、`DELETE ...` | ✓ |
| `GET /api/search` | ✓ SQL LIKE |
| `GET/PUT /api/profile` | ✓ |
| `POST /api/profile/memory` | ✓ 带近似去重 |
| `GET/PUT/DELETE /api/memory-summary` + `/generate` | ✓ 后台生成，`running` 轮询 |
| `GET/PUT /api/diary` | ✓ |
| `GET /api/calendar`、`GET/PUT /api/calendar/{date}` | ✓ |
| `POST /api/upload` | ✓ 12MB |
| `GET/PUT /api/avatars` | ✓ |
| `POST /api/thinking-summary` | ✓ 走 `SUMMARY_MODEL` |
| `POST /api/tool-caption` | ✓ 但不会被调到 |
| `GET /api/splash` | ✓ 本地句子，不过模型 |
| `POST /api/warmup` | ✓ 直接 `{ok:true}` |

❗ `/api/memory-summary`、`/api/avatars`、`/api/calendar` 这三组的字段名是按
上游 README 的描述写的，没能逐字比对 `index.html` 末尾的演示层（单文件
384KB）。要是前端哪一栏不显示，开浏览器 Network 看那个请求的响应，
对不上的键名在 `server/profile.py` 里改一下就行。

## 模型列表

改 `server/models.json`。不去拉上游 `/v1/models`——中转站那个接口常常返回
几百个型号，堆进前端选择器里没法用。`thinking` 字段只影响前端要不要显示
档位开关，填 `none` / `adaptive` / `extended`。

## 系统提示词

两种写法，`SYSTEM_PROMPT` 环境变量优先，否则读 `server/prompt.txt`（已 gitignore，
很适合放个人化的那些东西）。profile 里的名字、Saved memories、自定义指令和
长期印象摘要会自动拼在它后面。
