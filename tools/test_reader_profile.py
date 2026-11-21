# -*- coding: utf-8 -*-
"""
[EN]
Release gate for the **Reader profile** (branch ``reader``).

The Reader edition claims three things that no other test pins down, so this
module asserts them directly:

1. **Document-driven dynamic context** — the window is planned as
   ``document tokens + answer budget + safety margin``, clamped to
   ``[initial context, 131072]``; a document beyond the ceiling is reported
   instead of being silently truncated.
2. **Quantised KV cache** — the engine records ``q8_0`` and resolves the
   matching ``ggml_type`` (8), never silently falling back to F16.
3. **The removed stacks are really gone** — neither the source tree nor the
   import graph may still reference vision / speech / media / knowledge /
   memory / retrieval / extractive / automation. This is the assertion that
   replaces the upstream VLM and RAG checks on this branch.

No model and no network are required.

[ZH]
**Reader 版本**（``reader`` 分支）的发布门禁。

Reader 版作出的三项声明此前没有任何测试钉住，本模块直接断言：

1. **文档驱动的动态上下文** —— 窗口按 ``文档 tokens + 回答预算 + 安全余量``
   规划，并收敛到 ``[初始窗口, 131072]``；超过上限的文档会被如实报告，
   而不是静默截断。
2. **量化 KV 缓存** —— 引擎记录 ``q8_0``，并解析出对应的 ``ggml_type``（8），
   绝不静默退回 F16。
3. **被删的栈确实没了** —— 源码树与导入图都不得再引用 vision / speech /
   media / knowledge / memory / retrieval / extractive / automation。
   这条断言在本分支上取代上游的 VLM 与 RAG 检查。

无需模型、无需联网。

Run: ``python tools\\test_reader_profile.py``. Exit code 0 means all passed.
"""
import importlib.util
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import llm_engine  # noqa: E402
import version  # noqa: E402

PASSED = 0
FAILED = []

#: [EN] Modules the Reader profile deletes; none may be importable or imported.
#: [ZH] Reader 版删除的模块；既不可被导入，也不可出现在导入语句里。
REMOVED_MODULES = (
    "vision", "knowledge", "speech", "media", "extractive",
    "memory", "retrieval", "automation", "agent", "agent_tools", "mcp_client",
    # [EN] Pure-reader backend refactor (first stage): the market, model library,
    # [EN] provider routing, local HTTP API, web search, update checker, slash-command
    # [EN] layer and prompt-template library are physically deleted. Asserting their
    # [EN] absence here makes "the backend can never reach the network" a gate.
    # [ZH] 纯阅读器后端重构（第一阶段）：市场、模型库、Provider 路由、本地 HTTP API、
    # [ZH] 联网搜索、更新检查、斜杠指令层与提示词模板库已物理删除。在此断言它们不存在，
    # [ZH] 使"后端不可能触达网络"成为一条门禁。
    "marketplace", "skills_runtime", "model_hub", "model_query", "download_utils",
    "providers", "local_api", "search_engine", "update_check",
    "command_handler", "prompt_library",
)
#: [EN] The same names plus the Playwright chain, banned from packaged sources.
#: [ZH] 同一批名字再加上 Playwright 依赖链，禁止出现在打包源码里。
BANNED_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(" + "|".join(REMOVED_MODULES + ("playwright",)) + r")\b",
    re.MULTILINE,
)


def check(name, condition, detail=""):
    """[EN] Record one assertion / [ZH] 记录一条断言。"""
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def read_source(name):
    """[EN] Read one backend source file / [ZH] 读取一个后端源码文件。"""
    with io.open(os.path.join(BACKEND, name), "r", encoding="utf-8") as handle:
        return handle.read()


