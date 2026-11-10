"""
[EN]
Web search: DuckDuckGo queries, structured results, and result-injection text.

## Per-stage observability

This module covers the result-return and result-injection stages and makes every
stage observable:

| Stage | Handling in this module |
| --- | --- |
| Search trigger | Invoked explicitly by the caller (``main.Backend._search_if_enabled``) and logged |
| Result return | Returns a structured :class:`SearchResult` list plus ``ok``/``error`` state; errors are never returned as text fragments |
| Result injection | :meth:`SearchOutcome.as_context` produces a numbered list with explicit citation instructions (see below) |
| Model usage | The injected text requires the model to answer from the results and cite numbers, and gives it an exit to state that the results are insufficient |

## Rationale for the injection format

The previous injection was ``参考以下网络信息：\n<raw text>``. In such an
unstructured, unconstrained context, small models (0.6B/1.7B) ignore the retrieved
content and answer from memory, so the user sees no evidence that a search
occurred. The current format targets the attention characteristics of small
models:

1. Numbering (``[1] [2] [3]``) lets the model cite an item as a unit instead of
   searching for cues inside a block of mixed text;
2. Each item is compressed to one title line plus a snippet, because overly long
   bodies displace the user's actual question;
3. Explicit instructions require using the results and citing numbers when
   referencing them, and provide an exit for insufficient information; otherwise
   small models tend to fabricate to appear useful;
4. Explicit failure state: no results or a failure returns a status instead of the
   sentence "no relevant results found" being injected as search content, which
   previously fed that sentence to the model as source material and invited
   fabrication.

## Result count and length limits

* The default count is 5 (``config.search_results``, configurable). Three results
   frequently contain duplicate coverage of the same event from DuckDuckGo, which
   has low information value; five improve coverage without crowding the window.
   With a 400-character per-result limit, five results are at most 2000 characters,
   approximately 1000 tokens, or about 12% of an 8192-token window, which is a safe
   range for 0.6B/1.7B models.
* The total length limit is 3000 characters. Beyond that, results are truncated in
   order to the number that fit; keeping the first few intact is more useful than
   truncating every result.

## Trigger timing

Search is triggered on demand rather than on every question. The on/off state is
controlled by ``config.search_default`` and ``/search on|off``, following the
existing behavior, and when enabled every question triggers a search. Rationale:
local small models cannot reliably judge whether a question needs network access;
using the model as a router would add an extra inference pass (seconds to tens of
seconds on CPU) with unstable quality, while toggling the switch is itself an
explicit expression of intent.

[ZH]
联网搜索：DuckDuckGo 查询 + 结构化结果 + 结果注入文本。

## 各环节可观测性

本模块负责结果返回与结果注入两个环节，并确保每个环节均可观测：

| 环节 | 本模块的做法 |
| --- | --- |
| 搜索触发 | 调用方（``main.Backend._search_if_enabled``）显式调用并记录日志 |
| 结果返回 | 返回结构化 :class:`SearchResult` 列表及 ``ok``/``error`` 状态，不再将错误作为文本片段返回 |
| 结果注入 | :meth:`SearchOutcome.as_context` 生成编号列表与明确的引用指令（见下文） |
| 模型利用 | 注入文本末尾明确要求"基于上述结果回答并标注编号"，并提供"结果不足即说明"的出口 |

## 注入格式的设计理由

此前注入的内容为 ``参考以下网络信息：\n<裸文本>``。小模型（0.6B/1.7B）在这种
"无结构、无要求"的上下文中会直接忽略检索内容并凭记忆作答，用户因此看不到搜索结果的
痕迹。当前改动针对小模型的注意力特点：

1. 编号（``[1] [2] [3]``）使小模型能够把"某一条"作为整体引用，而不是在一段混合
   文本中寻找线索；
2. 每条压缩为一行标题加摘要：正文过长的结果会挤占用户真正的问题；
3. 显式指令：明确要求"优先使用上述结果""引用时标注编号"，并提供"信息不足时直接
   说明"的出口（否则小模型倾向于编造以显得有用）；
4. 明确的失败态：无结果或失败时返回状态，而不是把"未找到相关结果。"当作搜索结果
   注入（旧行为会把该句作为资料喂给模型，模型据此编造）。

## 条数与长度上限

* 条数默认 5（``config.search_results``，可配置）：3 条时常出现同一事件的重复报道，
   信息量较低；5 条可在不挤占窗口的前提下提高覆盖面。单条上限 400 字符，5 条最坏
   2000 字符，约 1000 token，占 8192 窗口约 12%，对 0.6B/1.7B 属于安全区间。
* 总长上限 3000 字符：超出时按顺序截断到可容纳的条数（而不是把每条都缩短，保留前几条
   的完整性比每条都残缺更有用）。

## 触发时机

本实现按需触发：由 ``config.search_default`` 与 ``/search on|off`` 决定开关状态
（沿用既有行为），开启时每次提问都会查询。理由：本地小模型不具备可靠的"该问题是否
需要联网"判断能力，用模型做路由会引入一次额外推理（纯 CPU 上数秒到数十秒）且判断
质量不稳定；而用户拨动开关本身即是一次明确的意图表达。
"""
from __future__ import annotations
from i18n import t as _t

