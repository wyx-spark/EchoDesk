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
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

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
    llama-cpp-python expects. An unknown name falls back to Q8_0, which is the Reader's
    hard baseline, rather than failing the model load.

    [ZH]
    把 KV Cache 类型名（``q8_0``）或字面枚举值转换成 llama-cpp-python 期望的整数。
    未知名称回退到 Q8_0（Reader 的硬性基线），而不是让模型加载失败。
    """
    if isinstance(name, int):
        return int(name)
    key = str(name or "").strip().lower()
    if key in GGML_TYPE_VALUES:
        return GGML_TYPE_VALUES[key]
    try:
        from llama_cpp._ggml import GGMLType  # noqa: PLC0415 - optional native import

        member = getattr(GGMLType, "GGML_TYPE_" + key.upper(), None)
        if member is not None:
            return int(member)
    except Exception:  # noqa: BLE001 - fall back to the documented ordinal
        pass
    return GGML_TYPE_VALUES["q8_0"]


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
    # [EN] Token budget reserved for the answer, plus the safety margin added on top of
    # [EN] the document body when the window is allocated.
    # [ZH] 为回答预留的 token 预算，以及分配窗口时在文档正文之外追加的安全余量。
    "output_token_budget": 4096,
    "context_safety_margin": 512,
    # [EN] Auto-tuning policy: the fixed long-text workload, the trial grid sizes and
    # [EN] the improvement threshold below which a candidate is rejected as noise.
    # [ZH] 自动调优策略：固定的长文本工作负载、候选网格规模，以及判定"候选只是噪声"
    # [ZH] 的提升阈值。
    "tuning": {
        "prompt_tokens": 512,
        "gen_tokens": 128,
        "repeats": 2,
        "min_improvement_ratio": 0.05,
        "max_trials": 8,
    },
    # [EN] Bookkeeping written by the tuner: when it last ran and what it measured.
    # [ZH] 由调优模块写入的记账信息：最后一次运行时间与实测结果。
    "tuned_at": "",
    "tuned_model": "",
    "tuned_baseline": {},
    "tuned_best": {},
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
    use_mlock: bool = True
    use_mmap: bool = False
    flash_attn: bool = True
    type_k: str = "q8_0"
    type_v: str = "q8_0"
    n_gpu_layers: int = 0
    n_ctx_default: int = 2048
    n_ctx_max: int = 131072
    output_token_budget: int = 4096
    context_safety_margin: int = 512
    tuning: Dict[str, Any] = field(default_factory=dict)
    tuned_at: str = ""
    tuned_model: str = ""
    tuned_baseline: Dict[str, Any] = field(default_factory=dict)
    tuned_best: Dict[str, Any] = field(default_factory=dict)
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

        tuning = merged.get("tuning")
        return cls(
            n_threads=as_int("n_threads"),
            n_batch=max(1, as_int("n_batch")),
            n_ubatch=max(1, as_int("n_ubatch")),
            use_mlock=as_bool("use_mlock"),
            use_mmap=as_bool("use_mmap"),
            flash_attn=as_bool("flash_attn"),
            type_k=str(merged.get("type_k") or "q8_0").strip().lower(),
            type_v=str(merged.get("type_v") or "q8_0").strip().lower(),
            n_gpu_layers=as_int("n_gpu_layers"),
            n_ctx_default=max(1, as_int("n_ctx_default")),
            n_ctx_max=max(1, as_int("n_ctx_max")),
            output_token_budget=max(0, as_int("output_token_budget")),
            context_safety_margin=max(0, as_int("context_safety_margin")),
            tuning=tuning if isinstance(tuning, dict) else {},
            tuned_at=str(merged.get("tuned_at") or ""),
            tuned_model=str(merged.get("tuned_model") or ""),
            tuned_baseline=(merged.get("tuned_baseline")
                            if isinstance(merged.get("tuned_baseline"), dict) else {}),
            tuned_best=(merged.get("tuned_best")
                        if isinstance(merged.get("tuned_best"), dict) else {}),
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
            "use_mlock": bool(self.use_mlock),
            "use_mmap": bool(self.use_mmap),
            "flash_attn": bool(self.flash_attn),
            "type_k": str(self.type_k),
            "type_v": str(self.type_v),
            "n_gpu_layers": int(self.n_gpu_layers),
            "n_ctx_default": int(self.n_ctx_default),
            "n_ctx_max": int(self.n_ctx_max),
            "output_token_budget": int(self.output_token_budget),
            "context_safety_margin": int(self.context_safety_margin),
            "tuning": dict(self.tuning),
            "tuned_at": str(self.tuned_at),
            "tuned_model": str(self.tuned_model),
            "tuned_baseline": dict(self.tuned_baseline),
            "tuned_best": dict(self.tuned_best),
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

    def llama_kwargs(self) -> Dict[str, Any]:
        """
        [EN]
        The construction keyword arguments handed to ``llama_cpp.Llama``. ``n_ctx`` and
        ``model_path`` are supplied by the caller because they vary per document.

        [ZH]
        交给 ``llama_cpp.Llama`` 的构建关键字参数。``n_ctx`` 与 ``model_path`` 由调用方
        提供，因为它们随文档变化。
        """
        return {
            "n_threads": self.resolve_n_threads(),
            "n_batch": int(self.n_batch),
            "n_ubatch": int(self.n_ubatch),
            "n_gpu_layers": int(self.n_gpu_layers),
            "use_mlock": bool(self.use_mlock),
            "use_mmap": bool(self.use_mmap),
            "flash_attn": bool(self.flash_attn),
            "type_k": ggml_type_value(self.type_k),
            "type_v": ggml_type_value(self.type_v),
        }

    def describe(self) -> Dict[str, Any]:
        """
        [EN]
        Machine-readable snapshot for the protocol (``inference_config`` action and the
        ``ready`` line).
        [ZH]
        供协议使用的机器可读快照（``inference_config`` 动作与 ``ready`` 行）。
        """
        return {
            "path": self.path,
            "n_threads": self.resolve_n_threads(),
            "n_threads_configured": int(self.n_threads),
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
            "output_token_budget": int(self.output_token_budget),
            "context_safety_margin": int(self.context_safety_margin),
            "tuned_at": str(self.tuned_at),
            "tuned_model": str(self.tuned_model),
            "tuning": dict(self.tuning),
        }

    def log_line(self) -> str:
        """
        [EN]
        The one-line summary required by the acceptance criteria, for example:
        ``Inference engine loaded with n_threads=16, n_batch=512, kv_cache=q8_0,
        mlock=true, flash_attn=true``.

        [ZH]
        验收要求的一行摘要，例如：
        ``Inference engine loaded with n_threads=16, n_batch=512, kv_cache=q8_0,
        mlock=true, flash_attn=true``。
        """
        return (
            "Inference engine loaded with n_threads=%d, n_batch=%d, kv_cache=%s, "
            "mlock=%s, flash_attn=%s"
            % (self.resolve_n_threads(), int(self.n_batch), str(self.type_k),
               "true" if self.use_mlock else "false",
               "true" if self.flash_attn else "false")
        )
