"""
[EN]
Hardware capability profiling and dynamic inference planning for EchoDesk.

The module performs a single, defensive scan of the host before a model is
loaded and derives every runtime tuning value from that scan:

  * CPU: physical core count, logical core count, AVX2 and AVX-512 support;
  * GPU: NVIDIA CUDA, AMD ROCm and Apple Metal availability, with free VRAM;
  * Memory: total and currently available system RAM;
  * Planning: a safe initial context window, a hardware ceiling for that
    window, a safe ``n_gpu_layers`` value, and a deterministic thread count.

Every probe is isolated inside an exception boundary. A probe failure must
never propagate to the caller: the planner silently degrades to the most
conservative CPU-only configuration and records the degradation in the
profile so that the condition remains observable in the log.

Privacy invariant: the profiler only observes aggregate capabilities (core
counts, byte counts, backend labels). It never reads a MAC address, a serial
number, a machine GUID or an absolute path for the purpose of tuning, and it
never writes a profile or a plan to disk. Results live in memory for the
lifetime of the process; anything that is serialized (logs, diagnostics, the
JSON line protocol) is restricted to numbers and identifiers. The release gate
``tools/check_release_privacy.py`` enforces the packaged side of this rule.

[ZH]
EchoDesk 的硬件能力探测与动态推理规划模块。

该模块在加载模型之前对宿主环境执行一次防御式扫描，并据此推导全部运行时
调优参数：

  * CPU：物理核心数、逻辑核心数、AVX2 与 AVX-512 支持情况；
  * GPU：NVIDIA CUDA、AMD ROCm 与 Apple Metal 的可用性及空闲显存；
  * 内存：系统总内存与当前可用内存；
  * 规划：安全初始上下文窗口、该窗口的硬件上限、安全的 ``n_gpu_layers``
    取值，以及确定性的线程数。

所有探测均置于异常边界之内。探测失败不得向调用方传播：规划器静默降级为最
保守的纯 CPU 配置，并把降级事实记录在档案对象中，使该状况在日志中仍然可观测。

隐私不变量：探测只观察聚合能力（核心数、字节数、后端名称），绝不为了调优去读取
MAC 地址、序列号、机器 GUID 或绝对路径，也绝不把档案或规划写入磁盘。结果仅在该
进程生命周期内驻留内存；任何被序列化的内容（日志、诊断、JSON 行协议）只允许包含
数值与标识符。发布物一侧由门禁 ``tools/check_release_privacy.py`` 强制校验。
"""
from __future__ import annotations

import ctypes
import glob
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("EchoDesk.Hardware")

#: [EN] Context window allocated for a freshly loaded model. A small initial
#: allocation keeps the KV cache, and therefore idle memory usage, low; the
#: window grows on demand up to the hardware ceiling.
#: [ZH] 新加载模型时分配的初始上下文窗口。较小的初始分配可压低 KV Cache 占用，
#: [ZH] 从而降低空闲内存占用；该窗口按需增长，直至硬件上限。
DEFAULT_INITIAL_CONTEXT = 2048

#: [EN] Absolute lower bound for any context window. Matches
#: ``config.MIN_CONTEXT`` so that the planner never produces a value the
#: configuration layer would reject.
#: [ZH] 任何上下文窗口的绝对下界。与 ``config.MIN_CONTEXT`` 保持一致，使规划器
#: [ZH] 不会产出配置层会拒绝的取值。
MIN_CONTEXT = 512

#: [EN] Fallback ceiling used only when no configuration value is supplied.
#: [ZH] 仅在未提供配置取值时使用的后备上限。
DEFAULT_MAX_CONTEXT = 32768

#: [EN] VRAM safety watermark. Twenty percent of free VRAM is deliberately left
#: unallocated to absorb driver overhead, allocator fragmentation and transient
#: compute buffers, preventing out-of-memory termination.
#: [ZH] 显存安全水位线。刻意保留 20% 的空闲显存不予分配，用于吸收驱动开销、
#: [ZH] 分配器碎片与瞬时计算缓冲，避免显存耗尽导致的进程终止。
VRAM_SAFETY_RATIO = 0.20

#: [EN] RAM safety ratio applied to the currently available physical memory.
#: [ZH] 针对当前可用物理内存施加的内存安全比例。
RAM_SAFETY_RATIO = 0.20

#: [EN] Fixed allowance for the computation graph, logits and temporary buffers
#: created by llama.cpp during inference. It is applied to the **system-memory**
#: context budget only; VRAM uses the separate, larger allowance below.
#: [ZH] 为 llama.cpp 推理期间创建的计算图、logits 与临时缓冲预留的固定额度。
#: [ZH] 该额度只用于**系统内存**的上下文预算；显存使用下方单独且更大的额度。
COMPUTE_BUFFER_BYTES = 256 * 1024 * 1024

#: [EN] Fixed allowance reserved inside free VRAM for the computation graph,
#: logits and transient CUDA/ROCm buffers. Raised from 256 MiB to 512 MiB in
#: v0.2.4 task 3: on 8 GiB cards the extra headroom avoids inference-time OOM at
#: the cost of one or two offloaded layers, which is the intended trade.
#: [ZH] 在空闲显存内为计算图、logits 与瞬时 CUDA/ROCm 缓冲预留的固定额度。
#: [ZH] v0.2.4 任务-3 从 256 MiB 提升到 512 MiB：8 GiB 显卡上多留的余量可避免推理期
#: [ZH] OOM，代价是少卸载一到两层 —— 这是刻意选择的取舍。
VRAM_COMPUTE_BUFFER_BYTES = 512 * 1024 * 1024

#: [EN] Context window values are rounded down to this granularity so that the
#: reported and allocated values remain stable and human-readable.
#: [ZH] 上下文窗口取值按该粒度向下取整，使上报值与分配值保持稳定且便于阅读。
CONTEXT_ROUNDING = 256

#: [EN] Allowed batch sizes for prompt processing. ``n_batch`` is a
#: [EN] construction-time llama.cpp parameter, so it is validated before a model
#: [EN] is loaded instead of being passed through unexamined.
#: [ZH] 提示词处理批大小的允许范围。``n_batch`` 是 llama.cpp 的构建期参数，
#: [ZH] 因此在加载模型之前完成校验，而不是原样透传。
MIN_N_BATCH = 64
MAX_N_BATCH = 2048

#: [EN] KV-cache storage width. F16 is the llama.cpp default and the highest
#: [EN] precision used by the shipped presets. Quantised KV caches require
#: [EN] flash-attention support on the active backend, which has not been
#: [EN] verified on the supported hardware set, so no preset enables them.
#: [ZH] KV Cache 的存储宽度。F16 是 llama.cpp 的默认值，也是发布预设使用的最高
#: [ZH] 精度。量化 KV Cache 需要当前后端提供 flash-attention 支持，尚未在受支持的
#: [ZH] 硬件集合上验证，因此没有任何预设启用量化。
KV_CACHE_TYPE_F16 = "f16"

_MIB = 1024 * 1024
_GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class InferencePreset:
    """
    [EN]
    One of the three inference presets exposed in the settings model page.

    A preset selects construction-time parameters only. Sampling parameters
    (temperature, top-p, repetition penalty) are a separate concern and remain in
    ``model_profile.PRESETS``; mixing the two would make "quality" mean two
    unrelated things at once.

    ``context_ratio`` reduces the hardware/native ceiling for the fast preset;
    the balanced and quality presets keep the full ceiling so that an explicitly
    requested window is never silently reduced. ``thread_preference`` selects
    logical cores for the fast preset, because more worker threads shorten prompt
    prefill, at the cost of contention with the rest of the system.

    [ZH]
    设置模型页暴露的三档推理预设之一。

    预设只决定构建期参数。采样参数（温度、top-p、重复惩罚）属于另一关注点，仍由
    ``model_profile.PRESETS`` 管理；把两者混在一起会使"质量"同时表示两件互不相关
    的事情。

    ``context_ratio`` 仅对极速档收窄硬件/原生上限；均衡与质量档保留完整上限，使
    用户显式请求的窗口不会被静默缩小。``thread_preference`` 让极速档选择逻辑核心，
    因为更多的并行线程可缩短提示词预填充时间，代价是与系统其余部分争用资源。
    """

    name: str
    initial_context: int
    n_batch: int
    context_ratio: float
    thread_preference: str
    kv_cache_type: str
    label_key: str
    note_key: str