import base64
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("EchoDesk.Search")

#: [EN] Maximum snippet length per result, in characters. Longer values crowd the
#: context; shorter values lose key information.
#: [ZH] 单条结果的摘要上限（字符）。过长会挤占上下文，过短则会丢失关键信息。
SNIPPET_CHARS = 400

#: [EN] Maximum total length of the injected context, in characters. Five results at
#: 400 characters each is about 2000, leaving 1000 characters for titles and
#: numbered lines.
#: [ZH] 注入上下文的总长上限（字符）。5 条 × 400 约 2000，另留 1000 字符给标题与
#: [ZH] 编号行。
CONTEXT_CHARS = 3000

#: [EN] Instruction supplied to the model with the injected results. It is
#: intentionally hard-coded in Chinese: like the system prompt in conversation.py it
#: is content sent to the model, not UI text, so it is not localized.
#: [ZH] 注入结果时提供给模型的指令（刻意固定为中文：与 conversation.py 中的系统
#: [ZH] 提示词同属"发送给模型的内容"，而非界面文本，因此不进入语言包）。
_INSTRUCTION = (
    "【使用要求】1) 优先依据上述搜索结果作答，不要凭记忆编造；"
    "2) 引用某条结果时在句末标注编号，如 [1]；"
    "3) 这些结果不足以回答时，直接说明「搜索结果不足以回答」并指出缺什么，不要硬答。"
)


