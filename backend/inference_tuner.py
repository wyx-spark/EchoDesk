"""
[EN]
Hardware auto-tuning (benchmark) for the Reader inference engine.

Purpose
-------
The comfortable default thread count is not necessarily the fastest one: memory
bandwidth, SMT siblings and the Windows scheduler make the optimum machine
specific. This module therefore measures instead of guessing.

Algorithm
---------
1. **Baseline** -- run a fixed long-text workload (``tuning.prompt_tokens`` of
   prompt prefill, ``tuning.gen_tokens`` of generation) with the parameters
   currently stored in ``inference_config.json`` and record generation speed
   (tokens per second) plus the first-token latency (TTFT).
2. **Probe** -- walk a small candidate grid that varies ``n_threads`` and
   ``n_batch`` one factor at a time, and re-measure each candidate.
3. **Adopt** -- a candidate is only accepted when it beats the best result so far
   by more than ``tuning.min_improvement_ratio`` (5 % by default). Anything
   smaller is treated as measurement noise, so a busy desktop cannot silently
   degrade the configuration.
4. **Persist** -- the winning combination is written back into
   ``inference_config.json`` together with the measurement that produced it.

Constraints honoured by the implementation
------------------------------------------
* **Never blocks the main thread**: :func:`tune` is synchronous, but both callers
  in ``main.py`` run it either on the streaming worker thread or on a private
  background thread, and it accepts a ``should_stop`` callback so ``stop`` can
  interrupt a running benchmark.
* **CPU only**: the benchmark never requests GPU offload; ``n_gpu_layers`` is
  taken from the configuration and is 0 for the Reader build.
* **No network**: the module imports nothing that can reach the network.

[ZH]
Reader 推理引擎的硬件自动调优（基准测试）。

目的
----
"舒适的默认线程数"未必是最快的线程数：内存带宽、超线程兄弟核与 Windows 调度器使
最优值因机器而异。因此本模块实测而不是猜测。

算法
----
1. **基线** —— 用 ``inference_config.json`` 中当前参数运行一段固定长度的长文本负载
   （``tuning.prompt_tokens`` 的 Prompt 预填充 + ``tuning.gen_tokens`` 的生成），记录
   生成速度（tokens/s）与首字延迟（TTFT）。
2. **试探** —— 在较小的候选网格上逐项调整 ``n_threads`` 与 ``n_batch``，每次重新实测。
3. **采纳** —— 只有当候选结果比当前最优结果高出 ``tuning.min_improvement_ratio``
   （默认 5%）以上时才采纳。更小的差异视为测量噪声，避免繁忙的桌面环境悄悄把配置改坏。
4. **落盘** —— 胜出的参数组合连同产生它的测量数据一并写回 ``inference_config.json``。

实现遵守的约束
--------------
* **绝不阻塞主线程**：:func:`tune` 本身是同步的，但 ``main.py`` 中的两个调用方分别把
  它放在流式工作线程或独立后台线程上，并传入 ``should_stop`` 回调，使 ``stop`` 能中断
  正在运行的基准测试。
* **纯 CPU**：基准测试从不请求 GPU 卸载；``n_gpu_layers`` 取自配置，Reader 构建中为 0。
* **零联网**：本模块不导入任何能触达网络的模块。
"""
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from inference_config import InferenceConfig

log = logging.getLogger("EchoDesk.Tuner")

#: [EN] Serialises tuning runs: two concurrent benchmarks would interleave on the same
#: [EN] native runtime and produce meaningless numbers.
#: [ZH] 串行化调优运行：两个并发基准测试会在同一原生运行时上互相干扰，得到无意义的数值。
_TUNE_LOCK = threading.Lock()

#: [EN] Fallback lock used only when the engine module is unavailable.
#: [ZH] 仅在引擎模块不可用时使用的兜底锁。
_FALLBACK_LOCK = threading.Lock()

#: [EN] Fixed long-text workload. It is deliberately independent of the user's document:
#: [EN] the point is a comparable measurement, not a real answer.
#: [ZH] 固定的长文本负载。刻意与用户文档无关：目的是可比较的测量值，而不是真实回答。
_FILLER_SENTENCE = (
    "The reader receives a long document, converts it to plain text and sends it to "
    "the local inference engine, which must keep the whole body resident in physical "
    "memory while answering questions about it. "
)

