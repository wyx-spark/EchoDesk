"""
[EN]
Agent invocation loop (v0.2.1 capability extension, task 2).

Responsibility: drive the "model -> tool call -> observe result -> decide again"
loop and emit every step explicitly instead of returning only the final answer,
while preserving the four hard constraints required by the design specification:

* Maximum step count: once ``max_steps`` (default 4) is reached, no further tool is
  executed; the model is instead instructed to conclude using the information
  already gathered, so the loop always terminates. This guards against a model that
  repeatedly invokes the same tool and saturates local machine resources.
* Interruptibility: ``stop_event`` is checked before and after every model call and
  every tool execution, sharing the same stop signal as the streaming path (the Esc
  key in the user interface).
* Per-step visibility: the loop emits ``agent_step`` / ``tool_call`` /
  ``tool_result`` events so the front end can render which tool was invoked, with
  which arguments, and what it returned, rather than only the final paragraph.
* Exception isolation: a failed single-step model call, a tool exception, or an
  empty result affects only that step; the loop continues and, when necessary,
  converges directly on "answer using the available information".

Why every step uses a non-streaming call: tool invocation requires the strict
"one JSON line" shape, and the llama.cpp Jinja handler has hard limitations in its
support for tool semantics while streaming (see :mod:`agent_tools`). A non-streaming
call combined with a progress event emitted before each invocation keeps the
interface from appearing stalled (a 0.6B call takes a few seconds).

[ZH]
Agent 调用循环（v0.2.1 能力扩展，任务二）。

职责：驱动"模型 → 调用工具 → 观察结果 → 再次决策"这一循环，并显式输出每一步，
而不是只返回最终答案；同时满足设计规范规定的四条硬约束：

* 最大步数：达到 ``max_steps``（默认 4）后不再执行任何工具，改为让模型基于已有
  信息给出结论，因此循环必然终止。该约束用于防止模型反复调用同一工具并耗尽本地
  机器资源。
* 可中断：每一步的模型调用与每一次工具执行前后都会检查 ``stop_event``，与流式
  路径共用同一个停止信号（即用户在界面上按 Esc 时触发的信号）。
* 每一步可见：循环产出 ``agent_step`` / ``tool_call`` / ``tool_result`` 三类事件，
  前端据此渲染"调用了哪个工具、参数是什么、返回了什么"，而不是只看到最后一段话。
* 异常隔离：单步模型调用失败、工具抛出异常或结果为空，都只影响该步；循环继续，
  必要时直接收敛到"使用已有信息作答"。

每一步采用非流式调用的原因：工具调用要求"一行 JSON"的严格形状，而 llama.cpp 的
Jinja handler 在流式模式下对工具语义的支持存在硬性限制（参见 :mod:`agent_tools`）。
非流式调用加上每次调用前发出的进度事件，可避免界面看起来卡住（一次 0.6B 调用
约需数秒）。
"""
from __future__ import annotations
from i18n import t as _t

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from agent_tools import RISK_GUARD, ToolRegistry, extract_final_answer, parse_tool_call
from reasoning import split_text

#: [EN] Maximum number of characters injected from a single tool result. Tool results
#: [EN] are fed back into the context verbatim; without a cap, one ``file_read`` call
#: [EN] can consume half of an 8192-token window.
#: [ZH] 单条工具结果最多注入的字符数。工具结果会原样回灌进上下文；若不设上限，
#: [ZH] 一次 ``file_read`` 调用即可占用 8192 窗口的一半。
MAX_OBSERVATION_CHARS = 1200

#: [EN] Cumulative injection limit across all tool results (on overflow, the earliest
#: [EN] entries are replaced with an elision note).
#: [ZH] 所有工具结果的累计注入上限（超出时从最早的结果开始替换为一条省略说明）。
MAX_TOTAL_OBSERVATION_CHARS = 4000


