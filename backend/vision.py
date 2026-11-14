# -*- coding: utf-8 -*-
"""
[EN]
Local vision-language inference (VLM) adapter.

The adapter keeps multimodal loading strictly optional. A vision request is
accepted only when all three conditions hold:

1. the configured (or auto-detected) vision model file exists;
2. a multimodal projector (``mmproj``) file is present next to that model or is
   configured explicitly;
3. the installed ``llama-cpp-python`` build exposes a compatible chat handler.

Dependency evaluation (v0.2.6 Part 2): no new Python distribution is introduced.
The multimodal projector and the vision model are ordinary GGUF files downloaded
through the existing model hub, and the chat handler ships with the already
locked ``llama-cpp-python``. ``Qwen25VLChatHandler`` is selected for Qwen-family
VL checkpoints, and ``Llava15ChatHandler`` is the fallback for LLaVA-format
checkpoints. Older runtimes that predate Qwen3-VL support still load a Qwen3-VL
checkpoint through the Qwen2.5-VL handler; if the bundled llama.cpp build lacks
the required vision projector implementation, the handler raises and this module
reports ``media.vision_failed`` instead of crashing the backend.

Image transport: the local file is read with the standard library and encoded as
a base64 ``data:`` URI before it reaches the chat handler. A raw Windows path is
never passed as ``image_url.url``, because handlers that resolve non-``data:``
values through ``urllib`` fail with ``unknown url type: c``.

[ZH]
本地视觉语言推理（VLM）适配层。

本适配层严格保持多模态加载的可选性。仅当以下三项条件全部成立时才受理视觉请求：

1. 已配置（或自动探测到）的视觉模型文件存在；
2. 该模型旁存在多模态投影（``mmproj``）文件，或被显式配置；
3. 已安装的 ``llama-cpp-python`` 构建暴露了兼容的聊天处理器。

依赖评估（v0.2.6 Part 2）：不引入任何新的 Python 发行包。多模态投影与视觉模型
都是普通 GGUF 文件，经由既有模型中心下载；聊天处理器随已锁定的
``llama-cpp-python`` 一同发布。Qwen 系列 VL 检查点选用
``Qwen25VLChatHandler``，LLaVA 格式检查点回退到 ``Llava15ChatHandler``。早于
Qwen3-VL 支持的运行时仍可通过 Qwen2.5-VL 处理器加载 Qwen3-VL 检查点；若随包
llama.cpp 构建缺少所需的视觉投影实现，处理器会抛出异常，本模块上报
``media.vision_failed``，而不是令后端崩溃。

图片传输：本地文件由标准库读取并编码为 base64 ``data:`` URI 后才交给聊天处理器。
绝不把原始 Windows 路径作为 ``image_url.url`` 传入，因为经 ``urllib`` 解析非
``data:`` 值的处理器会以 ``unknown url type: c`` 失败。
"""
from __future__ import annotations

import base64
import glob
import logging
import os
from typing import Any, Dict, List, Optional

from i18n import t as _t
# [EN] Reuse the process-wide inference mutex from the text engine: a vision turn
# [EN] decodes on the same llama.cpp runtime, and llama.cpp is not thread-safe.
# [ZH] 复用文本引擎的进程级推理互斥锁：视觉轮解码同样运行在同一套 llama.cpp
# [ZH] 运行时之上，而 llama.cpp 不是线程安全的。
from llm_engine import _INFERENCE_LOCK

log = logging.getLogger("EchoDesk.Vision")

#: [EN] Default context window for a vision turn. Image embeddings consume several
#: [EN] hundred tokens before the first text token, so the text default (2048) is
#: [EN] too small for a screenshot plus a question.
#: [ZH] 视觉轮的默认上下文窗口。图像嵌入在首个文本 token 之前就占用数百 token，
#: [ZH] 因此文本默认值（2048）对"截图 + 提问"过小。
DEFAULT_VISION_CONTEXT = 4096

#: [EN] Upper bound applied to a configured context value for vision turns.
#: [ZH] 视觉轮对配置上下文取值施加的上限。
MAX_VISION_CONTEXT = 8192

