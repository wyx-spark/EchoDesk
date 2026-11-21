"""
[EN]
Externalised inference configuration for the Reader backend.

Every llama.cpp construction parameter that used to live in Python code --
thread count, batch and micro-batch size, memory locking, memory mapping,
FlashAttention, KV-cache data type, GPU layer count and the document-driven
context window policy -- is read from ``inference_config.json``. The file is the
single source of truth: the hardware auto-tuner (``inference_tuner.py``) writes
its winning combination back into the same file, so a tuning result survives a
restart without touching any source file.

Resolution order for the configuration file:

1. ``ECHODESK_INFERENCE_CONFIG`` (absolute path; used by tests and by operators
   who keep the file outside the program directory);
2. ``<state_dir>/inference_config.json`` -- the same writable state directory
   that holds ``config.json``;
3. ``<program_dir>/inference_config.json`` -- development tree fallback.

[ZH]
Reader 后端的外部化推理配置。

原先写在 Python 代码里的所有 llama.cpp 构建参数 —— 线程数、批大小与微批大小、内存
锁定、内存映射、FlashAttention、KV Cache 数据类型、GPU 层数，以及文档驱动的上下文
窗口策略 —— 一律从 ``inference_config.json`` 读取。该文件是唯一的事实来源：硬件自动
调优模块（``inference_tuner.py``）把胜出的参数组合写回同一文件，因此调优结果重启后
依然有效，且不需要改动任何源码。

配置文件的解析顺序：

1. ``ECHODESK_INFERENCE_CONFIG``（绝对路径；供测试与"把文件放在程序目录之外"的运维使用）；
2. ``<state_dir>/inference_config.json`` —— 与 ``config.json`` 相同的可写状态目录；
3. ``<program_dir>/inference_config.json`` —— 开发目录兜底。
"""
import copy
import json
import logging
import os
import platform
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("EchoDesk.InferenceConfig")

#: [EN] ``ggml_type`` enum values needed by the configuration file. llama-cpp-python
#: [EN] expects integers here, while a JSON file is far more readable with names, so
#: [EN] the mapping lives next to the file format. The values are the documented
#: [EN] ``ggml_type`` ordinals and are verified against the binding when it exposes
#: [EN] ``GGMLType``.
#: [ZH] 配置文件所需的 ``ggml_type`` 枚举值。llama-cpp-python 在此处要求整数，而 JSON
#: [ZH] 用名字可读性远好得多，因此映射与文件格式放在一起。这些取值是文档化的
#: [ZH] ``ggml_type`` 序号；当绑定导出 ``GGMLType`` 时会与其核对。
GGML_TYPE_VALUES: Dict[str, int] = {
    "f32": 0,
    "f16": 1,
    "q4_0": 2,
    "q4_1": 3,
    "q5_0": 6,
    "q5_1": 7,
    "q8_0": 8,
    "q8_1": 9,
    "q2_k": 10,
    "q3_k": 11,
    "q4_k": 12,
    "q5_k": 13,
    "q6_k": 14,
    "q8_k": 15,
}


def ggml_type_value(name: Any) -> int:
    """
    [EN]
    Translate a KV-cache type name (``q8_0``) or a literal enum value into the integer
    the native llama.cpp API expects. An unknown name falls back to Q8_0, which is the Reader's
    hard baseline, rather than failing the model load.

    [ZH]
    把 KV Cache 类型名（``q8_0``）或字面枚举值转换成原生 llama.cpp 期望的整数。
    未知名称回退到 Q8_0（Reader 的硬性基线），而不是让模型加载失败。
    """
    if isinstance(name, int):
        return int(name)
    key = str(name or "").strip().lower()
    if key in GGML_TYPE_VALUES:
        return GGML_TYPE_VALUES[key]
    # [EN] The Reader edition has no Python binding to read the enum from; the numeric
    # [EN] values above are the stable ``ggml_type`` ordinals and Q8_0 is the fallback.
    # [ZH] Reader 版没有可读取枚举的 Python 绑定；上表数值就是稳定的 ``ggml_type``
    # [ZH] 序号，未知名称回退到 Q8_0。
    return GGML_TYPE_VALUES["q8_0"]


#: [EN] Supported RoPE scaling modes and the numeric ``llama_rope_scaling_type`` values
#: [EN] they map onto (``none`` = 0, ``linear`` = 1, ``yarn`` = 2).
#: [ZH] 支持的 RoPE 缩放模式，以及它们对应的 ``llama_rope_scaling_type`` 数值
#: [ZH] （``none`` = 0，``linear`` = 1，``yarn`` = 2）。
ROPE_TYPE_VALUES: Dict[str, int] = {"none": 0, "linear": 1, "yarn": 2}


def normalize_rope_type(value: Any) -> str:
    """
    [EN]
    Normalise the RoPE mode name. Anything unknown falls back to ``none``, because
    guessing an extension mode would silently change generation quality.

    [ZH]
    归一化 RoPE 模式名。未知取值回退到 ``none``：猜一个扩展模式会在用户不知情时
    改变生成质量。
    """
    key = str(value or "none").strip().lower()
    return key if key in ROPE_TYPE_VALUES else "none"


#: [EN] File name used when no explicit path is supplied.
#: [ZH] 未显式提供路径时使用的文件名。
FILENAME = "inference_config.json"

#: [EN] Environment variable that overrides the configuration path.
#: [ZH] 覆盖配置文件路径的环境变量。
ENV_OVERRIDE = "ECHODESK_INFERENCE_CONFIG"

#: [EN] Default values. They exist only so that a missing or partially written file
#: [EN] cannot crash the backend; the shipped ``inference_config.json`` always carries
#: [EN] the real numbers, and an optimisation run overwrites them.
#: [ZH] 默认值。它们的存在只是为了"文件缺失或被写坏"时不至于崩溃；随包分发的
#: [ZH] ``inference_config.json`` 始终携带真实数值，调优运行会覆盖它们。
#: [EN] Bounds for the prefill thread count. ``-1`` means "follow ``n_threads``",
#: [EN] ``0`` means "automatic" and every positive value is a concrete count; the
#: [EN] ceiling keeps a hand-edited file from asking for absurd thread pools.
#: [ZH] 预填充线程数的边界。``-1`` 表示"跟随 ``n_threads``"，``0`` 表示"自动"，
#: [ZH] 正值为具体线程数；上限用于阻止手改文件请求荒谬的线程池规模。
PREFILL_THREADS_MIN = -1
PREFILL_THREADS_MAX = 1024

#: [EN] Smallest meaningful prefill chunk: below this the per-chunk overhead and the
#: [EN] progress callback dominate the actual compute.
#: [ZH] 有意义的最小预填充分块：再小则每块开销与进度回调会盖过实际计算量。
PREFILL_CHUNK_MIN = 32

#: [EN] Bounds for the inter-chunk yield in milliseconds (0 = never yield, 50 = the
#: [EN] point where the yield itself becomes visible hesitation).
#: [ZH] 块间让核毫秒数的边界（0 = 不让核，50 = 让核本身已成可感知的停顿）。
PREFILL_YIELD_MIN = 0
PREFILL_YIELD_MAX = 50

#: [EN] Bounds for the no-progress watchdog in seconds. Below 10 s a slow first chunk
#: [EN] could trip it; above 300 s it stops protecting anything.
#: [ZH] 无进展看门狗秒数的边界。低于 10 秒时首块稍慢就会误触发；高于 300 秒则失去
#: [ZH] 保护意义。
PREFILL_STALL_MIN = 10
PREFILL_STALL_MAX = 300

