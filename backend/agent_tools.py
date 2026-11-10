"""
[EN]
Agent tool layer (v0.2.1 capability extension, task 2): tool interface, built-in
tool set, and permission tiers.

Design notes (each answers a question in the design specification that required a
DSH decision)
=========================================================================

1. Why the native llama.cpp ``tools`` / ``tool_choice`` parameters are not used
---------------------------------------------------------------------------
The underlying ``create_chat_completion`` does expose both parameters, but the
observed constraints are rigid: when a GGUF file carries
``tokenizer.chat_template``, the Jinja handler is used and ``tool_choice=None``
causes the entire tools payload to be ignored; ``tool_choice="auto"`` forces a
non-streaming decision internally and raises
``ValueError: Automatic streaming tool choice is not supported`` if the matched
function also requests ``stream=True``. In other words, whether tools are usable
depends on the chat template shipped with the model, while EchoDesk targets
arbitrary GGUF files (template support in the 0.6B class is especially
unreliable). This implementation therefore has the model emit one line of JSON
that we parse: no new dependency, valid for any GGUF, and unit-testable
(``parse_tool_call`` is a pure function). The cost is that the tool-call format
relies on prompt constraints, which is guaranteed jointly by the [output format]
section of ``MODES["agent"]`` and the "tool protocol" appended at runtime; the
parser also tolerates common deviations (code-fence wrapping, explanatory text
before or after the JSON, extraneous whitespace).

2. Permission tiers: three levels, with only the safest enabled by default
------------------------------------------------------------------------
  * ``safe``: pure computation and operations that only read content the user has
    already supplied (calculator, date, unit conversion, retrieval over uploaded
    files, reading the summary of an uploaded file). No outbound traffic, no
    arbitrary path access, no disk writes; enabled by default.
  * ``sensitive``: transmits data externally or reads arbitrary local content
    (web search, reading arbitrary local files). Disabled by default; it must be
    pre-authorized in ``agent.allow_sensitive`` in ``config.json`` or per session
    with ``/agent allow <tool>``.
  * Tools with persistent side effects, such as writing files or executing
    commands, are not provided at all in this version: minimal privilege, and no
    self-escalation path inside the product itself.

  An unauthorized tool does not fail silently: ``ToolRegistry.run`` returns an
  explanatory observation, the model continues with it (answering from known
  information), and the user can see in the conversation that the tool is not
  enabled and how to enable it.

3. Tool selection: existing capabilities plus several new tools (task requirement)
-------------------------------------------------------------------------------
Reused capabilities: ``doc_search`` (retrieval over uploaded files),
``doc_summary`` (segmented summary), ``web_search`` (web search, sensitive).
New: ``calc``, ``date``, ``convert``, ``file_read`` (sensitive). Every tool is
"one call, one string result", which keeps it easy for small models to
understand and for the interface to display.

[ZH]
Agent 工具层（v0.2.1 能力扩展，任务二）：工具接口、内置工具集与权限分级。

设计要点（每条都对应设计规范中需要由 DSH 判断的问题）
=====================================================

1. 为什么不使用 llama.cpp 原生的 ``tools`` / ``tool_choice``
底层 ``create_chat_completion`` 确实提供这两个形参，但实测约束非常严格：GGUF 带
``tokenizer.chat_template`` 时走 Jinja handler，``tool_choice=None`` 会使整段 tools
被忽略；``tool_choice="auto"`` 内部强制非流式决策，若命中函数又要求 ``stream=True``
会直接抛出 ``ValueError: Automatic streaming tool choice is not supported``。
也就是说，能否使用工具取决于模型自带的对话模板，而 EchoDesk 面向任意 GGUF
（0.6B 档位的模板支持尤其不可靠）。因此本实现让模型输出一行 JSON 并由本层解析：
零新增依赖、对任何 GGUF 都成立、可单元测试（``parse_tool_call`` 是纯函数）。
代价是工具调用格式依赖提示词约束，这一点由 ``MODES["agent"]`` 的【输出格式】
与运行时追加的「工具协议」共同保证；解析器还对常见偏差写法（代码块包裹、
前后附带解释文字、多余空白）做了容错。

2. 权限分级：三个等级，默认只开启最安全的一档
  * ``safe``：纯计算，以及只读取用户已提供内容的能力（计算器、日期、单位换算、
    在已上传文件中检索、读取已上传文件的摘要）。不外发、不读取任意路径、不写盘，
    默认可用。
  * ``sensitive``：会把数据带出本机或读取本机任意内容（联网搜索、读取任意本地文件）。
    默认关闭，需要在 ``config.json`` 的 ``agent.allow_sensitive`` 中或会话内
    用 ``/agent allow <工具>`` 预授权。
  * 写文件、执行命令这类具有持久副作用的工具，本版本一个都不提供：权限最小化，
    同时避免在产品内部留下可自我提权的入口。

  未授权的工具不会静默失败：``ToolRegistry.run`` 返回一条说明性观察结果，
  模型据此继续（用已知信息作答），用户也能在对话中看到该工具未启用及其开启方式。

3. 工具集选择：覆盖现有能力并新增若干工具（设计规范要求）
  复用现有能力：``doc_search``（检索已上传文件）、``doc_summary``（分段摘要）、
  ``web_search``（联网搜索，sensitive）。新增：``calc``、``date``、``convert``、
  ``file_read``（sensitive）。每个工具都是"一次调用、一个字符串结果"，
  便于小模型理解与界面显示。
"""
from __future__ import annotations

import ast
import datetime as _dt
import json
import math
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# [EN] Tool results are text that is pasted directly back into the model context
# [EN] and also rendered in the conversation area, so it must follow the UI language
# [EN] (v0.2.2 hardcoded-string audit, batch 6). The i18n module uses only the
# [EN] standard library, so no circular import is possible here.
# [ZH] 工具结果是直接贴回模型上下文、同时显示在对话区的文本，必须随界面语言变化
# [ZH] （v0.2.2 硬编码清查，第 6 批）。i18n 仅依赖标准库，此处不存在循环导入风险。
from i18n import t as _t

#: [EN] Locale-independent marker for permission-related observations; the front end
#: [EN] uses it to detect a denial, and it is stripped before display.
#: [ZH] 权限类观察结果的语言无关标记；前端据此判定"被拒绝"，显示前会被剥除。
DENIED_MARK = "[!DENIED] "


# ===========================================================================
# [EN] Tool interface
# [ZH] 工具接口
# ===========================================================================

#: [EN] Permission levels. ``safe`` is available by default; ``sensitive`` requires
#: [EN] pre-authorization; no tool uses ``guard`` in this version (the constant is
#: [EN] retained so configuration, documentation, and the front end share one
#: [EN] vocabulary, and a future tier has an explicit slot).
#: [ZH] 权限等级。``safe`` 默认可用；``sensitive`` 需要预授权；本版本没有使用
#: [ZH] ``guard`` 的工具（保留该常量是为了让配置、文档与前端共用统一的词表，
#: [ZH] 将来新增约束等级时也有明确的取值）。
RISK_SAFE = "safe"
RISK_SENSITIVE = "sensitive"
RISK_GUARD = "guard"