#: [EN] Inference preset table. Labels and notes are stored as language keys and
#: resolved at call time, because a module-level dictionary is evaluated at
#: import time and would freeze the active locale.
#: [ZH] 推理预设表。标签与说明以语言键形式保存，并在调用时解析，因为模块级字典
#: [ZH] 在导入时求值，会把当前语言固定下来。
INFERENCE_PRESETS: Dict[str, InferencePreset] = {
    "fast": InferencePreset(
        name="fast",
        initial_context=1024,
        n_batch=1024,
        context_ratio=0.5,
        thread_preference="logical",
        kv_cache_type=KV_CACHE_TYPE_F16,
        label_key="inference.preset_fast_label",
        note_key="inference.preset_fast_note",
    ),
    "balanced": InferencePreset(
        name="balanced",
        initial_context=DEFAULT_INITIAL_CONTEXT,
        n_batch=512,
        context_ratio=1.0,
        thread_preference="physical",
        kv_cache_type=KV_CACHE_TYPE_F16,
        label_key="inference.preset_balanced_label",
        note_key="inference.preset_balanced_note",
    ),
    "quality": InferencePreset(
        name="quality",
        initial_context=4096,
        n_batch=512,
        context_ratio=1.0,
        thread_preference="physical",
        kv_cache_type=KV_CACHE_TYPE_F16,
        label_key="inference.preset_quality_label",
        note_key="inference.preset_quality_note",
    ),
}

DEFAULT_INFERENCE_PRESET = "balanced"

#: [EN] Aliases accepted from older configuration files or command input.
#: [ZH] 兼容旧配置文件与命令输入而接受的别名。
_PRESET_ALIASES = {
    "speed": "fast", "light": "fast", "turbo": "fast",
    "default": "balanced", "normal": "balanced",
    "max": "quality", "high": "quality", "best": "quality",
}


def normalize_inference_preset(name: str) -> str:
    """
    [EN]
    Normalize a preset identifier. Unknown values fall back to the balanced
    preset, which is the documented default; the function never raises, because
    it runs on the model loading path.

    [ZH]
    规范化预设标识。未知取值回落到文档中的默认档（均衡）；该函数绝不抛出异常，
    因为它运行在模型加载路径之上。
    """
    key = str(name or "").strip().lower()
    key = _PRESET_ALIASES.get(key, key)
    return key if key in INFERENCE_PRESETS else DEFAULT_INFERENCE_PRESET


def get_inference_preset(name: str) -> InferencePreset:
    """
    [EN]
    Return the preset for the supplied identifier, normalized defensively.
    [ZH]
    返回给定标识对应的预设，并进行防御式规范化。
    """
    return INFERENCE_PRESETS[normalize_inference_preset(name)]


def inference_preset_menu() -> List[Dict[str, Any]]:
    """
    [EN]
    Return the three presets with localized labels and notes, for the settings
    model page and for diagnostics endpoints.

    Labels are resolved at call time rather than stored in the table, so a
    language switch takes effect without reloading this module.

    [ZH]
    返回三档预设及其已本地化的标签与说明，供设置模型页与诊断接口使用。

    标签在调用时解析而非存放在表中，因此切换界面语言无需重新加载本模块。
    """
    from i18n import t as _t

    return [
        {
            "id": preset.name,
            "label": _t(preset.label_key),
            "note": _t(preset.note_key),
            "initial_context": int(preset.initial_context),
            "n_batch": int(preset.n_batch),
            "context_ratio": float(preset.context_ratio),
            "thread_preference": preset.thread_preference,
            "kv_cache_type": preset.kv_cache_type,
        }
        for preset in INFERENCE_PRESETS.values()
    ]


@dataclass(frozen=True)
class CpuProfile:
    """
    [EN]
    Static CPU capabilities relevant to inference scheduling.

    [ZH]
    与推理调度相关的静态 CPU 能力。
    """

    physical_cores: int
    logical_cores: int
    avx2: bool
    avx512: bool

    def to_dict(self) -> Dict[str, Any]:
        """
        [EN]
        Serialize the CPU profile into a JSON-compatible mapping.
        [ZH]
        将 CPU 档案序列化为与 JSON 兼容的映射。
        """
        return {
            "physical_cores": int(self.physical_cores),
            "logical_cores": int(self.logical_cores),
            "avx2": bool(self.avx2),
            "avx512": bool(self.avx512),
        }