def test_dynamic_context():
    """[EN] Document-driven window planning / [ZH] 文档驱动的窗口规划。"""
    plan = llm_engine.LLMEngine.plan_context_for_tokens
    initial = llm_engine.DEFAULT_INITIAL_CONTEXT
    budget = llm_engine.DEFAULT_OUTPUT_TOKEN_BUDGET
    margin = llm_engine.CONTEXT_SAFETY_MARGIN
    ceiling = llm_engine.MAX_DYNAMIC_CONTEXT

    check("回答预算与安全余量为正", budget > 0 and margin > 0, f"budget={budget} margin={margin}")
    check("硬上限来自 Phi-3-Mini-128K（131072）", ceiling == 131072, f"ceiling={ceiling}")

    # [EN] The exact formula, not merely "it grows".
    # [ZH] 断言的是确切公式，而不是"它变大了"这种模糊结论。
    expected = 4000 + budget + margin
    check("小文档按 文档+预算+余量 精确取值", plan(4000) == max(initial, expected),
          f"plan(4000)={plan(4000)} expected={max(initial, expected)}")
    check("空文档收敛到初始窗口", plan(0) == max(initial, budget + margin), f"plan(0)={plan(0)}")
    check("负值不产生非法窗口", plan(-10) >= initial, f"plan(-10)={plan(-10)}")
    check("窗口随文档增大", plan(20000) > plan(4000), f"{plan(20000)} vs {plan(4000)}")

    engine = llm_engine.LLMEngine.__new__(llm_engine.LLMEngine)
    huge = ceiling + 1
    check("超上限文档被识别为装不下", engine.document_exceeds_capacity(huge) is True, f"tokens={huge}")
    check("恰好等于上限仍可接受", engine.document_exceeds_capacity(ceiling) is False, f"tokens={ceiling}")
    check("上限处不再增长", plan(ceiling) == ceiling, f"plan(ceiling)={plan(ceiling)}")


def test_kv_cache_quant():
    """[EN] Quantised KV cache / [ZH] 量化 KV 缓存。"""
    check("引擎记录的 KV 类型是 q8_0", llm_engine.KV_CACHE_Q8_0 == "q8_0",
          f"value={llm_engine.KV_CACHE_Q8_0}")
    check("ggml_type 兜底值为 8", llm_engine.kv_cache_q8_0_type() == 8,
          f"resolved={llm_engine.kv_cache_q8_0_type()}")
    source = read_source("llm_engine.py")
    check("引擎不再把 KV 类型写成 f16",
          "self.kv_cache_type = KV_CACHE_F16" not in source)


def test_removed_modules_absent():
    """[EN] The removed stacks are gone / [ZH] 被删的栈确实不存在。"""
    for name in REMOVED_MODULES:
        path = os.path.join(BACKEND, name + ".py")
        check(f"backend/{name}.py 已删除", not os.path.exists(path), path)
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):  # [EN] No parent package: not importable either.
            spec = None                     # [ZH] 没有父包，同样等于不可导入。
        check(f"{name} 不可导入", spec is None, f"spec={spec}")

    offenders = []
    for entry in sorted(os.listdir(BACKEND)):
        if not entry.endswith(".py"):
            continue
        text = read_source(entry)
        for match in BANNED_IMPORT.finditer(text):
            offenders.append(f"{entry}: {match.group(0).strip()}")
    check("后端源码不再导入已删模块", not offenders, "; ".join(offenders[:5]))


