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

from gguf_meta import MAGIC as GGUF_MAGIC, read_metadata
from llm_engine import _INFERENCE_LOCK
from vision_errors import (
    CODE_ASSET_MISSING,
    CODE_IMAGE_DECODE_ERROR,
    CODE_PAIR_MISMATCH,
    CODE_RUNTIME_UNAVAILABLE,
    VisionError,
)

log = logging.getLogger("EchoDesk.VisionNative")

#: [EN] Error codes are defined in :mod:`vision_errors` and re-exported here so
#: [EN] existing callers/tests keep one import site; the vocabulary itself must stay
#: [EN] shared with the wheel path.
#: [ZH] 错误码定义在 :mod:`vision_errors` 并在此重新导出，使现有调用方/测试保持单一
#: [ZH] 导入点；词表本身必须与轮子路径共用。
__all__ = [
    "CODE_RUNTIME_UNAVAILABLE", "CODE_ASSET_MISSING", "CODE_PAIR_MISMATCH",
    "CODE_IMAGE_DECODE_ERROR", "NativeVLM", "classify_pairing", "check_pairing",
    "image_budget_from_config", "PAIRING_VERIFIED", "PAIRING_MISMATCH",
    "PAIRING_UNVERIFIED", "PROJECTOR_FAMILY_WHITELIST",
    "DEFAULT_IMAGE_MIN_TOKENS", "DEFAULT_IMAGE_MAX_TOKENS",
]

#: [EN] Default context window for one vision turn.
#: [ZH] 单次视觉轮的默认上下文窗口。
DEFAULT_VISION_CONTEXT = 4096

#: [EN] Pairing states. ``verified`` / ``mismatch`` / ``unverified``: metadata can
#: [EN] prove the pair right or wrong, or say nothing at all. "Unknown" must not be
#: [EN] treated as "wrong" -- that would reject models that actually work -- but it
#: [EN] must be visible in status so support can tell the two apart.
#: [ZH] 配对三态。``verified`` / ``mismatch`` / ``unverified``：元数据可以证明配对正确、
#: [ZH] 错误，或什么也证明不了。「未知」不能当成「错误」——那会挡死本来可用的模型——
#: [ZH] 但必须出现在状态里，使支持人员能区分二者。
PAIRING_VERIFIED = "verified"
PAIRING_MISMATCH = "mismatch"
PAIRING_UNVERIFIED = "unverified"

#: [EN] The only exception to "family must match": a projector type whose family
#: [EN] token differs from a text architecture that is nevertheless known-good.
#: [EN] Every row is evidence-backed; adding a family means adding a row here.
#: [ZH] 「家族必须一致」的唯一例外：投影器类型的家族标记与文本架构不同、但已被证实
#: [ZH] 可用的组合。每一行都有实测依据；新增家族必须在此加一行。
PROJECTOR_FAMILY_WHITELIST: Dict[str, tuple] = {
    # [EN] Qwen3.5 (arch ``qwen35``) reuses the Qwen3-VL merger; verified end-to-end.
    # [ZH] Qwen3.5（arch ``qwen35``）复用 Qwen3-VL 的 merger；已端到端实测通过。
    "qwen3vl_merger": ("qwen3vl", "qwen35"),
}

#: [EN] Image token budget defaults. 1024/2048 is the range the Qwen-VL tokenizer
#: [EN] itself warns about: below 1024 the image grid is too coarse and the model
#: [EN] answers with fluent garbage instead of failing. Correctness beats latency, and
#: [EN] the same values apply to known and unknown families -- "unknown" must not mean
#: [EN] "silently degraded".
#: [ZH] 图像 token 预算默认值。1024/2048 正是 Qwen-VL 分词器自己警告的区间：低于 1024
#: [ZH] 时图像网格过粗，模型会以通顺的垃圾作答而不是失败。正确性优先于延迟；已知家族与
#: [ZH] 未知家族使用同一组值 ——「未知」不得等于「静默降级」。
DEFAULT_IMAGE_MIN_TOKENS = 1024
DEFAULT_IMAGE_MAX_TOKENS = 2048


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
        raise VisionError(CODE_ASSET_MISSING, "media.vision_model_missing" if label == "model" else "media.vision_mmproj_missing", name=os.path.basename(path or ""), detail="%s not found: %s" % (label, path))
    if not _gguf_magic_ok(path):
        raise VisionError(CODE_ASSET_MISSING, "media.vision_model_missing" if label == "model" else "media.vision_mmproj_missing", name=os.path.basename(path or ""), detail="%s is not a GGUF file: %s" % (label, path))


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


