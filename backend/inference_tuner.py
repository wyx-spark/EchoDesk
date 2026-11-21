"""
[EN]
Hardware auto-tuning (benchmark) for the Reader inference engine.

Purpose
-------
The comfortable default thread count is not necessarily the fastest one: memory
bandwidth, SMT siblings and the Windows scheduler make the optimum machine
specific. This module therefore measures instead of guessing.

Algorithm (v2 -- three separate measurements)
---------------------------------------------
1. **Probe + rung selection** (step 1.1) -- one 512-token baseline run measures this
   machine's rate. Among ``{512, 4096, 16384}`` the main rung is the largest one whose
   estimated round (``rung / probe_tps`` plus the probe's own decode time) fits into
   ``2 * tuning.primary_target_seconds``; if even 512 does not fit, 512 is used. The
   selection, its inputs and every intermediate estimate go into the report.
2. **Repeats** (step 1.1) -- ``clamp(floor(remaining / (candidates * round)), 3, 5)``:
   the budget decides, the floor of three is never negotiated. Derivation is reported.
3. **Baseline** -- run the selected rung with the parameters currently stored in
   ``inference_config.json`` and record three independent numbers per configuration:
   ``prefill_tps`` (prompt processing, taken from T1's own prefill measurement),
   ``decode_tps`` (tokens emitted after the first one, divided by that segment only)
   and ``p95_ms`` (the wake-up deviation of a 50 ms watcher thread that runs during
   prefill). ``ttft_ms`` stays as the first-token latency. Nothing is derived from
   the total wall clock: mixing prefill into throughput is exactly the defect v2
   removes.
4. **Probe grid** -- two levels, one factor at a time: first the decode thread count
   (prefill fixed at its automatic value), then the prefill thread count (decode
   fixed at the level-1 winner).
5. **Adopt** -- a candidate is accepted only when, in this order: it is ``ok`` and
   its ``p95_ms`` respects ``tuning.responsiveness_p95_ms``; its ``prefill_tps``
   median at the main rung beats the baseline median by at least
   ``tuning.min_improvement_ratio``; and its ``decode_tps`` median does not regress
   by more than ``tuning.decode_regression_ratio``. Every repeat is a median, never
   a maximum, so a lucky run cannot win.
6. **Verify** -- after the last candidate the baseline is re-measured once. A drift
   above 3 % voids the whole round (``reason=drift``) instead of adopting a number
   the machine can no longer reproduce. A ``max_seconds`` overrun ends the run with
   ``reason=time_budget`` and writes nothing.
7. **Long-text guard** (step 1.1) -- the winner and the baseline are measured once at
   ``min(2 * main rung, 16384)``. A regression above ``tuning.long_guard_ratio``
   vetoes the candidate (``reason=long_guard``): a configuration that is fast only on
   short prompts is not an improvement for a document reader. If the guard's own
   ``long_guard_seconds`` sub-budget cannot host the candidate run, the status becomes
   ``skipped_budget`` and the adoption carries ``long_guard_skipped`` -- "not tested"
   is never recorded as "passed".
8. **Persist** -- only a verified winner is written back, together with the
   measurement and the hardware fingerprint that produced it. A fingerprint
   mismatch later marks the result stale; it never re-runs the tuner by itself.

Constraints honoured by the implementation
------------------------------------------
* **Never blocks the main thread**: :func:`tune` is synchronous, but both callers
  in ``main.py`` run it either on the streaming worker thread or on a private
  background thread, and it accepts a ``should_stop`` callback so ``stop`` can
  interrupt a running benchmark.
* **CPU only**: the benchmark never requests GPU offload; ``n_gpu_layers`` is
  taken from the configuration and is 0 for the Reader build.
* **No network, no new dependency**: the p95 sampling uses a plain
  :class:`threading.Thread`; the standard library is enough.
* **``persist=False`` writes nothing**: the rehearsal in ``temp/`` relies on this,
  so every write goes through one guarded branch.

[ZH]
Reader 推理引擎的硬件自动调优（基准测试）。

目的
----
"舒适的默认线程数"未必是最快的线程数：内存带宽、超线程兄弟核与 Windows 调度器使
最优值因机器而异。因此本模块实测而不是猜测。

算法（v2 —— 三项独立测量）
--------------------------
1. **探测与选档**（步 1.1）—— 先用 512 档跑一次基线，得到本机速率。在
   ``{512, 4096, 16384}`` 中取"估算单轮时长（``rung / probe_tps`` 加上探测自身的解码
   耗时）不超过 ``2 * tuning.primary_target_seconds``"的最大档；连 512 都放不下时仍用
   512。选档结论、输入与每个中间估算值都写进报告。
2. **重复次数**（步 1.1）—— ``clamp(floor(剩余预算 / (候选数 × 单轮耗时)), 3, 5)``：
   由预算决定，下限 3 绝不可谈。推导过程写进报告。
3. **基线** —— 用 ``inference_config.json`` 中当前参数在主判据档上运行，对每组配置记录三个
   独立数值：``prefill_tps``（Prompt 处理，取自 T1 自己的预填充实测）、``decode_tps``
   （首个 token 之后的 token 数除以该段耗时）与 ``p95_ms``（预填充期间以 50 ms 周期
   运行的监视线程的实际唤醒偏差）。``ttft_ms`` 仍表示首字延迟。任何指标都不由总耗时
   反推：把预填充混进吞吐正是 v2 要消除的缺陷。
4. **试探** —— 两级、一次只变一个因子：先扫 decode 线程（prefill 固定为自动值），再扫
   prefill 线程（decode 固定为一级胜者）。
5. **采纳** —— 只有按顺序全部满足才接受：候选 ``ok`` 且 ``p95_ms`` 满足
   ``tuning.responsiveness_p95_ms``；主判据档的 ``prefill_tps`` 中位数比基线中位数高出
   ``tuning.min_improvement_ratio`` 以上；``decode_tps`` 中位数相对基线回退不超过
   ``tuning.decode_regression_ratio``。每次重复都取中位数、绝不取最大值，运气好的一次
   赢不了。
6. **校验** —— 全部候选测完后复测基线一次。漂移超过 3% 则整轮作废（``reason=drift``），
   而不是采纳一个这台机器再也复现不出的数字。超过 ``max_seconds`` 预算则以
   ``reason=time_budget`` 收尾且不写任何东西。
7. **长文本守卫**（步 1.1）—— 胜者与基线各在 ``min(2 × 主档, 16384)`` 上测一次。退化超过
   ``tuning.long_guard_ratio`` 则否决该候选（``reason=long_guard``）：只在短 prompt 上快的
   配置对文档阅读器来说不是改进。若守卫自己的 ``long_guard_seconds`` 子预算装不下候选那次
   运行，状态变为 ``skipped_budget``，采纳结论带 ``long_guard_skipped`` —— "没测"永远
   不会被记成"通过"。
8. **落盘** —— 只有被验证的胜者才写回，并附带产生它的测量数据与硬件指纹。日后指纹不符
   只会把结果标为 stale，绝不会自动重跑调优。

实现遵守的约束
--------------
* **绝不阻塞主线程**：:func:`tune` 本身是同步的，但调用方把它放在流式工作线程或独立后台
  线程上，并传入 ``should_stop`` 回调。
* **纯 CPU**：基准测试从不请求 GPU 卸载；``n_gpu_layers`` 取自配置，Reader 构建中为 0。
* **零联网、零新依赖**：p95 采样只用标准库的 :class:`threading.Thread`。
* **``persist=False`` 不写任何东西**：``temp/`` 下的演练依赖这一点，因此所有写入都集中在
  唯一一条带判断的分支里。
"""
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

from inference_config import InferenceConfig, hardware_fingerprint, scope_for_rung

log = logging.getLogger("EchoDesk.Tuner")

#: [EN] Serialises tuning runs: two concurrent benchmarks would interleave on the same
#: [EN] native runtime and produce meaningless numbers.
#: [ZH] 串行化调优运行：两个并发基准测试会在同一原生运行时上互相干扰，得到无意义的数值。
_TUNE_LOCK = threading.Lock()

#: [EN] Fallback lock used only when the engine module is unavailable.
#: [ZH] 仅在引擎模块不可用时使用的兜底锁。
_FALLBACK_LOCK = threading.Lock()

#: [EN] Period of the responsiveness watcher thread, as specified: the question it
#: [EN] answers is "while prefill pins every core, can a 50 ms timer still fire in time".
#: [ZH] 响应性监视线程的周期，按规格：它回答的是"预填充吃满所有核心时，50 ms 定时器还
#: [ZH] 能不能准时触发"。
WATCH_INTERVAL_S = 0.05

#: [EN] Baseline drift above this ratio voids the round.
#: [ZH] 基线漂移超过该比例则整轮作废。
DRIFT_RATIO = 0.03

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


# [EN] ---------- Small statistics helpers (standard library only) ----------
# [ZH] ---------- 小型统计辅助（仅标准库） ----------


def _median(values: List[float]) -> float:
    """
    [EN]
    Median of the supplied samples. v2 aggregates *repeats* with this, never with the
    maximum: a single lucky repeat is noise, and the whole point of N>=5 is that noise
    must not decide an adoption.

    [ZH]
    所给样本的中位数。v2 用它聚合**重复**，绝不用最大值：偶然跑快的一次只是噪声，而
    N>=5 的全部意义就在于噪声不得决定采纳。
    """
    data = sorted(float(value) for value in values if value is not None)
    if not data:
        return 0.0
    middle = len(data) // 2
    if len(data) % 2:
        return data[middle]
    return (data[middle - 1] + data[middle]) / 2.0


def _p95(values: List[float]) -> float:
    """
    [EN]
    95th percentile (nearest-rank) of the supplied samples. ``0.0`` for no samples, so
    a watcher that never got to run can never look *better* than a measured one by
    accident.

    [ZH]
    所给样本的 p95（最近秩法）。无样本时为 ``0.0``，因此"根本没跑起来的监视器"绝不会
    碰巧看起来比实测过的更好。
    """
    data = sorted(float(value) for value in values if value is not None)
    if not data:
        return 0.0
    rank = min(len(data), max(1, int(math.ceil(0.95 * len(data)))))
    return data[rank - 1]


