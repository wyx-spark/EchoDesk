# -*- coding: utf-8 -*-
"""
[EN]
Prefill-loop checks for the Reader native layer (v0.2.7-reader, step 2).

Two modes:

* default (unit): a fake ``_api`` records every native call, so chunk boundaries,
  progress callbacks, the abort type, thread-pool switching and the yield are checked
  without a model. No SKIP is allowed here.
* ``--live``: loads a real GGUF and measures the prefill wake-up latency with an
  independent watcher thread (one 50 ms sleep per sample, the overshoot is recorded).

Why a separate file: the gate list runs one script per concern, so a failure names the
concern instead of hiding inside a 4000-line test module.

[ZH]
Reader 原生层的预填充循环检查（v0.2.7-reader 步 2）。

两种模式：

* 默认（单元）：用假 ``_api`` 记录每一次原生调用，因此无需模型即可检查分块边界、进度
  回调、中止类型、线程池切换与让核。该模式不允许 SKIP。
* ``--live``：加载真实 GGUF，并用独立 watcher 线程测量预填充期间的唤醒延迟（每样本睡
  50ms，记录超出目标时长的部分）。

为什么单独成文件：门禁按关注点逐脚本执行，失败时能直接指出是哪个关注点，而不是在一个
四千行的测试模块里翻找。

Run:
  ``python tools\\test_prefill_progress.py``
  ``python tools\\test_prefill_progress.py --live --prompt-tokens 4096``
"""
import argparse
import ctypes
import math
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import llama_native  # noqa: E402
from llama_native import (  # noqa: E402
    NativeLlama,
    NativeLlamaAborted,
    NativeLlamaError,
)

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    """[EN] Record one assertion. [ZH] 记录一条断言。"""
    global PASSED
    if condition:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name} {detail}")


class FakeApi:
    """
    [EN]
    Records the native calls the prefill and decode paths make. ``llama_token`` is a
    real ctypes type because the loop builds ``(api.llama_token * n)(*chunk)``; unknown
    attributes return a recording no-op so the sampler path can run without a model.

    [ZH]
    记录预填充与解码路径发出的原生调用。``llama_token`` 必须是真实的 ctypes 类型，因为
    循环会构造 ``(api.llama_token * n)(*chunk)``；未知属性返回一个记录型空实现，使采样
    路径无需模型也能跑通。
    """

    llama_token = ctypes.c_int

    def __init__(self):
        self.decoded = []       # [EN] sizes of the chunks actually decoded / [ZH] 实际解码的分块大小
        self.thread_calls = []  # [EN] (n_threads, n_threads_batch) per switch / [ZH] 每次切换的参数

    # [EN] --- prefill path --- [ZH] --- 预填充路径 ---
    def llama_batch_get_one(self, array, count):
        return ("batch", int(count))

    def llama_decode(self, ctx, batch):
        self.decoded.append(int(batch[1]))
        return 0

    def llama_set_n_threads(self, ctx, threads, threads_batch):
        self.thread_calls.append((int(threads), int(threads_batch)))

    # [EN] --- sampler path (only the full-cache case walks it) --- [ZH] --- 采样路径 ---
    def llama_sampler_sample(self, sampler, ctx, index):
        return 0

    def llama_vocab_is_eog(self, vocab, token):
        return True

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _noop(*args, **kwargs):
            return 0

        return _noop


class NoThreadSetterApi(FakeApi):
    """[EN] ABI variant without ``llama_set_n_threads``. [ZH] 缺少该 ABI 变体的假实现。"""

    llama_set_n_threads = None


class TimeSpy:
    """[EN] Records ``sleep`` calls while delegating everything else. [ZH] 记录 sleep。"""

    def __init__(self):
        self.sleeps = []
        self._real = time

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def __getattr__(self, name):
        return getattr(self._real, name)


def make_llama(api=None, n_batch=10, n_threads=8, n_threads_batch=4,
               chunk=0, yield_ms=0):
    """[EN] Build a NativeLlama without loading a model. [ZH] 不加载模型地构造实例。"""
    llama = NativeLlama.__new__(NativeLlama)
    llama._api = api if api is not None else FakeApi()
    llama._ctx = object()
    llama._vocab = object()
    llama._cached_tokens = []
    llama._n_ctx = 100000
    llama._n_vocab = 1000
    llama.n_batch = n_batch
    llama.n_threads = n_threads
    llama.n_threads_batch = n_threads_batch
    llama.prefill_chunk_tokens = chunk
    llama.prefill_yield_ms = yield_ms
    llama.last_prefill = {"tokens": 0, "seconds": 0.0, "tps": 0.0}
    return llama