#: [EN] File-name markers that identify a vision-language checkpoint. The list is
#: [EN] advisory only: an existing multimodal projector always proves vision
#: [EN] capability regardless of the checkpoint name. It exists so that a
#: [EN] text-only model is reported as "not a VL model" instead of "missing a
#: [EN] projector", and so that native multimodal families such as Qwen3.5 are
#: [EN] recognized even when their file name does not contain ``vl``.
#: [ZH] 用于识别视觉语言检查点的文件名标记。该清单仅作提示：只要存在多模态投影
#: [ZH] 文件，无论检查点名称如何都应视为具备视觉能力。它的作用是让纯文本模型
#: [ZH] 报告为「不是 VL 模型」而非「缺少投影文件」，并识别 Qwen3.5 这类文件名中
#: [ZH] 不含 ``vl`` 的原生多模态系列。
VISION_NAME_HINTS = (
    "vl", "vision", "multimodal", "mmproj", "llava", "minicpm", "internvl",
    "pixtral", "mllama", "gemma3", "qwen3.5", "qwen3_5", "qwen35", "smolvlm",
    "idefics", "cogvlm", "glm-4v", "glm4v",
)


def model_name_suggests_vision(model_path: str) -> bool:
    """
    [EN]
    Return whether the checkpoint name indicates a vision-language model.

    [ZH]
    判断检查点名称是否指示视觉语言模型。
    """
    name = os.path.basename(model_path or "").lower()
    return any(hint in name for hint in VISION_NAME_HINTS)


class VisionError(Exception):
    """
    [EN]
    A rejected or failed vision operation, carrying a language-pack key.

    [ZH]
    被拒绝或失败的视觉操作，携带语言包键。
    """

    def __init__(self, code: str, key: str, **kwargs: object) -> None:
        super().__init__(code)
        self.code = str(code)
        self.key = str(key)
        self.kwargs = dict(kwargs)

    def localize(self, translate) -> str:
        """[EN] Render through the supplied translator. / [ZH] 通过给定翻译函数渲染。"""
        try:
            return str(translate(self.key, **self.kwargs))
        except Exception:  # noqa: BLE001 - [EN] never mask the original reason
                           # [ZH] 绝不掩盖原始原因
            return self.code


def find_mmproj(model_path: str, explicit: str = "") -> str:
    """
    [EN]
    Resolve the multimodal projector that belongs to ``model_path``.

    An explicit configuration value wins. Otherwise ``mmproj*.gguf`` files in the
    model's directory are considered, preferring ``Q8_0`` (the quantisation used by
    the project's model add-on), then ``F16``/``BF16``, then any remaining file.
    The function never loads the file; it only resolves a path.

    [ZH]
    解析属于 ``model_path`` 的多模态投影文件。

    显式配置值优先。否则考虑模型目录内的 ``mmproj*.gguf``，优先 ``Q8_0``
    （本项目模型补充包使用的量化档），其次 ``F16``/``BF16``，最后是其余文件。
    本函数从不加载文件，只解析路径。
    """
    if explicit and os.path.isfile(explicit):
        return os.path.abspath(explicit)
    if not model_path:
        return ""
    directory = os.path.dirname(os.path.abspath(model_path))
    candidates: List[str] = sorted(glob.glob(os.path.join(directory, "mmproj*.gguf")))
    if not candidates:
        return ""
    preference = ("q8_0", "f16", "bf16")
    for token in preference:
        for candidate in candidates:
            if token in os.path.basename(candidate).lower():
                return os.path.abspath(candidate)
    return os.path.abspath(candidates[0])