#: [EN] Prompt used for the benchmark. Long enough to exercise prefill and the KV cache.
#: [ZH] 基准测试使用的提示词。长度足以真实压到预填充与 KV Cache。
_BENCH_PROMPT = (
    "Read the following passage and then answer with a single sentence: what is the "
    "main topic?\n\n"
)


@dataclass
class TrialResult:
    """
    [EN]
    One measured configuration: the parameters plus the three numbers the tuner
    compares (TTFT, generation speed, total time).

    [ZH]
    一次被测量的配置：参数本身，加上调优器据以比较的三个数值（TTFT、生成速度、总耗时）。
    """

    n_threads: int
    n_batch: int
    ttft_ms: float = 0.0
    tps: float = 0.0
    tokens: int = 0
    load_ms: float = 0.0
    ok: bool = False
    error: str = ""
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """
        [EN] Serialise for the protocol and for the configuration file.
        [ZH] 序列化，供协议与配置文件使用。
        """
        return {
            "label": self.label,
            "n_threads": int(self.n_threads),
            "n_batch": int(self.n_batch),
            "ttft_ms": round(float(self.ttft_ms), 1),
            "tps": round(float(self.tps), 2),
            "tokens": int(self.tokens),
            "load_ms": round(float(self.load_ms), 1),
            "ok": bool(self.ok),
            "error": str(self.error),
        }


@dataclass
class TuneReport:
    """
    [EN]
    Outcome of one tuning run: the baseline, every trial, and the decision.
    [ZH]
    一次调优运行的结果：基线、全部试探，以及最终裁决。
    """

    model: str = ""
    baseline: Optional[TrialResult] = None
    trials: List[TrialResult] = field(default_factory=list)
    best: Optional[TrialResult] = None
    adopted: bool = False
    reason: str = ""
    config_path: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """
        [EN] Serialise the report as a protocol-friendly mapping.
        [ZH] 把报告序列化为协议友好的映射。
        """
        return {
            "model": self.model,
            "baseline": self.baseline.to_dict() if self.baseline else {},
            "trials": [trial.to_dict() for trial in self.trials],
            "best": self.best.to_dict() if self.best else {},
            "adopted": bool(self.adopted),
            "reason": str(self.reason),
            "config_path": str(self.config_path),
            "elapsed_s": round(float(self.elapsed_s), 1),
        }