def run_unit() -> int:
    # [EN] 1. Chunk boundaries, including a non-dividing tail.
    # [ZH] 1. 分块边界，含不能整除的尾块。
    llama = make_llama(n_batch=10)
    tokens = list(range(25))
    llama._prefill(tokens)
    check("25 token / n_batch=10 分成 [10, 10, 5]", llama._api.decoded == [10, 10, 5],
          llama._api.decoded)
    check("全部分块计入缓存", llama._cached_tokens == tokens,
          len(llama._cached_tokens))
    check("last_prefill 记录总数", llama.last_prefill["tokens"] == 25,
          llama.last_prefill)

    # [EN] 2. An explicit chunk size overrides n_batch (but stays inside the documented
    # [EN]    ``[32, n_batch]`` band) and still ends on the tail.
    # [ZH] 2. 显式分块大小覆盖 n_batch（但仍落在文档规定的 ``[32, n_batch]`` 区间内），
    # [ZH]    尾块依旧收口。
    llama = make_llama(n_batch=64, chunk=40)
    llama._prefill(list(range(100)))
    check("chunk=40 / n_batch=64 / 100 token 分成 [40, 40, 20]",
          llama._api.decoded == [40, 40, 20], llama._api.decoded)
    llama = make_llama(n_batch=64, chunk=200)
    llama._prefill(list(range(100)))
    check("chunk 超 n_batch 被压回（100 token -> [64, 36]）",
          llama._api.decoded == [64, 36], llama._api.decoded)

    # [EN] 3. Progress: one callback per chunk, monotonic, correct total and tps.
    # [ZH] 3. 进度：每块一次回调、单调不减、总数正确、tps 合法。
    llama = make_llama(n_batch=10)
    seen = []

    def on_progress(done, total, elapsed_s, tps):
        seen.append((done, total, elapsed_s, tps))

    llama._prefill(list(range(25)), on_prefill_progress=on_progress)
    check("每块结束回调一次（3 块 -> 3 次）", len(seen) == 3, len(seen))
    check("done 序列为 [10, 20, 25]", [row[0] for row in seen] == [10, 20, 25],
          [row[0] for row in seen])
    check("total 恒为 25", all(row[1] == 25 for row in seen),
          [row[1] for row in seen])
    check("done 严格单调递增",
          all(b[0] > a[0] for a, b in zip(seen, seen[1:])),
          [row[0] for row in seen])
    check("tps 为非负有限数",
          all(math.isfinite(row[3]) and row[3] >= 0 for row in seen),
          [row[3] for row in seen])

    # [EN] 4. Abort before the 3rd chunk: the right type, the partial bookkeeping, and
    # [EN]    no decode for the aborted chunk.
    # [ZH] 4. 第 3 块前中止：类型正确、部分记账保留、被中止的块不进入解码。
    llama = make_llama(n_batch=10)
    calls = {"n": 0}

    def should_abort():
        calls["n"] += 1
        return calls["n"] >= 3

    raised = None
    try:
        llama._prefill(list(range(25)), should_abort=should_abort)
    except BaseException as exc:  # noqa: BLE001 - the type is the assertion
        raised = exc
    check("第 3 块中止抛 NativeLlamaAborted", type(raised) is NativeLlamaAborted,
          type(raised).__name__)
    check("中止类型仍属 NativeLlamaError（引擎可统一捕获）",
          isinstance(raised, NativeLlamaError))
    check("中止时已解码的 2 块仍计入缓存",
          len(llama._cached_tokens) == 20, len(llama._cached_tokens))
    check("被中止的块没有进入 llama_decode", llama._api.decoded == [10, 10],
          llama._api.decoded)
    check("中止后 last_prefill 记录已解码数", llama.last_prefill["tokens"] == 20,
          llama.last_prefill)

    # [EN] 5. Thread switching: prefill pool first, decode pool after, in that order.
    # [ZH] 5. 线程切换：先预填充池，后 decode 池，顺序固定。
    llama = make_llama(n_batch=10, n_threads=8, n_threads_batch=4)
    llama._run_prefill(list(range(10)))
    check("线程切换顺序为 prefill 低 -> decode 高",
          llama._api.thread_calls == [(4, 4), (8, 8)], llama._api.thread_calls)

    llama = make_llama(n_batch=10, n_threads=8, n_threads_batch=4)
    list(llama._generate(list(range(10)), 4, 0.0, None, None, 1.0))
    check("_generate 端到端也是先预填充池后 decode 池",
          llama._api.thread_calls == [(4, 4), (8, 8)], llama._api.thread_calls)

    # [EN] 6. The abort path switches back too (the finally clause).
    # [ZH] 6. 中止路径同样切回（finally 子句）。
    llama = make_llama(n_batch=10, n_threads=8, n_threads_batch=4)
    try:
        llama._run_prefill(list(range(10)), should_abort=lambda: True)
    except NativeLlamaAborted:
        pass
    check("中止后仍切回 decode 线程池",
          llama._api.thread_calls == [(4, 4), (8, 8)], llama._api.thread_calls)
    check("中止前没有解码任何块", llama._api.decoded == [], llama._api.decoded)

    # [EN] 7. Fully cached prompt: no prefill, so no callback and no thread switch.
    # [ZH] 7. 整段命中缓存：没有预填充，因此不回调也不切线程。
    llama = make_llama(n_batch=10, n_threads=8, n_threads_batch=4)
    tokens = list(range(10))
    llama._cached_tokens = list(tokens)
    progress = []
    list(llama._generate(tokens, 4, 0.0, None, None, 1.0,
                         on_prefill_progress=lambda *row: progress.append(row)))
    check("common 全命中时零线程切换", llama._api.thread_calls == [],
          llama._api.thread_calls)
    check("common 全命中时零回调", progress == [], progress)
    check("common 全命中时零解码", llama._api.decoded == [], llama._api.decoded)
    check("common 全命中时不产出伪进度", llama.last_prefill["tokens"] == 0,
          llama.last_prefill)

    # [EN] 8. A throwing callback is logged, never propagated, and the loop finishes.
    # [ZH] 8. 回调抛异常只记日志、不外传，循环照常跑完。
    llama = make_llama(n_batch=10)
    thrown = {"v": False}

    def bad_progress(*_args):
        thrown["v"] = True
        raise RuntimeError("callback exploded")

    llama._prefill(list(range(25)), on_prefill_progress=bad_progress)
    check("回调抛异常不中断推理", thrown["v"] and llama._api.decoded == [10, 10, 5],
          llama._api.decoded)
    check("回调抛异常后缓存仍完整", len(llama._cached_tokens) == 25,
          len(llama._cached_tokens))

    # [EN] 9. Yield: one sleep per chunk at the configured length, none when 0.
    # [ZH] 9. 让核：每块按配置时长睡一次，配置为 0 时完全不睡。
    original_time = llama_native.time
    spy = TimeSpy()
    try:
        llama_native.time = spy
        llama = make_llama(n_batch=10, yield_ms=8)
        llama._prefill(list(range(25)))
        check("每块结束后让核一次（3 块 -> 3 次）", len(spy.sleeps) == 3,
              len(spy.sleeps))
        check("让核时长为 8ms", all(abs(value - 0.008) < 1e-9 for value in spy.sleeps),
              spy.sleeps[:3])
        spy.sleeps.clear()
        llama = make_llama(n_batch=10, yield_ms=0)
        llama._prefill(list(range(25)))
        check("yield=0 时完全不睡", spy.sleeps == [], spy.sleeps)
    finally:
        llama_native.time = original_time

    # [EN] 10. ABI without llama_set_n_threads must degrade, not crash.
    # [ZH] 10. 缺少 llama_set_n_threads 的 ABI 必须降级，而不是崩溃。
    llama = make_llama(api=NoThreadSetterApi(), n_batch=10, n_threads=8,
                       n_threads_batch=4)
    degraded = None
    try:
        llama._run_prefill(list(range(10)))
    except BaseException as exc:  # noqa: BLE001 - any raise is the failure
        degraded = exc
    check("llama_set_n_threads 缺失时降级跳过",
          degraded is None and llama._api.decoded == [10],
          f"{type(degraded).__name__ if degraded else 'ok'} / {llama._api.decoded}")

    total = PASSED + len(FAILED)
    print("-" * 42)
    print(f"PREFLIGHT {PASSED}/{total}")
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    print(f"共 {PASSED} 项，全部通过")
    return 0