def classify_pairing(model_meta: Dict[str, Any],
                     mmproj_meta: Dict[str, Any]) -> "tuple[str, Dict[str, Any]]":
    """
    [EN]
    Pure tri-state pairing classifier: no files, no native calls, no exceptions.

    Returns ``(state, evidence)`` where ``state`` is one of :data:`PAIRING_VERIFIED`,
    :data:`PAIRING_MISMATCH` or :data:`PAIRING_UNVERIFIED`.

    The asymmetry is deliberate:

    * known mismatch -> ``mismatch`` (llama.cpp would emit fluent garbage);
    * known match -> ``verified``;
    * metadata silent -> ``unverified`` and the caller may still load. Failing
      closed here would reject models that actually work; failing open silently
      would ship garbage. "Known-bad blocks, unknown loads and is labelled" is the
      only rule that is correct on both ends.

    [ZH]
    纯三态配对分类器：不读文件、不调原生、不抛异常。

    返回 ``(state, evidence)``，``state`` 取 :data:`PAIRING_VERIFIED`、
    :data:`PAIRING_MISMATCH` 或 :data:`PAIRING_UNVERIFIED` 之一。

    这种不对称是刻意的：

    * 已知不匹配 -> ``mismatch``（llama.cpp 会输出通顺的垃圾）；
    * 已知匹配 -> ``verified``；
    * 元数据沉默 -> ``unverified``，调用方仍可加载。此处 fail-closed 会拒绝本来可用的
      模型，fail-open 又会静默出垃圾；只有「已知错就拦、未知则放行并标记」在两端都正确。
    """
    arch = str(model_meta.get("general.architecture") or "").strip().lower()
    projector_type = str(mmproj_meta.get("clip.projector_type") or "").strip().lower()
    vision_key_present = "clip.has_vision_encoder" in mmproj_meta
    has_vision = bool(mmproj_meta.get("clip.has_vision_encoder"))
    hidden = model_meta.get(("%s.embedding_length" % arch) if arch else "")
    if hidden is None:
        hidden = model_meta.get("general.embedding_length")
    projection_dim = mmproj_meta.get("clip.vision.projection_dim")
    family = _leading_family(projector_type)
    allowed_families = PROJECTOR_FAMILY_WHITELIST.get(projector_type)

    evidence: Dict[str, Any] = {
        "architecture": arch,
        "projector_type": projector_type,
        "has_vision_encoder": has_vision if vision_key_present else None,
        "embedding_length": hidden,
        "projection_dim": projection_dim,
        "whitelisted_families": list(allowed_families) if allowed_families else None,
    }
    if vision_key_present and not has_vision:
        evidence["reason"] = "mmproj declares no vision encoder"
        return PAIRING_MISMATCH, evidence
    # [EN] Rule 1: unequal widths are always a mismatch. A width says how wide the
    # [EN] vectors the projector emits are; if they do not fit the residual stream
    # [EN] the pair is wrong no matter what the names say.
    # [ZH] 规则 1：宽度不等一律不匹配。宽度表示投影器输出向量有多宽；接不进残差流时，
    # [ZH] 名字怎么说都不对。
    if hidden is not None and projection_dim is not None:
        try:
            same = int(hidden) == int(projection_dim)
        except (TypeError, ValueError):
            same = None
        if same is False:
            evidence["reason"] = (
                "projection width %s != %s.embedding_length %s"
                % (projection_dim, arch, hidden))
            return PAIRING_MISMATCH, evidence
    # [EN] Rule 2: a family mismatch is a mismatch *regardless of width*. Two
    # [EN] unrelated families often share 2048 hidden size, and accepting that pair
    # [EN] is exactly the silent-garbage case this check exists to stop.
    # [EN] Rule 3: the only escape is the explicit whitelist table -- adding a family
    # [EN] means adding a row here, never widening the comparison.
    # [ZH] 规则 2：家族不匹配一律不匹配，**与宽度是否相同无关**。两个互不相关的家族
    # [ZH] 常常同为 2048，放行这种配对正是本校验要拦的静默垃圾。
    # [ZH] 规则 3：唯一例外是显式白名单表 —— 新增家族必须加一行，绝不放宽比较逻辑。
    if allowed_families is not None:
        family_ok = bool(arch) and arch in allowed_families
        if not family_ok:
            evidence["reason"] = (
                "text architecture %r is not whitelisted for projector %r "
                "(allowed: %s)" % (arch, projector_type, sorted(allowed_families)))
            return PAIRING_MISMATCH, evidence
    elif family and arch:
        if family != arch:
            evidence["reason"] = (
                "projector family %r != text architecture %r" % (family, arch))
            return PAIRING_MISMATCH, evidence
        family_ok = True
    else:
        family_ok = None
    # [EN] Everything the metadata can prove is consistent. Width equality on its own
    # [EN] is not proof (any two families can share 2048), so a pair with no family
    # [EN] signal stays "unverified" and is allowed with a warning.
    # [ZH] 元数据能证明的部分都一致。仅凭宽度相等不构成证明（任意两个家族都可能同为
    # [ZH] 2048），因此缺少家族信号的配对保持 "unverified"，放行并告警。
    if family_ok is True:
        return PAIRING_VERIFIED, evidence
    evidence["reason"] = "metadata is not sufficient to verify the pair"
    return PAIRING_UNVERIFIED, evidence