#: [EN] Textual convention for tool failure: results that start with these phrases
#: [EN] are classified as failures by ``ToolRegistry.run``.
#: [EN]
#: [EN] Why this is needed: tool implementations are contractually forbidden to raise
#: [EN] (exceptions make the agent loop harder to converge), so "cannot compute",
#: [EN] "no results", and "MCP peer error" are all returned as an explanatory text.
#: [EN] Without this distinction the interface would show all of them as successes,
#: [EN] and the user would see a tool that ran but produced nothing.
#: [EN] The convention lives here, rather than being re-implemented at each call
#: [EN] site, so that success and failure have exactly one definition.
#: [ZH] 工具"失败"的文本约定：以这些短语开头的结果会被 ``ToolRegistry.run``
#: [ZH] 判定为失败。
#: [ZH]
#: [ZH] 需要该约定的原因：工具体按约定不得抛异常（抛异常会使 Agent 循环更难收敛），
#: [ZH] 因此"无法计算""未检索到结果""MCP 对端报错"都以一段说明文本的形式返回。
#: [ZH] 若不加区分，这些情况在界面上都会显示为"成功"，用户看到的是
#: [ZH] "工具执行过但没有任何产出"。
#: [ZH] 把约定集中在此处而非在每个调用点各自判断，是为了让成功/失败只有一种口径。
_FAILURE_PREFIXES = (
    "计算失败", "换算失败", "日期计算失败", "检索失败", "摘要失败", "搜索失败",
    "读取失败", "MCP 工具调用失败", "MCP 工具调用异常", "工具执行出错",
    "命令执行失败", "Git 执行失败",
    "Command failed", "Git failed", "Command timed out",
)
_FAILURE_MARKERS = ("未启用", "已退出", "超时", "未确认", "需要确认")


def looks_like_failure(text: str) -> bool:
    """
    [EN]
    Apply the convention above to determine whether a tool result represents a
    failure.

    [ZH]
    按上述约定判断一段工具结果是否表示失败。
    """
    body = str(text or "").strip()
    if not body:
        return False
    if body.startswith(_FAILURE_PREFIXES):
        return True
    head = body[:60]
    return any(marker in head for marker in _FAILURE_MARKERS)


@dataclass
class Tool:
    """
    [EN]
    A tool that the model can invoke.

    ``run`` accepts a single argument dict and returns a string (the observation).
    The return value must be a string rather than a structured object because it is
    pasted back into the model context verbatim; the simpler the shape, the less
    likely a small model misreads it. A tool should not raise exceptions
    (``ToolRegistry.run`` also catches them); the correct way to signal an error is
    to return a sentence explaining why the call did not succeed, so the model has
    an opportunity to retry differently.

    [ZH]
    一个可供模型调用的工具。

    ``run`` 只接收一个参数 dict，返回字符串（观察结果）。返回值必须是字符串而非
    结构化对象：它要原样贴回模型上下文，形状越简单，小模型越不容易读错。
    工具不应抛出异常（``ToolRegistry.run`` 也会兜底捕获）；错误的正确表达方式是
    返回一句"为什么没有成功"的说明，让模型有机会换一种方式重试。
    """

    name: str
    #: [EN] One-line description shown to the model (part of the available-tool list
    #: [EN] in the system prompt).
    #: [ZH] 给模型看的一句话说明（进入系统提示词的「可用工具」清单）。
    description: str
    #: [EN] Argument description (same location; field names must be explicit,
    #: [EN] otherwise a small model does not know what to put in args).
    #: [ZH] 参数说明（同上；必须写明字段名，否则小模型不知道 args 中应放什么）。
    args_hint: str = ""
    risk: str = RISK_SAFE
    run: Callable[[Dict[str, Any]], str] = field(default=lambda _a: "")
    #: [EN] Example invocation (placed in the example section of the tool protocol
    #: [EN] to help the 0.6B class learn the format).
    #: [ZH] 示例调用（放入「工具协议」的示例区，帮助 0.6B 档位掌握格式）。
    example: str = ""


class ToolContext:
    """
    [EN]
    Host capabilities available to tools at runtime (injected by Backend so that the
    tool layer does not depend on main in reverse).

    Callbacks plus ready-made objects are used deliberately instead of letting tools
    import main, because the tool layer must remain unit-testable
    (``tools/test_agent.py`` builds this context from stubs directly).

    [ZH]
    工具运行时可用的宿主能力（由 Backend 注入，避免工具层反向依赖 main）。

    刻意采用"回调 + 已就绪对象"的方式，而不是让工具层 import main：工具层必须
    保持可单元测试（``tools/test_agent.py`` 直接使用桩实现构造该对象）。
    """

    def __init__(
        self,
        conv: Any = None,
        search: Any = None,
        summarizer: Any = None,
        count_tokens: Optional[Callable[[str], int]] = None,
        files_dir: str = "",
    ) -> None:
        self.conv = conv
        self.search = search
        self.summarizer = summarizer
        self.count_tokens = count_tokens
        self.files_dir = files_dir

    # [EN] ---------- Small helper methods for tools ----------
    # [ZH] ---------- 供工具使用的小工具方法 ----------

    def file_names(self) -> List[str]:
        conv = self.conv
        if conv is None:
            return []
        try:
            return [str(getattr(entry, "name", "") or "") for entry in conv.files]
        except Exception:
            return []

    def file_text(self, name: str = "") -> str:
        """
        [EN]
        Return the body text of an uploaded file (falls back to the active file when
        the name is empty or unmatched).

        [ZH]
        取某份已上传文件的正文（名字为空或找不到时取当前文件）。
        """
        conv = self.conv
        if conv is None:
            return ""
        try:
            entries = list(conv.files)
        except Exception:
            return ""
        if not entries:
            return ""
        target = None
        if name:
            for entry in entries:
                if str(getattr(entry, "name", "")) == name:
                    target = entry
                    break
            if target is None:                       # [EN] Allow a partial file-name match.
                                                     # [ZH] 允许用文件名的一部分进行匹配。
                for entry in entries:
                    if name and name in str(getattr(entry, "name", "")):
                        target = entry
                        break
        if target is None:
            index = getattr(conv, "active_index", -1)
            target = entries[index] if 0 <= index < len(entries) else entries[-1]
        # [EN] When the body is empty (content persisted to disk or only a summary is
        # [EN] available), fall back to the summary text so the tool always has input.
        # [ZH] 正文为空（已落盘或只有摘要）时退化为摘要文本，保证工具总有可用内容。
        body = str(getattr(target, "content", "") or "")
        if not body:
            body = str(getattr(target, "summary", "") or "")
        return body


# ===========================================================================
# [EN] Calculator (safe): AST whitelist evaluation instead of eval
# [ZH] 计算器（safe）：使用 AST 白名单求值，不使用 eval
# ===========================================================================

#: [EN] Allowed function names mapped to implementations. Deliberately limited to
#: [EN] math functions: no open/exec/__import__ and no attribute access, so the path
#: [EN] to arbitrary code execution is closed at the syntax level.
#: [ZH] 允许的函数名及其实现。刻意只包含数学函数：没有 open/exec/__import__，
#: [ZH] 也没有属性访问，因此在语法层面就封堵了获取任意代码执行的路径。
_CALC_FUNCS: Dict[str, Callable[..., Any]] = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil,
    "log": math.log, "log10": math.log10, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "pow": pow,
}