@dataclass
class AgentStep:
    """
    [EN]
    Record of a single agent step. Diagnostics and tests must both be able to read
    what actually happened.

    [ZH]
    单步记录。诊断与测试都必须能够读取"实际发生了什么"。
    """

    index: int
    text: str = ""              # [EN] Raw model output for this step.
                                # [ZH] 模型本步的原始输出。
    # [EN] Task 13 (self-test fix 1): the reasoning content of this step (the part
    # [EN] inside tags such as ``<think>``). It is stored separately from ``text``:
    # [EN] ``text`` is the body with reasoning removed, and both tool-call parsing
    # [EN] and final-answer extraction operate on it only.
    # [ZH] 相关设计（自测问题兼容性修正）：本步的思考内容（``<think>`` 等标签内部的部分）。
    # [ZH] 与 ``text`` 分开存储：``text`` 是去除思考后的正文，工具调用解析与最终答案
    # [ZH] 提取都只使用它。
    reasoning: str = ""
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    why: str = ""
    ok: bool = False
    #: [EN] Per-invocation confirmation decision for guard tools; ``None`` means
    #: [EN] the tool did not require confirmation.
    #: [ZH] 高危工具的逐次确认决定；``None`` 表示该工具无需确认。
    confirmed: Optional[bool] = None
    observation: str = ""
    elapsed_ms: int = 0


@dataclass
class AgentRun:
    """
    [EN]
    Complete result of one agent run.

    [ZH]
    一轮 Agent 任务的完整结果。
    """

    steps: List[AgentStep] = field(default_factory=list)
    answer: str = ""
    stopped: bool = False
    hit_max_steps: bool = False
    error: str = ""
    elapsed_ms: int = 0

    def trace(self) -> List[Dict[str, Any]]:
        """
        [EN]
        Machine-readable step trace, consumed by ``agent_diagnostics`` and by test
        assertions.

        ``reasoning`` is included as well (task 13): diagnostics must be able to
        distinguish "this step produced no reasoning" from "this step produced
        reasoning that was not displayed". Those two conditions were previously
        indistinguishable, which is the regression this task addresses.

        [ZH]
        机器可读的步骤轨迹，供 ``agent_diagnostics`` 与测试断言使用。

        同时包含 ``reasoning``（任务十三）：诊断必须能够区分"该步没有产生思考"与
        "该步产生了思考但未显示"。此前这两种情况无法区分，而这正是本任务修复的
        缺陷。
        """
        return [
            {"index": step.index, "tool": step.tool, "args": step.args, "ok": step.ok,
             "confirmed": step.confirmed,
             "observation": step.observation[:200], "elapsed_ms": step.elapsed_ms,
             "has_reasoning": bool(step.reasoning.strip())}
            for step in self.steps
        ]


