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

import base64
import logging
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

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
    error_kind: str = ""       # 失败**类别**（任务二第 4 条：给用户能看懂的说法）
    source: str = ""           # 实际生效的搜索源（"ddgs" / "bing"），供诊断用
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


# ===========================================================================
# 失败分类（任务二第 4 条：不要把底层异常栈直接抛给用户）
# ===========================================================================
#
# 旧行为是把 `outcome.error` 原样塞进提示，用户看到的是
#   「联网搜索失败（https://www.bing.com/search ConnectError: error sending
#     request for url (https://www.bing.com/search?q=...)）」
# —— 这是一句**给开发者看**的话，它既没告诉用户"我该怎么办"，也没区分
# "是你网络不通"和"是搜索源改版了"。这里把它归成有限的几类，
# 由语言包为每一类给出面向用户的说法。

#: 分类码 → 语言包键（面向用户的一句话）。键必须成对存在于各语言包里，
#: 由 ``tools/check_lang.py`` 校验完整性。
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
    """把底层异常文本归成 :data:`FAIL_KIND_KEYS` 里的一个类别。

    判断顺序有讲究：先排除"能明确指认"的（证书、超时、限流），再落到
    笼统的"网络不通"上——否则任何一个带 "connect" 字样的错误都会被
    归成网络问题，把真实原因盖掉。
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
# 内建备用搜索源（任务二第 6 条）
# ===========================================================================
#
# **为什么必须有它**：`duckduckgo_search` **8.1.1** 在国内网络下必然失败，
# 而这不是用户的操作问题，是"库 + 地区重定向"的固有不兼容。三处证据：
#
#   1. 该版本库里把后端**硬编码**成一家：
#      ``backends = ["bing"]  # temporaly disable html and lite backends``
#      （原文注释如此）—— 所以报错 URL 是 ``bing.com`` 而不是 duckduckgo.com，
#      **项目记录里写的"DuckDuckGo"并没有错，错的是这个库自己换到了 Bing**；
#   2. 它创建 primp 客户端时设了 ``follow_redirects=False``，
#      而 ``www.bing.com`` 在国内会被 **302** 到 ``cn.bing.com`` ——
#      库于是永远只拿到 302，判定失败；
#   3. 实测（2026-10-06，见 records）：
#      库配置 → ``302 Location: https://cn.bing.com/search?q=test``；
#      跟随重定向 → ``200``，约 99 KB，含 **10 个** ``li.b_algo`` 结果块。
#
# 所以这里内建一个"**跟随重定向 + 自己解析**"的 Bing 源作为兜底。
# 三个刻意的取舍：
#   * 用项目**必需依赖** ``requests`` 发请求（不新增依赖）；
#   * 用**标准库** ``html.parser`` 解析（不依赖 lxml/bs4 —— 它们是
#     ``duckduckgo_search`` 的间接依赖，而搜索对本项目是**可选功能**，
#     不能假定它们存在）；
#   * 只在主源不可用时才启用，不改变能正常联网用户的既有行为。

#: Bing 搜索入口（重定向到 ``cn.bing.com`` 由 requests 跟随）。
_BING_URL = "https://www.bing.com/search"

#: 带浏览器 UA：Bing 对无 UA / 爬虫 UA 的请求会拒答或重定向到验证页。
#: 这与 ``model_hub.py`` 里 HF 端点"必须带自己的 UA"是同一类坑。
_BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _decode_bing_url(href: str) -> str:
    """还原 Bing 的跳转链接。

    Bing 的结果链接常常是 ``https://www.bing.com/ck/a?u=a1aHR0cHM6Ly8...``，
    真实地址是 ``u`` 参数里去掉前两个字符后的 **base64url**（缺省 padding）。
    解不出来时原样返回，至少链接是可点的。
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
            except Exception:  # noqa: BLE001 —— 解码失败不该让整次搜索失败
                return href
    return href


