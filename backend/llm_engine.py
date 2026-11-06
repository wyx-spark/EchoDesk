"""
[EN]
LLM inference engine wrapper built on llama-cpp-python. It supports streaming and
non-streaming completion, interruption, and cache management.

It is additionally responsible for:
  * Reporting the active context window (configured value and the model training
    window) whenever a model is loaded or reloaded;
  * Estimating KV Cache memory usage from GGUF metadata;
  * Recording generation throughput for the "slower after extending the context"
    hint;
  * Separating reasoning content from the final answer. Reasoning models (Qwen3
    thinking mode, DeepSeek-R1, and similar) emit the reasoning trace inside
    ``content``, wrapped in tags. llama-cpp-python 0.3.x exposes neither a
    ``reasoning_format`` option nor a ``reasoning_content`` field, so the split is
    performed in this layer by the tag splitter in :mod:`reasoning`:
    ``chat_stream_split`` and ``chat_complete_split`` expose both channels, while
    ``chat_stream`` and ``chat_complete`` keep the compatible semantics of
    returning only the final answer, so summarization and automation callers
    never receive reasoning content by construction.

[ZH]
基于 llama-cpp-python 的 LLM 推理引擎封装，支持流式与非流式补全、中断以及缓存管理。

同时负责：
  * 模型加载/重载时输出当前上下文窗口（配置值与模型训练窗口）；
  * 依据 GGUF 元数据估算 KV Cache 内存占用；
  * 记录生成速度，供"扩展上下文后变慢"的提示使用；
  * 思考内容与最终回答的分流：推理模型（Qwen3 thinking 模式、DeepSeek-R1 等）会把
    思考过程写在 ``content`` 内并以标签包裹，而 llama-cpp-python 0.3.x 既没有
    ``reasoning_format`` 选项，也没有 ``reasoning_content`` 字段，因此分流在本层由
    :mod:`reasoning` 的标签分离器完成：``chat_stream_split`` / ``chat_complete_split``
    暴露双通道，``chat_stream`` / ``chat_complete`` 则保持"只返回最终回答"的兼容语义
    （摘要、自动化等调用方天然拿不到思考内容）。
"""
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from gguf_meta import describe_model, estimate_kv_cache_bytes, format_bytes, resolve_context_window
from i18n import log_user
from i18n import t as _t
from reasoning import ANSWER, ReasoningSplitter, normalize_tags, split_text

log = logging.getLogger("EchoDesk.LLM")

#: [EN] Default interval (seconds) for emitting a heartbeat while a streaming read
#: produces no new tokens. Prefill is slow and decode is fast, so the first token
#: may take more than ten seconds on a CPU-only host; without heartbeats the UI
#: cannot distinguish "no output yet" from "stalled". A heartbeat every 1.5 seconds
#: removes that ambiguity without flooding the protocol (a long reply remains
#: bounded to a few hundred heartbeat messages).
#: [ZH] 流式读取时"无新 token 即发心跳"的默认间隔（秒）。prefill 阶段慢、decode
#: 阶段快，纯 CPU 环境下首 token 可能需要等待十几秒，界面无法区分"暂无输出"与
#: "已卡死"。每 1.5 秒发送一次心跳可消除该歧义，又不会使协议消息量失控（一次长回复
#: 的心跳数量上限约为数百条）。
DEFAULT_HEARTBEAT_SECONDS = 1.5

#: [EN] Dedicated marker for heartbeat events in the channel stream:
#: ``(HEARTBEAT, "")``. It is not model output;
#: :meth:`LLMEngine.chat_stream_split` only forwards it as a signal, and the
#: splitter never sees it (otherwise a heartbeat would be treated as untagged body
#: text and fed into the answer channel).
#: [ZH] 心跳事件在通道流中的专用标记：``(HEARTBEAT, "")``。它并非模型输出，
#: :meth:`LLMEngine.chat_stream_split` 仅将其作为信号转发，分离器不会看到它
#: （否则心跳会被当作"无标签的正文"送入回答通道）。
HEARTBEAT = "heartbeat"

#: [EN] Sentinel used internally by the generator: the reader thread has finished,
#: either normally or by raising.
#: [ZH] 生成器内部使用的哨兵：读者线程已结束（正常结束或抛出异常）。
_EOF = object()


@dataclass
class StreamStats:
    """
    [EN]
    Runtime metrics of a single streaming generation, reported verbatim by
    ``main.Backend`` in the ``done`` event. The engine maintains them during
    generation. All fields are measured by the backend, not estimated by the
    frontend:

    ``first_token_ms``   Milliseconds from request dispatch to arrival of the first
                         token (time to first token);
    ``elapsed_ms``       Total milliseconds from start to end, refreshed live while
                         generating;
    ``prompt_chars``     Character count of the prompt sent to the model (input
                         size);
    ``output_chars``     Characters produced so far (including reasoning);
    ``tokens``           Output token count, measured with the model tokenizer;
    ``tokens_estimated`` Whether ``tokens`` is an estimate (True when the tokenizer
                         is unavailable);
    ``tps``              Tokens per second (tokens / generation time);
    ``prefill_done``     Whether the first token has been emitted; the frontend
                         uses it to display "waiting for first token";
    ``stopped``          Whether the user aborted generation.

    [ZH]
    单次流式生成的运行指标，由引擎在生成过程中维护，``main.Backend`` 在 ``done``
    事件中原样上报。所有字段均来自后端实测，而非前端估算：

    ``first_token_ms``   请求发出到首个 token 到达的毫秒数（首 token 延迟）；
    ``elapsed_ms``       从开始生成到结束的总毫秒数（生成过程中实时刷新）；
    ``prompt_chars``     送入模型的提示词字符数（输入规模）；
    ``output_chars``     已产出的字符数（含思考内容）；
    ``tokens``           输出 token 数（使用模型分词器精确计数）；
    ``tokens_estimated`` tokens 是否为估算值（分词器不可用时为 True）；
    ``tps``              token/s（tokens / 生成耗时）；
    ``prefill_done``     是否已产出首个 token（前端据此显示"等待首 token"）；
    ``stopped``          是否被用户中止。
    """

    first_token_ms: int = 0
    elapsed_ms: int = 0
    prompt_chars: int = 0
    output_chars: int = 0
    tokens: int = 0
    tokens_estimated: bool = False
    tps: float = 0.0
    prefill_done: bool = False
    stopped: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "first_token_ms": int(self.first_token_ms),
            "elapsed_ms": int(self.elapsed_ms),
            "prompt_chars": int(self.prompt_chars),
            "output_chars": int(self.output_chars),
            "tokens": int(self.tokens),
            "tokens_estimated": bool(self.tokens_estimated),
            "tps": round(float(self.tps), 2),
            "prefill_done": bool(self.prefill_done),
            "stopped": bool(self.stopped),
        }