@dataclass
class SearchResult:
    """
    [EN]
    A single search result, kept structured rather than concatenated into text.
    [ZH]
    单条搜索结果（结构化保存，不再拼接为一段文本）。
    """

    title: str
    snippet: str
    url: str

    def line(self, index: int) -> str:
        """
        [EN]
        Render the result as one block in the injected text: title, snippet, and URL,
        each on its own line.
        [ZH]
        渲染为注入文本中的一段：标题、摘要与链接各占一行。
        """
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
    """
    [EN]
    Complete result of one search: success status, the query, and the results.

    The previous implementation returned failures as strings
    (``"搜索失败: ..."``), leaving callers to distinguish success from failure with
    the fragile check ``"未找到" not in result``, so failure messages were sometimes
    injected into the model as source material. Status is now an explicit field.

    [ZH]
    一次搜索的完整结果：成功与否、查询内容与返回结果。

    旧实现将失败也作为字符串返回（``"搜索失败: ..."``），调用方只能通过
    ``"未找到" not in result`` 这类脆弱判断区分成功与失败，导致失败信息有时被当作
    "资料"注入给模型。现在状态为显式字段。
    """

    query: str = ""
    results: List[SearchResult] = field(default_factory=list)
    ok: bool = False
    # [EN] Failure reason, already sanitized and safe to display.
    # [ZH] 失败原因（已脱敏，可直接展示）。
    error: str = ""
    # [EN] Failure category, expressed in terms the user can understand.
    # [ZH] 失败类别（以用户可理解的方式表达）。
    error_kind: str = ""
    # [EN] Search source actually used ("ddgs" or "bing"), for diagnostics.
    # [ZH] 实际生效的搜索源（"ddgs" 或 "bing"），供诊断使用。
    source: str = ""
    elapsed_ms: int = 0

    @property
    def count(self) -> int:
        return len(self.results)

    @property
    def usable(self) -> bool:
        """
        [EN]
        Whether the outcome is worth injecting into the context: the search
        succeeded and at least one result was returned.
        [ZH]
        是否值得注入上下文：搜索成功且至少返回一条结果。
        """
        return self.ok and bool(self.results)

    def as_context(self, limit: int = CONTEXT_CHARS) -> str:
        """
        [EN]
        Render the results as text for injection into the model. Returns an empty
        string when no usable results exist.

        Text such as "no relevant results found" must not be returned: it would be
        treated as source material and the model would fabricate content from it.
        An empty string tells the caller that nothing can be injected, so it takes
        the branch that reports the situation to the user truthfully.

        [ZH]
        将结果渲染为注入模型的文本；无可用结果时返回空串。

        不能返回"未找到相关结果"这类文本：它会被当作资料，模型据此编造内容。调用方
        取得空串即知"无内容可注入"，从而走"如实告知用户"的分支。
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


# ===========================================================================
# [EN] Failure classification: low-level exception details are not exposed to the
# user directly.
# [ZH] 失败分类：不将底层异常细节直接暴露给用户。
# ===========================================================================
#
# [EN] The previous behavior inserted `outcome.error` verbatim into the prompt, so
# the user saw messages such as "search failed (https://www.bing.com/search
# ConnectError: error sending request for url (...))". Such text addresses
# developers: it neither tells the user what to do nor distinguishes a network
# problem from a changed search-source page structure. Failures are grouped into a
# small set of categories, and the language pack provides user-facing wording for
# each.
# [ZH] 旧行为将 `outcome.error` 原样放入提示，用户看到的是
# [ZH]   "联网搜索失败（https://www.bing.com/search ConnectError: error sending
# [ZH]     request for url (https://www.bing.com/search?q=...)）"
# [ZH] 这类面向开发者的文本，既未告知用户应如何处理，也未区分"网络不通"与"搜索源改版"。
# [ZH] 此处将其归为有限的若干类别，由语言包为每一类提供面向用户的说法。

#: [EN] Category code -> language-pack key holding a one-line user-facing message.
#: Keys must exist in every language pack, and ``tools/check_lang.py`` validates
#: their completeness.
#: [ZH] 分类码 → 语言包键（面向用户的一句话）。各语言包中必须成对存在这些键，
#: [ZH] 由 ``tools/check_lang.py`` 校验完整性。
FAIL_KIND_KEYS = {
    "network": "sys.search_failed_network",
    "timeout": "sys.search_failed_timeout",
    "certificate": "sys.search_failed_certificate",
    "rate_limit": "sys.search_failed_rate_limit",
    "source_changed": "sys.search_failed_source_changed",
    "no_dependency": "sys.search_failed_no_dependency",
    "empty": "sys.search_failed",
    "unknown": "sys.search_failed",
}


def classify_error(text: str) -> str:
    """
    [EN]
    Classify low-level exception text into one of the categories in
    :data:`FAIL_KIND_KEYS`.

    The evaluation order matters: specific causes (certificate, timeout, rate limit)
    are checked first, and only then the generic network category. Otherwise any
    error mentioning "connect" would be classified as a network problem and mask the
    actual cause.

    [ZH]
    将底层异常文本归入 :data:`FAIL_KIND_KEYS` 中的某一类别。

    判断顺序有明确要求：先排除可明确指认的原因（证书、超时、限流），再归入笼统的
    "网络不通"类别，否则任何包含 "connect" 字样的错误都会被归为网络问题，掩盖真实
    原因。
    """
    t = (text or "").lower()
    if not t:
        return "unknown"
    if "certificate" in t or "ssl" in t or "cert verify" in t or "tls" in t:
        return "certificate"
    if "timed out" in t or "timeout" in t:
        return "timeout"
    if "429" in t or "403" in t or "too many requests" in t or "rate limit" in t:
        return "rate_limit"
    if ("error sending request" in t or "connect" in t or "network" in t
            or "dns" in t or "resolve" in t or "unreachable" in t
            or "connection" in t):
        return "network"
    if "return none" in t or "no results" in t or "parse" in t or "empty" in t:
        return "source_changed"
    return "unknown"


# ===========================================================================
# [EN] Built-in fallback search source
# [ZH] 内建备用搜索源
# ===========================================================================
#
# [EN] Why it is required: `duckduckgo_search` 8.1.1 always fails on networks in
# mainland China. This is not a user error but an inherent incompatibility between
# the library and regional redirection. Three pieces of evidence:
#
#   1. That version hard-codes a single backend:
#      ``backends = ["bing"]  # temporaly disable html and lite backends``
#      (the comment is quoted verbatim). The failing URL is therefore bing.com
#      rather than duckduckgo.com: the project's references to "DuckDuckGo" were
#      correct, and the library itself switched to Bing;
#   2. It creates the primp client with ``follow_redirects=False``, while
#      ``www.bing.com`` is answered with a 302 to ``cn.bing.com`` in mainland China,
#      so the library always receives the 302 and reports failure;
#   3. Measured on 2026-10-06 (see the records): the library configuration receives
#      ``302 Location: https://cn.bing.com/search?q=test``, while following the
#      redirect yields ``200`` with approximately 99 KB containing 10 ``li.b_algo``
#      result blocks.
#
# A built-in Bing source that follows redirects and parses the page itself is
# therefore provided as a fallback. Three deliberate trade-offs:
#   * Requests are sent with ``requests``, an existing required dependency, so no
#     new dependency is introduced;
#   * Parsing uses the standard-library ``html.parser``, avoiding a dependency on
#     lxml or bs4. Those are indirect dependencies of ``duckduckgo_search``, and
#     search is an optional feature, so their presence cannot be assumed;
#   * The fallback is enabled only when the primary source is unavailable, leaving
#     the behavior of users with working network access unchanged.
#
# [ZH] 必须提供该备用源的原因：`duckduckgo_search` 8.1.1 在部分网络环境下必然失败，
# [ZH] 这不是用户操作问题，而是"库 + 地区重定向"的固有不兼容。三项证据：
#
# [ZH]   1. 该版本将后端硬编码为单一来源：
# [ZH]      ``backends = ["bing"]  # temporaly disable html and lite backends``
# [ZH]      （原文注释如此），因此报错 URL 是 ``bing.com`` 而非 duckduckgo.com；
# [ZH]      项目记录中写的"DuckDuckGo"并没有错，是该库自身切换到了 Bing；
# [ZH]   2. 它创建 primp 客户端时设置 ``follow_redirects=False``，而 ``www.bing.com``
# [ZH]      在该网络环境下会返回 302 重定向到 ``cn.bing.com``，库因此始终只拿到 302 并
# [ZH]      判定失败；
# [ZH]   3. 验证（2026-10-06，见 records）：库配置得到
# [ZH]      ``302 Location: https://cn.bing.com/search?q=test``；跟随重定向后得到
# [ZH]      ``200``，约 99 KB，包含 10 个 ``li.b_algo`` 结果块。
#
# [ZH] 因此此处内建一个"跟随重定向并自行解析"的 Bing 源作为兜底。三项刻意取舍：
# [ZH]   * 使用项目必需依赖 ``requests`` 发送请求（不新增依赖）；
# [ZH]   * 使用标准库 ``html.parser`` 解析（不依赖 lxml/bs4：它们是
# [ZH]     ``duckduckgo_search`` 的间接依赖，而搜索对本项目属于可选功能，不能假定其存在）；
# [ZH]   * 仅在主源不可用时启用，不改变网络正常用户的既有行为。

#: [EN] Bing search endpoint; requests follows the redirect to ``cn.bing.com``.
#: [ZH] Bing 搜索入口（重定向到 ``cn.bing.com``，由 requests 自动跟随）。
_BING_URL = "https://www.bing.com/search"

#: [EN] Browser-like User-Agent: Bing rejects requests with no UA or a crawler UA,
#: or redirects them to a verification page. This is the same constraint as the HF
#: endpoints in ``model_hub.py``, which also require a dedicated UA.
#: [ZH] 使用浏览器 User-Agent：Bing 会对无 UA 或爬虫 UA 的请求拒绝应答或重定向到
#: [ZH] 验证页。这与 ``model_hub.py`` 中 HF 端点"必须使用专用 UA"属于同一类约束。
_BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _decode_bing_url(href: str) -> str:
    """
    [EN]
    Decode a Bing redirect link.

    Result links are commonly ``https://www.bing.com/ck/a?u=a1aHR0cHM6Ly8...``; the
    real address is the base64url payload of the ``u`` parameter after removing its
    first two characters, without padding. When decoding fails, the original link is
    returned unchanged so that it remains clickable.

    [ZH]
    还原 Bing 的跳转链接。

    Bing 的结果链接通常形如 ``https://www.bing.com/ck/a?u=a1aHR0cHM6Ly8...``，真实
    地址为 ``u`` 参数去掉前两个字符后的 base64url 编码（无 padding）。解码失败时
    原样返回，以保证链接仍可点击。
    """
    if not href:
        return ""
    if href.startswith("https://www.bing.com/ck/a?"):
        raw = parse_qs(urlparse(href).query).get("u", [""])[0]
        if raw and len(raw) > 2:
            body = raw[2:]
            try:
                return base64.urlsafe_b64decode(
                    body + "=" * ((-len(body)) % 4)).decode("utf-8", "replace")
            # [EN] A decoding failure must not fail the entire search.
            # [ZH] 解码失败不应导致整次搜索失败。
            except Exception:  # noqa: BLE001
                return href
    return href


class _BingParser(HTMLParser):
    """
    [EN]
    Extract title, link, and snippet from ``li.b_algo`` blocks on a Bing result
    page.

    Only this structure is recognized. A Bing redesign makes the parser extract
    nothing, which surfaces as zero results, rather than raising. This is
    deliberate: a failed search can be reported truthfully, while a crash cannot.

    [ZH]
    从 Bing 结果页中抽取 ``li.b_algo`` 的标题、链接与摘要。

    仅识别这一种结构。Bing 改版会导致抽取不到内容（表现为无结果），而不会抛出异常。
    这是刻意设计：搜索失败可以如实告知，崩溃则不可接受。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[SearchResult] = []
        # [EN] Whether the parser is currently inside a b_algo block.
        # [ZH] 当前是否位于某个 b_algo 块内。
        self._in_li = False
        # [EN] Nesting depth of li elements inside b_algo, reset to zero when the
        # block ends.
        # [ZH] b_algo 内 li 的嵌套深度（块结束时归零）。
        self._depth = 0
        self._in_h2 = False
        self._in_p = False
        self._href = ""
        self._title: List[str] = []
        self._snippet: List[str] = []

    # [EN] ---------- Internal helpers ----------
    # [ZH] ---------- 内部方法 ----------

    def _begin(self) -> None:
        self._in_li = True
        self._depth = 0
        self._in_h2 = False
        self._in_p = False
        self._href = ""
        self._title = []
        self._snippet = []

    def _finish(self) -> None:
        self._in_li = False
        self._in_h2 = False
        self._in_p = False
        if self._href:
            title = " ".join("".join(self._title).split())
            snippet = " ".join("".join(self._snippet).split())
            url = _decode_bing_url(self._href)
            if url and (title or snippet):
                self.results.append(SearchResult(title=title, snippet=snippet, url=url))

    # [EN] ---------- HTMLParser callbacks ----------
    # [ZH] ---------- HTMLParser 回调 ----------

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "li":
            cls = attr.get("class") or ""
            if self._in_li:
                self._depth += 1
            elif "b_algo" in cls:
                self._begin()
            return
        if not self._in_li:
            return
        if tag == "h2":
            self._in_h2 = True
        elif tag == "a" and attr.get("href") and not self._href:
            self._href = attr["href"]
        elif tag == "p":
            self._in_p = True

    def handle_endtag(self, tag):
        if not self._in_li:
            return
        if tag == "li":
            if self._depth == 0:
                self._finish()
            else:
                self._depth -= 1
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "p":
            self._in_p = False

    def handle_data(self, data):
        if not self._in_li:
            return
        if self._in_h2:
            self._title.append(data)
        elif self._in_p:
            self._snippet.append(data)