#: [EN] Bounds for the v2 calibration policy (v0.2.7 step 1). They exist so that an
#: [EN] out-of-range or malformed ``tuning`` value degrades to something that still
#: [EN] measures instead of raising or producing a nonsensical comparison.
#: [ZH] v2 校准策略的边界（v0.2.7 步 1）。越界或写坏的 ``tuning`` 取值因此会退化成
#: [ZH] "仍可测量"的数值，而不是抛异常或产出无意义的比较。
TUNING_PROMPT_MIN = 32
TUNING_PROMPT_MAX = 1 << 20
TUNING_GEN_MIN = 1
TUNING_GEN_MAX = 4096
TUNING_REPEATS_MIN = 1
TUNING_REPEATS_MAX = 20
TUNING_MAX_TRIALS_MIN = 1
TUNING_MAX_TRIALS_MAX = 64
TUNING_MAX_SECONDS_MIN = 30
TUNING_MAX_SECONDS_MAX = 86400
TUNING_RATIO_MIN = 0.0
TUNING_RATIO_MAX = 1.0
TUNING_P95_MIN = 1.0
TUNING_P95_MAX = 10000.0
#: [EN] Bounds for the v0.2.7 step-1.1 load-adaptation policy: the per-round wall-clock
#: [EN] target that picks the main rung, the long-text guard tolerance, and the guard's
#: [EN] own sub-budget.
#: [ZH] v0.2.7 步 1.1 负载自适应策略的边界：选主档所用的单轮墙钟目标、长文本守卫容差，
#: [ZH] 以及守卫自己的子预算。
TUNING_PRIMARY_TARGET_MIN = 1.0
TUNING_PRIMARY_TARGET_MAX = 600.0
TUNING_LONG_GUARD_MIN = 10
TUNING_LONG_GUARD_MAX = 3600

#: [EN] Bounds for the rung at which a calibration counts as *long-scope*. Below it the
#: [EN] result is only valid for short prompts and must not rewrite the thread counts.
#: [ZH] 标定被视为**长文本范围**的档位下限。低于它时结果只对短 prompt 有效，不得改写
#: [ZH] 线程数。
TUNING_LONG_SCOPE_MIN = 64
TUNING_LONG_SCOPE_MAX = 16384

#: [EN] Allowed range of the environment-noise threshold. Zero would treat every
#: [EN] measurement as unstable, so the lower bound is a tiny positive value instead.
#: [ZH] 环境噪声阈值的允许区间。零会把任何测量都判为不稳，因此下限取一个极小正数。
TUNING_NOISE_MIN = 0.0001
TUNING_NOISE_MAX = 1.0

#: [EN] The two calibration scopes stored in ``tuned_scope``.
#: [ZH] ``tuned_scope`` 中保存的两种标定范围。
TUNED_SCOPES = ("short", "long")

#: [EN] Keys of the hardware fingerprint stored next to a tuning result. The order is
#: [EN] the order in which a mismatch is reported.
#: [ZH] 随调优结果一起保存的硬件指纹键。顺序即差异项的报出顺序。
FINGERPRINT_KEYS = (
    "model",
    "model_bytes",
    "n_layer",
    "n_embd",
    "physical_cores",
    "logical_cores",
    "cpu_brand",
)


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """
    [EN]
    Coerce ``value`` to an integer inside ``[low, high]``. A value that cannot be read
    as an integer falls back to ``default`` (which is itself clamped): a hand-edited or
    partially written configuration must degrade to a safe number, never raise.

    [ZH]
    把 ``value`` 转换成落在 ``[low, high]`` 内的整数。无法读成整数的取值回退到
    ``default``（其本身也会被钳制）：手改或写到一半的配置必须退化成安全数值，绝不抛异常。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)
    if number < low:
        return int(low)
    if number > high:
        return int(high)
    return number


def clamp_float(value: Any, low: float, high: float, default: float) -> float:
    """
    [EN]
    Coerce ``value`` to a finite float inside ``[low, high]``. Unreadable values and
    ``nan`` / ``inf`` fall back to ``default`` (itself clamped): the calibration
    policy is a measurement knob, so a broken number must never abort a run.

    [ZH]
    把 ``value`` 转换成落在 ``[low, high]`` 内的有限浮点数。无法读取的值与 ``nan`` /
    ``inf`` 回退到 ``default``（其自身也会被钳制）：校准策略是测量参数，写坏的数字绝不
    应当中止一次运行。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if number != number or number in (float("inf"), float("-inf")):
        number = float(default)
    if number < low:
        return float(low)
    if number > high:
        return float(high)
    return float(number)


def normalize_tuned_scope(value: Any) -> str:
    """
    [EN]
    Normalise the stored calibration scope. Anything unknown becomes ``""`` ("never
    calibrated"), never ``long``: claiming a long-scope calibration that was not
    performed is exactly the over-claim this field exists to prevent.

    [ZH]
    归一化已保存的标定范围。未知取值一律变 ``""``（"从未标定"），绝不变成 ``long``：
    声称做过一次并不存在的长文本标定，正是本字段要防止的过度声明。
    """
    key = str(value or "").strip().lower()
    return key if key in TUNED_SCOPES else ""


def scope_for_rung(guard_rung: int, long_scope_min_rung: int) -> str:
    """
    [EN]
    ``long`` when the guard rung reaches ``long_scope_min_rung`` tokens, else ``short``.
    A short-scope result is only valid for short prompts, so it may be recorded but
    must not rewrite the thread counts.

    [ZH]
    守卫档达到 ``long_scope_min_rung`` token 时为 ``long``，否则为 ``short``。短文本
    范围的结果只对短 prompt 有效，因此可以记录，但不得改写线程数。
    """
    try:
        reach = int(guard_rung) >= int(long_scope_min_rung)
    except (TypeError, ValueError):
        reach = False
    return "long" if reach else "short"


def cpu_brand() -> str:
    """
    [EN]
    Best-effort CPU brand string. Windows exposes ``PROCESSOR_IDENTIFIER``, POSIX
    systems go through :func:`platform.processor`; an empty string is a valid answer
    and simply means "not comparable", never a reason to raise.

    [ZH]
    尽力取得 CPU 品牌字符串。Windows 提供 ``PROCESSOR_IDENTIFIER``，POSIX 系统走
    :func:`platform.processor`；空串是合法答案，只表示"不可比较"，绝不因此抛异常。
    """
    for name in ("PROCESSOR_IDENTIFIER", "PROCESSOR_NAME"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    try:
        return str(platform.processor() or "").strip()
    except Exception:  # noqa: BLE001 - a brand string is informational only
        return ""


def _gguf_shape(model_path: str) -> Dict[str, int]:
    """
    [EN] Read ``n_layer`` / ``n_embd`` from the GGUF header; unknown on any failure.
    [ZH] 从 GGUF 头读取 ``n_layer`` / ``n_embd``；任何失败都视为未知。
    """
    try:
        from gguf_meta import read_model_metadata  # local import: optional at runtime

        meta = read_model_metadata(model_path)
    except Exception:  # noqa: BLE001 - a partial fingerprint is still useful
        return {}

    def as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "n_layer": as_int(getattr(meta, "n_layers", 0)),
        "n_embd": as_int(getattr(meta, "n_embd", 0)),
    }


def hardware_fingerprint(
    model_path: str = "",
    physical_cores: Optional[int] = None,
    logical_cores: Optional[int] = None,
    cpu_brand_value: Optional[str] = None,
    model_bytes: Optional[int] = None,
    n_layer: Optional[int] = None,
    n_embd: Optional[int] = None,
) -> Dict[str, Any]:
    """
    [EN]
    Build the hardware fingerprint a tuning result is bound to: ``model`` /
    ``model_bytes`` / ``n_layer`` / ``n_embd`` / ``physical_cores`` /
    ``logical_cores`` / ``cpu_brand``.

    Every argument is optional: values that are supplied are used verbatim (that is
    how the tests build a deterministic fingerprint without a real model), values
    that are not supplied are probed, and a probe that fails yields ``0`` / ``""``
    rather than an exception. A fingerprint is diagnostic evidence, never a
    precondition for anything.

    [ZH]
    构造调优结果所绑定的硬件指纹：``model`` / ``model_bytes`` / ``n_layer`` /
    ``n_embd`` / ``physical_cores`` / ``logical_cores`` / ``cpu_brand``。

    全部参数可选：显式给出的值原样采用（测试因此无需真实模型即可构造确定性指纹），
    未给出的值才去探测，探测失败得到 ``0`` / ``""`` 而不是异常。指纹是诊断证据，
    绝不是任何功能的前提。
    """
    path = str(model_path or "").strip()
    physical = physical_cores
    logical = logical_cores
    brand = cpu_brand_value
    if physical is None or logical is None:
        try:
            import hardware_profiler  # local import: probing is optional

            profile = hardware_profiler.probe()
            if physical is None:
                physical = int(profile.cpu.physical_cores or 0)
            if logical is None:
                logical = int(profile.cpu.logical_cores or 0)
        except Exception:  # noqa: BLE001 - an incomplete fingerprint is enough
            if logical is None:
                logical = int(os.cpu_count() or 0)
    if physical is None:
        physical = 0
    if logical is None:
        logical = 0
    if brand is None:
        brand = cpu_brand()

    size = 0 if model_bytes is None else int(model_bytes)
    layers = 0 if n_layer is None else int(n_layer)
    embd = 0 if n_embd is None else int(n_embd)
    if path:
        if model_bytes is None:
            try:
                size = int(os.path.getsize(path))
            except OSError:
                size = 0
        if n_layer is None or n_embd is None:
            shape = _gguf_shape(path)
            if n_layer is None:
                layers = int(shape.get("n_layer", 0))
            if n_embd is None:
                embd = int(shape.get("n_embd", 0))
    return {
        "model": os.path.basename(path) if path else "",
        "model_bytes": int(size),
        "n_layer": int(layers),
        "n_embd": int(embd),
        "physical_cores": int(physical),
        "logical_cores": int(logical),
        "cpu_brand": str(brand or ""),
    }