def _handler_names(model_path: str) -> List[str]:
    """
    [EN]
    Ordered chat-handler class names for a model file, most specific first.

    The names are resolved lazily against ``llama_cpp.llama_chat_format`` so that
    a build without a given handler degrades to the next candidate instead of
    failing at import time.

    [ZH]
    针对模型文件的有序聊天处理器类名，最具体的在前。

    这些名称在 ``llama_cpp.llama_chat_format`` 中惰性解析，使缺少某个处理器的
    构建退回到下一个候选项，而不是在导入期失败。
    """
    name = os.path.basename(model_path or "").lower()
    candidates: List[str] = []
    qwen_visual = "qwen" in name and (
        "vl" in name or "qwen3.5" in name or "qwen3_5" in name or "qwen35" in name
    )
    if qwen_visual:
        candidates.extend(("Qwen3VLChatHandler", "Qwen25VLChatHandler"))
    elif "llava" in name:
        candidates.append("Llava15ChatHandler")
    elif "minicpm" in name:
        candidates.append("MiniCPMv26ChatHandler")
    else:
        candidates.extend(("Qwen25VLChatHandler", "Llava15ChatHandler"))
    # [EN] Always keep a generic fallback: the checkpoint name is not a contract.
    # [ZH] 始终保留通用回退项：检查点文件名不是契约。
    for name_fallback in ("Qwen25VLChatHandler", "Llava15ChatHandler"):
        if name_fallback not in candidates:
            candidates.append(name_fallback)
    return candidates


def resolve_chat_handler(model_path: str, mmproj_path: str):
    """
    [EN]
    Instantiate the first available chat handler for this model.

    Returns a tuple ``(handler, class_name)``. Raises :class:`VisionError` with
    ``media.vision_handler_missing`` when the installed runtime exposes none of
    the candidates.

    [ZH]
    为该模型实例化第一个可用的聊天处理器。

    返回 ``(handler, class_name)`` 元组。若已安装运行时不暴露任何候选处理器，
    抛出代码为 ``media.vision_handler_missing`` 的 :class:`VisionError`。
    """
    try:
        from llama_cpp import llama_chat_format
    except Exception as exc:  # noqa: BLE001 - [EN] report the runtime, never crash
                             # [ZH] 报告运行时问题，绝不崩溃
        raise VisionError("handler_missing", "media.vision_handler_missing",
                          detail=str(exc)) from exc
    tried: List[str] = []
    for name in _handler_names(model_path):
        cls = getattr(llama_chat_format, name, None)
        if cls is None:
            tried.append(name)
            continue
        try:
            return cls(clip_model_path=mmproj_path, verbose=False), name
        except Exception as exc:  # noqa: BLE001 - [EN] try the next compatible handler
                                 # [ZH] 尝试下一个兼容处理器
            log.warning("视觉聊天处理器 %s 初始化失败：%s", name, exc)
            tried.append(name)
    raise VisionError("handler_missing", "media.vision_handler_missing",
                      detail=", ".join(tried))


