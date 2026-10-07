"""LLM 推理引擎封装（llama-cpp-python），支持流式、非流式、中断与缓存管理。

同时负责：
  * 模型加载/重载时输出当前上下文窗口（配置值 + 模型训练窗口）；
  * 按 GGUF 元数据估算 KV Cache 内存占用；
  * 记录生成速度，供"扩展上下文后变慢"的提示使用；
  * **思考内容与最终回答的分流**（v0.2.1 模型思考内容分离 任务二/三）：
    推理模型（Qwen3 thinking 模式、DeepSeek-R1 等）会把思考过程写在
    ``content`` 里（标签包裹），而 llama-cpp-python 0.3.x 既没有
    ``reasoning_format`` 也没有 ``reasoning_content`` 字段（实测见
    ``records/v0.2.1-模型思考内容分离记录.md``），所以分流在本层用
    :mod:`reasoning` 的标签分离器完成：``chat_stream_split`` /
    ``chat_complete_split`` 给出双通道，``chat_stream`` / ``chat_complete``
    保持"只给最终回答"的兼容语义（摘要、自动化等调用方天然拿不到思考内容）。
"""
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from gguf_meta import describe_model, estimate_kv_cache_bytes, format_bytes, resolve_context_window
from i18n import log_user
from reasoning import ANSWER, ReasoningSplitter, normalize_tags, split_text

log = logging.getLogger("EchoDesk.LLM")

#: 流式读取的"无新 token 就发心跳"的默认间隔（秒）。
#: 任务书任务五：prefill 阶段慢、decode 阶段快，纯 CPU 上首 token 可能要等十几秒，
#: 界面上"什么都没有"与"卡死了"无法区分。每 1.5 秒发一次心跳即可消除这种歧义，
#: 又不会把协议刷爆（一次长回复约几百条心跳上限）。
DEFAULT_HEARTBEAT_SECONDS = 1.5

#: 心跳事件在通道流里的专用标记：``(HEARTBEAT, "")``。
#: 它**不是**模型输出，:meth:`LLMEngine.chat_stream_split` 只把它当信号转发，
#: 分离器永远看不到它（否则心跳会被当成"没有标签的正文"喂进回答通道）。
HEARTBEAT = "heartbeat"

#: 生成器内部用的哨兵：读者线程结束（正常或异常）。
_EOF = object()