def test_version_is_reader():
    """[EN] Version binding / [ZH] 版本号绑定。"""
    check("APP_VERSION 带 reader 后缀", version.APP_VERSION.endswith("-reader"),
          f"APP_VERSION={version.APP_VERSION}")
    # [EN] Derived expectation, not a literal: this check exists to prove the four-part
    # [EN] form is still purely numeric **and** still derived from APP_VERSION, so a
    # [EN] version bump must not require editing the test. Three constraints, all
    # [EN] stronger than the old equality against one hard-coded string:
    # [EN]   1. shape  -- exactly four dot-separated non-empty numeric segments;
    # [EN]   2. derived -- it equals the four-part form computed from APP_VERSION here
    # [EN]      (strip the v/V prefix, cut at - or +, split on dots, pad to four);
    # [EN]   3. consistency -- APP_VERSION's numeric core equals the first three segments,
    # [EN]      so a number edited in version.py without the derivation following it (or
    # [EN]      the reverse) fails immediately.
    # [ZH] 这里是**派生期望**而不是字面量：该断言要证明的是"四段式仍是纯数字"**且**"仍由
    # [ZH] APP_VERSION 派生"，因此升版不需要改测试。三条约束都比原先"等于某个写死字符串"
    # [ZH] 更严：
    # [ZH]   1. 形状 —— 恰好四段、以点分隔、每段非空且全为数字；
    # [ZH]   2. 派生 —— 等于此处现场由 APP_VERSION 推导的四段（去 v/V 前缀、按 - 或 + 截断、
    # [ZH]      按点切分、不足四位补 0）；
    # [ZH]   3. 一致性 —— APP_VERSION 的数字核心等于四段式的前三段，因此"只改了 version.py
    # [ZH]      的数字而 app_version_4 的算法没跟上"（或反之）会立刻红。
    four = version.app_version_4()
    raw = version.APP_VERSION.lstrip("vV").strip()
    for separator in ("-", "+"):
        raw = raw.split(separator, 1)[0]
    parts = four.split(".")
    shape_ok = len(parts) == 4 and all(part.isdigit() for part in parts)
    derived = ".".join((raw.split(".") + ["0", "0", "0", "0"])[:4])
    app_core = ".".join(raw.split(".")[:3])
    four_core = ".".join(parts[:3])
    check("四段式版本号（形状 / 派生 / 一致性）",
          shape_ok and four == derived and app_core == four_core,
          f"实际 app_version_4()={four} 期望派生={derived} 形状={shape_ok} "
          f"核心 APP_VERSION={app_core} 四段前三段={four_core}")


def test_frontend_residue():
    """[EN] Front-end residue and screenshot deliverables / [ZH] 前端残留与截图交付物。"""
    removed_files = (
        "frontend-avalonia/EchoDesk.Avalonia/Views/Settings/AutomationPage.axaml",
        "frontend-avalonia/EchoDesk.Avalonia/Views/Settings/AutomationPage.axaml.cs",
        "frontend-avalonia/EchoDesk.Avalonia/Views/Settings/AgentPage.axaml",
        "frontend-avalonia/EchoDesk.Avalonia/Views/Settings/AgentPage.axaml.cs",
        "frontend-avalonia/EchoDesk.Avalonia/Views/Settings/DeveloperPage.axaml",
        "frontend-avalonia/EchoDesk.Avalonia/Views/Settings/DeveloperPage.axaml.cs",
        "frontend-avalonia/EchoDesk.Avalonia/Services/MediaService.cs",
        "frontend-avalonia/EchoDesk.Avalonia/Services/GlobalHotkeyService.cs",
        "backend/lang/en.automation.json",
        "backend/lang/zh-Hans.automation.json",
    )
    for relative in removed_files:
        path = os.path.join(ROOT, relative.replace("/", os.sep))
        check(f"{relative} 已删除", not os.path.exists(path), path)

    # [EN] The three documented scenes must still be produced by the probe; if a rename breaks
    # [EN] that, the README embeds an image the build can no longer regenerate.
    # [ZH] 文档里的三幕必须仍由探针产出；一旦改名而没同步，README 内嵌的图就再也生成不出来。
    probe_source = io.open(
        os.path.join(ROOT, "frontend-avalonia", "EchoDesk.Avalonia", "Probe.cs"),
        "r", encoding="utf-8").read()
    for scene in ("screenshot-main", "screenshot-file-parse", "screenshot-chat"):
        check(f"探针仍生成 {scene}", scene in probe_source)
        for suffix in ("", ".zh-Hans"):
            image = os.path.join(ROOT, "docs", "images", scene + suffix + ".png")
            check(f"docs/images/{os.path.basename(image)} 存在", os.path.exists(image), image)


def main():
    print("== Reader 版本门禁（动态上下文 / KV 量化 / 已删模块 / 版本 / 前端残留） ==")
    for test in (test_dynamic_context, test_kv_cache_quant, test_removed_modules_absent,
                 test_version_is_reader, test_frontend_residue):
        print(f"-- {test.__name__}")
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            FAILED.append(test.__name__)
            print(f"  [FAIL] {test.__name__} 执行异常：{type(exc).__name__}: {exc}")

    print()
    print("=" * 60)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
