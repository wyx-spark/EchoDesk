"""联网搜索：DuckDuckGo 查询 + 结构化结果 + 结果注入文本。

## 任务书任务二（优化已知问题-2）：搜索未被模型有效利用

任务书要求"逐环节排查"四个环节，本模块负责其中的**结果返回**与**结果注入**，
并把每个环节都做成**可观测**的：

| 环节 | 本模块的做法 |
| --- | --- |
| 搜索触发 | 调用方（``main.Backend._search_if_enabled``）显式调用并记日志 |
| 结果返回 | 返回结构化 :class:`SearchResult` 列表 + ``ok``/``error`` 状态，**不再把错误当成文本片段返回** |
| 结果注入 | :meth:`SearchOutcome.as_context` 生成**编号列表 + 明确的引用指令**（见下） |
| 模型利用 | 注入文本末尾明确要求"基于上述结果回答并标注编号"，并给出"结果不足就说不知道"的出口 |

## 为什么注入格式要这么写

之前注入的是 ``参考以下网络信息：\n<裸文本>``，小模型（0.6B/1.7B）在这种
"没有结构、没有要求"的上下文里会直接忽略检索内容、凭记忆作答 —— 用户看到的就是
"看不出搜索结果的痕迹"。这里的改动针对小模型的注意力特点：

1. **编号**：``[1] [2] [3]`` 让小模型能把"某一条"作为整体引用，而不是在一段
   混合文本里找线索；
2. **每条压到一行标题 + 摘要**：正文过长的结果会挤掉用户真正的问题；
3. **显式指令**：明确要求"优先使用上述结果""引用时标注编号"，并给出
   "信息不足时直说"的出口（否则小模型倾向于编造以显得有用）；
4. **明确的失败态**：无结果 / 失败时返回状态而不是一句"未找到相关结果。"被
   当成搜索结果注入（旧行为会把这句话当资料喂给模型，模型据此编造）。

## 条数与长度上限（自行判断项）

任务书要求 DSH 判断"搜索结果条数与长度上限"。取值与理由：

* **条数默认 5**（``config.search_results``，可配）：3 条经常被 DuckDuckGo 返回
  同一事件的重复报道（信息量低），5 条能在**不挤占窗口**的前提下提高覆盖面；
  单条上限 400 字符，5 条最坏 2000 字符 ≈ 1000 token，占 8192 窗口约 12%，
  对 0.6B/1.7B 是安全区间。
* **总长上限 3000 字符**：超过就按顺序截断到能放下的条数（不是把每条都砍短，
  保留前几条的完整性比每条都残缺更有用）。

## 触发时机（自行判断项）

任务书要求判断"每次提问都触发还是按需触发"。本实现**按需触发**：由
``config.search_default`` 与 ``/search on|off`` 决定开关状态（沿用既有行为），
打开时**每次提问都查**。理由：本地小模型没有可靠的"这题需不需要联网"判断能力，
用模型做路由会引入一次额外推理（纯 CPU 上几秒到几十秒）且判断质量不稳定；
而用户按下开关本身就是一次明确意图表达。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger("EchoDesk.Search")

#: 单条结果的摘要上限（字符）。过长会挤占上下文，过短则丢失关键信息。
SNIPPET_CHARS = 400

#: 注入上下文的总长上限（字符）。5 条 × 400 ≈ 2000，留 1000 字符给标题与编号行。
CONTEXT_CHARS = 3000

#: 注入时给模型的指令（**刻意写死中文**：与 conversation.py 里的系统提示词同属
#: "发给模型的内容"，不是界面文本，故不进语言包）。
_INSTRUCTION = (
    "【使用要求】1) 优先依据上述搜索结果作答，不要凭记忆编造；"
    "2) 引用某条结果时在句末标注编号，如 [1]；"
    "3) 这些结果不足以回答时，直接说明「搜索结果不足以回答」并指出缺什么，不要硬答。"
)


@dataclass
class SearchResult:
    """单条搜索结果（结构化，不再拼成一段文本）。"""

    title: str
    snippet: str
    url: str

    def line(self, index: int) -> str:
        """渲染成注入文本里的一行（标题 + 摘要 + 链接，各占一行）。"""
        parts = [f"[{index}] {self.title or '（无标题）'}"]
        body = (self.snippet or "").strip()
        if body:
            if len(body) > SNIPPET_CHARS:
                body = body[:SNIPPET_CHARS] + "…"
            parts.append(body)
        if self.url:
            parts.append(self.url)
        return "\n".join(parts)


@dataclass
class SearchOutcome:
    """一次搜索的完整结果：**成功与否**、查了什么、拿到了什么。

    旧实现把失败也当字符串返回（``"搜索失败: ..."``），调用方只能靠
    ``"未找到" not in result`` 这种脆弱判断区分成功与失败 —— 于是失败信息
    有时被当成"资料"注入给模型。现在状态是显式字段。
    """

    query: str = ""
    results: List[SearchResult] = field(default_factory=list)
    ok: bool = False
    error: str = ""            # 失败原因（已脱敏，可直接展示）
    elapsed_ms: int = 0

    @property
    def count(self) -> int:
        return len(self.results)

    @property
    def usable(self) -> bool:
        """是否值得注入上下文（成功且至少有一条结果）。"""
        return self.ok and bool(self.results)

    def as_context(self, limit: int = CONTEXT_CHARS) -> str:
        """把结果渲染成注入给模型的文本；无可用结果时返回空串。

        **不能返回"未找到相关结果"这类文本**：那会被当成资料，模型据此编造。
        调用方拿到空串就知道"没东西可注入"，从而走"如实告知用户"的分支。
        """
        if not self.usable:
            return ""
        lines: List[str] = [f"【搜索结果】共 {self.count} 条，来自网络检索："]
        used = 0
        shown = 0
        for index, item in enumerate(self.results, 1):
            block = item.line(index)
            if used + len(block) > limit and shown:
                break
            lines.append(block)
            used += len(block)
            shown += 1
        lines.append("")
        lines.append(_INSTRUCTION)
        return "\n".join(lines)


class SearchEngine:
    """DuckDuckGo 搜索（``duckduckgo_search``）。

    刻意**不在这里做静默降级**：拿不到结果就如实返回 ``ok=False`` 与原因，
    由调用方决定怎么告诉用户（"明确告知用户而非静默继续"，任务书要求）。
    """

    def __init__(self, max_results: int = 5):
        self.max_results = max(1, int(max_results or 5))

    def search(self, query: str) -> SearchOutcome:
        """执行一次搜索，返回结构化结果（**不抛异常**，失败也在返回值里）。"""
        import time

        text = (query or "").strip()
        outcome = SearchOutcome(query=text)
        if not text:
            outcome.error = "empty_query"
            return outcome
        started = time.perf_counter()
        try:
            from duckduckgo_search import DDGS

            with DDGS() as ddgs:
                raw = list(ddgs.text(text, max_results=self.max_results))
        except Exception as exc:  # noqa: BLE001 —— 网络/解析/限流都可能失败
            outcome.error = str(exc) or exc.__class__.__name__
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.warning("搜索失败（%s）：%s", text[:40], outcome.error)
            return outcome

        for item in raw or []:
            if not isinstance(item, dict):
                continue
            try:
                outcome.results.append(SearchResult(
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("body") or ""),
                    url=str(item.get("href") or ""),
                ))
            except Exception:
                log.debug("搜索结果条目解析失败（已跳过）", exc_info=True)
        outcome.ok = True
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info("搜索完成：%r → %d 条结果，%d ms", text[:40], outcome.count, outcome.elapsed_ms)
        return outcome

    # ---------- 兼容旧接口 ----------

    def search_text(self, query: str) -> str:
        """旧接口：返回注入用文本（无可用结果时为空串）。

        保留它是为了不破坏既有调用方与测试；新代码应直接用 :meth:`search`，
        这样才能拿到"为什么没有结果"。
        """
        return self.search(query).as_context()