_CALC_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}

_CALC_UNARY = {ast.UAdd: lambda a: +a, ast.USub: lambda a: -a}

#: [EN] Upper bound for exponentiation: expressions such as ``10**10**10`` would keep
#: [EN] the interpreter busy indefinitely, which is a local denial of service.
#: [ZH] 幂运算的上限：``10**10**10`` 这类写法会让解释器长时间计算，等同本地 DoS。
_MAX_POW = 1e6


class CalcError(ValueError):
    """
    [EN]
    The calculator refused to evaluate (unsupported syntax or limit exceeded).

    [ZH]
    计算器拒绝执行（语法不受支持或超出限制）。
    """


def _calc_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _calc_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError(_t("calc.digits_only"))
        return node.value
    if isinstance(node, ast.BinOp):
        op = _CALC_BINOPS.get(type(node.op))
        if op is None:
            raise CalcError(_t("calc.bad_operator"))
        left, right = _calc_node(node.left), _calc_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > _MAX_POW):
            raise CalcError(_t("calc.pow_limit"))
        try:
            return op(left, right)
        except ZeroDivisionError as exc:
            # [EN] Evaluation itself can also fail: `1/0`, `1%0`, and `0**-1` all
            # [EN] raise. This was caught during the first test run: tool_calc only
            # [EN] handled CalcError, so ZeroDivisionError propagated to the tool
            # [EN] registry (which did catch it and report "execution error", but the
            # [EN] user then saw a broken tool rather than "this expression cannot be
            # [EN] evaluated").
            # [ZH] 求值本身也可能失败：`1/0`、`1%0`、`0**-1` 都会抛出异常。
            # [ZH] 首轮验证即发现该问题：tool_calc 当时只捕获 CalcError，
            # [ZH] ZeroDivisionError 会穿透到工具注册表（注册表虽会兜底并返回
            # [ZH] "执行出错"，但对用户而言是"工具损坏"，而不是"该表达式无法计算"）。
            raise CalcError(_t("calc.div_zero")) from exc
        except (OverflowError, ValueError, TypeError) as exc:
            raise CalcError(_t("calc.out_of_range", error=exc)) from exc
    if isinstance(node, ast.UnaryOp):
        op = _CALC_UNARY.get(type(node.op))
        if op is None:
            raise CalcError(_t("calc.bad_unary"))
        return op(_calc_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _CALC_FUNCS:
            raise CalcError(_t("calc.bad_function"))
        if node.keywords:
            raise CalcError(_t("calc.function_kwargs"))
        return _CALC_FUNCS[node.func.id](*[_calc_node(a) for a in node.args])
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_calc_node(e) for e in node.elts]
    # [EN] Attribute access, subscripting, comprehensions, lambda, and bare names
    # [EN] (variables) are rejected: they are the common springboards that turn an
    # [EN] expression into arbitrary code (a.b.c, d["x"], __class__, ...).
    # [ZH] 属性访问、下标、推导式、lambda 与裸名字（变量）一律拒绝：
    # [ZH] 它们是把表达式变为任意代码的常见跳板（a.b.c、d["x"]、__class__ 等）。
    raise CalcError(_t("calc.bad_chars"))


def calc_eval(expr: str) -> float:
    """
    [EN]
    Safely evaluate an arithmetic expression (math functions allowed) and return a
    numeric value.

    ``eval`` is deliberately avoided: inputs such as
    ``__import__('os').system(...)`` have appeared in real environments, and local
    small models are highly susceptible to suggestion from text inside documents.

    [ZH]
    安全求值一个算术表达式（允许数学函数），返回数值。

    刻意不使用 ``eval``：``__import__('os').system(...)`` 这类输入在真实环境中
    已经出现过，而本地小模型极易被文档中的文字诱导。
    """
    text = str(expr or "").strip().replace("×", "*").replace("÷", "/")
    if not text:
        raise CalcError(_t("calc.empty"))
    if len(text) > 200:
        raise CalcError(_t("calc.too_long"))
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise CalcError(_t("calc.parse_error", error=exc.msg)) from exc
    value = _calc_node(tree)
    if isinstance(value, list):
        raise CalcError(_t("calc.list_result"))
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise CalcError(_t("calc.not_finite"))
    return value


def _fmt_number(value: Any) -> str:
    """
    [EN]
    Format a numeric value for display (integers without a decimal point; floats
    with at most six decimal places).

    [ZH]
    把数值格式化为便于阅读的字符串（整数不带小数点，浮点数最多保留 6 位小数）。
    """
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-12 and abs(value) < 1e15:
            return str(int(round(value)))
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def tool_calc(args: Dict[str, Any]) -> str:
    expr = args.get("expr", args.get("expression", ""))
    try:
        value = calc_eval(str(expr))
    except CalcError as exc:
        return _t("calc.failed_hint", error=exc)
    except Exception as exc:                     # [EN] Catch-all: tools never raise.
                                                 # [ZH] 兜底：工具绝不抛出异常。
        return _t("calc.failed", error=exc)
    return f"{expr} = {_fmt_number(value)}"


# ===========================================================================
# [EN] Date and time (safe)
# [ZH] 日期与时间（safe）
# ===========================================================================

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")

def _weekday_name(index: int) -> str:
    """
    [EN]
    Display name of a weekday, read as a comma-separated list from the language
    pack (previously a hardcoded Chinese tuple).

    [ZH]
    星期几的显示名：从语言包中按逗号分隔列表读取（原先为写死的中文元组）。
    """
    names = _t("date.weekdays").split(",")
    return names[index] if 0 <= index < len(names) else ""


def _parse_date(text: str) -> _dt.date:
    raw = str(text or "").strip()
    if not raw or raw.lower() in ("today", "now", "今天"):
        return _dt.date.today()
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(_t("date.bad_input", text=text))


def tool_date(args: Dict[str, Any]) -> str:
    """
    [EN]
    Date tool: ``op`` supports today (default) / add / diff / weekday.

    [ZH]
    日期工具：``op`` 支持 today（默认）/ add / diff / weekday。
    """
    op = str(args.get("op") or args.get("action") or "today").strip().lower()
    try:
        if op in ("today", "now", "current"):
            today = _dt.date.today()
            return _t("date.today", date=today.isoformat(),
                      weekday=_weekday_name(today.weekday()),
                      yday=today.timetuple().tm_yday)
        if op in ("add", "plus", "offset"):
            base = _parse_date(str(args.get("from") or args.get("date") or ""))
            days = int(args.get("days") or 0)
            weeks = int(args.get("weeks") or 0)
            months = int(args.get("months") or 0)
            result = base + _dt.timedelta(days=days + weeks * 7)
            if months:
                month = result.month - 1 + months
                year = result.year + month // 12
                month = month % 12 + 1
                day = min(result.day, _month_days(year, month))
                result = _dt.date(year, month, day)
            return _t("date.add", base=base.isoformat(), months=months, weeks=weeks,
                      days=days, result=result.isoformat(),
                      weekday=_weekday_name(result.weekday()))
        if op in ("diff", "between", "sub"):
            first = _parse_date(str(args.get("a") or args.get("start") or ""))
            second = _parse_date(str(args.get("b") or args.get("end") or ""))
            days = (second - first).days
            return _t("date.diff", first=first.isoformat(), second=second.isoformat(),
                      days=days, months=f"{days / 30.44:.1f}")
        if op in ("weekday", "whatday"):
            target = _parse_date(str(args.get("date") or ""))
            return _t("date.weekday", date=target.isoformat(),
                      weekday=_weekday_name(target.weekday()))
    except (ValueError, TypeError) as exc:
        return _t("date.failed", error=exc)
    return _t("date.bad_op")


