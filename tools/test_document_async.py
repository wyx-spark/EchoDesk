# -*- coding: utf-8 -*-
"""
[EN]
Unit test for v0.2.6 tasks 2.1 and 2.2: asynchronous document processing and
absolute context isolation.

Verification targets:
* The upload request is accepted before parsing or summarization starts.
* The lightweight ping action answers within 100 ms while a long document is
  being summarized in the background.
* The main conversation history is not modified during background processing.
* Chunk prompts are recorded in a private temporary conversation only.
* On completion, exactly one system message carrying the final summary is
  appended to the main conversation history.

[ZH]
v0.2.6 任务 2.1 与 2.2 的单元测试：文档异步处理与上下文绝对隔离。

验证目标：
* 解析与摘要开始前，上传请求即已受理。
* 长文档在后台摘要期间，轻量 ping 动作须在 100 毫秒内应答。
* 后台处理期间主对话历史不被修改。
* 分段提示词仅记录在私有临时会话中。
* 完成后，主对话历史只追加一条携带最终摘要的系统消息。

Run:
    python tools/test_document_async.py
Exit code: 0 = all checks passed, 1 = at least one failure.
"""
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from config import Config, FilesConfig  # noqa: E402

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


class SlowEngine:
    """
    [EN]
    Stub inference engine whose first streaming call blocks until the test
    releases it, making the "background job still running" window deterministic.

    [ZH]
    桩推理引擎：第一次流式调用会阻塞到测试放行，从而确定性地构造"后台作业仍在
    运行"的时间窗口。
    """

    def __init__(self):
        self.n_ctx = 4096
        self.n_ctx_train = 4096
        self.kv_cache_bytes = 0
        self.model_path = "stub.gguf"
        self.last_tps = 0.0
        self.last_elapsed = 0.0
        self.last_tokens = 0
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen_prompts = []

    def count_tokens(self, text):
        return max(1, len(str(text)) // 2)

    def chat_stream(self, messages, **kwargs):
        self.calls += 1
        self.seen_prompts.append([dict(m) for m in messages])
        if self.calls == 1:
            self.started.set()
            self.release.wait(10)
        yield "本段要点：金额、日期与义务均已核对。"

    def chat_complete(self, messages, temperature=0.7, max_tokens=1024):
        self.calls += 1
        self.seen_prompts.append([dict(m) for m in messages])
        return "最终摘要：文档共 50 页，核心条款与金额已归纳。"

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
    config.search_default = False
    config.summary_cache = False
    config.files = FilesConfig()
    config.summarize.chunk_chars = 3000
    config.summarize.overlap_chars = 100
    config.summarize.min_chars = 1000
    config.summarize.trigger_ratio = 0.3
    backend = backend_main.Backend(config)
    backend.llm = SlowEngine()
    backend.summarizer.engine = backend.llm
    return backend, config


def make_long_document(directory):
    path = os.path.join(directory, "50页长文档.txt")
    pages = []
    for page in range(1, 51):
        pages.append(
            f"第 {page} 页\n"
            f"甲方应于 2026 年第 {page} 个月支付合同价款 {page * 1000} 元。"
            f"乙方应在收到款项后 {page} 个工作日内交付第 {page} 批设备，"
            f"逾期每日按合同总额的 0.3% 支付违约金。\n"
            + ("本页其余条款为通用履约说明，双方应按约定履行通知、验收与保密义务。\n" * 8)
        )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("".join(pages))
    return path


def main():
    import main as backend_main  # noqa: E402

    directory = tempfile.mkdtemp(prefix="echodesk-docasync-")
    TEMP.append(directory)
    path = make_long_document(directory)
    backend, config = build_backend()
    doc_chars = os.path.getsize(path)

    print("\n=== 1. 上传请求立即受理 ===")
    started = time.perf_counter()
    response = backend.handle({"action": "file_async", "files": [path]})
    accept_ms = (time.perf_counter() - started) * 1000
    check("上传请求返回 accepted", response.get("accepted") is True, str(response))
    check("返回作业标识", bool(response.get("job_id")), str(response))
    check("受理耗时 < 100ms", accept_ms < 100, f"{accept_ms:.1f} ms")
    job_id = str(response.get("job_id") or "")
    job = backend._document_jobs.get(job_id)
    check("作业已登记", job is not None)

    print("\n=== 2. 后台摘要期间主对话保持空闲且 ping < 100ms ===")
    check("后台作业已开始", backend.llm.started.wait(5))
    history_before = len(backend.conv.history)
    files_before = len(backend.conv.files)
    latencies = []
    for _ in range(5):
        ping_started = time.perf_counter()
        pong = backend.handle({"action": "ping"})
        latencies.append((time.perf_counter() - ping_started) * 1000)
        check_once = pong.get("type") == "pong"
    check("ping 返回 pong", check_once, str(pong))
    check("ping 最大延迟 < 100ms", max(latencies) < 100,
          f"max={max(latencies):.2f} ms, all={['%.2f' % x for x in latencies]}")
    print(f"        [EVIDENCE] ping 最大延迟 = {max(latencies):.2f} ms，"
          f"全部样本 = {['%.2f' % x for x in latencies]} ms")
    check("后台处理期间主对话历史未变化",
          len(backend.conv.history) == history_before, str(backend.conv.history))
    check("后台处理期间主对话未登记文件",
          len(backend.conv.files) == files_before, str(backend.conv.file_names()))
    check("主对话历史不含分段提示词",
          all("【第 1/" not in str(m.get("content", "")) for m in backend.conv.history))

    print("\n=== 3. 放行后完成，仅注入一条最终摘要 ===")
    backend.llm.release.set()
    deadline = time.time() + 15
    while not job.terminal and time.time() < deadline:
        time.sleep(0.02)
    check("后台作业完成", job.status == "done", f"{job.status} / {job.error}")
    check("主对话只新增一条消息", len(backend.conv.history) == history_before + 1,
          str(len(backend.conv.history)))
    final = backend.conv.history[-1] if backend.conv.history else {}
    check("最终注入为 system 消息", final.get("role") == "system", str(final.get("role")))
    check("最终消息携带最终摘要",
          "【长文档摘要】" in str(final.get("content", ""))
          and "本段要点" in str(final.get("content", "")),
          str(final.get("content", ""))[:200])
    check("最终消息不含分段提示词",
          "【第 1/" not in str(final.get("content", ""))
          and "原文第 0-" not in str(final.get("content", "")),
          str(final.get("content", ""))[:200])
    check("文件已登记且摘要已写入",
          backend.conv.file_names() == ["50页长文档.txt"]
          and bool(backend.conv.file_summary), str(backend.conv.file_names()))
    check("临时会话未进入主对话",
          all("【第 1/" not in str(m.get("content", "")) for m in backend.conv.history))
    check("作业事件含 file_ready",
          any(e.get("type") == "file_ready" for e in job.events_since(0)["events"]))
    check("长文档确实超过一次窗口", doc_chars > 3000, str(doc_chars))

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
