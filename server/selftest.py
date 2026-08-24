#!/usr/bin/env python3
"""不联网的自检。跑存储层、去重、时钟和 messages 组装。

    python3 -m server.selftest

它不碰中转 API，不花额度。数据写在临时目录，跑完就删。
部署完先跑这个，能把“想不到的报错”和“API key 不对”分开。
"""

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="chatnest-selftest-")
os.environ.setdefault("CHAT_PASSWORD", "selftest")
os.environ.setdefault("CHAT_SECRET", "selftest-secret")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault("APP_TIMEZONE", "Asia/Shanghai")

from server import auth, clock, llm, profile as profile_store, store  # noqa: E402
from server.dedupe import is_near_duplicate  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"{mark}  {name}" + (f"  ← {detail}" if detail and not condition else ""))
    if not condition:
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

    # 分页：再塑几条，然后只拿 2 条
    for index in range(3):
        _, uid = store.begin_turn(f"第 {index} 轮", conv_id)
        store.complete_turn(conv_id, f"回答 {index}")
    limited = store.conversation_messages(conv_id, limit=2)
    check("limit=2 只回两条", len(limited["messages"]) == 2)
    check("has_more 为真", limited["has_more"] is True)
    check("next_before_id 有值", limited["next_before_id"] is not None)

    around = store.conversation_messages(conv_id, around_id=assistant_id, limit=4)
    check("around_id 两侧都有", len(around["messages"]) > 1, str(around))

    # 搜索
    results = store.search_messages("第一条")
    check("搜得到", len(results) == 1, str(results))
    check("搜索结果带 message_id", results and results[0]["message_id"] == user_id)
    check("% 不当通配符", store.search_messages("%") == [])

    # 重新生成：砍掉尾巴，再回滚
    before = len(store.conversation_messages(conv_id)["messages"])
    prepared = store.prepare_retry_turn(conv_id, assistant_id)
    check("retry 拿到源用户消息", prepared["user_message_id"] == user_id)
    check(
        "retry 砍掉了尾巴",
        len(store.conversation_messages(conv_id)["messages"]) < before,
    )
    store.restore_branch(prepared["branch_id"])
    check(
        "失败后能回滚",
        len(store.conversation_messages(conv_id)["messages"]) == before,
    )

    # 编辑：只能改用户消息
    try:
        store.prepare_edit_turn(conv_id, assistant_id, "改一下")
        check("不允许编辑回复", False, "竟然没报错")
    except ValueError:
        check("不允许编辑回复", True)

    store.delete_conversation(conv_id)
    check("删会话", store.conversation_list() == [])
    check(
        "删完消息也跟着走（级联）",
        store.search_messages("第一条") == [],
    )

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
    check("拦下时说得出跟哪条重", "杭州" in detail)

    profile = profile_store.read_profile()
    check("profile 里存下来了", len(profile["savedMemories"]) == 1)

    saved = profile_store.write_profile(
        {"nickname": "小猫", "preferences": {"enabled": True, "content": "说人话"}}
    )
    check("profile 写回去", saved["nickname"] == "小猫")
    context = profile_store.build_profile_context()
    check("profile 能拼进上下文", "说人话" in context, context)

    # ---- 日记 / 日历 ----
    profile_store.write_diary_entry("2026-08-24", "今天把后端接上了。")
    check("日记写得进", len(profile_store.read_diary()) == 1)
    check("日记能搜", len(profile_store.read_diary("后端")) == 1)
    check("搜不到就空", profile_store.read_diary("宇宙飞船") == [])
    try:
        profile_store.write_diary_entry("2026/08/24", "格式错的")
        check("日期格式校验", False, "竟然收下了")
    except ValueError:
        check("日期格式校验", True)

    profile_store.write_calendar_day(
        "2026-08-24",
        {"me": {"mood": "平静", "event": "写代码"}, "partner": {"mood": "好"}},
    )
    year = profile_store.calendar_year(2026)
    check("日历按年拼得出来", "2026-08-24" in year["days"], str(year))
    day = profile_store.read_calendar_day("2026-08-24")
    check("单天读得回", day["me"]["mood"] == "平静")
    check("没写过的天返回空壳", profile_store.read_calendar_day("2026-01-01")["me"]["mood"] == "")

    # ---- 长期印象 ----
    profile_store.write_summary(content="他写代码时很急。", running=False)
    summary = profile_store.read_summary()
    check("摘要存得下", summary["content"].startswith("他写代码"))
    check("running 默认 false", summary["running"] is False)

    # ---- 头像 ----
    profile_store.write_avatars({"me": {"url": "/a.png"}, "ai": {"url": "/b.png"}})
    check("头像存取", profile_store.read_avatars()["ai"]["url"] == "/b.png")

    # ---- 时钟 ----
    line = clock.clock_line()
    check("时钟带时区", "Asia/Shanghai" in line, line)
    check("时钟带星期", any(day in line for day in
          ("周一", "周二", "周三", "周四", "周五", "周六", "周日")), line)
    gap_line = clock.clock_line("2020-01-01T00:00:00+00:00")
    check("隔得久会提一句", "距上次说话" in gap_line, gap_line)
    recent = clock.now_local().isoformat()
    check("刚说过话就不提", "距上次说话" not in clock.clock_line(recent))
    check("format_gap 不报秒", clock.format_gap(3720) == "1小时1分"
          or clock.format_gap(3720) == "1小时2分", clock.format_gap(3720))

    # ---- messages 组装 ----
    history = [
        {"role": "user", "text": "一", "attachments": []},
        {"role": "assistant", "text": "二", "attachments": []},
        {"role": "user", "text": "三", "attachments": []},
    ]
    built = llm.build_messages(history, "你是助手", "\n\n[现在] 测试")
    check("system 在第一位", built[0]["role"] == "system")
    check("历史顺序对", [m["role"] for m in built[1:]] ==
          ["user", "assistant", "user"], str(built))
    check("时钟只拼在最后一条用户消息", built[-1]["content"].endswith("[现在] 测试"))
    check("时钟没沾到前面那条", "[现在]" not in built[1]["content"])
    check("system 里不带时钟", "[现在]" not in built[0]["content"])

    no_system = llm.build_messages(history, "", "")
    check("没 system 就不加", no_system[0]["role"] == "user")

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