def fingerprint_diff(stored: Any, current: Any) -> List[str]:
    """
    [EN]
    Return the fingerprint keys whose values differ, in ``FINGERPRINT_KEYS`` order.

    A key that is unknown on either side (absent, empty string, or ``0`` -- the
    fingerprint's "not probed" value) is skipped instead of reported as a mismatch:
    claiming a machine changed when it was merely not measured would be a false
    alarm, and this flag only ever *annotates* a result.

    [ZH]
    按 ``FINGERPRINT_KEYS`` 顺序返回取值不同的指纹键。

    任一侧未知的键（缺失、空串或 ``0`` —— 指纹里的"未探测"取值）直接跳过而**不**报为
    不符：把"没测到"说成"机器变了"是误报，而本标记只用于**标注**结果。
    """
    if not isinstance(stored, dict) or not stored:
        return []
    if not isinstance(current, dict) or not current:
        return []
    diff: List[str] = []
    for key in FINGERPRINT_KEYS:
        if key not in stored:
            continue
        want = stored.get(key)
        have = current.get(key)
        if _fingerprint_unknown(want) or _fingerprint_unknown(have):
            continue
        if isinstance(want, bool) or isinstance(have, bool):
            if bool(want) != bool(have):
                diff.append(key)
            continue
        if isinstance(want, (int, float)) or isinstance(have, (int, float)):
            try:
                if int(want) != int(have):
                    diff.append(key)
            except (TypeError, ValueError):
                if str(want).strip() != str(have).strip():
                    diff.append(key)
            continue
        if str(want).strip() != str(have).strip():
            diff.append(key)
    return diff


