# -*- coding: utf-8 -*-
"""
[EN]
Native multimodal (VLM) engine built directly on the vendored ``mtmd`` bindings.

This module is deliberately **not wired** into :mod:`vision` yet: the routing layer,
the error-to-language-key mapping and the runtime-policy gate all still belong to
the wheel path. It exists so the native vision path can be validated on its own
before anything depends on it.

Two lessons from the earlier steps are encoded here as hard rules, not comments:

1. **Never call ``llama_backend_init()`` on its own.** This llama.cpp revision no
   longer registers the ggml backends inside it, and a bare call leaves the
   registry empty. The next call, ``llama_model_load_from_file``, then returns
   NULL for every model -- an opaque failure that cost a debugging round in
   T3-2 step 2. The only supported sequence is
   ``llama_native._register_backends()``, which loads the backends explicitly from
   the library directory.
2. **Every native call runs inside the engine's process-wide mutex**
   (``llm_engine._INFERENCE_LOCK``, PROJECT.md hard constraint 11). Vision and text
   share one llama.cpp runtime in the same process; llama.cpp is not thread-safe.

[ZH]
直接建立在 vendored ``mtmd`` 绑定之上的原生多模态（VLM）引擎。

本模块刻意**尚未接线**到 :mod:`vision`：路由层、错误到语言包键的映射以及运行期策略
闸门目前仍属于轮子路径。先让原生视觉路径能独立验证，再谈依赖。

前面步骤的两个教训在这里是硬规则而非注释：

1. **绝不能单独调用 ``llama_backend_init()``。** 该 llama.cpp 修订不再在其中注册
   ggml 后端，裸调用会留下空注册表；紧接着的 ``llama_model_load_from_file`` 对任何
   模型都返回 NULL —— 这个不透明失败在 T3-2 第二步已经消耗过一轮排查。唯一受支持的
   序列是 ``llama_native._register_backends()``，它从库目录显式加载后端。
2. **所有原生调用都在引擎的进程级互斥锁内**（``llm_engine._INFERENCE_LOCK``，
   PROJECT.md 硬约束 11）。同一进程内视觉与文本共用一套 llama.cpp 运行期，而
   llama.cpp 不是线程安全的。
"""
from __future__ import annotations

import ctypes
import logging
import os
import random
import time
from ctypes import byref, c_float, c_int32
from typing import Any, Dict, List, Optional, Sequence, Union

from gguf_meta import read_metadata
from llm_engine import _INFERENCE_LOCK

log = logging.getLogger("EchoDesk.VisionNative")

#: [EN] GGUF magic; a non-GGUF file must never reach the native loader.
#: [ZH] GGUF 魔数；非 GGUF 文件绝不能交给原生加载器。
GGUF_MAGIC = b"GGUF"

#: [EN] Default context window for one vision turn.
#: [ZH] 单次视觉轮的默认上下文窗口。
DEFAULT_VISION_CONTEXT = 4096


class NativeVisionError(RuntimeError):
    """
    [EN]
    Base class for every failure of the native vision path.

    ``code`` is stable and machine-readable so the future routing layer can map it
    to a distinct user-facing message instead of collapsing every problem into
    "vision failed".

    [ZH]
    原生视觉路径所有失败的基类。

    ``code`` 稳定且机器可读，使将来的路由层能映射成不同的用户提示，而不是把所有问题
    都塌缩成「视觉失败」。
    """

    code = "vision_native_error"


class NativeVisionRuntimeUnavailable(NativeVisionError):
    """[EN] No usable native runtime (backend registry / model / context). / [ZH] 没有可用的原生运行期（后端注册表/模型/上下文）。"""

    code = "runtime_unavailable"


class NativeVisionAssetMissing(NativeVisionError):
    """[EN] The model or projector file is absent or not a GGUF file. / [ZH] 模型或投影文件缺失，或不是 GGUF 文件。"""

    code = "asset_missing"