@dataclass
class StreamStats:
    """一次流式生成的**运行指标**（任务七：状态区实时指标）。

    由引擎在生成过程中维护，``main.Backend`` 在 ``done`` 里原样上报。
    全部字段都来自后端实测，**不是前端估算**：

    ``first_token_ms``   请求发出到第一个 token 到达的毫秒数（首 token 延迟）；
    ``elapsed_ms``       从开始生成到结束的总毫秒数（生成中实时刷新）；
    ``prompt_chars``     送进模型的提示词字符数（输入规模）；
    ``output_chars``     已产出的字符数（含思考）；
    ``tokens``           输出 token 数（用模型分词器精确计数）；
    ``tokens_estimated`` tokens 是否为估算（分词器不可用时为 True）；
    ``tps``              token/s（tokens / 生成耗时）；
    ``prefill_done``     是否已经吐出过第一个 token（前端据此显示"等待首 token"）；
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
    """当前没有可用模型时，任何真正需要推理的调用都抛它。

    为什么要有一个专门的异常类型（v0.2.1 综合优化 任务一）：
    无模型就绪状态下，`chat`/`chat_stream`/分段摘要都要给用户**引导**而不是崩溃。
    有了它，引导逻辑就只需要在少数几个入口 `except NoModelError` 一次，
    不用在几十处调用点逐个判断"引擎是不是空的"（漏一处就是一个崩溃）。
    它的 `message` 已经是可直接展示给用户的文案（由调用方传入语言包文本）。
    """


class LLMEngine:
    """基于 llama.cpp 的本地大模型推理引擎。"""

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
        # 思考内容分离（config.reasoning）：enabled 控制是否分流，tags 是追加标签表。
        # 传 None（如第三方/测试构造）时按默认开启处理，与旧行为向后兼容。
        self.reasoning_enabled = bool(getattr(reasoning, "enabled", True))
        self.reasoning_tags = normalize_tags(getattr(reasoning, "tags", None))
        # 模型训练窗口（GGUF 元数据，不依赖加载）
        self.n_ctx_train = resolve_context_window(model_path, default=0)
        self.kv_cache_bytes = estimate_kv_cache_bytes(model_path, n_ctx)
        # 生成速度统计（tokens/秒）
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
        # llama.cpp 能报出实际生效值与训练值，用它校准元数据解析结果
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
        """模型加载/重载时输出当前上下文窗口大小。"""
        train = f"{self.n_ctx_train} tokens" if self.n_ctx_train else "未知"
        log.info(
            "上下文窗口: %d tokens（模型训练窗口 %s，KV Cache 约 %s，线程 %s，GPU 层 %d）",
            self.n_ctx, train, format_bytes(self.kv_cache_bytes),
            self.n_threads, self.n_gpu_layers,
        )

    def reload(self, model_path: Optional[str] = None, n_ctx: Optional[int] = None) -> None:
        """重新加载模型（更换模型路径或上下文长度）。"""
        if model_path:
            self.model_path = model_path
            self.n_ctx_train = resolve_context_window(model_path, default=0)
        if n_ctx:
            self.n_ctx = n_ctx
        self.llm = self._create(verbose=False)

    def load(self, model_path: str, n_ctx: Optional[int] = None) -> None:
        """加载/换到指定模型 —— 与 :meth:`LazyLLMEngine.load` **同名同语义**。

        为什么父类也要有这个方法（本轮实测踩到）：
        ``Backend.load_model()``（设置页「加载」按钮、模型档位「切换」、下载完成后的自动加载
        都走它）调用的是 ``self.llm.load(path, n_ctx)``，而 ``load`` 原先**只存在于**
        ``LazyLLMEngine``（无模型就绪那个子类）上。于是：
          * 启动时没有模型（发布版默认）→ 引擎是 LazyLLMEngine → 热加载正常；
          * 一旦**已经加载了模型**再换一个（用户日常最常做的操作：从 0.6B 档切到 1.7B 档）
            → 引擎是普通 ``LLMEngine`` → ``AttributeError: 'LLMEngine' object has no
            attribute 'load'``，用户看到的是"加载失败：'LLMEngine' object has no
            attribute 'load'"这种与真实原因无关的报错。
        这里给父类补一个同名方法（转发到 :meth:`reload`），让"是否已加载模型"不再决定
        API 是否存在；``LazyLLMEngine`` 的 ``load`` 有自己的实现（原地建底层对象），
        语义一致，不需要动。
        """
        self.reload(model_path, n_ctx)

    # ---------- 模型信息与缓存管理 ----------

    def model_info(self) -> Dict[str, str]:
        """返回模型元数据（架构、量化类型等），失败时返回空字典。"""
        try:
            meta = getattr(self.llm, "metadata", None) or {}
            return {str(k): str(v) for k, v in meta.items() if isinstance(v, (str, int, float))}
        except Exception:
            return {}

    def describe(self) -> Dict[str, object]:
        """汇总模型与窗口参数（供 /model、/status 与加载日志展示）。"""
        return describe_model(self.model_path, self.n_ctx)

    def kv_cache_text(self) -> str:
        """KV Cache 占用的人类可读文本。"""
        return format_bytes(estimate_kv_cache_bytes(self.model_path, self.n_ctx) or self.kv_cache_bytes)

    def count_tokens(self, text: str) -> int:
        """用模型分词器估算文本 token 数；失败时按字符粗估。"""
        try:
            return len(self.llm.tokenize(text.encode("utf-8")))
        except Exception:
            return len(text) // 2

    def clear_cache(self) -> bool:
        """清空 KV 缓存与输入历史（在模式/提示词/历史结构变化后调用）。"""
        try:
            reset = getattr(self.llm, "reset", None)
            if callable(reset):
                reset()
                return True
        except Exception:
            pass
        return False

    # ---------- 对话 ----------

    def make_splitter(self) -> Optional[ReasoningSplitter]:
        """造一个思考标签分离器（分离关闭时返回 ``None``）。

        调用方（``main.Backend.stream``）把它传给 :meth:`chat_stream_split`，
        生成结束后就能从同一个对象上取到 ``reasoning_text`` / ``unclosed``，
        不必让生成器额外回传状态。
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
        """底层非流式补全：返回模型**原样**输出（可能带思考标签）。"""
        kwargs: Dict[str, object] = {
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 修复四（方向六）：预设档位需要 top_p / 重复惩罚，缺省时不传，
        # 让 llama.cpp 用它自己的默认值（与旧行为保持一致）。
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
        """非流式补全，返回 ``(思考内容, 最终回答)``。"""
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
        """执行一次非流式对话补全，返回**最终回答**全文。

        思考内容被剥掉（v0.2.1 模型思考内容分离）：摘要、自动化、上下文压缩
        这些调用方要的是结论，把思考过程喂进去只会白占 token。
        需要思考内容请用 :meth:`chat_complete_split`。
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
        """把阻塞的 llama 流式迭代器变成"心跳 + 增量"事件流。

        ## 为什么必须另起线程（任务书任务五的根因之一）

        ``llama_cpp`` 的流式生成器在 **prefill 阶段会阻塞几十秒**（纯 CPU 上
        3000 字符的提示词实测十几秒才吐第一个 token）。如果消费者直接
        ``for chunk in stream``，这段时间里**什么都发不出去** —— 无论怎么
        flush 都没用，因为根本没有数据可 flush。任务书说的"生成一半卡住，
        等会儿又冒出来好多"，前半段就是这件事。

        做法：读者线程负责产出增量并放入有界队列，消费者用 ``get(timeout=...)``
        取；超时就产出一次心跳。于是：

        * **永远不会长时间无输出** —— 每 ``heartbeat_seconds`` 至少一次心跳；
        * **中断及时** —— 消费者发现 stop_event 就返回，不再等模型跑完；
        * **不攒批** —— 每拿到一个增量立刻产出，不做任何合并缓冲
          （合并是前端节流的事，后端攒批只会让"逐 token"变成"一批一批"）。

        产出 ``(kind, text)``，``kind`` ∈ ``"delta"`` / ``"heartbeat"``；
        结束或异常时产出 ``("end", None)``。

        **停止信号必须传进读者线程**（踩过的坑，回归测试抓出来的）：读者线程
        若一路把流抽干，用户点"停止"之后模型还在继续算、内容还会被消费 ——
        既浪费 CPU，也让"停止"退化成"停止显示"，还改变了"停止时已发出的内容
        到哪一段为止"的既有语义（`tools/test_reasoning.py` 的中断用例断言了它）。
        所以读者线程每取一块**之前**都检查 ``stop_event``，置位就立刻结束，
        不再向底层要新 token。
        """
        pending: "queue.Queue" = queue.Queue(maxsize=256)
        error_box: List[BaseException] = []

        def stopped() -> bool:
            return stop_event is not None and stop_event.is_set()

        #: 停止信号**被发现时**，底层已经取到手的那一块的序号。
        #:
        #: 为什么要这么细：读者线程比消费者快，停止信号置位时它手上那块已经被
        #: 底层"生成"出来了（对桩引擎而言就是 ``on_piece`` 已经回调过）。这一块
        #: 与它之后的都不算"用户已经看到的内容"，必须丢弃；它**之前**的才算，
        #: 必须照常交付。否则要么丢掉用户看过的思考（回归用例），要么多交付一块
        #: 用户根本没来得及看到的内容（停止语义变慢）。
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
                    # 有界队列：模型产得比界面消费得快时，这里会短暂阻塞读者线程，
                    # 从而把内存压住（不然高速模型会把队列撑成内存黑洞）。
                    pending.put(("delta", delta, index))
                    index += 1
            except BaseException as exc:  # noqa: BLE001 —— 原样带回消费者抛
                error_box.append(exc)
            finally:
                pending.put((_EOF, None, -1))

        worker = threading.Thread(target=reader, name="EchoDesk-LLM-Reader", daemon=True)
        worker.start()
        # heartbeat_seconds <= 0 表示关闭心跳：此时用无限等待，不做任何超时唤醒
        wait = max(0.1, float(heartbeat_seconds)) if heartbeat_seconds and heartbeat_seconds > 0 else None
        while True:
            try:
                item = pending.get(timeout=wait) if wait else pending.get()
            except queue.Empty:
                if stopped():
                    # 停止信号置位且队列已空：立刻结束（不再等模型跑完）
                    return
                # 没有新 token：这次"沉默"可能是 prefill，也可能是模型在慢慢算。
                # 心跳只说明"还在工作"，内容由前端决定怎么显示。
                if error_box:
                    raise error_box[0]
                yield ("heartbeat", None)
                continue
            kind, text, index = item
            # 队列里的内容可能已经排到了"停止之后才发现的那一块"：按 drop_from
            # 截断，把"停止之前就产出的内容"如实交付、把之后的丢掉。
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
        """底层流式补全：原样产出片段（含思考标签），并统计生成速度。

        产出 ``None`` 表示**心跳**（该刷指标了，但没有新内容）；产出字符串表示
        模型增量。速度统计刻意把思考内容也算进 ``last_tokens``：思考同样是模型
        生成的输出，"变慢了"要如实反映，不能因为分流就少算一截。
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
                    # 心跳期间刷新指标（前端靠它显示"已生成 N 字 / 已用 N 秒"）。
                    # 产出空串只是给上层一个"该刷指标了"的信号，上层按 kind 处理。
                    if stats is not None:
                        stats.elapsed_ms = int((time.perf_counter() - started) * 1000)
                        stats.output_chars = produced
                    yield None
                    continue
                produced += len(text)
                if stats is not None:
                    now = time.perf_counter()
                    if not stats.prefill_done:
                        # 首 token 延迟：从开始生成（含 prefill）到第一个 token 到达
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
        """生成结束后把 token 数补上进指标（任务七：token 计数要来自后端）。

        这里只做"能给就给"的尽力而为：引擎不缓存生成正文（可能要几万字），
        精确的 token 数由 ``main.Backend`` 拿到完整文本后调用
        :meth:`count_output_tokens` 补算。若调用方没补，就退回
        "字符 / 2"的粗估，并把 ``tokens_estimated`` 置真 —— 任务书要求
        "token 计数若为估算需标注"，不能把估算值当精确值报上去。
        """
        if stats.tokens > 0:
            return
        stats.tokens = max(0, int(stats.output_chars / 2)) if stats.output_chars else 0
        stats.tokens_estimated = bool(stats.output_chars)
        if stats.elapsed_ms > 0 and stats.tokens:
            stats.tps = stats.tokens / (stats.elapsed_ms / 1000.0)

    def count_output_tokens(self, text: str) -> Tuple[int, bool]:
        """数一段文本的 token 数，返回 ``(token 数, 是否为估算)``。

        供 ``main.Backend`` 在生成结束后补齐精确的输出 token 数。
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
        """流式补全，产出 ``(通道, 文本)`` 事件。

        通道取值见 :mod:`reasoning`：``reasoning``（思考）/ ``answer``（回答）/
        ``reset``（此前发到回答通道的内容其实属于思考，调用方需把回答区作废），
        外加 :data:`HEARTBEAT`（后端仍在工作但还没有新内容，见 :meth:`_iter_with_heartbeat`）。

        边界处理（标签跨 chunk、嵌套、未闭合、孤立结束标签）全部由
        :class:`reasoning.ReasoningSplitter` 负责，本方法只做接线。
        """
        if splitter is None:
            splitter = self.make_splitter()
        if splitter is None:
            # 分离关闭：思考内容不再分流，全部按回答通道直出（旧行为）
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
                # 心跳**不喂给分离器**：喂进去会被当成"没有标签的正文"，
                # 于是 prefill 阶段的心跳会把回答区刷成空段落。
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
        """执行流式对话补全，逐个产出**最终回答**的增量片段。

        思考内容已被剥掉（分段摘要、自动化等调用方拿到的就是干净答案）。
        需要把思考过程单独展示时用 :meth:`chat_stream_split`。
        心跳（:data:`HEARTBEAT`）在这里被丢掉 —— 本方法的调用方只关心文本。
        """
        for channel, text in self.chat_stream_split(
                messages, temperature, max_tokens, stop_event, top_p, repeat_penalty,
                stats=stats, heartbeat_seconds=heartbeat_seconds):
            if channel == ANSWER:
                yield text

    def speed_hint(self, min_tps: float = 3.0, threshold_tokens: int = 64) -> str:
        """生成速度明显偏慢时返回提示文本（否则返回空串）。

        threshold_tokens：文本太短时速度统计噪声大，不提示。
        """
        if self.last_tokens < threshold_tokens or self.last_elapsed <= 0:
            return ""
        if self.last_tps >= min_tps:
            return ""
        return (
            f"⏱️ 提示：当前生成速度约 {self.last_tps:.1f} 字/秒，偏慢。"
            f"上下文窗口为 {self.n_ctx} tokens（KV Cache 约 {self.kv_cache_text()}），"
            "窗口越大内存占用越高、速度越慢；可在 config.json 调小 context_length，"
            "或增大 n_threads / 使用更小的量化模型。"
        )


class LazyLLMEngine(LLMEngine):
    """**可先空着、之后原地加载**的推理引擎（v0.2.1 综合优化 任务一/任务五）。

    为什么需要它：模型缺失时后端要"不退出、照常服务"，等用户在模型市场下载完成
    或手动放入 `.gguf` 之后再**热加载**。做法有两种：

    * 换一个引擎对象 —— 但 `command_handler.llm_engine`、`summarizer.engine`、
      `Backend.llm` 等处的引用都要跟着重新接线，漏一个就会"看起来加载成功了，
      实际还在用旧对象"（而且是静默的）；
    * 保持**同一个对象**，只在内部把模型加载进来 —— 就是这里选的做法。

    所以它是 `LLMEngine` 的子类：未加载时 `self.llm is None`，复用父类全部方法，
    只把"会真的碰到底层 Llama 对象"的入口改成抛 :class:`NoModelError` 或退化为估算。
    未加载状态下仍然可用的能力（无模型时必须照常工作，任务书任务一的边界表）：
    token 粗估（`len(text)//2`）、清缓存、窗口/KV 描述、速度提示。
    """

    def __init__(self, n_ctx: int = 8192, verbose: bool = False,
                 n_threads: Optional[int] = None, n_batch: int = 512,
                 n_gpu_layers: int = 0, reasoning=None) -> None:
        # 刻意**不**调用 super().__init__：那条路会立刻 import llama_cpp 并加载模型。
        self.model_path = ""
        self.n_ctx = int(n_ctx or 0)
        self.n_threads = n_threads
        self.n_batch = n_batch
        self.n_gpu_layers = n_gpu_layers
        # 思考内容分离：未加载模型时也要先记住配置，热加载后立刻生效
        self.reasoning_enabled = bool(getattr(reasoning, "enabled", True))
        self.reasoning_tags = normalize_tags(getattr(reasoning, "tags", None))
        self.n_ctx_train = 0
        self.kv_cache_bytes = 0
        self.last_tps: float = 0.0
        self.last_elapsed: float = 0.0
        self.last_tokens: int = 0
        self.llm = None
        log.info("未加载模型：进入无模型就绪状态（可推理功能暂不可用，其余功能正常）")

    # ---------- 状态 ----------

    @property
    def ready(self) -> bool:
        """是否已经真的加载了模型。"""
        return self.llm is not None

    def load(self, model_path: str, n_ctx: Optional[int] = None) -> None:
        """把模型加载进**当前对象**（热加载入口）。

        失败时保持未加载状态并把异常抛给调用方（任务书任务五：热加载失败要
        保持无模型状态、给出明确错误、保留重试路径）——因此这里不吞异常，
        也不半途改动 `self.model_path`：先把底层对象建好，成功后才落字段。
        """
        path = str(model_path or "").strip()
        if not path:
            raise NoModelError("模型路径为空")
        from llama_cpp import Llama  # 延迟到真正加载时才 import

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
        """重新加载（父类同签名）。未加载时等价于 :meth:`load`。"""
        target = model_path or self.model_path
        if not target:
            raise NoModelError("模型路径为空")
        self.load(target, n_ctx)

    # ---------- 会碰到底层 Llama 的入口 ----------

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
        """未加载模型时按"约 2 字符 1 token"粗估。

        为什么必须退化而不是抛异常：文件解析、上下文占用显示、分段摘要的触发判断
        都要用 token 数，而这些都是**无模型时也要能用**的功能（任务一的功能边界表）。
        精度差一点没关系，它只用于估算与提示。
        """
        if not self.ready:
            return max(0, len(text or "") // 2)
        return super().count_tokens(text)

    def count_output_tokens(self, text: str) -> Tuple[int, bool]:
        """未加载模型时按字符粗估，并标注"这是估算"（任务七）。"""
        if not self.ready:
            return max(0, len(text or "") // 2), bool(text)
        return super().count_output_tokens(text)

    def describe(self) -> Dict[str, object]:
        if not self.ready:
            return {"model": "", "n_ctx": self.n_ctx, "n_ctx_train": 0,
                    "kv_cache_bytes": 0, "ready": False}
        return super().describe()
