# -*- coding: utf-8 -*-
"""Agent 平台测试（v0.2.1 能力扩展-1 任务二）。

覆盖任务书对 Agent 平台的每一条要求，并且**全部不需要模型、不需要网络**
（工具是纯本地计算与"只读已上传内容"，循环用脚本化桩 LLM 驱动）：

| 任务书要求 | 对应断言 |
| --- | --- |
| 抽象工具接口（模型声明要调什么，框架执行并返回） | `Tool` / `ToolRegistry.run` / `parse_tool_call` 的形状与容错 |
| 工具集覆盖现有能力 + 若干新工具 | 计算/日期/单位换算 + 文档检索/摘要 + 联网搜索/读文件（后两个敏感） |
| 调用循环 + 最大步数 | `AgentLoop` 的多步脚本、`hit_max_steps`、到顶后强制收敛 |
| 权限控制（有副作用需确认或预授权） | 敏感工具默认关闭、`allow` 后可调用、**没有**写盘/执行类工具 |
| 可见性（每一步都能看到） | 事件序列 `agent_step → tool_call → tool_result → done`，`trace()` 可读 |
| 可中断 | `stop_event` 置位后不再调用模型/工具，`stopped=True` |

另外三处是**安全与健壮性**，都来自真实风险而不是想象：
计算器用 AST 白名单求值（`__import__('os').system(...)` 这类输入必须被拒绝）、
工具抛异常只影响这一步、模型输出为空时不死循环。

跑法：``python tools\\test_agent.py``；退出码 0 = 全部通过。
"""
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import agent_tools as at          # noqa: E402
from agent import AgentLoop       # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


class StubConv:
    """最小会话桩：只实现工具层用到的文件接口。"""

    def __init__(self, files=None):
        self.files = files or []
        self.active_index = len(self.files) - 1 if self.files else -1


class StubEntry:
    def __init__(self, name, content="", summary="", chars=None, stored_at=""):
        self.name = name
        self.content = content
        self.summary = summary
        self.chars = chars if chars is not None else len(content)
        self.stored_at = stored_at