class NativeVisionPairMismatch(NativeVisionError):
    """
    [EN]
    The projector does not belong to the text model.

    A mismatched pair does not fail loudly in llama.cpp: it produces fluent-looking
    garbage, which is the most expensive class of defect to diagnose downstream.
    Therefore the pairing is checked from GGUF metadata *before* loading.

    [ZH]
    投影文件不属于该文本模型。

    配对错误在 llama.cpp 中不会显式失败：它会产出一段看起来通顺的垃圾，这是下游最难
    诊断的一类缺陷。因此在加载前就依据 GGUF 元数据完成配对校验。
    """

    code = "pair_mismatch"


class NativeVisionImageDecodeError(NativeVisionError):
    """[EN] The image bytes could not be decoded by the mtmd bitmap helper. / [ZH] mtmd 位图辅助函数无法解码图片字节。"""

    code = "image_decode_failed"


def _gguf_magic_ok(path: str) -> bool:
    """[EN] Cheap GGUF check. / [ZH] 廉价的 GGUF 校验。"""
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == GGUF_MAGIC
    except OSError:
        return False


def _require_asset(path: str, label: str) -> None:
    """[EN] Raise ``AssetMissing`` unless ``path`` is a readable GGUF. / [ZH] 除非 ``path`` 是可读 GGUF，否则抛 ``AssetMissing``。"""
    if not path or not os.path.isfile(path):
        raise NativeVisionAssetMissing("%s not found: %s" % (label, path))
    if not _gguf_magic_ok(path):
        raise NativeVisionAssetMissing("%s is not a GGUF file: %s" % (label, path))


def _leading_family(projector_type: str) -> str:
    """
    [EN]
    Extract the leading family token from ``clip.projector_type``.

    ``qwen3vl_merger`` -> ``qwen3vl``; ``mlp`` has no family token, so an empty
    string is returned and the caller falls back to the shape check alone.

    [ZH]
    从 ``clip.projector_type`` 提取前导家族标记。

    ``qwen3vl_merger`` -> ``qwen3vl``；``mlp`` 不含家族标记，返回空串，调用方退回到
    仅依赖形状校验。
    """
    token = str(projector_type or "").strip().lower().split("_", 1)[0]
    return token if any(ch.isdigit() for ch in token) else ""