class NoModelError(RuntimeError):
    """
    [EN]
    Raised by any call that genuinely requires inference when no model is loaded.

    A dedicated exception type is used because, in the no-model-ready state,
    ``chat``, ``chat_stream``, and segmented summarization must guide the user
    instead of failing. With this type, the guidance logic can catch
    ``NoModelError`` once at a few entry points rather than checking "is the engine
    empty" at dozens of call sites, where a single omission would be a crash. The
    ``message`` is already user-presentable text supplied by the caller from the
    language pack.

    [ZH]
    当前没有可用模型时，任何确实需要推理的调用都会抛出该异常。

    之所以使用专门的异常类型：在无模型就绪状态下，``chat`` / ``chat_stream`` /
    分段摘要都必须向用户给出引导而不是崩溃。有了该类型，引导逻辑只需在少数入口处
    ``except NoModelError`` 一次，无需在数十个调用点逐个判断"引擎是否为空"，漏掉
    一处即会导致崩溃。其 ``message`` 已是可直接展示给用户的文案（由调用方传入
    语言包文本）。
    """


class LLMEngine:
    """
    [EN]
    Local LLM inference engine backed by llama.cpp.
    [ZH]
    基于 llama.cpp 的本地大模型推理引擎。
    """

    def __init__(
        self,
        model_path: str,
        n_ctx: int = 8192,
        verbose: bool = False,
        n_threads: Optional[int] = None,
        n_batch: int = 512,
        n_gpu_layers: int = 0,
        reasoning=None,
    ) -> None:
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_batch = n_batch
        self.n_gpu_layers = n_gpu_layers
        # [EN] Reasoning separation (config.reasoning): `enabled` controls whether
        # the split is active, and `tags` is the list of additional tags. Passing
        # None (for example from a third-party or test construction) defaults to
        # enabled, preserving backward compatibility with the previous behavior.
        # [ZH] 思考内容分离（config.reasoning）：`enabled` 控制是否分流，`tags` 为
        # 追加标签表。传入 None（例如第三方或测试代码构造）时按默认开启处理，与旧行为
        # 向后兼容。
        self.reasoning_enabled = bool(getattr(reasoning, "enabled", True))
        self.reasoning_tags = normalize_tags(getattr(reasoning, "tags", None))
        # [EN] Model training window, read from GGUF metadata without loading the model.
        # [ZH] 模型训练窗口（来自 GGUF 元数据，不依赖模型加载）。
        self.n_ctx_train = resolve_context_window(model_path, default=0)
        self.kv_cache_bytes = estimate_kv_cache_bytes(model_path, n_ctx)
        # [EN] Generation throughput statistics (tokens per second).
        # [ZH] 生成速度统计（tokens/秒）。
        self.last_tps: float = 0.0
        self.last_elapsed: float = 0.0
        self.last_tokens: int = 0
        self.llm = self._create(verbose=verbose)

    def _create(self, verbose: bool = False):
        from llama_cpp import Llama

        llm = Llama(
            model_path=self.model_path,
            n_ctx=self.n_ctx,
            verbose=verbose,
            n_threads=self.n_threads,
            n_batch=self.n_batch,
            n_gpu_layers=self.n_gpu_layers,
        )
        # [EN] llama.cpp reports the effective and training context values; use them
        # to calibrate the metadata parsing result.
        # [ZH] llama.cpp 可报告实际生效值与训练值，以此校准元数据解析结果。
        try:
            self.n_ctx = int(llm.n_ctx())
        except Exception:
            pass
        try:
            self.n_ctx_train = int(llm.n_ctx_train()) or self.n_ctx_train
        except Exception:
            pass
        self.kv_cache_bytes = estimate_kv_cache_bytes(self.model_path, self.n_ctx)
        self.log_window()
        return llm

    def log_window(self) -> None:
        """
        [EN]
        Log the active context window size when a model is loaded or reloaded.
        [ZH]
        模型加载/重载时输出当前上下文窗口大小。
        """
        train = f"{self.n_ctx_train} tokens" if self.n_ctx_train else "未知"
        log.info(
            "上下文窗口: %d tokens（模型训练窗口 %s，KV Cache 约 %s，线程 %s，GPU 层 %d）",
            self.n_ctx, train, format_bytes(self.kv_cache_bytes),
            self.n_threads, self.n_gpu_layers,
        )

    def reload(self, model_path: Optional[str] = None, n_ctx: Optional[int] = None) -> None:
        """
        [EN]
        Reload the model, optionally changing the model path or context length.
        [ZH]
        重新加载模型，可更换模型路径或上下文长度。
        """
        if model_path:
            self.model_path = model_path
            self.n_ctx_train = resolve_context_window(model_path, default=0)
        if n_ctx:
            self.n_ctx = n_ctx
        self.llm = self._create(verbose=False)

    def load(self, model_path: str, n_ctx: Optional[int] = None) -> None:
        """
        [EN]
        Load, or switch to, the specified model. Same name and semantics as
        :meth:`LazyLLMEngine.load`.

        The parent class also needs this method because ``Backend.load_model()``
        (used by the "Load" button on the settings page, the model-tier "Switch"
        action, and automatic loading after a download) calls
        ``self.llm.load(path, n_ctx)``, while ``load`` previously existed only on
        ``LazyLLMEngine``, the subclass used when no model is ready. Consequently:
          * Starting without a model (the release default) yielded a
            ``LazyLLMEngine``, so hot loading worked;
          * Switching models after one was already loaded — the most common user
            action, for example moving from the 0.6B tier to the 1.7B tier —
            yielded a plain ``LLMEngine`` and failed with ``AttributeError:
            'LLMEngine' object has no attribute 'load'``, surfacing an error
            unrelated to the actual cause.
        Adding a same-named method to the parent class (forwarding to
        :meth:`reload`) decouples API availability from whether a model is already
        loaded. ``LazyLLMEngine.load`` keeps its own implementation, which builds
        the underlying object in place; the semantics match, so it is unchanged.

        [ZH]
        加载或切换到指定模型，与 :meth:`LazyLLMEngine.load` 同名同语义。

        父类同样需要该方法：``Backend.load_model()``（设置页的"加载"按钮、模型档位的
        "切换"操作、下载完成后的自动加载均经由此处）调用 ``self.llm.load(path, n_ctx)``，
        而 ``load`` 原先仅存在于 ``LazyLLMEngine``（无模型就绪时使用的子类）上。因此：
          * 启动时没有模型（发布版默认）→ 引擎为 LazyLLMEngine → 热加载正常；
          * 已加载模型后再切换模型（用户最常执行的操作，例如从 0.6B 档切到 1.7B 档）
            → 引擎为普通 ``LLMEngine`` → 抛出 ``AttributeError: 'LLMEngine' object has
            no attribute 'load'``，用户看到的是与真实原因无关的报错。
        为父类补充同名方法（转发到 :meth:`reload`）后，"是否已加载模型"不再决定 API
        是否存在。``LazyLLMEngine.load`` 保留自身实现（原地构建底层对象），语义一致，
        无需改动。
        """
        self.reload(model_path, n_ctx)

    # ---------- [EN] Model information and cache management ----------
    # ---------- [ZH] 模型信息与缓存管理 ----------

    def model_info(self) -> Dict[str, str]:
        """
        [EN]
        Return model metadata (architecture, quantization type, and similar).
        Returns an empty dictionary on failure.
        [ZH]
        返回模型元数据（架构、量化类型等），失败时返回空字典。
        """
        try:
            meta = getattr(self.llm, "metadata", None) or {}
            return {str(k): str(v) for k, v in meta.items() if isinstance(v, (str, int, float))}
        except Exception:
            return {}

    def describe(self) -> Dict[str, object]:
        """
        [EN]
        Aggregate model and window parameters for /model, /status, and load-log
        display.
        [ZH]
        汇总模型与窗口参数，供 /model、/status 与加载日志展示。
        """
        return describe_model(self.model_path, self.n_ctx)

    def kv_cache_text(self) -> str:
        """
        [EN]
        Human-readable text for KV Cache usage.
        [ZH]
        KV Cache 占用的可读文本。
        """
        return format_bytes(estimate_kv_cache_bytes(self.model_path, self.n_ctx) or self.kv_cache_bytes)

    def count_tokens(self, text: str) -> int:
        """
        [EN]
        Estimate the token count of the text with the model tokenizer. Falls back
        to a character-based estimate on failure.
        [ZH]
        使用模型分词器估算文本的 token 数；失败时退化为按字符粗估。
        """
        try:
            return len(self.llm.tokenize(text.encode("utf-8")))
        except Exception:
            return len(text) // 2

    def clear_cache(self) -> bool:
        """
        [EN]
        Clear the KV cache and the input history. Call this after any change to the
        mode, the prompt, or the history structure.
        [ZH]
        清空 KV 缓存与输入历史。模式、提示词或历史结构发生变化后应调用。
        """
        try:
            reset = getattr(self.llm, "reset", None)
            if callable(reset):
                reset()
                return True
        except Exception:
            pass
        return False

    # ---------- [EN] Conversation ----------
    # ---------- [ZH] 对话 ----------

    def make_splitter(self) -> Optional[ReasoningSplitter]:
        """
        [EN]
        Create a reasoning-tag splitter, or return ``None`` when separation is
        disabled.

        The caller (``main.Backend.stream``) passes it to
        :meth:`chat_stream_split` and can then read ``reasoning_text`` /
        ``unclosed`` from the same object after generation, so the generator does
        not have to return extra state.

        [ZH]
        创建思考标签分离器；分离关闭时返回 ``None``。

        调用方（``main.Backend.stream``）将其传给 :meth:`chat_stream_split`，生成结束
        后即可从同一对象上取得 ``reasoning_text`` / ``unclosed``，无需让生成器额外
        回传状态。
        """
        if not self.reasoning_enabled:
            return None
        return ReasoningSplitter(self.reasoning_tags)

    def _chat_complete_raw(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
    ) -> str:
        """
        [EN]
        Low-level non-streaming completion. Returns the raw model output, which may
        contain reasoning tags.
        [ZH]
        底层非流式补全，返回模型原样输出（可能包含思考标签）。
        """
        kwargs: Dict[str, object] = {
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # [EN] Preset tiers may specify top_p and a repetition penalty. When they are
        # omitted, do not forward them, so llama.cpp applies its own defaults and
        # the previous behavior is preserved.
        # [ZH] 预设档位可能需要 top_p 与重复惩罚；未指定时不传递，让 llama.cpp 使用
        # 自身默认值，以与旧行为保持一致。
        if top_p is not None:
            kwargs["top_p"] = float(top_p)
        if repeat_penalty is not None:
            kwargs["repeat_penalty"] = float(repeat_penalty)
        response = self.llm.create_chat_completion(**kwargs)
        return response["choices"][0]["message"]["content"]

    def chat_complete_split(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
    ) -> Tuple[str, str]:
        """
        [EN]
        Non-streaming completion. Returns ``(reasoning_content, final_answer)``.
        [ZH]
        非流式补全，返回 ``(思考内容, 最终回答)``。
        """
        text = self._chat_complete_raw(messages, temperature, max_tokens, top_p, repeat_penalty)
        if not self.reasoning_enabled:
            return "", text or ""
        thinking, answer = split_text(text or "", self.reasoning_tags)
        return thinking, answer

    def chat_complete(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
    ) -> str:
        """
        [EN]
        Execute one non-streaming chat completion and return the full final answer.

        Reasoning content is stripped: summarization, automation, and context
        compression callers need the conclusion only, and feeding the reasoning
        trace into them would waste tokens. Use :meth:`chat_complete_split` when
        the reasoning content is required.

        [ZH]
        执行一次非流式对话补全，返回最终回答全文。

        思考内容会被剥离：摘要、自动化与上下文压缩等调用方只需要结论，把思考过程传给
        它们只会额外消耗 token。需要思考内容时请使用 :meth:`chat_complete_split`。
        """
        return self.chat_complete_split(
            messages, temperature, max_tokens, top_p, repeat_penalty
        )[1]

    @staticmethod
    def _iter_with_heartbeat(
        stream,
        stop_event: Optional[threading.Event],
        heartbeat_seconds: float,
    ) -> Iterator[Tuple[str, Optional[str]]]:
        """
        [EN]
        Convert the blocking llama streaming iterator into an event stream of
        heartbeats and deltas.

        ## Why a separate thread is required

        The ``llama_cpp`` streaming generator blocks for tens of seconds during
        prefill; measured on CPU-only hardware, a 3000-character prompt takes more
        than ten seconds to emit its first token. If the consumer iterates
        ``for chunk in stream`` directly, nothing can be sent during that interval:
        no amount of flushing helps, because no data exists to flush. This accounts
        for the first half of the reported symptom where generation appears stalled
        and then a large batch of output arrives at once.

        Approach: a reader thread produces deltas into a bounded queue, and the
        consumer reads with ``get(timeout=...)``; a timeout emits one heartbeat.
        This yields:
        * No long silent interval: at least one heartbeat per
          ``heartbeat_seconds``;
        * Prompt interruption: the consumer returns as soon as stop_event is set,
          without waiting for the model to finish;
        * No batching: each delta is yielded immediately, with no coalescing buffer
          (coalescing belongs to frontend throttling; buffering in the backend
          would turn per-token delivery into batch delivery).

        Yields ``(kind, text)`` where ``kind`` is ``"delta"`` or ``"heartbeat"``,
        and yields ``("end", None)`` on completion or error.

        The stop signal must be visible to the reader thread. Regression tests
        caught this: if the reader thread drains the stream, the model keeps
        computing and its output keeps being consumed after the user presses
        "Stop". That wastes CPU, degrades "stop" into "stop displaying", and alters
        the established semantics of how much already-emitted content is delivered
        on stop (asserted by the interruption case in
        ``tools/test_reasoning.py``). The reader thread therefore checks
        ``stop_event`` before each fetch and terminates immediately when it is set,
        without requesting further tokens from the underlying model.

        [ZH]
        把阻塞的 llama 流式迭代器转换为"心跳 + 增量"事件流。

        ## 必须另起线程的原因

        ``llama_cpp`` 的流式生成器在 prefill 阶段会阻塞数十秒（纯 CPU 环境下，
        3000 字符的提示词实测需要十几秒才产出首个 token）。若消费者直接
        ``for chunk in stream``，该时间段内无法发出任何数据，flush 也无法解决，因为
        根本没有数据可 flush。观察到的"生成中途卡住、稍后一次性出现大量内容"现象，
        前半段即由此产生。

        做法：读者线程负责产出增量并放入有界队列，消费者通过 ``get(timeout=...)``
        读取；超时则产出一次心跳。由此得到：
        * 不会长时间无输出：每 ``heartbeat_seconds`` 至少产生一次心跳；
        * 中断及时：消费者检测到 stop_event 即返回，不再等待模型生成结束；
        * 不攒批：每取得一个增量立即产出，不做任何合并缓冲（合并属于前端节流的职责，
          后端攒批只会把"逐 token"变成"一批一批"）。

        产出 ``(kind, text)``，``kind`` 为 ``"delta"`` 或 ``"heartbeat"``；
        结束或异常时产出 ``("end", None)``。

        停止信号必须传入读者线程（回归测试已捕获该问题）：若读者线程持续抽干流，
        用户点击"停止"后模型仍会继续计算、内容仍会被消费，既浪费 CPU，也使"停止"
        退化为"停止显示"，并改变"停止时已发出的内容截止到哪一段"的既有语义
        （``tools/test_reasoning.py`` 的中断用例对此有断言）。因此读者线程在每次
        取块之前都检查 ``stop_event``，一旦置位立即结束，不再向底层请求新 token。
        """
        pending: "queue.Queue" = queue.Queue(maxsize=256)
        error_box: List[BaseException] = []

        def stopped() -> bool:
            return stop_event is not None and stop_event.is_set()

        #: [EN] Index of the chunk that the reader thread had already obtained from
        #: the underlying stream at the moment the stop signal was observed.
        #:
        #: The granularity matters: the reader thread runs ahead of the consumer, so
        #: when the stop signal is set, the chunk in its hand has already been
        #: "generated" by the underlying stream (for the stub engine, ``on_piece``
        #: has already fired). That chunk and all later ones do not count as content
        #: the user has seen and must be dropped; earlier chunks do count and must
        #: be delivered normally. Otherwise either reasoning the user saw is lost
        #: (covered by a regression case) or one chunk the user had no chance to see
        #: is delivered additionally, slowing the stop semantics.
        #: [ZH] 停止信号被发现时，读者线程已经从底层取得的那一块的序号。
        #:
        #: 之所以需要如此细的粒度：读者线程会领先于消费者，停止信号置位时，其手上的
        #: 那一块已被底层"生成"完成（对桩引擎而言 ``on_piece`` 已回调）。该块及其之后
        #: 的所有块都不属于"用户已经看到的内容"，必须丢弃；其之前的块才计入，必须照常
        #: 交付。否则要么丢失用户已看到的思考内容（回归用例），要么多交付一块用户根本
        #: 来不及看到的内容（使停止语义变慢）。
        drop_from: List[int] = []

        def reader() -> None:
            try:
                index = 0
                for chunk in stream:
                    if stopped():
                        drop_from.append(index)
                        break
                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {}).get("content") or choice.get("text")
                    if not delta:
                        continue
                    # [EN] Bounded queue: when the model produces faster than the UI
                    # consumes, this briefly blocks the reader thread and bounds
                    # memory usage (otherwise a fast model would grow the queue
                    # without limit).
                    # [ZH] 有界队列：模型产出快于界面消费时，此处会短暂阻塞读者线程，
                    # 从而限制内存占用（否则高速模型会使队列无限增长）。
                    pending.put(("delta", delta, index))
                    index += 1
            # [EN] Rethrown unchanged on the consumer side.
            # [ZH] 原样在消费者侧抛出。
            except BaseException as exc:  # noqa: BLE001
                error_box.append(exc)
            finally:
                pending.put((_EOF, None, -1))

        worker = threading.Thread(target=reader, name="EchoDesk-LLM-Reader", daemon=True)
        worker.start()
        # [EN] heartbeat_seconds <= 0 disables heartbeats: wait indefinitely without
        # any timeout wake-up in that case.
        # [ZH] heartbeat_seconds <= 0 表示关闭心跳：此时使用无限等待，不做任何超时唤醒。
        wait = max(0.1, float(heartbeat_seconds)) if heartbeat_seconds and heartbeat_seconds > 0 else None
        while True:
            try:
                item = pending.get(timeout=wait) if wait else pending.get()
            except queue.Empty:
                if stopped():
                    # [EN] Stop signal set and the queue is empty: end immediately
                    # without waiting for the model to finish.
                    # [ZH] 停止信号已置位且队列为空：立即结束，不再等待模型生成完毕。
                    return
                # [EN] No new token: the silence may be prefill or slow decoding. A
                # heartbeat only indicates that work is still in progress; how it is
                # displayed is decided by the frontend.
                # [ZH] 无新 token：这段静默可能来自 prefill，也可能是模型解码较慢。心跳
                # 仅表示仍在工作，具体展示方式由前端决定。
                if error_box:
                    raise error_box[0]
                yield ("heartbeat", None)
                continue
            kind, text, index = item
            # [EN] Queued items may already extend past the chunk identified when the
            # stop signal was observed: truncate at drop_from, delivering content
            # produced before the stop and discarding everything after it.
            # [ZH] 队列中的内容可能已排到"发现停止信号时的那个块"之后：按 drop_from
            # 截断，如实交付停止前产出的内容，丢弃其后的内容。
            if kind == _EOF:
                if error_box:
                    raise error_box[0]
                return
            if drop_from and index >= drop_from[0]:
                return
            yield (kind, text)

    def _chat_stream_raw(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        stats: Optional[StreamStats] = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> Iterator[Optional[str]]:
        """
        [EN]
        Low-level streaming completion. Yields raw fragments, including reasoning
        tags, and records generation throughput.

        Yielding ``None`` signals a heartbeat (metrics should be refreshed, but no
        new content exists); yielding a string yields a model delta. Throughput
        accounting deliberately includes reasoning content in ``last_tokens``:
        reasoning is model output too, so a slowdown must be reflected accurately
        rather than undercounted because of the split.

        [ZH]
        底层流式补全：原样产出片段（含思考标签），并统计生成速度。

        产出 ``None`` 表示心跳（需要刷新指标，但没有新内容）；产出字符串表示模型增量。
        速度统计刻意将思考内容计入 ``last_tokens``：思考同样属于模型生成的输出，
        "变慢"必须如实反映，不能因分流而少计一部分。
        """
        started = time.perf_counter()
        produced = 0
        if stats is not None:
            stats.prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        kwargs: Dict[str, object] = {
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if top_p is not None:
            kwargs["top_p"] = float(top_p)
        if repeat_penalty is not None:
            kwargs["repeat_penalty"] = float(repeat_penalty)
        stream = self.llm.create_chat_completion(**kwargs)
        try:
            for kind, text in self._iter_with_heartbeat(stream, stop_event, heartbeat_seconds):
                if kind == "heartbeat":
                    # [EN] Refresh metrics while a heartbeat is pending; the frontend
                    # uses them to display "N characters generated / N seconds
                    # elapsed". The empty payload only signals that metrics should
                    # be refreshed; the caller dispatches on `kind`.
                    # [ZH] 心跳期间刷新指标（前端据此显示"已生成 N 字 / 已用时 N 秒"）。
                    # 产出空串仅用于向上层发出"需要刷新指标"的信号，上层按 kind 处理。
                    if stats is not None:
                        stats.elapsed_ms = int((time.perf_counter() - started) * 1000)
                        stats.output_chars = produced
                    yield None
                    continue
                produced += len(text)
                if stats is not None:
                    now = time.perf_counter()
                    if not stats.prefill_done:
                        # [EN] Time to first token: from the start of generation
                        # (including prefill) until the first token arrives.
                        # [ZH] 首 token 延迟：从开始生成（含 prefill）到首个 token 到达的时间。
                        stats.prefill_done = True
                        stats.first_token_ms = int((now - started) * 1000)
                    stats.elapsed_ms = int((now - started) * 1000)
                    stats.output_chars = produced
                yield text
        finally:
            self.last_elapsed = time.perf_counter() - started
            self.last_tokens = produced
            self.last_tps = (produced / self.last_elapsed) if self.last_elapsed > 0 else 0.0
            if stats is not None:
                stats.elapsed_ms = int(self.last_elapsed * 1000)
                stats.output_chars = produced
                stats.stopped = bool(stop_event is not None and stop_event.is_set())
                self._finish_stats(stats)

    def _finish_stats(self, stats: StreamStats) -> None:
        """
        [EN]
        Fill in the token count after generation completes; token counts must come
        from the backend.

        This is best-effort only: the engine does not retain the generated body,
        which may span tens of thousands of characters. The exact token count is
        supplied by ``main.Backend`` through :meth:`count_output_tokens` once it
        holds the full text. If the caller does not supply it, fall back to the
        rough "characters / 2" estimate and set ``tokens_estimated`` to true, since
        an estimated token count must be marked as such and must not be reported as
        an exact value.

        [ZH]
        生成结束后补全 token 数指标（token 计数必须来自后端）。

        此处仅做尽力而为的补充：引擎不缓存生成正文（正文可能长达数万字），精确的
        token 数由 ``main.Backend`` 在取得完整文本后调用 :meth:`count_output_tokens`
        补算。若调用方未补算，则退化为"字符数 / 2"的粗估，并将 ``tokens_estimated``
        置为真：token 计数若为估算值必须标注，不能将估算值作为精确值上报。
        """
        if stats.tokens > 0:
            return
        stats.tokens = max(0, int(stats.output_chars / 2)) if stats.output_chars else 0
        stats.tokens_estimated = bool(stats.output_chars)
        if stats.elapsed_ms > 0 and stats.tokens:
            stats.tps = stats.tokens / (stats.elapsed_ms / 1000.0)

    def count_output_tokens(self, text: str) -> Tuple[int, bool]:
        """
        [EN]
        Count the tokens in a text and return ``(token_count, is_estimated)``.

        Used by ``main.Backend`` to supply the exact output token count after
        generation finishes.

        [ZH]
        统计一段文本的 token 数，返回 ``(token 数, 是否为估算值)``。

        供 ``main.Backend`` 在生成结束后补全精确的输出 token 数。
        """
        if not text:
            return 0, False
        try:
            return len(self.llm.tokenize(text.encode("utf-8"))), False
        except Exception:
            return max(1, len(text) // 2), True

    def chat_stream_split(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        splitter: Optional[ReasoningSplitter] = None,
        stats: Optional[StreamStats] = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> Iterator[Tuple[str, str]]:
        """
        [EN]
        Streaming completion that yields ``(channel, text)`` events.

        Channels are defined in :mod:`reasoning`: ``reasoning``, ``answer``, and
        ``reset`` (previously emitted answer-channel content actually belongs to
        reasoning, so the caller must invalidate the answer area), plus
        :data:`HEARTBEAT` (the backend is still working but has no new content; see
        :meth:`_iter_with_heartbeat`).

        Boundary handling — tags spanning chunks, nesting, unclosed tags, and
        orphan closing tags — is entirely the responsibility of
        :class:`reasoning.ReasoningSplitter`; this method only wires it up.

        [ZH]
        流式补全，产出 ``(通道, 文本)`` 事件。

        通道取值见 :mod:`reasoning`：``reasoning``（思考）、``answer``（回答）、
        ``reset``（此前发往回答通道的内容实际属于思考，调用方需作废回答区），以及
        :data:`HEARTBEAT`（后端仍在工作但暂无新内容，见 :meth:`_iter_with_heartbeat`）。

        边界处理（标签跨 chunk、嵌套、未闭合、孤立结束标签）全部由
        :class:`reasoning.ReasoningSplitter` 负责，本方法只做接线。
        """
        if splitter is None:
            splitter = self.make_splitter()
        if splitter is None:
            # [EN] Separation disabled: reasoning content is no longer split and is
            # emitted directly on the answer channel, preserving the previous behavior.
            # [ZH] 分离已关闭：思考内容不再分流，全部经回答通道直接产出（旧行为）。
            for piece in self._chat_stream_raw(messages, temperature, max_tokens,
                                               stop_event, top_p, repeat_penalty,
                                               stats=stats, heartbeat_seconds=heartbeat_seconds):
                if piece is None:
                    yield (HEARTBEAT, "")
                else:
                    yield (ANSWER, piece)
            return
        for piece in self._chat_stream_raw(messages, temperature, max_tokens,
                                           stop_event, top_p, repeat_penalty,
                                           stats=stats, heartbeat_seconds=heartbeat_seconds):
            if piece is None:
                # [EN] Heartbeats are not fed to the splitter: doing so would treat
                # them as untagged body text, so prefill-phase heartbeats would fill
                # the answer area with empty paragraphs.
                # [ZH] 心跳不送入分离器：送入后会被当作"无标签的正文"，导致 prefill
                # 阶段的心跳把回答区填充为空段落。
                yield (HEARTBEAT, "")
                continue
            for event in splitter.feed(piece):
                yield event
        for event in splitter.close():
            yield event

    def chat_stream(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        stats: Optional[StreamStats] = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> Iterator[str]:
        """
        [EN]
        Execute a streaming chat completion and yield increments of the final
        answer one by one.

        Reasoning content is stripped, so segmented summarization, automation, and
        similar callers receive the clean answer. Use :meth:`chat_stream_split`
        when the reasoning trace must be displayed separately. Heartbeats
        (:data:`HEARTBEAT`) are discarded here because callers of this method only
        care about text.

        [ZH]
        执行流式对话补全，逐个产出最终回答的增量片段。

        思考内容已被剥离（分段摘要、自动化等调用方获得的即为干净答案）。需要单独展示
        思考过程时使用 :meth:`chat_stream_split`。心跳（:data:`HEARTBEAT`）在此被丢弃，
        因为本方法的调用方只关心文本。
        """
        for channel, text in self.chat_stream_split(
                messages, temperature, max_tokens, stop_event, top_p, repeat_penalty,
                stats=stats, heartbeat_seconds=heartbeat_seconds):
            if channel == ANSWER:
                yield text

    def speed_hint(self, min_tps: float = 3.0, threshold_tokens: int = 64) -> str:
        """
        [EN]
        Return a hint when generation is significantly slow; otherwise return an
        empty string.

        threshold_tokens: throughput statistics are noisy for very short outputs,
        so no hint is produced below this count.

        [ZH]
        生成速度明显偏慢时返回提示文本，否则返回空串。

        threshold_tokens：文本过短时速度统计噪声较大，不给出提示。
        """
        if self.last_tokens < threshold_tokens or self.last_elapsed <= 0:
            return ""
        if self.last_tps >= min_tps:
            return ""
        return _t("sys.speed_hint", tps=f"{self.last_tps:.1f}", n_ctx=self.n_ctx,
                  kv=self.kv_cache_text())


class LazyLLMEngine(LLMEngine):
    """
    [EN]
    Inference engine that can start empty and load a model in place later.

    It is required because, when the model is missing, the backend must keep
    running and serving normally, and hot-load the model after the user finishes a
    download from the model marketplace or places a ``.gguf`` file manually. Two
    approaches exist:

    * Replace the engine object — but references in `command_handler.llm_engine`,
      `summarizer.engine`, `Backend.llm`, and elsewhere would all have to be
      rewired, and a single omission would silently keep using the old object;
    * Keep the same object and load the model into it — the approach taken here.

    It therefore subclasses `LLMEngine`: before loading, `self.llm is None`, all
    parent methods are reused, and only entry points that would actually touch the
    underlying Llama object raise :class:`NoModelError` or degrade to estimation.
    Capabilities that remain available without a model (they must keep working in
    the no-model state): rough token estimation (`len(text)//2`), cache clearing,
    window/KV description, and the speed hint.

    [ZH]
    可先保持为空、之后原地加载模型的推理引擎。

    需要它的原因：模型缺失时后端必须"不退出、照常服务"，待用户在模型市场下载完成或
    手动放入 ``.gguf`` 后再热加载。可选做法有两种：

    * 替换引擎对象 —— 但 `command_handler.llm_engine`、`summarizer.engine`、
      `Backend.llm` 等处的引用都需要重新接线，漏掉一处就会静默地继续使用旧对象；
    * 保持同一对象，仅在内部加载模型 —— 本实现采用的做法。

    因此它是 `LLMEngine` 的子类：未加载时 `self.llm is None`，复用父类的全部方法，
    仅把确实会访问底层 Llama 对象的入口改为抛出 :class:`NoModelError` 或退化为估算。
    未加载状态下仍可用的能力（无模型时必须照常工作）：token 粗估（`len(text)//2`）、
    清空缓存、窗口/KV 描述、速度提示。
    """

    def __init__(self, n_ctx: int = 8192, verbose: bool = False,
                 n_threads: Optional[int] = None, n_batch: int = 512,
                 n_gpu_layers: int = 0, reasoning=None) -> None:
        # [EN] Deliberately does not call super().__init__: that path would import
        # llama_cpp and load the model immediately.
        # [ZH] 刻意不调用 super().__init__：该路径会立即 import llama_cpp 并加载模型。
        self.model_path = ""
        self.n_ctx = int(n_ctx or 0)
        self.n_threads = n_threads
        self.n_batch = n_batch
        self.n_gpu_layers = n_gpu_layers
        # [EN] Reasoning separation: record the configuration even before the model
        # is loaded so it takes effect immediately after a hot load.
        # [ZH] 思考内容分离：未加载模型时也先记录配置，热加载后立即生效。
        self.reasoning_enabled = bool(getattr(reasoning, "enabled", True))
        self.reasoning_tags = normalize_tags(getattr(reasoning, "tags", None))
        self.n_ctx_train = 0
        self.kv_cache_bytes = 0
        self.last_tps: float = 0.0
        self.last_elapsed: float = 0.0
        self.last_tokens: int = 0
        self.llm = None
        log.info("未加载模型：进入无模型就绪状态（可推理功能暂不可用，其余功能正常）")

    # ---------- [EN] State ----------
    # ---------- [ZH] 状态 ----------

    @property
    def ready(self) -> bool:
        """
        [EN]
        Whether a model has actually been loaded.
        [ZH]
        是否已经实际加载了模型。
        """
        return self.llm is not None

    def load(self, model_path: str, n_ctx: Optional[int] = None) -> None:
        """
        [EN]
        Load the model into the current object (hot-load entry point).

        On failure the engine stays unloaded and the exception propagates to the
        caller: a failed hot load must leave the no-model state intact, report a
        clear error, and keep the retry path available. This method therefore does
        not swallow exceptions and does not partially update `self.model_path`; the
        underlying object is built first and the fields are assigned only after it
        succeeds.

        [ZH]
        将模型加载进当前对象（热加载入口）。

        失败时保持未加载状态，并将异常抛给调用方：热加载失败必须保持无模型状态、给出
        明确错误并保留重试路径。因此此处不吞掉异常，也不在中途修改 `self.model_path`；
        先构建底层对象，成功后才写入字段。
        """
        path = str(model_path or "").strip()
        if not path:
            raise NoModelError("模型路径为空")
        # [EN] Import deferred until the model is actually loaded.
        # [ZH] 延迟到真正加载模型时才 import。
        from llama_cpp import Llama

        target_ctx = int(n_ctx or self.n_ctx or 8192)
        train = resolve_context_window(path, default=0)
        llm = Llama(
            model_path=path,
            n_ctx=target_ctx,
            verbose=False,
            n_threads=self.n_threads,
            n_batch=self.n_batch,
            n_gpu_layers=self.n_gpu_layers,
        )
        self.llm = llm
        self.model_path = path
        self.n_ctx = target_ctx
        self.n_ctx_train = train
        try:
            self.n_ctx = int(llm.n_ctx())
        except Exception:
            pass
        try:
            self.n_ctx_train = int(llm.n_ctx_train()) or self.n_ctx_train
        except Exception:
            pass
        self.kv_cache_bytes = estimate_kv_cache_bytes(self.model_path, self.n_ctx)
        self.log_window()

    def reload(self, model_path: Optional[str] = None, n_ctx: Optional[int] = None) -> None:
        """
        [EN]
        Reload with the same signature as the parent method. When no model is
        loaded, this is equivalent to :meth:`load`.
        [ZH]
        重新加载（与父类同签名）。未加载模型时等价于 :meth:`load`。
        """
        target = model_path or self.model_path
        if not target:
            raise NoModelError("模型路径为空")
        self.load(target, n_ctx)

    # ---------- [EN] Entry points that touch the underlying Llama object ----------
    # ---------- [ZH] 会访问底层 Llama 对象的入口 ----------

    def chat_complete(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
    ) -> str:
        if not self.ready:
            raise NoModelError("尚未加载模型")
        return super().chat_complete(messages, temperature, max_tokens, top_p, repeat_penalty)

    def chat_complete_split(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
    ) -> Tuple[str, str]:
        if not self.ready:
            raise NoModelError("尚未加载模型")
        return super().chat_complete_split(messages, temperature, max_tokens, top_p, repeat_penalty)

    def chat_stream(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        stats: Optional[StreamStats] = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> Iterator[str]:
        if not self.ready:
            raise NoModelError("尚未加载模型")
        return super().chat_stream(messages, temperature, max_tokens, stop_event, top_p,
                                   repeat_penalty, stats=stats,
                                   heartbeat_seconds=heartbeat_seconds)

    def chat_stream_split(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        splitter: Optional[ReasoningSplitter] = None,
        stats: Optional[StreamStats] = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> Iterator[Tuple[str, str]]:
        if not self.ready:
            raise NoModelError("尚未加载模型")
        return super().chat_stream_split(messages, temperature, max_tokens, stop_event,
                                         top_p, repeat_penalty, splitter, stats=stats,
                                         heartbeat_seconds=heartbeat_seconds)

    def count_tokens(self, text: str) -> int:
        """
        [EN]
        Estimate at roughly two characters per token when no model is loaded.

        Degradation instead of an exception is required because file parsing,
        context-usage display, and the segmented-summarization trigger all rely on
        token counts, and these capabilities must remain available without a model.
        Slightly lower accuracy is acceptable because the value is used only for
        estimation and hints.

        [ZH]
        未加载模型时按"约 2 字符 1 token"粗估。

        必须退化为估算而不是抛出异常：文件解析、上下文占用显示、分段摘要的触发判断都
        需要 token 数，而这些功能在无模型时也必须可用。精度略低可以接受，因为该值仅
        用于估算与提示。
        """
        if not self.ready:
            return max(0, len(text or "") // 2)
        return super().count_tokens(text)

    def count_output_tokens(self, text: str) -> Tuple[int, bool]:
        """
        [EN]
        Estimate by character count when no model is loaded, and mark the result as
        an estimate.
        [ZH]
        未加载模型时按字符粗估，并标注该值为估算值。
        """
        if not self.ready:
            return max(0, len(text or "") // 2), bool(text)
        return super().count_output_tokens(text)

    def describe(self) -> Dict[str, object]:
        if not self.ready:
            return {"model": "", "n_ctx": self.n_ctx, "n_ctx_train": 0,
                    "kv_cache_bytes": 0, "ready": False}
        return super().describe()
