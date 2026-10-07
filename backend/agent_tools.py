"""Agent 工具层（v0.2.1 能力扩展-1 任务二）：工具接口、内置工具集与权限分级。

设计要点（每条都对应任务书里"需要 DSH 判断"的问题）
=====================================================

**1. 为什么不用 llama.cpp 原生的 ``tools``/``tool_choice``**
底层的 ``create_chat_completion`` 确实有这两个形参，但实测约束很硬：GGUF 带
``tokenizer.chat_template`` 时走 Jinja handler，``tool_choice=None`` 时**整段 tools
被忽略**；``tool_choice="auto"`` 内部强制非流式决策，若命中函数又要求 ``stream=True``
会直接 ``ValueError: Automatic streaming tool choice is not supported``。
也就是说"能不能用工具"取决于模型自带的对话模板，而 EchoDesk 面向的是任意 GGUF
（0.6B 档的模板支持尤其不可靠）。**所以本实现让模型输出一行 JSON，由我们解析**：
零新依赖、对任何 GGUF 都成立、可单测（``parse_tool_call`` 是纯函数）。
代价是"工具调用格式"靠提示词约束 —— 这一点用 ``MODES["agent"]`` 的【输出格式】
与运行时追加的「工具协议」共同保证，并且解析器对常见的跑偏写法（代码块包裹、
前后有解释文字、多余空白）都做了容错。

**2. 权限分级：三个等级，默认只开最安全的一档**
  * ``safe``：纯计算与"只看用户已经放进来的内容"（计算器、日期、单位换算、
    在已上传文件里检索、读已上传文件的摘要）。不外发、不读任意路径、不写盘，默认可用。
  * ``sensitive``：会把数据带出去或读本机任意内容（联网搜索、读取任意本地文件）。
    **默认关闭**，要在 ``config.json`` 的 ``agent.allow_sensitive`` 里或会话里
    用 ``/agent allow <工具>`` 预授权。
  * 写文件、执行命令这类**有持久副作用的工具本版一个都不提供** —— 权限最小化，
    也是"不要给自己的产品开一个能自我提权的口子"。

  没被授权的工具**不是静默失败**：``ToolRegistry.run`` 返回一条说明性观察结果，
  模型据此继续（用已知信息作答），用户也能在对话里看到"这个工具没启用，怎么开"。

**3. 工具集选择：覆盖现有能力 + 若干新工具（任务书要求）**
  复用现有能力：``doc_search``（检索已上传文件）、``doc_summary``（分段摘要）、
  ``web_search``（联网搜索，sensitive）。新增：``calc``、``date``、``convert``、
  ``file_read``（sensitive）。每个工具都是"一次调用、一个字符串结果"，
  便于小模型理解与显示。
"""
from __future__ import annotations

import ast
import datetime as _dt
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# 工具结果是**直接贴回模型上下文、也会显示在对话区**的文本，必须随界面语言走
# （v0.2.2 硬编码清查第 6 批）。i18n 是纯标准库模块，这里没有循环导入风险。
from i18n import t as _t

#: 权限类观察串的语言无关标记（前端据此判定"被拒绝"；显示前会剥掉）
DENIED_MARK = "[!DENIED] "


# ===========================================================================
# 工具接口
# ===========================================================================

#: 权限等级。``safe`` 默认可用；``sensitive`` 需要预授权；``guard`` 本版无工具
#: （保留常量是为了配置/文档/前端有统一的词表，将来要加也有明确的一档）。
RISK_SAFE = "safe"
RISK_SENSITIVE = "sensitive"
RISK_GUARD = "guard"


#: 工具"失败"的文本约定：以这些短语开头的结果会被 ``ToolRegistry.run`` 判为失败。
#:
#: 为什么需要它：工具体**约定不抛异常**（抛异常会让 Agent 循环更难收敛），
#: 于是"算不出来""没搜到""MCP 对端报错"都以**一段说明文本**的形式返回。若不区分，
#: 界面上这些都会显示成"成功"，用户看到的是"工具用过了但什么也没有"。
#: 约定写在这里、而不是每处各判各的，是为了让"成功/失败"只有一种口径。
_FAILURE_PREFIXES = (
    "计算失败", "换算失败", "日期计算失败", "检索失败", "摘要失败", "搜索失败",
    "读取失败", "MCP 工具调用失败", "MCP 工具调用异常", "工具执行出错",
)
_FAILURE_MARKERS = ("未启用", "已退出", "超时")


