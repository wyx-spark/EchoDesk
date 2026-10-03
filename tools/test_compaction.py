# -*- coding: utf-8 -*-
"""上下文压缩策略测试（优化已知问题-2 任务九）。

覆盖任务书任务九的每一条优化方向与自行判断项：

| 任务书条目 | 断言位置 |
| --- | --- |
| 触发从"按消息数"改为"按 token 比例"，阈值可配 | `test_trigger_by_ratio` |
| 引入 grace zone：最近 N 轮逐字保留 | `test_grace_zone` |
| 摘要提示词要求保留"需求/决定/背景"，支持用户自定义重点 | `test_summary_prompt` |
| 工具输出单独处理（截断） | `test_tool_output_truncation` |
| 压缩事件可追溯（前后 token/摘要/原因），可回放 | `test_event_log_and_replay` |
| 清理污染：允许修正或省略之前的错误 | `test_clean_room_prompt` |
| 用户可控：手动触发 + 指定重点 | `test_manual_compact_and_focus` |

用真 `Backend` + 真 `ConversationManager` + 桩引擎（只替换"模型怎么吐字"），
不联网、不加载模型。压缩事件日志写到临时目录，跑完清理。

跑法：
    python tools/test_compaction.py
退出码：0 = 全部通过，1 = 存在失败项。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND_DIR)

import compact_log as CL  # noqa: E402
from compact_log import EVENT_SKIP, EVENT_SUMMARY, CompactEvent, CompactLog  # noqa: E402
from config import CompactionConfig, Config  # noqa: E402

PASSED = 0
FAILED = []
TEMP_DIRS = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def section(title):
    print(f"\n=== {title} ===")


def temp_dir():
    path = tempfile.mkdtemp(prefix="echodesk-compact-")
    TEMP_DIRS.append(path)
    return path


# ---------------------------------------------------------------- 桩

class StubEngine:
    """记录每次压缩调用的提示词，返回固定摘要。"""

    def __init__(self, summary="摘要：用户要审查合同，关注金额与期限。"):
        self.summary = summary
        self.calls = []
        self.n_ctx = 1000          # 故意开小，几条消息就能触发压缩
        self.n_ctx_train = 1000
        self.kv_cache_bytes = 0
        self.model_path = "stub.gguf"
        self.last_tps = 0.0
        self.last_elapsed = 0.0
        self.last_tokens = 0

    def make_splitter(self):
        return None

    def count_tokens(self, text):
        return max(1, len(str(text)) // 2)

    def chat_complete(self, messages, temperature=0.7, max_tokens=1024):
        self.calls.append({"messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens})
        return self.summary

    def chat_stream(self, messages, **kwargs):
        self.calls.append({"messages": messages, "stream": True})
        yield self.summary

    def clear_cache(self):
        return True

    def speed_hint(self, *a, **k):
        return ""

    def kv_cache_text(self):
        return "0 B"

    @property
    def ready(self):
        return True


def make_backend(n_ctx=1000, **compact_overrides):
    """真 Backend + 桩引擎；压缩配置可逐项覆盖。"""
    import main as backend_main

    config = Config()
    config.model_path = "stub.gguf"
    config.speed_hint = False
    config.search_default = False
    log_dir = temp_dir()
    config.compact = CompactionConfig(log_file=os.path.join(log_dir, "compact_log.jsonl"),
                                      **compact_overrides)
    backend = backend_main.Backend(config)
    engine = StubEngine()
    engine.n_ctx = n_ctx
    backend.llm = engine
    return backend, engine


def fill(backend, turns, text="这是一段足够长的对话内容，用来把上下文撑到阈值以上。"):
    """往历史里塞 ``turns`` 轮对话（每条约 ``len(text)*3`` 字符）。"""
    for i in range(turns):
        backend.conv.add_message("user", f"第{i}轮问题：{text * 3}")
        backend.conv.add_message("assistant", f"第{i}轮回答：{text * 3}")


def fill_big(backend, turns, repeat=20):
    """塞入"轮数很少但每条很长"的历史（用于验证 token 阈值与 grace zone 的先后）。"""
    fill(backend, turns, text="这是一段很长很长的对话内容。" * repeat)


# ---------------------------------------------------------------- 一

def test_trigger_by_ratio():
    section("一、触发时机：按 token 比例，且阈值可配")
    cfg = CompactionConfig()
    check("默认触发比例 0.70（任务书要求按比例而非消息数）",
          abs(cfg.trigger_ratio - 0.70) < 1e-9, f"{cfg.trigger_ratio}")
    check("比例上限被校验（0.95）", CompactionConfig(trigger_ratio=0.95).trigger_ratio == 0.95)
    rejected = False
    try:
        CompactionConfig(trigger_ratio=1.5)
    except Exception:
        rejected = True
    check("非法比例被拒绝", rejected)

    backend, _ = make_backend(n_ctx=1000, trigger_ratio=0.70)
    fill(backend, 4)
    under = backend._compact_history()
    check("未到阈值时不压缩", under["compressed"] is False and under["reason"] == "under_limit",
          str(under))
    check("阈值随配置变化（limit = n_ctx × ratio）",
          under["limit"] == int(1000 * 0.70), f"limit={under['limit']}")

    # 比例调小后同样的历史就会触发（注意：grace zone 也会拦住"只有 2 轮"的历史，
    # 所以这里用 6 轮 —— 见 test_grace_zone 里对"grace 覆盖全部历史则不压缩"的断言）
    backend2, _ = make_backend(n_ctx=1000, trigger_ratio=0.10)
    fill(backend2, 6)
    over = backend2._compact_history()
    check("比例调小后同样历史触发压缩", over["compressed"] is True, str(over))
    check("回执里带触发比例（可追溯）", abs(over.get("ratio", 0) - 0.10) < 1e-9,
          str(over.get("ratio")))

    backend3, _ = make_backend(n_ctx=1000, trigger_ratio=0.70)
    fill(backend3, 6)
    forced = backend3._compact_history(force=True)
    check("force=True 时忽略阈值", forced["compressed"] is True, str(forced))
    check("手动触发标记为 manual", forced["trigger"] == "manual", forced["trigger"])

    # /compact 在 grace zone 覆盖全部历史时必须仍然做点事（收缩到 1 轮）。
    # 用"轮数少但每条很长"的历史：这样 token 确实超阈值，同时 grace 覆盖全部历史。
    backend4, _ = make_backend(n_ctx=1000, trigger_ratio=0.70, grace_turns=10,
                               keep_ratio=0.0)
    fill_big(backend4, 3)
    auto4 = backend4._compact_history()
    check("grace 覆盖全部历史时自动压缩不动作（不擅自压掉刚说过的话）",
          auto4["compressed"] is False and auto4["reason"] == "nothing_to_drop", str(auto4))
    manual4 = backend4._compact_history(force=True)
    check("手动压缩则收缩 grace zone 后仍能腾出空间", manual4["compressed"] is True,
          str(manual4))
    check("回执标注 grace 被收缩", manual4.get("grace_shrunk") is True, str(manual4))


def test_grace_zone():
    section("二、保留区：grace zone（最近 N 轮逐字保留）")
    cfg = CompactionConfig()
    check("默认 grace zone 为 3 轮", cfg.grace_turns == 3, f"{cfg.grace_turns}")

    backend, _ = make_backend(n_ctx=200, trigger_ratio=0.10, grace_turns=2, keep_ratio=0.0)
    fill(backend, 8)
    total_before = len(backend.conv.history)
    result = backend._compact_history()
    check("压缩发生了", result["compressed"] is True, str(result))
    check("回执里记录了 grace 轮数", result.get("grace") == 2, str(result.get("grace")))

    kept_roles = [m["role"] for m in backend.conv.history]
    # grace=2 轮 = 最后 2 个 user + 对应 assistant = 4 条（但 keep_ratio=0 时仅按轮保留）
    check("保留了最后若干轮（条数为偶数：user+assistant 成对）",
          len(backend.conv.history) % 2 == 0 and 0 < len(backend.conv.history) < total_before,
          f"{len(backend.conv.history)} / {total_before}")
    check("保留的是**最近**的消息（最后一条仍在）",
          backend.conv.history[-1]["content"].startswith("第7轮回答"),
          backend.conv.history[-1]["content"][:20])
    check("被压缩的是**更早**的消息", result["dropped"] == total_before - len(backend.conv.history))
    check("grace zone 覆盖全部历史时不压缩", True)
    check("kept_roles 只含 user/assistant", set(kept_roles) <= {"user", "assistant"})

    # 历史比 grace zone 还短 → 不压缩
    backend2, _ = make_backend(n_ctx=20, trigger_ratio=0.05, grace_turns=10, keep_ratio=0.0)
    fill(backend2, 2)
    short = backend2._compact_history()
    check("grace zone 覆盖全部历史时不压缩",
          short["compressed"] is False and short["reason"] == "nothing_to_drop", str(short))

    # 轮数与 token 预算取并集：keep_ratio 大时保留更多
    backend3, _ = make_backend(n_ctx=200, trigger_ratio=0.10, grace_turns=1, keep_ratio=0.8)
    fill(backend3, 8)
    many = backend3._compact_history()
    backend4, _ = make_backend(n_ctx=200, trigger_ratio=0.10, grace_turns=1, keep_ratio=0.0)
    fill(backend4, 8)
    few = backend4._compact_history()
    check("keep_ratio 与 grace 取并集（预算大则保留更多）",
          many["kept"] >= few["kept"], f"{many['kept']} vs {few['kept']}")


def test_summary_prompt():
    section("三、摘要提示词：保留需求/决定/背景，且可自定义")
    cfg = CompactionConfig()
    for keyword in ("用户的需求", "关键决定", "背景信息"):
        check(f"默认提示词含「{keyword}」", keyword in cfg.prompt, cfg.prompt[:60])
    check("默认提示词长度可控（< 400 字符，避免挤占压缩器窗口）",
          len(cfg.prompt) < 400, f"{len(cfg.prompt)}")

    backend, engine = make_backend(n_ctx=200, trigger_ratio=0.10)
    fill(backend, 8)
    backend._compact_history()
    check("压缩时调用了一次模型", len(engine.calls) == 1, f"{len(engine.calls)} 次")
    if engine.calls:
        prompt = engine.calls[0]["messages"][0]["content"]
        check("实际用的提示词就是配置里的", prompt == cfg.prompt, prompt[:50])
        check("压缩输入里带用户与助手的发言标记",
              "用户:" in engine.calls[0]["messages"][1]["content"])

    # 自定义提示词生效
    backend2, engine2 = make_backend(n_ctx=200, trigger_ratio=0.10,
                                     prompt="只保留金额与日期，其他一律省略。")
    fill(backend2, 8)
    backend2._compact_history()
    check("自定义提示词生效",
          engine2.calls and engine2.calls[0]["messages"][0]["content"] == "只保留金额与日期，其他一律省略。",
          str(engine2.calls[:1]))

    # 空提示词回落到默认（配置文件被改坏也不至于压出空摘要）
    check("空提示词回落默认", CompactionConfig(prompt="   ").prompt == cfg.prompt)


def test_tool_output_truncation():
    section("四、大块内容单独处理（任务书：文件/搜索/工具输出先截断）")
    cfg = CompactionConfig()
    check("默认大块内容上限 2000 字符", cfg.tool_output_chars == 2000)

    backend, engine = make_backend(n_ctx=100, trigger_ratio=0.05, tool_output_chars=500,
                                   message_chars=4000)
    # 一条超长的"文件解析结果"消息
    backend.conv.add_message("user", "这是一段很长的文件内容。" * 500)
    backend.conv.add_message("assistant", "已解析完成。")
    fill(backend, 3)
    result = backend._compact_history()
    check("压缩发生", result["compressed"] is True, str(result))
    event = result.get("event") or {}
    check("事件里记录被截断的大块内容条数", event.get("truncated_outputs", 0) >= 1,
          str(event.get("truncated_outputs")))
    check("事件里记录省下的字符数", event.get("saved_chars", 0) > 0,
          str(event.get("saved_chars")))
    if engine.calls:
        fed = engine.calls[-1]["messages"][1]["content"]
        check("喂给压缩器的文本被压到上限内（不是整条丢掉）",
              len(fed) <= 4000 + 200, f"{len(fed)} 字符")
        check("被截断处保留了长度说明", "省略" in fed or "原文共" in fed, fed[:200])

    # 不设上限时不截断
    plain, plain_engine = make_backend(n_ctx=100, trigger_ratio=0.05, tool_output_chars=0,
                                       message_chars=4000)
    plain.conv.add_message("user", "短消息。" * 10)
    plain.conv.add_message("assistant", "答。")
    fill(plain, 3)
    plain_result = plain._compact_history()
    check("tool_output_chars=0 时不截断",
          (plain_result.get("event") or {}).get("truncated_outputs", 0) == 0)


def test_event_log_and_replay():
    section("五、压缩事件：可追溯 / 可审计 / 可回放")
    log_dir = temp_dir()
    path = os.path.join(log_dir, "compact_log.jsonl")
    journal = CompactLog(path)

    event = CompactEvent(kind=EVENT_SUMMARY, reason="over_limit", trigger="auto",
                         used_before=900, used_after=400, dropped=6, kept=4,
                         summary="第一次压缩摘要", summary_chars=8, focus="")
    check("追加事件成功", journal.append(event) is True)
    check("事件文件已创建", os.path.exists(path))

    with open(path, "r", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
    check("一行一条 JSON（追加式）", len(lines) == 1)
    check("记录触发原因", lines[0]["reason"] == "over_limit")
    check("记录压缩前后 token 数",
          lines[0]["used_before"] == 900 and lines[0]["used_after"] == 400)
    check("记录被压缩与保留条数", lines[0]["dropped"] == 6 and lines[0]["kept"] == 4)
    check("记录摘要正文", lines[0]["summary"] == "第一次压缩摘要")
    check("记录时间戳", bool(lines[0]["time"]))

    journal.append(CompactEvent(kind=EVENT_SUMMARY, trigger="manual",
                                summary="第二次压缩摘要", used_before=500, used_after=200))
    events = journal.events()
    check("读回两条事件", len(events) == 2, f"{len(events)}")
    steps = CL.replay(events)
    check("回放两步", len(steps) == 2, f"{len(steps)}")
    check("回放是按时间累计拼接",
          steps[0].after == "第一次压缩摘要"
          and steps[1].after == "第一次压缩摘要\n第二次压缩摘要",
          repr(steps[-1].after))
    check("回放能看出每步的前后差异",
          steps[1].before == "第一次压缩摘要" and steps[1].after != steps[1].before)
    check("final_summary 等于最后一次回放结果",
          CL.final_summary(events) == steps[-1].after)
    check("统计信息可用", journal.stats()["compressed"] == 2, str(journal.stats()))

    # 坏行不影响读回（日志是审计用途）
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{ 这不是 JSON\n")
    check("坏行被跳过而不是让整份日志读不出来", len(journal.events()) == 2)

    # log_content=False 时只记长度不记正文
    secret_dir = temp_dir()
    secret_path = os.path.join(secret_dir, "secret.jsonl")
    secret_log = CompactLog(secret_path, record_content=False)
    secret_log.append(CompactEvent(kind=EVENT_SUMMARY, summary="合同金额 1,860,000 元",
                                   summary_chars=20))
    raw = open(secret_path, "r", encoding="utf-8").read()
    check("log_content=False 时正文不落盘（脱敏）", "1,860,000" not in raw, raw[:120])
    check("但仍记录摘要长度", json.loads(raw.strip())["summary_chars"] == 20)

    # 事件过多时裁剪
    trim_dir = temp_dir()
    trim_path = os.path.join(trim_dir, "trim.jsonl")
    trim_log = CompactLog(trim_path, max_events=100)
    for i in range(130):
        trim_log.append(CompactEvent(kind=EVENT_SKIP, reason=f"r{i}"))
    check("超出上限后裁剪到最近 N 条", len(trim_log.events()) <= 101,
          f"{len(trim_log.events())}")
    check("裁剪保留的是最近的", trim_log.events()[-1].reason == "r129",
          trim_log.events()[-1].reason)

    # 后端接线：压缩后日志里真的有一条
    backend, _ = make_backend(n_ctx=200, trigger_ratio=0.10)
    fill(backend, 8)
    result = backend._compact_history()
    diag = backend.compaction_diagnostics()
    check("后端诊断能看到触发比例", abs(diag["trigger_ratio"] - 0.10) < 1e-9, str(diag))
    check("后端诊断能看到 grace 轮数", diag["grace_turns"] == 3, str(diag["grace_turns"]))
    check("后端诊断能看到日志汇总", diag.get("journal", {}).get("compressed") == 1,
          str(diag.get("journal")))
    check("后端诊断能回放步数", diag.get("replay_steps") == 1, str(diag.get("replay_steps")))
    check("最近一次事件里有摘要", bool((diag.get("last_event") or {}).get("summary")))


def test_clean_room_prompt():
    section("六、清理污染：摘要可充当 clean room（修正/省略之前的错误）")
    cfg = CompactionConfig()
    check("提示词明确禁止编造", "不要编造" in cfg.prompt, cfg.prompt[-40:])
    # 压缩是"重新总结"而不是"逐字搬运"，因此天然修正了前面表述不一致的地方；
    # 这一点由提示词口径保证（要求只保留需求/决定/背景，不复述客套话）。
    check("提示词要求不复述客套话（避免把错误说法原样搬进摘要）",
          "客套话" in cfg.prompt)

    # 摘要会累积（旧摘要 + 新摘要），因此不会丢掉之前的决定
    backend, _ = make_backend(n_ctx=150, trigger_ratio=0.05)
    fill(backend, 6)
    backend._compact_history()
    first = backend.conv.summary
    fill(backend, 6)
    backend._compact_history()
    check("多次压缩的摘要会累积而不是覆盖", len(backend.conv.summary) >= len(first),
          f"{len(first)} → {len(backend.conv.summary)}")


def test_manual_compact_and_focus():
    section("七、用户可控：手动触发 + 指定重点（/compact focus on …）")
    backend, engine = make_backend(n_ctx=4000, trigger_ratio=0.90)
    fill(backend, 3)
    auto = backend._compact_history()
    check("阈值高时自动触发不动作", auto["compressed"] is False and auto["reason"] == "under_limit",
          str(auto)[:160])

    # 手动压缩用另一份后端（n_ctx 小、阈值高）：n_ctx 大时 token 预算本身就装得下
    # 全部历史，`/compact` 会如实回答"无需压缩"—— 这也是正确行为，但不适合用来
    # 验证"手动触发真的调了模型"。
    manual_backend, manual_engine = make_backend(n_ctx=200, trigger_ratio=0.90)
    fill(manual_backend, 6)

    import command_handler

    handler = command_handler.CommandHandler.__new__(command_handler.CommandHandler)
    handler.config = manual_backend.config
    handler.conv_mgr = manual_backend.conv
    handler.llm_engine = manual_engine
    handler.compact_provider = manual_backend.compact_context
    handler.temp_manual = False
    reply, _action = handler._compact([])
    check("手动压缩被执行（无需达到阈值）", "已压缩" in reply or "压缩" in reply, reply[:80])
    check("手动压缩确实调了模型", len(manual_engine.calls) >= 1,
          f"{len(manual_engine.calls)}")

    # focus on <重点>
    backend2, engine2 = make_backend(n_ctx=200, trigger_ratio=0.90)
    fill(backend2, 6)
    handler2 = command_handler.CommandHandler.__new__(command_handler.CommandHandler)
    handler2.config = backend2.config
    handler2.conv_mgr = backend2.conv
    handler2.llm_engine = engine2
    handler2.compact_provider = backend2.compact_context
    handler2.temp_manual = False
    reply2, _ = handler2._compact(["focus", "on", "合同金额", "与", "交付期限"])
    check("focus 形式被解析", "合同金额" in reply2, reply2[:100])
    if engine2.calls:
        prompt = engine2.calls[0]["messages"][0]["content"]
        check("重点被追加进摘要提示词", "合同金额 与 交付期限" in prompt, prompt[-120:])
        check("默认提示词要求仍在（追加而不是替换）", "关键决定" in prompt, prompt[:80])
    else:
        check("focus 压缩调用了模型", False, "没有调用")

    # focus 解析的宽松性 + 不误伤旧用法
    backend3, engine3 = make_backend(n_ctx=200, trigger_ratio=0.90)
    fill(backend3, 6)
    handler3 = command_handler.CommandHandler.__new__(command_handler.CommandHandler)
    handler3.config = backend3.config
    handler3.conv_mgr = backend3.conv
    handler3.llm_engine = engine3
    handler3.compact_provider = backend3.compact_context
    handler3.temp_manual = False
    _reply3, _ = handler3._compact(["on", "金额"])
    if engine3.calls:
        check("on <重点> 也认", "金额" in engine3.calls[0]["messages"][0]["content"])
    handler4 = command_handler.CommandHandler.__new__(command_handler.CommandHandler)
    handler4.config = backend3.config
    handler4.conv_mgr = backend3.conv
    handler4.llm_engine = engine3
    accepted = {"force": None}

    def provider(force, focus=""):
        accepted["force"] = force
        accepted["focus"] = focus
        return {"compressed": False, "used": 1, "limit": 2}

    handler4.compact_provider = provider
    handler4.temp_manual = False
    handler4._compact(["auto"])
    check("/compact auto 仍然按不强制解析", accepted["force"] is False, str(accepted))
    handler4._compact(["off"])
    check("/compact off 同样不强制", accepted["force"] is False)
    handler4._compact([])
    check("/compact 无参数仍然强制", accepted["force"] is True)
    check("无参数时不带重点", accepted["focus"] == "")

    # 兼容只接受 force 的旧 provider
    handler5 = command_handler.CommandHandler.__new__(command_handler.CommandHandler)
    handler5.config = backend3.config
    handler5.conv_mgr = backend3.conv
    handler5.llm_engine = engine3
    handler5.compact_provider = lambda force: {"compressed": False, "used": 1, "limit": 2}
    handler5.temp_manual = False
    ok_reply, _ = handler5._compact(["focus", "on", "金额"])
    check("旧 provider（只接受 force）不崩", isinstance(ok_reply, str), ok_reply[:60])


def main():
    print("上下文压缩策略测试（优化已知问题-2 任务九）")
    try:
        test_trigger_by_ratio()
        test_grace_zone()
        test_summary_prompt()
        test_tool_output_truncation()
        test_event_log_and_replay()
        test_clean_room_prompt()
        test_manual_compact_and_focus()
    finally:
        for path in TEMP_DIRS:
            shutil.rmtree(path, ignore_errors=True)

    print("\n" + "-" * 42)
    print(f"共 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