def check_pairing(model_path: str, mmproj_path: str) -> Dict[str, Any]:
    """
    [EN]
    Read both GGUF metadata blocks, classify the pair and raise on a known mismatch.

    Returns ``{"state": ..., ...evidence}``. A known mismatch raises
    :class:`vision_errors.VisionError` with ``code == "pair_mismatch"``; an
    unverifiable pair returns ``state == "unverified"`` so the caller can load it
    and warn once.

    [ZH]
    读取两个 GGUF 元数据块、分类配对，并在已知不匹配时抛出。

    返回 ``{"state": ..., ...evidence}``。已知不匹配抛
    :class:`vision_errors.VisionError`（``code == "pair_mismatch"``）；无法判定的配对
    返回 ``state == "unverified"``，由调用方加载并告警一次。
    """
    try:
        model_meta = read_metadata(model_path)
    except Exception as exc:  # noqa: BLE001 - [EN] unreadable metadata is an asset fault
                             # [ZH] 元数据不可读属于资产故障
        raise VisionError(CODE_ASSET_MISSING, "media.vision_model_missing",
                          name=os.path.basename(model_path),
                          detail="cannot read metadata from model %s: %s"
                                 % (model_path, exc)) from exc
    try:
        mmproj_meta = read_metadata(mmproj_path)
    except Exception as exc:  # noqa: BLE001
        raise VisionError(CODE_ASSET_MISSING, "media.vision_mmproj_missing",
                          name=os.path.basename(mmproj_path),
                          detail="cannot read metadata from mmproj %s: %s"
                                 % (mmproj_path, exc)) from exc

    state, evidence = classify_pairing(model_meta, mmproj_meta)
    if state == PAIRING_MISMATCH:
        evidence["state"] = state
        raise VisionError(
            CODE_PAIR_MISMATCH, "media.vision_failed",
            detail=("%s: %s + %s" % (evidence.get("reason", "pair mismatch"),
                                     os.path.basename(model_path),
                                     os.path.basename(mmproj_path))),
            pairing=evidence)
    evidence["state"] = state
    return evidence


