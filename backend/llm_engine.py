"""LLM 推理引擎封装（llama-cpp-python），支持流式、非流式、中断与缓存管理。

同时负责：
  * 模型加载/重载时输出当前上下文窗口（配置值 + 模型训练窗口）；
  * 按 GGUF 元数据估算 KV Cache 内存占用；
  * 记录生成速度，供"扩展上下文后变慢"的提示使用。
"""
import logging
import threading
import time
from typing import Dict, Iterator, List, Optional

from gguf_meta import describe_model, estimate_kv_cache_bytes, format_bytes, resolve_context_window

log = logging.getLogger("EchoDesk.LLM")


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
    ) -> None:
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_batch = n_batch
        self.n_gpu_layers = n_gpu_layers
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

    def chat_complete(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
    ) -> str:
        """执行一次非流式对话补全，返回助手回复全文。"""
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

    def chat_stream(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
    ) -> Iterator[str]:
        """执行流式对话补全，逐个产出增量文本片段；stop_event 置位时提前结束。

        同时统计生成长度与耗时，写入 last_tokens / last_elapsed / last_tps。
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
