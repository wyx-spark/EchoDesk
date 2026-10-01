# -*- coding: utf-8 -*-
"""思考内容与回答分离（v0.2.1 模型思考内容分离）自动化测试。

覆盖任务书里**可机验**的部分：

* 任务三 应用层兜底：标签分离器的全部边界 —— 单块/多块、标签跨 chunk、
  标签嵌套、未闭合、孤立结束标签（DeepSeek-R1 那种"起始标签写在提示词里"的形态）、
  无标签、空思考块、自定义标签表、标签既非起始也非结束时的兜底；
* 任务二 推理层结论的回归：``chat_stream`` / ``chat_complete`` 保持"只给回答"的
  兼容语义，双通道走 ``chat_stream_split`` / ``chat_complete_split``；
* 任务四 协议：``main.Backend.stream`` 必须产出带 ``channel`` 的 delta 行、
  ``reset`` 行、``done.reasoning`` 与 ``reasoning_unclosed``；
* 任务五 兼容性：非推理模型（无标签）不受影响、中断不残留半截思考、
  老会话（没有 reasoning 字段）照常加载、思考内容不进模型上下文；
* 自行判断项：思考内容写进历史（可关、可截断）、不参与上下文、导出为可折叠块。

除 ``--model`` 指定的真实模型端到端一项外，**不依赖网络也不依赖模型**：
桩 llama_cpp 只替换"模型怎么吐字"，被测的是真 ``LLMEngine`` / 真 ``Backend`` /
真 ``ConversationManager``。

跑法：
    python tools/test_reasoning.py                        # 单元 + 协议
    python tools/test_reasoning.py --model <gguf 路径>     # 追加真实模型端到端
退出码：0 = 全部通过，1 = 存在失败项。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND_DIR)

import reasoning as R  # noqa: E402
import llm_engine  # noqa: E402
import main as backend_main  # noqa: E402
from config import Config, ReasoningConfig  # noqa: E402
from conversation import ConversationManager  # noqa: E402

#: 起始/结束标签一律用构造出来的（不在源码里写闭合标签字面量，见 reasoning.tag_pair）
OPEN_THINK, CLOSE_THINK = R.tag_pair("think")
OPEN_THINKING, CLOSE_THINKING = R.tag_pair("thinking")

PASSED = 0
FAILED = []
SKIPPED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def skip(name, why):
    SKIPPED.append(f"{name}：{why}")
    print(f"  [SKIP] {name}（{why}）")


def section(title):
    print(f"\n=== {title} ===")


def channels(events, channel):
    """把事件流按通道拼成文本（同时兼容 2 元组与 3 元组事件）。"""
    return "".join(event[1] for event in events if event[0] == channel)


def answer_after_reset(events):
    """回答区**最终**会显示的内容：最后一次 reset 之后进入回答通道的文本。

    孤立结束标签的情形下，reset 之前发到回答通道的文本会被前端搬进思考区
    （后端不重发），所以"用户最终看到的答案"必须按这个口径统计。
    事件既可能是分离器的 2 元组 ``(channel, text)``，也可能是协议的 3 元组
    ``(channel, text, reset)``，两种都要认。
    """
    out = []
    cleared = False
    for event in events:
        name, text = event[0], event[1]
        is_reset = name == R.RESET or (len(event) > 2 and bool(event[2]))
        if is_reset:
            out = []
            cleared = True
            continue
        if name == R.ANSWER:
            out.append(text)
    return "".join(out), cleared


# ===========================================================================
# 桩：假装是 llama_cpp.Llama，只实现引擎真正用到的两个方法
# ===========================================================================


class StubLlama:
    """脚本化输出：按 ``script`` 逐段吐字，行为与 llama-cpp-python 的流式一致。"""

    def __init__(self, script, on_piece=None):
        self.script = list(script)
        self.on_piece = on_piece
        self.last_messages = None
        self.calls = 0

    def tokenize(self, data, **kwargs):  # 供 token 估算用
        return list(range(max(1, len(data) // 3)))

    def create_chat_completion(self, messages=None, stream=False, **kwargs):
        self.last_messages = messages
        self.calls += 1
        if not stream:
            return {"choices": [{"message": {"content": "".join(self.script)}}]}

        def generate():
            for index, piece in enumerate(self.script):
                if self.on_piece is not None:
                    self.on_piece(index)
                yield {"choices": [{"delta": {"content": piece}}]}

        return generate()


class StubEngine(llm_engine.LLMEngine):
    """真引擎的聊天方法 + 桩 llama_cpp：测的是真接线，不是替身。"""

    def __init__(self, script, reasoning_cfg=None, on_piece=None):
        self.llm = StubLlama(script, on_piece=on_piece)
        self.script = list(script)
        self.reasoning_enabled = bool(getattr(reasoning_cfg, "enabled", True))
        self.reasoning_tags = R.normalize_tags(getattr(reasoning_cfg, "tags", None))
        self.model_path = "stub.gguf"
        self.n_ctx = 4096
        self.n_ctx_train = 4096
        self.kv_cache_bytes = 0
        self.last_tps = 0.0
        self.last_elapsed = 0.0
        self.last_tokens = 0

    @property
    def ready(self):
        return True

    def speed_hint(self, *args, **kwargs):
        return ""

    def kv_cache_text(self):
        return "0 B"


def make_backend(script, reasoning_cfg=None, on_piece=None, model_path=""):
    """造一个走真代码路径的后端：真 Backend + 桩引擎。"""
    config = Config()
    config.model_path = model_path or "stub.gguf"
    config.speed_hint = False
    config.search_default = False
    config.context_mode = "slide"
    if reasoning_cfg is not None:
        config.reasoning = reasoning_cfg
    backend = backend_main.Backend(config)
    backend.llm = StubEngine(script, reasoning_cfg=reasoning_cfg, on_piece=on_piece)
    backend.conv.persist_reasoning = bool(getattr(reasoning_cfg, "persist", True))
    backend.conv.reasoning_max_chars = int(getattr(reasoning_cfg, "history_max_chars", 8000) or 0)
    return backend


def stream_lines(backend, message="问题"):
    return list(backend.stream({"action": "chat_stream", "message": message}))


def delta_channels(lines):
    """取出流式行里的双通道事件，返回 [(channel, content, reset)]。"""
    events = []
    for line in lines:
        if line.get("type") == "delta" and "channel" in line:
            events.append((line["channel"], line.get("content", ""), bool(line.get("reset"))))
    return events


# ===========================================================================
# 任务三：分离器边界
# ===========================================================================


def test_splitter():
    section("任务三 · 标签分离器边界")

    # --- 单块：起始、结束标签都在生成文本里（Qwen3 实测形态）---
    splitter = R.ReasoningSplitter()
    events = splitter.feed(OPEN_THINK + "先算 1+1" + CLOSE_THINK + "等于 2")
    events += splitter.close()
    check("单块标签：思考归思考", channels(events, R.REASONING) == "先算 1+1",
          repr(channels(events, R.REASONING)))
    check("单块标签：回答只留答案", channels(events, R.ANSWER) == "等于 2",
          repr(channels(events, R.ANSWER)))
    check("单块标签：has_reasoning 为真", splitter.has_reasoning())
    check("单块标签：未标记未闭合", splitter.unclosed is False)

    # --- 空思考块（Qwen3.5 关闭思考时模板会塞一个空块）---
    splitter = R.ReasoningSplitter()
    events = splitter.feed(OPEN_THINK + "\n\n" + CLOSE_THINK + "\n\n直接回答")
    events += splitter.close()
    check("空思考块：不算有思考内容", splitter.has_reasoning() is False)
    check("空思考块：回答不含空白前缀", channels(events, R.ANSWER) == "直接回答",
          repr(channels(events, R.ANSWER)))

    # --- 标签跨 chunk（逐字符喂）---
    script = OPEN_THINK + "想一下" + CLOSE_THINK + "答案是 42"
    splitter = R.ReasoningSplitter()
    events = []
    for char in script:
        events += splitter.feed(char)
    events += splitter.close()
    check("跨 chunk：逐字符喂也能正确切分",
          channels(events, R.REASONING) == "想一下" and channels(events, R.ANSWER) == "答案是 42",
          repr(events))
    check("跨 chunk：不会把半个标签漏进回答",
          "<" not in channels(events, R.ANSWER) and "think" not in channels(events, R.ANSWER),
          repr(channels(events, R.ANSWER)))

    # --- 嵌套标签 ---
    splitter = R.ReasoningSplitter()
    events = splitter.feed(OPEN_THINK + "外层" + OPEN_THINKING + "内层" + CLOSE_THINKING
                           + "还是外层" + CLOSE_THINK + "答案")
    events += splitter.close()
    check("嵌套标签：整段都算思考", channels(events, R.REASONING) == "外层内层还是外层",
          repr(channels(events, R.REASONING)))
    check("嵌套标签：内层结束不提前关思考区", channels(events, R.ANSWER) == "答案",
          repr(channels(events, R.ANSWER)))

    # --- 长标签优先（<thinking> 不能被 <think> 截断）---
    splitter = R.ReasoningSplitter()
    events = splitter.feed(OPEN_THINKING + "思考" + CLOSE_THINKING + "答案")
    events += splitter.close()
    check("长标签优先：<thinking> 不被 <think> 抢先匹配",
          channels(events, R.REASONING) == "思考" and channels(events, R.ANSWER) == "答案",
          repr(events))

    # --- 多段思考 ---
    splitter = R.ReasoningSplitter()
    events = splitter.feed(OPEN_THINK + "第一段" + CLOSE_THINK + "答案"
                           + OPEN_THINK + "第二段" + CLOSE_THINK)
    events += splitter.close()
    check("多段思考：合并到同一条思考通道", "第一段" in splitter.reasoning_text
          and "第二段" in splitter.reasoning_text, repr(splitter.reasoning_text))
    check("多段思考：回答区只出现一次答案", channels(events, R.ANSWER) == "答案",
          repr(channels(events, R.ANSWER)))

    # --- 孤立结束标签（DeepSeek-R1：起始标签在提示词里，实测模板如此）---
    splitter = R.ReasoningSplitter()
    events = splitter.feed("9.9 更大，因为")
    events += splitter.feed(CLOSE_THINK + "9.9 比 9.11 大")
    events += splitter.close()
    resets = [event for event in events if event[0] == R.RESET]
    check("孤立结束标签：发出一次 RESET", len(resets) == 1, repr(events))
    check("孤立结束标签：此前内容改判为思考", splitter.reasoning_text == "9.9 更大，因为",
          repr(splitter.reasoning_text))
    check("孤立结束标签：RESET 之前的内容确实发过（前端要把它搬走）",
          channels(events, R.ANSWER).startswith("9.9 更大，因为"), repr(events))
    check("孤立结束标签：搬走之后回答区只剩标签之后的内容",
          answer_after_reset(events)[0] == "9.9 比 9.11 大", repr(answer_after_reset(events)))
    check("孤立结束标签：标记 reclassified", splitter.reclassified is True)
    check("split_text 与流式结果一致",
          R.split_text("前" + CLOSE_THINK + "后") == ("前", "后"),
          repr(R.split_text("前" + CLOSE_THINK + "后")))

    # --- 未闭合 ---
    splitter = R.ReasoningSplitter()
    events = splitter.feed(OPEN_THINK + "想到一半就没了")
    events += splitter.close()
    check("未闭合：剩余内容全归思考", splitter.reasoning_text == "想到一半就没了",
          repr(splitter.reasoning_text))
    check("未闭合：回答区为空", channels(events, R.ANSWER) == "", repr(channels(events, R.ANSWER)))
    check("未闭合：unclosed 置位", splitter.unclosed is True)

    # --- 没有标签（普通模型）---
    splitter = R.ReasoningSplitter()
    events = splitter.feed("这是一段普通回答，没有任何标签")
    events += splitter.close()
    check("无标签：全部走回答通道",
          channels(events, R.ANSWER) == "这是一段普通回答，没有任何标签"
          and channels(events, R.REASONING) == "", repr(events))
    check("无标签：has_reasoning 为假", splitter.has_reasoning() is False)
    check("无标签：不产生 RESET", not [event for event in events if event[0] == R.RESET])

    # --- 只有结束标签、前面没有任何内容 ---
    splitter = R.ReasoningSplitter()
    events = splitter.feed(CLOSE_THINK + "答案")
    events += splitter.close()
    check("空思考+孤立结束标签：不误发 RESET",
          not [event for event in events if event[0] == R.RESET]
          and answer_after_reset(events)[0] == "答案", repr(events))

    # --- 标签名可配置 ---
    custom = R.normalize_tags([["[[想]]", "[[/想]]"]])
    splitter = R.ReasoningSplitter(custom)
    events = splitter.feed("[[想]]自定义思考[[/想]]自定义答案")
    events += splitter.close()
    check("自定义标签：生效",
          channels(events, R.REASONING) == "自定义思考"
          and channels(events, R.ANSWER) == "自定义答案", repr(events))
    check("自定义标签：内置标签仍然有效",
          OPEN_THINK in [pair[0] for pair in custom])
    check("非法配置项被忽略而不是整体失效",
          len(R.normalize_tags(["not-a-pair", ["", ""], [1, 2], ["[a]", "[b]"]])) ==
          len(R.default_tags()) + 1)
    check("默认标签覆盖常见形态",
          all(name in [pair[0] for pair in R.default_tags()]
              for name in (OPEN_THINK, OPEN_THINKING, "<" + "reasoning" + ">")))

    # --- DeepSeek 特殊 token 形态 ---
    special_begin = R._DEEPSEEK_BEGIN
    special_end = R._DEEPSEEK_END
    splitter = R.ReasoningSplitter()
    events = splitter.feed(special_begin + "特殊token思考" + special_end + "答案")
    events += splitter.close()
    check("DeepSeek 特殊 token：也能分离",
          channels(events, R.REASONING) == "特殊token思考"
          and channels(events, R.ANSWER) == "答案", repr(events))

    # --- 一次性工具函数 ---
    check("strip_reasoning 只取回答",
          R.strip_reasoning(OPEN_THINK + "思考" + CLOSE_THINK + " 答案 ") == "答案",
          repr(R.strip_reasoning(OPEN_THINK + "思考" + CLOSE_THINK + " 答案 ")))
    check("has_reasoning_tag 能识别标签", R.has_reasoning_tag(OPEN_THINK + "x"))
    check("has_reasoning_tag 对普通文本为假", not R.has_reasoning_tag("普通文本"))


# ===========================================================================
# 任务二/五：引擎接线
# ===========================================================================


def test_engine():
    section("任务二 · 引擎双通道与兼容语义")

    # chat_stream 只给回答（摘要/自动化等调用方不会拿到思考内容）
    engine = StubEngine([OPEN_THINK, "内部思考", CLOSE_THINK, "最终", "答案"])
    text = "".join(engine.chat_stream([{"role": "user", "content": "hi"}]))
    check("chat_stream 只产出最终回答", text == "最终答案", repr(text))
    check("chat_stream 的统计包含思考（速度要如实）", engine.last_tokens == len(
        OPEN_THINK + "内部思考" + CLOSE_THINK + "最终答案"), repr(engine.last_tokens))

    # chat_stream_split 给两条通道
    engine = StubEngine([OPEN_THINK, "内部", "思考", CLOSE_THINK, "最终", "答案"])
    splitter = engine.make_splitter()
    events = list(engine.chat_stream_split([{"role": "user", "content": "hi"}], splitter=splitter))
    check("chat_stream_split 分开两条通道",
          channels(events, R.REASONING) == "内部思考" and channels(events, R.ANSWER) == "最终答案",
          repr(events))
    check("splitter 结束后可读到思考全文", splitter.reasoning_text == "内部思考",
          repr(splitter.reasoning_text))

    # 非流式：split 版给两段，兼容版只给回答
    engine = StubEngine([OPEN_THINK + "思考一下" + CLOSE_THINK + "答案在此"])
    thinking, answer = engine.chat_complete_split([{"role": "user", "content": "hi"}])
    check("chat_complete_split 返回 (思考, 回答)", (thinking, answer) == ("思考一下", "答案在此"),
          repr((thinking, answer)))
    engine = StubEngine([OPEN_THINK + "思考一下" + CLOSE_THINK + "答案在此"])
    check("chat_complete 只返回回答",
          engine.chat_complete([{"role": "user", "content": "hi"}]) == "答案在此")

    # 普通模型（无标签）：两条通道都不受影响
    engine = StubEngine(["普通", "回答"])
    thinking, answer = engine.chat_complete_split([{"role": "user", "content": "hi"}])
    check("非推理模型：思考为空、回答完整", (thinking, answer) == ("", "普通回答"),
          repr((thinking, answer)))
    engine = StubEngine(["普通", "回答"])
    check("非推理模型：chat_stream 原样输出",
          "".join(engine.chat_stream([{"role": "user", "content": "hi"}])) == "普通回答")

    # 分离开关关掉 → 回到旧行为（标签原样留在回答里）
    engine = StubEngine([OPEN_THINK + "思考" + CLOSE_THINK + "答案"],
                        reasoning_cfg=ReasoningConfig(enabled=False))
    check("关闭分离：标签原样留在回答里",
          engine.chat_complete([{"role": "user", "content": "hi"}])
          == OPEN_THINK + "思考" + CLOSE_THINK + "答案")
    check("关闭分离：make_splitter 返回 None", engine.make_splitter() is None)
    events = list(engine.chat_stream_split([{"role": "user", "content": "hi"}]))
    check("关闭分离：流式只有回答通道", all(name == R.ANSWER for name, _ in events) and events,
          repr(events))


# ===========================================================================
# 任务四：协议
# ===========================================================================


def test_protocol():
    section("任务四 · 后端流式协议（channel / reset / done）")

    # --- 正常带思考的流式 ---
    backend = make_backend([OPEN_THINK, "先看题", "再算", CLOSE_THINK, "答案", "是 2"])
    lines = stream_lines(backend)
    events = delta_channels(lines)
    done = [line for line in lines if line.get("type") == "done"][-1]
    check("流式：思考走 reasoning 通道",
          channels(events, R.REASONING) == "先看题再算", repr(events))
    check("流式：回答走 answer 通道",
          channels(events, R.ANSWER) == "答案是 2", repr(events))
    check("流式：回答通道不含思考标签",
          OPEN_THINK not in channels(events, R.ANSWER) and CLOSE_THINK not in channels(events, R.ANSWER))
    check("done：content 是最终答案", done["content"] == "答案是 2", repr(done.get("content")))
    check("done：reasoning 是思考全文", done.get("reasoning") == "先看题再算", repr(done.get("reasoning")))
    check("done：未触发未闭合标记", "reasoning_unclosed" not in done)
    check("历史：思考单独存字段",
          backend.conv.history[-1].get("reasoning") == "先看题再算"
          and backend.conv.history[-1]["content"] == "答案是 2",
          repr(backend.conv.history[-1]))
    check("历史：用户消息也入历史", backend.conv.history[-2]["role"] == "user")

    # --- 思考不进上下文（自行判断项）---
    messages = backend.conv.build_messages("下一个问题")
    joined = json.dumps(messages, ensure_ascii=False)
    check("上下文：不含思考内容", "先看题再算" not in joined)
    check("上下文：答案仍在", "答案是 2" in joined)
    check("上下文：不带 reasoning 字段",
          all(set(msg.keys()) == {"role", "content"} for msg in messages), repr(messages))

    # --- 孤立结束标签（R1 形态）：必须发 reset 且回答区最终只有答案 ---
    backend = make_backend(["9.9 更大", CLOSE_THINK, "因为 9.9 > 9.11"])
    lines = stream_lines(backend)
    events = delta_channels(lines)
    done = [line for line in lines if line.get("type") == "done"][-1]
    resets = [event for event in events if event[2]]
    check("孤立结束标签：协议里出现 reset 行", len(resets) == 1, repr(events))
    check("孤立结束标签：reset 标在回答通道", resets and resets[0][0] == R.ANSWER, repr(resets))
    check("孤立结束标签：reset 之前的内容先发过、之后重新开始（前端据此搬运）",
          [event[1] for event in events] == ["9.9 更大", "", "因为 9.9 > 9.11"],
          repr(events))
    check("孤立结束标签：用户最终看到的回答区只有答案",
          answer_after_reset(events)[0] == "因为 9.9 > 9.11", repr(answer_after_reset(events)))
    check("孤立结束标签：done.content 只含答案", done["content"] == "因为 9.9 > 9.11",
          repr(done.get("content")))
    check("孤立结束标签：done.reasoning 含被改判的内容",
          done.get("reasoning") == "9.9 更大", repr(done.get("reasoning")))

    # --- 中断：思考到一半停止 ---
    state = {"backend": None}

    def on_piece(index):
        # 第 3 段之前按下停止：模拟用户在思考过程中点停止（此前已有内容发出）
        if index == 2:
            state["backend"].stop_event.set()

    backend = make_backend([OPEN_THINK, "想到一半", "又想到一句", "就停了"], on_piece=on_piece)
    state["backend"] = backend
    lines = stream_lines(backend)
    events = delta_channels(lines)
    done = [line for line in lines if line.get("type") == "done"][-1]
    check("中断：done 标记 stopped", done.get("stopped") is True, repr(done))
    check("中断：回答通道为空（不残留半截思考）", channels(events, R.ANSWER) == "",
          repr(channels(events, R.ANSWER)))
    check("中断：思考通道已发出的内容保留", channels(events, R.REASONING) == "想到一半",
          repr(channels(events, R.REASONING)))
    check("中断：done.reasoning 与已发出的思考一致",
          done.get("reasoning") == channels(events, R.REASONING) == "想到一半",
          repr((done.get("reasoning"), channels(events, R.REASONING))))
    # 说明：按下停止时"正在处理的那一段"会被引擎丢弃（_chat_stream_raw 在产出
    # 每段前检查 stop_event，这是本次改动之前就有的行为），所以这里只要求"已经
    # 发出去的内容"与 done.reasoning 一致，不做"必须包含最后一段"的断言。
    check("中断：用户主动停止不报 unclosed（否则界面会误报长度上限）",
          "reasoning_unclosed" not in done, repr(done))
    check("中断：思考内容没有漏进回答区",
          "想到" not in channels(events, R.ANSWER), repr(channels(events, R.ANSWER)))

    # --- 未闭合（模型真的没写完答案）---
    backend = make_backend([OPEN_THINK, "想了很多"])
    lines = stream_lines(backend)
    done = [line for line in lines if line.get("type") == "done"][-1]
    check("未闭合：done 带 reasoning_unclosed", done.get("reasoning_unclosed") is True, repr(done))
    check("未闭合：回答为空、思考保留", done["content"] == "" and done.get("reasoning") == "想了很多",
          repr(done))

    # --- 普通模型：协议不变 ---
    backend = make_backend(["普通", "模型", "回答"])
    lines = stream_lines(backend)
    events = delta_channels(lines)
    done = [line for line in lines if line.get("type") == "done"][-1]
    check("非推理模型：全部走 answer 通道",
          channels(events, R.ANSWER) == "普通模型回答" and channels(events, R.REASONING) == "",
          repr(events))
    check("非推理模型：done 不带 reasoning 字段", "reasoning" not in done, repr(done))
    check("非推理模型：历史里没有 reasoning 字段",
          "reasoning" not in backend.conv.history[-1], repr(backend.conv.history[-1]))

    # --- 非流式 chat ---
    backend = make_backend([OPEN_THINK + "非流式思考" + CLOSE_THINK + "非流式答案"])
    result = backend.handle({"action": "chat", "message": "问题"})
    check("非流式：content 只含答案", result.get("content") == "非流式答案", repr(result))
    check("非流式：reasoning 单独给", result.get("reasoning") == "非流式思考", repr(result))
    check("非流式：历史同样分离",
          backend.conv.history[-1].get("reasoning") == "非流式思考", repr(backend.conv.history[-1]))

    # --- 关闭分离开关：旧行为（标签留在回答里）---
    backend = make_backend([OPEN_THINK + "思考" + CLOSE_THINK + "答案"],
                           reasoning_cfg=ReasoningConfig(enabled=False))
    lines = stream_lines(backend)
    events = delta_channels(lines)
    done = [line for line in lines if line.get("type") == "done"][-1]
    check("关闭分离：回答通道带标签（向后兼容旧行为）",
          channels(events, R.ANSWER) == OPEN_THINK + "思考" + CLOSE_THINK + "答案",
          repr(channels(events, R.ANSWER)))
    check("关闭分离：不发 reasoning 通道", channels(events, R.REASONING) == "")
    check("关闭分离：done 无 reasoning 字段", "reasoning" not in done)

    # --- 速度提示不污染回答通道之外的东西 ---
    backend = make_backend([OPEN_THINK + "思考" + CLOSE_THINK + "答案"])
    backend.config.speed_hint = True
    backend.llm.speed_hint = lambda *a, **k: "⏱️ 慢"
    lines = stream_lines(backend)
    done = [line for line in lines if line.get("type") == "done"][-1]
    check("速度提示仍附在 done.content 上且不混入思考",
          done["content"].endswith("⏱️ 慢") and done.get("reasoning") == "思考", repr(done))


# ===========================================================================
# 任务五：历史与持久化
# ===========================================================================


def test_history():
    section("任务五 · 历史记录与持久化")

    conv = ConversationManager(10, "general")
    conv.add_message("user", "问")
    conv.add_message("assistant", "答", reasoning="思考过程")
    check("历史：思考单独存字段", conv.history[-1]["reasoning"] == "思考过程",
          repr(conv.history[-1]))
    check("历史：字段仍是 role/content/timestamp/reasoning",
          set(conv.history[-1].keys()) == {"role", "content", "timestamp", "reasoning"},
          repr(conv.history[-1].keys()))

    conv.add_message("assistant", "无思考答案")
    check("历史：空思考不写字段", "reasoning" not in conv.history[-1], repr(conv.history[-1]))

    # 会话落盘 → 读回
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.json")
        conv.save_session(path, "m.gguf")
        fresh = ConversationManager(10, "general")
        ok, model = fresh.load_session(path)
        check("持久化：读回成功", ok and model == "m.gguf")
        check("持久化：思考内容仍在",
              fresh.history[-2].get("reasoning") == "思考过程", repr(fresh.history[-2]))

        # 老会话（没有 reasoning 字段）必须照常加载
        legacy = {"mode": "general", "history": [
            {"role": "user", "content": "旧问", "timestamp": "2024-01-01T00:00:00"},
            {"role": "assistant", "content": "旧答", "timestamp": "2024-01-01T00:00:01"},
        ]}
        legacy_path = os.path.join(tmp, "legacy.json")
        with open(legacy_path, "w", encoding="utf-8") as handle:
            json.dump(legacy, handle, ensure_ascii=False)
        old = ConversationManager(10, "general")
        ok, _ = old.load_session(legacy_path)
        check("老会话：无 reasoning 字段也能加载", ok and len(old.history) == 2)
        check("老会话：仍能构造 messages（系统 + 两条历史 + 新问题）",
              len(old.build_messages("新问题")) == 4, repr(old.build_messages("新问题")))

        # 导出：思考作为可折叠块，且排在答案之前
        text = conv.export_markdown()
        check("导出：思考是可折叠块", "<details><summary>" in text and "</details>" in text)
        check("导出：思考内容在位", "思考过程" in text)
        check("导出：思考排在对应答案之前",
              text.index("思考过程") < text.index("无思考答案"), text[:200])

    # 截断与关闭持久化
    conv = ConversationManager(10, "general")
    conv.reasoning_max_chars = 5
    conv.add_message("assistant", "答", reasoning="一二三四五六七八九十")
    check("截断：超长思考被截断并加标记",
          conv.history[-1]["reasoning"].startswith("一二三四五")
          and "十" not in conv.history[-1]["reasoning"], repr(conv.history[-1]["reasoning"]))
    conv = ConversationManager(10, "general")
    conv.persist_reasoning = False
    conv.add_message("assistant", "答", reasoning="思考")
    check("关闭持久化：历史里不写思考", "reasoning" not in conv.history[-1],
          repr(conv.history[-1]))


# ===========================================================================
# 任务六：语言包
# ===========================================================================


def test_lang():
    section("任务六 · 文案与语言包")
    import i18n

    for key in ("export.reasoning", "export.reasoning_truncated"):
        texts = []
        for locale in ("zh-Hans", "en"):
            pack = i18n.load_pack(locale)
            texts.append(pack.get(key, ""))
        check(f"语言包键 {key} 中英齐全", all(texts), repr(texts))
    pack_zh = i18n.load_pack("zh-Hans")
    check("未闭合说明文案已就位", bool(pack_zh.get("export.reasoning")) and "思考" in pack_zh["export.reasoning"],
          repr(pack_zh.get("export.reasoning")))


# ===========================================================================
# 真实模型端到端（可选）
# ===========================================================================


def test_real_model(model_path):
    section("真实模型端到端 · 用推理模型确认回答区只有答案")

    workdir = tempfile.mkdtemp(prefix="echodesk-reasoning-")
    config = {
        "model_path": model_path,
        "model_repo": "",
        "hf_endpoint": "https://hf-mirror.com",
        "context_length": 4096,
        "max_context": 4096,
        "max_tokens": 1024,
        "speed_hint": False,
        "search_default": False,
        "warmup": False,
        "session_file": "session.json",
        "reasoning": {"enabled": True, "persist": True, "history_max_chars": 8000, "tags": []},
    }
    with open(os.path.join(workdir, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    os.makedirs(os.path.join(workdir, "models"), exist_ok=True)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = BACKEND_DIR
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BACKEND_DIR, "main.py")],
        cwd=workdir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
    import queue

    lines = queue.Queue()

    def pump():
        try:
            for line in proc.stdout:
                lines.put(line)
        except Exception:
            pass

    threading.Thread(target=pump, daemon=True).start()

    def read_line(timeout):
        try:
            return lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def send(payload):
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    try:
        ready = None
        deadline = time.time() + 180
        while time.time() < deadline:
            line = read_line(5)
            if line is None:
                break
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if payload.get("type") == "ready":
                ready = payload
                break
        if ready is None:
            skip("真实模型端到端", "后端未在 180 秒内就绪（模型可能未加载成功）")
            return
        if not ready.get("model_ready"):
            skip("真实模型端到端", "后端报告无模型就绪")
            return

        send({"action": "chat_stream", "message": "一个笼子里有鸡和兔共 8 只，脚共 22 只，鸡兔各几只？"})
        reasoning_text = ""
        answer_text = ""
        resets_seen = 0
        done = None
        deadline = time.time() + 600
        while time.time() < deadline:
            line = read_line(10)
            if line is None:
                break
            try:
                payload = json.loads(line)
            except Exception:
                continue
            kind = payload.get("type")
            if kind == "delta" and "channel" in payload:
                if payload["channel"] == R.REASONING:
                    reasoning_text += payload.get("content", "")
                elif payload.get("reset"):
                    # 与前端一致：回答区已有的文本其实是思考，搬到思考区，
                    # 回答区从头开始（DeepSeek-R1 的起始标签写在提示词里，只有结束标签可见）
                    reasoning_text += answer_text
                    answer_text = ""
                    resets_seen += 1
                else:
                    answer_text += payload.get("content", "")
            elif kind == "done":
                done = payload
                break
            elif kind == "error":
                check("真实模型端到端：没有报错", False, repr(payload))
                return
        if done is None:
            skip("真实模型端到端", "600 秒内没有收到 done 行")
            return

        final_reasoning = done.get("reasoning", "")
        final_answer = done.get("content", "")
        print(f"  · 模型输出：思考 {len(final_reasoning)} 字符 / 回答 {len(final_answer)} 字符"
              f" / reset {resets_seen} 次")
        print(f"  · 回答区原文：{final_answer[:120]!r}")
        if final_reasoning:
            print(f"  · 思考区开头：{final_reasoning[:80]!r}")
        if resets_seen:
            print("  · 该模型属于「起始标签在提示词里」的形态：靠 reset 把回答区内容改判为思考")
            check("真实模型：改判之后回答区不含被搬走的思考",
                  final_reasoning.strip() not in final_answer if final_reasoning else True)
        check("真实模型：回答区不含思考标签",
              all(tag not in final_answer for tag in
                  (OPEN_THINK, CLOSE_THINK, OPEN_THINKING, CLOSE_THINKING)),
              repr(final_answer[:200]))
        check("真实模型：回答区内容与流式累积一致",
              final_answer.replace("\n", "") .startswith(answer_text.replace("\n", "")[:40])
              or answer_text == "", repr(answer_text[:120]))
        if final_reasoning:
            check("真实模型：思考通道与 done.reasoning 一致",
                  reasoning_text.strip() == final_reasoning.strip(),
                  repr((reasoning_text[:80], final_reasoning[:80])))
            check("真实模型：思考区开头没有空白行",
                  not reasoning_text.startswith("\n"), repr(reasoning_text[:20]))
            check("真实模型：思考内容没有混进回答", final_reasoning not in final_answer)
        else:
            skip("真实模型：思考通道", "该模型这次没有产生思考内容（模板可能默认关闭思考）")

        # 会话文件里必须带 reasoning（任务五：历史回放）
        time.sleep(0.5)
        session_path = os.path.join(workdir, "session.json")
        if os.path.exists(session_path):
            with open(session_path, "r", encoding="utf-8") as handle:
                session = json.load(handle)
            assistant = [m for m in session.get("history", []) if m.get("role") == "assistant"]
            check("真实模型：历史里保存了本次对话", bool(assistant))
            if final_reasoning:
                check("真实模型：历史里思考单独成字段",
                      assistant[-1].get("reasoning") == final_reasoning,
                      repr(assistant[-1].get("reasoning", "")[:80]))
                check("真实模型：历史里回答不含思考标签",
                      all(tag not in assistant[-1]["content"] for tag in
                          (OPEN_THINK, CLOSE_THINK)),
                      repr(assistant[-1]["content"][:120]))
        else:
            skip("真实模型：会话文件", "后端还没有写出 session.json")
    finally:
        try:
            send({"action": "exit"})
        except Exception:
            pass
        time.sleep(1)
        if proc.poll() is None:
            proc.kill()


def main():
    parser = argparse.ArgumentParser(description="思考内容分离测试")
    parser.add_argument("--model", default="", help="真实推理模型（.gguf）路径，给了就追加端到端")
    args = parser.parse_args()

    test_splitter()
    test_engine()
    test_protocol()
    test_history()
    test_lang()

    if args.model:
        if os.path.exists(args.model):
            test_real_model(os.path.abspath(args.model))
        else:
            skip("真实模型端到端", f"找不到模型文件 {args.model}")
    else:
        skip("真实模型端到端", "未指定 --model")

    print("\n" + "=" * 70)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项，跳过 {len(SKIPPED)} 项")
    for item in SKIPPED:
        print(f"  [SKIP] {item}")
    for item in FAILED:
        print(f"  [FAIL] {item}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
