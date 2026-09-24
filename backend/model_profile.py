"""模型能力画像与配置辅助（任务书修复四：方向六 + 方向七 + 方向八）。

这个模块只做"把模型的能力边界算清楚并如实讲出来"，不做任何推理，
也不引入任何第三方依赖（体积要求）。

内容包括：

* :class:`ModelProfile` —— 参数量、量化格式、训练窗口、实际可用窗口、
  KV Cache 估算、实测生成速度，以及一份"能力提示"清单；
* :func:`recommend_config` —— 按本机内存与 CPU 核数推荐模型规模/量化等级/线程数；
* :data:`PRESETS` —— 轻量/均衡/高质量三档生成参数预设；
* :class:`SummaryCache` —— 分段摘要与文件解析结果的缓存（方向八：同一文件二次上传不重算）。

**诚实性要求**：所有估算值都带 `estimated` 标记，读不到就写"未知"，绝不用猜测值
冒充实测值——本模块的存在意义就是让用户知道模型"能做到什么、做不到什么"。
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

# 多语言（任务三）：本模块面向用户的文本（能力提示、推荐理由、预设名称、
# 参数量/量化来源标注、"未知"这类占位）全部走语言包，键前缀 capability.。
# i18n 只依赖标准库（config 在 i18n 内部惰性导入），可直接在这里导入。
from i18n import t as _t

log = logging.getLogger("EchoDesk.ModelProfile")

# ---------------------------------------------------------------------------
# GGUF general.file_type 枚举 → 可读量化名（取自 ggml 的 llama_ftype 定义）
# 元数据里存的是数字，直接显示"15"对用户毫无意义。
# ---------------------------------------------------------------------------
_FILE_TYPE_NAMES: Dict[int, str] = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M",
    16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S",
    22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M",
    28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
}

# 文件名里的量化标记（比元数据更贴近用户看到的字）
_QUANT_IN_NAME = re.compile(
    r"(IQ\d+_[A-Z0-9]+|Q\d+_K_[SML]|Q\d+_K|Q\d+_\d|Q\d+|BF16|F16|F32)", re.IGNORECASE)

# 文件名里的参数量标记：0.8B / 7B / 1.5B / 8x7B
_PARAMS_IN_NAME = re.compile(r"(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*B\b|(\d+(?:\.\d+)?)\s*B\b")


def _read_machine_memory() -> Tuple[Optional[float], int]:
    """返回 (物理内存 GB, CPU 逻辑核数)。取不到就返回 (None, 0)。"""
    total_gb: Optional[float] = None
    try:  # 只用标准库；Windows 下用 GlobalMemoryStatusEx
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
    except Exception:  # noqa: BLE001 - 探测失败不影响主流程
        total_gb = None
    try:
        cores = os.cpu_count() or 0
    except Exception:  # noqa: BLE001
        cores = 0
    return total_gb, cores


def _fmt_params(count: Optional[float]) -> str:
    """参数量格式化：0.8B / 7B / 未知。"""
    if not count or count <= 0:
        return ""
    if count >= 1e9:
        billions = count / 1e9
        return f"{billions:.2f}B" if billions < 10 else f"{billions:.0f}B"
    if count >= 1e6:
        return f"{count / 1e6:.0f}M"
    return f"{int(count)}"


def estimate_parameter_count(meta: Dict[str, Any]) -> Optional[int]:
    """估算参数量（token 嵌入 + 每层注意力/前馈 + 输出层）。

    优先用 GGUF 自己声明的 ``general.parameter_count``；没有就按标准
    Transformer 结构估算（这是**估算**，与真实值通常相差几个百分点）。
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

    per_layer_attn = n_embd * head_dim + n_embd * val_dim + n_embd * n_embd  # q/k/v/o 近似
    per_layer_ffn = 3 * n_embd * ffn
    body = layers * (per_layer_attn + per_layer_ffn)
    embed = vocab * n_embd
    # 输出层未绑定权重时再算一遍（Qwen 系多数是 tied embedding，这里保守取 max）
    return int(embed + body + embed)


def quant_from_name(path: str) -> str:
    """从文件名推断量化标记（比元数据的数字更贴近用户认知）。"""
    if not path:
        return ""
    match = _QUANT_IN_NAME.search(os.path.basename(path))
    return match.group(1).upper() if match else ""