def _build_prompt(prompt_tokens: int) -> str:
    """
    [EN]
    Build the fixed benchmark prompt. The sentence above is repeated until the rough
    character budget implied by ``prompt_tokens`` is reached (about four characters
    per token is a deliberate over-estimate; the tokenizer decides the truth).

    [ZH]
    构造固定的基准提示词。上面的句子被重复到 ``prompt_tokens`` 所隐含的粗略字符预算
    为止（按每个 token 约四个字符刻意高估；真实长度由分词器决定）。
    """
    target_chars = max(256, int(prompt_tokens) * 4)
    body = _FILLER_SENTENCE * (target_chars // len(_FILLER_SENTENCE) + 1)
    return _BENCH_PROMPT + body[:target_chars]


def _inference_guard():
    """
    [EN]
    Return the engine's process-wide inference lock.

    The benchmark loads its own native model while the reader keeps one loaded, and
    llama.cpp must never be entered concurrently. Taking the same lock the engine uses
    means a benchmark trial and a conversation turn exclude each other instead of
    corrupting the shared backend state. If the engine module cannot be imported (a
    standalone use of this module), a private lock is returned so the benchmark still
    serialises against itself.

    [ZH]
    返回引擎的进程级推理锁。

    基准测试会自己加载一个原生模型，而阅读器本身也持有一个，llama.cpp 绝不能被并发进入。
    获取引擎所用的同一把锁，意味着基准测试的试探与对话轮次互斥，而不是破坏共享的后端
    状态。若无法导入引擎模块（本模块被独立使用），则返回私有锁，使基准测试至少与自身
    串行。
    """
    global _FALLBACK_LOCK
    try:
        from llm_engine import _INFERENCE_LOCK  # local import: avoid an import cycle

        return _INFERENCE_LOCK
    except Exception:  # noqa: BLE001 - standalone use must still work
        return _FALLBACK_LOCK


def _measure_once(
    model_path: str,
    config: InferenceConfig,
    n_threads: int,
    n_batch: int,
    prompt_tokens: int,
    gen_tokens: int,
    repeats: int,
    label: str,
    should_stop: Optional[Callable[[], bool]] = None,
) -> TrialResult:
    """
    [EN]
    Load the model with one concrete configuration and measure TTFT plus generation
    speed. The best run of ``repeats`` is kept: the first run includes warm-up
    effects (page faults, clock ramp-up) that would otherwise penalise whichever
    configuration happens to be measured first.

    TTFT is measured by streaming and timing the first emitted token; generation
    speed is derived from the number of emitted tokens over the decode window, so it
    is unaffected by model load time.

    [ZH]
    以一组具体参数加载模型并测量 TTFT 与生成速度。``repeats`` 次中保留最好的一次：
    首次运行包含预热效应（缺页、频率爬升），否则会惩罚"恰好先被测到"的那一组配置。

    TTFT 通过流式输出并对首个 token 计时得到；生成速度由解码窗口内输出的 token 数
    推导，因此不受模型加载耗时影响。
    """
    result = TrialResult(n_threads=int(n_threads), n_batch=int(n_batch), label=label)
    override = InferenceConfig.from_dict(config.to_dict(), path=config.path)
    override.n_threads = int(n_threads)
    override.n_batch = int(n_batch)
    override.n_ubatch = min(int(override.n_ubatch), int(n_batch))

    best: Optional[TrialResult] = None
    for attempt in range(max(1, int(repeats))):
        if should_stop is not None and should_stop():
            result.error = "stopped"
            return best or result
        llm = None
        # [EN] One trial owns the native runtime for its whole life: construction, prefill
        # [EN] and generation are all inside the engine's inference lock, so a concurrent
        # [EN] conversation turn waits instead of decoding into the same backend.
        # [ZH] 一次试探在整个生命周期内独占原生运行时：构建、预填充与生成全部位于引擎的
        # [ZH] 推理锁内，因此并发的对话轮次会等待，而不是解码进同一个后端。
        guard = _inference_guard()
        guard.acquire()
        try:
            from llama_cpp import Llama  # local import: keeps the module importable anywhere

            load_started = time.perf_counter()
            llm = Llama(
                model_path=model_path,
                n_ctx=int(override.n_ctx_default),
                verbose=False,
                **override.llama_kwargs(),
            )
            load_ms = (time.perf_counter() - load_started) * 1000.0

            prompt = _build_prompt(prompt_tokens)
            started = time.perf_counter()
            first_token_at = 0.0
            produced = 0
            stream = llm(
                prompt,
                max_tokens=int(gen_tokens),
                temperature=0.0,
                stream=True,
                echo=False,
            )
            for chunk in stream:
                if should_stop is not None and should_stop():
                    break
                text = ""
                try:
                    text = chunk["choices"][0]["text"]
                except (KeyError, IndexError, TypeError):
                    text = ""
                if text and not first_token_at:
                    first_token_at = time.perf_counter()
                if text:
                    produced += 1
            finished = time.perf_counter()
            total = finished - started
            ttft_ms = ((first_token_at or finished) - started) * 1000.0
            tps = (produced / total) if total > 0 and produced else 0.0
            trial = TrialResult(
                n_threads=int(n_threads),
                n_batch=int(n_batch),
                ttft_ms=ttft_ms,
                tps=tps,
                tokens=produced,
                load_ms=load_ms,
                ok=produced > 0,
                label=label,
            )
            if not trial.ok:
                trial.error = "no tokens generated"
            if best is None or trial.tps > best.tps:
                best = trial
            log.info(
                "[tune] %-24s n_threads=%2d n_batch=%4d ttft=%7.1fms tps=%6.2f tokens=%d",
                label, n_threads, n_batch, ttft_ms, tps, produced,
            )
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not abort tuning
            result.error = "%s: %s" % (type(exc).__name__, exc)
            log.warning("[tune] %s 失败: %s", label, result.error)
            return best or result
        finally:
            close = getattr(llm, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - release is best effort
                    log.debug("释放基准测试模型失败（忽略）", exc_info=True)
            guard.release()
    return best or result


def candidate_grid(
    config: InferenceConfig,
    logical_cores: int = 0,
    max_trials: int = 8,
) -> List[Dict[str, int]]:
    """
    [EN]
    Build the probe grid: keep the configured thread count and batch size as the
    baseline, then vary **one factor at a time** around it.

    Varying a single factor keeps the comparison honest: with two factors changing
    at once, a regression cannot be attributed to either of them. The grid is
    ordered so that the most likely wins (SMT-off thread count, then a larger
    prefill batch) are measured first, and it is truncated to ``max_trials`` so a
    full run stays in the minutes range on a CPU-only host.

    [ZH]
    构造试探网格：以配置中的线程数与批大小作为基线，随后围绕它**一次只变一个因子**。

    单因子变化保证比较是诚实的：两个因子同时改变时，性能回退无法归因到其中任何一个。
    网格按"最可能取胜的顺序"排列（先关超线程的线程数，再更大的预填充批），并截断到
    ``max_trials``，使纯 CPU 机器上的一次完整调优保持在分钟级。
    """
    base_threads = max(1, config.resolve_n_threads())
    base_batch = max(1, int(config.n_batch))
    cores = int(logical_cores or os.cpu_count() or base_threads)

    thread_candidates: List[int] = []
    for value in (
        base_threads,
        max(1, base_threads - 2),
        max(1, base_threads + 2),
        max(1, base_threads // 2),
        min(cores, base_threads + 4),
    ):
        if value > 0 and value not in thread_candidates:
            thread_candidates.append(value)

    batch_candidates: List[int] = []
    for value in (base_batch, base_batch * 2, max(64, base_batch // 2), base_batch * 4):
        if value not in batch_candidates:
            batch_candidates.append(value)

    grid: List[Dict[str, int]] = []
    for threads in thread_candidates:
        grid.append({"n_threads": threads, "n_batch": base_batch})
    for batch in batch_candidates:
        grid.append({"n_threads": base_threads, "n_batch": batch})

    # [EN] The first entry repeats the baseline configuration; it is skipped by the
    # [EN] caller, which measures the baseline separately with the current settings.
    # [ZH] 首项即基线配置的重复；调用方会跳过它，因为基线已用当前设置单独测过。
    return grid[1:][: max(1, int(max_trials))]


def iter_tune(
    model_path: str,
    config: Optional[InferenceConfig] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    persist: bool = True,
) -> Any:
    """
    [EN]
    Generator form of the tuning pass: yields one structured event per stage
    (``start``, ``baseline``, ``trial``, ``persisted`` / ``keep``, ``done``). The
    final ``done`` event carries both the JSON-ready report (``report``) and the
    live object (``_report``), so a streaming caller can forward progress while a
    blocking caller still gets the object.

    Every stage is reported, including the rejected candidates, because "which
    configurations lost and by how much" is the evidence that the 5 % rule was
    actually applied rather than assumed.

    [ZH]
    调优过程的生成器形式：每个阶段产出一个结构化事件（``start``、``baseline``、
    ``trial``、``persisted`` / ``keep``、``done``）。最后的 ``done`` 事件同时携带
    可直接 JSON 化的报告（``report``）与活对象（``_report``），因此流式调用方能转发
    进度、阻塞调用方仍能拿到对象。

    所有阶段都会上报，包括被拒绝的候选项 —— "哪些配置落败、差距多少"正是"5% 规则
    确实生效"而不是被假定的证据。
    """
    started = time.perf_counter()
    cfg = config or InferenceConfig.load()
    report = TuneReport(model=os.path.basename(str(model_path or "")),
                        config_path=cfg.path or "")

    if not _TUNE_LOCK.acquire(blocking=False):
        report.reason = "already_running"
        yield {"stage": "error", "reason": "already_running", "_report": report}
        return
    try:
        path = str(model_path or "").strip()
        if not path or not os.path.isfile(path):
            report.reason = "model_missing"
            yield {"stage": "error", "reason": "model_missing", "path": path,
                   "_report": report}
            return

        prompt_tokens = cfg.tuning_int("prompt_tokens", 512)
        gen_tokens = cfg.tuning_int("gen_tokens", 128)
        repeats = max(1, cfg.tuning_int("repeats", 2))
        threshold = cfg.tuning_float("min_improvement_ratio", 0.05)
        max_trials = cfg.tuning_int("max_trials", 8)
        # [EN] The grid is derived from this machine's logical core count, so the
        # [EN] probe range adapts to the host instead of assuming one hardware class.
        # [ZH] 候选网格由本机逻辑核数推导，因此试探范围随主机自适应，而不是假定某一档硬件。
        try:
            logical = int(os.cpu_count() or 0)
        except Exception:  # noqa: BLE001 - core count is informational only
            logical = 0

        yield {"stage": "start", "model": report.model, "config": cfg.describe()}
        log.info("[tune] 基线测量开始 model=%s n_threads=%d n_batch=%d",
                 report.model, cfg.resolve_n_threads(), cfg.n_batch)

        baseline = _measure_once(
            path, cfg, cfg.resolve_n_threads(), int(cfg.n_batch),
            prompt_tokens, gen_tokens, repeats, "baseline", should_stop,
        )
        report.baseline = baseline
        yield {"stage": "baseline", "trial": baseline.to_dict()}
        if not baseline.ok:
            report.reason = baseline.error or "baseline_failed"
            yield {"stage": "error", "reason": report.reason, "_report": report}
            return

        best = baseline
        adopt: Optional[TrialResult] = None
        for index, candidate in enumerate(
                candidate_grid(cfg, logical_cores=logical, max_trials=max_trials),
                start=1):
            if should_stop is not None and should_stop():
                report.reason = "stopped"
                break
            trial = _measure_once(
                path, cfg, candidate["n_threads"], candidate["n_batch"],
                prompt_tokens, gen_tokens, repeats,
                "trial%d" % index, should_stop,
            )
            report.trials.append(trial)
            improved = bool(
                trial.ok and best.tps > 0
                and (trial.tps - best.tps) / best.tps > threshold)
            yield {"stage": "trial", "trial": trial.to_dict(),
                   "improved": improved, "threshold": threshold,
                   "best_tps": round(float(best.tps), 2)}
            if improved:
                adopt = trial
                best = trial
                log.info("[tune] 采纳 trial%d：n_threads=%d n_batch=%d tps=%.2f",
                         index, trial.n_threads, trial.n_batch, trial.tps)

        report.best = best
        cfg.tuned_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        cfg.tuned_model = report.model
        cfg.tuned_baseline = baseline.to_dict()
        cfg.tuned_best = best.to_dict()
        if adopt is not None:
            report.adopted = True
            report.reason = "improved"
            cfg.n_threads = int(adopt.n_threads)
            cfg.n_batch = int(adopt.n_batch)
            if persist:
                report.config_path = cfg.save() or cfg.path
            yield {"stage": "persisted", "path": report.config_path,
                   "skipped": not persist}
        else:
            report.reason = report.reason or "no_improvement"
            if persist:
                cfg.save()
            yield {"stage": "keep", "trial": best.to_dict(), "threshold": threshold}

        report.elapsed_s = time.perf_counter() - started
        log.info("[tune] 结束：reason=%s best_tps=%.2f elapsed=%.1fs",
                 report.reason, best.tps, report.elapsed_s)
        yield {"stage": "done", "report": report.to_dict(), "_report": report}
    finally:
        _TUNE_LOCK.release()


def tune(
    model_path: str,
    config: Optional[InferenceConfig] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    persist: bool = True,
) -> TuneReport:
    """
    [EN]
    Blocking convenience wrapper around :func:`iter_tune`. ``progress`` receives one
    structured dictionary per stage and its exceptions are swallowed, so a faulty
    callback can never abort a benchmark that has already loaded the model.

    The caller decides where it runs: ``main.py`` invokes it either on the streaming
    worker thread or on a dedicated background thread, so the protocol loop is never
    blocked.

    [ZH]
    :func:`iter_tune` 的阻塞式便捷封装。``progress`` 在每个阶段收到一个结构化字典，
    其异常会被吞掉，因此写坏的回调绝不会中断已经开始加载模型的基准测试。

    运行位置由调用方决定：``main.py`` 要么把它放在流式工作线程，要么放在独立后台线程，
    因此协议循环永远不会被阻塞。
    """
    report: Optional[TuneReport] = None
    for event in iter_tune(model_path, config=config, should_stop=should_stop,
                           persist=persist):
        inner = event.get("_report")
        if isinstance(inner, TuneReport):
            report = inner
        if progress is not None:
            try:
                progress({key: value for key, value in event.items()
                          if not key.startswith("_")})
            except Exception:  # noqa: BLE001 - a broken callback must not abort tuning
                log.debug("调优进度回调失败（忽略）", exc_info=True)
    return report or TuneReport(reason="no_result")