#: [EN] MIME types for the image formats accepted by the vision path. A data URI
#: [EN] needs an explicit media type; the extension is mapped here instead of being
#: [EN] guessed, so an unsupported suffix is rejected deterministically.
#: [ZH] 视觉链路接受格式的 MIME 类型。data URI 必须携带明确的媒体类型；此处按扩展名
#: [ZH] 映射而非猜测，使不受支持的后缀被确定性地拒绝。
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def image_data_uri(image_path: str) -> str:
    """
    [EN]
    Encode a local image as a ``data:`` URI for the chat handler.

    The image bytes are read with the standard library and base64-encoded; the
    filesystem path never reaches the handler. This is required because some
    llama-cpp-python chat handlers pass ``image_url.url`` to ``urllib`` when it does
    not start with ``data:``: a Windows path such as ``C:\\Users\\...png`` is then
    interpreted as a URL with scheme ``c`` and raises
    ``URLError: <urlopen error unknown url type: c>``. A data URI is unambiguous for
    every handler, keeps the image local, and does not change the line protocol
    (only the path still travels between the frontend and backend).

    The size, containment and magic-byte policy is applied earlier by
    :func:`media.validate_image_path`; this helper still rejects a missing,
    empty or unsupported file so it is safe when called directly.

    [ZH]
    把本地图片编码为 ``data:`` URI 供聊天处理器使用。

    图片字节由标准库读取并做 base64 编码；文件系统路径绝不会到达处理器。这是必需
    的：部分 llama-cpp-python 聊天处理器在 ``image_url.url`` 不以 ``data:`` 开头时
    会把它交给 ``urllib``，于是 ``C:\\Users\\...png`` 被当作 scheme 为 ``c`` 的 URL，
    抛出 ``URLError: <urlopen error unknown url type: c>``。data URI 对任何处理器都
    无歧义、图片始终留在本机，且不改变行协议（前端与后端之间仍只传路径）。

    尺寸、目录包含关系与魔数策略已由 :func:`media.validate_image_path` 先行施加；
    本函数仍会拒绝缺失、空文件或不受支持的后缀，因此可被直接安全调用。
    """
    raw_path = str(image_path or "").strip()
    resolved = os.path.abspath(raw_path) if raw_path else ""
    if not resolved or not os.path.isfile(resolved):
        raise VisionError("image_missing", "media.error.not_found", path=raw_path)
    extension = os.path.splitext(resolved)[1].lower()
    mime = _IMAGE_MIME.get(extension)
    if not mime:
        raise VisionError("bad_image", "media.error.bad_image", path=resolved)
    try:
        with open(resolved, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise VisionError("image_unreadable", "media.error.unreadable",
                          path=resolved, reason=str(exc)) from exc
    if not raw:
        raise VisionError("image_empty", "media.error.empty", path=resolved)
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class VisionEngine:
    """
    [EN]
    One loaded vision model. Constructing the engine loads the model, which is
    expensive; :class:`VisionService` creates it on demand and reuses it.

    [ZH]
    一个已加载的视觉模型。构造引擎即加载模型，代价较高；
    :class:`VisionService` 按需创建并复用。
    """

    def __init__(self, model_path: str, mmproj_path: str,
                 n_ctx: int = DEFAULT_VISION_CONTEXT,
                 n_threads: int = 0, n_gpu_layers: int = 0,
                 verbose: bool = False) -> None:
        self.model_path = os.path.abspath(model_path)
        self.mmproj_path = os.path.abspath(mmproj_path)
        self.n_ctx = max(1024, int(n_ctx))
        self.handler_name = ""
        self.llm: Any = None
        self._load(verbose=verbose, n_threads=n_threads, n_gpu_layers=n_gpu_layers)

    def _load(self, verbose: bool, n_threads: int, n_gpu_layers: int) -> None:
        """[EN] Construct the llama.cpp object with the multimodal handler. / [ZH] 以多模态处理器构建 llama.cpp 对象。"""
        from llama_cpp import Llama

        handler, self.handler_name = resolve_chat_handler(self.model_path, self.mmproj_path)
        kwargs: Dict[str, Any] = {
            "model_path": self.model_path,
            "chat_handler": handler,
            "n_ctx": self.n_ctx,
            "verbose": bool(verbose),
        }
        if n_threads:
            kwargs["n_threads"] = int(n_threads)
        if n_gpu_layers:
            kwargs["n_gpu_layers"] = int(n_gpu_layers)
        # [EN] Constructing a native model mutates shared backend state and must
        # [EN] not overlap with an in-flight decode elsewhere.
        # [ZH] 构建原生模型会改变共享后端状态，绝不能与其他正在进行的解码重叠。
        with _INFERENCE_LOCK:
            self.llm = Llama(**kwargs)
        log.info("视觉模型已加载：%s（处理器 %s）",
                 os.path.basename(self.model_path), self.handler_name)

    def chat(self, image_path: str, prompt: str, max_tokens: int = 512,
             temperature: float = 0.3) -> str:
        """
        [EN]
        Run one vision turn: the user prompt plus the local image.

        The image is read locally and passed to the handler as a base64 ``data:``
        URI (see :func:`image_data_uri`). A raw filesystem path must not be placed
        in ``image_url.url``: handlers that treat a non-``data:`` value as a URL
        would try to open ``C:\\...`` and fail with ``unknown url type: c``.

        [ZH]
        执行一次视觉轮：用户提示词加本地图片。

        图片在本地读取，并以 base64 ``data:`` URI 交给处理器（见
        :func:`image_data_uri`）。绝不能把原始文件系统路径放进 ``image_url.url``：
        会把非 ``data:`` 值当作 URL 的处理器将尝试打开 ``C:\\...``，并以
        ``unknown url type: c`` 失败。
        """
        if self.llm is None:
            raise VisionError("not_loaded", "media.vision_failed", detail="engine not loaded")
        # [EN] Encode before building the request so a read/format failure is
        # [EN] reported as a VisionError instead of an opaque handler exception.
        # [ZH] 在构造请求前完成编码，使读取/格式失败以 VisionError 上报，而不是
        # [ZH] 变成处理器内部的不透明异常。
        image_url = image_data_uri(image_path)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": str(prompt)},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }]
        # [EN] A vision completion runs prefill and decode in one native call, so
        # [EN] the mutex covers the whole call and queues behind chat/summarization.
        # [ZH] 视觉补全在单次原生调用内完成 prefill 与 decode，因此互斥锁覆盖全程，
        # [ZH] 并在聊天/摘要之后排队。
        with _INFERENCE_LOCK:
            result = self.llm.create_chat_completion(
                messages=messages,
                max_tokens=max(16, int(max_tokens)),
                temperature=float(temperature),
            )
        try:
            content = result["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - [EN] malformed runtime response
                                 # [ZH] 运行时返回结构异常
            raise VisionError("bad_response", "media.vision_failed", detail=str(exc)) from exc
        return str(content or "").strip()

    def close(self) -> None:
        """[EN] Release the native model. / [ZH] 释放原生模型。"""
        # [EN] Freeing the model while another thread decodes would be a
        # [EN] use-after-free; wait for any in-flight native operation.
        # [ZH] 在另一线程解码时释放模型会造成释放后使用；必须等待进行中的原生操作。
        with _INFERENCE_LOCK:
            llm = self.llm
            self.llm = None
            close = getattr(llm, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - [EN] cleanup must not mask errors
                                       # [ZH] 清理过程不得掩盖错误
                    log.debug("释放视觉模型失败（忽略）", exc_info=True)


class VisionService:
    """
    [EN]
    Lazy owner of the vision engine, used by the dispatcher.

    ``status()`` is cheap and reports why vision is unavailable without attempting
    to load a model; ``chat()`` loads on first use and re-raises a localized
    failure reason.

    [ZH]
    视觉引擎的惰性持有者，供分发层使用。

    ``status()`` 代价很低，可在不尝试加载模型的情况下报告视觉不可用的原因；
    ``chat()`` 首次使用时加载模型，并抛出已本地化的失败原因。
    """

    def __init__(self, model_path: str = "", mmproj_path: str = "",
                 n_ctx: int = DEFAULT_VISION_CONTEXT,
                 n_threads: int = 0, n_gpu_layers: int = 0) -> None:
        self.model_path = os.path.abspath(model_path) if model_path else ""
        self.mmproj_path = os.path.abspath(mmproj_path) if mmproj_path else ""
        self.n_ctx = min(MAX_VISION_CONTEXT, max(1024, int(n_ctx or DEFAULT_VISION_CONTEXT)))
        self.n_threads = int(n_threads or 0)
        self.n_gpu_layers = int(n_gpu_layers or 0)
        self._engine: Optional[VisionEngine] = None
        self.last_error = ""

    def configure(self, model_path: str, mmproj_path: str = "",
                  n_ctx: Optional[int] = None) -> None:
        """
        [EN]
        Point the service at the given model pair, discarding the loaded engine only
        when the pair really changed. The dispatcher calls this on every media
        action so that a model switch performed through the settings page is
        observed without a second notification path.

        [ZH]
        将服务指向给定的模型组合；仅当组合确实变化时才丢弃已加载引擎。分发层在每次
        媒体动作时调用，使经设置页执行的模型切换无需第二条通知路径即可被观察到。
        """
        new_model = os.path.abspath(model_path) if model_path else ""
        new_mmproj = os.path.abspath(mmproj_path) if mmproj_path else ""
        new_ctx = min(MAX_VISION_CONTEXT, max(1024, int(n_ctx))) if n_ctx else self.n_ctx
        if (new_model == self.model_path and new_mmproj == self.mmproj_path
                and new_ctx == self.n_ctx):
            return
        self.close()
        self.model_path = new_model
        self.mmproj_path = new_mmproj
        self.n_ctx = new_ctx
        self.last_error = ""

    def reload(self, model_path: str, mmproj_path: str = "",
               n_ctx: Optional[int] = None) -> None:
        """
        [EN] Point the service at a different model and discard the loaded engine.
        [ZH] 将服务指向另一个模型，并丢弃已加载引擎。
        """
        self.close()
        self.model_path = os.path.abspath(model_path) if model_path else ""
        self.mmproj_path = os.path.abspath(mmproj_path) if mmproj_path else ""
        if n_ctx:
            self.n_ctx = min(MAX_VISION_CONTEXT, max(1024, int(n_ctx)))
        self.last_error = ""

    def status(self) -> Dict[str, Any]:
        """
        [EN]
        Machine-readable availability report for ``media_state``.

        [ZH]
        供 ``media_state`` 使用的机器可读可用性报告。
        """
        reason = ""
        if not self.model_path:
            reason = "media.vision_no_model"
        elif not os.path.isfile(self.model_path):
            reason = "media.vision_model_missing"
        elif not self.mmproj_path:
            # [EN] Without a projector, a text-only checkpoint is not a vision model;
            # [EN] report that distinction instead of implying that a projector file is
            # [EN] merely missing. The interface hides the image entry in this case.
            # [ZH] 没有投影文件时，纯文本检查点并非视觉模型；应报告这一区别，而不是暗示
            # [ZH] 只是缺少投影文件。此时界面会隐藏图片入口。
            reason = ("media.vision_no_mmproj"
                      if model_name_suggests_vision(self.model_path)
                      else "media.vision_not_vlm")
        elif not os.path.isfile(self.mmproj_path):
            reason = "media.vision_mmproj_missing"
        return {
            "available": not reason,
            "is_vlm": model_name_suggests_vision(self.model_path),
            "model": os.path.basename(self.model_path),
            "model_path": self.model_path,
            "mmproj": os.path.basename(self.mmproj_path),
            "mmproj_path": self.mmproj_path,
            "loaded": self._engine is not None,
            "handler": getattr(self._engine, "handler_name", ""),
            "reason": reason,
            "reason_text": _t(reason) if reason else "",
            "last_error": self.last_error,
        }

    def chat(self, image_path: str, prompt: str, max_tokens: int = 512,
             temperature: float = 0.3) -> str:
        """
        [EN]
        Ensure the engine is loaded and run one vision turn.

        [ZH]
        确保引擎已加载，并执行一次视觉轮。
        """
        if not self.model_path:
            raise VisionError("no_model", "media.vision_no_model")
        if not os.path.isfile(self.model_path):
            raise VisionError("model_missing", "media.vision_model_missing",
                              name=os.path.basename(self.model_path))
        if not self.mmproj_path:
            raise VisionError("no_mmproj", "media.vision_no_mmproj")
        if not os.path.isfile(self.mmproj_path):
            raise VisionError("mmproj_missing", "media.vision_mmproj_missing",
                              name=os.path.basename(self.mmproj_path))
        if self._engine is None:
            try:
                self._engine = VisionEngine(
                    self.model_path, self.mmproj_path, n_ctx=self.n_ctx,
                    n_threads=self.n_threads, n_gpu_layers=self.n_gpu_layers,
                )
            except VisionError:
                raise
            except Exception as exc:  # noqa: BLE001 - [EN] surface the runtime reason
                                     # [ZH] 上报运行时原因
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise VisionError("load_failed", "media.vision_failed",
                                  detail=self.last_error) from exc
        try:
            return self._engine.chat(image_path, prompt, max_tokens=max_tokens,
                                     temperature=temperature)
        except VisionError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise VisionError("chat_failed", "media.vision_failed",
                              detail=self.last_error) from exc

    def close(self) -> None:
        """[EN] Release the loaded engine, if any. / [ZH] 释放已加载引擎（如有）。"""
        engine = self._engine
        self._engine = None
        if engine is not None:
            engine.close()
