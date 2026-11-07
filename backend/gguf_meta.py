"""
[EN]
Lightweight GGUF metadata reader and memory estimation.

Only the KV metadata section of the GGUF header is parsed; tensor data is not
loaded. This makes the training-context length, layer count, attention-head
count and related parameters available without loading the model into memory.
The values are used to:

1. resolve the context-window limit per model (``resolve_context_window``);
2. estimate KV-cache memory (``estimate_kv_cache_bytes``);
3. emit context-window details when a model is loaded or reloaded.

The GGUF specification is available at
https://github.com/ggml-org/ggml/blob/master/docs/gguf.md

[ZH]
GGUF 元数据的轻量读取与内存估算。

仅解析 GGUF 头部的 KV 元数据区，不读取张量数据。因此无需将模型载入内存即可
获得训练上下文长度、层数、注意力头数等关键参数。这些值用于：

1. 按模型解析上下文窗口上限（``resolve_context_window``）；
2. 估算 KV Cache 内存占用（``estimate_kv_cache_bytes``）；
3. 在模型加载或重载时输出上下文窗口信息。

GGUF 规范见 https://github.com/ggml-org/ggml/blob/master/docs/gguf.md
"""
import os
from i18n import t as _t
import struct
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

MAGIC = b"GGUF"

# [EN] GGUF metadata value types mapped to struct format strings. Strings and
# [EN] arrays are handled separately.
# [ZH] GGUF 元数据值类型到 struct 格式字符串的映射。字符串与数组单独处理。
_SCALAR_FORMATS = {
    0: "<B",   # UINT8
    1: "<b",   # INT8
    2: "<H",   # UINT16
    3: "<h",   # INT16
    4: "<I",   # UINT32
    5: "<i",   # INT32
    6: "<f",   # FLOAT32
    7: "<B",   # BOOL(uint8)
    10: "<Q",  # UINT64
    11: "<q",  # INT64
    12: "<d",  # FLOAT64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9

# [EN] Maximum number of array elements read eagerly. Tokenizer vocabularies in
# [EN] metadata can contain tens of thousands of entries and need not be loaded.
# [ZH] 数组最多预读的元素数量。元数据中的分词器词表可能包含数万项，无需全部载入。
_MAX_ARRAY_ITEMS = 64


class GgufReader:
    """
    [EN]
    Read GGUF metadata key-value pairs sequentially. Unsupported or malformed
    entries raise an exception that the caller is expected to handle.

    [ZH]
    按顺序读取 GGUF 元数据键值对。遇到不支持或格式异常的条目时抛出异常，由调用方处理。
    """

    def __init__(self, fh) -> None:
        self._fh = fh

    def _read(self, fmt: str) -> Any:
        size = struct.calcsize(fmt)
        data = self._fh.read(size)
        if len(data) != size:
            raise EOFError("GGUF 文件意外结束")
        return struct.unpack(fmt, data)[0]

    def _string(self) -> str:
        length = self._read("<Q")
        return self._fh.read(length).decode("utf-8", errors="replace")

    def _value(self, vtype: int) -> Any:
        if vtype == _TYPE_STRING:
            return self._string()
        if vtype == _TYPE_ARRAY:
            item_type = self._read("<I")
            count = self._read("<Q")
            if count > _MAX_ARRAY_ITEMS:
                # [EN] Skip oversized arrays such as vocabularies: read the first
                # [EN] few entries for type checks and seek past the remainder.
                # [ZH] 跳过大数组（如词表）：只读取前若干项用于类型判断，其余部分通过 seek 跳过。
                head = [self._value(item_type) for _ in range(_MAX_ARRAY_ITEMS)]
                self._skip_array_items(item_type, count - _MAX_ARRAY_ITEMS)
                return head
            return [self._value(item_type) for _ in range(count)]
        fmt = _SCALAR_FORMATS.get(vtype)
        if fmt is None:
            raise ValueError(f"未知 GGUF 值类型: {vtype}")
        value = self._read(fmt)
        return bool(value) if vtype == 7 else value

    def _skip_array_items(self, item_type: int, count: int) -> None:
        """
        [EN]
        Skip the remaining elements of an array. Variable-length types must be
        read one by one.

        [ZH]
        跳过数组剩余元素。变长类型只能逐项读取。
        """
        if item_type in (_TYPE_STRING, _TYPE_ARRAY):
            for _ in range(count):
                self._value(item_type)
            return
        fmt = _SCALAR_FORMATS.get(item_type)
        if fmt is None:
            raise ValueError(f"未知 GGUF 值类型: {item_type}")
        self._fh.seek(struct.calcsize(fmt) * count, os.SEEK_CUR)


def read_metadata(path: str, max_pairs: int = 4096) -> Dict[str, Any]:
    """
    [EN]
    Read the metadata dictionary of a GGUF file. Returns an empty dictionary if
    the file is missing or malformed.

    [ZH]
    读取 GGUF 文件的元数据字典。文件不存在或格式异常时返回空字典。
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != MAGIC:
                return {}
            version = struct.unpack("<I", fh.read(4))[0]
            # [EN] Version 1 uses a different layout and is intentionally unsupported.
            # [ZH] 版本 1 的布局不同，刻意不提供兼容。
            if version < 2:
                return {}
            reader = GgufReader(fh)
            # [EN] tensor_count is present in the header but not used here.
            # [ZH] 头部包含 tensor_count，但本模块不使用该值。
            reader._read("<Q")
            kv_count = reader._read("<Q")
            meta: Dict[str, Any] = {"general.gguf_version": version}
            for _ in range(min(kv_count, max_pairs)):
                key = reader._string()
                vtype = reader._read("<I")
                meta[key] = reader._value(vtype)
            return meta
    except Exception:
        return {}


def _as_int(value: Any) -> Optional[int]:
    """
    [EN]
    Convert a metadata value to int defensively. For arrays, the first element is
    used because fields such as ``head_count_kv`` may be arrays.

    [ZH]
    稳健地将元数据值转换为 int。对于数组取首项，因为 ``head_count_kv`` 等字段
    可能以数组形式出现。
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, (list, tuple)) and value:
        return _as_int(value[0])
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


