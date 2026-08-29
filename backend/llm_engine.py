"""LLM 推理引擎封装（llama-cpp-python），支持流式、非流式、中断与缓存管理。"""
import threading
from typing import Dict, Iterator, List, Optional


class LLMEngine:
    """基于 llama.cpp 的本地大模型推理引擎。"""

    def __init__(
        self,
        model_path: str,
        n_ctx: int = 4096,
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
        self.llm = self._create(verbose=verbose)

    def _create(self, verbose: bool = False):
        from llama_cpp import Llama

        return Llama(
            model_path=self.model_path,
            n_ctx=self.n_ctx,
            verbose=verbose,
            n_threads=self.n_threads,
            n_batch=self.n_batch,
            n_gpu_layers=self.n_gpu_layers,
        )

    def reload(self, model_path: Optional[str] = None, n_ctx: Optional[int] = None) -> None:
        """重新加载模型（更换模型路径或上下文长度）。"""
        if model_path:
            self.model_path = model_path
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
    ) -> str:
        """执行一次非流式对话补全，返回助手回复全文。"""
        response = self.llm.create_chat_completion(
            messages=messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response["choices"][0]["message"]["content"]

    def chat_stream(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_event: Optional[threading.Event] = None,
    ) -> Iterator[str]:
        """执行流式对话补全，逐个产出增量文本片段；stop_event 置位时提前结束。"""
        stream = self.llm.create_chat_completion(
            messages=messages,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        for chunk in stream:
            if stop_event is not None and stop_event.is_set():
                break
            choice = chunk.get("choices", [{}])[0]
            delta = choice.get("delta", {}).get("content") or choice.get("text")
            if delta:
                yield delta