def _month_days(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (_dt.date(year + (1 if month == 12 else 0), (month % 12) + 1, 1)
            - _dt.timedelta(days=1)).day


# ===========================================================================
# [EN] Unit conversion (safe)
# [ZH] 单位换算（safe）
# ===========================================================================

#: [EN] Factor from each unit to the base unit of its category (temperature is
#: [EN] handled separately because it requires an offset rather than a factor).
#: [ZH] 每类单位到基准单位的倍率（温度需要偏移而非倍率，单独处理）。
_UNITS: Dict[str, Dict[str, float]] = {
    "length": {"m": 1, "km": 1000, "cm": 0.01, "mm": 0.001, "um": 1e-6,
               "mi": 1609.344, "yd": 0.9144, "ft": 0.3048, "in": 0.0254},
    "mass": {"kg": 1, "g": 0.001, "mg": 1e-6, "t": 1000, "lb": 0.45359237, "oz": 0.028349523125},
    "area": {"m2": 1, "km2": 1e6, "cm2": 1e-4, "ha": 10000, "亩": 666.6666667,
             "ft2": 0.09290304, "acre": 4046.8564224},
    "volume": {"l": 1, "ml": 0.001, "m3": 1000, "gal": 3.785411784, "qt": 0.946352946},
    "data": {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4,
             "kib": 1024, "mib": 1024 ** 2},
    "time": {"s": 1, "min": 60, "h": 3600, "day": 86400, "week": 604800},
    "speed": {"m/s": 1, "km/h": 1 / 3.6, "mph": 0.44704, "knot": 0.514444},
    "pressure": {"pa": 1, "kpa": 1000, "mpa": 1e6, "bar": 1e5, "atm": 101325, "mmhg": 133.322},
}

#: [EN] Unit aliases (Chinese names and common spellings mapped to table keys).
#: [ZH] 单位别名（中文名称及常见写法映射到表中的键）。
_UNIT_ALIASES = {
    "米": "m", "千米": "km", "公里": "km", "厘米": "cm", "毫米": "mm", "微米": "um",
    "英里": "mi", "码": "yd", "英尺": "ft", "英寸": "in",
    "千克": "kg", "公斤": "kg", "克": "g", "毫克": "mg", "吨": "t", "磅": "lb", "盎司": "oz",
    "平方米": "m2", "平方千米": "km2", "平方公里": "km2", "平方厘米": "cm2",
    "公顷": "ha", "平方英尺": "ft2", "英亩": "acre",
    "升": "l", "毫升": "ml", "立方米": "m3", "加仑": "gal",
    "字节": "b", "千字节": "kb", "兆字节": "mb", "吉字节": "gb", "太字节": "tb",
    "秒": "s", "分钟": "min", "小时": "h", "天": "day", "周": "week",
    "帕": "pa", "千帕": "kpa", "兆帕": "mpa", "巴": "bar", "标准大气压": "atm", "毫米汞柱": "mmhg",
    "摄氏度": "c", "华氏度": "f", "开尔文": "k",
}


def _normalize_unit(text: str) -> str:
    raw = str(text or "").strip().lower().replace("°", "")
    return _UNIT_ALIASES.get(raw, raw)


def tool_convert(args: Dict[str, Any]) -> str:
    try:
        value = float(args.get("value"))
    except (TypeError, ValueError):
        return _t("convert.bad_value")
    src = _normalize_unit(str(args.get("from") or args.get("unit") or ""))
    dst = _normalize_unit(str(args.get("to") or ""))
    if not src or not dst:
        return _t("convert.missing_args")
    # [EN] Temperature requires an offset, so it is computed separately.
    # [ZH] 温度需要偏移量，单独计算。
    if src in ("c", "f", "k") or dst in ("c", "f", "k"):
        if src not in ("c", "f", "k") or dst not in ("c", "f", "k"):
            return _t("convert.temp_mismatch")
        celsius = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[src]
        out = {"c": celsius, "f": celsius * 9 / 5 + 32, "k": celsius + 273.15}[dst]
        return f"{_fmt_number(value)} {src.upper()} = {_fmt_number(round(out, 6))} {dst.upper()}"
    for category, table in _UNITS.items():
        if src in table and dst in table:
            out = value * table[src] / table[dst]
            return f"{_fmt_number(value)} {src} = {_fmt_number(round(out, 10))} {dst}"
        if src in table and dst not in table and dst not in _all_units():
            return _t("convert.unknown_unit", unit=dst,
                      units=", ".join(sorted(table)))
    return _t("convert.mismatch", src=src, dst=dst)


def _all_units() -> set:
    units = set()
    for table in _UNITS.values():
        units |= set(table)
    return units | {"c", "f", "k"}


# ===========================================================================
# [EN] Uploaded files: retrieval and summaries (safe)
# [ZH] 已上传文件：检索与摘要（safe）
# ===========================================================================

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；;!?\n])")


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and s.strip()]


def _keyword_score(query: str, sentence: str) -> float:
    """
    [EN]
    Minimal relevance score: the number of occurrences in the sentence of query
    terms (two-character sliding windows for Chinese, words for English).

    Nothing beyond this is introduced deliberately: the tool layer must have zero
    dependencies and millisecond latency. Retrieval quality is the responsibility of
    ``retrieval.py`` (RAG depth); this function only needs to pick out relevant
    passages.

    [ZH]
    极简相关度：查询中的词（中文按 2 字滑窗、英文按词）在句子中出现的次数。

    刻意不引入额外组件：工具层要求零依赖、毫秒级延迟。检索质量由
    ``retrieval.py``（RAG 深度）承担，这里只需保证能挑出相关段落。
    """
    if not query or not sentence:
        return 0.0
    terms: List[str] = []
    lowered = query.lower()
    terms += [w for w in re.findall(r"[a-z0-9_]+", lowered) if len(w) > 1]
    han = re.findall(r"[\u4e00-\u9fff]+", query)
    for run in han:
        if len(run) <= 2:
            terms.append(run)
        else:
            terms += [run[i:i + 2] for i in range(len(run) - 1)]
    score = 0.0
    target = sentence.lower()
    for term in set(terms):
        count = target.count(term)
        if count:
            score += count * (1.0 + 0.2 * (len(term) - 2))
    return score


