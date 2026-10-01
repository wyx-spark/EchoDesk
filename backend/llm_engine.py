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
import threading
import time
from typing import Dict, Iterator, List, Optional, Tuple

from gguf_meta import describe_model, estimate_kv_cache_bytes, format_bytes, resolve_context_window
from reasoning import ANSWER, ReasoningSplitter, normalize_tags, split_text

log = logging.getLogger("EchoDesk.LLM")


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

    def _chat_stream_raw(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
    ) -> Iterator[str]:
        """底层流式补全：原样产出片段（含思考标签），并统计生成速度。

        速度统计刻意把思考内容也算进 ``last_tokens``：思考同样是模型生成的
        输出，"变慢了"要如实反映，不能因为分流就少算一截。
        """
        started = time.perf_counter()
        produced = 0
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
            for chunk in stream:
                if stop_event is not None and stop_event.is_set():
                    break
                choice = chunk.get("choices", [{}])[0]
                delta = choice.get("delta", {}).get("content") or choice.get("text")
                if delta:
                    produced += len(delta)
                    yield delta
        finally:
            self.last_elapsed = time.perf_counter() - started
            self.last_tokens = produced
            self.last_tps = (produced / self.last_elapsed) if self.last_elapsed > 0 else 0.0

    def chat_stream_split(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        splitter: Optional[ReasoningSplitter] = None,
    ) -> Iterator[Tuple[str, str]]:
        """流式补全，产出 ``(通道, 文本)`` 事件。

        通道取值见 :mod:`reasoning`：``reasoning``（思考）/ ``answer``（回答）/
        ``reset``（此前发到回答通道的内容其实属于思考，调用方需把回答区作废）。

        边界处理（标签跨 chunk、嵌套、未闭合、孤立结束标签）全部由
        :class:`reasoning.ReasoningSplitter` 负责，本方法只做接线。
        """
        if splitter is None:
            splitter = self.make_splitter()
        if splitter is None:
            # 分离关闭：思考内容不再分流，全部按回答通道直出（旧行为）
            for piece in self._chat_stream_raw(messages, temperature, max_tokens,
                                               stop_event, top_p, repeat_penalty):
                yield (ANSWER, piece)
            return
        for piece in self._chat_stream_raw(messages, temperature, max_tokens,
                                           stop_event, top_p, repeat_penalty):
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
    ) -> Iterator[str]:
        """执行流式对话补全，逐个产出**最终回答**的增量片段。

        思考内容已被剥掉（分段摘要、自动化等调用方拿到的就是干净答案）。
        需要把思考过程单独展示时用 :meth:`chat_stream_split`。
        """
        for channel, text in self.chat_stream_split(
                messages, temperature, max_tokens, stop_event, top_p, repeat_penalty):
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
    ) -> Iterator[str]:
        if not self.ready:
            raise NoModelError("尚未加载模型")
        return super().chat_stream(messages, temperature, max_tokens, stop_event, top_p, repeat_penalty)

    def chat_stream_split(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        splitter: Optional[ReasoningSplitter] = None,
    ) -> Iterator[Tuple[str, str]]:
        if not self.ready:
            raise NoModelError("尚未加载模型")
        return super().chat_stream_split(messages, temperature, max_tokens, stop_event,
                                         top_p, repeat_penalty, splitter)

    def count_tokens(self, text: str) -> int:
        """未加载模型时按"约 2 字符 1 token"粗估。

        为什么必须退化而不是抛异常：文件解析、上下文占用显示、分段摘要的触发判断
        都要用 token 数，而这些都是**无模型时也要能用**的功能（任务一的功能边界表）。
        精度差一点没关系，它只用于估算与提示。
        """
        if not self.ready:
            return max(0, len(text or "") // 2)
        return super().count_tokens(text)

    def describe(self) -> Dict[str, object]:
        if not self.ready:
            return {"model": "", "n_ctx": self.n_ctx, "n_ctx_train": 0,
                    "kv_cache_bytes": 0, "ready": False}
        return super().describe()