class _WakeWatcher:
    """
    [EN]
    Samples the *actual wake-up deviation* of a 50 ms timer while prefill runs.

    Each iteration sleeps until a fixed schedule target, then records how late the
    thread actually became runnable: ``(now - target) * 1000`` ms. During prompt
    processing every core sits inside a spinning ggml barrier, so a positive p95 here
    means the interface thread would also miss its own 50 ms promise. The schedule is
    re-based on the current time after a late wake, so one slow sample cannot make the
    following ones look artificially early.

    [ZH]
    在预填充期间采样 50 ms 定时器的**实际唤醒偏差**。

    每次迭代睡到固定的计划时刻，然后记录线程实际可运行的时间差：``(now - target) *
    1000`` 毫秒。Prompt 处理期间所有核心都困在 ggml 的自旋屏障里，因此这里 p95 为正
    就意味着界面线程同样会错过自己的 50 ms 承诺。晚到之后的计划会按当前时间重新基准化，
    所以一次慢采样不会让后续采样看起来人为地早。
    """

    def __init__(self, interval_s: float = WATCH_INTERVAL_S) -> None:
        self._interval = max(0.001, float(interval_s))
        self._stop = threading.Event()
        self._deviations: List[float] = []
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """[EN] Start sampling. [ZH] 开始采样。"""
        self._deviations = []
        self._stop.clear()
        thread = threading.Thread(target=self._run, name="prefill-wake-watcher",
                                  daemon=True)
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        target = time.perf_counter() + self._interval
        while not self._stop.is_set():
            delay = target - time.perf_counter()
            if delay > 0 and self._stop.wait(delay):
                break
            now = time.perf_counter()
            self._deviations.append((now - target) * 1000.0)
            target = max(target + self._interval, now)
            if len(self._deviations) >= 200000:
                # [EN] A hard cap: the p95 of a two-hundred-thousand-sample run is not
                # [EN] more informative, and an unbounded list in a long prefill would
                # [EN] compete with the very memory it is measuring.
                # [ZH] 硬上限：二十万样本之后的 p95 不再更有信息量，而长预填充里的无界
                # [ZH] 列表会与它正在测量的内存争抢带宽。
                break

    def stop(self) -> List[float]:
        """[EN] Stop sampling and return the deviations collected so far. [ZH] 停止采样并返回已收集的偏差。"""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        return list(self._deviations)

    @property
    def p95_ms(self) -> float:
        """[EN] p95 of the collected wake-up deviations, in milliseconds. [ZH] 已收集唤醒偏差的 p95（毫秒）。"""
        return _p95(self._deviations)


@dataclass
class TrialResult:
    """
    [EN]
    One measured configuration: the parameters plus the three separated numbers the
    tuner compares, and the legacy fields it still reports.

    [ZH]
    一次被测量的配置：参数本身，加上调优器据以比较的三项分离数值，以及仍会上报的既有字段。
    """

    n_threads: int
    n_batch: int
    n_threads_batch: int = 0
    prompt_tokens: int = 0
    ttft_ms: float = 0.0
    tps: float = 0.0
    prefill_tps: float = 0.0
    decode_tps: float = 0.0
    p95_ms: float = 0.0
    prefill_tokens: int = 0
    prefill_seconds: float = 0.0
    tokens: int = 0
    load_ms: float = 0.0
    repeats: int = 0
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
            "n_threads_batch": int(self.n_threads_batch),
            "n_batch": int(self.n_batch),
            "prompt_tokens": int(self.prompt_tokens),
            "ttft_ms": round(float(self.ttft_ms), 1),
            "tps": round(float(self.tps), 2),
            "prefill_tps": round(float(self.prefill_tps), 2),
            "decode_tps": round(float(self.decode_tps), 2),
            "p95_ms": round(float(self.p95_ms), 1),
            "prefill_tokens": int(self.prefill_tokens),
            "prefill_seconds": round(float(self.prefill_seconds), 3),
            "tokens": int(self.tokens),
            "load_ms": round(float(self.load_ms), 1),
            "repeats": int(self.repeats),
            "ok": bool(self.ok),
            "error": str(self.error),
        }


@dataclass
class Judgement:
    """
    [EN]
    Verdict on one candidate, with the numbers behind it so the report can show *why*
    something lost instead of just that it did.

    [ZH]
    对一个候选的裁决，连同依据的数字，因此报告能说明它**为什么**落败，而不只是落败。
    """

    accepted: bool = False
    reason: str = ""
    p95_ok: bool = True
    prefill_gain: float = 0.0
    decode_delta: float = 0.0
    #: [EN] Whether the decode-regression rule was applied to this verdict. ``False``
    #: [EN] means the decode measurement was found unreliable for this round, so the
    #: [EN] candidate was judged on prefill and responsiveness only.
    #: [ZH] 本裁决是否应用了 decode 退化规则。``False`` 表示本轮 decode 测量被判为不可靠，
    #: [ZH] 因此候选只按预填充与响应性裁决。
    decode_veto_applied: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """[EN] Serialise. [ZH] 序列化。"""
        return {
            "accepted": bool(self.accepted),
            "reason": str(self.reason),
            "p95_ok": bool(self.p95_ok),
            "prefill_gain": round(float(self.prefill_gain), 4),
            "decode_delta": round(float(self.decode_delta), 4),
            "decode_veto_applied": bool(self.decode_veto_applied),
        }