def check_pairing(model_path: str, mmproj_path: str) -> Dict[str, Any]:
    """
    [EN]
    Verify that ``mmproj_path`` belongs to ``model_path`` using GGUF metadata only.

    Two independent checks:

    * **shape** -- ``clip.vision.projection_dim`` must equal the text model's
      ``<arch>.embedding_length``. This is the real tensor contract: the projector
      emits vectors of that width into the text model's residual stream.
    * **family** -- when ``clip.projector_type`` carries a family token (e.g.
      ``qwen3vl_merger``), it is compared with ``general.architecture``. A token
      without digits (``mlp``, ``merger``) carries no family information and is
      skipped rather than guessed. A family mismatch is only fatal when the shape
      evidence is unavailable: real pairs exist whose projector token differs
      (Qwen3.5 uses ``qwen3vl_merger``), and rejecting those would be a false
      negative.

    Returns the evidence used, so a caller can log it. Raises
    :class:`NativeVisionPairMismatch` on any violation.

    [ZH]
    仅用 GGUF 元数据校验 ``mmproj_path`` 是否属于 ``model_path``。

    两项独立校验：

    * **形状** —— ``clip.vision.projection_dim`` 必须等于文本模型的
      ``<arch>.embedding_length``。这是真实的张量契约：投影器输出的向量宽度要接进
      文本模型的残差流。
    * **家族** —— 当 ``clip.projector_type`` 带家族标记（如 ``qwen3vl_merger``）时，
      必须与 ``general.architecture`` 匹配。不含数字的标记（``mlp``、``merger``）不
      携带家族信息，此时跳过而不是猜测。

    返回所用证据，便于调用方记录日志。任一违反即抛
    :class:`NativeVisionPairMismatch`。
    """
    try:
        model_meta = read_metadata(model_path)
    except Exception as exc:  # noqa: BLE001 - [EN] unreadable metadata is an asset fault
                             # [ZH] 元数据不可读属于资产故障
        raise NativeVisionAssetMissing(
            "cannot read metadata from model %s: %s" % (model_path, exc)) from exc
    try:
        mmproj_meta = read_metadata(mmproj_path)
    except Exception as exc:  # noqa: BLE001
        raise NativeVisionAssetMissing(
            "cannot read metadata from mmproj %s: %s" % (mmproj_path, exc)) from exc

    arch = str(model_meta.get("general.architecture") or "").strip().lower()
    projector_type = str(mmproj_meta.get("clip.projector_type") or "").strip().lower()
    has_vision = bool(mmproj_meta.get("clip.has_vision_encoder"))
    hidden = model_meta.get(("%s.embedding_length" % arch) if arch else "")
    if hidden is None:
        hidden = model_meta.get("general.embedding_length")
    projection_dim = mmproj_meta.get("clip.vision.projection_dim")

    evidence: Dict[str, Any] = {
        "architecture": arch,
        "projector_type": projector_type,
        "has_vision_encoder": has_vision,
        "embedding_length": hidden,
        "projection_dim": projection_dim,
    }
    if not has_vision:
        raise NativeVisionPairMismatch(
            "mmproj has no vision encoder (clip.has_vision_encoder is false): %s"
            % os.path.basename(mmproj_path))
    if hidden is not None and projection_dim is not None:
        if int(hidden) != int(projection_dim):
            raise NativeVisionPairMismatch(
                "projector width does not match the text model: %s=%s but "
                "clip.vision.projection_dim=%s (%s + %s)"
                % ("%s.embedding_length" % arch, hidden, projection_dim,
                   os.path.basename(model_path), os.path.basename(mmproj_path)))
        # [EN] The width matched, so the pair is physically compatible even if the
        # [EN] projector family token differs: Qwen3.5 (arch ``qwen35``) legitimately
        # [EN] reuses the ``qwen3vl_merger`` projector. A mismatch is recorded as a
        # [EN] warning instead of a rejection; rejecting here would refuse a real pair.
        # [ZH] 宽度已匹配，说明这一对在物理上兼容，即使投影器家族标记不同：Qwen3.5
        # [ZH]（arch ``qwen35``）确实复用 ``qwen3vl_merger`` 投影器。此时只记录警告而
        # [ZH] 不拒绝；在此拒绝会误杀真实可用的一对。
        family = _leading_family(projector_type)
        if family and arch and family != arch:
            evidence["family_warning"] = (
                "projector family %r != text architecture %r; accepted because the "
                "projection width matches" % (family, arch))
            log.warning("视觉配对家族标记不一致但宽度一致，按可用处理：%s", evidence["family_warning"])
        return evidence
    # [EN] Shape evidence is incomplete, so the family token is the only signal left.
    # [ZH] 形状证据不完整，此时家族标记是唯一可用的判据。
    family = _leading_family(projector_type)
    if family and arch and family != arch:
        raise NativeVisionPairMismatch(
            "projector family %r does not match text architecture %r and the "
            "projection width is unavailable (%s + %s)"
            % (family, arch, os.path.basename(model_path),
               os.path.basename(mmproj_path)))
    return evidence