@dataclass(frozen=True)
class GpuProfile:
    """
    [EN]
    Detected accelerator and its usable video memory.

    ``detected_backend`` records what the host exposes, while ``available``
    records whether the installed llama.cpp build can actually offload layers
    to that accelerator. A dedicated NVIDIA driver installation combined with a
    CPU-only llama.cpp build therefore yields ``detected_backend="cuda"`` and
    ``available=False``, which is the truthful state and prevents a futile
    offload attempt.

    [ZH]
    已探测到的加速器及其可用显存。

    ``detected_backend`` 记录宿主实际暴露的加速器类型，``available`` 记录当前
    安装的 llama.cpp 构建是否真的能够把网络层卸载到该加速器上。因此，"已安装
    NVIDIA 驱动但 llama.cpp 为纯 CPU 构建"这一组合会得到
    ``detected_backend="cuda"`` 且 ``available=False`` 的结果；该结果符合真实
    状态，并可避免一次注定失败的卸载尝试。
    """

    detected_backend: str
    available: bool
    name: str
    total_vram_bytes: int
    free_vram_bytes: int
    reason: str = ""

    @property
    def label(self) -> str:
        """
        [EN]
        Short backend label used in logs and status text.
        [ZH]
        用于日志与状态文本的简短后端名称。
        """
        if not self.available:
            return "CPU"
        labels = {"cuda": "CUDA", "rocm": "ROCm", "metal": "Metal"}
        return labels.get(self.detected_backend, "GPU")

    def to_dict(self) -> Dict[str, Any]:
        """
        [EN]
        Serialize the GPU profile into a JSON-compatible mapping.
        [ZH]
        将 GPU 档案序列化为与 JSON 兼容的映射。
        """
        return {
            "detected_backend": self.detected_backend,
            "available": bool(self.available),
            "name": self.name,
            "total_vram_bytes": int(self.total_vram_bytes),
            "free_vram_bytes": int(self.free_vram_bytes),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class HardwareProfile:
    """
    [EN]
    Immutable snapshot of the host capabilities consumed by the inference
    planner. ``degraded`` is true when at least one probe fell back to a
    conservative default; ``notes`` records the individual fallbacks.

    [ZH]
    供推理规划器消费的宿主能力不可变快照。``degraded`` 表示至少有一项探测
    回落到保守默认值；``notes`` 记录各项回落的具体原因。
    """

    cpu: CpuProfile
    gpu: GpuProfile
    ram_total_bytes: int
    ram_available_bytes: int
    degraded: bool = False
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        """
        [EN]
        Serialize the complete hardware profile.
        [ZH]
        序列化完整的硬件档案。
        """
        return {
            "cpu": self.cpu.to_dict(),
            "gpu": self.gpu.to_dict(),
            "ram_total_bytes": int(self.ram_total_bytes),
            "ram_available_bytes": int(self.ram_available_bytes),
            "degraded": bool(self.degraded),
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        """
        [EN]
        Produce a compact single-line description for diagnostics.
        [ZH]
        生成用于诊断的单行简要描述。
        """
        return (
            f"cpu={self.cpu.physical_cores}P/{self.cpu.logical_cores}L "
            f"avx2={self.cpu.avx2} avx512={self.cpu.avx512} "
            f"gpu={self.gpu.label} vram_free={self.gpu.free_vram_bytes} "
            f"ram={self.ram_total_bytes}/{self.ram_available_bytes} "
            f"degraded={self.degraded}"
        )


@dataclass(frozen=True)
class InferencePlan:
    """
    [EN]
    Complete set of runtime tuning values derived from a hardware profile.

    ``n_ctx_initial`` governs the first model load and ``n_ctx_max`` is the
    ceiling to which the engine may expand on demand. ``n_gpu_layers`` follows
    the llama.cpp convention: ``-1`` requests full offload, ``0`` forces CPU
    inference, and a positive value offloads exactly that many layers.

    [ZH]
    由硬件档案推导出的完整运行时调优参数。

    ``n_ctx_initial`` 决定首次加载模型时的窗口，``n_ctx_max`` 是引擎可按需扩展
    至的上限。``n_gpu_layers`` 遵循 llama.cpp 的约定：``-1`` 表示全量卸载，
    ``0`` 表示强制纯 CPU 推理，正数表示精确卸载该数量的网络层。
    """

    n_ctx_initial: int
    n_ctx_max: int
    n_gpu_layers: int
    n_threads: int
    backend: str
    model_layers: int
    model_bytes: int
    kv_cache_bytes: int
    vram_usable_bytes: int
    # [EN] Construction-time parameters contributed by the inference preset and
    # [EN] the model metadata: batch size, KV-cache storage width, the model's
    # [EN] native training window, the hardware ceiling before the preset ratio,
    # [EN] and the constraint that determined the final window.
    # [ZH] 由推理预设与模型元数据贡献的构建期参数：批大小、KV Cache 存储宽度、
    # [ZH] 模型原生训练窗口、应用预设比例之前的硬件上限，以及决定最终窗口的约束。
    n_batch: int = 512
    kv_cache_type: str = KV_CACHE_TYPE_F16
    preset: str = DEFAULT_INFERENCE_PRESET
    native_context: int = 0
    hardware_context: int = 0
    context_limited_by: str = ""
    model_metadata: Dict[str, Any] = field(default_factory=dict)
    # [EN] Override diagnostics (v0.2.4 task 3): whether the user's values were
    # [EN] adopted verbatim, the safe recommendation computed independently of
    # [EN] that decision, and the localized warnings the interface must show.
    # [ZH] 覆盖诊断（v0.2.4 任务-3）：用户取值是否被原样采纳、独立于该决定计算出的
    # [ZH] 安全推荐值，以及界面必须展示的已本地化警告。
    allow_override: bool = False
    n_ctx_requested: int = 0
    n_gpu_layers_requested: int = 0
    n_batch_requested: int = 0
    recommended_n_ctx: int = 0
    recommended_n_gpu_layers: int = 0
    recommended_n_batch: int = 0
    hardware_budget: int = 0
    gpu_available: bool = False
    gpu_name: str = ""
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    reasons: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        """
        [EN]
        Serialize the inference plan into a JSON-compatible mapping.
        [ZH]
        将推理规划序列化为与 JSON 兼容的映射。
        """
        return {
            "n_ctx_initial": int(self.n_ctx_initial),
            "n_ctx_max": int(self.n_ctx_max),
            "n_gpu_layers": int(self.n_gpu_layers),
            "n_threads": int(self.n_threads),
            "backend": self.backend,
            "model_layers": int(self.model_layers),
            "model_bytes": int(self.model_bytes),
            "kv_cache_bytes": int(self.kv_cache_bytes),
            "vram_usable_bytes": int(self.vram_usable_bytes),
            "n_batch": int(self.n_batch),
            "kv_cache_type": self.kv_cache_type,
            "preset": self.preset,
            "native_context": int(self.native_context),
            "hardware_context": int(self.hardware_context),
            "context_limited_by": self.context_limited_by,
            "model_metadata": dict(self.model_metadata),
            "allow_override": bool(self.allow_override),
            "n_ctx_requested": int(self.n_ctx_requested),
            "n_gpu_layers_requested": int(self.n_gpu_layers_requested),
            "n_batch_requested": int(self.n_batch_requested),
            "recommended_n_ctx": int(self.recommended_n_ctx),
            "recommended_n_gpu_layers": int(self.recommended_n_gpu_layers),
            "recommended_n_batch": int(self.recommended_n_batch),
            "hardware_budget": int(self.hardware_budget),
            "gpu_available": bool(self.gpu_available),
            "gpu_name": self.gpu_name,
            "warnings": list(self.warnings),
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# [EN] Defensive probe helpers
# [ZH] 防御式探测辅助函数
# ---------------------------------------------------------------------------

def _run_quiet(args: List[str], timeout: float = 5.0) -> Optional[subprocess.CompletedProcess]:
    """
    [EN]
    Execute an external probe command without showing a console window and
    without allowing it to block the caller indefinitely. Returns ``None`` when
    the executable is unavailable or the invocation fails.

    [ZH]
    在不显示控制台窗口、且不无限期阻塞调用方的前提下执行外部探测命令。当可执行
    文件不可用或调用失败时返回 ``None``。
    """
    executable = shutil.which(args[0]) if args else None
    if not executable:
        return None
    try:
        kwargs: Dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "timeout": max(0.5, float(timeout)),
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run([executable] + list(args[1:]), **kwargs)
    except Exception:  # noqa: BLE001 - a failed probe is not an application failure
        return None


def _logical_core_count() -> int:
    """
    [EN]
    Return the logical processor count, falling back to one on failure.
    [ZH]
    返回逻辑处理器数量，失败时回落到 1。
    """
    try:
        value = int(os.cpu_count() or 0)
    except Exception:  # noqa: BLE001
        value = 0
    return max(1, value)


def _physical_core_count(logical: int, notes: List[str]) -> int:
    """
    [EN]
    Return the number of physical CPU cores.

    The preferred source is ``psutil`` when it happens to be installed; the
    standard library fallbacks below then cover every supported platform. When
    every source fails, the logical count is reported and the fallback is
    recorded, because over-reporting the thread count is preferable to
    under-utilising the processor.

    [ZH]
    返回 CPU 物理核心数。

    若环境中恰好安装了 ``psutil`` 则优先使用；随后的标准库回退覆盖全部受支持
    平台。当所有来源均失败时，返回逻辑核心数并记录该回落事实，因为线程数偏高
    优于处理器利用不足。
    """
    # [EN] No optional third-party probe here: ``psutil`` is not a declared runtime
    # [EN] dependency, and the standard-library paths below cover every supported
    # [EN] platform. Keeping an undeclared import would make a clean environment fail
    # [EN] the dependency gate (and the import would silently fail at runtime anyway).
    # [ZH] 这里不引入可选第三方探测：``psutil`` 不是已声明的运行期依赖，下面的标准库路径
    # [ZH] 已覆盖全部受支持平台。保留未声明导入会让干净环境无法通过依赖门禁（而且运行期
    # [ZH] 本来也会静默失败）。
    try:
        if os.name == "nt":
            return _windows_physical_cores(logical, notes)
        if sys.platform.startswith("linux"):
            return _linux_physical_cores(logical, notes)
        if sys.platform == "darwin":
            result = _run_quiet(["sysctl", "-n", "hw.physicalcpu"])
            if result is not None and result.returncode == 0:
                value = int(str(result.stdout).strip() or 0)
                if value > 0:
                    return value
    except Exception:  # noqa: BLE001
        pass
    notes.append("physical_cores_fallback")
    return max(1, logical)


def _windows_physical_cores(logical: int, notes: List[str]) -> int:
    """
    [EN]
    Enumerate physical cores through ``GetLogicalProcessorInformationEx`` with
    ``RelationProcessorCore``. The variable-length records are parsed from a raw
    buffer, so no fragile structure packing is involved.

    [ZH]
    通过 ``GetLogicalProcessorInformationEx`` 的 ``RelationProcessorCore``
    关系枚举物理核心。变长记录直接从原始缓冲区解析，以避免脆弱的结构体对齐
    处理。
    """
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        size = ctypes.c_uint(0)
        kernel32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(size))
        if not size.value:
            notes.append("physical_cores_unavailable")
            return max(1, logical)
        buffer = ctypes.create_string_buffer(size.value)
        if not kernel32.GetLogicalProcessorInformationEx(0, buffer, ctypes.byref(size)):
            notes.append("physical_cores_unavailable")
            return max(1, logical)
        raw = buffer.raw[: size.value]
        count = 0
        offset = 0
        while offset + 8 <= len(raw):
            record_size = int.from_bytes(raw[offset + 4: offset + 8], "little")
            if record_size <= 0 or offset + record_size > len(raw):
                break
            count += 1
            offset += record_size
        return count or max(1, logical)
    except Exception:  # noqa: BLE001
        notes.append("physical_cores_unavailable")
        return max(1, logical)


def _linux_physical_cores(logical: int, notes: List[str]) -> int:
    """
    [EN]
    Derive the physical core count from ``/proc/cpuinfo`` on Linux.
    [ZH]
    在 Linux 上从 ``/proc/cpuinfo`` 推导物理核心数。
    """
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        pairs = set()
        for block in text.split("\n\n"):
            physical = re.search(r"^physical id\s*:\s*(\d+)", block, re.MULTILINE)
            core = re.search(r"^core id\s*:\s*(\d+)", block, re.MULTILINE)
            if physical and core:
                pairs.add((physical.group(1), core.group(1)))
        if pairs:
            return len(pairs)
        match = re.search(r"^cpu cores\s*:\s*(\d+)", text, re.MULTILINE)
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    except Exception:  # noqa: BLE001
        pass
    notes.append("physical_cores_unavailable")
    return max(1, logical)


def _cpu_features(notes: List[str]) -> Tuple[bool, bool]:
    """
    [EN]
    Return ``(avx2, avx512)`` support flags. Unknown support is reported as
    ``False``; the flags are informational only and never change correctness.

    [ZH]
    返回 ``(avx2, avx512)`` 支持标志。未知支持情况一律报告为 ``False``；
    这两个标志仅用于信息展示，不影响正确性。
    """
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # [EN] Win32 processor-feature identifiers: 40 = AVX2, 41 = AVX-512F.
            # [ZH] Win32 处理器特性标识：40 = AVX2，41 = AVX-512F。
            return bool(kernel32.IsProcessorFeaturePresent(40)), bool(
                kernel32.IsProcessorFeaturePresent(41)
            )
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
                flags = handle.read().lower()
            return " avx2" in flags, "avx512f" in flags
        if sys.platform == "darwin":
            result = _run_quiet(["sysctl", "-n", "machdep.cpu.leaf7_features"])
            if result is not None and result.returncode == 0:
                flags = str(result.stdout).lower()
                return "avx2" in flags, "avx512f" in flags
    except Exception:  # noqa: BLE001
        notes.append("cpu_features_unavailable")
    return False, False


def _windows_memory() -> Tuple[int, int]:
    """
    [EN]
    Return ``(total, available)`` physical memory on Windows.
    [ZH]
    返回 Windows 上的 ``(总内存, 可用内存)``。
    """

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    return 0, 0


def _linux_memory() -> Tuple[int, int]:
    """
    [EN]
    Return ``(total, available)`` physical memory from ``/proc/meminfo``.
    [ZH]
    从 ``/proc/meminfo`` 读取 ``(总内存, 可用内存)``。
    """
    total = available = 0
    with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
    return total, available or max(0, total // 2)


def _darwin_memory() -> Tuple[int, int]:
    """
    [EN]
    Return ``(total, available)`` physical memory on macOS.
    [ZH]
    返回 macOS 上的 ``(总内存, 可用内存)``。
    """
    total = 0
    result = _run_quiet(["sysctl", "-n", "hw.memsize"])
    if result is not None and result.returncode == 0:
        total = int(str(result.stdout).strip() or 0)
    available = 0
    page_size = 4096
    result = _run_quiet(["vm_stat"])
    if result is not None and result.returncode == 0:
        text = str(result.stdout)
        match = re.search(r"page size of (\d+) bytes", text)
        if match:
            page_size = int(match.group(1))
        free = inactive = 0
        for line in text.splitlines():
            if line.startswith("Pages free:"):
                free = int(re.sub(r"\D", "", line.split(":")[1].split(".")[0]) or 0)
            elif line.startswith("Pages inactive:"):
                inactive = int(re.sub(r"\D", "", line.split(":")[1].split(".")[0]) or 0)
        available = (free + inactive) * page_size
    if total and not available:
        available = total // 2
    return total, available


def _memory_bytes(notes: List[str]) -> Tuple[int, int]:
    """
    [EN]
    Return ``(total, available)`` physical memory in bytes. Every platform
    failure is contained and recorded; the final fallback reports zero so that
    the planner applies its most conservative limits.

    [ZH]
    返回以字节计的 ``(总内存, 可用内存)``。各平台失败均被捕获并记录；最终回落
    报告零值，使规划器采用最保守的限制。
    """
    try:
        if os.name == "nt":
            total, available = _windows_memory()
            if total > 0:
                return total, available
        elif sys.platform.startswith("linux"):
            total, available = _linux_memory()
            if total > 0:
                return total, available
        elif sys.platform == "darwin":
            total, available = _darwin_memory()
            if total > 0:
                return total, available
    except Exception:  # noqa: BLE001
        pass
    notes.append("memory_probe_failed")
    return 0, 0


def _best_gpu(candidates: List[GpuProfile], notes: List[str]) -> Optional[GpuProfile]:
    """
    [EN]
    Select the candidate with the largest free video memory.

    Multiple accelerators are deliberately not summed: llama.cpp offloads to one
    device by default, so summing would overstate the budget and could produce
    an offload target that does not fit any single device.

    [ZH]
    选择空闲显存最大的候选设备。

    刻意不对多块加速器求和：llama.cpp 默认卸载到单块设备，求和会高估预算，
    并可能得出任何单块设备都无法容纳的卸载目标。
    """
    usable = [item for item in candidates if item is not None and item.available]
    if not usable:
        return None
    if len(usable) > 1:
        notes.append("multiple_gpus_best_free_vram_selected")
    return max(usable, key=lambda item: item.free_vram_bytes)


def _probe_nvidia(notes: List[str]) -> Optional[GpuProfile]:
    """
    [EN]
    Probe NVIDIA devices through ``nvidia-smi``.
    [ZH]
    通过 ``nvidia-smi`` 探测 NVIDIA 设备。
    """
    result = _run_quiet(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ]
    )
    if result is None:
        return None
    if result.returncode != 0:
        notes.append("nvidia_smi_failed")
        return None
    candidates: List[GpuProfile] = []
    for line in str(result.stdout).splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total = int(float(parts[-2]) * _MIB)
            free = int(float(parts[-1]) * _MIB)
        except (TypeError, ValueError):
            continue
        name = ", ".join(parts[:-2]) or "NVIDIA GPU"
        candidates.append(
            GpuProfile(
                detected_backend="cuda",
                available=True,
                name=name,
                total_vram_bytes=total,
                free_vram_bytes=free,
            )
        )
    return _best_gpu(candidates, notes)


def _probe_amd(notes: List[str]) -> Optional[GpuProfile]:
    """
    [EN]
    Probe AMD devices through the amdgpu sysfs interface. The interface exposes
    the same information that ROCm reports and requires no proprietary tooling.

    [ZH]
    通过 amdgpu 的 sysfs 接口探测 AMD 设备。该接口提供与 ROCm 相同的信息，
    且不依赖任何专有工具。
    """
    candidates: List[GpuProfile] = []
    try:
        for total_path in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
            directory = os.path.dirname(total_path)
            try:
                with open(total_path, "r", encoding="ascii") as handle:
                    total = int(handle.read().strip())
                used = 0
                used_path = os.path.join(directory, "mem_info_vram_used")
                if os.path.isfile(used_path):
                    with open(used_path, "r", encoding="ascii") as handle:
                        used = int(handle.read().strip())
            except (OSError, ValueError):
                continue
            if total <= 0:
                continue
            name = "AMD GPU"
            name_path = os.path.join(directory, "product_name")
            try:
                if os.path.isfile(name_path):
                    with open(name_path, "r", encoding="utf-8", errors="replace") as handle:
                        name = handle.read().strip() or name
            except OSError:
                pass
            candidates.append(
                GpuProfile(
                    detected_backend="rocm",
                    available=True,
                    name=name,
                    total_vram_bytes=total,
                    free_vram_bytes=max(0, total - used),
                )
            )
    except Exception:  # noqa: BLE001
        notes.append("amd_probe_failed")
    return _best_gpu(candidates, notes)


def _probe_metal(notes: List[str], ram_total: int) -> Optional[GpuProfile]:
    """
    [EN]
    Report Apple Metal on macOS. Unified memory is bounded by the wired-memory
    limit reported by the kernel; when that value is unavailable, seventy
    percent of physical memory is used as a conservative approximation.

    [ZH]
    在 macOS 上报告 Apple Metal。统一内存以内核报告的驻留内存上限为界；该值
    不可用时，使用物理内存的 70% 作为保守近似。
    """
    if sys.platform != "darwin":
        return None
    total = 0
    result = _run_quiet(["sysctl", "-n", "iogpu.wired_limit_mb"])
    if result is not None and result.returncode == 0:
        try:
            total = int(str(result.stdout).strip() or 0) * _MIB
        except ValueError:
            total = 0
    if total <= 0 and ram_total > 0:
        total = int(ram_total * 0.70)
    if total <= 0:
        notes.append("metal_vram_unavailable")
        return None
    return GpuProfile(
        detected_backend="metal",
        available=True,
        name="Apple Metal",
        total_vram_bytes=total,
        free_vram_bytes=total,
    )


def _llama_gpu_offload_supported() -> Optional[bool]:
    """
    [EN]
    Ask the installed llama.cpp build whether it supports GPU offload. Returns
    ``None`` when the answer cannot be determined, in which case the detected
    accelerator is trusted.

    [ZH]
    询问当前安装的 llama.cpp 构建是否支持 GPU 卸载。无法确定时返回 ``None``，
    此时以探测到的加速器为准。
    """
    try:
        import llama_cpp  # type: ignore

        probe = getattr(llama_cpp, "llama_supports_gpu_offload", None)
        if callable(probe):
            return bool(probe())
        inner = getattr(llama_cpp, "llama_cpp", None)
        probe = getattr(inner, "llama_supports_gpu_offload", None)
        if callable(probe):
            return bool(probe())
    except Exception:  # noqa: BLE001 - a missing backend is a normal deployment
        return None
    return None


_CACHED_PROFILE: Optional[HardwareProfile] = None


def probe(force: bool = False) -> HardwareProfile:
    """
    [EN]
    Return the hardware profile, probing the host at most once per process.

    The function is total: every internal failure is converted into a
    conservative CPU-only profile. It never raises, because it is invoked on the
    startup path where an exception would terminate the backend process.

    [ZH]
    返回硬件档案，每个进程至多执行一次探测。

    该函数为全函数：所有内部失败都会转换为保守的纯 CPU 档案。该函数绝不抛出
    异常，因为它运行于启动路径之上，一旦抛出将终止后端进程。
    """
    global _CACHED_PROFILE
    if _CACHED_PROFILE is not None and not force:
        return _CACHED_PROFILE
    try:
        profile = _probe_impl()
    except Exception:  # noqa: BLE001 - the startup path must never be interrupted
        log.warning("硬件探测发生未预期异常，已静默降级为保守配置", exc_info=True)
        logical = _logical_core_count()
        profile = HardwareProfile(
            cpu=CpuProfile(physical_cores=max(1, logical), logical_cores=logical,
                           avx2=False, avx512=False),
            gpu=GpuProfile("none", False, "", 0, 0, "probe_exception"),
            ram_total_bytes=0,
            ram_available_bytes=0,
            degraded=True,
            notes=("probe_exception",),
        )
    _CACHED_PROFILE = profile
    return profile


def reset_cache() -> None:
    """
    [EN]
    Discard the cached profile. Provided for tests and for explicit
    re-enumeration after a hardware change.
    [ZH]
    丢弃已缓存的档案。供测试使用，或在硬件发生变化后显式重新枚举。
    """
    global _CACHED_PROFILE
    _CACHED_PROFILE = None


def _probe_impl() -> HardwareProfile:
    """
    [EN]
    Perform the actual probe sequence and assemble the profile.
    [ZH]
    执行实际探测流程并组装档案。
    """
    notes: List[str] = []
    logical = _logical_core_count()
    physical = _physical_core_count(logical, notes)
    avx2, avx512 = _cpu_features(notes)
    ram_total, ram_available = _memory_bytes(notes)

    gpu_candidates: List[GpuProfile] = []
    for detector in (
        lambda: _probe_nvidia(notes),
        lambda: _probe_amd(notes),
        lambda: _probe_metal(notes, ram_total),
    ):
        try:
            candidate = detector()
        except Exception:  # noqa: BLE001
            candidate = None
        if candidate is not None:
            gpu_candidates.append(candidate)
    gpu = _best_gpu(gpu_candidates, notes)

    if gpu is None:
        gpu = GpuProfile("none", False, "", 0, 0, "no_supported_gpu")
    else:
        supported = _llama_gpu_offload_supported()
        if supported is False:
            gpu = GpuProfile(
                detected_backend=gpu.detected_backend,
                available=False,
                name=gpu.name,
                total_vram_bytes=gpu.total_vram_bytes,
                free_vram_bytes=gpu.free_vram_bytes,
                reason="llama_cpp_without_gpu_offload",
            )
            notes.append("llama_cpp_without_gpu_offload")

    degraded = bool(notes)
    profile = HardwareProfile(
        cpu=CpuProfile(physical_cores=physical, logical_cores=logical,
                       avx2=avx2, avx512=avx512),
        gpu=gpu,
        ram_total_bytes=max(0, int(ram_total)),
        ram_available_bytes=max(0, int(ram_available)),
        degraded=degraded,
        notes=tuple(notes),
    )
    log.info("硬件探测完成: %s", profile.summary())
    return profile


# ---------------------------------------------------------------------------
# [EN] Model shape inspection
# [ZH] 模型形态检查
# ---------------------------------------------------------------------------

def _read_model_metadata(model_path: str):
    """
    [EN]
    Read the structured GGUF metadata used by the planner. The import is
    deferred so that this module remains importable while the model layer is
    being initialized, and every failure degrades to empty metadata rather than
    interrupting planning.

    [ZH]
    读取规划器使用的结构化 GGUF 元数据。导入被延迟到调用时，使本模块在模型层
    初始化期间仍可导入；所有失败都降级为空元数据，而不中断规划流程。
    """
    path = str(model_path or "").strip()
    try:
        from gguf_meta import ModelMetadata, read_model_metadata

        if not path:
            return ModelMetadata()
        return read_model_metadata(path)
    except Exception:  # noqa: BLE001 - unreadable metadata is a normal case
        return None


def _model_shape(model_path: str) -> Tuple[int, int]:
    """
    [EN]
    Return ``(layer_count, file_size_bytes)`` for a GGUF model. Unavailable
    values are reported as zero and never interrupt planning.

    [ZH]
    返回 GGUF 模型的 ``(网络层数, 文件字节数)``。不可用的取值报告为零，且不会
    中断规划流程。
    """
    layers = 0
    size = 0
    path = str(model_path or "").strip()
    if path:
        try:
            if os.path.isfile(path):
                size = int(os.path.getsize(path))
        except OSError:
            size = 0
        metadata = _read_model_metadata(path)
        if metadata is not None:
            layers = max(0, int(getattr(metadata, "n_layers", 0) or 0))
    return layers, size


def _kv_bytes_per_token(model_path: str, kv_cache_type: str = KV_CACHE_TYPE_F16) -> float:
    """
    [EN]
    Return the KV-cache cost of a single context token, or zero when the model
    metadata is insufficient. The width is read from the model header, so the
    estimate covers grouped-query attention models exactly rather than assuming
    one key/value head per layer.

    [ZH]
    返回单个上下文 token 的 KV Cache 开销；模型元数据不足时返回零。宽度取自模型
    头部，因此该估算能精确覆盖分组查询注意力模型，而不是假定每层一个键/值头。
    """
    path = str(model_path or "").strip()
    if not path or not os.path.isfile(path):
        return 0.0
    metadata = _read_model_metadata(path)
    if metadata is not None:
        value = float(getattr(metadata, "kv_bytes_per_token", lambda _b=2: 0.0)(2))
        if value > 0:
            return value
    try:
        from gguf_meta import estimate_kv_cache_bytes

        sample = estimate_kv_cache_bytes(path, 1024)
        if sample and sample > 0:
            return float(sample) / 1024.0
    except Exception:  # noqa: BLE001
        return 0.0
    return 0.0


def _kv_cache_bytes(model_path: str, n_ctx: int) -> int:
    """
    [EN]
    Estimate the KV-cache size for the supplied context window.
    [ZH]
    估算指定上下文窗口下的 KV Cache 大小。
    """
    path = str(model_path or "").strip()
    if not path or not os.path.isfile(path) or int(n_ctx) <= 0:
        return 0
    try:
        from gguf_meta import estimate_kv_cache_bytes

        value = estimate_kv_cache_bytes(path, int(n_ctx))
        return int(value) if value and value > 0 else 0
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# [EN] Planning primitives
# [ZH] 规划原语
# ---------------------------------------------------------------------------

def _context_limit_detail(
    profile: HardwareProfile,
    model_path: str = "",
    train_context: int = 0,
    requested_max: int = 0,
    ceiling: int = DEFAULT_MAX_CONTEXT,
) -> Tuple[int, str]:
    """
    [EN]
    Compute the hardware context ceiling together with the constraint that
    determined it.

    The returned reason is one of ``native`` (the model's trained window),
    ``config`` (the configured ceiling or request), ``hardware`` (the available
    memory budget, including the unknown-metadata fallback), or ``unknown`` when
    no constraint could be identified. The reason is reported to the interface so
    that a reduced window is explained rather than silently applied.

    [ZH]
    计算硬件上下文上限，并给出决定该上限的约束。

    返回的原因取值为：``native``（模型的训练窗口）、``config``（配置上限或请求
    值）、``hardware``（可用内存预算，含元数据未知时的回退），以及在无法识别任何
    约束时的 ``unknown``。该原因会上报给界面，使被收窄的窗口得到解释，而不是被
    静默应用。
    """
    constraints: List[Tuple[str, int]] = []
    if ceiling and int(ceiling) > 0:
        constraints.append(("config", int(ceiling)))
    if requested_max and int(requested_max) > 0:
        constraints.append(("config", int(requested_max)))

    trained = int(train_context or 0)
    if trained <= 0 and model_path:
        try:
            from gguf_meta import resolve_context_window

            trained = int(resolve_context_window(model_path, 0) or 0)
        except Exception:  # noqa: BLE001
            trained = 0
    if trained > 0:
        constraints.append(("native", trained))

    if constraints:
        reason, cap = min(constraints, key=lambda item: item[1])
    else:
        reason, cap = "unknown", DEFAULT_MAX_CONTEXT

    per_token = _kv_bytes_per_token(model_path)
    if per_token > 0 and profile.ram_available_bytes > 0:
        _, model_bytes = _model_shape(model_path)
        budget = (
            int(profile.ram_available_bytes * (1.0 - RAM_SAFETY_RATIO))
            - int(model_bytes)
            - COMPUTE_BUFFER_BYTES
        )
        memory_cap = int(budget // per_token) if budget > 0 else 0
        if memory_cap > 0:
            if memory_cap < cap:
                cap = memory_cap
                reason = "hardware"
        else:
            cap = MIN_CONTEXT
            reason = "hardware"
    elif per_token <= 0:
        # [EN] Without metadata, an 8192-token ceiling is the conservative
        # [EN] assumption; a larger value cannot be justified from the file.
        # [ZH] 元数据不可用时，以 8192 token 为保守假设；更大的取值无法由文件
        # [ZH] 内容得到支持。
        if cap > 8192:
            cap = 8192
            reason = "hardware"

    cap = max(MIN_CONTEXT, int(cap))
    remainder = cap % CONTEXT_ROUNDING
    if remainder:
        cap -= remainder
    return int(max(MIN_CONTEXT, cap)), reason


def safe_context_limit(
    profile: HardwareProfile,
    model_path: str = "",
    train_context: int = 0,
    requested_max: int = 0,
    ceiling: int = DEFAULT_MAX_CONTEXT,
) -> int:
    """
    [EN]
    Compute the largest context window that the host can serve safely.

    The result is the minimum of four constraints: the configured ceiling, the
    requested maximum, the model's trained window, and the memory budget derived
    from currently available RAM. The memory budget reserves a fixed ratio of
    available memory plus the computation buffer, so the calculation remains
    conservative under concurrent load.

    [ZH]
    计算宿主能够安全承载的最大上下文窗口。

    结果取四项约束的最小值：配置上限、请求上限、模型训练窗口，以及由当前可用
    内存推导出的内存预算。内存预算保留固定比例的可用内存与计算缓冲，因此在
    并发负载下仍保持保守。
    """
    limit, _ = _context_limit_detail(
        profile,
        model_path=model_path,
        train_context=train_context,
        requested_max=requested_max,
        ceiling=ceiling,
    )
    return int(limit)


def initial_context_for(
    n_ctx_max: int, preferred: int = DEFAULT_INITIAL_CONTEXT
) -> int:
    """
    [EN]
    Derive the initial context window from the hardware ceiling.

    The initial window is the preferred small allocation, rounded down to the
    context granularity and clamped to the ceiling. It is never larger than the
    ceiling and never smaller than the absolute minimum.

    [ZH]
    由硬件上限推导初始上下文窗口。

    初始窗口取首选的较小分配值，按上下文粒度向下取整并收敛到上限。该值不会
    超过上限，也不会低于绝对下界。
    """
    maximum = max(MIN_CONTEXT, int(n_ctx_max or MIN_CONTEXT))
    target = max(MIN_CONTEXT, min(int(preferred), maximum))
    target = max(MIN_CONTEXT, target - (target % CONTEXT_ROUNDING))
    return int(min(maximum, target))


def _gpu_layer_decision(
    profile: HardwareProfile,
    model_layers: int,
    model_bytes: int,
    kv_cache_bytes: int,
    safety_ratio: float = VRAM_SAFETY_RATIO,
    compute_buffer: int = VRAM_COMPUTE_BUFFER_BYTES,
) -> Tuple[int, str]:
    """
    [EN]
    Decide how many transformer layers can be offloaded to the accelerator.

    The decision applies three rules in order:

    1. With no usable accelerator, or with unusable model dimensions, the result
       is forced to zero (pure CPU inference).
    2. When model weights, KV cache and the computation buffer all fit inside
       the usable VRAM (free VRAM minus the safety watermark), full offload
       (``-1``) is requested.
    3. Otherwise the remaining VRAM is divided by the per-layer weight size.
       One layer-equivalent is withheld for embeddings and the output head,
       which remain resident on the host in llama.cpp's partial-offload mode.

    [ZH]
    决定可卸载至加速器的网络层数量。

    决策按以下三条规则依次执行：

    1. 没有可用加速器，或模型维度不可用时，结果强制为零（纯 CPU 推理）。
    2. 当模型权重、KV Cache 与计算缓冲均能容纳在可用显存（空闲显存减去安全
       水位线）之内时，请求全量卸载（``-1``）。
    3. 否则以剩余显存除以单层权重大小。llama.cpp 的部分卸载模式下词嵌入与输出
       头保留在宿主侧，因此预留一个单层等效额度。
    """
    gpu = profile.gpu
    if not gpu.available or gpu.free_vram_bytes <= 0:
        return 0, "no_accelerator"
    if model_layers <= 0 or model_bytes <= 0:
        return 0, "model_dimensions_unknown"

    usable = int(gpu.free_vram_bytes * (1.0 - float(safety_ratio)))
    overhead = max(0, int(kv_cache_bytes)) + max(0, int(compute_buffer))
    if usable <= overhead:
        return 0, "insufficient_vram"
    if model_bytes + overhead <= usable:
        return -1, "full_offload_fits_safety_watermark"

    per_layer = float(model_bytes) / float(model_layers + 1)
    if per_layer <= 0:
        return 0, "model_dimensions_unknown"
    remaining = float(usable - overhead) - per_layer
    layers = int(max(0.0, remaining) // per_layer)
    layers = max(0, min(int(model_layers), layers))
    if layers <= 0:
        return 0, "insufficient_vram"
    return layers, "partial_offload"


def compute_gpu_layers(
    profile: HardwareProfile,
    model_layers: int,
    model_bytes: int,
    kv_cache_bytes: int = 0,
    safety_ratio: float = VRAM_SAFETY_RATIO,
    compute_buffer: int = VRAM_COMPUTE_BUFFER_BYTES,
) -> int:
    """
    [EN]
    Public wrapper around :func:`_gpu_layer_decision` that returns only the
    layer count, suitable for direct assertions in tests.

    [ZH]
    :func:`_gpu_layer_decision` 的公开包装，仅返回层数，便于在测试中直接断言。
    """
    layers, _ = _gpu_layer_decision(
        profile=profile,
        model_layers=model_layers,
        model_bytes=model_bytes,
        kv_cache_bytes=kv_cache_bytes,
        safety_ratio=safety_ratio,
        compute_buffer=compute_buffer,
    )
    return int(layers)


def plan_inference(
    profile: Optional[HardwareProfile] = None,
    model_path: str = "",
    train_context: int = 0,
    requested_context: int = 0,
    max_context: int = DEFAULT_MAX_CONTEXT,
    requested_gpu_layers: int = 0,
    n_threads_override: int = 0,
    initial_context_preference: int = 0,
    inference_profile: str = "",
    requested_n_batch: int = 0,
    allow_override: bool = False,
) -> InferencePlan:
    """
    [EN]
    Produce the complete inference plan for a model on the supplied profile.

    The plan combines three inputs that were previously examined in isolation:

    1. the GGUF header, for the model's native context window, layer count and
       attention-head geometry;
    2. the hardware profile, for the GPU offload decision and the memory-bounded
       context ceiling;
    3. the inference preset, for the batch size, the initial window, the context
       ratio and the thread preference.

    The native window is a hard ceiling: neither the requested context nor the
    available memory can raise it. The memory budget can only lower it, and the
    constraint that determined the final window is reported through
    ``context_limited_by`` so that the interface can explain the reduction.

    ``requested_gpu_layers`` preserves the established manual override: a
    positive value or ``-1`` is honoured when an accelerator is available, and is
    forcibly reset to zero when it is not. ``n_threads_override`` preserves the
    user's explicit thread count; zero selects the value implied by the preset.

    [ZH]
    在给定档案上为某个模型生成完整的推理规划。

    规划综合此前各自独立查看的三类输入：

    1. GGUF 头部：模型原生上下文窗口、层数与注意力头几何；
    2. 硬件档案：GPU 卸载决策与受内存约束的上下文上限；
    3. 推理预设：批大小、初始窗口、上下文比例与线程偏好。

    原生窗口是硬性上限：请求的上下文与可用内存都不能抬高它，内存预算只能收窄
    它。决定最终窗口的约束通过 ``context_limited_by`` 上报，使界面能够解释收窄的
    原因。

    ``requested_gpu_layers`` 保留既有的手工覆盖语义：存在可用加速器时，正数或
    ``-1`` 均被采纳；不存在可用加速器时强制归零。``n_threads_override`` 保留
    用户显式指定的线程数；取零时采用预设隐含的取值。

    ``allow_override`` (v0.2.4 task 3) changes the role of the planner from
    "decision maker" to "advisor": when true, the requested context, GPU layer
    count and batch size are adopted verbatim, and the plan carries localized
    ``warnings`` plus the independent ``recommended_*`` values. The default
    false preserves the established automatic narrowing, so no existing caller
    changes behaviour without opting in.

    ``allow_override``（v0.2.4 任务-3）把规划器的角色从"决策者"改为"顾问"：
    为真时原样采纳请求的上下文、GPU 层数与批大小，并在规划中带上已本地化的
    ``warnings`` 以及独立计算出的 ``recommended_*`` 推荐值。默认假保留既有的自动
    收窄行为，因此未显式开启的调用方行为不变。
    """
    hardware = profile if profile is not None else probe()
    from i18n import t as _t

    preset = get_inference_preset(inference_profile)
    metadata = _read_model_metadata(model_path)
    layers = int(getattr(metadata, "n_layers", 0) or 0)
    model_bytes = 0
    if model_path:
        layers, model_bytes = _model_shape(model_path)

    native_context = int(
        getattr(metadata, "n_ctx_train", 0) or 0
    ) or int(train_context or 0)

    # [EN] Safe recommendation, computed independently of the user's request:
    # [EN] the panel reports the machine's budget even while an override is
    # [EN] active, so "recommended" never mutates into "what you typed".
    # [ZH] 安全推荐值独立于用户请求计算：即使覆盖已生效，诊断面板上报的仍是机器的
    # [ZH] 预算，避免"推荐值"悄悄变成"用户刚输入的值"。
    recommended_hardware_context, _recommended_reason = _context_limit_detail(
        hardware,
        model_path=model_path,
        train_context=native_context,
        requested_max=0,
        ceiling=max_context,
    )
    ratio = float(preset.context_ratio)
    if ratio < 1.0:
        recommended_cap = int(recommended_hardware_context * ratio)
        recommended_cap = max(
            MIN_CONTEXT, recommended_cap - (recommended_cap % CONTEXT_ROUNDING)
        )
    else:
        recommended_cap = int(recommended_hardware_context)
    recommended_n_ctx = max(MIN_CONTEXT, int(recommended_cap))
    preferred_initial = int(initial_context_preference or preset.initial_context)
    recommended_initial = initial_context_for(recommended_n_ctx, preferred_initial)
    recommended_kv_cache_bytes = _kv_cache_bytes(model_path, recommended_n_ctx)

    recommended_n_gpu_layers = 0
    recommended_gpu_reason = "cpu_only"
    if hardware.gpu.available:
        recommended_n_gpu_layers, recommended_gpu_reason = _gpu_layer_decision(
            profile=hardware,
            model_layers=layers,
            model_bytes=model_bytes,
            kv_cache_bytes=recommended_kv_cache_bytes,
        )
    recommended_n_batch = int(preset.n_batch)

    # [EN] Automatic plan (the established behaviour). ``requested_context`` is
    # [EN] still honoured as a ceiling here; only an explicit override skips the
    # [EN] narrowing below.
    # [ZH] 自动规划（既有行为）。此处仍把 ``requested_context`` 当作上限；只有显式
    # [ZH] 覆盖才会跳过下面的收窄。
    hardware_context, hardware_reason = _context_limit_detail(
        hardware,
        model_path=model_path,
        train_context=native_context,
        requested_max=requested_context,
        ceiling=max_context,
    )
    if ratio < 1.0:
        preset_cap = int(hardware_context * ratio)
        preset_cap = max(MIN_CONTEXT, preset_cap - (preset_cap % CONTEXT_ROUNDING))
    else:
        preset_cap = int(hardware_context)
    auto_n_ctx_max = int(min(hardware_context, preset_cap))
    if auto_n_ctx_max < hardware_context:
        auto_context_limited_by = "preset"
    elif hardware_reason == "native":
        auto_context_limited_by = "native"
    elif hardware_reason == "config":
        auto_context_limited_by = "config"
    else:
        auto_context_limited_by = "hardware"

    warnings: List[str] = []
    requested_ctx_value = int(requested_context or 0)
    if allow_override and requested_ctx_value > 0:
        n_ctx_max = max(MIN_CONTEXT, requested_ctx_value)
        n_ctx_initial = n_ctx_max
        context_limited_by = "override"
        if n_ctx_max > recommended_hardware_context:
            warnings.append(_t(
                "inference.warn_context_over_budget",
                requested=n_ctx_max,
                budget=recommended_hardware_context,
            ))
        if native_context > 0 and n_ctx_max > native_context:
            warnings.append(_t(
                "inference.warn_context_over_native",
                requested=n_ctx_max,
                native=native_context,
            ))
    else:
        n_ctx_max = auto_n_ctx_max
        n_ctx_initial = initial_context_for(n_ctx_max, preferred_initial)
        context_limited_by = auto_context_limited_by
    kv_cache_bytes = _kv_cache_bytes(model_path, n_ctx_max)

    reasons: List[str] = []
    reasons.append(f"preset_{preset.name}")
    if allow_override:
        n_gpu_layers = int(requested_gpu_layers)
        reasons.append("manual_override")
        if not hardware.gpu.available and n_gpu_layers != 0:
            warnings.append(_t(
                "inference.warn_gpu_unavailable",
                layers=n_gpu_layers,
            ))
        elif hardware.gpu.available:
            if n_gpu_layers == -1 and recommended_n_gpu_layers != -1:
                warnings.append(_t(
                    "inference.warn_gpu_full_offload",
                    requested=n_gpu_layers,
                    recommended=recommended_n_gpu_layers,
                ))
            elif (n_gpu_layers > 0 and recommended_n_gpu_layers >= 0
                  and n_gpu_layers > recommended_n_gpu_layers):
                warnings.append(_t(
                    "inference.warn_gpu_layers_over",
                    requested=n_gpu_layers,
                    recommended=recommended_n_gpu_layers,
                ))
            if layers > 0 and n_gpu_layers > layers:
                warnings.append(_t(
                    "inference.warn_gpu_layers_over_model",
                    requested=n_gpu_layers,
                    layers=layers,
                ))
        backend = hardware.gpu.label if n_gpu_layers != 0 else "CPU"
    elif not hardware.gpu.available:
        n_gpu_layers = 0
        reasons.append("cpu_only")
        if hardware.gpu.detected_backend != "none":
            reasons.append(hardware.gpu.reason or "accelerator_unavailable")
    elif requested_gpu_layers:
        n_gpu_layers = int(requested_gpu_layers)
        reasons.append("manual_override")
    else:
        n_gpu_layers, reason = _gpu_layer_decision(
            profile=hardware,
            model_layers=layers,
            model_bytes=model_bytes,
            kv_cache_bytes=kv_cache_bytes,
        )
        reasons.append(reason)

    if not allow_override:
        if n_gpu_layers != 0 and hardware.gpu.available:
            backend = hardware.gpu.label
        else:
            backend = "CPU"
            n_gpu_layers = 0

    logical = max(1, int(hardware.cpu.logical_cores or 1))
    physical = int(hardware.cpu.physical_cores or 0)
    if int(n_threads_override) > 0:
        n_threads = int(n_threads_override)
    elif preset.thread_preference == "logical":
        n_threads = logical
    else:
        n_threads = physical if physical > 0 else logical
    n_threads = max(1, min(logical, int(n_threads)))

    if int(requested_n_batch or 0) > 0:
        requested_batch_value = int(requested_n_batch)
    else:
        requested_batch_value = 0
    if requested_batch_value > 0:
        n_batch = requested_batch_value
        if allow_override and (n_batch < MIN_N_BATCH or n_batch > MAX_N_BATCH):
            warnings.append(_t(
                "inference.warn_batch_out_of_range",
                batch=n_batch,
                low=MIN_N_BATCH,
                high=MAX_N_BATCH,
            ))
        if not allow_override:
            n_batch = max(MIN_N_BATCH, min(MAX_N_BATCH, n_batch))
    else:
        n_batch = int(preset.n_batch)
        n_batch = max(MIN_N_BATCH, min(MAX_N_BATCH, n_batch))

    if allow_override:
        reasons.append("override")
    if warnings:
        reasons.append("has_warnings")
    if model_path and layers <= 0:
        reasons.append("model_layer_count_unknown")
    if hardware.degraded:
        reasons.append("hardware_profile_degraded")
    if n_ctx_max <= MIN_CONTEXT:
        reasons.append("context_clamped_to_minimum")

    usable_vram = (
        int(hardware.gpu.free_vram_bytes * (1.0 - VRAM_SAFETY_RATIO))
        if hardware.gpu.available
        else 0
    )
    plan = InferencePlan(
        n_ctx_initial=int(n_ctx_initial),
        n_ctx_max=int(n_ctx_max),
        n_gpu_layers=int(n_gpu_layers),
        n_threads=int(n_threads),
        backend=backend,
        model_layers=int(layers),
        model_bytes=int(model_bytes),
        kv_cache_bytes=int(kv_cache_bytes),
        vram_usable_bytes=int(usable_vram),
        n_batch=int(n_batch),
        kv_cache_type=str(preset.kv_cache_type),
        preset=str(preset.name),
        native_context=int(native_context),
        hardware_context=int(hardware_context),
        context_limited_by=str(context_limited_by),
        model_metadata=(metadata.to_dict() if metadata is not None else {}),
        allow_override=bool(allow_override),
        n_ctx_requested=requested_ctx_value,
        n_gpu_layers_requested=int(requested_gpu_layers),
        n_batch_requested=requested_batch_value,
        recommended_n_ctx=int(recommended_n_ctx),
        recommended_n_gpu_layers=int(recommended_n_gpu_layers),
        recommended_n_batch=int(recommended_n_batch),
        hardware_budget=int(recommended_hardware_context),
        gpu_available=bool(hardware.gpu.available),
        gpu_name=str(hardware.gpu.name or hardware.gpu.label),
        warnings=tuple(warnings),
        reasons=tuple(reasons),
    )
    log.info(
        "推理规划: preset=%s initial_ctx=%d max_ctx=%d native_ctx=%d batch=%d "
        "layers=%d threads=%d backend=%s model_layers=%d kv=%d vram_usable=%d "
        "limited_by=%s override=%s warnings=%d reasons=%s",
        preset.name, plan.n_ctx_initial, plan.n_ctx_max, plan.native_context,
        plan.n_batch, plan.n_gpu_layers, plan.n_threads, plan.backend,
        plan.model_layers, plan.kv_cache_bytes, plan.vram_usable_bytes,
        plan.context_limited_by, plan.allow_override, len(plan.warnings),
        ",".join(plan.reasons),
    )
    return plan
