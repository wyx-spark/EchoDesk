"""
[EN]
Model capability profiling and configuration helpers.

This module computes and reports model capability boundaries without performing
inference and without introducing third-party dependencies.

Contents:

* :class:`ModelProfile`: parameter count, quantization, training context,
  usable context, KV-cache estimate, measured throughput, and capability
  warnings.
* :func:`recommend_config`: recommends model size, quantization, thread count,
  and context length from available memory and CPU cores.
* :data:`PRESETS`: generation presets for light, balanced, and quality modes.
* :class:`SummaryCache`: caches chunk summaries and parsed file results so that
  uploading the same file twice does not repeat the work.

All estimated values are marked as estimated. Unavailable values are reported
as unknown; estimated values are never presented as measurements. The purpose
of this module is to make the model's capabilities and limitations explicit.

[ZH]
模型能力画像与配置辅助。

本模块在不执行推理、不引入第三方依赖的前提下计算并报告模型能力边界。

内容包括：

* :class:`ModelProfile`：参数量、量化格式、训练上下文、实际可用上下文、
  KV Cache 估算、实测吞吐与能力提示。
* :func:`recommend_config`：根据可用内存与 CPU 核数推荐模型规模、量化等级、
  线程数与上下文长度。
* :data:`PRESETS`：轻量、均衡与高质量三档生成参数预设。
* :class:`SummaryCache`：缓存分段摘要与文件解析结果，使同一文件二次上传时
  不重复计算。

所有估算值均明确标记为估算。不可用的值报告为未知；估算值不得冒充实测值。
本模块的目的是明确说明模型能够完成与不能完成的工作。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from gguf_meta import (
    architecture,
    estimate_kv_cache_bytes,
    format_bytes,
    read_metadata,
    resolve_context_window,
)

# [EN] User-facing text in this module (capability warnings, recommendation
# [EN] reasons, preset labels, source annotations, and unknown-value
# [EN] placeholders) is resolved from the language pack under the
# [EN] ``capability.`` prefix. i18n depends only on the standard library, so it
# [EN] can be imported directly here.
# [ZH] 本模块中面向用户的文本（能力提示、推荐理由、预设名称、来源标注与未知值
# [ZH] 占位）统一通过 ``capability.`` 前缀从语言包解析。i18n 仅依赖标准库，
# [ZH] 因此可在此直接导入。
from i18n import t as _t

log = logging.getLogger("EchoDesk.ModelProfile")

# ---------------------------------------------------------------------------
# [EN] GGUF general.file_type enum to readable quantization name, following the
# [EN] ggml llama_ftype definitions. Metadata stores a numeric value; displaying
# [EN] "15" directly would be meaningless to users.
# [ZH] GGUF general.file_type 枚举到可读量化名称的映射，依据 ggml 的
# [ZH] llama_ftype 定义。元数据存储的是数值，直接显示“15”对用户没有意义。
# ---------------------------------------------------------------------------
_FILE_TYPE_NAMES: Dict[int, str] = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M",
    16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S",
    22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M",
    28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
}

# [EN] Quantization marker in a file name. It often matches what the user sees
# [EN] more closely than the numeric metadata value.
# [ZH] 文件名中的量化标记。相比元数据中的数值，它通常更接近用户看到的文本。
_QUANT_IN_NAME = re.compile(
    r"(IQ\d+_[A-Z0-9]+|Q\d+_K_[SML]|Q\d+_K|Q\d+_\d|Q\d+|BF16|F16|F32)", re.IGNORECASE)

# [EN] Parameter-count marker in a file name: 0.8B, 7B, 1.5B, or 8x7B.
# [ZH] 文件名中的参数量标记：0.8B、7B、1.5B 或 8x7B。
_PARAMS_IN_NAME = re.compile(r"(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*B\b|(\d+(?:\.\d+)?)\s*B\b")


def _read_machine_memory() -> Tuple[Optional[float], int]:
    """
    [EN]
    Return (physical memory in GB, logical CPU count). Unavailable values are
    returned as (None, 0).

    [ZH]
    返回 (物理内存 GB, 逻辑 CPU 核数)。无法获取时返回 (None, 0)。
    """
    total_gb: Optional[float] = None
    # [EN] Standard library only; Windows uses GlobalMemoryStatusEx.
    # [ZH] 仅使用标准库；Windows 下使用 GlobalMemoryStatusEx。
    try:
        if os.name == "nt":
            import ctypes

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
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total_gb = status.ullTotalPhys / (1024 ** 3)
    # [EN] A failed probe must not interrupt the main flow.
    # [ZH] 探测失败不得中断主流程。
    except Exception:  # noqa: BLE001
        total_gb = None
    try:
        cores = os.cpu_count() or 0
    except Exception:  # noqa: BLE001
        cores = 0
    return total_gb, cores


def _fmt_params(count: Optional[float]) -> str:
    """
    [EN]
    Format a parameter count as 0.8B, 7B, or an empty string when unknown.

    [ZH]
    将参数量格式化为 0.8B、7B，未知时返回空串。
    """
    if not count or count <= 0:
        return ""
    if count >= 1e9:
        billions = count / 1e9
        return f"{billions:.2f}B" if billions < 10 else f"{billions:.0f}B"
    if count >= 1e6:
        return f"{count / 1e6:.0f}M"
    return f"{int(count)}"


def estimate_parameter_count(meta: Dict[str, Any]) -> Optional[int]:
    """
    [EN]
    Estimate the parameter count from token embeddings, per-layer attention and
    feed-forward weights, and the output layer.

    The GGUF-declared ``general.parameter_count`` is preferred. When it is
    absent, a standard Transformer structure is used; the result is an estimate
    and typically differs from the true count by a few percent.

    [ZH]
    根据 token 嵌入、每层注意力与前馈权重以及输出层估算参数量。

    优先使用 GGUF 声明的 ``general.parameter_count``。该字段缺失时按标准
    Transformer 结构估算；结果为估算值，通常与真实值相差数个百分点。
    """
    if not meta:
        return None
    declared = meta.get("general.parameter_count")
    if isinstance(declared, (int, float)) and declared > 0:
        return int(declared)
    arch = architecture(meta)

    def get(key: str, default: int = 0) -> int:
        value = meta.get(f"{arch}.{key}")
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    layers = get("block_count")
    n_embd = get("embedding_length")
    if not layers or not n_embd:
        return None
    heads = get("attention.head_count") or 0
    heads_kv = get("attention.head_count_kv") or heads
    key_len = get("attention.key_length")
    val_len = get("attention.value_length")
    head_dim = key_len or (n_embd // heads if heads else 0)
    val_dim = val_len or head_dim
    ffn = get("feed_forward_length") or int(4 * n_embd)
    vocab = get("vocab_size") or 0

    # [EN] Approximate q/k/v/o projection parameters.
    # [ZH] q/k/v/o 投影参数的近似计算。
    per_layer_attn = n_embd * head_dim + n_embd * val_dim + n_embd * n_embd
    per_layer_ffn = 3 * n_embd * ffn
    body = layers * (per_layer_attn + per_layer_ffn)
    embed = vocab * n_embd
    # [EN] Add the output layer once more when weights are untied. Qwen models
    # [EN] commonly use tied embeddings; using the conservative upper bound.
    # [ZH] 权重未绑定时再次计入输出层。Qwen 系列通常使用绑定嵌入；此处取保守上界。
    return int(embed + body + embed)


def quant_from_name(path: str) -> str:
    """
    [EN]
    Infer the quantization marker from a file name. The marker is usually more
    recognizable to users than the numeric metadata value.

    [ZH]
    从文件名推断量化标记。相比元数据中的数值，该标记通常更容易被用户识别。
    """
    if not path:
        return ""
    match = _QUANT_IN_NAME.search(os.path.basename(path))
    return match.group(1).upper() if match else ""


def params_from_name(path: str) -> Optional[float]:
    """
    [EN]
    Infer the parameter count from a file name (for example,
    Qwen3.5-0.8B becomes 0.8e9).

    [ZH]
    从文件名推断参数量（例如 Qwen3.5-0.8B 转为 0.8e9）。
    """
    if not path:
        return None
    match = _PARAMS_IN_NAME.search(os.path.basename(path))
    if not match:
        return None
    # [EN] MoE notation such as 8x7B.
    # [ZH] 8x7B 这类 MoE 标记。
    if match.group(1) and match.group(2):
        return float(match.group(1)) * float(match.group(2)) * 1e9
    return float(match.group(3)) * 1e9


@dataclass
class ModelProfile:
    """
    [EN]
    Complete capability profile of one model, shared by ``/status``, the
    settings window, and capability warnings.

    [ZH]
    单个模型的完整能力画像，供 ``/status``、设置窗口与能力提示共用。
    """

    path: str = ""
    name: str = ""
    architecture: str = ""
    parameters: Optional[int] = None
    # [EN] Source of the parameter count: metadata, filename inference, or unknown.
    # [ZH] 参数量来源：元数据、文件名推断或未知。
    parameters_source: str = ""
    quantization: str = ""
    quantization_source: str = ""
    train_context: int = 0
    usable_context: int = 0
    kv_cache_bytes: Optional[int] = None
    n_threads: int = 0
    n_gpu_layers: int = 0
    ram_gb: Optional[float] = None
    cpu_cores: int = 0
    layers: Optional[int] = None
    head_count_kv: Optional[int] = None
    last_tps: float = 0.0
    last_tokens: int = 0
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """
        [EN]
        Populate interface labels such as "unknown" using the active locale.

        ``_t(...)`` must not be used as a dataclass default: defaults are
        evaluated at module import time, which would freeze the locale and
        prevent later interface language changes from taking effect.

        [ZH]
        使用当前语言填充“未知”等界面文案。

        不得将 ``_t(...)`` 用作 dataclass 默认值：默认值在模块导入时求值，
        会把语言固定在导入时刻，导致后续界面语言切换无法生效。
        """
        if not self.parameters_source:
            self.parameters_source = _t("capability.unknown")
        if not self.quantization_source:
            self.quantization_source = _t("capability.unknown")

    # [EN] ---------- Display ----------
    # [ZH] ---------- 展示 ----------

    def params_text(self) -> str:
        text = _fmt_params(float(self.parameters) if self.parameters else None)
        return text or _t("capability.unknown")

    def kv_text(self) -> str:
        return format_bytes(self.kv_cache_bytes)

    def speed_text(self) -> str:
        if self.last_tokens <= 0 or self.last_tps <= 0:
            return _t("capability.speed_unknown")
        return _t("capability.speed_text", tps=f"{self.last_tps:.1f}", tokens=self.last_tokens)

    def context_usage_hint(self) -> str:
        """
        [EN]
        Explain the relationship between the training context and the usable
        context, including why the effective window is smaller.

        [ZH]
        说明训练上下文与实际可用上下文之间的关系，并解释有效窗口为何更小。
        """
        if not self.train_context:
            return ""
        if self.usable_context >= self.train_context:
            return _t("capability.context_full")
        return _t("capability.context_partial", usable=self.usable_context,
                  train=self.train_context,
                  percent=self.usable_context * 100 // max(1, self.train_context))

    def public(self) -> Dict[str, Any]:
        """
        [EN]
        Return machine-readable fields. The frontend must not parse localized
        display text.

        [ZH]
        返回机器可读字段。前端不得解析本地化显示文本。
        """
        return {
            "name": self.name,
            "path": self.path,
            "architecture": self.architecture,
            "parameters": self.parameters,
            "parameters_text": self.params_text(),
            "parameters_source": self.parameters_source,
            "quantization": self.quantization,
            "quantization_source": self.quantization_source,
            "train_context": self.train_context,
            "usable_context": self.usable_context,
            "kv_cache_bytes": self.kv_cache_bytes,
            "kv_cache_text": self.kv_text(),
            "n_threads": self.n_threads,
            "n_gpu_layers": self.n_gpu_layers,
            "ram_gb": round(self.ram_gb, 1) if self.ram_gb else None,
            "cpu_cores": self.cpu_cores,
            "layers": self.layers,
            "head_count_kv": self.head_count_kv,
            "last_tps": round(self.last_tps, 2),
            "last_tokens": self.last_tokens,
            "speed_text": self.speed_text(),
            "warnings": list(self.warnings),
        }


def build_profile(engine: Any, cfg: Any = None, model_path: str = "") -> ModelProfile:
    """
    [EN]
    Build a capability profile from the inference engine and configuration.
    Partial information is returned when the engine is unavailable.

    [ZH]
    从推理引擎与配置汇总能力画像。引擎不可用时仍返回部分信息。
    """
    path = model_path or str(getattr(engine, "model_path", "") or "")
    profile = ModelProfile(path=path, name=os.path.basename(path))
    meta = read_metadata(path) if path and os.path.isfile(path) else {}
    profile.architecture = architecture(meta) if meta else _t("capability.unknown")

    # [EN] Parameter count: metadata first, then filename inference. The source
    # [EN] is recorded in both cases.
    # [ZH] 参数量：优先元数据，其次文件名推断；两种情况均记录来源。
    declared = estimate_parameter_count(meta)
    if declared:
        profile.parameters = declared
        profile.parameters_source = _t("capability.source_gguf")
    else:
        guessed = params_from_name(path)
        if guessed:
            profile.parameters = int(guessed)
            profile.parameters_source = _t("capability.source_filename")

    # [EN] Quantization: convert the numeric file_type enum to a name; fall back
    # [EN] to the filename when conversion is not possible.
    # [ZH] 量化：将数值 file_type 枚举转换为名称；无法转换时回退到文件名。
    file_type = meta.get("general.file_type") if meta else None
    if isinstance(file_type, (int, float)) and int(file_type) in _FILE_TYPE_NAMES:
        profile.quantization = _FILE_TYPE_NAMES[int(file_type)]
        profile.quantization_source = _t("capability.source_gguf")
    else:
        by_name = quant_from_name(path)
        if by_name:
            profile.quantization = by_name
            profile.quantization_source = _t("capability.source_filename")

    profile.train_context = int(getattr(engine, "n_ctx_train", 0) or 0) or \
        (resolve_context_window(path, default=0) if path else 0)
    profile.usable_context = int(getattr(engine, "n_ctx", 0) or 0)
    profile.kv_cache_bytes = estimate_kv_cache_bytes(path, profile.usable_context) if path else None
    profile.n_threads = int(getattr(engine, "n_threads", 0) or 0)
    profile.n_gpu_layers = int(getattr(engine, "n_gpu_layers", 0) or 0)
    profile.last_tps = float(getattr(engine, "last_tps", 0.0) or 0.0)
    profile.last_tokens = int(getattr(engine, "last_tokens", 0) or 0)
    profile.layers = meta.get(f"{profile.architecture}.block_count") if meta else None
    profile.head_count_kv = meta.get(f"{profile.architecture}.attention.head_count_kv") if meta else None

    ram_gb, cores = _read_machine_memory()
    profile.ram_gb = ram_gb
    profile.cpu_cores = cores
    profile.warnings = capability_warnings(profile, cfg)
    return profile


def capability_warnings(profile: ModelProfile, cfg: Any = None) -> List[str]:
    """
    [EN]
    Return warnings for configurations that exceed model or machine capacity.
    An empty list means no warning applies.

    The function reports facts and does not modify configuration; users can
    only make an informed choice when the cause is visible. The return value is
    a list of strings consumed by the frontend capability panel,
    ``command_handler``, and ``ModelProfile.public()["warnings"]``. Each string
    is resolved from the language pack under the ``capability.warn_*`` keys.

    [ZH]
    返回模型或机器能力超出限制时的提示清单。空列表表示没有告警。

    本函数只如实报告，不擅自修改配置；只有原因可见，用户才能做出知情选择。
    返回值为字符串列表，由前端能力面板、``command_handler`` 与
    ``ModelProfile.public()["warnings"]`` 直接消费。每条文案通过
    ``capability.warn_*`` 键从语言包解析。
    """
    notes: List[str] = []

    # [EN] 1) KV cache exceeds a reasonable fraction of available memory.
    # [ZH] 1) KV Cache 超出可用内存的合理比例。
    if profile.kv_cache_bytes and profile.ram_gb:
        # [EN] Leave half for the system and the model weights.
        # [ZH] 留一半给系统与模型权重。
        limit = profile.ram_gb * 1024 ** 3 * 0.5
        if profile.kv_cache_bytes > limit:
            notes.append(_t("capability.warn_kv_over_ram", kv=profile.kv_text(),
                            ram=f"{profile.ram_gb:.1f}"))

    # [EN] 2) Usable context is much smaller than the training context.
    # [ZH] 2) 实际可用上下文远小于训练上下文。
    if profile.train_context and profile.usable_context:
        if profile.usable_context < profile.train_context * 0.5:
            notes.append(_t("capability.warn_small_window",
                            percent=profile.usable_context * 100 // profile.train_context,
                            usable=profile.usable_context, train=profile.train_context))

    # [EN] 3) CPU-only execution with a large parameter count.
    # [ZH] 3) 纯 CPU 运行且参数量较大。
    if profile.n_gpu_layers == 0 and profile.parameters and profile.parameters > 4e9:
        notes.append(_t("capability.warn_cpu_only", params=profile.params_text()))

    # [EN] 4) Low generation throughput; only reported when measured data exists.
    # [ZH] 4) 生成吞吐偏低；仅在存在验证数据时提示。
    min_tps = float(getattr(cfg, "speed_min_tps", 3.0) or 3.0)
    if profile.last_tokens >= 64 and 0 < profile.last_tps < min_tps:
        notes.append(_t("capability.warn_slow", tps=f"{profile.last_tps:.1f}",
                        min_tps=f"{min_tps:.0f}"))

    # [EN] 5) Thread count is zero, meaning automatic selection.
    # [ZH] 5) 线程数为 0，表示自动选择。
    if profile.n_threads == 0 and profile.cpu_cores:
        notes.append(_t("capability.warn_threads_auto", cores=profile.cpu_cores))

    return notes


def recommend_config(ram_gb: Optional[float], cpu_cores: int) -> Dict[str, Any]:
    """
    [EN]
    Recommend model size, quantization, thread count, and context length from
    available memory and CPU cores.

    The rules are empirical starting points, not hard conclusions. The returned
    ``reasons`` list is shown alongside the recommendation so that users can see
    the basis for it. Like capability warnings, ``reasons`` is a list of strings
    resolved from the language pack under the ``capability.reason_*`` keys.

    [ZH]
    根据可用内存与 CPU 核数推荐模型规模、量化等级、线程数与上下文长度。

    规则为经验性起点，而非硬性结论。返回的 ``reasons`` 列表会与推荐结果一同
    显示，使用户能够看到推荐依据。与能力提示一致，``reasons`` 是字符串列表，
    通过 ``capability.reason_*`` 键从语言包解析。
    """
    ram = ram_gb or 0.0
    cores = cpu_cores or 0
    reasons: List[str] = []

    if ram >= 30:
        params, quant, ctx = "7B", "Q4_K_M", 16384
        reasons.append(_t("capability.reason_ram_high"))
    elif ram >= 15:
        params, quant, ctx = "3B", "Q4_K_M", 8192
        reasons.append(_t("capability.reason_ram_mid"))
    elif ram >= 7:
        params, quant, ctx = "1.5B", "Q4_K_M", 8192
        reasons.append(_t("capability.reason_ram_low"))
    else:
        params, quant, ctx = "0.8B", "Q4_K_M", 4096
        reasons.append(_t("capability.reason_ram_min"))

    threads = max(1, cores - 1) if cores > 1 else (cores or 0)
    if threads:
        reasons.append(_t("capability.reason_threads", cores=cores, threads=threads))
    else:
        reasons.append(_t("capability.reason_threads_unknown"))

    if params in ("7B", "3B"):
        reasons.append(_t("capability.reason_params_tradeoff"))

    return {
        "params": params,
        "quantization": quant,
        "context_length": ctx,
        "n_threads": threads,
        "n_batch": 512,
        "ram_gb": round(ram, 1) if ram else None,
        "cpu_cores": cores,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# [EN] Generation presets: light, balanced, and quality modes.
# [ZH] 生成参数预设：轻量、均衡与高质量三档。
# ---------------------------------------------------------------------------
#: [EN] Preset table. It stores generation parameters and language keys for
#: [EN] labels and notes. Labels and notes are interface text displayed by
#: [EN] ``/preset list`` and the settings selector, but a module-level dictionary
#: [EN] is evaluated at import time. Storing keys and resolving them in
#: [EN] :func:`apply_preset` and :func:`preset_menu` avoids freezing the locale.
#: [ZH] 预设表。保存生成参数以及标签与说明的语言键。标签与说明是由
#: [ZH] ``/preset list`` 与设置界面下拉框显示的界面文本，但模块级字典在导入时
#: [ZH] 求值；保存键名并在 :func:`apply_preset` 与 :func:`preset_menu` 中解析，
#: [ZH] 可避免语言被固定在导入时刻。
PRESETS: Dict[str, Dict[str, Any]] = {
    "light": {
        "temperature": 0.3, "top_p": 0.8, "repeat_penalty": 1.05,
        "max_tokens": 512,
        "label_key": "capability.preset_light_label",
        "note_key": "capability.preset_light_note",
    },
    "balanced": {
        "temperature": 0.7, "top_p": 0.9, "repeat_penalty": 1.1,
        "max_tokens": 1024,
        "label_key": "capability.preset_balanced_label",
        "note_key": "capability.preset_balanced_note",
    },
    "quality": {
        "temperature": 0.4, "top_p": 0.95, "repeat_penalty": 1.15,
        "max_tokens": 2048,
        "label_key": "capability.preset_quality_label",
        "note_key": "capability.preset_quality_note",
    },
}


def _preset_view(key: str, value: Dict[str, Any]) -> Dict[str, Any]:
    """
    [EN]
    Return a displayable copy of a preset with labels and notes resolved in the
    active locale.

    [ZH]
    返回预设的可显示副本，其中标签与说明按当前语言解析。
    """
    out = {k: v for k, v in value.items() if k not in ("label_key", "note_key")}
    out["id"] = key
    out["label"] = _t(value["label_key"])
    out["note"] = _t(value["note_key"])
    return out


def apply_preset(name: str) -> Optional[Dict[str, Any]]:
    """
    [EN]
    Return preset parameters, or ``None`` for an unknown name. The returned
    labels and notes are localized.

    [ZH]
    返回预设参数；名称未知时返回 ``None``。返回值中的标签与说明已完成本地化。
    """
    key = (name or "").strip().lower()
    if key not in PRESETS:
        return None
    return _preset_view(key, PRESETS[key])


def preset_menu() -> List[Dict[str, Any]]:
    """
    [EN]
    Return the preset list for ``/preset list`` and the settings selector.

    [ZH]
    返回预设清单，供 ``/preset list`` 与设置窗口下拉框使用。
    """
    return [_preset_view(key, value) for key, value in PRESETS.items()]


# ---------------------------------------------------------------------------
# [EN] Chunk-summary and parsed-file cache
# [ZH] 分段摘要与文件解析缓存
# ---------------------------------------------------------------------------


class SummaryCache:
    """
    [EN]
    Cache chunk summaries by file fingerprint and summary parameters, so that
    uploading the same file twice does not repeat the work.

    The fingerprint combines path, size, modification time, and summary
    parameters; it remains valid as long as the file content is unchanged. The
    cache is human-readable JSON and can be deleted directly; ``/clearcache``
    also clears it.

    [ZH]
    按文件指纹与摘要参数缓存分段摘要，使同一文件二次上传时不重复计算。

    指纹由路径、大小、修改时间与摘要参数组成；文件内容不变时指纹保持有效。
    缓存为可读 JSON，可直接删除；``/clearcache`` 也会清空缓存。
    """

    def __init__(self, path: str, limit: int = 32) -> None:
        self.path = os.path.abspath(path) if path else ""
        self.limit = max(1, int(limit))
        self._data: Dict[str, Any] = self._load()

    # [EN] ---------- Read/write ----------
    # [ZH] ---------- 读写 ----------

    def _load(self) -> Dict[str, Any]:
        if not self.path:
            return {"schema": 1, "entries": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"schema": 1, "entries": {}}

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # [EN] Keep only the most recently used entries to bound cache size.
            # [ZH] 仅保留最近使用的若干条目，以限制缓存大小。
            entries = self._data.get("entries") or {}
            if len(entries) > self.limit:
                ordered = sorted(entries.items(),
                                 key=lambda kv: float(kv[1].get("used_at") or 0), reverse=True)
                self._data["entries"] = dict(ordered[: self.limit])
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=1)
        except OSError:
            log.debug("摘要缓存写入失败（忽略）", exc_info=True)

    # [EN] ---------- Fingerprint and query ----------
    # [ZH] ---------- 指纹与查询 ----------

    @staticmethod
    def fingerprint(file_path: str, params: Dict[str, Any]) -> str:
        """
        [EN]
        Build a file fingerprint from path, size, modification time, and summary
        parameters.

        [ZH]
        根据路径、大小、修改时间与摘要参数生成文件指纹。
        """
        try:
            stat = os.stat(file_path)
            stamp = f"{os.path.abspath(file_path)}|{stat.st_size}|{int(stat.st_mtime)}"
        except OSError:
            stamp = f"{os.path.abspath(file_path)}|missing"
        payload = stamp + "|" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def get(self, file_path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        [EN]
        Return the cached entry (text, chunk_count, origin_chars) on a hit, or
        ``None`` on a miss.

        [ZH]
        命中时返回缓存条目（text、chunk_count、origin_chars）；未命中时返回
        ``None``。
        """
        key = self.fingerprint(file_path, params)
        entry = (self._data.get("entries") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        entry["used_at"] = time.time()
        entry["hits"] = int(entry.get("hits") or 0) + 1
        return entry

    def put(self, file_path: str, params: Dict[str, Any], text: str,
            chunk_count: int = 0, origin_chars: int = 0) -> None:
        """
        [EN]
        Write a cache entry without immediately persisting it; the caller
        decides when to save.

        [ZH]
        写入缓存条目但不立即落盘；由调用方决定何时保存。
        """
        key = self.fingerprint(file_path, params)
        entries = self._data.setdefault("entries", {})
        entries[key] = {
            "file": os.path.basename(file_path),
            "text": text,
            "chunk_count": int(chunk_count),
            "origin_chars": int(origin_chars),
            "created_at": time.time(),
            "used_at": time.time(),
            "hits": 0,
        }

    def clear(self) -> int:
        """
        [EN]
        Clear the cache and return the number of removed entries.

        [ZH]
        清空缓存并返回移除的条目数。
        """
        count = len(self._data.get("entries") or {})
        self._data = {"schema": 1, "entries": {}}
        self.save()
        return count

    def stats(self) -> Dict[str, Any]:
        entries = self._data.get("entries") or {}
        return {
            "path": self.path,
            "entries": len(entries),
            "hits": sum(int(e.get("hits") or 0) for e in entries.values() if isinstance(e, dict)),
            "limit": self.limit,
        }