def _fingerprint_unknown(value: Any) -> bool:
    """
    [EN] True for the fingerprint's "not probed" placeholders.
    [ZH] 指纹里的"未探测"占位值返回真。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return False


DEFAULTS: Dict[str, Any] = {
    # [EN] Threads handed to llama.cpp. A value <= 0 means "derive from the physical
    # [EN] core count on this machine"; the tuner always writes a concrete number.
    # [ZH] 交给 llama.cpp 的线程数。取值 <= 0 表示"按本机物理核数推导"；
    # [ZH] 调优模块始终写入具体数值。
    "n_threads": 0,
    # [EN] Prompt prefill batch size and the micro-batch used by the compute buffer.
    # [ZH] Prompt 预填充的批大小，以及计算缓冲使用的微批大小。
    "n_batch": 512,
    "n_ubatch": 512,
    # [EN] Dedicated thread count for prefill (prompt processing). ``0`` = automatic,
    # [EN] derived as ``max(1, resolve_n_threads() - 2)`` so the UI thread keeps a core;
    # [EN] a negative value follows ``n_threads`` exactly. Prefill is the phase that
    # [EN] pins every core inside a spinning ggml barrier, so it gets a smaller pool of
    # [EN] its own instead of starving the interface thread.
    # [ZH] 预填充（prompt 处理）专用线程数。``0`` = 自动，按
    # [ZH] ``max(1, resolve_n_threads() - 2)`` 推导，给界面线程留出核心；负值严格跟随
    # [ZH] ``n_threads``。预填充是让所有核心在 ggml 自旋屏障里吃满的阶段，因此单独给它
    # [ZH] 一组更小的线程池，而不是让界面线程抢不到时间片。
    "n_threads_batch": 0,
    # [EN] Tokens per prefill chunk: ``0`` = follow ``n_batch``; otherwise clamped to
    # [EN] ``[32, n_batch]``. Chunk size changes neither the total work nor the speed,
    # [EN] only the granularity of progress reporting and of cancellation.
    # [ZH] 预填充每块 token 数：``0`` = 跟随 ``n_batch``；否则钳制在 ``[32, n_batch]``。
    # [ZH] 分块大小既不改变总计算量也不改变速度，只影响进度上报与可中断性的粒度。
    "prefill_chunk_tokens": 0,
    # [EN] Milliseconds yielded between prefill chunks so the rest of the system keeps
    # [EN] its time slice.
    # [ZH] 预填充分块之间让出 CPU 的毫秒数，让系统其余部分仍能拿到时间片。
    "prefill_yield_ms": 8,
    # [EN] No-progress watchdog threshold in seconds. Only armed while prefill runs;
    # [EN] reaching it aborts with an actionable message instead of appearing hung.
    # [ZH] 无进展看门狗的阈值（秒）。仅在预填充运行期间生效；达到阈值即以可操作提示
    # [ZH] 中止，而不是看起来像卡死。
    "prefill_stall_seconds": 45,
    # [EN] Force the model and the KV cache into physical RAM so Windows cannot page
    # [EN] them out to the swap file (the single largest cause of long-document stalls).
    # [ZH] 强制将模型与 KV Cache 锁定在物理内存，防止 Windows 将其换出到虚拟内存
    # [ZH] （长文档卡顿最主要的原因）。
    "use_mlock": True,
    # [EN] Memory-mapped model loading. Off by default because mlock already pins the
    # [EN] pages and mmap would defeat it on some Windows builds.
    # [ZH] 内存映射加载模型。默认关闭：mlock 已经锁定了页面，在部分 Windows 构建上
    # [ZH] mmap 反而会削弱锁定效果。
    "use_mmap": False,
    # [EN] FlashAttention acceleration inside llama.cpp.
    # [ZH] llama.cpp 内部的 FlashAttention 加速开关。
    "flash_attn": True,
    # [EN] KV-cache quantisation for keys and values. Fixed at q8_0: it halves the
    # [EN] long-context memory footprint against f16 and is the Reader's hard baseline.
    # [ZH] Key/Value 的 KV Cache 量化。固定为 q8_0：相对 f16 将长上下文内存占用减半，
    # [ZH] 是 Reader 的硬性基线。
    "type_k": "q8_0",
    "type_v": "q8_0",
    # [EN] Layer count offloaded to an accelerator. The Reader targets CPU-only
    # [EN] inference, so the shipped value is 0.
    # [ZH] 卸载到加速器的层数。Reader 以纯 CPU 推理为目标，因此随包取值为 0。
    "n_gpu_layers": 0,
    # [EN] Initial context window and the hard ceiling of the document-driven
    # [EN] allocation.
    # [ZH] 初始上下文窗口，以及文档驱动分配的硬上限。
    "n_ctx_default": 2048,
    "n_ctx_max": 131072,
    # [EN] RoPE scaling: the switch that lets a model accept a window larger than the
    # [EN] one it was trained for. ``none`` keeps the model's own behaviour; ``linear``
    # [EN] and ``yarn`` extend it. ``scale`` is the extension factor (2 = twice the
    # [EN] original), ``freq_scale`` is its reciprocal and is derived when left at 0,
    # [EN] and ``orig_ctx`` is the model's original window (0 = read from the file).
    # [ZH] RoPE 缩放：让模型接受比训练窗口更大的上下文的开关。``none`` 保持模型自身
    # [ZH] 行为；``linear`` 与 ``yarn`` 用于扩展。``scale`` 是扩展倍数（2 = 两倍），
    # [ZH] ``freq_scale`` 是其倒数、留 0 时自动推导，``orig_ctx`` 是模型原始窗口
    # [ZH] （0 = 从模型文件读取）。
    "rope_type": "none",
    "rope_scale": 1.0,
    "rope_freq_scale": 1.0,
    "rope_orig_ctx": 0,
    # [EN] Token budget reserved for the answer, plus the safety margin added on top of
    # [EN] the document body when the window is allocated.
    # [ZH] 为回答预留的 token 预算，以及分配窗口时在文档正文之外追加的安全余量。
    "output_token_budget": 4096,
    "context_safety_margin": 512,
    # [EN] Auto-tuning policy (v2): the fixed long-text workloads, the trial grid sizes,
    # [EN] the improvement threshold below which a candidate is rejected as noise, the
    # [EN] decode-regression / responsiveness gates, and the wall-clock budget.
    # [EN] ``prompt_tokens`` is the legacy key and is honoured as ``prompt_tokens_short``.
    # [ZH] 自动调优策略（v2）：固定的长文本负载档位、候选网格规模、判定"候选只是噪声"的
    # [ZH] 提升阈值、decode 回退与响应性闸门，以及墙钟预算。``prompt_tokens`` 是旧键，
    # [ZH] 按 ``prompt_tokens_short`` 兼容处理。
    "tuning": {
        # [EN] Legacy single-workload key (pre-v2 files only carry this one).
        # [ZH] 旧的单档负载键（v2 之前的文件只有它）。
        "prompt_tokens": 512,
        "prompt_tokens_short": 512,
        "prompt_tokens_long": 4096,
        "prompt_tokens_xlong": 16384,
        "gen_tokens": 128,
        "repeats": 5,
        "min_improvement_ratio": 0.03,
        "decode_regression_ratio": 0.02,
        "responsiveness_p95_ms": 50,
        "max_trials": 8,
        "max_seconds": 900,
        # [EN] Step 1.1 load adaptation: the main rung is the largest one whose estimated
        # [EN] round stays within ``2 * primary_target_seconds``; the long-text guard is
        # [EN] measured at ``min(2 * main rung, 16384)`` and vetoes a candidate that loses
        # [EN] more than ``long_guard_ratio`` there. ``long_guard_seconds`` is the guard's
        # [EN] own sub-budget; running out of it is reported, never passed off as a pass.
        # [ZH] 步 1.1 负载自适应：主档取"估算单轮时长不超过 ``2 * primary_target_seconds``"
        # [ZH] 的最大档；长文本守卫在 ``min(2 × 主档, 16384)`` 上测量，守卫档退化超过
        # [ZH] ``long_guard_ratio`` 的候选被否决。``long_guard_seconds`` 是守卫自己的子预算；
        # [ZH] 预算不足会被如实上报，绝不冒充通过。
        "primary_target_seconds": 10,
        "long_guard_ratio": 0.02,
        "long_guard_seconds": 300,
        # [EN] A calibration whose guard rung reaches this many tokens counts as
        # [EN] long-scope and may rewrite the thread counts; anything below is
        # [EN] short-scope and is recorded for reference only.
        # [ZH] 守卫档达到该 token 数的标定视为长文本范围，可以改写线程数；低于它则是短
        # [ZH] 文本范围，只作参考记录。
        "long_scope_min_rung": 8192,
        # [EN] Environment-noise threshold: two identical short probes are compared, and
        # [EN] a relative difference above this ratio aborts the run with
        # [EN] ``reason=noisy_environment`` before any candidate is measured.
        # [ZH] 环境噪声阈值：先跑两次完全相同的短探测并比较，相对差超过该比例时在测量任何
        # [ZH] 候选之前就以 ``reason=noisy_environment`` 收尾。
        "noise_probe_ratio": 0.03,
    },
    # [EN] Bookkeeping written by the tuner: when it last ran and what it measured.
    # [ZH] 由调优模块写入的记账信息：最后一次运行时间与实测结果。
    "tuned_at": "",
    "tuned_model": "",
    #: [EN] Calibration scope of the stored result: ``short`` or ``long`` (empty = never
    #: [EN] calibrated). Only a long-scope result may rewrite the thread counts.
    #: [ZH] 已保存结果的标定范围：``short`` 或 ``long``（空 = 从未标定）。只有长文本范围
    #: [ZH] 的结果可以改写线程数。
    "tuned_scope": "",
    "tuned_baseline": {},
    "tuned_best": {},
    # [EN] Hardware fingerprint the tuning result was measured on. A mismatch marks the
    # [EN] result stale; it never triggers an automatic re-tune.
    # [ZH] 调优结果所测得的硬件指纹。不符时把结果标为 stale，绝不自动触发重调。
    "tuned_fingerprint": {},
}


def default_path() -> str:
    """
    [EN]
    Resolve the configuration path with the documented three-step priority.
    [ZH]
    按文档约定的三步优先级解析配置文件路径。
    """
    override = str(os.environ.get(ENV_OVERRIDE) or "").strip()
    if override:
        return os.path.abspath(override)
    try:
        from config import program_dir, state_dir  # local import: avoid an import cycle

        state = state_dir()
        candidate = os.path.join(state, FILENAME)
        # [EN] Prefer a directory that already holds the file; otherwise fall through to
        # [EN] the program directory so a development checkout reads its own file.
        # [ZH] 优先使用已经存在该文件的目录；否则退回程序目录，使开发检出读取自己的文件。
        if os.path.isfile(candidate):
            return candidate
        legacy = os.path.join(program_dir(), FILENAME)
        if os.path.isfile(legacy):
            return legacy
        return candidate
    except Exception:  # noqa: BLE001 - path resolution must never break start-up
        return os.path.abspath(os.path.join(os.path.dirname(__file__), FILENAME))


@dataclass
class InferenceConfig:
    """
    [EN]
    Strongly typed view of ``inference_config.json``.

    Unknown keys are preserved in :attr:`extra` so that a file written by a newer
    release survives a round trip through this version instead of being silently
    truncated.

    [ZH]
    ``inference_config.json`` 的强类型视图。

    未知键保留在 :attr:`extra` 中：由更新版本写出的文件经本版本读写一轮后不会被
    静默截断。
    """

    n_threads: int = 0
    n_batch: int = 512
    n_ubatch: int = 512
    # [EN] Prefill (prompt-processing) tuning block; semantics live in ``DEFAULTS``.
    # [ZH] 预填充（prompt 处理）调优参数块；语义见上方 ``DEFAULTS``。
    n_threads_batch: int = 0
    prefill_chunk_tokens: int = 0
    prefill_yield_ms: int = 8
    prefill_stall_seconds: int = 45
    use_mlock: bool = True
    use_mmap: bool = False
    flash_attn: bool = True
    type_k: str = "q8_0"
    type_v: str = "q8_0"
    n_gpu_layers: int = 0
    n_ctx_default: int = 2048
    n_ctx_max: int = 131072
    # [EN] RoPE scaling block (see ``DEFAULTS`` above for the semantics).
    # [ZH] RoPE 缩放参数块（语义见上方 ``DEFAULTS``）。
    rope_type: str = "none"
    rope_scale: float = 1.0
    rope_freq_scale: float = 1.0
    rope_orig_ctx: int = 0
    output_token_budget: int = 4096
    context_safety_margin: int = 512
    tuning: Dict[str, Any] = field(default_factory=dict)
    tuned_at: str = ""
    tuned_model: str = ""
    #: [EN] Scope of the stored calibration: ``short`` / ``long`` (see ``TUNED_SCOPES``).
    #: [ZH] 已保存标定的范围：``short`` / ``long``（见 ``TUNED_SCOPES``）。
    tuned_scope: str = ""
    tuned_baseline: Dict[str, Any] = field(default_factory=dict)
    tuned_best: Dict[str, Any] = field(default_factory=dict)
    #: [EN] Hardware fingerprint recorded by the tuner; see :func:`hardware_fingerprint`.
    #: [ZH] 调优模块记录的硬件指纹；见 :func:`hardware_fingerprint`。
    tuned_fingerprint: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    #: [EN] Absolute path this object was loaded from (empty for in-memory objects).
    #: [ZH] 本对象加载自的绝对路径（内存对象为空串）。
    path: str = ""

    # [EN] ---------- Loading and saving ----------
    # [ZH] ---------- 加载与保存 ----------

    @classmethod
    def load(cls, path: Optional[str] = None) -> "InferenceConfig":
        """
        [EN]
        Read the configuration file. A missing or malformed file never raises: the
        defaults are used and the problem is logged, because a broken tuning file must
        not prevent the reader from opening a document.

        [ZH]
        读取配置文件。文件缺失或格式非法都不抛异常：改用默认值并记录日志，因为一个
        写坏的调优文件绝不能让阅读器无法打开文档。
        """
        target = os.path.abspath(path) if path else default_path()
        data: Dict[str, Any] = {}
        if os.path.isfile(target):
            try:
                with open(target, "r", encoding="utf-8-sig") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    log.warning("推理配置文件顶层不是对象，已忽略: %s", target)
            except Exception as exc:  # noqa: BLE001 - fall back to defaults
                log.warning("推理配置文件读取失败，改用默认值: %s (%s)", target, exc)
        else:
            log.info("推理配置文件不存在，使用默认值: %s", target)
        return cls.from_dict(data, path=target)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: str = "") -> "InferenceConfig":
        """
        [EN] Build an instance from a plain mapping, filling gaps with the defaults.
        [ZH] 由普通映射构造实例，缺失项以默认值补齐。
        """
        merged = copy.deepcopy(DEFAULTS)
        for key, value in (data or {}).items():
            if key == "tuning" and isinstance(value, dict):
                merged["tuning"].update(value)
            else:
                merged[key] = value
        # [EN] v2 compatibility: a file (or a caller) that only knows the legacy
        # [EN] ``prompt_tokens`` keeps that workload as the *short* tier instead of being
        # [EN] silently moved to the new default.
        # [ZH] v2 兼容：只认识旧键 ``prompt_tokens`` 的文件（或调用方）把该负载当作**短**
        # [ZH] 档继续使用，而不是被悄悄改成新的默认值。
        legacy_tuning = (data or {}).get("tuning")
        if isinstance(legacy_tuning, dict):
            if "prompt_tokens" in legacy_tuning and "prompt_tokens_short" not in legacy_tuning:
                merged["tuning"]["prompt_tokens_short"] = legacy_tuning.get("prompt_tokens")

        known = {name for name in cls.__dataclass_fields__ if name not in ("extra", "path")}
        extra = {k: v for k, v in merged.items() if k not in known}

        def as_int(key: str) -> int:
            try:
                return int(merged.get(key) or 0)
            except (TypeError, ValueError):
                return int(DEFAULTS[key])

        def as_bool(key: str) -> bool:
            value = merged.get(key)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        def as_float(key: str) -> float:
            try:
                return float(merged.get(key) or DEFAULTS[key])
            except (TypeError, ValueError):
                return float(DEFAULTS[key])

        tuning = merged.get("tuning")
        # [EN] Batch size is resolved first because the prefill chunk ceiling is
        # [EN] expressed in terms of it: clamping against a stale value would let a
        # [EN] chunk exceed the batch actually used by the compute buffer.
        # [ZH] 先解析批大小：预填充分块的上限以它为基准，若按旧值钳制，分块可能超过
        # [ZH] 计算缓冲实际使用的批大小。
        n_batch = max(1, as_int("n_batch"))
        chunk = clamp_int(merged.get("prefill_chunk_tokens"), 0, 2 ** 31 - 1, 0)
        if chunk > 0:
            # [EN] Either "follow n_batch" (0) or a real chunk inside the meaningful band.
            # [ZH] 要么"跟随 n_batch"（0），要么是落在有意义区间内的真实分块。
            chunk = min(n_batch, max(PREFILL_CHUNK_MIN, chunk))
        return cls(
            n_threads=as_int("n_threads"),
            n_batch=n_batch,
            n_ubatch=max(1, as_int("n_ubatch")),
            n_threads_batch=clamp_int(merged.get("n_threads_batch"), PREFILL_THREADS_MIN,
                                      PREFILL_THREADS_MAX, DEFAULTS["n_threads_batch"]),
            prefill_chunk_tokens=chunk,
            prefill_yield_ms=clamp_int(merged.get("prefill_yield_ms"), PREFILL_YIELD_MIN,
                                       PREFILL_YIELD_MAX, DEFAULTS["prefill_yield_ms"]),
            prefill_stall_seconds=clamp_int(merged.get("prefill_stall_seconds"),
                                            PREFILL_STALL_MIN, PREFILL_STALL_MAX,
                                            DEFAULTS["prefill_stall_seconds"]),
            use_mlock=as_bool("use_mlock"),
            use_mmap=as_bool("use_mmap"),
            flash_attn=as_bool("flash_attn"),
            type_k=str(merged.get("type_k") or "q8_0").strip().lower(),
            type_v=str(merged.get("type_v") or "q8_0").strip().lower(),
            n_gpu_layers=as_int("n_gpu_layers"),
            n_ctx_default=max(1, as_int("n_ctx_default")),
            n_ctx_max=max(1, as_int("n_ctx_max")),
            rope_type=normalize_rope_type(merged.get("rope_type")),
            rope_scale=max(1.0, as_float("rope_scale")),
            rope_freq_scale=min(1.0, max(0.0, as_float("rope_freq_scale"))),
            rope_orig_ctx=max(0, as_int("rope_orig_ctx")),
            output_token_budget=max(0, as_int("output_token_budget")),
            context_safety_margin=max(0, as_int("context_safety_margin")),
            tuning=tuning if isinstance(tuning, dict) else {},
            tuned_at=str(merged.get("tuned_at") or ""),
            tuned_model=str(merged.get("tuned_model") or ""),
            tuned_scope=normalize_tuned_scope(merged.get("tuned_scope")),
            tuned_baseline=(merged.get("tuned_baseline")
                            if isinstance(merged.get("tuned_baseline"), dict) else {}),
            tuned_best=(merged.get("tuned_best")
                        if isinstance(merged.get("tuned_best"), dict) else {}),
            tuned_fingerprint=(merged.get("tuned_fingerprint")
                               if isinstance(merged.get("tuned_fingerprint"), dict) else {}),
            extra=extra,
            path=path,
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        [EN] Serialise back to a JSON-compatible mapping (unknown keys preserved).
        [ZH] 序列化为与 JSON 兼容的映射（未知键保留）。
        """
        data: Dict[str, Any] = dict(self.extra)
        data.update({
            "n_threads": int(self.n_threads),
            "n_batch": int(self.n_batch),
            "n_ubatch": int(self.n_ubatch),
            "n_threads_batch": int(self.n_threads_batch),
            "prefill_chunk_tokens": int(self.prefill_chunk_tokens),
            "prefill_yield_ms": int(self.prefill_yield_ms),
            "prefill_stall_seconds": int(self.prefill_stall_seconds),
            "use_mlock": bool(self.use_mlock),
            "use_mmap": bool(self.use_mmap),
            "flash_attn": bool(self.flash_attn),
            "type_k": str(self.type_k),
            "type_v": str(self.type_v),
            "n_gpu_layers": int(self.n_gpu_layers),
            "n_ctx_default": int(self.n_ctx_default),
            "n_ctx_max": int(self.n_ctx_max),
            "rope_type": str(self.rope_type),
            "rope_scale": float(self.rope_scale),
            "rope_freq_scale": float(self.rope_freq_scale),
            "rope_orig_ctx": int(self.rope_orig_ctx),
            "output_token_budget": int(self.output_token_budget),
            "context_safety_margin": int(self.context_safety_margin),
            "tuning": dict(self.tuning),
            "tuned_at": str(self.tuned_at),
            "tuned_model": str(self.tuned_model),
            "tuned_scope": str(self.tuned_scope),
            "tuned_baseline": dict(self.tuned_baseline),
            "tuned_best": dict(self.tuned_best),
            "tuned_fingerprint": dict(self.tuned_fingerprint),
        })
        return data

    def save(self, path: Optional[str] = None) -> str:
        """
        [EN]
        Write the configuration back, creating the parent directory when needed.
        Returns the path actually written. A write failure is logged and returned as
        an empty string: the tuning result is a performance optimisation, never a
        precondition for reading.

        [ZH]
        写回配置，必要时创建父目录。返回实际写入的路径。写入失败只记录日志并返回空串：
        调优结果属于性能优化，绝不是阅读的前提条件。
        """
        target = os.path.abspath(path or self.path or default_path())
        try:
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            self.path = target
            return target
        except Exception as exc:  # noqa: BLE001 - never break a reading session
            log.warning("推理配置写入失败: %s (%s)", target, exc)
            return ""

    # [EN] ---------- Resolution helpers ----------
    # [ZH] ---------- 解析辅助 ----------

    def resolve_n_threads(self) -> int:
        """
        [EN]
        Concrete thread count: the configured value, or -- when it is <= 0 -- the
        physical core count reported by the hardware profiler.

        [ZH]
        具体的线程数：配置值；当配置值 <= 0 时取硬件探测器报告的物理核数。
        """
        if int(self.n_threads) > 0:
            return int(self.n_threads)
        try:
            import hardware_profiler

            cores = int(hardware_profiler.probe().cpu.physical_cores or 0)
        except Exception:  # noqa: BLE001 - a probe failure must not stop loading
            cores = 0
        if cores > 0:
            return cores
        return max(1, (os.cpu_count() or 2) // 2)

    def resolve_n_threads_batch(self, logical_cores: int = 0) -> int:
        """
        [EN]
        Concrete thread count for prefill, never below 1.

        ``n_threads_batch > 0`` is used as configured; a negative value follows
        :meth:`resolve_n_threads` exactly; ``0`` (the default) is automatic and leaves
        two cores free for the interface thread. ``logical_cores`` lets a caller -- and
        the tests -- supply an already known core count instead of probing the machine;
        a value ``<= 0`` means "probe it".

        The return value is always ``>= 1`` so that ``llama_kwargs()`` can never hand a
        zero to the native layer and make it guess.

        [ZH]
        预填充的具体线程数，恒不小于 1。

        ``n_threads_batch > 0`` 按配置取用；负值严格跟随 :meth:`resolve_n_threads`；
        ``0``（默认）为自动，给界面线程留出两个核心。``logical_cores`` 供调用方与测试
        传入已知核数而无需探测本机；取值 ``<= 0`` 表示"现场探测"。

        返回值恒 ``>= 1``，保证 ``llama_kwargs()`` 绝不会把 0 丢给原生层让它自己猜。
        """
        configured = int(self.n_threads_batch)
        if configured > 0:
            return int(min(PREFILL_THREADS_MAX, configured))
        base = int(logical_cores) if int(logical_cores or 0) > 0 else self.resolve_n_threads()
        if configured < 0:
            # [EN] Explicitly "follow the decode threads": no headroom is subtracted.
            # [ZH] 显式"跟随 decode 线程"：不扣除任何余量。
            return max(1, base)
        return max(1, base - 2)

    def resolve_prefill_chunk_tokens(self) -> int:
        """
        [EN]
        Concrete prefill chunk size: ``0`` follows ``n_batch``, any other value is
        clamped into ``[32, n_batch]``. The batch size itself is clamped here too so a
        directly constructed object cannot produce a chunk larger than the batch.
        [ZH]
        预填充的具体分块大小：``0`` 跟随 ``n_batch``，其余取值钳制在 ``[32, n_batch]``。
        这里同样对批大小做钳制，使直接构造的对象也不可能产出大于批的分块。
        """
        n_batch = max(1, int(self.n_batch))
        chunk = int(self.prefill_chunk_tokens)
        if chunk <= 0:
            return n_batch
        return int(min(n_batch, max(PREFILL_CHUNK_MIN, chunk)))

    def resolve_prefill_yield_ms(self) -> int:
        """
        [EN] Inter-chunk yield in milliseconds, clamped into ``[0, 50]``.
        [ZH] 块间让核毫秒数，钳制在 ``[0, 50]``。
        """
        return clamp_int(self.prefill_yield_ms, PREFILL_YIELD_MIN, PREFILL_YIELD_MAX,
                         DEFAULTS["prefill_yield_ms"])

    def resolve_prefill_stall_seconds(self) -> int:
        """
        [EN] No-progress watchdog threshold in seconds, clamped into ``[10, 300]``.
        [ZH] 无进展看门狗阈值（秒），钳制在 ``[10, 300]``。
        """
        return clamp_int(self.prefill_stall_seconds, PREFILL_STALL_MIN, PREFILL_STALL_MAX,
                         DEFAULTS["prefill_stall_seconds"])

    def _tuning_raw(self, key: str) -> Any:
        """
        [EN] Raw value of one ``tuning`` key, or ``None`` when the block is unusable.
        [ZH] ``tuning`` 块中某个键的原始取值；块不可用时为 ``None``。
        """
        if not isinstance(self.tuning, dict):
            return None
        return self.tuning.get(key)

    def tuning_prompt_tokens(self, size: str = "short") -> int:
        """
        [EN]
        Prefill workload of one tier, clamped into the meaningful band.

        ``short`` / ``long`` / ``xlong`` map onto ``prompt_tokens_short`` /
        ``prompt_tokens_long`` / ``prompt_tokens_xlong``. The short tier also accepts the
        legacy ``prompt_tokens`` key, which is how a pre-v2 configuration keeps its
        behaviour instead of raising or silently changing workload.

        [ZH]
        某一档预填充负载，钳制在有意义的区间内。

        ``short`` / ``long`` / ``xlong`` 分别对应 ``prompt_tokens_short`` /
        ``prompt_tokens_long`` / ``prompt_tokens_xlong``。短档同时接受旧键
        ``prompt_tokens``：v2 之前的配置因此保持原有行为，既不抛异常也不悄悄换负载。
        """
        keys = {"short": "prompt_tokens_short", "long": "prompt_tokens_long",
                "xlong": "prompt_tokens_xlong"}
        key = keys.get(str(size or "short").strip().lower(), "prompt_tokens_short")
        default = int(DEFAULTS["tuning"].get(key, 512))
        raw = self._tuning_raw(key)
        if raw is None and key == "prompt_tokens_short":
            raw = self._tuning_raw("prompt_tokens")
        return clamp_int(raw, TUNING_PROMPT_MIN, TUNING_PROMPT_MAX, default)

    def tuning_gen_tokens(self) -> int:
        """
        [EN] Generated tokens per measured run, clamped into ``[1, 4096]``.
        [ZH] 每次测量生成的 token 数，钳制在 ``[1, 4096]``。
        """
        return clamp_int(self._tuning_raw("gen_tokens"), TUNING_GEN_MIN, TUNING_GEN_MAX,
                         int(DEFAULTS["tuning"]["gen_tokens"]))

    def tuning_repeats(self) -> int:
        """
        [EN] Repeats per configuration; the median is used, never the maximum.
        [ZH] 每组配置的重复次数；取中位数，绝不取最大值。
        """
        return clamp_int(self._tuning_raw("repeats"), TUNING_REPEATS_MIN,
                         TUNING_REPEATS_MAX, int(DEFAULTS["tuning"]["repeats"]))

    def tuning_max_trials(self) -> int:
        """
        [EN] Upper bound on the number of probes in one run (both levels together).
        [ZH] 单次运行试探数的上限（两级合计）。
        """
        return clamp_int(self._tuning_raw("max_trials"), TUNING_MAX_TRIALS_MIN,
                         TUNING_MAX_TRIALS_MAX, int(DEFAULTS["tuning"]["max_trials"]))

    def tuning_max_seconds(self) -> int:
        """
        [EN] Wall-clock budget of one run, clamped into ``[30, 86400]`` seconds.
        [ZH] 单次运行的墙钟预算，钳制在 ``[30, 86400]`` 秒。
        """
        return clamp_int(self._tuning_raw("max_seconds"), TUNING_MAX_SECONDS_MIN,
                         TUNING_MAX_SECONDS_MAX, int(DEFAULTS["tuning"]["max_seconds"]))

    def tuning_ratio(self, key: str, fallback: float) -> float:
        """
        [EN] A ratio from the ``tuning`` block, clamped into ``[0, 1]``; never raises.
        [ZH] ``tuning`` 块里的比例，钳制在 ``[0, 1]``；绝不抛异常。
        """
        return clamp_float(self._tuning_raw(key), TUNING_RATIO_MIN, TUNING_RATIO_MAX,
                           float(fallback))

    def tuning_p95_ms(self) -> float:
        """
        [EN] Responsiveness gate: the largest acceptable p95 wake deviation in ms.
        [ZH] 响应性闸门：可接受的 p95 唤醒偏差上限（毫秒）。
        """
        return clamp_float(self._tuning_raw("responsiveness_p95_ms"), TUNING_P95_MIN,
                           TUNING_P95_MAX,
                           float(DEFAULTS["tuning"]["responsiveness_p95_ms"]))

    def tuning_primary_target_seconds(self) -> float:
        """
        [EN]
        Target duration of one measured round. The main rung is the largest of
        ``{short, long, xlong}`` whose estimated round stays within twice this value.

        [ZH]
        单轮测量的目标时长。主档取 ``{short, long, xlong}`` 中"估算单轮时长不超过它的两倍"
        的最大档。
        """
        return clamp_float(self._tuning_raw("primary_target_seconds"),
                           TUNING_PRIMARY_TARGET_MIN, TUNING_PRIMARY_TARGET_MAX,
                           float(DEFAULTS["tuning"]["primary_target_seconds"]))

    def tuning_long_guard_ratio(self) -> float:
        """
        [EN]
        Long-text guard tolerance: losing more than this ratio of prefill speed at the
        guard rung vetoes the candidate, because a configuration that is fast only on
        short prompts is not an improvement.
        [ZH]
        长文本守卫容差：在守卫档预填充速度损失超过该比例的候选被否决，因为只在短 prompt 上
        快的配置不算改进。
        """
        return clamp_float(self._tuning_raw("long_guard_ratio"), TUNING_RATIO_MIN,
                           TUNING_RATIO_MAX,
                           float(DEFAULTS["tuning"]["long_guard_ratio"]))

    def tuning_long_guard_seconds(self) -> int:
        """
        [EN]
        Sub-budget of the long-text guard, in seconds. Running out of it is reported as
        ``skipped_budget`` instead of being treated as a pass.
        [ZH]
        长文本守卫的子预算（秒）。用尽时报告为 ``skipped_budget``，而不是当作通过。
        """
        return clamp_int(self._tuning_raw("long_guard_seconds"), TUNING_LONG_GUARD_MIN,
                         TUNING_LONG_GUARD_MAX,
                         int(DEFAULTS["tuning"]["long_guard_seconds"]))

    def tuning_long_scope_min_rung(self) -> int:
        """
        [EN]
        Guard-rung threshold at which a calibration becomes long-scope and is allowed
        to rewrite the thread counts.
        [ZH]
        标定成为长文本范围（并允许改写线程数）所需的守卫档阈值。
        """
        return clamp_int(self._tuning_raw("long_scope_min_rung"), TUNING_LONG_SCOPE_MIN,
                         TUNING_LONG_SCOPE_MAX,
                         int(DEFAULTS["tuning"]["long_scope_min_rung"]))

    def tuning_noise_probe_ratio(self) -> float:
        """
        [EN]
        Largest relative difference tolerated between two identical short probes. Above
        it the environment is declared unstable and the run stops *before* scanning
        candidates, because a noisy host cannot support a 3 % adoption threshold.

        [ZH]
        两次完全相同的短探测之间可容忍的最大相对差。超过即判定环境不稳，并在**扫描候选
        之前**停止，因为噪声主机撑不起 3% 的采纳门槛。
        """
        return clamp_float(self._tuning_raw("noise_probe_ratio"), TUNING_NOISE_MIN,
                           TUNING_NOISE_MAX,
                           float(DEFAULTS["tuning"]["noise_probe_ratio"]))

    def resolve_tuning(self) -> Dict[str, Any]:
        """
        [EN]
        The normalised tuning policy actually used by the calibrator: legacy keys
        mapped, missing keys defaulted, illegal values clamped. The raw block stays
        untouched in :attr:`tuning` so a round trip loses nothing.

        [ZH]
        校准器实际采用的归一化调优策略：旧键已映射、缺键已取默认、非法值已钳制。原始块
        仍原样保存在 :attr:`tuning` 中，因此往返不会丢失任何内容。
        """
        return {
            "repeats": self.tuning_repeats(),
            "min_improvement_ratio": self.tuning_ratio(
                "min_improvement_ratio", float(DEFAULTS["tuning"]["min_improvement_ratio"])),
            "decode_regression_ratio": self.tuning_ratio(
                "decode_regression_ratio",
                float(DEFAULTS["tuning"]["decode_regression_ratio"])),
            "responsiveness_p95_ms": self.tuning_p95_ms(),
            "prompt_tokens_short": self.tuning_prompt_tokens("short"),
            "prompt_tokens_long": self.tuning_prompt_tokens("long"),
            "prompt_tokens_xlong": self.tuning_prompt_tokens("xlong"),
            "gen_tokens": self.tuning_gen_tokens(),
            "max_trials": self.tuning_max_trials(),
            "max_seconds": self.tuning_max_seconds(),
            "primary_target_seconds": self.tuning_primary_target_seconds(),
            "long_guard_ratio": self.tuning_long_guard_ratio(),
            "long_guard_seconds": self.tuning_long_guard_seconds(),
            "long_scope_min_rung": self.tuning_long_scope_min_rung(),
            "noise_probe_ratio": self.tuning_noise_probe_ratio(),
        }

    def _tuned_model_path(self) -> str:
        """
        [EN]
        Best-effort path of the model the stored fingerprint refers to: looked up by
        name in the standard ``models`` directories, read-only and silent. An empty
        string means "not resolvable here", which simply skips the model keys.

        [ZH]
        尽力解析已保存指纹所指模型的路径：按文件名在标准 ``models`` 目录里只读查找，不
        打印任何东西。返回空串表示"此处无法解析"，此时跳过模型相关键。
        """
        stored = self.tuned_fingerprint if isinstance(self.tuned_fingerprint, dict) else {}
        name = os.path.basename(str(stored.get("model") or self.tuned_model or "").strip())
        if not name:
            return ""
        directories: List[str] = []
        try:
            import config as app_config  # local import: avoid an import cycle

            directories.append(os.path.join(app_config.program_dir(), "models"))
        except Exception:  # noqa: BLE001 - path discovery is optional
            pass
        directories.append(os.path.join(os.getcwd(), "models"))
        for directory in directories:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return ""

    def tuned_fingerprint_diff(self, model_path: str = "") -> List[str]:
        """
        [EN]
        Fingerprint keys that differ from the stored ones. Empty means "not usable to
        prove staleness" -- either nothing was stored or the value is unknown here.

        [ZH]
        与已保存指纹不同的键。空列表表示"不足以证明已失效" —— 要么从未保存过，要么此处
        无法获知该取值。
        """
        stored = self.tuned_fingerprint if isinstance(self.tuned_fingerprint, dict) else {}
        if not stored:
            return []
        path = str(model_path or "").strip() or self._tuned_model_path()
        return fingerprint_diff(stored, hardware_fingerprint(path))

    def tuned_is_stale(self, model_path: str = "") -> bool:
        """
        [EN]
        Whether the stored tuning result was measured on a different machine or model.
        Stale is an annotation only: nothing here re-runs the tuner.

        [ZH]
        已保存的调优结果是否测于另一台机器或另一个模型。stale 只是标注：这里绝不重跑调优。
        """
        return bool(self.tuned_fingerprint_diff(model_path))

    def tuning_int(self, key: str, fallback: int) -> int:
        """
        [EN] Read one integer from the ``tuning`` block with a safe fallback.
        [ZH] 从 ``tuning`` 块读取一个整数，异常时使用兜底值。
        """
        try:
            return int(self.tuning.get(key, fallback))
        except (TypeError, ValueError):
            return int(fallback)


    def tuning_float(self, key: str, fallback: float) -> float:
        """
        [EN] Read one float from the ``tuning`` block with a safe fallback.
        [ZH] 从 ``tuning`` 块读取一个浮点数，异常时使用兜底值。
        """
        try:
            return float(self.tuning.get(key, fallback))
        except (TypeError, ValueError):
            return float(fallback)

    def resolve_rope(self) -> Dict[str, Any]:
        """
        [EN]
        Effective RoPE scaling handed to the native runtime.

        ``scale`` is the human-facing extension factor (a window of 64K on a 32K model
        uses 2), while llama.cpp expects its reciprocal in ``rope_freq_scale``. When
        the reciprocal is left at 0 the native library keeps the model's own value,
        which is exactly what ``none`` needs. ``orig_ctx`` of 0 also means "read from
        the model file".

        [ZH]
        交给原生运行时的生效 RoPE 缩放参数。

        ``scale`` 是给人看的扩展倍数（32K 模型开 64K 窗口即 2 倍），而 llama.cpp 需要
        它的倒数 ``rope_freq_scale``。倒数留 0 时原生库沿用模型自带取值，这正是
        ``none`` 所需；``orig_ctx`` 为 0 同样表示"从模型文件读取"。
        """
        mode = normalize_rope_type(self.rope_type)
        if mode == "none":
            # [EN] A disabled mode must not leak a stale factor into the engine.
            # [ZH] 关闭状态下绝不能把残留的倍数带进引擎。
            return {"type": "none", "scale": 1.0, "freq_scale": 0.0, "orig_ctx": 0}
        scale = float(self.rope_scale or 1.0)
        if scale < 1.0:
            scale = 1.0
        # [EN] The extension factor is authoritative: 4x always means 1/4 frequency, even
        # [EN] when the stored frequency is stale (a hand-edited file, or a value left over
        # [EN] from a previous tier). A stored frequency is only honoured when no factor is
        # [EN] in play, and even then it stays inside the meaningful (0, 1] range.
        # [ZH] 扩展倍数是权威：4 倍永远对应 1/4 频率，即使存下来的频率已经过时（手改文件，
        # [ZH] 或上一次档位残留）。只有没有倍数时才采用存下来的频率，且仍限制在 (0, 1]。
        if scale > 1.0:
            freq = 1.0 / scale
        else:
            freq = float(self.rope_freq_scale or 0.0)
            if freq <= 0.0 or freq > 1.0:
                freq = 0.0
        return {
            "type": mode,
            "scale": round(scale, 4),
            "freq_scale": round(freq, 6),
            "orig_ctx": max(0, int(self.rope_orig_ctx or 0)),
        }

    def llama_kwargs(self) -> Dict[str, Any]:
        """
        [EN]
        The construction keyword arguments handed to ``llama_cpp.Llama``. ``n_ctx`` and
        ``model_path`` are supplied by the caller because they vary per document.

        [ZH]
        交给 ``llama_cpp.Llama`` 的构建关键字参数。``n_ctx`` 与 ``model_path`` 由调用方
        提供，因为它们随文档变化。
        """
        rope = self.resolve_rope()
        return {
            "n_threads": self.resolve_n_threads(),
            # [EN] Resolved, never 0: the native layer is told the final count instead of
            # [EN] being allowed to invent its own for the prefill phase.
            # [ZH] 解析后的最终值，绝不为 0：直接告诉原生层预填充阶段用多少线程，
            # [ZH] 而不是让它自己另猜一个。
            "n_threads_batch": self.resolve_n_threads_batch(),
            "n_batch": int(self.n_batch),
            "n_ubatch": int(self.n_ubatch),
            "n_gpu_layers": int(self.n_gpu_layers),
            "use_mlock": bool(self.use_mlock),
            "use_mmap": bool(self.use_mmap),
            "flash_attn": bool(self.flash_attn),
            "type_k": ggml_type_value(self.type_k),
            "type_v": ggml_type_value(self.type_v),
            "rope_type": rope["type"],
            "rope_scale": rope["scale"],
            "rope_freq_scale": rope["freq_scale"],
            "rope_orig_ctx": rope["orig_ctx"],
        }

    def describe(self, model_path: str = "") -> Dict[str, Any]:
        """
        [EN]
        Machine-readable snapshot for the protocol (``inference_config`` action and the
        ``ready`` line).

        ``tuning_resolved`` is the normalised v2 policy actually in force, and
        ``tuned_stale`` / ``tuned_fingerprint_diff`` annotate a stored tuning result
        whose hardware fingerprint no longer matches this host. ``model_path`` lets a
        caller that knows the active model include the model keys in that comparison;
        without it only the values this module can probe are compared.
        [ZH]
        供协议使用的机器可读快照（``inference_config`` 动作与 ``ready`` 行）。

        ``tuning_resolved`` 是实际生效的 v2 归一化策略；``tuned_stale`` /
        ``tuned_fingerprint_diff`` 标注已保存的调优结果其硬件指纹与本机不再相符。
        ``model_path`` 让知道当前模型的调用方把模型相关键也纳入比较；不提供时只比较本模块
        能自行探测到的取值。
        """
        return {
            "path": self.path,
            "n_threads": self.resolve_n_threads(),
            "n_threads_configured": int(self.n_threads),
            "n_threads_batch": self.resolve_n_threads_batch(),
            "n_threads_batch_configured": int(self.n_threads_batch),
            "prefill_chunk_tokens": self.resolve_prefill_chunk_tokens(),
            "prefill_chunk_configured": int(self.prefill_chunk_tokens),
            "prefill_yield_ms": self.resolve_prefill_yield_ms(),
            "prefill_stall_seconds": self.resolve_prefill_stall_seconds(),
            "n_batch": int(self.n_batch),
            "n_ubatch": int(self.n_ubatch),
            "kv_cache": str(self.type_k),
            "type_k": str(self.type_k),
            "type_v": str(self.type_v),
            "mlock": bool(self.use_mlock),
            "mmap": bool(self.use_mmap),
            "flash_attn": bool(self.flash_attn),
            "n_gpu_layers": int(self.n_gpu_layers),
            "n_ctx_default": int(self.n_ctx_default),
            "n_ctx_max": int(self.n_ctx_max),
            "rope": self.resolve_rope(),
            "output_token_budget": int(self.output_token_budget),
            "context_safety_margin": int(self.context_safety_margin),
            "tuned_at": str(self.tuned_at),
            "tuned_model": str(self.tuned_model),
            "tuned_scope": str(self.tuned_scope),
            "tuning": dict(self.tuning),
            "tuning_resolved": self.resolve_tuning(),
            "tuned_fingerprint": dict(self.tuned_fingerprint),
            "tuned_stale": self.tuned_is_stale(model_path),
            "tuned_fingerprint_diff": self.tuned_fingerprint_diff(model_path),
        }

    def log_line(self) -> str:
        """
        [EN]
        The one-line summary required by the acceptance criteria, for example:
        ``Inference engine loaded with n_threads=16, n_threads_batch=12, n_batch=512,
        prefill_chunk=512, kv_cache=q8_0, mlock=true, flash_attn=true``.

        [ZH]
        验收要求的一行摘要，例如：
        ``Inference engine loaded with n_threads=16, n_threads_batch=12, n_batch=512,
        prefill_chunk=512, kv_cache=q8_0, mlock=true, flash_attn=true``。
        """
        return (
            "Inference engine loaded with n_threads=%d, n_threads_batch=%d, n_batch=%d, "
            "prefill_chunk=%d, kv_cache=%s, mlock=%s, flash_attn=%s"
            % (self.resolve_n_threads(), self.resolve_n_threads_batch(),
               int(self.n_batch), self.resolve_prefill_chunk_tokens(), str(self.type_k),
               "true" if self.use_mlock else "false",
               "true" if self.flash_attn else "false")
        )
