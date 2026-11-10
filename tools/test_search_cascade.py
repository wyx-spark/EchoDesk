# -*- coding: utf-8 -*-
"""
[EN]
Unit test for v0.2.6 task 2.3: ordered multi-provider search cascade.

Verification targets:
* The provider order is DuckDuckGo -> Bing -> SearXNG.
* A primary timeout or HTTP 429 switches silently to the next provider and still
  returns usable results.
* A reachable "no results" answer is accepted and does not trigger fallback.
* When every provider fails, the combined reason is returned once.
* The SearXNG provider parses the documented JSON response.

[ZH]
v0.2.6 任务 2.3 的单元测试：有序多提供方搜索级联。

验证目标：
* 提供方顺序为 DuckDuckGo → Bing → SearXNG。
* 主提供方超时或返回 HTTP 429 时静默切换到下一个，并仍返回可用结果。
* "连通但无结果"被采信，不触发回退。
* 全部提供方失败时，合并原因只上报一次。
* SearXNG 提供方按文档化的 JSON 响应解析。

Run:
    python tools/test_search_cascade.py
Exit code: 0 = all checks passed, 1 = at least one failure.
"""
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import search_engine as se  # noqa: E402
from search_engine import SearchEngine, SearchOutcome, SearchProvider, SearchResult  # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


class StubProvider(SearchProvider):
    """
    [EN]
    Provider stub that returns a preconfigured outcome and records invocations.

    [ZH]
    返回预置结果并记录调用次数的提供方桩。
    """

    def __init__(self, name, outcome):
        super().__init__(timeout=1)
        self.name = name
        self.outcome = outcome
        self.calls = 0

    def search(self, query, max_results):
        self.calls += 1
        return self.outcome


def fail(name, kind, error="boom"):
    return StubProvider(name, SearchOutcome(query="q", source=name, ok=False,
                                            error=error, error_kind=kind))


def ok(name, count=1):
    results = [SearchResult(title=f"{name} title {i}", snippet="snippet",
                            url=f"https://example.com/{name}/{i}")
               for i in range(count)]
    return StubProvider(name, SearchOutcome(query="q", source=name, ok=True,
                                            results=results))


def main():
    print("\n=== 1. 级联顺序与静默回退 ===")
    primary = fail("ddgs", "timeout", "Read timed out")
    fallback = ok("bing", 2)
    engine = SearchEngine(max_results=5, providers=[primary, fallback])
    outcome = engine.search("测试查询")
    check("主提供方超时后仍返回可用结果", outcome.usable is True, str(outcome))
    check("结果来自备用提供方 bing", outcome.source == "bing", outcome.source)
    check("两个提供方都被尝试", primary.calls == 1 and fallback.calls == 1,
          f"{primary.calls}/{fallback.calls}")
    check("静默回退不向调用方暴露主源错误", outcome.error == "", outcome.error)

    primary = fail("ddgs", "rate_limit", "HTTP 429 too many requests")
    fallback = ok("bing", 1)
    outcome = SearchEngine(providers=[primary, fallback]).search("测试查询")
    check("主提供方 429 时自动回退", outcome.source == "bing" and outcome.usable,
          outcome.source)

    ddg = fail("ddgs", "timeout")
    bing = fail("bing", "rate_limit", "HTTP 429")
    searx = ok("searxng", 3)
    outcome = SearchEngine(providers=[ddg, bing, searx]).search("测试查询")
    check("第三提供方 SearXNG 被调用并返回结果",
          outcome.source == "searxng" and outcome.count == 3, outcome.source)
    check("三级级联全部按顺序尝试", ddg.calls == 1 and bing.calls == 1
          and searx.calls == 1, f"{ddg.calls}/{bing.calls}/{searx.calls}")

    print("\n=== 2. 连通但无结果不触发回退 ===")
    empty_primary = StubProvider("ddgs", SearchOutcome(query="q", source="ddgs",
                                                       ok=True, results=[]))
    untouched = ok("bing", 1)
    outcome = SearchEngine(providers=[empty_primary, untouched]).search("测试查询")
    check("无结果但连通视为有效答复", outcome.ok is True and outcome.count == 0,
          str(outcome))
    check("不再无谓调用备用源", untouched.calls == 0, str(untouched.calls))

    print("\n=== 3. 全部失败时合并上报 ===")
    ddg = fail("ddgs", "network", "ConnectError")
    bing = fail("bing", "rate_limit", "HTTP 429 too many requests")
    searx = fail("searxng", "timeout", "Read timed out")
    outcome = SearchEngine(providers=[ddg, bing, searx]).search("测试查询")
    check("全部失败时 ok=False", outcome.ok is False)
    check("错误包含全部三个来源",
          all(token in outcome.error for token in ("ConnectError", "429", "timed out")),
          outcome.error)
    check("失败类别取最后一个提供方", outcome.error_kind == "timeout",
          outcome.error_kind)
    check("没有结果可注入", outcome.as_context() == "", outcome.as_context())

    print("\n=== 4. 默认提供方装配 ===")
    default = SearchEngine(max_results=5)
    check("默认顺序为 ddgs -> bing",
          [p.name for p in default.providers] == ["ddgs", "bing"],
          str([p.name for p in default.providers]))
    configured = SearchEngine(max_results=5, searxng_url="http://127.0.0.1:8080")
    check("配置 SearXNG 后成为第三提供方",
          [p.name for p in configured.providers] == ["ddgs", "bing", "searxng"],
          str([p.name for p in configured.providers]))
    only_primary = ok("ddgs", 1)
    unused_fallback = ok("bing", 1)
    SearchEngine(fallback_enabled=False,
                 providers=[only_primary, unused_fallback]).search("测试查询")
    check("关闭兜底时不再调用备用提供方",
          only_primary.calls == 1 and unused_fallback.calls == 0,
          f"{only_primary.calls}/{unused_fallback.calls}")

    print("\n=== 5. SearXNG 提供方解析 JSON ===")
    payload = {"results": [
        {"title": "结果一", "content": "摘要一", "url": "https://example.org/1"},
        {"title": "结果二", "content": "摘要二", "url": "https://example.org/2"},
    ]}
    response = mock.Mock(status_code=200)
    response.json.return_value = payload
    with mock.patch("requests.get", return_value=response) as getter:
        provider = se.SearXNGProvider("http://127.0.0.1:8080", timeout=3)
        outcome = provider.search("查询", 5)
    check("SearXNG 返回成功", outcome.ok is True and outcome.count == 2, str(outcome))
    check("SearXNG 请求使用 JSON 格式",
          getter.call_args.kwargs["params"]["format"] == "json",
          str(getter.call_args))
    check("SearXNG 结果字段映射正确",
          outcome.results[0].title == "结果一"
          and outcome.results[0].snippet == "摘要一"
          and outcome.results[0].url == "https://example.org/1",
          str(outcome.results[0]))

    response_429 = mock.Mock(status_code=429)
    with mock.patch("requests.get", return_value=response_429):
        limited = se.SearXNGProvider("http://127.0.0.1:8080").search("查询", 5)
    check("SearXNG 429 归类为限流并可继续级联",
          limited.ok is False and limited.error_kind == "rate_limit",
          limited.error_kind)

    print("\n=== 6. 失败分类仍可用 ===")
    check("超时分类", se.classify_error("Read timed out") == "timeout")
    check("429 分类", se.classify_error("HTTP 429 too many requests") == "rate_limit")

    print("\n------------------------------------------")
    print(f"共 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