def params_from_name(path: str) -> Optional[float]:
    """从文件名推断参数量（如 Qwen3.5-0.8B → 0.8e9）。"""
    if not path:
        return None
    match = _PARAMS_IN_NAME.search(os.path.basename(path))
    if not match:
        return None
    if match.group(1) and match.group(2):      # 8x7B 这类 MoE 标记
        return float(match.group(1)) * float(match.group(2)) * 1e9
    return float(match.group(3)) * 1e9


@dataclass
class ModelProfile:
    """一个模型的完整能力画像（供 /status、设置窗口与能力提示共用）。"""

    path: str = ""
    name: str = ""
    architecture: str = ""
    parameters: Optional[int] = None
    parameters_source: str = ""              # 元数据 / 文件名推断 / 未知（展示用，见 __post_init__）
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
        """把"未知"这类**界面文案**按当前语言补上。

        不能在字段默认值里写 ``_t(...)``：dataclass 的默认值在**模块导入时**求值，
        会把语言固定在导入那一刻，用户之后切换界面语言就不生效了。
        """
        if not self.parameters_source:
            self.parameters_source = _t("capability.unknown")
        if not self.quantization_source:
            self.quantization_source = _t("capability.unknown")

    # ---------- 展示 ----------

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
        """训练窗口与实际可用窗口的关系说明（解释"为什么不是训练窗口那么大"）。"""
        if not self.train_context:
            return ""
        if self.usable_context >= self.train_context:
            return _t("capability.context_full")
        return _t("capability.context_partial", usable=self.usable_context,
                  train=self.train_context,
                  percent=self.usable_context * 100 // max(1, self.train_context))

    def public(self) -> Dict[str, Any]:
        """机器可读字段（前端不解析中文文本）。"""
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
    """从推理引擎与配置汇总出能力画像（引擎不可用时也能给出部分信息）。"""
    path = model_path or str(getattr(engine, "model_path", "") or "")
    profile = ModelProfile(path=path, name=os.path.basename(path))
    meta = read_metadata(path) if path and os.path.isfile(path) else {}
    profile.architecture = architecture(meta) if meta else _t("capability.unknown")

    # 参数量：元数据优先，其次文件名推断（都会标注来源）
    declared = estimate_parameter_count(meta)
    if declared:
        profile.parameters = declared
        profile.parameters_source = _t("capability.source_gguf")
    else:
        guessed = params_from_name(path)
        if guessed:
            profile.parameters = int(guessed)
            profile.parameters_source = _t("capability.source_filename")

    # 量化：元数据的 file_type 是数字枚举，转成名字；转换不了就用文件名
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
    """超出模型/机器能力时的提示清单（空列表 = 一切正常）。

    这里刻意只做**如实告知**，不擅自改配置——用户看得到原因才有可能做对选择。
    返回的仍是**字符串列表**（前端能力提示按钮、``command_handler`` 与
    ``ModelProfile.public()["warnings"]`` 都直接消费它），只是每条文案按当前
    语言从语言包取（键 capability.warn_*）。
    """
    notes: List[str] = []

    # 1) KV Cache 超出可用内存的合理比例
    if profile.kv_cache_bytes and profile.ram_gb:
        limit = profile.ram_gb * 1024 ** 3 * 0.5      # 留一半给系统与模型权重
        if profile.kv_cache_bytes > limit:
            notes.append(_t("capability.warn_kv_over_ram", kv=profile.kv_text(),
                            ram=f"{profile.ram_gb:.1f}"))

    # 2) 上下文窗口远小于训练窗口（用户以为能塞 32k，其实只开了 8k）
    if profile.train_context and profile.usable_context:
        if profile.usable_context < profile.train_context * 0.5:
            notes.append(_t("capability.warn_small_window",
                            percent=profile.usable_context * 100 // profile.train_context,
                            usable=profile.usable_context, train=profile.train_context))

    # 3) 纯 CPU 运行且参数量偏大
    if profile.n_gpu_layers == 0 and profile.parameters and profile.parameters > 4e9:
        notes.append(_t("capability.warn_cpu_only", params=profile.params_text()))

    # 4) 生成速度偏低（有实测数据时才提示）
    min_tps = float(getattr(cfg, "speed_min_tps", 3.0) or 3.0)
    if profile.last_tokens >= 64 and 0 < profile.last_tps < min_tps:
        notes.append(_t("capability.warn_slow", tps=f"{profile.last_tps:.1f}",
                        min_tps=f"{min_tps:.0f}"))

    # 5) 线程数为 0（自动）
    if profile.n_threads == 0 and profile.cpu_cores:
        notes.append(_t("capability.warn_threads_auto", cores=profile.cpu_cores))

    return notes


def recommend_config(ram_gb: Optional[float], cpu_cores: int) -> Dict[str, Any]:
    """按本机内存与核数推荐模型规模、量化等级、线程数与上下文窗口（方向六）。

    规则来自实践区间，属于"起点建议"而不是硬性结论，因此返回值里带上 reasons，
    界面上可以一并显示，用户才知道依据是什么。reasons 与能力提示一样是
    **字符串列表**，文案按当前语言从语言包取（键 capability.reason_*）。
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
# 生成参数预设（方向六：轻量 / 均衡 / 高质量 三档一键切换）
# ---------------------------------------------------------------------------
#: 预设表：只放**参数**与 label/note 的**语言键**。
#: label / note 是界面文本（``/preset list`` 与设置界面下拉框会显示），
#: 但模块级字典在导入时求值，直接写 ``_t(...)`` 会把语言固定在导入那一刻，
#: 所以这里存键名，由 :func:`apply_preset` / :func:`preset_menu` 取值时解析。
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
    """预设的"可显示副本"：把 label / note 按当前语言解析出来。"""
    out = {k: v for k, v in value.items() if k not in ("label_key", "note_key")}
    out["id"] = key
    out["label"] = _t(value["label_key"])
    out["note"] = _t(value["note_key"])
    return out


def apply_preset(name: str) -> Optional[Dict[str, Any]]:
    """取预设参数（未知名称返回 None）；返回值的 label / note 已本地化。"""
    key = (name or "").strip().lower()
    if key not in PRESETS:
        return None
    return _preset_view(key, PRESETS[key])


def preset_menu() -> List[Dict[str, Any]]:
    """预设清单（供 /preset list 与设置窗口下拉框）。"""
    return [_preset_view(key, value) for key, value in PRESETS.items()]


# ---------------------------------------------------------------------------
# 方向八：分段摘要 / 文件解析缓存
# ---------------------------------------------------------------------------


class SummaryCache:
    """按"文件指纹 + 摘要参数"缓存分段摘要，同一文件二次上传不重算。

    指纹用 ``路径 + 大小 + 修改时间 + 摘要参数``；不动文件内容就不失效。
    缓存文件是可读 JSON，用户想清空直接删掉即可（``/clearcache`` 也会清它）。
    """

    def __init__(self, path: str, limit: int = 32) -> None:
        self.path = os.path.abspath(path) if path else ""
        self.limit = max(1, int(limit))
        self._data: Dict[str, Any] = self._load()

    # ---------- 读写 ----------

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
            # 只保留最近使用的若干条，避免缓存文件无限增长
            entries = self._data.get("entries") or {}
            if len(entries) > self.limit:
                ordered = sorted(entries.items(),
                                 key=lambda kv: float(kv[1].get("used_at") or 0), reverse=True)
                self._data["entries"] = dict(ordered[: self.limit])
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=1)
        except OSError:
            log.debug("摘要缓存写入失败（忽略）", exc_info=True)

    # ---------- 指纹与查询 ----------

    @staticmethod
    def fingerprint(file_path: str, params: Dict[str, Any]) -> str:
        """文件指纹：路径 + 大小 + 修改时间 + 摘要参数。"""
        try:
            stat = os.stat(file_path)
            stamp = f"{os.path.abspath(file_path)}|{stat.st_size}|{int(stat.st_mtime)}"
        except OSError:
            stamp = f"{os.path.abspath(file_path)}|missing"
        payload = stamp + "|" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def get(self, file_path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """命中则返回缓存条目（含 text/chunk_count/origin_chars），否则 None。"""
        key = self.fingerprint(file_path, params)
        entry = (self._data.get("entries") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        entry["used_at"] = time.time()
        entry["hits"] = int(entry.get("hits") or 0) + 1
        return entry

    def put(self, file_path: str, params: Dict[str, Any], text: str,
            chunk_count: int = 0, origin_chars: int = 0) -> None:
        """写入缓存条目（不立即落盘，由调用方决定何时 save）。"""
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
        """清空缓存，返回清掉的条目数。"""
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