def search_bing(query: str, max_results: int = 5, timeout: int = 15) -> SearchOutcome:
    """
    [EN]
    Built-in Bing source: fetch the result page while following redirects, then
    parse it with the standard library.

    The only substantive difference from ``duckduckgo_search`` is that redirects are
    followed, which is precisely the reason the library fails on the affected
    networks (see the note at the top of this section).

    [ZH]
    内建 Bing 源：跟随重定向获取结果页，再使用标准库解析。

    与 ``duckduckgo_search`` 的唯一实质差别在于是否跟随重定向，而这正是该库在相关
    网络环境下失败的全部原因（见本节顶部说明）。
    """
    outcome = SearchOutcome(query=query, source="bing")
    started = time.perf_counter()
    try:
        import requests
    # [EN] requests is a required dependency, so this path should not normally occur.
    # [ZH] requests 为必需依赖，正常情况下不会进入此分支。
    except ImportError:
        outcome.error = _t("search.no_dependency_requests")
        outcome.error_kind = "no_dependency"
        return outcome

    headers = {
        "User-Agent": _BING_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(_BING_URL, params={"q": query}, headers=headers,
                            timeout=timeout, allow_redirects=True)
    # [EN] Network errors are captured and reported through the outcome.
    # [ZH] 网络异常在此捕获，并通过 outcome 上报。
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.error_kind = classify_error(outcome.error)
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    if resp.status_code != 200:
        outcome.error = f"HTTP {resp.status_code}"
        outcome.error_kind = ("rate_limit" if resp.status_code in (403, 429)
                              else "source_changed")
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    parser = _BingParser()
    try:
        parser.feed(resp.text)
    # [EN] A parse failure is treated as a changed source page structure.
    # [ZH] 解析失败按"搜索源改版"处理。
    except Exception as exc:  # noqa: BLE001
        outcome.error = _t("search.parse_failed", error=exc)
        outcome.error_kind = "source_changed"
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    outcome.results = parser.results[:max(1, int(max_results or 5))]
    outcome.ok = True
    outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info("搜索完成（内建 Bing 源）：%r → %d 条结果，%d ms",
             query[:40], outcome.count, outcome.elapsed_ms)
    return outcome


# ===========================================================================
# [EN] Search provider interface and cascade (v0.2.6 task 2.3)
# [ZH] 搜索提供方接口与级联（v0.2.6 任务 2.3）
# ===========================================================================
#
# [EN] The previous implementation hard-coded two sources inside one class. The
# [EN] cascade is now expressed as an ordered provider list: every provider
# [EN] implements the same ``search`` contract, and the orchestrator moves to the
# [EN] next provider whenever the current one times out, is rate-limited, or
# [EN] otherwise fails. Provider failures are logged and do not surface to the
# [EN] user until every provider has failed.
# [ZH] 此前两种搜索源硬编码在同一个类中。现在级联被表达为有序提供方列表：每个
# [ZH] 提供方实现同一 ``search`` 契约，当前提供方超时、被限流或出现其他失败时，
# [ZH] 编排器自动切换到下一个。提供方失败仅记录日志，直到全部失败才向用户呈现。

class SearchProvider(ABC):
    """
    [EN]
    Contract implemented by every search source.

    A provider must never raise: transient failures are reported through
    ``SearchOutcome.ok`` and ``error_kind`` so the cascade can continue.

    [ZH]
    所有搜索源实现的统一契约。

    提供方不得抛出异常：瞬时故障通过 ``SearchOutcome.ok`` 与 ``error_kind``
    上报，使级联得以继续。
    """

    #: [EN] Stable provider identifier used in diagnostics and logs.
    #: [ZH] 诊断与日志中使用的稳定提供方标识。
    name = "provider"

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = max(1, int(timeout or 15))

    @abstractmethod
    def search(self, query: str, max_results: int) -> SearchOutcome:
        """
        [EN]
        Execute one query and return a structured outcome; never raises.

        [ZH]
        执行一次查询并返回结构化结果；不得抛出异常。
        """


class DuckDuckGoProvider(SearchProvider):
    """
    [EN]
    Primary provider backed by the optional ``duckduckgo_search`` package.

    [ZH]
    基于可选依赖 ``duckduckgo_search`` 的主提供方。
    """

    name = "ddgs"

    def search(self, query: str, max_results: int) -> SearchOutcome:
        outcome = SearchOutcome(query=query, source=self.name)
        started = time.perf_counter()
        try:
            from duckduckgo_search import DDGS

            with DDGS(timeout=self.timeout) as ddgs:
                raw = list(ddgs.text(query, max_results=max_results))
        except ImportError:
            outcome.error = _t("search.no_dependency_ddgs")
            outcome.error_kind = "no_dependency"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        except Exception as exc:  # noqa: BLE001 - cascade must continue
            outcome.error = str(exc) or exc.__class__.__name__
            outcome.error_kind = classify_error(outcome.error)
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.warning("搜索提供方 %s 失败：%s", self.name, outcome.error)
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
        log.info("搜索完成（%s）：%r → %d 条结果，%d ms", self.name, query[:40],
                 outcome.count, outcome.elapsed_ms)
        return outcome


class BingProvider(SearchProvider):
    """
    [EN]
    Secondary provider wrapping the built-in redirect-following Bing source.

    [ZH]
    包装内建"跟随重定向" Bing 源的次提供方。
    """

    name = "bing"

    def search(self, query: str, max_results: int) -> SearchOutcome:
        # [EN] The module-level function is resolved at call time so tests can
        # [EN] replace it with a stub without patching the class.
        # [ZH] 模块级函数在调用时解析，测试可在不打补丁到类的情况下替换为桩。
        return search_bing(query, max_results, timeout=self.timeout)


class SearXNGProvider(SearchProvider):
    """
    [EN]
    Third provider for a locally deployed SearXNG instance.

    The endpoint is supplied by configuration (``searxng_url``); when it is
    empty the provider is not added to the cascade. JSON output is required
    (``format=json``), which is the documented SearXNG API shape.

    [ZH]
    面向本机部署 SearXNG 实例的第三提供方。

    端点由配置 ``searxng_url`` 提供；为空时该提供方不加入级联。要求 JSON 输出
    （``format=json``），这是 SearXNG 文档化的接口形态。
    """

    name = "searxng"

    def __init__(self, base_url: str, timeout: int = 15) -> None:
        super().__init__(timeout)
        self.base_url = str(base_url or "").strip().rstrip("/")

    def search(self, query: str, max_results: int) -> SearchOutcome:
        outcome = SearchOutcome(query=query, source=self.name)
        started = time.perf_counter()
        if not self.base_url:
            outcome.error = _t("search.searxng_not_configured")
            outcome.error_kind = "no_dependency"
            return outcome
        try:
            import requests
        except ImportError:
            outcome.error = _t("search.no_dependency_requests")
            outcome.error_kind = "no_dependency"
            return outcome
        headers = {
            "User-Agent": _BING_UA,
            "Accept": "application/json",
        }
        try:
            response = requests.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json"},
                headers=headers,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - cascade must continue
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.error_kind = classify_error(outcome.error)
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.warning("搜索提供方 %s 失败：%s", self.name, outcome.error)
            return outcome
        if response.status_code != 200:
            outcome.error = f"HTTP {response.status_code}"
            outcome.error_kind = ("rate_limit" if response.status_code in (403, 429)
                                  else "source_changed")
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.warning("搜索提供方 %s 返回 %s", self.name, outcome.error)
            return outcome
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - malformed payload is a source change
            outcome.error = f"json: {exc}"
            outcome.error_kind = "source_changed"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        for item in list(payload.get("results") or [])[:max(1, int(max_results or 5))]:
            if not isinstance(item, dict):
                continue
            outcome.results.append(SearchResult(
                title=str(item.get("title") or ""),
                snippet=str(item.get("content") or ""),
                url=str(item.get("url") or ""),
            ))
        outcome.ok = True
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info("搜索完成（%s）：%r → %d 条结果，%d ms", self.name, query[:40],
                 outcome.count, outcome.elapsed_ms)
        return outcome


class SearchEngine:
    """
    [EN]
    Search orchestration with an ordered provider cascade.

    Default order: DuckDuckGo -> Bing -> SearXNG (the last one only when
    ``searxng_url`` is configured). A provider that times out or returns HTTP
    429/403 is treated as unavailable and the next provider is tried without
    interrupting the user. When every provider fails, the combined reasons are
    returned in one structured outcome.

    [ZH]
    使用有序提供方级联的搜索编排。

    默认顺序：DuckDuckGo → Bing → SearXNG（仅当配置了 ``searxng_url``）。
    超时或返回 HTTP 429/403 的提供方被视为不可用，自动尝试下一个，不打断用户。
    全部失败时，将各提供方的失败原因合并到同一个结构化结果中返回。
    """

    def __init__(self, max_results: int = 5, fallback_enabled: bool = True,
                 timeout: int = 15, searxng_url: str = "",
                 providers: Optional[List[SearchProvider]] = None):
        self.max_results = max(1, int(max_results or 5))
        self.timeout = max(1, int(timeout or 15))
        self.searxng_url = str(searxng_url or "").strip()
        #: [EN] Whether to enable fallback providers. Retained for testability:
        #: when a stub simulates primary-source conditions, real fallback
        #: providers would issue network requests and turn a simulated failure
        #: into an actual success.
        #: [ZH] 是否启用备用提供方。保留该开关是为了可测性：桩模拟主源情形时，
        #: 真实备用提供方会发起网络请求，把"模拟失败"变成"真实成功"。
        self.fallback_enabled = bool(fallback_enabled)
        self.providers: List[SearchProvider] = list(providers) if providers else [
            DuckDuckGoProvider(self.timeout),
            BingProvider(self.timeout),
        ]
        if providers is None and self.searxng_url:
            self.providers.append(SearXNGProvider(self.searxng_url, self.timeout))

    def _search_ddgs(self, text: str) -> SearchOutcome:
        """
        [EN]
        Primary source: ``duckduckgo_search`` (an optional dependency).

        From version 8.1.1 onward this library always fails on the affected
        networks; the reason is documented at :func:`search_bing`. It is retained
        because it remains a more complete parser in regions with direct access to
        DuckDuckGo or Bing, so it should not be removed outright.

        [ZH]
        主源：``duckduckgo_search``（可选依赖）。

        自 8.1.1 起，该库在相关网络环境下必然失败，原因见 :func:`search_bing` 的
        说明。保留它是由于在可直连 DuckDuckGo / Bing 的地区，它仍是更完整的解析器，
        不应直接删除。

        [EN]
        Returns the outcome of the DuckDuckGo provider. Retained as a
        compatibility entry point for callers written before the provider
        interface existed.
        """
        return DuckDuckGoProvider(self.timeout).search(text, self.max_results)

    def search(self, query: str) -> SearchOutcome:
        """
        [EN]
        Execute one search and return a structured result. This method never raises;
        failures are reported in the return value.

        Providers are tried in the configured order (DuckDuckGo -> Bing ->
        SearXNG). The first provider that answers -- including a reachable
        "no results" answer -- wins. Timeouts, HTTP 429/403, and other provider
        failures cause a silent switch to the next provider; only when every
        provider fails is the combined reason reported.

        [ZH]
        执行一次搜索并返回结构化结果（不抛出异常，失败信息也包含在返回值中）。

        按配置顺序（DuckDuckGo → Bing → SearXNG）尝试提供方。第一个成功应答的
        提供方胜出（包括"连通但无结果"）。超时、HTTP 429/403 及其他提供方故障
        均静默切换到下一个；仅当全部提供方失败时，才合并上报原因。
        """
        text = (query or "").strip()
        outcome = SearchOutcome(query=text)
        if not text:
            outcome.error = "empty_query"
            outcome.error_kind = "empty"
            return outcome

        # [EN] The criterion is ``ok``, not ``usable``: a reachable provider's
        # [EN] answer must be accepted, including the "reachable but no results"
        # [EN] case (ok=True, count=0). That is valid information rather than a
        # [EN] failure, and merging it with "could not search" would remove the
        # [EN] different guidance the two cases require.
        # [ZH] 判据是 ``ok`` 而非 ``usable``：提供方连通时其答复即应被采信，包括
        # [ZH] "连通但无结果"（ok=True, count=0）。这属于有效信息而非故障；若与
        # [ZH] "搜不通"混为一谈，就会丢失两种情形所需的差异化提示。
        candidates = self.providers if self.fallback_enabled else self.providers[:1]
        failures: List[SearchOutcome] = []
        for provider in candidates:
            try:
                current = provider.search(text, self.max_results)
            except Exception as exc:  # noqa: BLE001 - contract violation is isolated
                current = SearchOutcome(query=text, source=provider.name)
                current.error = f"{type(exc).__name__}: {exc}"
                current.error_kind = classify_error(current.error)
            if current.ok:
                if failures:
                    log.info("搜索已回退到 %s：%d 条结果", provider.name, current.count)
                return current
            failures.append(current)
            log.info("搜索提供方 %s 不可用（%s），尝试下一个",
                     provider.name, current.error_kind or current.error)

        if not failures:
            outcome.error = _t("search.both_sources_failed",
                               primary=_t("search.primary_empty"),
                               fallback=_t("search.fallback_empty"))
            outcome.error_kind = "unknown"
            return outcome
        if not self.fallback_enabled or len(failures) == 1:
            # [EN] Fallback explicitly disabled (test path) or only one provider
            # [EN] configured: return its reason unchanged.
            # [ZH] 兜底已显式关闭（测试路径）或只配置了一个提供方：如实带回其原因。
            first = failures[0]
            first.error_kind = first.error_kind or classify_error(first.error)
            return first

        if len(failures) == 2:
            primary, fallback = failures
            outcome.error = _t("search.both_sources_failed",
                               primary=primary.error or _t("search.primary_empty"),
                               fallback=fallback.error or _t("search.fallback_empty"))
        else:
            details = "; ".join(
                f"{item.source or 'source'}: {item.error or _t('search.primary_empty')}"
                for item in failures)
            outcome.error = _t("search.all_sources_failed", details=details)
        # [EN] The category is taken from the last provider, which reflects the
        # [EN] actual network condition most closely; the primary source always
        # [EN] fails on the affected networks, so its category gives no actionable
        # [EN] guidance.
        # [ZH] 分类以最后一个提供方为准：它最能反映当前网络状况；主源在相关网络
        # [ZH] 环境下必然失败，其分类对用户没有指导意义。
        outcome.error_kind = (failures[-1].error_kind
                              or classify_error(failures[-1].error)
                              or failures[0].error_kind or "unknown")
        log.warning("全部搜索源都失败：%s", outcome.error)
        return outcome

    # [EN] ---------- Legacy interface compatibility ----------
    # [ZH] ---------- 兼容旧接口 ----------

    def search_text(self, query: str) -> str:
        """
        [EN]
        Legacy interface: return injection text, or an empty string when no usable
        results exist.

        It is retained to avoid breaking existing callers and tests. New code should
        call :meth:`search` directly so that the reason for missing results is
        available.

        [ZH]
        旧接口：返回注入用文本（无可用结果时为空串）。

        保留该接口以避免破坏既有调用方与测试。新代码应直接使用 :meth:`search`，
        以便获取"为什么没有结果"的信息。
        """
        return self.search(query).as_context()
