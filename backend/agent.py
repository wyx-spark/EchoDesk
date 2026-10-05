"""Agent 调用循环（v0.2.1 能力扩展-1 任务二）。

职责：把"模型 → 调用工具 → 看结果 → 再决定"这个循环跑起来，并把每一步**显式**
产出去（不是只给最终答案），同时守住任务书要求的四条硬约束：

* **最大步数**：``max_steps``（默认 4）之后不再执行工具，改成让模型"用已有信息给结论"，
  所以循环一定有终点。防的是"模型反复调同一个工具把本地机器跑满"。
* **可中断**：每步的模型调用与每次工具执行前后都检查 ``stop_event``，
  与流式路径共用同一个停止信号（用户在界面上按 Esc 就是它）。
* **每一步可见**：循环产出 ``agent_step`` / ``tool_call`` / ``tool_result`` 三类事件，
  前端据此渲染"调了什么、参数是什么、返回了什么"，而不是只看到最后一段话。
* **异常隔离**：单步模型调用失败、工具抛异常、结果为空都只影响这一步，
  循环继续（必要时直接收敛到"用已有信息作答"）。

为什么每步是**非流式**调用：工具调用要求"一行 JSON"这种严格形状，而 llama.cpp 的
Jinja handler 在流式下对工具语义的支持有硬限制（见 :mod:`agent_tools` 的说明）。
非流式 + 每次调用前发一个进度事件，界面上不会显得卡住（一次 0.6B 调用约数秒）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

from agent_tools import ToolRegistry, extract_final_answer, parse_tool_call

#: 单条工具结果最多注入多少字符。工具结果会原样回灌进上下文，不设上限时
#: 一次 ``file_read`` 就能把 8192 的窗口吃掉一半。
MAX_OBSERVATION_CHARS = 1200

#: 所有工具结果累计注入上限（超出时从最早的开始替换成一句省略说明）。
MAX_TOTAL_OBSERVATION_CHARS = 4000


@dataclass
class AgentStep:
    """一步的记录（诊断与测试都要能读到"到底发生了什么"）。"""

    index: int
    text: str = ""              # 模型这一步的原始输出
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    why: str = ""
    ok: bool = False
    observation: str = ""
    elapsed_ms: int = 0


@dataclass
class AgentRun:
    """一轮 Agent 任务的完整结果。"""

    steps: List[AgentStep] = field(default_factory=list)
    answer: str = ""
    stopped: bool = False
    hit_max_steps: bool = False
    error: str = ""
    elapsed_ms: int = 0

    def trace(self) -> List[Dict[str, Any]]:
        """机器可读的步骤轨迹（供 ``agent_diagnostics`` 与测试断言）。"""
        return [
            {"index": step.index, "tool": step.tool, "args": step.args, "ok": step.ok,
             "observation": step.observation[:200], "elapsed_ms": step.elapsed_ms}
            for step in self.steps
        ]


class AgentLoop:
    """工具调用循环。

    ``llm`` 只要求实现 ``chat_complete(messages, temperature=..., max_tokens=...)``
    （``LLMEngine`` 与测试桩都满足），便于在不加载模型的情况下单测整个循环。
    """

    def __init__(
        self,
        llm: Any,
        registry: ToolRegistry,
        max_steps: int = 4,
        stop_event: Any = None,
        max_tokens: int = 512,
        max_observation_chars: int = MAX_OBSERVATION_CHARS,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.max_steps = max(1, int(max_steps or 1))
        self.stop_event = stop_event
        self.max_tokens = max(64, int(max_tokens or 512))
        self.max_observation_chars = max(200, int(max_observation_chars or 200))

    # ---------- 内部小工具 ----------

    def _stopped(self) -> bool:
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _complete(self, messages: List[dict], temperature: float) -> str:
        """调用一次模型（非流式）。异常在这里被翻译成空串，由调用方决定怎么收敛。"""
        try:
            text = self.llm.chat_complete(
                messages, temperature=temperature, max_tokens=self.max_tokens,
            )
        except Exception:
            return ""
        return str(text or "")

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

    # ---------- 主循环 ----------

    def run(
        self,
        messages: List[dict],
        temperature: float = 0.5,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> AgentRun:
        """跑完一轮 Agent 任务。

        ``on_event`` 收到的事件形状与后端协议一致（``agent_step`` / ``tool_call`` /
        ``tool_result``），调用方（``Backend._stream_agent``）直接把它们转成流式行即可。
        """
        emit = on_event or (lambda _event: None)
        started = time.time()
        run = AgentRun()
        convo: List[dict] = [dict(m) for m in messages]
        observations: List[str] = []          # 已注入的工具结果（累计长度用）
        total_chars = 0

        for index in range(1, self.max_steps + 1):
            if self._stopped():
                run.stopped = True
                break
            emit({"type": "agent_step", "index": index, "phase": "thinking",
                  "max_steps": self.max_steps})
            text = self._complete(convo, temperature)
            step = AgentStep(index=index, text=text)
            run.steps.append(step)
            if not text.strip():
                # 模型这一轮什么都没说：不再硬撑，收敛（模型侧失败或已停止）
                run.error = "模型没有返回内容"
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
            ok, observation = self.registry.run(step.tool, step.args)
            step.elapsed_ms = int((time.time() - tool_started) * 1000)
            step.ok = ok
            step.observation = self._clip(observation, self.max_observation_chars)
            emit({"type": "tool_result", "index": index, "tool": step.tool, "ok": ok,
                  "observation": step.observation, "elapsed_ms": step.elapsed_ms})

            # 累计超限时把最早的结果替换掉：宁可让模型少看一段旧结果，
            # 也不能让工具结果把上下文窗口挤爆（那会让后续每步都变慢甚至截断）。
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

        # 达到步数上限仍在调工具：再给一次"只用已有信息作答"的机会，
        # 这样用户总能拿到一段结论，而不是一句"步数用完了"。
        if not run.answer and not run.stopped and run.hit_max_steps:
            convo.append({"role": "user",
                          "content": f"已达到本次最大工具调用步数（{self.max_steps}）。"
                                     f"请不要再调用工具，直接用已有信息输出「最终答案：…」；"
                                     f"信息不足的部分如实说明。"})
            emit({"type": "agent_step", "index": self.max_steps + 1, "phase": "finalize",
                  "max_steps": self.max_steps})
            final_text = self._complete(convo, temperature)
            run.answer = extract_final_answer(final_text) or final_text.strip()

        if not run.answer and not run.stopped:
            # 兜底：把最后一步的文本当答案（比空手而归有用），没有文本就给原因
            for step in reversed(run.steps):
                if step.text.strip():
                    run.answer = extract_final_answer(step.text)
                    break
            if not run.answer:
                run.answer = run.error or "模型没有给出可用回答。"

        run.elapsed_ms = int((time.time() - started) * 1000)
        return run


def run_summary_json(run: AgentRun) -> str:
    """把一轮 Agent 结果压成一行 JSON（供日志/诊断用，人类可读优先）。"""
    return json.dumps(
        {"steps": len(run.steps), "stopped": run.stopped, "hit_max_steps": run.hit_max_steps,
         "elapsed_ms": run.elapsed_ms, "tools": [s.tool for s in run.steps]},
        ensure_ascii=False,
    )