def looks_like_failure(text: str) -> bool:
    """按上面的约定判断一段工具结果是否表示失败。"""
    body = str(text or "").strip()
    if not body:
        return False
    if body.startswith(_FAILURE_PREFIXES):
        return True
    head = body[:60]
    return any(marker in head for marker in _FAILURE_MARKERS)


@dataclass
class Tool:
    """一个可供模型调用的工具。

    ``run`` 只接收一个参数 dict，返回**字符串**（观察结果）。返回值必须是字符串
    而不是结构化对象：它要原样贴回模型的上下文，形状越简单小模型越不容易读错。
    工具**不应该抛异常**（``ToolRegistry.run`` 也会兜住），错误的正确表达是
    返回一句"为什么没成功"，让模型有机会换一种方式再试。
    """

    name: str
    #: 给模型看的一句话说明（进系统提示词的「可用工具」清单）。
    description: str
    #: 参数说明（同上，写清字段名，否则小模型不知道 args 里该放什么）。
    args_hint: str = ""
    risk: str = RISK_SAFE
    run: Callable[[Dict[str, Any]], str] = field(default=lambda _a: "")
    #: 示例（进「工具协议」的示例区，帮助 0.6B 学会格式）。
    example: str = ""


class ToolContext:
    """工具运行时可用的宿主能力（由 Backend 注入，避免工具层反向依赖 main）。

    刻意用"回调 + 已就绪对象"而不是让工具 import main：工具层要保持可单测
    （``tools/test_agent.py`` 直接用桩实现构造它）。
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

    # ---------- 供工具使用的小工具方法 ----------

    def file_names(self) -> List[str]:
        conv = self.conv
        if conv is None:
            return []
        try:
            return [str(getattr(entry, "name", "") or "") for entry in conv.files]
        except Exception:
            return []

    def file_text(self, name: str = "") -> str:
        """取某份已上传文件的正文（名字为空或找不到时取当前文件）。"""
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
            if target is None:                       # 允许用文件名的一部分匹配
                for entry in entries:
                    if name and name in str(getattr(entry, "name", "")):
                        target = entry
                        break
        if target is None:
            index = getattr(conv, "active_index", -1)
            target = entries[index] if 0 <= index < len(entries) else entries[-1]
        # 正文为空（落盘/只有摘要）时退化为摘要文本，保证工具总有东西可用
        body = str(getattr(target, "content", "") or "")
        if not body:
            body = str(getattr(target, "summary", "") or "")
        return body


# ===========================================================================
# 计算器（safe）—— 用 AST 白名单求值，不用 eval
# ===========================================================================

#: 允许的函数名 → 实现。**刻意只有数学函数**：没有 open/exec/__import__，
#: 也没有属性访问，因此"拿到任意代码执行"这条路在语法层就被堵住了。
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

#: 幂运算的上限：``10**10**10`` 这类写法会让解释器算到天荒地老（等于一条本地 DoS）。
_MAX_POW = 1e6


class CalcError(ValueError):
    """计算器拒绝执行（语法不支持或超出限制）。"""


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
            # **求值本身也会失败**：`1/0`、`1%0`、`0**-1` 都会抛异常。
            # 首轮实测就是这么被抓到的 —— tool_calc 当时只捕获 CalcError，
            # ZeroDivisionError 直接穿透到工具注册表（虽然注册表会兜住并返回
            # "执行出错"，但那对用户是"工具坏了"，而不是"这个表达式算不了"）。
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
    # 属性访问 / 下标 / 推导式 / lambda / 名字（变量）一律拒绝：
    # 它们是把表达式变成任意代码的常见跳板（a.b.c、d["x"]、__class__…）
    raise CalcError(_t("calc.bad_chars"))


def calc_eval(expr: str) -> float:
    """安全求值一个算术表达式（允许数学函数），返回数值。

    刻意不用 ``eval``：``__import__('os').system(...)`` 这类输入在真实环境里出现过，
    而本地小模型又极易被文档里的文字诱导。
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
    """把数值格式化成人看的字符串（整数不带小数点，浮点最多 6 位小数）。"""
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
    except Exception as exc:                     # 兜底：工具**绝不抛异常**
        return _t("calc.failed", error=exc)
    return f"{expr} = {_fmt_number(value)}"