def image_budget_from_config(media_cfg: Any) -> "tuple[int, int]":
    """
    [EN]
    Resolve the effective ``(image_min_tokens, image_max_tokens)`` from the media
    configuration.

    The wiring step passes the result to :class:`NativeVLM`; keeping the resolution
    here means the defaults live in exactly one place and ``status()`` can report
    what was actually used, which is the only way support can distinguish "slow" or
    "garbage output" from "misconfigured" after the fact.

    [ZH]
    从媒体配置解析实际生效的 ``(image_min_tokens, image_max_tokens)``。

    接线步把结果传给 :class:`NativeVLM`；把解析放在这里意味着默认值只有一处定义，且
    ``status()`` 能报告真正使用的值 —— 这是支持人员事后区分「慢」/「输出垃圾」与
    「配置错误」的唯一依据。
    """
    minimum = getattr(media_cfg, "vision_image_min_tokens", DEFAULT_IMAGE_MIN_TOKENS)
    maximum = getattr(media_cfg, "vision_image_max_tokens", DEFAULT_IMAGE_MAX_TOKENS)
    minimum = int(minimum) if minimum else DEFAULT_IMAGE_MIN_TOKENS
    maximum = int(maximum) if maximum else DEFAULT_IMAGE_MAX_TOKENS
    if maximum < minimum:
        maximum = minimum
    return minimum, maximum


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
                 image_min_tokens: int = DEFAULT_IMAGE_MIN_TOKENS,
                 image_max_tokens: int = DEFAULT_IMAGE_MAX_TOKENS) -> None:
        self.model_path = os.path.abspath(model_path) if model_path else ""
        self.mmproj_path = os.path.abspath(mmproj_path) if mmproj_path else ""
        self.n_ctx = max(1024, int(n_ctx or DEFAULT_VISION_CONTEXT))
        self.n_threads = int(n_threads or 0)
        self.n_gpu_layers = int(n_gpu_layers or 0)
        self.verbose = bool(verbose)
        self._n_batch = min(max(64, self.n_ctx), 512)
        # [EN] Qwen-VL tokenizers warn that the default grid is too coarse for
        # [EN] grounding; 0 means "use the module default", never "disable".
        # [ZH] Qwen-VL 分词器警告默认网格对 grounding 过粗；0 表示「使用模块默认值」，
        # [ZH] 而不是「关闭」。
        self.image_min_tokens = (int(image_min_tokens) if image_min_tokens
                                 else DEFAULT_IMAGE_MIN_TOKENS)
        self.image_max_tokens = (int(image_max_tokens) if image_max_tokens
                                 else DEFAULT_IMAGE_MAX_TOKENS)
        if self.image_max_tokens < self.image_min_tokens:
            self.image_max_tokens = self.image_min_tokens
        self.pairing: Dict[str, Any] = {}
        self.pairing_state = PAIRING_UNVERIFIED
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
        self.pairing_state = str(self.pairing.get("state") or PAIRING_UNVERIFIED)
        if self.pairing_state == PAIRING_UNVERIFIED:
            # [EN] Unknown is not wrong: load it, but make the state visible and say
            # [EN] it once in the log so a later "garbage output" report is traceable.
            # [ZH] 未知不等于错误：仍然加载，但让状态可见并在日志里说一次，使日后
            # [ZH] 「输出是垃圾」的反馈可以追溯。
            log.warning(
                "视觉配对无法从元数据判定（pairing=unverified），按可用处理：%s + %s；原因：%s",
                os.path.basename(self.model_path), os.path.basename(self.mmproj_path),
                self.pairing.get("reason", ""))

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
                raise VisionError(CODE_RUNTIME_UNAVAILABLE, "media.vision_failed", detail=(
                    "llama_model_load_from_file returned NULL: %s" % self.model_path))
            self.timings["model_load_ms"] = (time.time() - started) * 1000

            ctx_params = llama_abi.llama_context_default_params()
            ctx_params.n_ctx = self.n_ctx
            ctx_params.n_batch = min(self.n_ctx, 512)
            if hasattr(ctx_params, "n_ubatch"):
                ctx_params.n_ubatch = min(self.n_ctx, 512)
            ctx_params.n_threads = self.n_threads
            self._ctx = llama_abi.llama_init_from_model(self._model, ctx_params)
            if not self._ctx:
                raise VisionError(CODE_RUNTIME_UNAVAILABLE, "media.vision_failed", detail=(
                    "llama_init_from_model returned NULL: %s" % self.model_path))

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
                raise VisionError(CODE_PAIR_MISMATCH, "media.vision_failed", detail=(
                    "mtmd_init_from_file rejected the projector for this model: "
                    "%s + %s" % (os.path.basename(self.model_path),
                                 os.path.basename(self.mmproj_path))))
            if not bool(mtmd_cpp.mtmd_support_vision(self._mctx)):
                raise VisionError(CODE_PAIR_MISMATCH, "media.vision_failed", detail=(
                    "projector reports no vision support: %s"
                    % os.path.basename(self.mmproj_path)))
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
            "pairing_state": self.pairing_state,
            "image_min_tokens": self.image_min_tokens,
            "image_max_tokens": self.image_max_tokens,
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
            raise VisionError(CODE_RUNTIME_UNAVAILABLE, "media.vision_failed",
                              detail="native vision engine is closed")
        if isinstance(image_path, (str, bytes, os.PathLike)):
            paths: List[str] = [os.fspath(image_path)]
        else:
            paths = [os.fspath(item) for item in image_path]
        if not paths:
            raise VisionError(CODE_IMAGE_DECODE_ERROR, "media.error.bad_image",
                              path="", detail="no image was supplied")

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
                    raise VisionError(CODE_RUNTIME_UNAVAILABLE, "media.vision_failed", detail=(
                        "mtmd_helper_eval_chunks failed with code %d" % rc))
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
            raise VisionError(CODE_IMAGE_DECODE_ERROR, "media.error.bad_image",
                              path=path, detail="image not found: %s" % path)
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise VisionError(CODE_IMAGE_DECODE_ERROR, "media.error.bad_image",
                              path=path,
                              detail="cannot read image %s: %s" % (path, exc)) from exc
        if not data:
            raise VisionError(CODE_IMAGE_DECODE_ERROR, "media.error.bad_image", path=path, detail=("image is empty: %s" % path))
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        wrapper = self._mtmd.mtmd_helper_bitmap_init_from_buf(
            self._mctx, buffer, len(data), False, self._bitmap_opt)
        bitmap = wrapper.bitmap if wrapper else None
        if not bitmap:
            raise VisionError(CODE_IMAGE_DECODE_ERROR, "media.error.bad_image", path=path, detail=(
                "mtmd could not decode %s (unsupported or corrupt image)" % path))
        return bitmap

    def _tokenize(self, bitmaps, prompt: str):
        """[EN] Tokenize ``marker*N + prompt`` against the given bitmaps. / [ZH] 用给定位图对 ``marker*N + prompt`` 分词。"""
        text = "".join(self._marker + "\n" for _ in bitmaps) + str(prompt or "")
        chunks = self._mtmd.mtmd_input_chunks_init()
        if not chunks:
            raise VisionError(CODE_RUNTIME_UNAVAILABLE, "media.vision_failed",
                              detail="mtmd_input_chunks_init returned NULL")
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
                raise VisionError(CODE_IMAGE_DECODE_ERROR, "media.error.bad_image", path=path, detail=(
                    "mtmd_tokenize rejected the input (code %d): %d marker(s) / "
                    "%d bitmap(s)" % (rc, text.count(self._marker), count)))
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

