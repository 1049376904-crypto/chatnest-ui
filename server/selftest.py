#!/usr/bin/env python3
"""不联网的自检。跑存储层、去重、时钟、messages 组装和路由鉴权。

    python3 -m server.selftest

它不碰中转 API，不花额度。数据写在临时目录，跑完就删。
部署完先跑这个，能把「代码有问题」和「API key 不对」分开。
"""

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="chatnest-selftest-")
# setdefault + load_dotenv 的顺序有讲究：dotenv 默认不覆盖已存在的环境变量，
# 所以这里先占位，就算你的 server/.env 里写了真实密码也不会影响自检。
os.environ.setdefault("CHAT_PASSWORD", "selftest")
os.environ.setdefault("CHAT_SECRET", "selftest-secret")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault("APP_TIMEZONE", "Asia/Shanghai")

from server import auth, clock, llm, profile as profile_store, store  # noqa: E402
from server.dedupe import is_near_duplicate  # noqa: E402

failures: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    ok = bool(condition)
    mark = "OK  " if ok else "FAIL"
    print(f"{mark}  {name}" + (f"  ← {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


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
    }
    check("README 那张表的接口都在", expected <= paths, str(sorted(expected - paths)))

    # ---- 配置规范化 ----
    from server.config import OPENAI_BASE_URL

    check("base_url 不带尾斜杠", not OPENAI_BASE_URL.endswith("/"), OPENAI_BASE_URL)
    check(
        "base_url 不带 /chat/completions",
        not OPENAI_BASE_URL.endswith("/chat/completions"),
        OPENAI_BASE_URL,
    )

    print()
    if failures:
        print(f"{len(failures)} 项没过：")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("全部通过。接下来填 .env 里的 OPENAI_BASE_URL 和 OPENAI_API_KEY。")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        import shutil

        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