# ===========================================================================
# 日期与时间（safe）
# ===========================================================================

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")

def _weekday_name(index: int) -> str:
    """星期几的显示名：从语言包按逗号列表取（原先写死的中文元组）。"""
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
    """日期工具：``op`` 支持 today（默认）/ add / diff / weekday。"""
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
# 单位换算（safe）
# ===========================================================================

#: 每类单位到"基准单位"的倍率（温度单独处理，因为它需要偏移而不是倍率）。
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

#: 单位别名（中文/常见写法 → 表里的键）。
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
    # 温度：需要偏移，单独算
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
# 已上传文件：检索与摘要（safe）
# ===========================================================================

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；;!?\n])")


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and s.strip()]


def _keyword_score(query: str, sentence: str) -> float:
    """极简相关度：查询里的词（中文按 2 字滑窗 + 英文按词）在句子里出现的次数。

    刻意不引入向量/BM25 之外的东西：工具层要零依赖、毫秒级。检索质量由
    ``retrieval.py``（RAG 深度）承担，这里只保证"能挑出相关段落"。
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
    """在**已上传文件**里按关键词挑相关段落（不外发、不读任意路径）。"""
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
    """取某份已上传文件的**已有**摘要（不触发新的模型调用，避免工具里套模型）。"""
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
# 联网搜索（sensitive）与本地文件读取（sensitive）
# ===========================================================================

def tool_web_search(args: Dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query:
        return _t("tool.search_need_query")
    if ctx.search is None:
        return _t("tool.search_no_component")
    try:
        outcome = ctx.search.search(query)
    except Exception as exc:                     # 搜索失败不能把整轮对话带崩
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


#: 文本类扩展名白名单：``file_read`` 只读这些，二进制/可执行文件一律拒绝。
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
# 注册表
# ===========================================================================

class ToolRegistry:
    """工具注册表 + 权限检查 + 统一调用入口。"""

    def __init__(self, tools: Optional[List[Tool]] = None,
                 allowed_sensitive: Optional[List[str]] = None,
                 ctx: Optional[ToolContext] = None) -> None:
        self.tools: Dict[str, Tool] = {}
        self.allowed_sensitive: set = {str(x).strip().lower() for x in (allowed_sensitive or [])}
        self.ctx = ctx or ToolContext()
        self.calls: List[Dict[str, Any]] = []          # 调用审计（可观测性）
        for tool in tools if tools is not None else default_tools():
            self.register(tool)

    # ---------- 注册与查询 ----------

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def names(self) -> List[str]:
        return list(self.tools)

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(str(name or "").strip())

    def available(self) -> List[Tool]:
        """当前**真正可调用**的工具（权限也考虑在内）—— 写进提示词的就是这些。"""
        return [tool for tool in self.tools.values() if self.is_allowed(tool)]

    def is_allowed(self, tool: Tool) -> bool:
        if tool.risk == RISK_GUARD:
            return False                              # 本版不提供任何有写入副作用的工具
        if tool.risk == RISK_SENSITIVE:
            return tool.name.lower() in self.allowed_sensitive
        return True

    def allow(self, name: str, allowed: bool = True) -> bool:
        """会话/配置级预授权开关。返回是否命中了某个工具。"""
        tool = self.get(name)
        if tool is None:
            return False
        key = tool.name.lower()
        if allowed:
            self.allowed_sensitive.add(key)
        else:
            self.allowed_sensitive.discard(key)
        return True

    # ---------- 调用 ----------

    def run(self, name: str, args: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """执行一个工具，返回 ``(是否成功, 观察结果文本)``。

        **任何情况下都返回文本**（不抛异常、不静默失败）：
        * 权限不足 → 说明"没启用 + 怎么启用"，模型可以据此改用别的办法；
        * 工具内部抛异常 → 兜住并写成一句失败说明，同时记进 ``calls`` 审计。
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
        try:
            if tool.name in _CTX_TOOLS:
                result = tool.run(args, self.ctx)
            else:
                result = tool.run(args)
            text = str(result if result is not None else "")
        except Exception as exc:                       # 工具异常隔离（任务书要求）
            self.calls.append({"tool": name, "ok": False, "reason": "error", "error": str(exc)})
            return False, _t("tool.exec_failed", name=name, error=exc)
        # 工具按约定不抛异常，失败以说明文本返回 —— 这里按统一约定识别，
        # 否则"算不出来""MCP 对端报错"在界面上会显示成成功（见 _FAILURE_PREFIXES）。
        if looks_like_failure(text):
            self.calls.append({"tool": name, "ok": False, "reason": "reported"})
            return False, text
        self.calls.append({"tool": name, "ok": True, "args": args})
        return True, text

    def public_list(self) -> List[Dict[str, Any]]:
        """给界面/文档用的工具清单（含权限与是否启用）。"""
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
        """运行时追加到系统提示词的「工具协议 + 可用工具」段落。

        **为什么放在这里而不是写进 ``MODES["agent"]``**：可用工具清单是动态的
        （受权限开关影响），而模式块有 700 字符的长度上限、且对所有模式共用一份
        审计口径。运行期追加既不占那个预算，也不会出现"提示词里列了没启用的工具"。
        """
        enabled = self.available()
        lines = ["[工具协议]", "需要工具时，只输出**一行** JSON，不要输出别的文字：",
                 '{"tool": "工具名", "args": {...}, "why": "一句话为什么"}',
                 "收到「工具结果」后继续：可以再调一个工具，或输出以「最终答案：」开头的结论。",
                 "一次只调一个工具；工具没启用或失败时，用已有信息作答并说明。",
                 # 这一条来自实测：0.6B 档位在一次"总额 − 手续费"的提问里算出了**手续费**
                 # 就当成最终结论（3000×7.2×0.05 = 1080），工具调用本身完全正确。
                 # 与其去猜模型哪里想错了，不如把"交卷前再读一遍题"写成一条明确要求。
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


#: 需要注入上下文的工具（签名是 ``(args, ctx)``）。其余工具只接收 args。
#: 用一张表而不是检查签名：签名检查在打包/装饰器环境下容易失效，
#: 而这张表在注册表构造时就能一眼看全"哪些工具要碰宿主状态"。
_CTX_TOOLS = {"doc_search", "doc_summary", "web_search", "file_read"}


def default_tools() -> List[Tool]:
    """内置工具集（任务书要求"至少覆盖现有能力 + 若干新工具"）。"""
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
    ]