class ScriptedLLM:
    """按脚本逐次返回的桩 LLM（每次 `chat_complete` 取下一个回复）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat_complete(self, messages, temperature=0.7, max_tokens=1024, **kwargs):
        self.calls.append(list(messages))
        if not self.replies:
            return ""
        return self.replies.pop(0)


class ExplodingLLM:
    def __init__(self):
        self.calls = 0

    def chat_complete(self, *a, **k):
        self.calls += 1
        raise RuntimeError("模型调用失败")


print("=== 1. 计算器：AST 白名单求值（不用 eval）===")
check("四则运算", at.calc_eval("(128.6*10000)*0.003") == 3858.0, str(at.calc_eval("(128.6*10000)*0.003")))
check("幂与取模", at.calc_eval("2**10 % 7") == 1024 % 7)
check("数学函数", abs(at.calc_eval("sqrt(16)") - 4) < 1e-9)
check("round 可用", at.calc_eval("round(3.14159, 2)") == 3.14)
check("中文乘除号也认", at.calc_eval("3 × 4 ÷ 2") == 6.0)
check("多层括号与负数", at.calc_eval("-((1+2)*3)") == -9)
for _bad, _label in (
    ('__import__("os").system("dir")', "import 调用"),
    ("open('x')", "open 调用"),
    ("a.b", "属性访问"),
    ("(1).__class__", "dunder 属性"),
    ("d['k']", "下标"),
    ("[x for x in range(3)]", "推导式"),
    ("(lambda: 1)()", "lambda"),
    ("10**10**10", "超大幂运算"),
):
    try:
        at.calc_eval(_bad)
        check(f"拒绝危险表达式：{_label}", False, "竟然执行了")
    except at.CalcError:
        check(f"拒绝危险表达式：{_label}", True)
check("错误信息会告诉模型换个表达式", "计算失败" in at.tool_calc({"expr": "1/0"}) or
      "计算失败" in at.tool_calc({"expr": "__import__('os')"}))
check("整数结果不带小数点", at.tool_calc({"expr": "2*3"}).endswith("= 6"))

print("=== 2. 日期与单位换算 ===")
check("日期加减（跨月按月末收敛）",
      "2026-02-28" in at.tool_date({"op": "add", "from": "2026-01-31", "months": 1}))
check("天数差",
      "73 天" in at.tool_date({"op": "diff", "a": "2026-01-01", "b": "2026-03-15"}))
check("星期几",
      "星期" in at.tool_date({"op": "weekday", "date": "2026-03-15"}))
check("默认就是今天", "今天是" in at.tool_date({}))
check("坏日期给出可读错误", "无法识别" in at.tool_date({"op": "weekday", "date": "昨天"}))
check("重量换算", "6.613868" in at.tool_convert({"value": 3, "from": "kg", "to": "lb"}),
      at.tool_convert({"value": 3, "from": "kg", "to": "lb"}))
check("中文单位名可用", "1609" in at.tool_convert({"value": 1, "from": "英里", "to": "m"}))
check("温度换算（有偏移，不是倍率）",
      at.tool_convert({"value": 100, "from": "c", "to": "f"}).endswith("212 F"))
check("不同类单位拒绝换算", "不是同一类单位" in at.tool_convert({"value": 1, "from": "kg", "to": "m"}))
check("缺参数给出可读错误", "换算失败" in at.tool_convert({"value": 1}))

print("=== 3. 工具调用解析（模型输出形状容错）===")
check("标准一行 JSON",
      (at.parse_tool_call('{"tool": "calc", "args": {"expr": "1+1"}, "why": "算"}') or {}).get("tool") == "calc")
check("代码块包裹也能解析",
      (at.parse_tool_call('```json\n{"tool":"calc","args":{"expr":"1"}}\n```') or {}).get("tool") == "calc")
check("前后有解释文字也能解析",
      (at.parse_tool_call('我来算一下：{"tool":"calc","args":{"expr":"1"}} 然后回答') or {}).get("tool")
      == "calc")
check("键名写成 name 也认",
      (at.parse_tool_call('{"name": "date", "op": "today"}') or {}).get("args") == {"op": "today"})
check("参数平铺在顶层也认",
      (at.parse_tool_call('{"tool": "calc", "expr": "2*3"}') or {}).get("args") == {"expr": "2*3"})
check("嵌套括号不被截断",
      (at.parse_tool_call('{"tool":"calc","args":{"expr":"{\\"a\\":1}"}}') or {}).get("args")
      == {"expr": '{"a":1}'})
check("被截断的 JSON 当普通回答（不抛异常）",
      at.parse_tool_call('{"tool": "calc", "args": {"expr": "1+1"') is None)
check("普通回答不是工具调用", at.parse_tool_call("这是一段普通回答。") is None)
check("空串不是工具调用", at.parse_tool_call("") is None)
check("「最终答案：」被识别",
      at.extract_final_answer("过程略。\n**最终答案：** 甲 15 岁") == "甲 15 岁")
check("没有标记时返回原文", at.extract_final_answer("就是一段结论") == "就是一段结论")
check("工具调用行不会混进最终答案",
      "calc" not in at.extract_final_answer('{"tool":"calc","args":{"expr":"1"}}\n结论是 2'))

print("=== 4. 权限分级与异常隔离 ===")
_registry = at.ToolRegistry()
check("默认只开安全工具",
      set(_registry.names()) - set(t.name for t in _registry.available())
      == {"web_search", "file_read"},
      str([t.name for t in _registry.available()]))
check("敏感工具未授权时给出「怎么开」的说明",
      "未启用" in _registry.run("web_search", {"query": "x"})[1]
      and "/agent allow web_search" in _registry.run("web_search", {"query": "x"})[1])
check("授权后可以调用", _registry.allow("web_search") and
      "web_search" in [t.name for t in _registry.available()])
check("收回授权", _registry.allow("web_search", False) and
      "web_search" not in [t.name for t in _registry.available()])
check("未知工具不抛异常、给出可用清单",
      _registry.run("nope", {})[0] is False and "没有名为" in _registry.run("nope", {})[1])
check("本版不提供任何写盘/执行类工具（权限最小化）",
      all(tool.risk != at.RISK_GUARD for tool in at.default_tools()))
check("工具清单可给界面用",
      all({"name", "risk", "enabled", "description"} <= set(item)
          for item in _registry.public_list()))
_protocol = _registry.protocol_text()
check("协议文本只列已启用的工具", "calc" in _protocol and "web_search" not in _protocol.split("[未启用的工具]")[0])
check("协议文本点名未启用的工具", "[未启用的工具]" in _protocol and "web_search" in _protocol)
check("协议文本给出 JSON 形状与示例",
      '"tool"' in _protocol and "最终答案" in _protocol and "[格式示例]" in _protocol)


def _raising(_args):
    raise RuntimeError("工具炸了")


_registry.register(at.Tool(name="boom", description="故意抛异常", run=_raising))
_ok, _text = _registry.run("boom", {})
check("工具抛异常被兜住（返回说明而不是崩溃）", _ok is False and "执行出错" in _text, _text)
check("调用审计记录了失败原因", any(c.get("reason") == "error" for c in _registry.calls))

print("=== 5. 文档检索工具（只读已上传内容）===")
_conv = StubConv([
    StubEntry("采购合同.txt",
              "第三条 乙方应于合同生效后 60 日内交付全部设备。"
              "第四条 甲方应在验收合格后 30 个工作日内支付 128.6 万元。"
              "第五条 乙方逾期交货的，每逾期一日按合同总额的 0.3% 支付违约金。"),
    StubEntry("会议纪要.txt", "本次会议讨论了办公用品采购流程，决定改为线上申请。"),
])
_ctx = at.ToolContext(conv=_conv)
_reg = at.ToolRegistry(ctx=_ctx)
_hit = _reg.run("doc_search", {"query": "违约金", "top": 2})[1]
check("检索命中相关段落", "违约金" in _hit, _hit[:120])
check("检索结果带文件名（可溯源）", "采购合同.txt" in _hit, _hit[:120])
check("检索可以指定文件", "会议纪要" in _reg.run("doc_search", {"query": "线上申请"})[1])
check("没有文件时给出可操作的说明",
      "还没有上传" in at.ToolRegistry(ctx=at.ToolContext(conv=StubConv())).run(
          "doc_search", {"query": "x"})[1])
check("检索不到也不报错", "没有在已上传文件里找到" in _reg.run("doc_search", {"query": "火星基地"})[1])
_sum = _reg.run("doc_summary", {"file": "采购合同"})[1]
check("摘要工具：没有摘要时给开头片段并说明", "还没有生成分段摘要" in _sum and "第三条" in _sum)
_conv2 = StubConv([StubEntry("合同.txt", summary="这是一段摘要", chars=9999, content="原文")])
check("摘要工具：有摘要时直接给摘要",
      "这是一段摘要" in at.ToolRegistry(ctx=at.ToolContext(conv=_conv2)).run("doc_summary", {})[1])

print("=== 6. Agent 循环：多步、收敛、可中断 ===")
_tool_call = '{"tool": "calc", "args": {"expr": "3000*7.2"}, "why": "算汇率"}'
_llm = ScriptedLLM([_tool_call, "最终答案：3000 美元约合 21600 元人民币。"])
_events = []
_loop = AgentLoop(_llm, at.ToolRegistry(), max_steps=4)
_run = _loop.run([{"role": "user", "content": "3000 美元按 7.2 是多少人民币"}], on_event=_events.append)
check("多步循环跑完并给出最终答案", _run.answer.startswith("3000 美元"), _run.answer)
check("执行了一次工具调用", len([s for s in _run.steps if s.tool]) == 1
      and _run.steps[0].tool == "calc",
      str([(s.index, s.tool) for s in _run.steps]))
check("工具结果回灌给模型（第二次调用的上下文里有工具结果）",
      any("工具结果" in str(m.get("content", "")) for m in _llm.calls[1]),
      str(_llm.calls[1])[-200:])
_types = [event["type"] for event in _events]
check("事件序列：进度 → 工具调用 → 工具结果（每一步可见）",
      _types[:3] == ["agent_step", "tool_call", "tool_result"], str(_types))
check("轨迹可机器读取", _run.trace() and _run.trace()[0]["ok"] is True)
check("没有触顶", _run.hit_max_steps is False and _run.stopped is False)

_llm2 = ScriptedLLM([_tool_call] * 8)
_events2 = []
_run2 = AgentLoop(_llm2, at.ToolRegistry(), max_steps=3).run(
    [{"role": "user", "content": "循环调工具"}], on_event=_events2.append)
check("最大步数被遵守（只执行 3 次）",
      len([s for s in _run2.steps if s.tool]) == 3, str(len(_run2.steps)))
check("触顶被标记", _run2.hit_max_steps is True)
check("触顶后强制收敛：仍然给出答案", bool(_run2.answer), _run2.answer[:80])
check("收敛时明确要求「不要再调工具」",
      any("不要再调用工具" in str(m.get("content", "")) for m in _llm2.calls[-1]),
      str(_llm2.calls[-1])[-160:])

_stop = threading.Event()
_stop.set()
_llm3 = ScriptedLLM([_tool_call, "最终答案：不该出现"])
_run3 = AgentLoop(_llm3, at.ToolRegistry(), stop_event=_stop).run(
    [{"role": "user", "content": "x"}])
check("停止信号生效：一次模型都没调用", _llm3.calls == [], str(len(_llm3.calls)))
check("停止被标记", _run3.stopped is True)

_partial_stop = threading.Event()


def _emit_and_stop(event):
    if event.get("type") == "tool_result":
        _partial_stop.set()


_llm4 = ScriptedLLM([_tool_call, "最终答案：不完整"])
_run4 = AgentLoop(_llm4, at.ToolRegistry(), stop_event=_partial_stop, max_steps=4).run(
    [{"role": "user", "content": "x"}], on_event=_emit_and_stop)
check("工具执行后置位停止：不再进行下一步", _run4.stopped is True and len(_llm4.calls) == 1,
      f"calls={len(_llm4.calls)}")

_run5 = AgentLoop(ExplodingLLM(), at.ToolRegistry()).run([{"role": "user", "content": "x"}])
check("模型调用异常：不崩溃、给出原因",
      _run5.answer and "模型" in _run5.answer, _run5.answer)
check("空回复不死循环", len(ScriptedLLM([]).calls) == 0 and
      AgentLoop(ScriptedLLM([]), at.ToolRegistry(), max_steps=3).run(
          [{"role": "user", "content": "x"}]).answer != "")

_reg_denied = at.ToolRegistry()
_llm6 = ScriptedLLM(['{"tool": "web_search", "args": {"query": "x"}}', "最终答案：没搜到就用已知信息。"])
_run6 = AgentLoop(_llm6, _reg_denied).run([{"role": "user", "content": "x"}])
check("未授权的工具不会执行，但循环继续到结论",
      _run6.steps[0].ok is False and _run6.answer.startswith("没搜到"),
      f"{_run6.steps[0].observation[:40]} / {_run6.answer}")

print("=== 7. 后端接线：agent 模式的流式协议 ===")
import main as backend_main  # noqa: E402
from config import Config  # noqa: E402


def make_backend(mode="agent", replies=None, allow_sensitive=None):
    config = Config()
    config.model_path = "stub.gguf"
    config.speed_hint = False
    config.agent.max_steps = 3
    if allow_sensitive:
        config.agent.allow_sensitive = list(allow_sensitive)

    class StubLlama:
        """chat 模式会走流式路径，因此桩引擎也要有一个能吐字的底层。"""

        def __init__(self):
            self.script = ["普通回答"]

        def tokenize(self, data, **kwargs):
            return list(range(max(1, len(data or b"") // 3)))

        def create_chat_completion(self, messages=None, stream=False, **kwargs):
            text = self.script[0] if self.script else ""
            if not stream:
                return {"choices": [{"message": {"content": text}}]}
            return iter([{"choices": [{"delta": {"content": text}}]}])

    class StubEngine(backend_main.LLMEngine):
        def __init__(self):
            self.script = list(replies or [])
            self.calls = []
            self.llm = StubLlama()
            self.reasoning_enabled = False
            self.reasoning_tags = []
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

        def speed_hint(self, *a, **k):
            return ""

        def kv_cache_text(self):
            return "0 B"

        def chat_complete(self, messages, temperature=0.7, max_tokens=1024, **kwargs):
            self.calls.append([dict(m) for m in messages])
            return self.script.pop(0) if self.script else ""

    backend = backend_main.Backend(config)
    backend.llm = StubEngine()
    backend.conv.set_mode(mode)
    return backend


_backend = make_backend(replies=['{"tool": "calc", "args": {"expr": "21600/7.2"}, "why": "反算"}',
                                 "最终答案：按 7.2 的汇率，3000 美元合 21600 元。"])
_lines = list(_backend.stream({"action": "chat_stream", "message": "3000 美元是多少人民币"}))
_types = [line.get("type") for line in _lines]
check("agent 轮产出 tool_call / tool_result 事件",
      "tool_call" in _types and "tool_result" in _types, str(_types))
check("done 行带 agent 摘要（步数/工具）",
      (_lines[-1].get("agent") or {}).get("tools") == ["calc"], str(_lines[-1].get("agent")))
check("最终答案进了 done 与 delta",
      "21600" in str(_lines[-1].get("content")) and
      any("21600" in str(line.get("content")) for line in _lines if line.get("type") == "delta"))
check("对话历史记录了这一轮（用户 + 助手各一条）",
      [m["role"] for m in _backend.conv.history[-2:]] == ["user", "assistant"],
      str([m["role"] for m in _backend.conv.history]))
check("诊断接口能看到每一步",
      (_backend.agent_diagnostics().get("last_run") or {}).get("tools") == ["calc"],
      str(_backend.agent_diagnostics().get("last_run")))

_sys_prompt_sent = _backend.llm.calls[0][0]["content"] if _backend.llm.calls else ""
check("工具协议是**运行期追加**的（进了发给模型的系统提示词）",
      "[工具协议]" in _sys_prompt_sent and "[可用工具]" in _sys_prompt_sent)
check("工具协议不进模式块（守住 700 字符的提示词预算）",
      "[工具协议]" not in _backend.conv.get_system_prompt(),
      str(len(_backend.conv.get_system_prompt())))

_chat_backend = make_backend(mode="chat", replies=["普通回答"])
_chat_lines = list(_chat_backend.stream({"action": "chat_stream", "message": "你好"}))
check("chat 模式不受影响（不出现 agent 事件）",
      "tool_call" not in [line.get("type") for line in _chat_lines],
      str([line.get("type") for line in _chat_lines]))

_deny_backend = make_backend(replies=['{"tool": "web_search", "args": {"query": "x"}}',
                                      "最终答案：没有联网也先给结论。"])
_deny_lines = list(_deny_backend.stream({"action": "chat_stream", "message": "查一下"}))
_result = next((line for line in _deny_lines if line.get("type") == "tool_result"), {})
check("打包场景下敏感工具默认不可用，且如实告知",
      _result.get("ok") is False and "未启用" in str(_result.get("observation")),
      str(_result.get("observation"))[:80])

# ---------------- 任务十三：Agent 每一步的思考过程（自测问题修复-1） ----------------
# 用户报的现象是"Agent 模式下思考过程不显示"。**根因不是没产生，是没分离也没发出去**：
# Agent 走非流式 chat_complete，返回的原始文本（含 <think> 段落）被整段当作"这一步说了
# 什么"，而 agent_step 事件里又没有正文，界面自然无从渲染。下面把三件事钉住：
#   1. 思考要被分离出来、放进 reasoning；
#   2. 思考要真的发到前端（phase="thought" 事件）；
#   3. **解析只看正文** —— 思考里写"可以调用 calc 工具"不能被当成真的工具调用。
_think_backend = make_backend(replies=[
    "<think>用户问汇率，我先算一下。可以调用 calc 工具。</think>最终答案：按 7.2 折算。",
])
_think_lines = list(_think_backend.stream({"action": "chat_stream", "message": "3000 美元是多少人民币"}))
_thoughts = [line for line in _think_lines
             if line.get("type") == "agent_step" and line.get("phase") == "thought"]
check("任务十三：发出了带内容的思考事件", len(_thoughts) == 1, str(len(_thoughts)))
check("任务十三：思考事件里带 reasoning 正文",
      bool(_thoughts) and "我先算一下" in str(_thoughts[0].get("reasoning")),
      str(_thoughts[0].get("reasoning"))[:60] if _thoughts else "")
check("任务十三：思考里的「调用 calc 工具」没被误判成工具调用",
      "tool_call" not in [line.get("type") for line in _think_lines],
      str([line.get("type") for line in _think_lines]))
check("任务十三：最终答案不含思考内容",
      "我先算一下" not in str(_think_lines[-1].get("content")),
      str(_think_lines[-1].get("content"))[:80])
check("任务十三：诊断里能看到这一步确实有思考",
      bool(((_think_backend.agent_diagnostics().get("last_run") or {})
            .get("steps") or [{}])[0].get("has_reasoning")),
      str(_think_backend.agent_diagnostics().get("last_run"))[:120])

print()
print("=" * 60)
print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
if FAILED:
    for name in FAILED:
        print(f"  - {name}")
sys.exit(0 if not FAILED else 1)