class _BingParser(HTMLParser):
    """从 Bing 结果页里抽出 ``li.b_algo`` 的标题 / 链接 / 摘要。

    只认这一种结构。Bing 改版会让它"抽不到东西"（表现为没有结果），
    而不会崩 —— 这是刻意的：搜索失败可以如实告知，崩掉不行。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[SearchResult] = []
        self._in_li = False        # 当前是否在一个 b_algo 里
        self._depth = 0            # b_algo 内 li 的嵌套深度（结束时归零）
        self._in_h2 = False
        self._in_p = False
        self._href = ""
        self._title: List[str] = []
        self._snippet: List[str] = []

    # ---------- 内部 ----------

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

    # ---------- HTMLParser 回调 ----------

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
    """内建 Bing 源：**跟随重定向**地取结果页，再用标准库解析。

    与 ``duckduckgo_search`` 的唯一实质差别就是"跟随重定向"这一件事，
    而那正是国内网络下它失败的全部原因（见本节顶部说明）。
    """
    outcome = SearchOutcome(query=query, source="bing")
    started = time.perf_counter()
    try:
        import requests
    except ImportError:  # requests 是必需依赖，正常不会走到这里
        outcome.error = "requests 未安装"
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
    except Exception as exc:  # noqa: BLE001 —— 解析失败按"源改版"处理
        outcome.error = f"解析失败: {exc}"
        outcome.error_kind = "source_changed"
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    outcome.results = parser.results[:max(1, int(max_results or 5))]
    outcome.ok = True
    outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info("搜索完成（内建 Bing 源）：%r → %d 条结果，%d ms",
             query[:40], outcome.count, outcome.elapsed_ms)
    return outcome


class SearchEngine:
    """搜索编排：先走 DuckDuckGo 库，失败时回落到内建 Bing 源。

    刻意**不在这里做静默降级**：拿不到结果就如实返回 ``ok=False`` 与原因，
    由调用方决定怎么告诉用户（"明确告知用户而非静默继续"，任务书要求）。
    """

    def __init__(self, max_results: int = 5, fallback_enabled: bool = True):
        self.max_results = max(1, int(max_results or 5))
        #: 主源失败时是否启用内建 Bing 源（见 :func:`search_bing`）。
        #: 保留开关是为了**可测性**：测试用桩 DDGS 模拟各种主源情形时，
        #: 兜底源会真的发起网络请求、把"模拟失败"变成"实际成功"，
        #: 那样测的就不是被测对象了。产品路径保持默认开启。
        self.fallback_enabled = bool(fallback_enabled)

    def _search_ddgs(self, text: str) -> SearchOutcome:
        """源 1：``duckduckgo_search``（可选依赖）。

        ⚠️ **8.1.1 起这个库在国内网络下必然失败**，原因写在
        :func:`search_bing` 的说明里；这里保留它是因为在能直连
        DuckDuckGo / bing 的地区它仍然是更完整的解析器，不该一刀切删掉。
        """
        outcome = SearchOutcome(query=text, source="ddgs")
        started = time.perf_counter()
        try:
            from duckduckgo_search import DDGS

            with DDGS() as ddgs:
                raw = list(ddgs.text(text, max_results=self.max_results))
        except ImportError:
            outcome.error = "duckduckgo_search 未安装"
            outcome.error_kind = "no_dependency"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        except Exception as exc:  # noqa: BLE001 —— 网络/解析/限流都可能失败
            outcome.error = str(exc) or exc.__class__.__name__
            outcome.error_kind = classify_error(outcome.error)
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
        log.info("搜索完成（ddgs）：%r → %d 条结果，%d ms",
                 text[:40], outcome.count, outcome.elapsed_ms)
        return outcome

    def search(self, query: str) -> SearchOutcome:
        """执行一次搜索，返回结构化结果（**不抛异常**，失败也在返回值里）。

        两级源（任务二第 6 条"评估备用搜索源"的落地）：

        1. ``duckduckgo_search`` —— 能满足就用它，保持既有行为；
        2. :func:`search_bing` —— 内建源，跟随重定向后自己解析，
           用项目**必需依赖** requests 与标准库 html.parser，**不新增依赖**。

        两个都失败时，把**两个源各自的原因**都带回 ``error``，
        让用户/排查者一眼看出是哪一层不通，而不是只看到一句笼统的失败。
        """
        text = (query or "").strip()
        outcome = SearchOutcome(query=text)
        if not text:
            outcome.error = "empty_query"
            outcome.error_kind = "empty"
            return outcome

        primary = self._search_ddgs(text)
        # 判据是 ``ok`` 而不是 ``usable``：主源**连通**时它的答复就该被采信，
        # 包括"连通但没有结果"（ok=True, count=0）。那是有效信息，不是故障——
        # 把它当故障去回落到第二个源，会让一次"确实没搜到"的查询白白多跑一次
        # 网络请求，而且会让"没搜到"和"搜不通"这两种**必须给用户不同说法**的
        # 情形混成一种（既有回归用例正是断言这两者要分开）。
        # 另有一层理由：8.1.1 的主源实际打的就是 Bing（backends=["bing"]），
        # 与兜底源是同一个搜索引擎，主源连通时兜底不会有额外收获。
        if primary.ok:
            return primary

        if not self.fallback_enabled:
            # 兜底被显式关掉（测试路径）：把主源的原因如实带回
            primary.source = primary.source or "ddgs"
            primary.error_kind = primary.error_kind or classify_error(primary.error)
            return primary

        fallback = search_bing(text, self.max_results)
        if fallback.usable:
            log.info("主搜索源不可用（%s），已回落到内建 Bing 源：%d 条结果",
                     primary.error_kind or primary.error, fallback.count)
            return fallback

        outcome.error = f"{primary.error or '主源无结果'} ｜ 备用源：{fallback.error or '无结果'}"
        # 分类以**备用源**为准：它才是真正能反映当前网络状况的那一个
        # （主源在国内是"必然失败"，它的分类对用户没有指导意义）。
        outcome.error_kind = fallback.error_kind or primary.error_kind or "unknown"
        log.warning("两个搜索源都失败：%s", outcome.error)
        return outcome

    # ---------- 兼容旧接口 ----------

    def search_text(self, query: str) -> str:
        """旧接口：返回注入用文本（无可用结果时为空串）。

        保留它是为了不破坏既有调用方与测试；新代码应直接用 :meth:`search`，
        这样才能拿到"为什么没有结果"。
        """
        return self.search(query).as_context()
