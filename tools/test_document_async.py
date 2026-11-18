# -*- coding: utf-8 -*-
"""Asynchronous document intake for the **Reader profile** (branch ``reader``).

What this edition actually promises
-----------------------------------
The Reader build binds one 128K model and sizes the context window **from the
uploaded document** (``LLMEngine.plan_context_for_tokens``), so the historical
map-reduce segmented summariser is gone: there is no chunk prompt, no merged
summary and therefore no summary system message. What still has to hold is the
asynchronous contract around it:

* the upload request is accepted before parsing starts (under 100 ms);
* the lightweight ``ping`` action stays under 100 ms while the job runs;
* the main conversation is untouched by intermediate work and never contains a
  chunk prompt;
* on completion the file is registered with its full body and the dynamic
  window was allocated for exactly its token count;
* a document beyond the 128K ceiling is **refused with an explanation** instead
  of being silently truncated.

Run: ``python tools\\test_document_async.py``. Exit code 0 means all passed.

[ZH]
**Reader 版本**（``reader`` 分支）的文档异步接入测试。

本版承诺的到底是什么
--------------------
Reader 只绑定一个 128K 模型，并按上传文档动态分配上下文窗口
（``LLMEngine.plan_context_for_tokens``），因此历史的分段 map-reduce 摘要器已不存在：
没有分段提示词、没有归并摘要，也就没有摘要系统消息。仍然必须成立的是它周围的异步契约：

* 解析开始前上传请求即已受理（100 毫秒以内）；
* 作业运行期间轻量 ``ping`` 动作保持在 100 毫秒以内；
* 主对话不被中间过程触碰，也绝不出现分段提示词；
* 作业完成后文件以完整正文登记，且已按它的 token 数分配动态窗口；
* 超过 128K 上限的文档会被**明确拒绝并说明原因**，而不是静默截断。

运行：``python tools\\test_document_async.py``。退出码 0 表示全部通过。
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from config import Config, FilesConfig  # noqa: E402
from llm_engine import (  # noqa: E402
    CONTEXT_SAFETY_MARGIN,
    DEFAULT_OUTPUT_TOKEN_BUDGET,
    MAX_DYNAMIC_CONTEXT,
)

PASSED = 0
FAILED = []
TEMP = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


class RecordingEngine:
    """[EN] Stub engine that records document-token allocation; no inference happens.
    [ZH] 记录文档 token 分配的桩引擎；本版不触发任何推理。
    """

    def __init__(self):
        self.n_ctx = 4096
        self.n_ctx_train = 4096
        self.kv_cache_bytes = 0
        self.model_path = "stub.gguf"
        self.last_tps = 0.0
        self.last_elapsed = 0.0
        self.last_tokens = 0
        self.allocated = []
        self.inference_calls = 0

    def count_tokens(self, text):
        return max(1, len(str(text)) // 2)

    def document_exceeds_capacity(self, file_tokens):
        return int(file_tokens or 0) > MAX_DYNAMIC_CONTEXT

    def set_document_tokens(self, file_tokens):
        # [EN] Mirrors LLMEngine.set_document_tokens: record the count, then plan the window as
        # [EN] document + answer budget + safety margin, clamped to the hard ceiling.
        # [ZH] 与 LLMEngine.set_document_tokens 同一口径：先记下 token 数，再按
        # [ZH] 文档 + 回答预算 + 安全余量 规划窗口，并收敛到硬上限。
        from llm_engine import LLMEngine

        self.allocated.append(int(file_tokens))
        self.n_ctx = max(self.n_ctx, LLMEngine.plan_context_for_tokens(int(file_tokens)))
        return self.n_ctx

    def chat_stream(self, messages, **kwargs):
        self.inference_calls += 1
        yield "（本版不做分段摘要）"

    def chat_complete(self, messages, temperature=0.7, max_tokens=1024):
        self.inference_calls += 1
        return "（本版不做分段摘要）"

    def clear_cache(self):
        return True

    @property
    def ready(self):
        return True


def build_backend():
    import main as backend_main

    config = Config()
    config.model_path = "stub.gguf"
    config.session_file = ""
    config.speed_hint = False
    # [EN] Reader: search_default no longer exists (the web-search layer is removed).
    # [ZH] Reader：search_default 已不存在（联网搜索层已删除）。
    config.summary_cache = False
    config.files = FilesConfig()
    backend = backend_main.Backend(config)
    backend.llm = RecordingEngine()
    backend.summarizer.engine = backend.llm
    return backend


def make_document(directory, name, pages):
    """[EN] A document of the requested page count / [ZH] 按指定页数生成文档。"""
    path = os.path.join(directory, name)
    body = []
    for page in range(1, pages + 1):
        body.append(
            f"第 {page} 页\n"
            f"甲方应于 2026 年第 {page} 个月支付合同价款 {page * 1000} 元。"
            f"乙方应在收到款项后 {page} 个工作日内交付第 {page} 批设备，"
            f"逾期每日按合同总额的 0.3% 支付违约金。\n"
            + ("本页其余条款为通用履约说明，双方应按约定履行通知、验收与保密义务。\n" * 8)
        )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("".join(body))
    return path


def wait_terminal(job, timeout=20.0):
    deadline = time.time() + timeout
    while not job.terminal and time.time() < deadline:
        time.sleep(0.02)
    return job


def main():
    directory = tempfile.mkdtemp(prefix="echodesk-docasync-")
    TEMP.append(directory)
    path = make_document(directory, "50页长文档.txt", 50)
    backend = build_backend()
    engine = backend.llm

    print("\n=== 1. 上传请求立即受理 ===")
    started = time.perf_counter()
    response = backend.handle({"action": "file_async", "files": [path]})
    accept_ms = (time.perf_counter() - started) * 1000
    check("上传请求返回 accepted", response.get("accepted") is True, str(response))
    check("返回作业标识", bool(response.get("job_id")), str(response))
    check("受理耗时 < 100ms", accept_ms < 100, f"{accept_ms:.1f} ms")
    job = backend._document_jobs.get(str(response.get("job_id") or ""))
    check("作业已登记", job is not None)
    if job is None:
        return report()

    print("\n=== 2. 作业期间主对话空闲且 ping < 100ms ===")
    latencies = []
    pong = {}
    for _ in range(5):
        ping_started = time.perf_counter()
        pong = backend.handle({"action": "ping"})
        latencies.append((time.perf_counter() - ping_started) * 1000)
    check("ping 返回 pong", pong.get("type") == "pong", str(pong))
    check("ping 最大延迟 < 100ms", max(latencies) < 100,
          f"max={max(latencies):.2f} ms, all={['%.2f' % x for x in latencies]} ms")
    check("作业期间主对话历史为空", backend.conv.history == [], str(backend.conv.history))

    print("\n=== 3. 完成后文件登记，且没有分段摘要残留 ===")
    wait_terminal(job)
    check("后台作业完成", job.status == "done", f"{job.status} / {job.error}")
    check("文件已登记", backend.conv.file_names() == ["50页长文档.txt"],
          str(backend.conv.file_names()))
    check("登记的是完整正文（未截断）",
          len(backend.conv.files[0].content) > 10000 if backend.conv.files else False,
          str(len(backend.conv.files[0].content) if backend.conv.files else 0))
    check("本版不产出摘要系统消息",
          not any(m.get("role") == "system" for m in backend.conv.history),
          str(backend.conv.history)[:200])
    check("主对话不含分段提示词",
          all("【第 1/" not in str(m.get("content", "")) for m in backend.conv.history))
    check("本版不调用推理做分段摘要", engine.inference_calls == 0, str(engine.inference_calls))
    check("作业事件含 file_ready",
          any(e.get("type") == "file_ready" for e in job.events_since(0)["events"]))

    print("\n=== 4. 文档驱动的动态窗口已按该文档分配 ===")
    # [EN] The parser may normalise whitespace, so the ground truth is the registered body, not
    # [EN] the bytes on disk.
    # [ZH] 解析器可能规整空白，因此基准是登记下来的正文，而不是磁盘字节。
    expected_tokens = max(1, len(backend.conv.files[0].content) // 2)
    expected_window = min(MAX_DYNAMIC_CONTEXT,
                          max(4096, expected_tokens + DEFAULT_OUTPUT_TOKEN_BUDGET
                              + CONTEXT_SAFETY_MARGIN))
    check("记录了文档 token 数", engine.allocated == [expected_tokens],
          f"allocated={engine.allocated} expected={expected_tokens}")
    check("窗口按 文档+预算+余量 扩张", engine.n_ctx == expected_window,
          f"n_ctx={engine.n_ctx} expected={expected_window}")

    print("\n=== 5. 超过 128K 上限的文档被明确拒绝 ===")
    huge = make_document(directory, "300页超长文档.txt", 700)
    huge_tokens = max(1, os.path.getsize(huge) // 2)
    check("样本确实超过硬上限", huge_tokens > MAX_DYNAMIC_CONTEXT, str(huge_tokens))
    rejected = backend.handle({"action": "file_async", "files": [huge]})
    huge_job = backend._document_jobs.get(str(rejected.get("job_id") or ""))
    check("请求仍然被异步受理", rejected.get("accepted") is True, str(rejected))
    if huge_job is not None:
        wait_terminal(huge_job)
        events = huge_job.events_since(0)["events"]
        failed = [e for e in events if e.get("type") == "file_status"
                  and e.get("phase") == "failed"]
        check("作业报告失败而不是成功", bool(failed), str(events)[:300])
        message = str(failed[0].get("content", "")) if failed else ""
        check("失败说明里带硬上限数值", str(MAX_DYNAMIC_CONTEXT) in message, message[:200])
        check("超限文件未被登记", "300页超长文档.txt" not in backend.conv.file_names(),
              str(backend.conv.file_names()))
    check("第二份文档没有挤掉第一份",
          "50页长文档.txt" in backend.conv.file_names(), str(backend.conv.file_names()))

    return report()


def report():
    print("\n------------------------------------------")
    print(f"共 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        for path in TEMP:
            import shutil
            shutil.rmtree(path, ignore_errors=True)
