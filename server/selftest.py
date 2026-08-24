#!/usr/bin/env python3
"""不联网的自检。跑存储层、去重、时钟、配置、工具名洗洗、路由鉴权。

    python3 -m server.selftest

它不碰中转 API、不碰 MCP server，不花额度。数据写在临时目录，跑完就删。
部署完先跑这个，能把「代码有问题」和「API key / MCP 地址不对」分开。
"""

import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="chatnest-selftest-")
# setdefault + load_dotenv 的顺序有讲究：dotenv 默认不覆盖已存在的环境变量，
# 所以这里先占位，就算你的 server/.env 里写了真实密码也不会影响自检。
# DATA_DIR 必须硬赋值（不能 setdefault），不然会写到你真正的数据目录里。
os.environ.setdefault("CHAT_PASSWORD", "selftest")
os.environ.setdefault("CHAT_SECRET", "selftest-secret")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault("APP_TIMEZONE", "Asia/Shanghai")

from server import auth, clock, llm, profile as profile_store, settings, store  # noqa: E402
from server.dedupe import is_near_duplicate  # noqa: E402
from server.mcp_client import ToolRegistry, _parse_body, _slug  # noqa: E402

failures: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    ok = bool(condition)
    mark = "OK  " if ok else "FAIL"
    print(f"{mark}  {name}" + (f"  ← {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


class FakeResponse:
    """冒充 httpx.Response，只为了验 _parse_body 能不能同时吃 JSON 和 SSE。"""

    def __init__(self, text: str, content_type: str) -> None:
        self.text = text
        self.headers = {"content-type": content_type}


def main() -> int:
    print(f"数据目录：{_TMP}\n")

    # ---- 登录 ----
    token = auth.issue_token("selftest")
    check("密码对了发 token", bool(token))
    check("token 验签通过", auth.verify_token(token or ""))
    check("错密码不发 token", auth.issue_token("wrong") is None)
    check("乱造的 token 不认", not auth.verify_token("deadbeef"))

    # ---- 存储 ----
    store.initialize_store()
    conv_id, user_id = store.begin_turn("你好，这是第一条")
    check("建会话", bool(conv_id))
    assistant_id = store.complete_turn(conv_id, "我在。", "想了一下")
    check("落回复", assistant_id > user_id)

    listed = store.conversation_list()
    check("会话列表有一条", len(listed) == 1, str(listed))
    check("标题取首条消息", listed[0]["title"] == "你好，这是第一条")

    page = store.conversation_messages(conv_id)
    check("读回两条消息", len(page["messages"]) == 2, str(page))
    check(
        "消息字段齐",
        all(
            key in page["messages"][0]
            for key in ("id", "role", "text", "thinking", "attachments", "traces")
        ),
    )

    # traces 落库与读回——工具卡片刷新后能不能恢复全靠这一条。
    # 字段名必须跟前端 _buildTraceRowFromHistory 对得上：
    # summary 用 text，tool_use 用 id/name/input，tool_result 用 tool_use_id/content。
    traces_conv, _ = store.begin_turn("带工具的一轮")
    store.complete_turn(
        traces_conv, "查完了。", "",
        [
            {"type": "summary", "text": "查了一下记忆库"},
            {"type": "tool_use", "id": "call_1", "name": "latent_search",
             "input": {"q": "柳州"}, "text_offset": 0},
            {"type": "tool_result", "tool_use_id": "call_1",
             "content": "找到三条", "is_error": False},
        ],
    )
    reloaded = store.conversation_messages(traces_conv)["messages"][-1]
    check("traces 落库后读得回", len(reloaded["traces"]) == 3, str(reloaded["traces"]))
    check("summary 在第一位", reloaded["traces"][0]["type"] == "summary")
    check("summary 用 text 字段", reloaded["traces"][0].get("text") == "查了一下记忆库")
    check("tool_use 带 id/name",
          reloaded["traces"][1].get("id") == "call_1"
          and reloaded["traces"][1].get("name") == "latent_search")
    check("tool_result 按 tool_use_id 配对",
          reloaded["traces"][2].get("tool_use_id") == "call_1")
    check("input 保持字典", isinstance(reloaded["traces"][1].get("input"), dict))
    store.delete_conversation(traces_conv)

    # 分页：再塞几轮，然后只拿 2 条
    for index in range(3):
        store.begin_turn(f"第 {index} 轮", conv_id)
        store.complete_turn(conv_id, f"回答 {index}")
    total = len(store.conversation_messages(conv_id)["messages"])
    check("一共八条", total == 8, str(total))

    limited = store.conversation_messages(conv_id, limit=2)
    check("limit=2 只回两条", len(limited["messages"]) == 2)
    check("has_more 为真", limited["has_more"] is True)
    check("next_before_id 有值", limited["next_before_id"] is not None)
    check("默认拿的是最后一窗", limited["messages"][-1]["text"] == "回答 2")

    older = store.conversation_messages(
        conv_id, before_id=limited["next_before_id"], limit=2
    )
    check("before_id 翻到更早的", older["messages"][-1]["id"] < limited["messages"][0]["id"])
    check("往前翻时 has_newer 为真", older["has_newer"] is True)

    around = store.conversation_messages(conv_id, around_id=assistant_id, limit=4)
    ids = [m["id"] for m in around["messages"]]
    check("around_id 命中那一条", assistant_id in ids, str(ids))
    check("around_id 两侧都有", len(ids) > 1, str(ids))

    # ---- 搜索 ----
    results = store.search_messages("第一条")
    check("搜得到", len(results) == 1, str(results))
    check("结果带 message_id", bool(results) and results[0]["message_id"] == user_id)
    check("结果带会话标题", bool(results) and results[0]["conv_title"])
    check("% 不当通配符", store.search_messages("%") == [])
    check("_ 不当通配符", store.search_messages("第_条") == [])
    check("反斜杠不炸", store.search_messages("\\") == [])
    check("空查询返回空", store.search_messages("   ") == [])

    # ---- 重新生成：砍尾巴再回滚 ----
    before = len(store.conversation_messages(conv_id)["messages"])
    prepared = store.prepare_retry_turn(conv_id, assistant_id)
    check("retry 拿到源用户消息", prepared["user_message_id"] == user_id)
    check(
        "retry 砍掉了尾巴",
        len(store.conversation_messages(conv_id)["messages"]) < before,
    )
    store.restore_branch(prepared["branch_id"])
    restored = store.conversation_messages(conv_id)["messages"]
    check("失败后能回滚", len(restored) == before, f"{len(restored)} != {before}")
    check("回滚后内容也对", restored[1]["text"] == "我在。", str(restored[1]))

    # ---- 编辑：只能改用户消息 ----
    try:
        store.prepare_edit_turn(conv_id, assistant_id, "改一下")
        check("不允许编辑回复", False, "竟然没报错")
    except ValueError:
        check("不允许编辑回复", True)

    try:
        store.prepare_edit_turn(conv_id, user_id, "   ")
        check("不允许改成空", False, "竟然收下了")
    except ValueError:
        check("不允许改成空", True)

    edited = store.prepare_edit_turn(conv_id, user_id, "你好，改过了")
    check("编辑生效", edited["message"] == "你好，改过了")
    check(
        "编辑后砍掉了后面的",
        len(store.conversation_messages(conv_id)["messages"]) == 1,
    )

    # ---- 删除与级联 ----
    store.delete_conversation(conv_id)
    check("删会话", store.conversation_list() == [])
    check("消息跟着级联删", store.search_messages("改过了") == [])
    try:
        store.conversation_messages(conv_id)
        check("删完读不到", False, "竟然还读得到")
    except store.ConversationNotFound:
        check("删完读不到", True)

    # ---- 去重 ----
    dedupe_cases = [
        ("我住新加坡", "我在新加坡住", True),
        ("喜欢喝冰美式", "喜欢喝冰美式咖啡", True),
        ("喜欢香菜", "不喜欢香菜", False),
        ("周一要交报告", "周五要交报告", False),
        ("他喜欢猫", "他讨厌猫", False),
    ]
    for a, b, want in dedupe_cases:
        check(f"去重：{a} ↔ {b}", is_near_duplicate(a, b) == want)

    # ---- 记忆写入 ----
    memory, reason, _ = profile_store.add_saved_memory("我住在杭州")
    check("记忆写得进", memory is not None, reason)
    dup, reason, detail = profile_store.add_saved_memory("我在杭州住")
    check("换个说法被拦", dup is None and reason == "duplicate", f"{reason} {detail}")
    check("拦下时说得出跟哪条重", "杭州" in detail, detail)
    empty, reason, _ = profile_store.add_saved_memory("   ")
    check("空记忆不写", empty is None and reason == "empty")

    profile = profile_store.read_profile()
    check("profile 里存下来了", len(profile["savedMemories"]) == 1)

    saved = profile_store.write_profile(
        {"nickname": "小猫", "preferences": {"enabled": True, "content": "说人话"}}
    )
    check("profile 写回去", saved["nickname"] == "小猫")
    context = profile_store.build_profile_context()
    check("自定义指令拼进上下文", "说人话" in context, context)

    # 关掉的偏好不该进上下文
    profile_store.write_profile(
        {"nickname": "小猫", "preferences": {"enabled": False, "content": "说人话"}}
    )
    check("关掉的偏好不进上下文", "说人话" not in profile_store.build_profile_context())

    # ---- 日记 ----
    profile_store.write_diary_entry("2026-08-24", "今天把后端接上了。")
    check("日记写得进", len(profile_store.read_diary()) == 1)
    check("日记能搜", len(profile_store.read_diary("后端")) == 1)
    check("搜不到就空", profile_store.read_diary("宇宙飞船") == [])
    profile_store.write_diary_entry("2026-08-24", "")
    check("写空等于删掉那天", profile_store.read_diary() == [])
    try:
        profile_store.write_diary_entry("2026/08/24", "格式错的")
        check("日期格式校验", False, "竟然收下了")
    except ValueError:
        check("日期格式校验", True)

    # ---- 日历 ----
    profile_store.write_calendar_day(
        "2026-08-24",
        {"me": {"mood": "平静", "event": "写代码"}, "partner": {"mood": "好"}},
    )
    year = profile_store.calendar_year(2026)
    check("日历按年拼得出来", "2026-08-24" in year["days"], str(year))
    check("不是那一年的不混进来", profile_store.calendar_year(2025)["days"] == {})
    day = profile_store.read_calendar_day("2026-08-24")
    check("单天读得回", day["me"]["mood"] == "平静")
    check("另一半也存下", day["partner"]["mood"] == "好")
    check(
        "没写过的天返回空壳",
        profile_store.read_calendar_day("2026-01-01")["me"]["mood"] == "",
    )

    # ---- 长期印象 ----
    profile_store.write_summary(content="他写代码时很急。", running=False)
    summary = profile_store.read_summary()
    check("摘要存得下", summary["content"].startswith("他写代码"))
    check("running 默认 false", summary["running"] is False)
    check("摘要也进上下文", "他写代码时很急" in profile_store.build_profile_context())
    profile_store.write_summary(enabled=False)
    check(
        "关掉的摘要不进上下文",
        "他写代码时很急" not in profile_store.build_profile_context(),
    )
    check("只改开关不动正文", profile_store.read_summary()["content"] != "")

    # ---- 头像 ----
    profile_store.write_avatars({"me": {"url": "/a.png"}, "ai": {"url": "/b.png"}})
    check("头像存取", profile_store.read_avatars()["ai"]["url"] == "/b.png")

    # ---- 运行期配置 ----
    check("默认开工具", settings.tools_enabled() is True)
    check("默认关详细日志", settings.debug_log() is False)
    check("没配模型也至少报一个", len(settings.models()) >= 1)

    settings.update({
        "chat_model": "test-model",
        "max_tokens": 1234,
        "openai_api_key": "sk-selftest-abcdefghijklmnop",
        "mcp_servers": [
            {"id": "s1", "name": "测试", "url": "http://127.0.0.1:1/mcp",
             "token": "tok-secret-value", "enabled": True},
        ],
    })
    check("改完即生效", settings.chat_model() == "test-model")
    check("数字项生效", settings.max_tokens() == 1234)
    check("key 存得进", settings.api_key() == "sk-selftest-abcdefghijklmnop")

    view = settings.public_view()
    check("对外不露完整 key",
          "abcdefghijklmnop" not in json.dumps(view, ensure_ascii=False),
          str(view.get("openai_api_key_masked")))
    check("key 打码带省略号", "\u2026" in view["openai_api_key_masked"])
    check("对外不露 MCP token",
          "tok-secret-value" not in json.dumps(view, ensure_ascii=False))
    check("但告诉你存了 token", view["mcp_servers"][0]["has_token"] is True)

    # 界面传回打码值时不能把真 key 覆盖成垃圾
    settings.update({"openai_api_key": view["openai_api_key_masked"]})
    check("打码值不会覆盖真 key",
          settings.api_key() == "sk-selftest-abcdefghijklmnop", settings.api_key())
    # 不传 mcp_servers 时旧的不能消失
    check("没传的键不动", len(settings.mcp_servers()) == 1)
    # 传了服务列表但 token 留空时沿用旧 token
    settings.update({"mcp_servers": [
        {"id": "s1", "name": "测试", "url": "http://127.0.0.1:1/mcp",
         "token": "", "enabled": True},
    ]})
    check("token 留空则沿用旧的",
          settings.mcp_servers()[0]["token"] == "tok-secret-value")

    settings.update({"openai_api_key": "__clear__", "chat_model": "", "max_tokens": 0})
    check("__clear__ 能清空 key", not settings.current()["openai_api_key"])
    check("留空回退到 .env", settings.max_tokens() > 0)

    check("base_url 不带尾斜杠", not settings.base_url().endswith("/"))
    check(
        "base_url 不带 /chat/completions",
        not settings.base_url().endswith("/chat/completions"),
        settings.base_url(),
    )
    check("工具轮数有上限", settings.max_tool_rounds() <= 30)

    # ---- 工具名洗洗 ----
    # OpenAI 只收 ^[a-zA-Z0-9_-]{1,64}$，中文、点、斜杠都过不去。
    check("中文名被洗掉", _slug("搜索记忆") == "mcp", _slug("搜索记忆"))
    check("点和斜杠变下划线", _slug("latent.search/v2") == "latent_search_v2")
    check("合法名原样保留", _slug("latent_search") == "latent_search")
    check("不会超 64 字", len(_slug("x" * 200)) <= 64)

    # ---- MCP 响应解析（不联网）----
    plain = _parse_body(FakeResponse('{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}',
                                     "application/json"))
    check("能解 JSON 响应", plain.get("result") == {"tools": []})
    sse_body = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"a"}]}}\n\n'
    )
    parsed = _parse_body(FakeResponse(sse_body, "text/event-stream"))
    check("能解 SSE 响应",
          parsed.get("result", {}).get("tools") == [{"name": "a"}], str(parsed))

    registry = ToolRegistry()
    check("没拉过工具时 schemas 为空", registry.tool_schemas() == [])
    check("status 字段齐",
          {"loaded", "tool_count", "tools", "by_server", "errors"}
          <= set(registry.status()))

    # ---- 时钟 ----
    line = clock.clock_line()
    check("时钟带时区", "Asia/Shanghai" in line, line)
    check(
        "时钟带星期",
        any(day in line for day in ("周一", "周二", "周三", "周四", "周五", "周六", "周日")),
        line,
    )
    gap_line = clock.clock_line("2020-01-01T00:00:00+00:00")
    check("隔得久会提一句", "距上次说话" in gap_line, gap_line)
    check("刚说过话就不提", "距上次说话" not in clock.clock_line(clock.now_local().isoformat()))
    check("裸时间戳按 UTC 认", clock.parse_ts("2020-01-01T00:00:00") is not None)
    check("垃圾时间戳不炸", clock.parse_ts("昨天下午") is None)
    check("format_gap 报到分", clock.format_gap(3720) == "1小时2分", clock.format_gap(3720))
    check("整点不带零分", clock.format_gap(7200) == "2小时", clock.format_gap(7200))
    check("超一天报天", clock.format_gap(90000) == "1天1小时", clock.format_gap(90000))

    # ---- messages 组装 ----
    history = [
        {"role": "user", "text": "一", "attachments": []},
        {"role": "assistant", "text": "二", "attachments": []},
        {"role": "user", "text": "三", "attachments": []},
    ]
    built = llm.build_messages(history, "你是助手", "\n\n[现在] 测试")
    check("system 在第一位", built[0]["role"] == "system")
    check(
        "历史顺序对",
        [m["role"] for m in built[1:]] == ["user", "assistant", "user"],
        str(built),
    )
    check("时钟拼在最后一条用户消息", built[-1]["content"].endswith("[现在] 测试"))
    check("时钟没沾到前面那条", "[现在]" not in built[1]["content"])
    check("system 里不带时钟", "[现在]" not in built[0]["content"])
    check("没 system 就不加", llm.build_messages(history, "", "")[0]["role"] == "user")
    check("空文本不占位", len(llm.build_messages(
        [{"role": "user", "text": "", "attachments": []}], "", ""
    )) == 0)

    # ---- 流式 tool_calls 分片累积 ----
    # 上游就是这么发的：name 只在第一片，arguments 一小段一小段拼。
    accumulator = llm._ToolCallAccumulator()
    accumulator.feed([{"index": 0, "id": "call_x",
                       "function": {"name": "latent_search", "arguments": ""}}])
    accumulator.feed([{"index": 0, "function": {"arguments": '{"que'}}])
    accumulator.feed([{"index": 0, "function": {"arguments": 'ry":"柳州"}'}}])
    calls = accumulator.finish()
    check("分片拼成一个调用", len(calls) == 1, str(calls))
    check("name 拼对", calls and calls[0]["name"] == "latent_search")
    check("arguments 拼成完整 JSON",
          calls and json.loads(calls[0]["arguments"]) == {"query": "柳州"},
          calls[0]["arguments"] if calls else "")

    multi = llm._ToolCallAccumulator()
    multi.feed([
        {"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}},
        {"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}},
    ])
    check("多个并行调用不混", [c["name"] for c in multi.finish()] == ["one", "two"])

    nameless = llm._ToolCallAccumulator()
    nameless.feed([{"index": 0, "function": {"arguments": "{}"}}])
    check("没名字的丢掉", nameless.finish() == [])

    # ---- 路由鉴权 ----
    from fastapi.routing import APIRoute

    from server.main import app

    public = {"/health", "/api/auth", "/api/models", "/api/splash"}
    naked = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/")
        and route.path not in public
        and not route.dependencies
    ]
    check("所有 /api/ 路由都要 token", naked == [], str(naked))

    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    expected = {
        "/api/auth", "/api/models", "/api/chat", "/api/sessions",
        "/api/sessions/{session_id}/messages", "/api/sessions/{session_id}/title",
        "/api/sessions/{session_id}/star", "/api/sessions/{session_id}",
        "/api/search", "/api/profile", "/api/profile/memory",
        "/api/memory-summary", "/api/memory-summary/generate",
        "/api/diary", "/api/calendar", "/api/calendar/{date}",
        "/api/upload", "/api/avatars", "/api/thinking-summary",
        "/api/tool-caption", "/api/splash", "/api/warmup",
        "/api/admin/settings", "/api/admin/reload",
        "/api/admin/mcp/refresh", "/api/admin/mcp/probe",
    }
    check("接口都在", expected <= paths, str(sorted(expected - paths)))

    # 控制台页面得真的存在，不然 /dashboard 会 500
    from server.config import ROOT

    check("dashboard.html 在位", (ROOT / "dashboard.html").is_file())

    print()
    if failures:
        print(f"{len(failures)} 项没过：")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        import shutil

        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
