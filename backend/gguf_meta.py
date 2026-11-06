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
from typing import Any, Dict, Optional

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
    meta = read_metadata(path)
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
    meta = read_metadata(path)
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
    meta = read_metadata(path)
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