def tool_doc_search(args: Dict[str, Any], ctx: ToolContext) -> str:
    """
    [EN]
    Select relevant passages from uploaded files by keyword (no outbound traffic, no
    arbitrary path access).

    [ZH]
    在已上传文件中按关键词挑选相关段落（不外发数据、不读取任意路径）。
    """
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query:
        return _t("tool.retrieve_need_query")
    top_k = int(args.get("top") or 3)
    top_k = max(1, min(top_k, 8))
    names = ctx.file_names()
    if not names:
        return _t("tool.retrieve_no_files")
    wanted = str(args.get("file") or "").strip()
    hits: List[Tuple[float, str, str]] = []
    for name in names:
        if wanted and wanted not in name:
            continue
        body = ctx.file_text(name)
        if not body:
            continue
        for sentence in _sentences(body):
            score = _keyword_score(query, sentence)
            if score > 0:
                hits.append((score, name, sentence))
    if not hits:
        return _t("tool.retrieve_no_match", query=query, count=len(names))
    hits.sort(key=lambda item: -item[0])
    lines = [f"在已上传文件里找到 {len(hits)} 段相关内容，最相关的 {min(top_k, len(hits))} 段：" ]
    for index, (score, name, sentence) in enumerate(hits[:top_k], 1):
        text = sentence if len(sentence) <= 200 else sentence[:200] + "…"
        lines.append(f"[{index}] 文件《{name}》：{text}")
    return "\n".join(lines)


def tool_doc_summary(args: Dict[str, Any], ctx: ToolContext) -> str:
    """
    [EN]
    Return the summary already computed for an uploaded file (no new model call is
    triggered, which avoids a model invocation nested inside a tool).

    [ZH]
    取某份已上传文件已有的摘要（不触发新的模型调用，避免在工具内嵌套模型调用）。
    """
    names = ctx.file_names()
    if not names:
        return _t("tool.summary_no_files")
    wanted = str(args.get("file") or "").strip()
    conv = ctx.conv
    entries = list(getattr(conv, "files", []) or [])
    for entry in entries:
        name = str(getattr(entry, "name", ""))
        if wanted and wanted not in name:
            continue
        summary = str(getattr(entry, "summary", "") or "")
        chars = int(getattr(entry, "chars", 0) or 0)
        if summary:
            return _t("tool.summary_ready", name=name, chars=chars, summary=summary)
        body = str(getattr(entry, "content", "") or "")
        if body:
            head = body[:600]
            return (f"《{name}》还没有生成分段摘要（原文 {chars} 字符）。"
                    f"先给开头 600 字，需要全篇请让用户用 /summarize：\n{head}")
        stored = str(getattr(entry, "stored_at", "") or "")
        if stored:
            return _t("tool.summary_stored_only", name=name, stored=stored)
    return _t("tool.summary_not_found",
              wanted=wanted or _t("tool.summary_unspecified"), names=", ".join(names))


# ===========================================================================
# [EN] Web search (sensitive) and local file reading (sensitive)
# [ZH] 联网搜索（sensitive）与本地文件读取（sensitive）
# ===========================================================================