@dataclass
class TuneReport:
    """
    [EN]
    Outcome of one tuning run: the probe, the rung it selected, the repeat count the
    budget allowed, the baseline, every trial, the drift check, the long-text guard,
    and the decision.
    [ZH]
    一次调优运行的结果：探测量、选出的主档、预算允许的重复次数、基线、全部试探、漂移
    自检、长文本守卫，以及最终裁决。
    """

    model: str = ""
    probe: Optional[TrialResult] = None
    baseline: Optional[TrialResult] = None
    baseline_short: Optional[TrialResult] = None
    baseline_retest: Optional[TrialResult] = None
    trials: List[TrialResult] = field(default_factory=list)
    best: Optional[TrialResult] = None
    adopted: bool = False
    reason: str = ""
    config_path: str = ""
    elapsed_s: float = 0.0
    policy: Dict[str, Any] = field(default_factory=dict)
    rung: Dict[str, Any] = field(default_factory=dict)
    repeats_policy: Dict[str, Any] = field(default_factory=dict)
    #: [EN] Environment-noise verdict built from two identical short probes.
    #: [ZH] 由两次相同短探测得出的环境噪声判定。
    noise_probe: Dict[str, Any] = field(default_factory=dict)
    drift: Dict[str, Any] = field(default_factory=dict)
    #: [EN] Second decode measurement of the baseline taken at the end of the round.
    #: [ZH] 轮末对基线 decode 的第二次测量。
    decode_retest: Optional[TrialResult] = None
    decode_drift: Dict[str, Any] = field(default_factory=dict)
    #: [EN] True when the round's decode deltas cannot be trusted (drift beyond 3 %),
    #: [EN] so the decode-regression veto was suspended for this round only.
    #: [ZH] 本轮的 decode 差值不可信（漂移超过 3%）时为真，因此 decode 退化否决只在本轮
    #: [ZH] 被暂停。
    decode_deltas_unreliable: bool = False
    long_guard: Dict[str, Any] = field(default_factory=dict)
    long_guard_skipped: bool = False
    #: [EN] One independent re-measurement of the winner at the main rung, checked
    #: [EN] against the end-of-round baseline. ``unconfirmed_improvement`` is its own
    #: [EN] reason: it is neither "nothing was faster" nor "the machine drifted".
    #: [ZH] 胜者在主判据档上的第二次独立测量，对照轮末基线核对。``unconfirmed_improvement``
    #: [ZH] 是独立的 reason：既不是"没有更快"，也不是"机器漂了"。
    confirm: Dict[str, Any] = field(default_factory=dict)
    #: [EN] Calibration scope of the result: ``short`` / ``long`` (see
    #: [EN] ``scope_for_rung``). Only ``long`` may rewrite the thread counts.
    #: [ZH] 结果的标定范围：``short`` / ``long``（见 ``scope_for_rung``）。只有 ``long``
    #: [ZH] 可以改写线程数。
    tuned_scope: str = ""
    #: [EN] Whether the winning thread counts were actually written into the
    #: [EN] configuration. False for a short-scope result (recorded for reference only).
    #: [ZH] 胜出的线程数是否真的写入了配置。短文本范围的结果为 False（只作参考记录）。
    applied: bool = False
    xlong: Dict[str, Any] = field(default_factory=dict)
    judgements: Dict[str, Any] = field(default_factory=dict)
    fingerprint: Dict[str, Any] = field(default_factory=dict)
    persisted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """
        [EN] Serialise the report as a protocol-friendly mapping.
        [ZH] 把报告序列化为协议友好的映射。
        """
        return {
            "model": self.model,
            "probe": self.probe.to_dict() if self.probe else {},
            "baseline": self.baseline.to_dict() if self.baseline else {},
            "baseline_short": self.baseline_short.to_dict() if self.baseline_short else {},
            "baseline_retest": (self.baseline_retest.to_dict()
                                if self.baseline_retest else {}),
            "trials": [trial.to_dict() for trial in self.trials],
            "best": self.best.to_dict() if self.best else {},
            "adopted": bool(self.adopted),
            "reason": str(self.reason),
            "config_path": str(self.config_path),
            "elapsed_s": round(float(self.elapsed_s), 1),
            "policy": dict(self.policy),
            "rung": dict(self.rung),
            "repeats_policy": dict(self.repeats_policy),
            "noise_probe": dict(self.noise_probe),
            "drift": dict(self.drift),
            "decode_retest": (self.decode_retest.to_dict()
                              if self.decode_retest else {}),
            "decode_drift": dict(self.decode_drift),
            "decode_deltas_unreliable": bool(self.decode_deltas_unreliable),
            "long_guard": dict(self.long_guard),
            "long_guard_skipped": bool(self.long_guard_skipped),
            "confirm": dict(self.confirm),
            "tuned_scope": str(self.tuned_scope),
            "applied": bool(self.applied),
            "xlong": dict(self.xlong),
            "judgements": dict(self.judgements),
            "fingerprint": dict(self.fingerprint),
            "persisted": bool(self.persisted),
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


def _deadline_reached(deadline: float) -> bool:
    """
    [EN]
    Whether the wall-clock budget of this run is spent. A module-level function so the
    tests can drive the ``time_budget`` path without waiting fifteen minutes.

    [ZH]
    本次运行的墙钟预算是否已经用完。做成模块级函数，测试因此无需等十五分钟就能驱动
    ``time_budget`` 路径。
    """
    return time.perf_counter() >= float(deadline)


def _measure_once(
    model_path: str,
    config: InferenceConfig,
    n_threads: int,
    n_batch: int,
    prompt_tokens: int,
    gen_tokens: int,
    repeats: int,
    label: str,
    n_threads_batch: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> TrialResult:
    """
    [EN]
    Load the model with one concrete configuration and measure the three separated
    numbers.

    * ``prefill_tps`` comes from the native layer's own ``last_prefill`` bookkeeping
      (written by T1's progress callback). It is **not** derived from the total time:
      deriving it that way is precisely what made the old ``tps`` unusable for
      comparing prompt processing.
    * ``decode_tps`` is the number of tokens produced *after* the first one divided by
      the duration of that segment only, so prompt length cannot pollute it.
    * ``p95_ms`` is the p95 wake-up deviation of a 50 ms watcher thread started right
      before prompt processing and stopped at the first token.

    ``repeats`` runs are aggregated with the **median** of each metric, never the
    maximum: one lucky run is noise. TTFT, load time and the legacy total ``tps`` are
    reported alongside.

    [ZH]
    用一组具体参数加载模型，测量三项分离指标。

    * ``prefill_tps`` 取自原生层自己的 ``last_prefill`` 记账（由 T1 的进度回调写入），
      而**不是**由总耗时反推：那样反推正是旧 ``tps`` 无法用于比较 Prompt 处理的原因。
    * ``decode_tps`` 是**首个 token 之后**产出的 token 数除以该段的耗时，因此 Prompt
      长度不会污染它。
    * ``p95_ms`` 是一个 50 ms 监视线程的唤醒偏差 p95，线程在 Prompt 处理前启动、在首个
      token 处停止。

    ``repeats`` 次运行按各指标**中位数**聚合，绝不取最大值：偶然跑快的一次只是噪声。
    TTFT、加载耗时与既有的总 ``tps`` 一并上报。
    """
    requested = int(n_threads_batch) if n_threads_batch else 0
    result = TrialResult(n_threads=int(n_threads), n_batch=int(n_batch),
                         n_threads_batch=requested, prompt_tokens=int(prompt_tokens),
                         label=label, repeats=max(1, int(repeats)))
    override = InferenceConfig.from_dict(config.to_dict(), path=config.path)
    override.n_threads = int(n_threads)
    override.n_batch = int(n_batch)
    override.n_ubatch = min(int(override.n_ubatch), int(n_batch))
    if requested > 0:
        override.n_threads_batch = requested

    runs: List[Dict[str, float]] = []
    failures: List[str] = []
    for attempt in range(max(1, int(repeats))):
        if should_stop is not None and should_stop():
            result.error = result.error or "stopped"
            break
        llm = None
        # [EN] One trial owns the native runtime for its whole life: construction,
        # [EN] prefill and generation are all inside the engine's inference lock, so a
        # [EN] concurrent conversation turn waits instead of decoding into the same
        # [EN] backend.
        # [ZH] 一次试探在整个生命周期内独占原生运行时：构建、预填充与生成全部位于引擎的
        # [ZH] 推理锁内，因此并发的对话轮次会等待，而不是解码进同一个后端。
        guard = _inference_guard()
        guard.acquire()
        watcher: Optional[_WakeWatcher] = None
        try:
            from llama_native import NativeLlama  # local import: keeps the module importable anywhere

            load_started = time.perf_counter()
            llm = NativeLlama(
                model_path=model_path,
                n_ctx=int(override.n_ctx_default),
                verbose=False,
                **override.llama_kwargs(),
            )
            load_ms = (time.perf_counter() - load_started) * 1000.0

            prompt = _build_prompt(prompt_tokens)
            progress: Dict[str, float] = {"tokens": 0.0, "seconds": 0.0}

            def on_progress(done, total, elapsed_s, tps, _progress=progress):
                # [EN] T1's callback contract: fallback only -- the authoritative number
                # [EN] is read back from ``last_prefill`` after the run.
                # [ZH] T1 的回调契约：仅作兜底 —— 权威数值在运行结束后从
                # [ZH] ``last_prefill`` 读回。
                try:
                    _progress["tokens"] = float(done)
                    _progress["seconds"] = float(elapsed_s)
                except (TypeError, ValueError):
                    pass

            watcher = _WakeWatcher()
            started = time.perf_counter()
            first_token_at = 0.0
            produced = 0
            watcher.start()
            stream = llm.create_completion(
                prompt,
                max_tokens=int(gen_tokens),
                temperature=0.0,
                stream=True,
                echo=False,
                on_prefill_progress=on_progress,
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
                    # [EN] Prefill is over the moment the first token exists: the
                    # [EN] watcher stops here instead of measuring the decode phase.
                    # [ZH] 首个 token 出现即预填充结束：监视器在此停止，而不是去测解码阶段。
                    watcher.stop()
                if text:
                    produced += 1
            finished = time.perf_counter()
            deviations = watcher.stop()
            watcher = None

            prefill_tokens = 0
            prefill_seconds = 0.0
            prefill_tps = 0.0
            last = getattr(llm, "last_prefill", None)
            if isinstance(last, dict):
                try:
                    prefill_tps = float(last.get("tps") or 0.0)
                    prefill_tokens = int(last.get("tokens") or 0)
                    prefill_seconds = float(last.get("seconds") or 0.0)
                except (TypeError, ValueError):
                    prefill_tps, prefill_tokens, prefill_seconds = 0.0, 0, 0.0
            if prefill_tps <= 0.0:
                # [EN] Fallback to the progress callback's own prefill numbers -- still a
                # [EN] prefill measurement, never the total runtime.
                # [ZH] 回退到进度回调自己上报的预填充数字 —— 仍然是预填充测量，绝不是总耗时。
                prefill_tokens = prefill_tokens or int(progress["tokens"])
                prefill_seconds = prefill_seconds or float(progress["seconds"])
                if prefill_tokens > 0 and prefill_seconds > 0:
                    prefill_tps = prefill_tokens / prefill_seconds

            decode_seconds = (finished - first_token_at) if first_token_at else 0.0
            decode_tokens = max(0, produced - 1) if first_token_at else 0
            decode_tps = (decode_tokens / decode_seconds) if decode_seconds > 0 else 0.0
            total = finished - started
            tps = (produced / total) if total > 0 and produced else 0.0

            runs.append({
                "ttft_ms": ((first_token_at or finished) - started) * 1000.0,
                "tps": tps,
                "prefill_tps": prefill_tps,
                "decode_tps": decode_tps,
                "p95_ms": _p95(deviations),
                "prefill_tokens": float(prefill_tokens),
                "prefill_seconds": prefill_seconds,
                "tokens": float(produced),
                "load_ms": load_ms,
            })
            log.info(
                "[tune] %-22s n_threads=%2d batch_threads=%2d prefill=%7.2f tps "
                "decode=%6.2f tps p95=%6.1f ms ttft=%7.1f ms tokens=%d",
                label, n_threads, override.resolve_n_threads_batch(),
                prefill_tps, decode_tps, runs[-1]["p95_ms"], runs[-1]["ttft_ms"], produced,
            )
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not abort tuning
            failures.append("%s: %s" % (type(exc).__name__, exc))
            log.warning("[tune] %s 失败: %s", label, failures[-1])
        finally:
            if watcher is not None:
                watcher.stop()
            close = getattr(llm, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - release is best effort
                    log.debug("释放基准测试模型失败（忽略）", exc_info=True)
            guard.release()

    if not runs:
        result.error = failures[-1] if failures else (result.error or "no measurement")
        return result

    result.ttft_ms = _median([run["ttft_ms"] for run in runs])
    result.tps = _median([run["tps"] for run in runs])
    result.prefill_tps = _median([run["prefill_tps"] for run in runs])
    result.decode_tps = _median([run["decode_tps"] for run in runs])
    result.p95_ms = _median([run["p95_ms"] for run in runs])
    result.prefill_tokens = int(max(run["prefill_tokens"] for run in runs))
    result.prefill_seconds = _median([run["prefill_seconds"] for run in runs])
    result.tokens = int(max(run["tokens"] for run in runs))
    result.load_ms = _median([run["load_ms"] for run in runs])
    # [EN] Report the *resolved* prefill thread count, so downstream levels compare
    # [EN] concrete values even when the configuration says "automatic" (0).
    # [ZH] 上报**解析后**的预填充线程数，于是即使配置写的是"自动"（0），后续层级比较的
    # [ZH] 也是具体数值。
    result.n_threads_batch = int(override.resolve_n_threads_batch())
    produced_runs = sum(1 for run in runs if run["tokens"] > 0)
    # [EN] "ok" needs a majority of successful repeats *and* a real prefill number:
    # [EN] without prefill_tps the candidate cannot be judged on the main metric at all.
    # [ZH] "ok" 需要多数重复成功**且**有真实的预填充数值：没有 ``prefill_tps`` 的候选根本
    # [ZH] 无法按主指标裁决。
    result.ok = (produced_runs * 2 >= max(1, int(repeats))) and result.prefill_tps > 0.0
    if not result.ok:
        if failures:
            result.error = failures[-1]
        elif produced_runs == 0:
            result.error = "no tokens generated"
        else:
            result.error = "no prefill measurement"
    return result


def evaluate_candidate(
    baseline: TrialResult,
    candidate: TrialResult,
    min_improvement_ratio: float = 0.03,
    decode_regression_ratio: float = 0.02,
    responsiveness_p95_ms: float = 50.0,
    apply_decode_veto: bool = True,
) -> Judgement:
    """
    [EN]
    Apply the acceptance criteria **in the specified order**:

    1. the candidate is ``ok`` and its ``p95_ms`` is within the responsiveness gate;
    2. its ``prefill_tps`` median at the main rung improves on the baseline by at least
       ``min_improvement_ratio``;
    3. its ``decode_tps`` median does not regress by more than
       ``decode_regression_ratio`` -- **unless** ``apply_decode_veto`` is ``False``,
       which the caller sets when the round's decode measurement was itself found
       unstable. Treating a noisy decode delta as a causal veto would reject good
       candidates for a measurement artefact.

    The drift checks are round-level (see :func:`baseline_drift` and
    :func:`decode_drift`), because they need every candidate to be finished first. The
    first failing rule decides the reason, so the report names the veto that actually
    fired instead of a generic "lost".

    [ZH]
    按**规定顺序**应用采纳判据：

    1. 候选 ``ok`` 且 ``p95_ms`` 满足响应性闸门；
    2. 主判据档的 ``prefill_tps`` 中位数相对基线提升至少 ``min_improvement_ratio``；
    3. 其 ``decode_tps`` 中位数相对基线回退不超过 ``decode_regression_ratio`` ——
       **除非** ``apply_decode_veto`` 为 ``False``：调用方在本轮 decode 测量本身被判定为
       不稳定时会这样传。把噪声当成因果否决会因测量假象而否掉好候选。

    漂移检查在轮次级别（见 :func:`baseline_drift` 与 :func:`decode_drift`），因为它们需要
    全部候选先测完。第一条失败的规则决定 reason，因此报告给出真正生效的否决项，而不是
    笼统的"落败"。
    """
    verdict = Judgement(decode_veto_applied=bool(apply_decode_veto))
    if not candidate.ok:
        verdict.reason = "not_ok"
        verdict.p95_ok = False
        return verdict

    verdict.p95_ok = float(candidate.p95_ms) <= float(responsiveness_p95_ms)
    base_prefill = float(baseline.prefill_tps or 0.0)
    if base_prefill > 0.0:
        verdict.prefill_gain = (float(candidate.prefill_tps) - base_prefill) / base_prefill
    base_decode = float(baseline.decode_tps or 0.0)
    if base_decode > 0.0:
        verdict.decode_delta = (float(candidate.decode_tps) - base_decode) / base_decode

    if base_prefill <= 0.0:
        verdict.reason = "no_baseline"
        return verdict
    if not verdict.p95_ok:
        verdict.reason = "p95"
        return verdict
    if verdict.prefill_gain < float(min_improvement_ratio):
        verdict.reason = "no_improvement"
        return verdict
    if apply_decode_veto and base_decode > 0.0 \
            and verdict.decode_delta < -float(decode_regression_ratio):
        verdict.reason = "decode_regression"
        return verdict
    verdict.accepted = True
    verdict.reason = "improved"
    return verdict


def baseline_drift(
    baseline: TrialResult,
    retest: TrialResult,
    limit: float = DRIFT_RATIO,
) -> Dict[str, Any]:
    """
    [EN]
    Compare the original baseline with the one measured after all candidates. The
    ratio is relative to the original value; a baseline of zero (nothing measured) is
    treated as invalid drift, because an unverifiable round must not adopt anything.

    [ZH]
    把原始基线与全部候选测完之后的那次复测相比。比例相对原值计算；基线为零（什么都没
    测到）视为漂移无效，因为无法校验的一轮不得采纳任何东西。
    """
    before = float(baseline.prefill_tps or 0.0) if baseline is not None else 0.0
    after = float(retest.prefill_tps or 0.0) if retest is not None else 0.0
    if before <= 0.0:
        ratio = 1.0
    else:
        ratio = abs(after - before) / before
    return {
        "before_prefill_tps": round(before, 3),
        "after_prefill_tps": round(after, 3),
        "ratio": round(ratio, 4),
        "limit": float(limit),
        "invalid": bool(ratio > float(limit)),
    }


# [EN] ---------- Step-1.1 load adaptation, guard and repeat policy ----------
# [ZH] ---------- 步 1.1 负载自适应、长文本守卫与重复次数策略 ----------

#: [EN] Hard ceiling of the long-text guard rung, per the specification.
#: [ZH] 长文本守卫档的硬上限，按规格。
GUARD_RUNG_CEILING = 16384

#: [EN] Inclusive floor / ceiling of the adaptive repeat count. The floor is a hard
#: [EN] requirement: a budget overrun must never be "solved" by dropping below three.
#: [ZH] 自适应重复次数的闭区间上下限。下限是硬要求：预算不够绝不能靠降到三次以下来"解决"。
REPEATS_FLOOR = 3
REPEATS_CEILING = 5

#: [EN] Tokens generated by each noise probe. Deliberately short: the probe exists to
#: [EN] compare two identical runs cheaply, not to measure throughput precisely.
#: [ZH] 每次噪声探测生成的 token 数。刻意很短：探测的目的是低成本比较两次相同运行，
#: [ZH] 而不是精确测吞吐。
NOISE_PROBE_GEN_TOKENS = 32

#: [EN] A decode segment shorter than this cannot support a 3 % comparison: with a
#: [EN] handful of tokens the runtime is dominated by scheduling jitter. Such a probe
#: [EN] still compares prefill and records ``decode_compared=false`` instead of
#: [EN] pretending to have measured it.
#: [ZH] 短于该时长的解码段撑不起 3% 的比较：只有几个 token 时耗时被调度抖动主导。这类探测
#: [ZH] 仍会比较预填充，但记录 ``decode_compared=false``，而不是假装测过了。
NOISE_MIN_DECODE_SECONDS = 0.10

#: [EN] Actionable advice attached to a noisy-environment verdict.
#: [ZH] 随"环境不稳"结论给出的可操作建议。
NOISE_ADVICE = "关闭其它占用 CPU 的程序后重试（close other CPU-heavy programs, then retry）"


def estimate_round_seconds(rung: int, prefill_tps: float, decode_tps: float,
                           gen_tokens: int) -> float:
    """
    [EN]
    Estimated wall-clock duration of one measured round at ``rung``: the prompt
    processing part (``rung / prefill_tps``) plus the generation part
    (``gen_tokens / decode_tps``). Both parts come from the 512-token probe.

    The specification writes the estimate as ``rung / probe_tps``; the decode part is
    added because a "round" in this calibrator *is* prefill plus ``gen_tokens`` of
    generation, and dropping it would let a rung be chosen that cannot finish inside
    ``max_seconds`` -- exactly the defect the adaptation exists to remove. Both parts
    are reported separately so the arithmetic is auditable.

    [ZH]
    某个档位下一轮测量的估算墙钟时长：Prompt 处理部分（``rung / prefill_tps``）加上生成
    部分（``gen_tokens / decode_tps``），两部分都来自 512 token 的探测。

    规格把估算写作 ``rung / probe_tps``；这里补上生成部分，因为本校准器里"一轮"**就是**
    预填充加上 ``gen_tokens`` 个 token 的生成，漏掉它就会选出一个跑不进 ``max_seconds``
    的档位 —— 正是本次自适应要消除的缺陷。两部分分别上报，计算过程可核对。
    """
    rung = max(1, int(rung))
    prefill = (float(rung) / float(prefill_tps)) if float(prefill_tps or 0.0) > 0.0 \
        else float("inf")
    if float(decode_tps or 0.0) > 0.0 and int(gen_tokens or 0) > 0:
        decode = float(gen_tokens) / float(decode_tps)
    else:
        decode = 0.0
    return prefill + decode


def select_rung(
    probe_tps: float,
    decode_tps: float,
    gen_tokens: int,
    rungs: List[int],
    target_seconds: float,
) -> Dict[str, Any]:
    """
    [EN]
    Algorithm 1-2: from the ascending rungs (512 / 4096 / 16384 by default) pick the
    largest one whose estimated round fits into ``2 * target_seconds``. If even the
    smallest rung does not fit, use the smallest rung -- the main criterion is never
    lowered to make a run finish, it only follows the measured machine.

    Every input and every intermediate estimate is returned, so the report can show
    *why* a rung was selected rather than just which one.

    [ZH]
    算法 1–2：在升序档位（默认 512 / 4096 / 16384）中取"估算单轮时长不超过
    ``2 * target_seconds``"的最大档。若连最小档都放不下，就用最小档 —— 主判据绝不为了
    让一次运行跑完而降档，它只跟随实测机器能力。

    全部输入与每个中间估算值都会返回，因此报告能说明**为什么**选了这个档，而不只是选了哪个。
    """
    ordered = sorted({int(rung) for rung in rungs if int(rung) > 0})
    if not ordered:
        ordered = [512]
    limit = 2.0 * float(target_seconds)
    estimates: Dict[str, Any] = {}
    selected: Optional[int] = None
    for rung in ordered:
        estimate = estimate_round_seconds(rung, probe_tps, decode_tps, gen_tokens)
        estimates[str(rung)] = round(estimate, 3) if estimate != float("inf") else "inf"
        if estimate <= limit:
            selected = rung
    reason = "estimate_within_2x_target"
    if selected is None:
        selected = ordered[0]
        reason = "fallback_smallest_rung"
    return {
        "selected_rung": int(selected),
        "probe_tps": round(float(probe_tps or 0.0), 2),
        "probe_decode_tps": round(float(decode_tps or 0.0), 2),
        "gen_tokens": int(gen_tokens or 0),
        "target_seconds": float(target_seconds),
        "limit_seconds": round(limit, 3),
        "estimates_seconds": estimates,
        "rungs": ordered,
        "reason": reason,
    }


def derive_repeats(
    remaining_seconds: float,
    candidates: int,
    round_seconds: float,
    minimum: int = REPEATS_FLOOR,
    maximum: int = REPEATS_CEILING,
) -> Dict[str, Any]:
    """
    [EN]
    Algorithm 3: ``repeats = clamp(floor(remaining / (candidates * round)), 3, 5)``.

    ``maximum`` is capped by the configured ``tuning.repeats`` so an operator who asked
    for fewer repeats keeps that upper bound, while the **floor of 3 is never
    negotiated**: a small budget yields three repeats and a ``time_budget`` ending, not
    two repeats and a fake pass.

    [ZH]
    算法 3：``repeats = clamp(floor(剩余预算 / (候选数 × 单轮耗时)), 3, 5)``。

    ``maximum`` 会被配置里的 ``tuning.repeats`` 压住，因此显式要求更少重复次数的运维仍能
    保留该上限；而**下限 3 绝不可谈**：预算小只会得到三次重复与 ``time_budget`` 收尾，
    而不是两次重复与一个假的通过。
    """
    floor = max(REPEATS_FLOOR, int(minimum))
    ceiling = max(floor, int(maximum))
    denom = max(1, int(candidates)) * max(1e-6, float(round_seconds))
    raw = int(float(remaining_seconds) // denom) if float(remaining_seconds) > 0 else 0
    value = max(floor, min(ceiling, raw))
    return {
        "value": int(value),
        "raw_floor": int(raw),
        "floor": int(floor),
        "ceiling": int(ceiling),
        "remaining_seconds": round(float(remaining_seconds), 1),
        "candidates": int(candidates),
        "round_seconds": round(float(round_seconds), 2),
    }


def evaluate_long_guard(
    baseline_guard: TrialResult,
    candidate_guard: TrialResult,
    ratio: float = 0.02,
) -> Dict[str, Any]:
    """
    [EN]
    Algorithm 5: compare the candidate with the baseline at the guard rung. A candidate
    that loses more than ``ratio`` of prefill throughput cannot be adopted, no matter
    how much it won on the shorter main rung -- that is the "fast on short prompts,
    slow on long ones" configuration the guard exists to reject.

    Two numbers describe the same comparison, with **opposite and therefore explicit**
    signs: ``guard_delta`` (positive = the candidate is faster, in percent) and
    ``regression`` (positive = the candidate is slower, as a ratio). The pair exists so
    a report can be read without guessing the sign convention.

    [ZH]
    算法 5：在守卫档把候选与基线相比。预填充吞吐损失超过 ``ratio`` 的候选不得被采纳，
    无论它在较短的主判据档赢了多少 —— 这正是守卫存在的目的：拒绝"短负载快、长负载慢"
    的配置。

    同一次比较用两个**符号相反、因此含义显式**的字段表达：``guard_delta``（正 = 候选
    更快，百分比）与 ``regression``（正 = 候选更慢，比例）。这样读报告时无需猜符号约定。
    """
    before = float(baseline_guard.prefill_tps or 0.0) if baseline_guard is not None else 0.0
    after = float(candidate_guard.prefill_tps or 0.0) if candidate_guard is not None else 0.0
    if before <= 0.0:
        regression = 1.0
    else:
        regression = (before - after) / before
    return {
        "baseline_prefill_tps": round(before, 3),
        "candidate_prefill_tps": round(after, 3),
        "guard_delta": round((0.0 - regression) * 100.0, 2),
        "regression": round(regression, 4),
        "limit": float(ratio),
        "vetoed": bool(regression > float(ratio)),
    }


def decode_drift(
    baseline: TrialResult,
    retest: TrialResult,
    limit: float = DRIFT_RATIO,
) -> Dict[str, Any]:
    """
    [EN]
    Compare the baseline's ``decode_tps`` with a second measurement taken at the end of
    the round. Decode is a much noisier quantity than prefill on a shared CPU, so a
    drift beyond ``limit`` means the decode deltas of this round cannot be used to veto
    candidates. The rule is **not** disabled by this check: it is suspended for the
    round with ``decode_deltas_unreliable=True`` recorded.

    [ZH]
    把基线的 ``decode_tps`` 与轮末的第二次测量对比。在共享 CPU 上 decode 比 prefill 噪声
    大得多，因此漂移超过 ``limit`` 意味着本轮的 decode 差值不能用来否决候选。该规则并**不是**
    被这次检查废掉：它只是在本轮被暂停，并记录 ``decode_deltas_unreliable=True``。
    """
    before = float(baseline.decode_tps or 0.0) if baseline is not None else 0.0
    after = float(retest.decode_tps or 0.0) if retest is not None else 0.0
    if before <= 0.0:
        ratio = 1.0
    else:
        ratio = abs(after - before) / before
    return {
        "before_decode_tps": round(before, 3),
        "after_decode_tps": round(after, 3),
        "ratio": round(ratio, 4),
        "limit": float(limit),
        "unreliable": bool(ratio > float(limit)),
    }


def _relative_gap(first: float, second: float) -> float:
    """
    [EN]
    ``|a - b| / max(a, b)``, the symmetric relative difference used by the noise probe.
    Two unmeasurable values (``0`` / negative) yield ``0.0`` so a failed measurement
    cannot masquerade as instability; the caller decides what to do with that.
    [ZH]
    ``|a - b| / max(a, b)``，噪声探测使用的对称相对差。两侧都不可测（``0`` / 负值）时返回
    ``0.0``，使"没测到"不能伪装成"不稳"；如何处置由调用方决定。
    """
    first = float(first or 0.0)
    second = float(second or 0.0)
    largest = max(first, second)
    if largest <= 0.0:
        return 0.0
    return abs(first - second) / largest


def evaluate_noise_probe(
    first: TrialResult,
    second: TrialResult,
    limit: float = 0.03,
    min_decode_seconds: float = NOISE_MIN_DECODE_SECONDS,
) -> Dict[str, Any]:
    """
    [EN]
    Compare two identical short probes. **Any** of the two metrics whose relative
    difference exceeds ``limit`` declares the environment unstable -- prefill and decode
    are checked separately because a host can be stable on one and noisy on the other.

    A decode segment shorter than ``min_decode_seconds`` is recorded as
    ``decode_compared=false`` and does not decide the verdict: a handful of tokens
    cannot support a 3 % comparison, and failing a run because of that would be a false
    alarm rather than a measurement.

    [ZH]
    比较两次完全相同的短探测。**任一项**相对差超过 ``limit`` 即判定环境不稳 —— 预填充与
    decode 分别检查，因为一台主机可能一项稳定、另一项抖动。

    短于 ``min_decode_seconds`` 的解码段记录为 ``decode_compared=false`` 且不参与判定：
    几个 token 撑不起 3% 的比较，据此判失败是误报而不是测量。
    """
    prefill_a = float(first.prefill_tps or 0.0)
    prefill_b = float(second.prefill_tps or 0.0)
    decode_a = float(first.decode_tps or 0.0)
    decode_b = float(second.decode_tps or 0.0)
    prefill_seconds_a = float(first.prefill_seconds or 0.0)
    prefill_seconds_b = float(second.prefill_seconds or 0.0)
    # [EN] ``decode_tps`` counts the tokens emitted *after* the first one, so the
    # [EN] duration of that segment is ``(tokens - 1) / decode_tps``.
    # [ZH] ``decode_tps`` 只统计首个 token **之后**的输出，因此该段时长是
    # [ZH] ``(tokens - 1) / decode_tps``。
    decode_seconds_a = (max(0, first.tokens - 1) / decode_a) if decode_a > 0.0 else 0.0
    decode_seconds_b = (max(0, second.tokens - 1) / decode_b) if decode_b > 0.0 else 0.0
    decode_compared = (decode_a > 0.0 and decode_b > 0.0
                       and min(decode_seconds_a, decode_seconds_b) >= min_decode_seconds)
    prefill_ratio = _relative_gap(prefill_a, prefill_b)
    decode_ratio = _relative_gap(decode_a, decode_b) if decode_compared else 0.0
    unstable = bool(prefill_ratio > float(limit)
                    or (decode_compared and decode_ratio > float(limit)))
    return {
        "prompt_tokens": int(first.prompt_tokens or 0),
        "gen_tokens": int(max(first.tokens, second.tokens)),
        "prefill_tps_a": round(prefill_a, 3),
        "prefill_tps_b": round(prefill_b, 3),
        "prefill_seconds_a": round(prefill_seconds_a, 4),
        "prefill_seconds_b": round(prefill_seconds_b, 4),
        "prefill_ratio": round(prefill_ratio, 4),
        "decode_tps_a": round(decode_a, 3),
        "decode_tps_b": round(decode_b, 3),
        "decode_seconds_a": round(decode_seconds_a, 4),
        "decode_seconds_b": round(decode_seconds_b, 4),
        "decode_ratio": round(decode_ratio, 4),
        "decode_compared": bool(decode_compared),
        "limit": float(limit),
        "unstable": unstable,
        "advice": NOISE_ADVICE if unstable else "",
    }


def _noise_probe(
    model_path: str,
    config: InferenceConfig,
    prompt_tokens: int,
    gen_tokens: int,
    n_threads: int,
    n_batch: int,
    n_threads_batch: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    [EN]
    Run the two identical short probes the noise verdict is built from, using the same
    measurement path as every other trial. The first probe is returned as ``probe`` so
    the caller can reuse it for rung selection instead of paying for a third run.

    [ZH]
    用与其它试探完全相同的测量路径跑两次相同的短探测，作为噪声判定的依据。第一次探测以
    ``probe`` 返回，调用方因此可以直接拿它选档，而不必再付第三次运行的代价。
    """
    first = _measure_once(
        model_path, config, int(n_threads), int(n_batch), int(prompt_tokens),
        int(gen_tokens), 1, "noise_probe_a", n_threads_batch=n_threads_batch,
        should_stop=should_stop,
    )
    second = _measure_once(
        model_path, config, int(n_threads), int(n_batch), int(prompt_tokens),
        int(gen_tokens), 1, "noise_probe_b", n_threads_batch=n_threads_batch,
        should_stop=should_stop,
    )
    return {"first": first, "second": second}


def _guard_deadline_reached(deadline: float) -> bool:
    """
    [EN]
    Whether the long-text guard's own sub-budget is spent. A module-level function so a
    test can drive the ``skipped_budget`` path without waiting five minutes.

    [ZH]
    长文本守卫自己的子预算是否已用尽。做成模块级函数，测试因此无需等五分钟就能驱动
    ``skipped_budget`` 路径。
    """
    return time.perf_counter() >= float(deadline)


def _confirm_budget_available(deadline: float, estimate_seconds: float) -> bool:
    """
    [EN]
    Whether the remaining round budget can host one winner confirmation run. The check
    happens *before* the run, so "not enough budget" is a decision, not an exception
    halfway through a measurement.

    [ZH]
    本轮剩余预算能否容下一次胜者复测。检查发生在运行**之前**，因此"预算不足"是一个决定，
    而不是测量做到一半的异常。
    """
    return (float(deadline) - time.perf_counter()) > float(estimate_seconds)


def evaluate_confirmation(
    winner_confirm: TrialResult,
    baseline_after: TrialResult,
    min_improvement_ratio: float = 0.03,
) -> Dict[str, Any]:
    """
    [EN]
    Reproducibility gate: the winner was measured once **after** everything else, and
    its prefill rate must still beat the end-of-round baseline by the adoption
    threshold. Averaging two winner runs would just average the same noise again, which
    is exactly what this check exists to avoid -- it is a *second, independent* run
    against a freshly measured baseline.

    ``baseline_after_tps`` comes from the drift check's retest, so no third baseline run
    is needed.

    [ZH]
    可复现闸门：胜者在其它一切结束**之后**再测一次，其预填充速率必须仍比轮末基线高出
    采纳阈值。把胜者的两次测量取均值只是把同一份噪声再平均一遍 —— 那正是本检查要避免的；
    这里是对照新测基线的**第二次独立**运行。

    ``baseline_after_tps`` 取自漂移自检的复测，因此不需要第三次基线运行。
    """
    confirm_tps = float(winner_confirm.prefill_tps or 0.0) if winner_confirm else 0.0
    after_tps = float(baseline_after.prefill_tps or 0.0) if baseline_after else 0.0
    if after_tps > 0.0:
        delta = (confirm_tps - after_tps) / after_tps
    else:
        delta = -1.0
    return {
        "winner_confirm_tps": round(confirm_tps, 3),
        "baseline_after_tps": round(after_tps, 3),
        "delta": round(delta, 4),
        "threshold": float(min_improvement_ratio),
        "passed": bool(after_tps > 0.0 and delta >= float(min_improvement_ratio)),
    }


def decode_thread_candidates(config: InferenceConfig, logical_cores: int = 0) -> List[int]:
    """
    [EN]
    Level 1 grid: the decode thread counts to try, with prefill threads held at their
    automatic value. ``{base, base-1, base-2, base+1}`` clamped to ``[1, logical]``,
    deduplicated -- one factor changes at a time, so a regression stays attributable.

    [ZH]
    一级网格：要尝试的 decode 线程数，prefill 线程固定为自动值。``{base, base-1,
    base-2, base+1}`` 钳制到 ``[1, 逻辑核]`` 并去重 —— 一次只变一个因子，回退才能归因。
    """
    base = max(1, int(config.resolve_n_threads()))
    cores = max(1, int(logical_cores or os.cpu_count() or base))
    values: List[int] = []
    for value in (base, base - 1, base - 2, base + 1):
        clamped = min(cores, max(1, int(value)))
        if clamped not in values:
            values.append(clamped)
    return values


def prefill_thread_candidates(
    config: InferenceConfig,
    logical_cores: int = 0,
) -> List[int]:
    """
    [EN]
    Level 2 grid: the prefill thread counts to try, with decode threads held at the
    level-1 winner. ``{auto, auto-1, auto+1, base}`` clamped to ``[1, logical]``.

    [ZH]
    二级网格：要尝试的 prefill 线程数，decode 线程固定为一级胜者。``{auto, auto-1,
    auto+1, base}`` 钳制到 ``[1, 逻辑核]``。
    """
    base = max(1, int(config.resolve_n_threads()))
    cores = max(1, int(logical_cores or os.cpu_count() or base))
    # [EN] "auto" is the configuration's own automatic prefill resolution -- derived from
    # [EN] the *resolve_n_threads()* base, not from the logical core count, so it still
    # [EN] means "two cores left for the interface thread" on this machine.
    # [ZH] "自动"就是配置自身的自动预填充解析值 —— 以 ``resolve_n_threads()`` 为基准而不是
    # [ZH] 逻辑核数，因此在本机仍然表示"给界面线程留两个核心"。
    automatic = int(config.resolve_n_threads_batch())
    values: List[int] = []
    for value in (automatic, automatic - 1, automatic + 1, base):
        clamped = min(cores, max(1, int(value)))
        if clamped not in values:
            values.append(clamped)
    return values


def candidate_grid(
    config: InferenceConfig,
    logical_cores: int = 0,
    max_trials: int = 8,
) -> List[Dict[str, int]]:
    """
    [EN]
    The level-1 probe grid in the legacy mapping shape (``n_threads`` /
    ``n_threads_batch`` / ``n_batch``), truncated to ``max_trials``. Kept as a named
    function because it is the documented, inspectable description of "what will be
    tried"; the run itself walks the two levels separately.

    [ZH]
    一级试探网格的旧映射形式（``n_threads`` / ``n_threads_batch`` / ``n_batch``），截断到
    ``max_trials``。保留为具名函数，因为它是"将要试什么"的文档化、可检查的描述；实际运行
    分两级分别遍历。
    """
    base_batch = max(1, int(config.n_batch))
    cores = max(1, int(logical_cores or os.cpu_count() or config.resolve_n_threads()))
    auto_batch = int(config.resolve_n_threads_batch())
    grid: List[Dict[str, int]] = []
    for threads in decode_thread_candidates(config, cores):
        grid.append({"n_threads": threads, "n_threads_batch": auto_batch,
                     "n_batch": base_batch})
    return grid[: max(1, int(max_trials))]


def iter_tune(
    model_path: str,
    config: Optional[InferenceConfig] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    persist: bool = True,
) -> Iterator[Dict[str, Any]]:
    """
    [EN]
    Generator form of the tuning pass: yields one structured event per stage
    (``probe``, ``rung``, ``repeats``, ``baseline``, ``trial``, ``drift``,
    ``long_guard``, ``persisted`` / ``keep`` / ``aborted``, ``done``). The final
    ``done`` event carries both the JSON-ready report (``report``) and the live object
    (``_report``), so a streaming caller can forward progress while a blocking caller
    still gets the object.

    Every stage is reported, including the rejected candidates, because "which
    configurations lost and by how much" is the evidence that the criteria were
    actually applied rather than assumed.

    ``persist=False`` is a hard promise: no branch of this function touches the
    configuration file, and the report says so (``persisted=False``).

    [ZH]
    调优过程的生成器形式：每个阶段产出一个结构化事件（``probe``、``rung``、``repeats``、
    ``baseline``、``trial``、``drift``、``long_guard``、``persisted`` / ``keep`` /
    ``aborted``、``done``）。最后的 ``done`` 事件同时携带可直接 JSON 化的报告
    （``report``）与活对象（``_report``），因此流式调用方能转发进度、阻塞调用方仍能拿到
    对象。

    所有阶段都会上报，包括被拒绝的候选项 —— "哪些配置落败、差距多少"正是"判据确实生效"
    而不是被假定的证据。

    ``persist=False`` 是硬承诺：本函数没有任何分支会碰配置文件，报告也会如实说明
    （``persisted=False``）。
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

        policy = cfg.resolve_tuning()
        report.policy = dict(policy)
        repeats_cap = int(policy["repeats"])
        threshold = float(policy["min_improvement_ratio"])
        decode_limit = float(policy["decode_regression_ratio"])
        p95_limit = float(policy["responsiveness_p95_ms"])
        max_trials = int(policy["max_trials"])
        max_seconds = float(policy["max_seconds"])
        short_tokens = int(policy["prompt_tokens_short"])
        long_tokens = int(policy["prompt_tokens_long"])
        xlong_tokens = int(policy["prompt_tokens_xlong"])
        gen_tokens = int(policy["gen_tokens"])
        primary_target = float(policy["primary_target_seconds"])
        guard_ratio = float(policy["long_guard_ratio"])
        guard_budget = float(policy["long_guard_seconds"])
        long_scope_min_rung = int(policy["long_scope_min_rung"])
        deadline = started + max_seconds
        # [EN] The grid is derived from this machine's logical core count, so the probe
        # [EN] range adapts to the host instead of assuming one hardware class.
        # [ZH] 候选网格由本机逻辑核数推导，因此试探范围随主机自适应，而不是假定某一档硬件。
        try:
            logical = int(os.cpu_count() or 0)
        except Exception:  # noqa: BLE001 - core count is informational only
            logical = 0
        core_count = max(1, logical or cfg.resolve_n_threads())

        yield {"stage": "start", "model": report.model, "config": cfg.describe(path),
               "policy": dict(policy), "persist": bool(persist)}

        # [EN] ---- Noise probe: run the 512-token workload twice, back to back ----
        # [EN] A host that cannot reproduce two identical short runs cannot support a
        # [EN] 3 % adoption threshold, so this verdict comes *before* any candidate (and
        # [EN] before any configuration mutation). It is its own reason
        # [EN] (``noisy_environment``), never ``no_improvement`` and never ``drift``.
        # [ZH] ---- 噪声探测：把 512 档工作负载连续跑两次 ----
        # [ZH] 复现不出两次相同短运行的主机撑不起 3% 的采纳门槛，因此该判定发生在**任何
        # [ZH] 候选之前**（也在任何配置改动之前）。它有自己的 reason
        # [ZH] （``noisy_environment``），既不是 ``no_improvement`` 也不是 ``drift``。
        noise_limit = float(policy["noise_probe_ratio"])
        noise_gen = max(1, min(gen_tokens, NOISE_PROBE_GEN_TOKENS))
        probe_pair = _noise_probe(
            path, cfg, short_tokens, noise_gen, cfg.resolve_n_threads(),
            int(cfg.n_batch), n_threads_batch=int(cfg.n_threads_batch),
            should_stop=should_stop,
        )
        probe = probe_pair["first"]
        second_probe = probe_pair["second"]
        noise = evaluate_noise_probe(probe, second_probe, noise_limit)
        report.noise_probe = noise
        log.info("[tune] 噪声探测 prefill %.2f/%.2f (%.2f%%) decode %.2f/%.2f (%.2f%%) "
                 "limit=%.2f%% compared=%s",
                 noise["prefill_tps_a"], noise["prefill_tps_b"],
                 noise["prefill_ratio"] * 100.0, noise["decode_tps_a"],
                 noise["decode_tps_b"], noise["decode_ratio"] * 100.0,
                 noise_limit * 100.0, noise["decode_compared"])
        yield {"stage": "noise_probe", "noise_probe": dict(noise),
               "first": probe.to_dict(), "second": second_probe.to_dict()}
        report.probe = probe
        # [EN] Kept for the pre-1.1 report shape: the first probe *is* the short-tier
        # [EN] baseline, and it is reused for rung selection (no third run).
        # [ZH] 保留 1.1 之前的报告字段：第一次探测**就是**短档基线，并被复用于选档
        # [ZH] （不跑第三次）。
        report.baseline_short = probe
        if not probe.ok or not second_probe.ok:
            report.reason = probe.error or second_probe.error or "probe_failed"
            report.elapsed_s = time.perf_counter() - started
            yield {"stage": "aborted", "reason": report.reason,
                   "report": report.to_dict(), "_report": report}
            return
        if noise["unstable"]:
            # [EN] Early exit. No candidate scan, no guard, and not a single cfg field is
            # [EN] touched: an unstable host must not leave a bookmark behind.
            # [ZH] 早退。不扫候选、不跑守卫、一个 cfg 字段都不碰：不稳的主机不得留下任何
            # [ZH] 书签。
            report.reason = "noisy_environment"
            report.adopted = False
            report.long_guard = {"status": "not_run", "reason": "noisy_environment"}
            report.elapsed_s = time.perf_counter() - started
            log.warning("[tune] 环境不稳：prefill 差 %.2f%%、decode 差 %.2f%%，阈值 %.2f%%；"
                        "未进入候选扫描", noise["prefill_ratio"] * 100.0,
                        noise["decode_ratio"] * 100.0, noise_limit * 100.0)
            yield {"stage": "aborted", "reason": "noisy_environment",
                   "noise_probe": dict(noise),
                   "report": report.to_dict(), "_report": report}
            return

        # [EN] ---- Algorithm 2: pick the main rung from the (reused) probe ----
        # [ZH] ---- 算法 2：由（复用的）探测量选出主判据档 ----
        rungs = sorted({short_tokens, long_tokens, xlong_tokens})
        rung = select_rung(probe.prefill_tps, probe.decode_tps, gen_tokens, rungs,
                           primary_target)
        report.rung = rung
        main_tokens = int(rung["selected_rung"])
        # [EN] Algorithm 4 fixes the guard rung from the main rung: twice the main load,
        # [EN] capped at 16384 tokens.
        # [ZH] 算法 4 由主档确定守卫档：主负载的两倍，封顶 16384 token。
        guard_rung = min(2 * main_tokens, GUARD_RUNG_CEILING)
        round_seconds = float(
            rung["estimates_seconds"].get(str(main_tokens), 0.0)
            if isinstance(rung["estimates_seconds"].get(str(main_tokens)), (int, float))
            else primary_target)
        yield {"stage": "rung", "rung": dict(rung)}

        # [EN] ---- Algorithm 3: repeats from the remaining budget ----
        # [ZH] ---- 算法 3：由剩余预算推导重复次数 ----
        planned_candidates = min(
            max_trials,
            len(decode_thread_candidates(cfg, core_count))
            + len(prefill_thread_candidates(cfg, core_count)),
        )
        repeats_policy = derive_repeats(
            max_seconds - (time.perf_counter() - started), planned_candidates,
            round_seconds, REPEATS_FLOOR, max(REPEATS_FLOOR, min(REPEATS_CEILING,
                                                                 repeats_cap)),
        )
        repeats = int(repeats_policy["value"])
        report.repeats_policy = repeats_policy
        yield {"stage": "repeats", "repeats": dict(repeats_policy)}

        log.info("[tune] 选档 rung=%d probe_tps=%.2f estimate=%.2fs limit=%.2fs "
                 "repeats=%d model=%s", main_tokens, float(probe.prefill_tps),
                 round_seconds, float(rung["limit_seconds"]), repeats, report.model)

        baseline = _measure_once(
            path, cfg, cfg.resolve_n_threads(), int(cfg.n_batch), main_tokens,
            gen_tokens, repeats, "baseline",
            n_threads_batch=int(cfg.n_threads_batch), should_stop=should_stop,
        )
        report.baseline = baseline
        yield {"stage": "baseline", "trial": baseline.to_dict()}
        if not baseline.ok:
            report.reason = baseline.error or "baseline_failed"
            report.elapsed_s = time.perf_counter() - started
            yield {"stage": "aborted", "reason": report.reason,
                   "report": report.to_dict(), "_report": report}
            return

        baseline_key = (int(baseline.n_threads), int(baseline.n_threads_batch))
        measured_keys = {baseline_key}
        rejections: List[str] = []
        abort_reason = ""

        def measure_candidate(label: str, threads: int, batch_threads: int) -> TrialResult:
            """[EN] Measure one candidate at the main rung. [ZH] 在主判据档上测量一个候选。"""
            return _measure_once(
                path, cfg, int(threads), int(cfg.n_batch), main_tokens, gen_tokens,
                repeats, label, n_threads_batch=int(batch_threads),
                should_stop=should_stop,
            )

        def note(trial: TrialResult) -> Judgement:
            """[EN] Judge, record, and yield-ready the candidate. [ZH] 裁决、记录并准备上报。"""
            verdict = evaluate_candidate(baseline, trial, threshold, decode_limit, p95_limit)
            report.judgements[trial.label] = verdict.to_dict()
            if not verdict.accepted and verdict.reason != "improved":
                rejections.append(verdict.reason)
            return verdict

        # [EN] ---- Level 1: decode threads, prefill fixed at its automatic value ----
        # [ZH] ---- 一级：decode 线程，prefill 固定为自动值 ----
        auto_batch_threads = int(cfg.resolve_n_threads_batch())
        level1_best = baseline
        level1_candidates = decode_thread_candidates(cfg, core_count)
        for index, threads in enumerate(level1_candidates, start=1):
            if len(report.trials) >= max_trials:
                break
            if _deadline_reached(deadline):
                abort_reason = "time_budget"
                break
            if should_stop is not None and should_stop():
                abort_reason = "stopped"
                break
            key = (int(threads), auto_batch_threads)
            if key in measured_keys:
                # [EN] Same parameters as an already measured point (the baseline itself
                # [EN] when prefill is already automatic): measuring it twice would only
                # [EN] spend budget.
                # [ZH] 与已测点参数相同（当 prefill 本来就是自动值时就是基线本身）：再测一遍
                # [ZH] 只会浪费预算。
                continue
            measured_keys.add(key)
            label = "decode%d" % index
            trial = measure_candidate(label, threads, auto_batch_threads)
            report.trials.append(trial)
            verdict = note(trial)
            yield {"stage": "trial", "level": "decode", "trial": trial.to_dict(),
                   "judgement": verdict.to_dict(), "threshold": threshold,
                   "best_prefill_tps": round(float(level1_best.prefill_tps), 2)}
            if trial.ok and (trial.p95_ms <= p95_limit) \
                    and trial.prefill_tps > level1_best.prefill_tps:
                level1_best = trial
            if verdict.accepted:
                log.info("[tune] %s 通过判据：n_threads=%d n_threads_batch=%d "
                         "prefill=%.2f (+%.2f%%) decode=%+.2f%%",
                         label, trial.n_threads, trial.n_threads_batch,
                         trial.prefill_tps, verdict.prefill_gain * 100.0,
                         verdict.decode_delta * 100.0)

        # [EN] ---- Level 2: prefill threads, decode fixed at the level-1 winner ----
        # [ZH] ---- 二级：prefill 线程，decode 固定为一级胜者 ----
        decode_winner = int(level1_best.n_threads)
        if not abort_reason:
            for index, batch_threads in enumerate(
                    prefill_thread_candidates(cfg, core_count), start=1):
                if len(report.trials) >= max_trials:
                    break
                if _deadline_reached(deadline):
                    abort_reason = "time_budget"
                    break
                if should_stop is not None and should_stop():
                    abort_reason = "stopped"
                    break
                key = (decode_winner, int(batch_threads))
                if key in measured_keys:
                    continue
                measured_keys.add(key)
                label = "prefill%d" % index
                trial = measure_candidate(label, decode_winner, batch_threads)
                report.trials.append(trial)
                verdict = note(trial)
                yield {"stage": "trial", "level": "prefill", "trial": trial.to_dict(),
                       "judgement": verdict.to_dict(), "threshold": threshold,
                       "decode_winner": decode_winner}
                if verdict.accepted:
                    log.info("[tune] %s 通过判据：n_threads=%d n_threads_batch=%d",
                             label, trial.n_threads, trial.n_threads_batch)

        if abort_reason:
            report.reason = abort_reason
            report.elapsed_s = time.perf_counter() - started
            # [EN] A budget overrun or a stop is *not* a verified result: the round is
            # [EN] closed with what was measured and nothing is written back.
            # [ZH] 预算用尽或中止都不是被验证的结果：用已测数据收尾，不写回任何东西。
            log.info("[tune] 收尾：reason=%s 已测 %d 组，未写回", abort_reason,
                     len(report.trials))
            yield {"stage": "aborted", "reason": abort_reason,
                   "report": report.to_dict(), "_report": report}
            return

        # [EN] ---- Round-level drift check: did the machine stay put? ----
        # [ZH] ---- 轮级漂移自检：这台机器在整个过程中没变吗 ----
        retest = _measure_once(
            path, cfg, baseline.n_threads, baseline.n_batch, main_tokens, gen_tokens,
            1, "baseline_retest", n_threads_batch=baseline.n_threads_batch,
            should_stop=should_stop,
        )
        report.baseline_retest = retest
        drift = baseline_drift(baseline, retest)
        report.drift = drift
        yield {"stage": "drift", "retest": retest.to_dict(), "drift": drift}
        if drift["invalid"]:
            report.adopted = False
            report.reason = "drift"
            # [EN] The guard never ran: say so explicitly instead of leaving an empty
            # [EN] mapping that a reader could mistake for "nothing to report".
            # [ZH] 守卫没有执行：明确写出来，而不是留一个空映射让读者误以为"无需上报"。
            report.long_guard = {"status": "not_run", "reason": "drift",
                                 "rung": int(guard_rung)}
            report.elapsed_s = time.perf_counter() - started
            log.warning("[tune] 基线漂移 %.1f%% 超过 %.1f%%，整轮作废",
                        float(drift["ratio"]) * 100.0, float(drift["limit"]) * 100.0)
            # [EN] An invalid round writes nothing at all -- a number the machine cannot
            # [EN] reproduce must not become the shipped configuration.
            # [ZH] 作废的一轮什么都不写：这台机器复现不出的数字绝不能用作出厂配置。
            yield {"stage": "aborted", "reason": "drift",
                   "report": report.to_dict(), "_report": report}
            return

        # [EN] ---- Decode drift re-check: is the decode delta of this round trustworthy? ----
        # [ZH] ---- decode 漂移复核：本轮的 decode 差值可信吗 ----
        decode_retest = _measure_once(
            path, cfg, baseline.n_threads, baseline.n_batch, main_tokens, gen_tokens,
            1, "baseline_decode_retest", n_threads_batch=baseline.n_threads_batch,
            should_stop=should_stop,
        )
        report.decode_retest = decode_retest
        decode_shift = decode_drift(baseline, decode_retest)
        report.decode_drift = decode_shift
        report.decode_deltas_unreliable = bool(decode_shift["unreliable"])
        yield {"stage": "decode_drift", "retest": decode_retest.to_dict(),
               "decode_drift": decode_shift}
        if report.decode_deltas_unreliable:
            # [EN] Decode is the noisy quantity here. If the baseline itself moved, the
            # [EN] per-candidate decode deltas are measurement artefacts, so the round
            # [EN] stops using them as a veto -- visibly, and only for this round.
            # [ZH] 这里的噪声大户是 decode。若基线自己都漂了，各候选的 decode 差值就是测量
            # [ZH] 假象，因此本轮不再用它作否决 —— 明示且仅限本轮。
            log.warning("[tune] decode 漂移 %.1f%% 超过 %.1f%%，本轮暂停 decode 否决",
                        float(decode_shift["ratio"]) * 100.0,
                        float(decode_shift["limit"]) * 100.0)
            for trial in report.trials:
                verdict = evaluate_candidate(baseline, trial, threshold, decode_limit,
                                             p95_limit, apply_decode_veto=False)
                report.judgements[trial.label] = verdict.to_dict()

        # [EN] Winner = accepted candidate with the highest prefill median at the main
        # [EN] rung. The guard below may still overturn it; a second finalist is kept so
        # [EN] that a guard veto on the first does not have to void the round.
        # [ZH] 胜者 = 主判据档 prefill 中位数最高的已通过候选。下面的守卫仍可推翻它；同时
        # [ZH] 保留第二个最终候选，使"第一名被守卫否决"不必导致整轮作废。
        accepted: List[TrialResult] = [
            trial for trial in report.trials
            if report.judgements.get(trial.label, {}).get("accepted")
        ]
        accepted.sort(key=lambda trial: float(trial.prefill_tps), reverse=True)
        finalists = accepted[:2]
        winner: Optional[TrialResult] = finalists[0] if finalists else None
        report.best = winner or baseline

        # [EN] ---- Algorithm 4-5: long-text guard on the final two candidates ----
        # [ZH] ---- 算法 4–5：对最终两个候选做长文本守卫 ----
        tuned_scope = scope_for_rung(guard_rung, long_scope_min_rung)
        report.tuned_scope = tuned_scope
        guard: Dict[str, Any] = {
            "rung": int(guard_rung),
            "scope": tuned_scope,
            "status": "not_applicable",
            "baseline": {},
            "candidates": [],
            "candidate": {},
            "selected_label": "",
            "guard_vetoed_first": False,
            "guard_delta": None,
            "regression": None,
            "limit": float(guard_ratio),
            "budget_seconds": float(guard_budget),
            "elapsed_s": 0.0,
        }
        guard_winner: Optional[TrialResult] = None
        guard_skipped = False
        if not finalists:
            guard["status"] = "not_applicable"
            guard["note"] = "no_candidate_accepted"
        else:
            guard_deadline = time.perf_counter() + guard_budget
            guard_started = time.perf_counter()
            baseline_guard: Optional[TrialResult] = None
            if _guard_deadline_reached(guard_deadline):
                guard_skipped = True
            else:
                baseline_guard = _measure_once(
                    path, cfg, baseline.n_threads, baseline.n_batch, guard_rung,
                    gen_tokens, 1, "long_guard_baseline",
                    n_threads_batch=baseline.n_threads_batch, should_stop=should_stop,
                )
                guard["baseline"] = baseline_guard.to_dict()
                if not baseline_guard.ok:
                    # [EN] Without a valid reference there is nothing to compare, so the
                    # [EN] guard is unverified -- never "passed".
                    # [ZH] 没有有效参照就无从比较，因此守卫为"未验证" —— 绝不是"通过"。
                    guard_skipped = True
                    guard["note"] = "baseline_guard_failed"

            measured_any = False
            guard_results: List[Any] = []
            for rank, trial in enumerate(finalists):
                if guard_skipped:
                    break
                estimate = estimate_round_seconds(guard_rung, trial.prefill_tps,
                                                  trial.decode_tps, gen_tokens)
                if _guard_deadline_reached(guard_deadline) or \
                        (time.perf_counter() - guard_started) + estimate > guard_budget:
                    # [EN] Remaining finalists cannot be verified inside the sub-budget.
                    # [ZH] 剩余最终候选无法在子预算内验证。
                    guard_skipped = True
                    guard["skipped_rank"] = rank
                    break
                measured = _measure_once(
                    path, cfg, trial.n_threads, trial.n_batch, guard_rung, gen_tokens,
                    1, "long_guard_%s" % trial.label,
                    n_threads_batch=trial.n_threads_batch, should_stop=should_stop,
                )
                verdict_guard = evaluate_long_guard(baseline_guard, measured,
                                                    guard_ratio) if baseline_guard else {}
                record = {
                    "label": trial.label,
                    "rank": rank,
                    "n_threads": int(trial.n_threads),
                    "n_threads_batch": int(trial.n_threads_batch),
                    "prefill_tps": round(float(measured.prefill_tps), 3),
                    "decode_tps": round(float(measured.decode_tps), 3),
                    "guard_delta": verdict_guard.get("guard_delta"),
                    "regression": verdict_guard.get("regression"),
                    "vetoed": bool(verdict_guard.get("vetoed")),
                }
                guard["candidates"].append(record)
                if not guard["candidate"]:
                    guard["candidate"] = measured.to_dict()
                measured_any = True
                guard_results.append((trial, record))
            # [EN] Both finalists are measured (one run each); the winner is the
            # [EN] best-ranked one that *passed* the guard -- "guard passed + best main
            # [EN] criterion", in that order of precedence.
            # [ZH] 两个最终候选各测一次；胜者是通过守卫者中主判据最优的那个 ——
            # [ZH] "守卫通过 + 主判据最优"，按此优先级。
            passing = [item for item in guard_results if not item[1]["vetoed"]]
            if passing:
                guard_winner = passing[0][0]
                guard["selected_label"] = passing[0][1]["label"]
                guard["guard_delta"] = passing[0][1]["guard_delta"]
                guard["regression"] = passing[0][1]["regression"]
                guard["status"] = "passed"
                # [EN] True only when the top-ranked candidate lost and a lower-ranked
                # [EN] one took over, which is exactly what the flag is for.
                # [ZH] 仅当排名第一的候选落败、由更低排名者继任时为真 —— 这正是该标记的
                # [ZH] 用途。
                guard["guard_vetoed_first"] = bool(
                    finalists and guard["selected_label"] != finalists[0].label)
            elif measured_any:
                # [EN] Every measured finalist was vetoed at the guard rung.
                # [ZH] 所有已测最终候选都在守卫档被否决。
                guard["status"] = "vetoed"
                guard["regression"] = max(
                    float(item["regression"]) for item in guard["candidates"])
                guard["guard_delta"] = min(
                    float(item["guard_delta"]) for item in guard["candidates"])
                guard["guard_vetoed_first"] = True
            if guard_skipped and guard_winner is None and guard["status"] != "vetoed":
                guard["status"] = "skipped_budget"
                report.long_guard_skipped = True
                # [EN] Pick the best-ranked finalist that could not be verified, and say
                # [EN] so on the adoption -- "not tested" is not "passed".
                # [ZH] 取排名最前但未能验证的最终候选，并在采纳结论上写明 —— "没测"不是
                # [ZH] "通过"。
                skipped_rank = int(guard.get("skipped_rank", 0))
                if skipped_rank < len(finalists):
                    guard_winner = finalists[skipped_rank]
                    guard["selected_label"] = guard_winner.label
            guard["elapsed_s"] = round(time.perf_counter() - guard_started, 1)
        report.long_guard = guard
        yield {"stage": "long_guard", "long_guard": dict(guard),
               "long_guard_skipped": bool(report.long_guard_skipped)}

        if guard_winner is None and guard["status"] == "vetoed":
            # [EN] The long-text guard outranks the short-rung win: a configuration that
            # [EN] is 8 % faster on the main rung but slower on the guard rung is not an
            # [EN] improvement for a document reader.
            # [ZH] 长文本守卫高于主档收益：在主判据档快 8% 却在守卫档更慢的配置，对文档
            # [ZH] 阅读器来说不是改进。
            report.adopted = False
            report.reason = "long_guard"
            report.best = baseline
            report.elapsed_s = time.perf_counter() - started
            log.warning("[tune] 长文本守卫否决全部最终候选（守卫档 %d）", guard_rung)
            yield {"stage": "aborted", "reason": "long_guard",
                   "report": report.to_dict(), "_report": report}
            return

        winner = guard_winner if guard_winner is not None else winner
        report.best = winner or baseline

        # [EN] ---- Reproducibility gate: confirm the winner once, for real ----
        # [ZH] ---- 可复现闸门：对胜者再独立测一次 ----
        # [EN] Cost contract: one run, one candidate, main rung only. The comparison
        # [EN] baseline is the drift check's retest -- no third baseline run. Averaging
        # [EN] the winner's two runs instead would simply average the same noise twice.
        # [ZH] 成本契约：只跑 1 次、只跑 1 个候选、只跑主判据档。对照基线用漂移自检的复测
        # [ZH] —— 不跑第三次基线。把胜者两次测量取均值，只会把同一份噪声平均两遍。
        confirm: Dict[str, Any] = {
            "status": "not_applicable",
            "winner_label": "",
            "winner_confirm_tps": 0.0,
            "baseline_after_tps": 0.0,
            "delta": 0.0,
            "threshold": float(threshold),
            "passed": False,
            "skipped_reason": "",
        }
        if winner is not None and report.long_guard.get("status") == "passed":
            confirm["winner_label"] = winner.label
            confirm["baseline_after_tps"] = round(
                float(report.baseline_retest.prefill_tps or 0.0), 3)
            estimate_confirm = estimate_round_seconds(
                main_tokens, winner.prefill_tps, winner.decode_tps, gen_tokens)
            if not _confirm_budget_available(deadline, estimate_confirm):
                # [EN] No budget for evidence means no change: "better safe than sorry"
                # [EN] is not a reason to adopt an unverified number.
                # [ZH] 没有预算做取证就不改：绝不用"宁可信其有"来采纳一个未验证的数字。
                confirm["status"] = "skipped_budget"
                confirm["skipped_reason"] = "remaining_budget"
                confirm["estimated_seconds"] = round(estimate_confirm, 2)
                report.confirm = confirm
                report.adopted = False
                report.applied = False
                report.reason = "unconfirmed_improvement"
                report.best = baseline
                report.elapsed_s = time.perf_counter() - started
                log.warning("[tune] 复测预算不足（需 %.1fs），不采纳胜者 %s",
                            estimate_confirm, winner.label)
                yield {"stage": "confirm", "confirm": dict(confirm)}
                yield {"stage": "aborted", "reason": "unconfirmed_improvement",
                       "report": report.to_dict(), "_report": report}
                return
            winner_confirm = _measure_once(
                path, cfg, winner.n_threads, winner.n_batch, main_tokens, gen_tokens,
                1, "winner_confirm", n_threads_batch=winner.n_threads_batch,
                should_stop=should_stop,
            )
            confirm.update(evaluate_confirmation(winner_confirm, report.baseline_retest,
                                                 threshold))
            confirm["status"] = "measured"
            report.confirm = confirm
            yield {"stage": "confirm", "confirm": dict(confirm),
                   "trial": winner_confirm.to_dict()}
            if not confirm["passed"]:
                # [EN] ``unconfirmed_improvement`` is deliberately its own reason: the
                # [EN] candidate did win the scan (not ``no_improvement``), the machine
                # [EN] did not drift (not ``drift``), the host was quiet (not
                # [EN] ``noisy_environment``) and the guard passed (not ``long_guard``).
                # [EN] The win simply did not reproduce.
                # [ZH] ``unconfirmed_improvement`` 刻意独立成一种 reason：候选确实在扫描中
                # [ZH] 赢了（不是 ``no_improvement``），机器没有漂移（不是 ``drift``），
                # [ZH] 主机安静（不是 ``noisy_environment``），守卫也通过了（不是
                # [ZH] ``long_guard``）—— 只是这次胜利无法复现。
                report.adopted = False
                report.applied = False
                report.reason = "unconfirmed_improvement"
                report.best = baseline
                report.elapsed_s = time.perf_counter() - started
                log.warning("[tune] 复测未达门槛：%.2f vs %.2f = %+.2f%% < %.2f%%",
                            float(confirm["winner_confirm_tps"]),
                            float(confirm["baseline_after_tps"]),
                            float(confirm["delta"]) * 100.0, threshold * 100.0)
                yield {"stage": "aborted", "reason": "unconfirmed_improvement",
                       "report": report.to_dict(), "_report": report}
                return
        else:
            report.confirm = confirm

        # [EN] ---- Only a verified winner is written back, and only for long scope ----
        # [ZH] ---- 只有被验证的胜者才写回，且仅限长文本范围 ----
        if winner is not None and report.long_guard["status"] != "not_applicable":
            report.adopted = True
            report.reason = "improved"
            if tuned_scope == "long":
                cfg.n_threads = int(winner.n_threads)
                cfg.n_threads_batch = int(winner.n_threads_batch)
                report.applied = True
            else:
                # [EN] A short-scope calibration is recorded for reference only: its
                # [EN] winning thread counts are not allowed to rewrite the live
                # [EN] configuration, because they were never proven on long prompts.
                # [ZH] 短文本范围的标定只作参考记录：它的胜出线程数不得改写生效配置，
                # [ZH] 因为这些数字从未在长 prompt 上被证明。
                report.applied = False
                log.info("[tune] 短文本范围（守卫档 %d < 阈值 %d），线程数只记录不写回",
                         guard_rung, long_scope_min_rung)
        else:
            first_rejection = rejections[0] if rejections else ""
            report.reason = first_rejection or "no_improvement"
            if report.reason == "improved":
                report.reason = "no_improvement"

        fingerprint = hardware_fingerprint(path)
        report.fingerprint = fingerprint
        cfg.tuned_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        cfg.tuned_model = report.model
        cfg.tuned_scope = tuned_scope
        cfg.tuned_baseline = baseline.to_dict()
        cfg.tuned_best = report.best.to_dict()
        cfg.tuned_fingerprint = fingerprint
        if persist:
            report.config_path = cfg.save() or cfg.path
            report.persisted = True
        report.elapsed_s = time.perf_counter() - started
        if report.adopted:
            yield {"stage": "persisted", "path": report.config_path,
                   "adopted": True, "skipped": not persist,
                   "tuned_scope": tuned_scope, "applied": bool(report.applied),
                   "long_guard_status": report.long_guard.get("status", ""),
                   "long_guard_skipped": bool(report.long_guard_skipped),
                   "guard_vetoed_first": bool(
                       report.long_guard.get("guard_vetoed_first")),
                   "fingerprint": dict(fingerprint)}
        else:
            yield {"stage": "keep", "trial": report.best.to_dict(),
                   "threshold": threshold, "reason": report.reason,
                   "tuned_scope": tuned_scope, "applied": bool(report.applied),
                   "long_guard_status": report.long_guard.get("status", ""),
                   "skipped": not persist}

        log.info("[tune] 结束：reason=%s adopted=%s applied=%s scope=%s "
                 "best_prefill=%.2f guard=%s guard_skipped=%s rung=%d repeats=%d "
                 "decode_unreliable=%s elapsed=%.1fs",
                 report.reason, report.adopted, report.applied, tuned_scope,
                 float(report.best.prefill_tps or 0.0),
                 report.long_guard.get("status", ""), bool(report.long_guard_skipped),
                 int(rung["selected_rung"]), repeats,
                 bool(report.decode_deltas_unreliable), report.elapsed_s)
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
