# -*- coding: utf-8 -*-
"""联网搜索链路测试（优化已知问题-2 任务二）。

任务书要求"逐环节排查"四个环节，本文件把其中**可机验**的三项做成断言，
并覆盖"优化方向"里的每一条：

| 任务书优化方向 | 对应断言 |
| --- | --- |
| 搜索结果精简，只保留最相关的几条 | 条数上限、单条摘要上限 |
| 用明确的指示要求模型基于搜索结果回答 | 注入文本必须含引用指令与编号 |
| 注入格式结构化（编号列表） | `[1] [2] [3]` 逐条渲染、每条独立成块 |
| 结果为空或质量差时明确告知用户 | 失败/空结果 → 不注入 + 给用户文案 |

外加**逐环节可观测**：`Backend.search_diagnostics()` 把"是否开启 / 是否执行 /
返回条数 / 是否成功 / 失败原因 / 给用户的说明"全部暴露出来，测试直接断言它，
不需要去解析日志。

桩 DDGS（替换 `duckduckgo_search.DDGS`）：不联网、不依赖搜索结果随时变化，
但被测的是**真 SearchEngine / 真 Backend / 真 conversation**。

跑法：
    python tools/test_search.py
退出码：0 = 全部通过，1 = 存在失败项。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND_DIR)

from config import Config  # noqa: E402
from search_engine import (  # noqa: E402
    CONTEXT_CHARS,
    FAIL_KIND_KEYS,
    SNIPPET_CHARS,
    SearchEngine,
    SearchOutcome,
    SearchResult,
    classify_error,
)

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


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 桩

FAKE_ROWS = [
    {"title": "2026 年企业所得税税率",
     "body": "一般企业适用 25%，小型微利企业减按 25% 计入应纳税所得额。" * 12,
     "href": "https://example.com/tax-1"},
    {"title": "小型微利企业优惠",
     "body": "年应纳税所得额不超过 300 万元的部分，减按 25% 计入应纳税所得额。",
     "href": "https://example.com/tax-2"},
    {"title": "增值税税率表", "body": "13% / 9% / 6% 三档。", "href": "https://example.com/vat"},
]


class FakeDDGS:
    rows = FAKE_ROWS
    error = None
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def text(self, query, max_results=5):
        FakeDDGS.calls += 1
        if FakeDDGS.error:
            raise RuntimeError(FakeDDGS.error)
        return list(FakeDDGS.rows)[:max_results]


FAKE_BING_ROWS = [
    {"title": "内建源标题一", "snippet": "内建源摘要一", "url": "https://example.org/fb-1"},
    {"title": "内建源标题二", "snippet": "内建源摘要二", "url": "https://example.org/fb-2"},
]

#: 内建 Bing 兜底源的可观测状态（任务二新增）。
FAKE_BING = {"rows": [], "error": "ConnectError: error sending request for url", "calls": 0}


def fake_search_bing(query, max_results=5, timeout=15):
    """替换 ``search_engine.search_bing``。

    产品路径上，主源失败后会**真的**去请求 Bing；而本文件要测的是"主源失败时
    后端怎么告知用户"，不能让真实网络参与进来 —— 否则一次成功的联网会让
    "失败告知"这类断言变成假阳性/假阴性（这正是加兜底源时踩到的）。
    """
    FAKE_BING["calls"] += 1
    outcome = SearchOutcome(query=query, source="bing")
    if FAKE_BING["error"]:
        outcome.error = FAKE_BING["error"]
        outcome.error_kind = classify_error(outcome.error)
        return outcome
    outcome.results = [SearchResult(**row) for row in FAKE_BING["rows"]][:max_results]
    outcome.ok = True
    return outcome


def install_fake_bing(rows=None, error="ConnectError: error sending request for url"):
    """安装内建源桩。默认让它**失败**，保持"主源失败"的既有测试语义。"""
    import search_engine
    search_engine.search_bing = fake_search_bing
    FAKE_BING["rows"] = list(rows or [])
    FAKE_BING["error"] = error
    FAKE_BING["calls"] = 0


def install_fake():
    import duckduckgo_search
    duckduckgo_search.DDGS = FakeDDGS
    FakeDDGS.error = None
    FakeDDGS.rows = FAKE_ROWS
    FakeDDGS.calls = 0
    install_fake_bing()


def make_backend(search_enabled=True):
    """真 Backend + 桩引擎（只替换"模型怎么吐字"）。"""
    import main as backend_main

    config = Config()
    config.model_path = "stub.gguf"
    config.search_default = bool(search_enabled)
    config.speed_hint = False

    seen = {}

    class StubLlama:
        def tokenize(self, data, **kwargs):
            return list(range(max(1, len(data) // 3)))

        def create_chat_completion(self, messages=None, stream=False, **kwargs):
            seen["messages"] = messages
            if not stream:
                return {"choices": [{"message": {"content": "答"}}]}
            return iter([{"choices": [{"delta": {"content": "答"}}]}])

    class StubEngine(backend_main.LLMEngine):
        def __init__(self):
            self.llm = StubLlama()
            self.reasoning_enabled = True
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

    backend = backend_main.Backend(config)
    backend.llm = StubEngine()
    return backend, seen


# ---------------------------------------------------------------- 一

def test_structured_results():
    section("一、结果返回：结构化（不再是拼好的一段文本）")
    engine = SearchEngine(max_results=5)
    outcome = engine.search("2026 年企业所得税税率")

    check("返回 SearchOutcome 而不是字符串", isinstance(outcome, SearchOutcome))
    check("成功标记 ok", outcome.ok is True)
    check("拿到 3 条结果", outcome.count == 3, f"实际 {outcome.count}")
    check("每条都是 SearchResult", all(isinstance(r, SearchResult) for r in outcome.results))
    check("标题/摘要/链接都不为空",
          all(r.title and r.snippet and r.url for r in outcome.results))
    check("记录了查询词", outcome.query == "2026 年企业所得税税率")
    check("记录了耗时字段（毫秒，允许 0）", outcome.elapsed_ms >= 0)
    check("usable 为真（成功且有结果）", outcome.usable is True)

    # 空查询不联网
    empty_query = engine.search("   ")
    check("空查询直接判失败且不联网", empty_query.ok is False and empty_query.error == "empty_query")


def test_result_limit():
    section("二、结果精简：条数与摘要长度上限（优化方向一）")
    engine = SearchEngine(max_results=2)
    outcome = engine.search("税率")
    check("条数上限生效（max_results=2）", outcome.count <= 2, f"实际 {outcome.count}")

    long_line = SearchResult(title="T", snippet="正文" * 3000, url="https://e.com").line(1)
    check("单条摘要被截断到上限附近",
          len(long_line) <= SNIPPET_CHARS + 60,
          f"{len(long_line)} 字符（上限 {SNIPPET_CHARS}）")
    check("截断处有省略号", "…" in long_line)

    many = SearchOutcome(query="q", ok=True, results=[
        SearchResult(title=f"标题{i}", snippet="正文" * 300, url=f"https://e.com/{i}")
        for i in range(10)])
    context = many.as_context()
    check("总长上限生效（按条截断）",
          len(context) <= CONTEXT_CHARS + 200, f"实际 {len(context)} 字符")
    check("总长受限时保留前面的完整条目（不是每条都砍短）",
          "[1]" in context and "…" not in context.split("[2]")[0].split("\n")[1][:SNIPPET_CHARS],
          context[:120])


def test_injection_format():
    section("三、注入格式：编号 + 明确指令（优化方向二、三）")
    engine = SearchEngine(max_results=5)
    context = engine.search("税率").as_context()

    check("注入文本非空", len(context) > 0)
    check("带编号 [1]", "[1]" in context)
    check("带编号 [2]", "[2]" in context)
    check("每条独立成块（编号行以 [n] 开头）",
          sum(1 for line in context.splitlines() if line.startswith("[")) == 3,
          context)
    check("含明确的引用指令", "使用要求" in context)
    check("指令要求标注编号", "标注编号" in context)
    check("指令给出「不足就说不知道」的出口", "不足以回答" in context)
    check("注入文本不含「未找到」这类会被误当资料的句子",
          "未找到" not in context and "搜索失败" not in context)
    check("注入量占 8192 窗口的比例可控（< 1500 token 粗估）",
          len(context) // 2 < 1500, f"{len(context)} 字符 ≈ {len(context) // 2} token")


def test_failure_paths():
    section("四、失败与空结果：明确告知，不静默继续，也不伪装成资料（优化方向四）")
    engine = SearchEngine(max_results=5)

    FakeDDGS.error = "Connection timed out"
    failed = engine.search("随便问点什么")
    check("失败时 ok=False", failed.ok is False)
    check("失败原因被记录", bool(failed.error))
    check("失败时不产出注入文本（关键：旧实现会把它喂给模型）",
          failed.as_context() == "")
    check("失败时 usable 为假", failed.usable is False)
    FakeDDGS.error = None

    FakeDDGS.rows = []
    empty = engine.search("不可能有结果的问题")
    check("无结果时 ok=True 且条数为 0（区分于失败）",
          empty.ok is True and empty.count == 0)
    check("无结果时不产出注入文本", empty.as_context() == "")
    FakeDDGS.rows = FAKE_ROWS

    # 质量差（条目缺字段）不应崩
    FakeDDGS.rows = [{"title": "", "body": "", "href": ""}, "不是字典", 123]
    weird = engine.search("畸形结果")
    check("畸形结果不崩溃且被过滤", weird.ok is True and weird.count == 1,
          f"条数 {weird.count}")
    check("空字段渲染成占位而不是 None",
          "（无标题）" in weird.as_context(), weird.as_context()[:80])
    FakeDDGS.rows = FAKE_ROWS


def test_backend_wiring():
    section("五、后端接线：四个环节都有迹可循")
    install_fake()
    backend, seen = make_backend(search_enabled=True)
    lines = list(backend.stream({"action": "chat_stream", "message": "企业所得税税率是多少"}))
    diag = backend.search_diagnostics()

    check("环节1 触发：搜索确实被执行", diag["executed"] is True)
    check("环节1 触发：开关状态可见", diag["enabled"] is True)
    check("环节2 返回：条数可见且为 3", diag["results"] == 3, str(diag))
    check("环节2 返回：成功标记可见", diag["ok"] is True)
    check("成功时没有多余的告知文案", diag["note"] == "", repr(diag["note"]))
    check("桩 DDGS 确实被调用过", FakeDDGS.calls >= 1, f"{FakeDDGS.calls} 次")

    joined = "\n".join(str(m.get("content", "")) for m in seen.get("messages", []))
    check("环节3 注入：搜索结果真的进了 messages", "【搜索结果】" in joined)
    check("环节3 注入：带编号", "[1]" in joined)
    check("环节3 注入：带引用指令", "使用要求" in joined)
    check("环节3 注入：搜索结果排在用户问题之前",
          joined.index("【搜索结果】") < joined.index("企业所得税税率是多少"),
          joined[-200:])
    check("环节4 模型利用：注入内容是独立一段（不与问题混在一行）",
          "【搜索结果】" in joined and "\n\n问题：" in joined, joined[-260:])

    # 关闭开关时不应联网
    install_fake()
    off_backend, off_seen = make_backend(search_enabled=False)
    list(off_backend.stream({"action": "chat_stream", "message": "问题"}))
    off_diag = off_backend.search_diagnostics()
    check("关闭搜索时不执行搜索", off_diag["executed"] is False)
    check("关闭搜索时不调用 DDGS", FakeDDGS.calls == 0, f"{FakeDDGS.calls} 次")
    check("关闭搜索时不注入搜索结果",
          "【搜索结果】" not in "\n".join(
              str(m.get("content", "")) for m in off_seen.get("messages", [])))

    # 失败时告诉用户
    install_fake()
    FakeDDGS.error = "Connection timed out"
    fail_backend, _ = make_backend(search_enabled=True)
    fail_lines = list(fail_backend.stream({"action": "chat_stream", "message": "再问一次"}))
    fail_diag = fail_backend.search_diagnostics()
    first_text = "".join(line.get("content", "") for line in fail_lines
                         if line.get("type") == "delta")
    check("失败时诊断里带告知文案", bool(fail_diag["note"]))
    check("失败文案真的发给前端", "没有" in first_text and "搜索结果" in first_text,
          repr(first_text[:80]))
    check("失败文案随界面语言（中文界面用中文）", "⚠️" in first_text)
    FakeDDGS.error = None

    # 空结果时同样告知
    install_fake()
    FakeDDGS.rows = []
    empty_backend, _ = make_backend(search_enabled=True)
    empty_lines = list(empty_backend.stream({"action": "chat_stream", "message": "问"}))
    empty_diag = empty_backend.search_diagnostics()
    empty_first = "".join(line.get("content", "") for line in empty_lines
                          if line.get("type") == "delta")
    check("空结果时诊断里带告知文案", bool(empty_diag["note"]), str(empty_diag))
    check("空结果文案与失败文案不同（区分两种情形）",
          empty_diag["note"] != fail_diag["note"])
    check("空结果文案真的发给前端", "没有" in empty_first, repr(empty_first[:80]))
    FakeDDGS.rows = FAKE_ROWS


def test_lang_keys():
    section("六、文案：中英双语齐全且占位符一致")
    import json

    import i18n

    for locale in ("zh-Hans", "en"):
        path = os.path.join(BACKEND_DIR, "lang", f"{locale}.json")
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        sys_section = data.get("sys", {})
        for key in ("search_failed", "search_empty"):
            check(f"{locale} 含 sys.{key}", key in sys_section)
        if "search_failed" in sys_section:
            check(f"{locale} 的 search_failed 含 {{error}} 占位符",
                  "{error}" in sys_section["search_failed"])
    # 用真 i18n 取一次，确认能渲染
    try:
        i18n.get().set_locale("en")
        rendered = i18n.t("sys.search_failed", error="timeout")
        check("英文文案能渲染出原因", "timeout" in rendered, rendered)
        i18n.get().set_locale("zh-Hans")
        rendered_cn = i18n.t("sys.search_empty")
        check("中文文案能渲染", len(rendered_cn) > 0)
    except Exception as exc:  # pragma: no cover
        check("i18n 渲染不抛异常", False, str(exc))


def test_builtin_fallback():
    """六、任务二（自测问题修复-1）：内建 Bing 兜底源与失败分类。

    这一组覆盖本轮新增的两件事：
      * 主源（duckduckgo_search）在国内必然失败时，**内建源要接管**；
      * 失败要给**面向用户的类别**，而不是把异常栈抛出去。
    """
    section("六、内建兜底源与失败分类（任务二）")

    check("产品默认开启兜底源（不能把搜索修成静默不可用）",
          SearchEngine().fallback_enabled is True)

    # 主源失败 + 兜底成功 → 用兜底的结果
    install_fake()
    FakeDDGS.error = "https://www.bing.com/search ConnectError: error sending request for url"
    install_fake_bing(rows=FAKE_BING_ROWS, error=None)
    engine = SearchEngine(max_results=5)
    outcome = engine.search("企业所得税")
    check("主源失败时兜底源被调用", FAKE_BING["calls"] == 1, f"{FAKE_BING['calls']} 次")
    check("兜底成功后判为可用", outcome.usable is True, str(outcome))
    check("结果来自内建源", outcome.source == "bing", outcome.source)
    check("兜底结果条数正确", outcome.count == 2, str(outcome.count))
    check("兜底结果进了注入文本", "内建源标题一" in outcome.as_context())
    FakeDDGS.error = None

    # 主源成功时不碰兜底（不该多发一次网络请求）
    install_fake()
    install_fake_bing(rows=FAKE_BING_ROWS, error=None)
    SearchEngine(max_results=5).search("企业所得税")
    check("主源成功时不调用兜底源", FAKE_BING["calls"] == 0, f"{FAKE_BING['calls']} 次")

    # 两个源都失败 → 两个原因都带回，类别以备用源为准
    install_fake()
    FakeDDGS.error = "some primary failure"
    install_fake_bing(error="HTTP 429 too many requests")
    both = SearchEngine(max_results=5).search("企业所得税")
    check("两个源的原因都写进 error",
          "some primary failure" in both.error and "429" in both.error, both.error)
    check("失败类别以备用源为准（rate_limit）", both.error_kind == "rate_limit",
          both.error_kind)
    FakeDDGS.error = None

    # 兜底可关闭（测试隔离用；关掉后如实返回主源原因）
    install_fake()
    FakeDDGS.error = "Connection timed out"
    install_fake_bing(rows=FAKE_BING_ROWS, error=None)
    off = SearchEngine(max_results=5, fallback_enabled=False).search("企业所得税")
    check("关闭兜底后不调用兜底源", FAKE_BING["calls"] == 0, f"{FAKE_BING['calls']} 次")
    check("关闭兜底后判为不可用", off.usable is False)
    check("关闭兜底后带出类别", off.error_kind == "timeout", off.error_kind)
    FakeDDGS.error = None

    # ---------- 分类器 ----------
    cases = [
        ("ConnectError: error sending request for url", "network"),
        ("SSLError: certificate verify failed", "certificate"),
        ("HTTP 429 too many requests", "rate_limit"),
        ("Read timed out", "timeout"),
        ("return None. params={}", "source_changed"),
        ("完全没有见过的错误", "unknown"),
        ("", "unknown"),
    ]
    for text, want in cases:
        got = classify_error(text)
        check(f"分类 {want}：{text[:38]!r}", got == want, f"得到 {got}")

    # 分类码都必须有对应的语言包键，否则 _t 会回落成原始键名给用户看
    check("每个分类码都有语言包键",
          set(FAIL_KIND_KEYS) >= {"network", "timeout", "certificate",
                                  "rate_limit", "source_changed", "no_dependency",
                                  "empty", "unknown"},
          str(sorted(FAIL_KIND_KEYS)))

    import json
    for locale in ("zh-Hans", "en"):
        path = os.path.join(BACKEND_DIR, "lang", f"{locale}.json")
        with open(path, "r", encoding="utf-8-sig") as handle:
            sys_section = json.load(handle).get("sys", {})
        # FAIL_KIND_KEYS 里存的是完整键（"sys.xxx"），语言包 sys 段里只有 "xxx"
        wanted = {key.split(".", 1)[1] for key in FAIL_KIND_KEYS.values()}
        missing = [key for key in wanted if key not in sys_section]
        check(f"{locale} 含全部失败分类文案", not missing, str(missing))
        # 面向用户的分类文案不该再暴露底层异常串（只有兜底的 search_failed 带 {error}）
        leaked = [key for key in wanted
                  if key != "search_failed" and key in sys_section
                  and "{error}" in sys_section[key]]
        check(f"{locale} 的分类文案不暴露原始错误占位符", not leaked, str(leaked))


def main():
    print("联网搜索链路测试（优化已知问题-2 任务二）")
    install_fake()
    test_structured_results()
    test_result_limit()
    test_injection_format()
    test_failure_paths()
    test_backend_wiring()
    test_lang_keys()
    test_builtin_fallback()

    print("\n" + "-" * 42)
    print(f"共 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