def tool_web_search(args: Dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query:
        return _t("tool.search_need_query")
    if ctx.search is None:
        return _t("tool.search_no_component")
    try:
        outcome = ctx.search.search(query)
    except Exception as exc:                     # [EN] A search failure must not
                                                 # [EN] abort the whole turn.
                                                 # [ZH] 搜索失败不得导致整轮对话中断。
        return _t("tool.search_failed", error=exc)
    if not getattr(outcome, "ok", False):
        return _t("tool.search_failed_hint",
                  error=getattr(outcome, "error", "") or _t("tool.search_unknown_reason"))
    results = list(getattr(outcome, "results", []) or [])
    if not results:
        return _t("tool.search_no_result", query=query)
    lines = [f"搜索「{query}」返回 {len(results)} 条结果："]
    for index, item in enumerate(results[:5], 1):
        snippet = str(getattr(item, "snippet", "") or "").strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        lines.append(f"[{index}] {getattr(item, 'title', '')}\n    {snippet}")
    return "\n".join(lines)


#: [EN] Whitelist of text file extensions: ``file_read`` reads only these; binary and
#: [EN] executable files are always rejected.
#: [ZH] 文本类扩展名白名单：``file_read`` 只读取这些扩展名，二进制与可执行文件一律拒绝。
_READABLE_EXTS = (".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ".ini",
                  ".py", ".js", ".ts", ".cs", ".java", ".c", ".h", ".cpp", ".go", ".rs",
                  ".html", ".xml", ".sql", ".bat", ".ps1", ".sh")


def tool_file_read(args: Dict[str, Any], ctx: ToolContext) -> str:
    path = str(args.get("path") or "").strip().strip('"')
    if not path:
        return _t("tool.read_need_path")
    if not os.path.isabs(path):
        base = ctx.files_dir or os.getcwd()
        path = os.path.join(base, path)
    if not os.path.isfile(path):
        return _t("tool.read_not_found", path=path)
    if os.path.splitext(path)[1].lower() not in _READABLE_EXTS:
        return _t("tool.read_text_only", exts=", ".join(_READABLE_EXTS[:6]))
    try:
        size = os.path.getsize(path)
        if size > 512 * 1024:
            return _t("tool.read_too_large", size=size, limit="512 KB")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    except OSError as exc:
        return _t("tool.read_failed", error=exc)
    head = body[:2000]
    tail = "" if len(body) <= 2000 else _t("tool.read_truncated", chars=len(body))
    return _t("tool.read_content", name=os.path.basename(path), head=head, tail=tail)


# ===========================================================================
# [EN] Guarded system tools (v0.2.6 task 2.4)
# [ZH] 高危系统工具（v0.2.6 任务 2.4）
# ===========================================================================
#
# [EN] These tools have persistent side effects and are therefore classified as
# [EN] ``guard``: the registry refuses to run them unless the caller explicitly
# [EN] passes ``confirmed=True``. The Agent loop obtains that flag only after the
# [EN] user approves the ``confirm_tool`` event in the UI. A direct call without
# [EN] confirmation is denied, which is the defence-in-depth invariant tested by
# [EN] ``tools/test_agent_guard.py``.
# [ZH] 这些工具具有持久副作用，因此归类为 ``guard``：除非调用方显式传入
# [ZH] ``confirmed=True``，注册表拒绝执行。Agent 循环仅在用户于界面批准
# [ZH] ``confirm_tool`` 事件后才取得该标记。未经确认的直接调用一律拒绝，这是
# [ZH] ``tools/test_agent_guard.py`` 断言的分层防御不变量。

#: [EN] Maximum characters of command output echoed back into the model context.
#: [ZH] 回灌到模型上下文的命令输出字符上限。
_SYSTEM_OUTPUT_CHARS = 4000

#: [EN] Command timeout bounds in seconds.
#: [ZH] 命令超时上下限（秒）。
_MIN_COMMAND_TIMEOUT = 5
_MAX_COMMAND_TIMEOUT = 120


def _clamp_timeout(value: Any) -> int:
    """
    [EN]
    Clamp a caller-supplied timeout to the supported range.

    [ZH]
    把调用方给出的超时限制到支持区间。
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = 30
    return max(_MIN_COMMAND_TIMEOUT, min(_MAX_COMMAND_TIMEOUT, seconds))


def _run_system_command(argv: List[str], cwd: str, timeout: int):
    """
    [EN]
    Run a process with captured output and no shell interpretation.

    ``argv`` is always a list, so the executable and its arguments cannot be
    altered by shell metacharacters. Only the command string handed to the
    explicitly chosen shell is interpreted by that shell, which is the intended
    semantics of ``execute_shell``.

    [ZH]
    以"参数列表、不经过 shell 解释"的方式运行进程。

    ``argv`` 始终为列表，可执行文件与参数不会被 shell 元字符改写。只有交给
    显式选定 shell 的命令字符串由该 shell 解释，这正是 ``execute_shell`` 的
    预期语义。
    """
    kwargs: Dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "cwd": cwd,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, **kwargs)


def _clip_output(text: str) -> str:
    """
    [EN]
    Limit command output to the configured context budget.

    [ZH]
    把命令输出限制在上下文预算之内。
    """
    body = str(text or "").strip()
    if len(body) <= _SYSTEM_OUTPUT_CHARS:
        return body
    return body[:_SYSTEM_OUTPUT_CHARS] + _t("tool.output_truncated",
                                            chars=len(body))


def tool_execute_shell(args: Dict[str, Any]) -> str:
    """
    [EN]
    Execute one command in PowerShell (Windows) or Bash (POSIX).

    The shell is selected from an explicit whitelist; no other interpreter is
    used. The command string is the only shell-interpreted value, which is the
    documented contract of this tool, and execution is gated by the per-call
    confirmation described above.

    [ZH]
    在 PowerShell（Windows）或 Bash（POSIX）中执行一条命令。

    shell 从显式白名单中选择，不使用其他解释器。命令字符串是唯一交由 shell
    解释的值，这是本工具文档化的契约；执行受前述逐次确认闸门约束。
    """
    command = str(args.get("command") or args.get("cmd") or "").strip()
    if not command:
        return _t("tool.shell_need_command")
    timeout = _clamp_timeout(args.get("timeout_seconds"))
    cwd = str(args.get("cwd") or os.getcwd())
    if not os.path.isdir(cwd):
        return _t("tool.shell_bad_cwd", cwd=cwd)
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        argv = [shell, "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        shell = shutil.which("bash") or shutil.which("sh")
        argv = [shell, "-lc", command]
    if not shell:
        return _t("tool.shell_no_shell")
    try:
        completed = _run_system_command(argv, cwd, timeout)
    except subprocess.TimeoutExpired:
        return _t("tool.shell_timeout", seconds=timeout)
    except OSError as exc:
        return _t("tool.shell_failed", error=exc)
    output = _clip_output(f"{completed.stdout or ''}{completed.stderr or ''}")
    if completed.returncode != 0:
        return _t("tool.shell_failed", error=_t(
            "tool.shell_exit_code", code=completed.returncode, output=output))
    return _t("tool.shell_result", code=completed.returncode,
              output=output or _t("tool.shell_empty"))


def tool_git_command(args: Dict[str, Any]) -> str:
    """
    [EN]
    Run one Git command in a caller-specified working directory.

    Arguments are supplied either as a list or as a shell-like string. The
    string form is tokenized locally with ``shlex`` and never passed through a
    shell, so quoting cannot introduce an additional command.

    [ZH]
    在调用方指定的工作目录中执行一条 Git 命令。

    参数可以传列表或类 shell 字符串。字符串形式在本地用 ``shlex`` 分词，
    绝不经过 shell，因此引号无法引入额外命令。
    """
    raw = args.get("args")
    if raw is None:
        raw = args.get("command")
    if isinstance(raw, list):
        parts = [str(item) for item in raw if str(item)]
    else:
        parts = shlex.split(str(raw or ""))
    if not parts:
        return _t("tool.git_need_args")
    executable = shutil.which("git")
    if not executable:
        return _t("tool.git_no_git")
    timeout = _clamp_timeout(args.get("timeout_seconds"))
    cwd = str(args.get("cwd") or os.getcwd())
    if not os.path.isdir(cwd):
        return _t("tool.shell_bad_cwd", cwd=cwd)
    try:
        completed = _run_system_command([executable] + parts, cwd, timeout)
    except subprocess.TimeoutExpired:
        return _t("tool.git_timeout", seconds=timeout)
    except OSError as exc:
        return _t("tool.git_failed", error=exc)
    output = _clip_output(f"{completed.stdout or ''}{completed.stderr or ''}")
    if completed.returncode != 0:
        return _t("tool.git_failed", error=_t(
            "tool.shell_exit_code", code=completed.returncode, output=output))
    return _t("tool.git_result", code=completed.returncode,
              output=output or _t("tool.shell_empty"))


# ===========================================================================
# [EN] Registry
# [ZH] 注册表
# ===========================================================================

class ToolRegistry:
    """
    [EN]
    Tool registry, permission checks, and the unified invocation entry point.

    [ZH]
    工具注册表、权限检查与统一调用入口。
    """

    def __init__(self, tools: Optional[List[Tool]] = None,
                 allowed_sensitive: Optional[List[str]] = None,
                 ctx: Optional[ToolContext] = None) -> None:
        self.tools: Dict[str, Tool] = {}
        self.allowed_sensitive: set = {str(x).strip().lower() for x in (allowed_sensitive or [])}
        self.ctx = ctx or ToolContext()
        self.calls: List[Dict[str, Any]] = []          # [EN] Invocation audit log.
                                                       # [ZH] 调用审计记录（可观测性）。
        for tool in tools if tools is not None else default_tools():
            self.register(tool)

    # [EN] ---------- Registration and lookup ----------
    # [ZH] ---------- 注册与查询 ----------

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def names(self) -> List[str]:
        return list(self.tools)

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(str(name or "").strip())

    def available(self) -> List[Tool]:
        """
        [EN]
        Tools that are actually invocable under the current permissions; exactly
        these are written into the prompt.

        [ZH]
        当前真正可调用的工具（已考虑权限），写入提示词的就是这些。
        """
        return [tool for tool in self.tools.values() if self.is_allowed(tool)]

    def is_allowed(self, tool: Tool) -> bool:
        # [EN] Guard tools are listed and callable in principle, but
        # [EN] ``run()`` refuses to execute them without an explicit
        # [EN] per-invocation confirmation (v0.2.6 task 2.4).
        # [ZH] 高危工具在原则上可见且可调用，但 ``run()`` 在没有逐次显式确认时
        # [ZH] 拒绝执行（v0.2.6 任务 2.4）。
        if tool.risk == RISK_GUARD:
            return True
        if tool.risk == RISK_SENSITIVE:
            return tool.name.lower() in self.allowed_sensitive
        return True

    def allow(self, name: str, allowed: bool = True) -> bool:
        """
        [EN]
        Session- or configuration-level pre-authorization switch. Returns whether the
        name matched a registered tool.

        [ZH]
        会话级或配置级的预授权开关。返回是否匹配到某个已注册的工具。
        """
        tool = self.get(name)
        if tool is None:
            return False
        key = tool.name.lower()
        if allowed:
            self.allowed_sensitive.add(key)
        else:
            self.allowed_sensitive.discard(key)
        return True

    # [EN] ---------- Invocation ----------
    # [ZH] ---------- 调用 ----------

    def run(self, name: str, args: Optional[Dict[str, Any]] = None,
            confirmed: bool = False) -> Tuple[bool, str]:
        """
        [EN]
        Execute a tool and return ``(succeeded, observation text)``.

        ``confirmed`` is the per-invocation approval produced by the user's
        response to a ``confirm_tool`` event. Guard tools are refused unless it
        is true, so no code path can execute a system command merely by calling
        ``run``.

        A text result is returned in every case (no exception, no silent failure):
        * Insufficient permission -> explains that the tool is not enabled and how to
          enable it, so the model can choose another approach;
        * An exception inside the tool -> caught and converted into a failure
          explanation, and also recorded in the ``calls`` audit log.

        [ZH]
        执行一个工具，返回 ``(是否成功, 观察结果文本)``。

        ``confirmed`` 是用户对 ``confirm_tool`` 事件作出批准后产生的逐次授权。
        高危工具在该值为真之前一律拒绝执行，因此不存在“仅调用 run 就能执行系统
        命令”的代码路径。

        任何情况下都返回文本（不抛异常、不静默失败）：
        * 权限不足 → 说明"未启用 + 如何启用"，模型可据此改用其他办法；
        * 工具内部抛出异常 → 兜底捕获并写成失败说明，同时记入 ``calls`` 审计。
        """
        tool = self.get(name)
        args = args if isinstance(args, dict) else {}
        if tool is None:
            known = ", ".join(self.names())
            self.calls.append({"tool": name, "ok": False, "reason": "unknown"})
            return False, _t("tool.unknown_tool", name=name, known=known)
        if not self.is_allowed(tool):
            self.calls.append({"tool": name, "ok": False, "reason": "denied"})
            if tool.risk == RISK_SENSITIVE:
                return False, (f"工具「{name}」属于敏感操作（{tool.description}），"
                               f"当前未启用。请在设置里或让用户用 /agent allow {name} 开启；"
                               f"现在请基于已有信息作答或换用其它工具。")
            return False, DENIED_MARK + _t("tool.unavailable", name=name)
        if tool.risk == RISK_GUARD and not confirmed:
            self.calls.append({"tool": name, "ok": False, "reason": "unconfirmed"})
            return False, DENIED_MARK + _t("tool.confirm_required", name=name)
        try:
            if tool.name in _CTX_TOOLS:
                result = tool.run(args, self.ctx)
            else:
                result = tool.run(args)
            text = str(result if result is not None else "")
        except Exception as exc:                       # [EN] Tool exception isolation
                                                       # [EN] (task requirement).
                                                       # [ZH] 工具异常隔离（设计规范要求）。
            self.calls.append({"tool": name, "ok": False, "reason": "error", "error": str(exc)})
            return False, _t("tool.exec_failed", name=name, error=exc)
        # [EN] Tools do not raise by contract; failures are reported as explanatory
        # [EN] text. The convention is applied here, because otherwise "cannot
        # [EN] compute" or "MCP peer error" would appear as success in the interface
        # [EN] (see _FAILURE_PREFIXES).
        # [ZH] 工具按约定不抛异常，失败以说明文本返回；这里按统一约定识别，
        # [ZH] 否则"无法计算""MCP 对端报错"在界面上会显示为成功（见 _FAILURE_PREFIXES）。
        if looks_like_failure(text):
            self.calls.append({"tool": name, "ok": False, "reason": "reported"})
            return False, text
        self.calls.append({"tool": name, "ok": True, "args": args})
        return True, text

    def public_list(self) -> List[Dict[str, Any]]:
        """
        [EN]
        Tool listing for the interface and documentation, including permission tier
        and enabled state.

        [ZH]
        供界面与文档使用的工具清单（包含权限等级与启用状态）。
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "args_hint": tool.args_hint,
                "risk": tool.risk,
                "enabled": self.is_allowed(tool),
                "example": tool.example,
            }
            for tool in self.tools.values()
        ]

    def protocol_text(self) -> str:
        """
        [EN]
        The "tool protocol + available tools" section appended to the system prompt at
        runtime.

        Why it is assembled here instead of being written into ``MODES["agent"]``:
        the available-tool list is dynamic (it depends on permission switches),
        whereas mode blocks are capped at 700 characters and share a single audit
        baseline across all modes. Runtime appending neither consumes that budget nor
        produces a prompt that lists tools which are not enabled.

        [ZH]
        运行时追加到系统提示词的「工具协议 + 可用工具」段落。

        为什么在此处生成而不写进 ``MODES["agent"]``：可用工具清单是动态的
        （受权限开关影响），而模式块有 700 字符的长度上限，且所有模式共用同一份
        审计口径。运行期追加既不占用该预算，也不会出现"提示词中列出了未启用工具"
        的情况。
        """
        enabled = self.available()
        lines = ["[工具协议]", "需要工具时，只输出**一行** JSON，不要输出别的文字：",
                 '{"tool": "工具名", "args": {...}, "why": "一句话为什么"}',
                 "收到「工具结果」后继续：可以再调一个工具，或输出以「最终答案：」开头的结论。",
                 "一次只调一个工具；工具没启用或失败时，用已有信息作答并说明。",
                 # [EN] This rule comes from a test run: for the question
                 # [EN] "total minus fee", the 0.6B class computed the fee
                 # [EN] (3000 x 7.2 x 0.05 = 1080) and reported it as the final
                 # [EN] answer, while the tool invocation itself was correct.
                 # [EN] Rather than guessing where the model misread the task, the
                 # [EN] requirement to re-read the question before answering is stated
                 # [EN] explicitly.
                 # [ZH] 该要求来自验证：在一次"总额 − 手续费"的提问中，0.6B 档位
                 # [ZH] 算出了手续费（3000×7.2×0.05 = 1080）并把它当作最终结论，
                 # [ZH] 而工具调用本身完全正确。与其推测模型在哪一步理解有误，
                 # [ZH] 不如把"作答前重新阅读问题"写成一条明确要求。
                 "给出「最终答案：」之前，再读一遍用户的问题，确认你算的量与他问的量一致"
                 "（要总额还是差额、要比例还是金额、有没有算漏一步）。"]
        if enabled:
            lines.append("[可用工具]")
            for tool in enabled:
                hint = f" 参数：{tool.args_hint}" if tool.args_hint else ""
                lines.append(f"- {tool.name}：{tool.description}{hint}")
        else:
            lines.append("[可用工具]（当前没有启用的工具，请直接作答）")
        disabled = [tool for tool in self.tools.values() if not self.is_allowed(tool)]
        if disabled:
            lines.append("[未启用的工具] " + ", ".join(
                f"{tool.name}（{tool.risk}）" for tool in disabled))
        example = next((tool.example for tool in enabled if tool.example), "")
        if example:
            lines.append(f"[格式示例] {example}")
        return "\n".join(lines)