# ===========================================================================
# 模型输出 → 工具调用（纯函数，可单测）
# ===========================================================================

_TOOL_LINE = re.compile(r"\{\s*\"(?:tool|tool_name|name)\"\s*:", re.S)


def _strip_code_fence(text: str) -> str:
    """去掉 ```json … ``` 包裹（小模型很爱加代码块）。"""
    match = re.search(r"```[a-zA-Z]*\s*(.+?)```", text or "", re.S)
    return match.group(1) if match else (text or "")


def _json_objects(text: str) -> List[str]:
    """从一段文本里切出所有**括号平衡**的 JSON 对象字面量（字符串内的括号不算）。"""
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
    """从模型输出里解析出工具调用；不是工具调用则返回 ``None``。

    容错点（都是实测中小模型真会写出来的形状）：
      * 用 ```json 代码块包起来；
      * JSON 前后还写了"我来算一下："这类解释；
      * 键名写成 ``tool_name`` / ``name``；
      * args 直接平铺在顶层（``{"tool":"calc","expr":"1+1"}``）。
    解析失败一律返回 None（当成普通回答处理），**绝不抛异常**。
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
            # 平铺写法：除 tool/why 之外的键都当参数
            args = {k: v for k, v in data.items()
                    if k not in ("tool", "tool_name", "name", "why", "reason")}
        why = str(data.get("why") or data.get("reason") or "").strip()
        return {"tool": name.strip(), "args": args, "why": why}
    return None


#: "最终答案"的常见写法（模型可能加粗、加冒号或换行）。
_FINAL_MARK = re.compile(r"(?:最终答案|最终结论|最后答案|答案)\s*[:：]?\s*", re.S)


def extract_final_answer(text: str) -> str:
    """取出「最终答案：」之后的内容；没有这个标记则返回原文（去掉工具调用行）。

    会清掉尾随的 Markdown 强调符号：模型极爱写 ``**最终答案：** 甲 15 岁``，
    直接取冒号之后会得到 ``** 甲 15 岁``（星号留在答案里，界面看起来像格式坏了）。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    match = None
    for candidate in _FINAL_MARK.finditer(raw):
        match = candidate                            # 取最后一处，避免把示例里的标记当结果
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