def find_model(explicit: str) -> str:
    """[EN] Locate the GGUF model for --live. [ZH] 为 --live 定位 GGUF 模型。"""
    if explicit:
        return os.path.abspath(explicit) if os.path.isfile(explicit) else ""
    folder = os.path.join(ROOT, "backend", "models")
    if not os.path.isdir(folder):
        return ""
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".gguf"):
            return os.path.join(folder, name)
    return ""


def percentile(values, ratio: float) -> float:
    """[EN] Nearest-rank percentile. [ZH] 最近秩百分位。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(math.ceil(ratio * len(ordered))) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def run_live(args) -> int:
    """
    [EN]
    Real-model observation: watch the wake-up latency from an independent thread while
    a 4K-token prefill runs, then report tokens / seconds / tok/s and p95 / p99.

    [ZH]
    真实模型观测：在 4K token 预填充运行期间，用独立线程观察唤醒延迟，最后报告
    token / 秒 / tok/s 与 p95 / p99。
    """
    model = find_model(args.model)
    if not model:
        print(f"[SKIP] 未找到模型：{os.path.join(ROOT, 'backend', 'models', '*.gguf')}")
        return 0

    prompt_tokens = max(64, int(args.prompt_tokens))
    n_ctx = max(8192, prompt_tokens + 2048)
    threads = int(args.threads or max(1, (os.cpu_count() or 4) - 1))
    threads_batch = max(1, threads - 2)
    try:
        import cuda_bootstrap

        cuda_bootstrap.ensure()
    except Exception as exc:  # noqa: BLE001 - the DLLs may already be loaded
        print(f"[INFO] 原生库引导跳过：{exc}")

    print(f"model={os.path.basename(model)} n_ctx={n_ctx} n_threads={threads} "
          f"n_threads_batch={threads_batch} n_batch={args.batch} "
          f"chunk={args.chunk} yield_ms={args.yield_ms}")
    llama = NativeLlama(
        model, n_ctx=n_ctx, n_threads=threads, n_threads_batch=threads_batch,
        n_batch=args.batch, n_ubatch=args.batch, use_mlock=False, use_mmap=True,
        # [EN] q8_0 K/V cache quantisation is the Reader baseline and the native layer
        # [EN] rejects it without FlashAttention, so the live harness mirrors the real
        # [EN] configuration instead of the bare constructor defaults.
        # [ZH] q8_0 的 K/V Cache 量化是 Reader 基线，而原生层在没有 FlashAttention 时
        # [ZH] 会直接拒绝；因此 live 驱动沿用真实配置，而不是构造函数的裸默认值。
        flash_attn=True,
        prefill_chunk_tokens=args.chunk, prefill_yield_ms=args.yield_ms)
    try:
        seed = "The quick brown fox jumps over the lazy dog. "
        base = llama.tokenize(seed, add_bos=True, special=True)
        if not base:
            print("[SKIP] 分词器没有产出 token")
            return 0
        tokens = (base * ((prompt_tokens // len(base)) + 1))[:prompt_tokens]

        samples = []
        barrier = threading.Event()
        period = 0.05

        def watcher() -> None:
            while not barrier.is_set():
                before = time.perf_counter()
                barrier.wait(period)
                elapsed = time.perf_counter() - before
                # [EN] Overshoot only: waking early is not a latency problem.
                # [ZH] 只记超出量：提前醒来不算延迟问题。
                samples.append(max(0.0, (elapsed - period) * 1000.0))

        thread = threading.Thread(target=watcher, name="prefill-watcher", daemon=True)
        thread.start()
        chunks = []

        def on_progress(done, total, elapsed_s, tps):
            chunks.append((done, total))

        started = time.perf_counter()
        try:
            llama._run_prefill(tokens, on_prefill_progress=on_progress)
        finally:
            barrier.set()
            thread.join(timeout=2.0)
        wall = time.perf_counter() - started

        stats = dict(llama.last_prefill)
        p95 = percentile(samples, 0.95)
        p99 = percentile(samples, 0.99)
        print(f"chunks={len(chunks)} samples={len(samples)} wall={wall:.3f}s")
        print(f"PREFILL_LIVE tokens={int(stats['tokens'])} "
              f"seconds={float(stats['seconds']):.3f} "
              f"tps={float(stats['tps']):.1f} "
              f"p95_ms={p95:.1f} p99_ms={p99:.1f}")
        return 0
    finally:
        llama.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="prefill loop checks (v0.2.7 step 2)")
    parser.add_argument("--live", action="store_true", help="真实模型观测")
    parser.add_argument("--prompt-tokens", type=int, default=4096)
    parser.add_argument("--model", default="")
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--batch", type=int, default=512)
    parser.add_argument("--chunk", type=int, default=0)
    parser.add_argument("--yield-ms", type=int, default=8)
    args = parser.parse_args()
    if args.live:
        return run_live(args)
    return run_unit()


if __name__ == "__main__":
    sys.exit(main())