#: [EN] Tools that require an injected context (signature ``(args, ctx)``); all other
#: [EN] tools receive args only. A lookup table is used instead of signature
#: [EN] inspection because introspection is fragile under packaging and decorators,
#: [EN] whereas this table shows at registry construction time which tools touch host
#: [EN] state.
#: [ZH] 需要注入上下文的工具（签名为 ``(args, ctx)``）。其余工具只接收 args。
#: [ZH] 使用查表而非签名检查：签名检查在打包与装饰器环境下容易失效，
#: [ZH] 而该表在构造注册表时即可一眼看清哪些工具会访问宿主状态。
_CTX_TOOLS = {"doc_search", "doc_summary", "web_search", "file_read"}


def default_tools() -> List[Tool]:
    """
    [EN]
    Built-in tool set (the task requires coverage of existing capabilities plus
    several new tools).

    [ZH]
    内置工具集（设计规范要求"至少覆盖现有能力并新增若干工具"）。
    """
    return [
        Tool(
            name="calc",
            description=_t("tool.calc_desc"),
            args_hint=_t("tool.calc_args"),
            risk=RISK_SAFE,
            run=tool_calc,
            example=_t("tool.calc_example"),
        ),
        Tool(
            name="date",
            description=_t("tool.date_desc"),
            args_hint='{"op": "today|add|diff|weekday", "from": "2026-03-15", "days": 30, "a": "…", "b": "…"}',
            risk=RISK_SAFE,
            run=tool_date,
            example=_t("tool.date_example"),
        ),
        Tool(
            name="convert",
            description=_t("tool.convert_desc"),
            args_hint='{"value": 3, "from": "kg", "to": "lb"}',
            risk=RISK_SAFE,
            run=tool_convert,
            example=_t("tool.convert_example"),
        ),
        Tool(
            name="doc_search",
            description=_t("tool.retrieve_desc"),
            args_hint=_t("tool.retrieve_args"),
            risk=RISK_SAFE,
            run=tool_doc_search,
        ),
        Tool(
            name="doc_summary",
            description=_t("tool.summary_desc"),
            args_hint=_t("tool.summary_args"),
            risk=RISK_SAFE,
            run=tool_doc_summary,
        ),
        Tool(
            name="web_search",
            description=_t("tool.search_desc"),
            args_hint=_t("tool.search_args"),
            risk=RISK_SENSITIVE,
            run=tool_web_search,
        ),
        Tool(
            name="file_read",
            description=_t("tool.read_desc"),
            args_hint=_t("tool.read_args"),
            risk=RISK_SENSITIVE,
            run=tool_file_read,
        ),
        Tool(
            name="execute_shell",
            description=_t("tool.shell_desc"),
            args_hint=_t("tool.shell_args"),
            risk=RISK_GUARD,
            run=tool_execute_shell,
            example=_t("tool.shell_example"),
        ),
        Tool(
            name="git_command",
            description=_t("tool.git_desc"),
            args_hint=_t("tool.git_args"),
            risk=RISK_GUARD,
            run=tool_git_command,
            example=_t("tool.git_example"),
        ),
    ]