class AgentLoop:
    """
    [EN]
    Tool invocation loop.

    ``llm`` only has to implement ``chat_complete(messages, temperature=...,
    max_tokens=...)`` (both ``LLMEngine`` and the test stubs satisfy this), so the
    whole loop can be unit-tested without loading a model.

    [ZH]
    工具调用循环。

    ``llm`` 只需实现 ``chat_complete(messages, temperature=..., max_tokens=...)``
    （``LLMEngine`` 与测试桩均满足该接口），因此无需加载模型即可对整个循环做单元
    测试。
    """

    def __init__(
        self,
        llm: Any,
        registry: ToolRegistry,
        max_steps: int = 4,
        stop_event: Any = None,
        max_tokens: int = 512,
        max_observation_chars: int = MAX_OBSERVATION_CHARS,
        confirm_tool: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.max_steps = max(1, int(max_steps or 1))
        self.stop_event = stop_event
        self.max_tokens = max(64, int(max_tokens or 512))
        self.max_observation_chars = max(200, int(max_observation_chars or 200))
        #: [EN] Callback that asks the user to approve a guard tool. It must
        #: [EN] return only after the user decided, and returns ``False`` on
        #: [EN] denial, timeout, or absence of a user interface.
        #: [ZH] 请求用户批准高危工具的回调。必须等到用户作出决定后才返回；
        #: [ZH] 拒绝、超时或无界面时返回 ``False``。
        self.confirm_tool = confirm_tool

    # [EN] ---------- Internal helpers ----------
    # [ZH] ---------- 内部辅助方法 ----------

    def _stopped(self) -> bool:
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _complete(self, messages: List[dict], temperature: float) -> Tuple[str, str]:
        """
        [EN]
        Perform one non-streaming model call and return ``(reasoning, body)``.

        Exceptions are translated here into two empty strings; the caller decides how
        to converge.

        Task 13 (self-test fix 1): reasoning must be separated at this point for two
        independent reasons.

        1. The interface must display the reasoning of every step. The reported
           symptom was "the reasoning process is not displayed in Agent mode". The
           root cause was not that the model produced no reasoning, but that this
           path never separated it: ``chat_complete`` returns the entire raw model
           output (including ``<think>`` sections) and the loop treated it as the
           content of the step, while the ``agent_step`` events sent to the front end
           carried only index/phase and no body text. Consequently the interface had
           no renderable reasoning content. The regular chat path splits the two
           channels via ``chat_stream_split`` and ``ReasoningSplitter``; the Agent path
           did not.
        2. Parsing must operate on the body only. ``parse_tool_call`` and
           ``extract_final_answer`` both perform shape matching over the model output.
           If reasoning text is fed in as well, phrases such as "I can call file_read"
           that appear inside the reasoning are misclassified as a real tool call.
           Separating before parsing eliminates this failure mode.

        [ZH]
        执行一次非流式模型调用，返回 ``(思考内容, 正文)``。

        异常在此处被转换为两个空串，由调用方决定如何收敛。

        任务十三（自测问题修复一）：必须在此处分离思考内容，原因有两点。

        1. 界面必须显示每一步的思考。用户报告的现象是"Agent 模式下不显示思考过程"。
           根因并非模型没有产生思考，而是该路径从未做过分离：``chat_complete``
           整段返回模型原始输出（含 ``<think>`` 段落），循环直接将其视为本步内容；
           而发往前端的 ``agent_step`` 事件只包含 index/phase，不包含正文，因此界面
           没有任何可渲染的思考内容。普通对话路径通过 ``chat_stream_split`` 与
           ``ReasoningSplitter`` 拆分两条通道，Agent 路径未做同样处理。
        2. 解析必须只针对正文。``parse_tool_call`` 与 ``extract_final_answer`` 均对
           模型输出做形状匹配；若把思考段落一并传入，思考中出现的"我可以调用
           file_read"之类语句会被误判为真实的工具调用。先分离再解析可消除该失效
           模式。
        """
        try:
            text = self.llm.chat_complete(
                messages, temperature=temperature, max_tokens=self.max_tokens,
            )
        except Exception:
            return "", ""
        return split_text(str(text or ""))

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        body = str(text or "").strip()
        if len(body) <= limit:
            return body
        return body[:limit] + f"…（共 {len(body)} 字符，已截断）"

    def _observation_message(self, tool: str, observation: str, ok: bool) -> str:
        head = f"【工具结果：{tool}】" if ok else f"【工具不可用：{tool}】"
        return (f"{head}\n{observation}\n\n"
                f"请继续：可以再调用一个工具，或用「最终答案：」开头给出结论。")

    def _request_confirmation(self, step: AgentStep, index: int) -> bool:
        """
        [EN]
        Ask the host to confirm one guard-tool invocation.

        The request carries a fresh identifier plus the exact tool name and
        arguments, so the confirmation shown to the user is the content that
        will actually run. A missing callback, an exception, or a negative
        answer all mean denial: the safe default is never to execute.

        [ZH]
        请求宿主确认一次高危工具调用。

        请求携带全新标识、精确的工具名与参数，因此展示给用户的确认内容就是
        即将执行的内容。回调缺失、抛出异常或给出否定答复均视为拒绝：安全默认值
        永远是"不执行"。
        """
        if self.confirm_tool is None:
            return False
        if step.tool == "execute_shell":
            preview = str(step.args.get("command") or step.args.get("cmd") or "")
        elif step.tool == "git_command":
            raw = step.args.get("args")
            preview = " ".join(raw) if isinstance(raw, list) else str(raw or "")
        else:
            preview = json.dumps(step.args, ensure_ascii=False)
        request = {
            "type": "confirm_tool",
            "request_id": uuid.uuid4().hex[:16],
            "tool": step.tool,
            "args": step.args,
            "why": step.why,
            "command": preview,
        }
        try:
            return bool(self.confirm_tool(request))
        except Exception:
            return False

    # [EN] ---------- Main loop ----------
    # [ZH] ---------- 主循环 ----------

    def run(
        self,
        messages: List[dict],
        temperature: float = 0.5,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> AgentRun:
        """
        [EN]
        Execute one complete agent run.

        The event shapes delivered to ``on_event`` match the backend protocol
        (``agent_step`` / ``tool_call`` / ``tool_result``); the caller
        (``Backend._stream_agent``) converts them directly into stream lines.

        [ZH]
        执行一轮完整的 Agent 任务。

        传给 ``on_event`` 的事件形状与后端协议一致（``agent_step`` / ``tool_call`` /
        ``tool_result``），调用方（``Backend._stream_agent``）可直接将其转换为流式
        行。
        """
        emit = on_event or (lambda _event: None)
        started = time.time()
        run = AgentRun()
        convo: List[dict] = [dict(m) for m in messages]
        observations: List[str] = []          # [EN] Tool results already injected (for the cumulative length).
                                              # [ZH] 已注入的工具结果（用于累计长度）。
        total_chars = 0

        for index in range(1, self.max_steps + 1):
            if self._stopped():
                run.stopped = True
                break
            emit({"type": "agent_step", "index": index, "phase": "thinking",
                  "max_steps": self.max_steps})
            reasoning, text = self._complete(convo, temperature)
            step = AgentStep(index=index, text=text, reasoning=reasoning)
            run.steps.append(step)
            if reasoning:
                # [EN] Task 13: emit the reasoning content as a separate event. It must
                # [EN] be emitted after the model returns: the preceding "thinking" event
                # [EN] exists to let the interface immediately show "step N is thinking"
                # [EN] and avoid a multi-second blank period; the content is only
                # [EN] available at this point, hence the additional event carrying
                # [EN] ``reasoning``.
                # [ZH] 相关设计：将思考内容作为独立事件发出。该事件必须位于模型返回
                # [ZH] 之后：前一条 "thinking" 事件的职责是让界面立即显示"第 N 步正在
                # [ZH] 思考"，避免数秒的空白；内容此时才获取到，因此另发一条携带
                # [ZH] ``reasoning`` 的事件。
                emit({"type": "agent_step", "index": index, "phase": "thought",
                      "max_steps": self.max_steps, "reasoning": reasoning})
            if not text.strip():
                # [EN] The model produced no body text this round (or emitted reasoning
                # [EN] only): stop retrying and converge.
                # [ZH] 模型本轮未输出正文（或仅输出思考）：不再继续尝试，直接收敛。
                run.error = _t("agent.no_content")
                break

            call = parse_tool_call(text)
            if call is None:
                run.answer = extract_final_answer(text)
                emit({"type": "agent_step", "index": index, "phase": "answer",
                      "text": run.answer[:400]})
                break

            step.tool = call["tool"]
            step.args = call["args"]
            step.why = call.get("why", "")
            emit({"type": "tool_call", "index": index, "tool": step.tool,
                  "args": step.args, "why": step.why})

            if self._stopped():
                run.stopped = True
                break

            tool_started = time.time()
            # [EN] v0.2.6 task 2.4: guard tools require an explicit per-call
            # [EN] confirmation. Without approval the tool is not invoked at all.
            # [ZH] v0.2.6 任务 2.4：高危工具需要逐次显式确认。未获批准时完全不调用
            # [ZH] 该工具。
            tool_spec = self.registry.get(step.tool)
            guard = tool_spec is not None and tool_spec.risk == RISK_GUARD
            approved = True
            if guard:
                approved = self._request_confirmation(step, index)
                step.confirmed = approved
            if approved:
                ok, observation = self.registry.run(
                    step.tool, step.args, confirmed=guard)
            else:
                ok, observation = False, _t("tool.confirm_denied")
            step.elapsed_ms = int((time.time() - tool_started) * 1000)
            step.ok = ok
            step.observation = self._clip(observation, self.max_observation_chars)
            emit({"type": "tool_result", "index": index, "tool": step.tool, "ok": ok,
                  "observation": step.observation, "elapsed_ms": step.elapsed_ms})

            # [EN] On cumulative overflow, drop the earliest results. It is preferable
            # [EN] for the model to lose an older observation than for tool results to
            # [EN] overflow the context window, which would slow down or truncate every
            # [EN] subsequent step.
            # [ZH] 累计超限时丢弃最早的结果：宁可让模型少看一段旧结果，也不能让工具
            # [ZH] 结果挤爆上下文窗口，否则后续每一步都会变慢甚至被截断。
            total_chars += len(step.observation)
            observations.append(step.observation)
            if total_chars > MAX_TOTAL_OBSERVATION_CHARS and len(observations) > 1:
                dropped = observations.pop(0)
                total_chars -= len(dropped)

            convo.append({"role": "assistant", "content": text})
            convo.append({"role": "user",
                          "content": self._observation_message(step.tool, step.observation, ok)})

            if index == self.max_steps:
                run.hit_max_steps = True

        # [EN] The step limit was reached while still invoking tools: give the model
        # [EN] one further opportunity to answer using only the information already
        # [EN] gathered, so the user always receives a conclusion rather than a bare
        # [EN] "step limit exhausted" notice.
        # [ZH] 达到步数上限时仍在调用工具：再给模型一次仅使用已有信息作答的机会，
        # [ZH] 使调用方总能获得一段结论，而不是仅得到"步数已用完"的提示。
        if not run.answer and not run.stopped and run.hit_max_steps:
            convo.append({"role": "user",
                          "content": f"已达到本次最大工具调用步数（{self.max_steps}）。"
                                     f"请不要再调用工具，直接用已有信息输出「最终答案：…」；"
                                     f"信息不足的部分如实说明。"})
            emit({"type": "agent_step", "index": self.max_steps + 1, "phase": "finalize",
                  "max_steps": self.max_steps})
            final_reasoning, final_text = self._complete(convo, temperature)
            # [EN] Task 13: this step may also produce reasoning; emit it as well so that
            # [EN] the finalization step is not rendered as an event without content.
            # [ZH] 相关设计：该步同样可能产生思考，也一并发出，使界面上"最后收尾"
            # [ZH] 这一步不会成为一个没有内容的事件。
            if final_reasoning:
                emit({"type": "agent_step", "index": self.max_steps + 1, "phase": "thought",
                      "max_steps": self.max_steps, "reasoning": final_reasoning})
            run.answer = extract_final_answer(final_text) or final_text.strip()

        if not run.answer and not run.stopped:
            # [EN] Fallback: use the last non-empty step text as the answer (more useful
            # [EN] than returning nothing); if no text exists, report the reason instead.
            # [ZH] 兜底：将最后一步的非空文本作为答案（比空结果更有用）；若无文本，
            # [ZH] 则返回原因。
            for step in reversed(run.steps):
                if step.text.strip():
                    run.answer = extract_final_answer(step.text)
                    break
            if not run.answer:
                run.answer = run.error or _t("agent.no_answer")

        run.elapsed_ms = int((time.time() - started) * 1000)
        return run


def run_summary_json(run: AgentRun) -> str:
    """
    [EN]
    Compress one agent run into a single JSON line for logging and diagnostics;
    human readability takes priority.

    [ZH]
    将一轮 Agent 结果压缩为一行 JSON，用于日志与诊断；优先保证人类可读。
    """
    return json.dumps(
        {"steps": len(run.steps), "stopped": run.stopped, "hit_max_steps": run.hit_max_steps,
         "elapsed_ms": run.elapsed_ms, "tools": [s.tool for s in run.steps]},
        ensure_ascii=False,
    )