# [EN] Metadata cache. The inference planner reads the same file five to eight
# [EN] times per model load (native window, layer count, KV cost per token, KV
# [EN] cost at the ceiling, and the human-readable summary). Each read opens and
# [EN] parses the header, so the parse is memoised by path, size and modification
# [EN] time. The cache is bounded so that a long-lived process scanning a large
# [EN] model directory cannot grow it without limit; a metadata change (a
# [EN] replaced file) yields a different key and is re-read.
# [ZH] 元数据缓存。推理规划器在每次加载模型时会读取同一文件五到八次（原生窗口、
# [ZH] 层数、单 token 的 KV 开销、上限处的 KV 开销以及可读摘要）。每次读取都会
# [ZH] 打开并解析文件头，因此按路径、大小与修改时间做记忆化。缓存设有上限，
# [ZH] 使长期运行的进程在扫描大型模型目录时不会无限增长；元数据变化（文件被
# [ZH] 替换）会产生不同的键并被重新读取。
_METADATA_CACHE: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
_METADATA_LOCK = threading.Lock()
_METADATA_CACHE_LIMIT = 16


def cached_metadata(path: str) -> Dict[str, Any]:
    """
    [EN]
    Return the GGUF metadata dictionary, memoised by path, size and modification
    time. Returns an empty dictionary when the file cannot be read.

    [ZH]
    返回 GGUF 元数据字典，按路径、大小与修改时间进行记忆化。文件无法读取时返回
    空字典。
    """
    text = str(path or "").strip()
    if not text:
        return {}
    try:
        stat = os.stat(text)
        key = (os.path.abspath(text), int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        return {}
    with _METADATA_LOCK:
        hit = _METADATA_CACHE.get(key)
    if hit is not None:
        return hit
    meta = read_metadata(text)
    with _METADATA_LOCK:
        if len(_METADATA_CACHE) >= _METADATA_CACHE_LIMIT:
            # [EN] Evict the oldest insertion; the access pattern is a handful of
            # [EN] recent models, so insertion order is a sufficient policy.
            # [ZH] 驱逐最早插入的条目；访问模式只涉及少量近期模型，因此插入顺序
            # [ZH] 已足以作为淘汰策略。
            for stale in list(_METADATA_CACHE)[: max(1, _METADATA_CACHE_LIMIT // 2)]:
                _METADATA_CACHE.pop(stale, None)
        _METADATA_CACHE[key] = meta
    return meta


@dataclass(frozen=True)
class ModelMetadata:
    """
    [EN]
    Structured subset of the GGUF header that governs inference planning.

    The fields are read from the native KV section only; tensor data is never
    touched. ``n_ctx_train`` is the context length the model was trained with and
    is a hard ceiling: a larger window cannot be justified by the file. A zero
    value means the field could not be read, and callers must fall back to a
    conservative assumption instead of treating zero as a real limit.

    [ZH]
    用于支配推理规划的 GGUF 头部结构化子集。

    字段仅来自原生 KV 元数据区，绝不触碰张量数据。``n_ctx_train`` 是模型训练时
    的上下文长度，属于硬性上限：更大的窗口无法由文件内容得到支持。取值为零表示
    该字段无法读取，调用方必须回退到保守假设，而不得把零当作真实上限。
    """

    path: str = ""
    architecture: str = ""
    n_ctx_train: int = 0
    n_layers: int = 0
    n_heads: int = 0
    n_kv_heads: int = 0
    n_embd: int = 0
    key_length: int = 0
    value_length: int = 0
    file_type: int = -1

    @property
    def readable(self) -> bool:
        """
        [EN]
        Whether any dimension was read from the file.
        [ZH]
        是否从文件中读到了任意一项维度信息。
        """
        return bool(self.n_ctx_train or self.n_layers or self.n_heads or self.n_embd)

    @property
    def head_dim(self) -> int:
        """
        [EN]
        Per-head key dimension, derived from the embedding width when the file
        does not declare it explicitly.
        [ZH]
        单个注意力头的键维度；文件未显式声明时由嵌入宽度推导。
        """
        if self.key_length > 0:
            return int(self.key_length)
        if self.n_embd > 0 and self.n_heads > 0:
            return int(self.n_embd // self.n_heads)
        return 0

    def kv_bytes_per_token(self, bytes_per_element: int = 2) -> float:
        """
        [EN]
        KV-cache cost of one context token in bytes, assuming the supplied
        element width (2 bytes for F16, which is the llama.cpp default).

        Formula: n_layers x n_kv_heads x (key_dim + value_dim) x bytes.

        [ZH]
        单个上下文 token 的 KV Cache 字节开销，按给定的元素宽度计算（F16 为
        2 字节，这也是 llama.cpp 的默认值）。

        公式：n_layers × n_kv_heads × (key_dim + value_dim) × 字节数。
        """
        try:
            kv_heads = int(self.n_kv_heads or self.n_heads or 0)
            layers = int(self.n_layers or 0)
            key_dim = int(self.key_length or self.head_dim or 0)
            value_dim = int(self.value_length or key_dim)
            if layers <= 0 or kv_heads <= 0 or key_dim <= 0:
                return 0.0
            return float(layers * kv_heads * (key_dim + value_dim) * max(1, int(bytes_per_element)))
        except (TypeError, ValueError):
            return 0.0

    def to_dict(self) -> Dict[str, Any]:
        """
        [EN]
        Serialize the metadata for logs, diagnostics and the JSON line protocol.
        [ZH]
        将元数据序列化，供日志、诊断与 JSON 行协议使用。
        """
        return {
            "path": self.path,
            "architecture": self.architecture,
            "n_ctx_train": int(self.n_ctx_train),
            "n_layers": int(self.n_layers),
            "n_heads": int(self.n_heads),
            "n_kv_heads": int(self.n_kv_heads),
            "n_embd": int(self.n_embd),
            "key_length": int(self.key_length),
            "value_length": int(self.value_length),
            "file_type": int(self.file_type),
            "readable": self.readable,
        }


def read_model_metadata(path: str) -> ModelMetadata:
    """
    [EN]
    Read the structured model metadata used by the inference planner.

    Every field is optional: an unreadable or malformed file yields an empty
    metadata object rather than an exception. Parsing failure is a normal
    deployment condition (a partially downloaded file, an unsupported GGUF
    version), and the planner must remain able to produce a conservative plan.

    [ZH]
    读取推理规划器使用的结构化模型元数据。

    所有字段都是可选的：不可读或格式异常的文件会返回空元数据对象，而不是抛出
    异常。解析失败属于正常部署状况（文件尚未下载完成、GGUF 版本不受支持），
    规划器必须仍能产出保守的规划。
    """
    text = str(path or "").strip()
    meta = cached_metadata(text)
    if not meta:
        return ModelMetadata(path=text)
    arch = architecture(meta)

    def value(*keys: str) -> int:
        for key in keys:
            parsed = _as_int(meta.get(key))
            if parsed is not None:
                return int(parsed)
        return 0

    return ModelMetadata(
        path=text,
        architecture=arch,
        n_ctx_train=value(f"{arch}.context_length", "general.context_length",
                          "llama.context_length"),
        n_layers=value(f"{arch}.block_count"),
        n_heads=value(f"{arch}.attention.head_count"),
        n_kv_heads=value(f"{arch}.attention.head_count_kv"),
        n_embd=value(f"{arch}.embedding_length"),
        key_length=value(f"{arch}.attention.key_length"),
        value_length=value(f"{arch}.attention.value_length"),
        file_type=value("general.file_type"),
    )


def architecture(meta: Dict[str, Any]) -> str:
    """
    [EN]
    Return the model architecture name, such as qwen3 or llama.

    [ZH]
    返回模型架构名称，例如 qwen3 或 llama。
    """
    value = meta.get("general.architecture")
    return str(value) if isinstance(value, str) and value else "llama"


def resolve_context_window(path: str, default: int = 4096) -> int:
    """
    [EN]
    Return the model's training context window in tokens, or ``default`` when
    the value cannot be read.

    [ZH]
    返回模型训练时的上下文窗口 token 数；无法读取时返回 ``default``。
    """
    meta = cached_metadata(path)
    if not meta:
        return default
    arch = architecture(meta)
    for key in (f"{arch}.context_length", "general.context_length", "llama.context_length"):
        value = _as_int(meta.get(key))
        if value and value > 0:
            return value
    return default


def estimate_kv_cache_bytes(path: str, n_ctx: int) -> Optional[int]:
    """
    [EN]
    Estimate KV-cache memory in bytes for the given context length, assuming
    fp16 storage.

    Formula: n_ctx x n_layer x n_head_kv x (key_dim + value_dim) x 2 bytes.
    Returns ``None`` when the metadata is insufficient.

    [ZH]
    估算给定上下文长度下 KV Cache 的内存占用（字节），按 fp16 存储计算。

    公式：n_ctx × n_layer × n_head_kv × (key_dim + value_dim) × 2 字节。
    元数据不足时返回 ``None``。
    """
    meta = cached_metadata(path)
    if not meta:
        return None
    arch = architecture(meta)
    layers = _as_int(meta.get(f"{arch}.block_count"))
    if not layers:
        return None
    head_kv = _as_int(meta.get(f"{arch}.attention.head_count_kv"))
    head_count = _as_int(meta.get(f"{arch}.attention.head_count"))
    n_embd = _as_int(meta.get(f"{arch}.embedding_length"))
    key_dim = _as_int(meta.get(f"{arch}.attention.key_length"))
    value_dim = _as_int(meta.get(f"{arch}.attention.value_length"))

    if not head_kv:
        head_kv = head_count
    if not head_kv or not layers:
        return None
    if not key_dim:
        if not (n_embd and head_count):
            return None
        key_dim = n_embd // head_count
    if not value_dim:
        value_dim = key_dim

    return int(n_ctx) * int(layers) * int(head_kv) * (int(key_dim) + int(value_dim)) * 2


def describe_model(path: str, n_ctx: int) -> Dict[str, Any]:
    """
    [EN]
    Summarize key model parameters for logs and the ``/model`` command.

    [ZH]
    汇总模型关键参数，供日志与 ``/model`` 命令展示。
    """
    meta = cached_metadata(path)
    arch = architecture(meta) if meta else _t("sys.log_unknown")
    train_ctx = resolve_context_window(path, default=0) if meta else 0
    return {
        "architecture": arch,
        "train_context": train_ctx,
        "n_ctx": n_ctx,
        "layers": _as_int(meta.get(f"{arch}.block_count")) if meta else None,
        "head_count": _as_int(meta.get(f"{arch}.attention.head_count")) if meta else None,
        "head_count_kv": _as_int(meta.get(f"{arch}.attention.head_count_kv")) if meta else None,
        "kv_cache_bytes": estimate_kv_cache_bytes(path, n_ctx),
        "quantization": meta.get("general.file_type") if meta else None,
    }


def format_bytes(n: Optional[int]) -> str:
    """
    [EN]
    Format a byte count for KV-cache usage display.

    [ZH]
    格式化字节数，用于展示 KV Cache 占用。
    """
    if not n:
        return _t("sys.log_unknown")
    if n >= 1073741824:
        return f"{n / 1073741824:.2f} GB"
    return f"{n / 1048576:.1f} MB"