# ===========================================================================
# [EN] Model output -> tool call (pure functions, unit-testable)
# [ZH] 模型输出 → 工具调用（纯函数，可单元测试）
# ===========================================================================

_TOOL_LINE = re.compile(r"\{\s*\"(?:tool|tool_name|name)\"\s*:", re.S)


def _strip_code_fence(text: str) -> str:
    """
    [EN]
    Remove a ```json ... ``` fence (small models frequently add one).

    [ZH]
    去掉 ```json … ``` 包裹（小模型经常添加代码块）。
    """
    match = re.search(r"```[a-zA-Z]*\s*(.+?)```", text or "", re.S)
    return match.group(1) if match else (text or "")


def _json_objects(text: str) -> List[str]:
    """
    [EN]
    Extract every brace-balanced JSON object literal from a text (braces inside
    strings are not counted).

    [ZH]
    从一段文本中切分出所有括号平衡的 JSON 对象字面量（字符串内的括号不计入）。
    """
    out: List[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = index
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start:index + 1])
                    start = -1
    return out


def parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """
    [EN]
    Parse a tool call from model output; return ``None`` when the text is not a tool
    call.

    Tolerance cases (shapes small models were observed to produce):
      * wrapped in a ```json code fence;
      * explanatory text such as "let me calculate" before or after the JSON;
      * key names written as ``tool_name`` / ``name``;
      * args flattened at the top level (``{"tool":"calc","expr":"1+1"}``).
    A parse failure always returns None (the text is treated as a normal answer) and
    never raises.

    [ZH]
    从模型输出中解析出工具调用；若不是工具调用则返回 ``None``。

    容错点（均为实测中小模型确实会产生的写法）：
      * 用 ```json 代码块包裹；
      * JSON 前后附带"我来计算一下"这类解释文字；
      * 键名写成 ``tool_name`` / ``name``；
      * args 直接平铺在顶层（``{"tool":"calc","expr":"1+1"}``）。
    解析失败一律返回 None（按普通回答处理），绝不抛出异常。
    """
    raw = _strip_code_fence(text or "")
    if not raw.strip():
        return None
    candidates = [obj for obj in _json_objects(raw) if _TOOL_LINE.search(obj)]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("tool") or data.get("tool_name") or data.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        args = data.get("args")
        if not isinstance(args, dict):
            args = data.get("arguments")
        if not isinstance(args, dict):
            # [EN] Flattened form: every key other than tool/why is treated as an
            # [EN] argument.
            # [ZH] 平铺写法：除 tool/why 之外的键都当作参数。
            args = {k: v for k, v in data.items()
                    if k not in ("tool", "tool_name", "name", "why", "reason")}
        why = str(data.get("why") or data.get("reason") or "").strip()
        return {"tool": name.strip(), "args": args, "why": why}
    return None


#: [EN] Common spellings of "final answer" (the model may add emphasis, a colon, or
#: [EN] a line break).
#: [ZH] "最终答案"的常见写法（模型可能加粗、加冒号或换行）。
_FINAL_MARK = re.compile(r"(?:最终答案|最终结论|最后答案|答案)\s*[:：]?\s*", re.S)


def extract_final_answer(text: str) -> str:
    """
    [EN]
    Return the content following the "final answer:" marker, or the original text
    (with tool-call lines removed) when the marker is absent.

    Trailing Markdown emphasis characters are stripped: models frequently write
    ``**final answer:** ...``, and naively taking everything after the colon leaves
    the leading ``**`` in the answer, which makes the interface look corrupted.

    [ZH]
    取出「最终答案：」之后的内容；没有该标记时返回原文（去掉工具调用行）。

    会清除尾随的 Markdown 强调符号：模型经常写 ``**最终答案：** 甲 15 岁``，
    直接取冒号之后会得到 ``** 甲 15 岁``（星号残留在答案中，界面看起来像格式损坏）。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    match = None
    for candidate in _FINAL_MARK.finditer(raw):
        match = candidate                            # [EN] Take the last match so a
                                                     # [EN] marker inside an example
                                                     # [EN] is not mistaken for the result.
                                                     # [ZH] 取最后一处，避免把示例中的标记当作结果。
    if match:
        tail = raw[match.end():].strip()
        tail = re.sub(r"^[\s*_`]+", "", tail).strip()
        tail = re.sub(r"[\s*_`]+$", "", tail).strip()
        if tail:
            return tail
    lines = []
    for line in raw.splitlines():
        if _TOOL_LINE.search(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()