class NativeVLM:
    """
    [EN]
    One loaded native vision model plus its mtmd projector.

    Construction loads both; :meth:`chat` runs one non-streaming turn and returns
    the generated text. Every native call is serialized by the shared inference
    mutex.

    [ZH]
    一个已加载的原生视觉模型及其 mtmd 投影器。

    构造即加载两者；:meth:`chat` 执行一次非流式视觉轮并返回生成文本。所有原生调用都由
    共享推理互斥锁串行化。
    """

    def __init__(self, model_path: str, mmproj_path: str,
                 n_ctx: int = DEFAULT_VISION_CONTEXT, n_threads: int = 0,
                 n_gpu_layers: int = 0, verbose: bool = False,
                 image_min_tokens: int = 0, image_max_tokens: int = 0) -> None:
        self.model_path = os.path.abspath(model_path) if model_path else ""
        self.mmproj_path = os.path.abspath(mmproj_path) if mmproj_path else ""
        self.n_ctx = max(1024, int(n_ctx or DEFAULT_VISION_CONTEXT))
        self.n_threads = int(n_threads or 0)
        self.n_gpu_layers = int(n_gpu_layers or 0)
        self.verbose = bool(verbose)
        self._n_batch = min(max(64, self.n_ctx), 512)
        # [EN] Qwen-VL tokenizers warn that the default grid is too coarse for
        # [EN] grounding ("at minimum 1024 image tokens"); 0 keeps the build default.
        # [ZH] Qwen-VL 分词器警告默认网格对 grounding 过粗（"至少 1024 image tokens"）；
        # [ZH] 取 0 表示沿用构建默认值。
        self.image_min_tokens = int(image_min_tokens or 0)
        self.image_max_tokens = int(image_max_tokens or 0)
        self.pairing: Dict[str, Any] = {}
        self._lib = None
        self._model = None
        self._ctx = None
        self._mctx = None
        self._bitmap_opt = None
        self._marker = ""
        self._closed = False
        self.timings: Dict[str, float] = {}
        self._load()

    # ------------------------------------------------------------------ loading
    def _load(self) -> None:
        """[EN] Validate assets, check pairing, then load model/context/projector. / [ZH] 校验资产、检查配对，再加载模型/上下文/投影器。"""
        _require_asset(self.model_path, "model")
        _require_asset(self.mmproj_path, "mmproj")
        self.pairing = check_pairing(self.model_path, self.mmproj_path)

        # [EN] Imported here so merely importing this module never loads a DLL.
        # [ZH] 在此导入，使「仅导入本模块」绝不加载任何 DLL。
        import llama_capi  # noqa: F401 - pins LLAMA_CPP_LIB_PATH to backend/native/<rid>
        import llama_abi.llama_cpp as llama_abi  # noqa: PLC0415
        import llama_native  # noqa: PLC0415
        from llama_abi import mtmd_cpp  # noqa: PLC0415

        self._lib = llama_abi
        self._mtmd = mtmd_cpp
        started = time.time()
        with _INFERENCE_LOCK:
            # [EN] Hard rule 1: register the ggml backends through the tested
            # [EN] sequence; a bare llama_backend_init() leaves the registry empty.
            # [ZH] 硬规则 1：经已验证的序列注册 ggml 后端；裸 llama_backend_init()
            # [ZH] 会留下空注册表。
            llama_native._register_backends(llama_abi)  # noqa: SLF001

            model_params = llama_abi.llama_model_default_params()
            model_params.n_gpu_layers = self.n_gpu_layers
            self._model = llama_abi.llama_model_load_from_file(
                self.model_path.encode("utf-8"), model_params)
            if not self._model:
                raise NativeVisionRuntimeUnavailable(
                    "llama_model_load_from_file returned NULL: %s" % self.model_path)
            self.timings["model_load_ms"] = (time.time() - started) * 1000

            ctx_params = llama_abi.llama_context_default_params()
            ctx_params.n_ctx = self.n_ctx
            ctx_params.n_batch = min(self.n_ctx, 512)
            if hasattr(ctx_params, "n_ubatch"):
                ctx_params.n_ubatch = min(self.n_ctx, 512)
            ctx_params.n_threads = self.n_threads
            self._ctx = llama_abi.llama_init_from_model(self._model, ctx_params)
            if not self._ctx:
                raise NativeVisionRuntimeUnavailable(
                    "llama_init_from_model returned NULL: %s" % self.model_path)

            mtmd_params = mtmd_cpp.mtmd_context_params_default()
            mtmd_params.use_gpu = bool(self.n_gpu_layers)
            mtmd_params.print_timings = self.verbose
            mtmd_params.n_threads = self.n_threads
            mtmd_params.warmup = True
            if self.image_min_tokens > 0:
                mtmd_params.image_min_tokens = self.image_min_tokens
            if self.image_max_tokens > 0:
                mtmd_params.image_max_tokens = self.image_max_tokens
            started = time.time()
            self._mctx = mtmd_cpp.mtmd_init_from_file(
                self.mmproj_path.encode("utf-8"), self._model, mtmd_params)
            self.timings["mmproj_load_ms"] = (time.time() - started) * 1000
            if not self._mctx:
                raise NativeVisionPairMismatch(
                    "mtmd_init_from_file rejected the projector for this model: "
                    "%s + %s" % (os.path.basename(self.model_path),
                                 os.path.basename(self.mmproj_path)))
            if not bool(mtmd_cpp.mtmd_support_vision(self._mctx)):
                raise NativeVisionPairMismatch(
                    "projector reports no vision support: %s"
                    % os.path.basename(self.mmproj_path))
            self._marker = mtmd_cpp.mtmd_default_marker().decode("utf-8")
            self._bitmap_opt = mtmd_cpp.mtmd_helper_init_opt_default()
        log.info("原生视觉模型已加载：%s + %s（projector=%s）",
                 os.path.basename(self.model_path),
                 os.path.basename(self.mmproj_path),
                 self.pairing.get("projector_type"))

    # -------------------------------------------------------------------- public
    def status(self) -> Dict[str, Any]:
        """[EN] Machine-readable state, mirroring the wheel service's shape. / [ZH] 机器可读状态，形状对齐轮子服务。"""
        return {
            "available": not self._closed and self._mctx is not None,
            "engine": "native-mtmd",
            "model": os.path.basename(self.model_path),
            "model_path": self.model_path,
            "mmproj": os.path.basename(self.mmproj_path),
            "mmproj_path": self.mmproj_path,
            "loaded": self._mctx is not None,
            "handler": "mtmd",
            "marker": self._marker,
            "pairing": dict(self.pairing),
            "last_error": "",
        }

    def chat(self, image_path: Union[str, Sequence[str]], prompt: str,
             max_tokens: int = 512, temperature: float = 0.3) -> str:
        """
        [EN]
        Run one non-streaming vision turn over one or more local images.

        The media markers are emitted in image order, and every marker consumes
        exactly one bitmap; a marker/bitmap count mismatch is reported as a decode
        error instead of being silently reshaped.

        [ZH]
        对一张或多张本地图片执行一次非流式视觉轮。

        媒体标记按图片顺序排列，每个标记恰好消费一个位图；标记与位图数量不一致时报
        解码错误，而不是静默重排。
        """
        if self._closed:
            raise NativeVisionRuntimeUnavailable("native vision engine is closed")
        if isinstance(image_path, (str, bytes, os.PathLike)):
            paths: List[str] = [os.fspath(image_path)]
        else:
            paths = [os.fspath(item) for item in image_path]
        if not paths:
            raise NativeVisionImageDecodeError("no image was supplied")

        started = time.time()
        with _INFERENCE_LOCK:
            # [EN] Every ``chat`` is an independent turn: the caller keeps the
            # [EN] conversation as text, not as KV state. Without clearing, the
            # [EN] second call starts at position 0 while the cache still holds the
            # [EN] previous turn and M-RoPE rejects the batch ("X < Y" required).
            # [ZH] 每次 ``chat`` 都是独立的一轮：调用方用文本保存对话，而不是 KV 状态。
            # [ZH] 不清空的话，第二次调用从位置 0 开始、而缓存仍持有上一轮，M-RoPE 会
            # [ZH] 以「必须满足 X < Y」拒绝该批次。
            self._reset_memory()
            bitmaps = [self._decode_bitmap(path) for path in paths]
            try:
                chunks = self._tokenize(bitmaps, prompt)
            finally:
                for bitmap in bitmaps:
                    try:
                        self._mtmd.mtmd_bitmap_free(bitmap)
                    except Exception:  # noqa: BLE001 - [EN] cleanup must not mask the
                                       # [ZH] 清理不得掩盖真正的错误
                        log.debug("释放位图失败（忽略）", exc_info=True)
            try:
                n_past = c_int32(0)
                eval_started = time.time()
                rc = self._mtmd.mtmd_helper_eval_chunks(
                    self._mctx, self._ctx, chunks, 0, 0,
                    self._n_batch, True, byref(n_past))
                self.timings["eval_ms"] = (time.time() - eval_started) * 1000
                if rc != 0:
                    raise NativeVisionRuntimeUnavailable(
                        "mtmd_helper_eval_chunks failed with code %d" % rc)
                text = self._generate(int(n_past.value), max_tokens, temperature)
            finally:
                try:
                    self._mtmd.mtmd_input_chunks_free(chunks)
                except Exception:  # noqa: BLE001
                    log.debug("释放输入分块失败（忽略）", exc_info=True)
        self.timings["total_ms"] = (time.time() - started) * 1000
        return text

    def close(self) -> None:
        """[EN] Free the projector, context and model. / [ZH] 释放投影器、上下文与模型。"""
        if self._closed:
            return
        self._closed = True
        with _INFERENCE_LOCK:
            for name, closer, arg in (
                    ("mtmd", getattr(self._mtmd, "mtmd_free", None), self._mctx),
                    ("context", getattr(self._lib, "llama_free", None), self._ctx),
                    ("model", getattr(self._lib, "llama_model_free", None),
                     self._model)):
                if callable(closer) and arg is not None:
                    try:
                        closer(arg)
                    except Exception:  # noqa: BLE001 - [EN] cleanup is best-effort
                                       # [ZH] 清理尽力而为
                        log.debug("释放 %s 失败（忽略）", name, exc_info=True)
            self._mctx = None
            self._ctx = None
            self._model = None

    # ------------------------------------------------------------------- helpers
    def _reset_memory(self) -> None:
        """[EN] Drop all KV state so each turn starts at position 0. / [ZH] 清空全部 KV 状态，使每轮都从位置 0 开始。"""
        try:
            memory = self._lib.llama_get_memory(self._ctx)
            if memory:
                self._lib.llama_memory_clear(memory, True)
        except Exception:  # noqa: BLE001 - [EN] an old build without the API still runs
                           # [ZH] 缺少该 API 的旧构建仍可运行
            log.debug("清空 KV 缓存失败（忽略）", exc_info=True)

    def _decode_bitmap(self, path: str):
        """[EN] Decode image bytes through the vendored mtmd helper. / [ZH] 经 vendored mtmd 辅助函数解码图片字节。"""
        if not path or not os.path.isfile(path):
            raise NativeVisionImageDecodeError("image not found: %s" % path)
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise NativeVisionImageDecodeError(
                "cannot read image %s: %s" % (path, exc)) from exc
        if not data:
            raise NativeVisionImageDecodeError("image is empty: %s" % path)
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        wrapper = self._mtmd.mtmd_helper_bitmap_init_from_buf(
            self._mctx, buffer, len(data), False, self._bitmap_opt)
        bitmap = wrapper.bitmap if wrapper else None
        if not bitmap:
            raise NativeVisionImageDecodeError(
                "mtmd could not decode %s (unsupported or corrupt image)" % path)
        return bitmap

    def _tokenize(self, bitmaps, prompt: str):
        """[EN] Tokenize ``marker*N + prompt`` against the given bitmaps. / [ZH] 用给定位图对 ``marker*N + prompt`` 分词。"""
        text = "".join(self._marker + "\n" for _ in bitmaps) + str(prompt or "")
        chunks = self._mtmd.mtmd_input_chunks_init()
        if not chunks:
            raise NativeVisionRuntimeUnavailable("mtmd_input_chunks_init returned NULL")
        input_text = self._mtmd.mtmd_input_text()
        encoded = text.encode("utf-8")
        input_text.text = encoded
        input_text.text_len = len(encoded)
        input_text.add_special = True
        input_text.parse_special = True
        count = len(bitmaps)
        array = (self._mtmd.mtmd_bitmap_p_ctypes * count)(*bitmaps) if count else None
        tokenize_started = time.time()
        rc = self._mtmd.mtmd_tokenize(
            self._mctx, chunks, byref(input_text), array, count)
        self.timings["tokenize_ms"] = (time.time() - tokenize_started) * 1000
        if rc != 0:
            try:
                self._mtmd.mtmd_input_chunks_free(chunks)
            finally:
                raise NativeVisionImageDecodeError(
                    "mtmd_tokenize rejected the input (code %d): %d marker(s) / "
                    "%d bitmap(s)" % (rc, text.count(self._marker), count))
        self.timings["prompt_tokens"] = float(
            self._mtmd.mtmd_helper_get_n_tokens(chunks))
        return chunks

    def _generate(self, n_past: int, max_tokens: int, temperature: float) -> str:
        """
        [EN]
        Greedy (``temperature <= 0``) or multinomial decode loop.

        Positions are assigned explicitly starting at ``n_past``; the batch helper
        is avoided because this revision's ``llama_batch_get_one`` restarts at 0.

        [ZH]
        贪心（``temperature <= 0``）或多项式采样解码循环。

        位置从 ``n_past`` 起显式赋值；不使用批量辅助函数，因为该修订的
        ``llama_batch_get_one`` 会把位置从 0 重新开始。
        """
        vocab = self._lib.llama_model_get_vocab(self._model)
        n_vocab = int(self._lib.llama_n_vocab(vocab))
        budget = max(1, int(max_tokens or 1))
        started = time.time()
        raw = bytearray()
        batch = self._lib.llama_batch_init(1, 0, 1)
        position = n_past
        try:
            for step in range(budget):
                logits = self._lib.llama_get_logits(self._ctx)
                flat = (c_float * n_vocab)()
                ctypes.memmove(flat, logits, n_vocab * ctypes.sizeof(c_float))
                best = self._pick(flat, n_vocab, temperature)
                piece = self._piece(vocab, best)
                if step == 0:
                    self.timings["first_token_ms"] = (
                        self.timings.get("eval_ms", 0.0) + (time.time() - started) * 1000)
                raw.extend(piece)
                if step == budget - 1:
                    break
                batch.n_tokens = 1
                batch.token[0] = best
                batch.pos[0] = position
                batch.n_seq_id[0] = 1
                batch.seq_id[0][0] = 0
                batch.logits[0] = 1
                if self._lib.llama_decode(self._ctx, batch) != 0:
                    log.warning("原生视觉解码在第 %d 步返回非零", step)
                    break
                position += 1
        finally:
            self._lib.llama_batch_free(batch)
        return raw.decode("utf-8", "replace")

    def _pick(self, flat, n_vocab: int, temperature: float) -> int:
        """[EN] Argmax or temperature sampling over one logits vector. / [ZH] 对一组 logits 取 argmax 或按温度采样。"""
        if temperature is None or float(temperature) <= 0.0:
            return max(range(n_vocab), key=lambda index: flat[index])
        scale = 1.0 / float(temperature)
        weights = [pow(2.718281828459045, flat[index] * scale) for index in range(n_vocab)]
        total = sum(weights)
        r = random.random() * total
        upto = 0.0
        for index, weight in enumerate(weights):
            upto += weight
            if upto >= r:
                return index
        return n_vocab - 1

    def _piece(self, vocab, token: int) -> bytes:
        """[EN] Token id to raw bytes. / [ZH] token id 转原始字节。"""
        buffer = ctypes.create_string_buffer(256)
        written = int(self._lib.llama_token_to_piece(vocab, token, buffer, 256, 0, True))
        return buffer.raw[:written] if written > 0 else b""
