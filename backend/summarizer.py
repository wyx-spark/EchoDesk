"""
[EN]
Document text preparation for the Reader profile.

The Reader build binds a single 128K model and allocates the context window from
the uploaded document (``LLMEngine.plan_context_for_tokens``), so the historical
map-reduce segmented summarization pipeline is no longer part of this branch. The
previous module split long text into overlapping chunks, summarized each chunk,
merged the summaries recursively and kept per-chunk source pointers; that logic
has been removed together with its extractive pre-filter dependency.

What remains is the minimal contract still used by the request dispatcher and the
self-test: :class:`SummaryResult`, :func:`split_chunks` and a pass-through
:class:`DocumentSummarizer` whose ``summarize`` returns the original text with
``summarized=False``. Keeping the same public surface means callers do not need a
special case while the engine consumes the full document body in one pass.

[ZH]
Reader 版本的文档文本准备层。

Reader 只绑定一个 128K 模型，并依据上传文档动态分配上下文窗口
（``LLMEngine.plan_context_for_tokens``），因此历史上基于 map-reduce 的分段摘要链路
在本分支不再存在。旧模块把长文本切成带重叠的段落、逐段摘要、递归归并并记录逐段
原文指针；该逻辑已连同其抽取式预筛依赖一并删除。

保留的是请求分发层与自检仍在使用的最小契约：``SummaryResult``、``split_chunks`` 与
直通式 ``DocumentSummarizer``（``summarize`` 原样返回文本并标记 ``summarized=False``）。
保持同一套公开接口，调用方无需为"引擎一次吞入全文"增加特例分支。
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import SummarizeConfig

log = logging.getLogger("EchoDesk.Summarizer")


@dataclass
class SummaryResult:
    """
    [EN]
    Result object retained for API compatibility with the request dispatcher.

    In the Reader profile ``summarize`` is a pass-through, so ``text`` always
    equals the input, ``summarized`` stays ``False`` and the chunk counters stay
    at zero. The fields are kept because ``main.py`` reads ``chunk_count``,
    ``origin_chars``, ``pointer``, ``status`` and ``aborted`` when it records a
    document job outcome.

    [ZH]
    为保持请求分发层 API 兼容而保留的结果对象。

    Reader 中 ``summarize`` 为直通实现，因此 ``text`` 恒等于输入、``summarized`` 恒为
    ``False``、分段计数恒为零。保留这些字段是因为 ``main.py`` 记录文档作业结果时仍会
    读取 ``chunk_count``、``origin_chars``、``pointer``、``status`` 与 ``aborted``。
    """

    text: str
    chunk_count: int = 0
    segments: List[Tuple[int, int, str]] = field(default_factory=list)
    origin_chars: int = 0
    summarized: bool = False
    status: str = "ok"
    ok_chunks: int = 0
    failed_chunks: int = 0
    partial_chunks: int = 0
    timeout_chunks: int = 0
    skipped_chunks: int = 0
    slow_chunks: int = 0
    aborted: bool = False
    elapsed: float = 0.0
    error: str = ""

    @property
    def pointer(self) -> str:
        """
        [EN]
        Source pointer (empty in the Reader profile; no chunk ranges exist).
        [ZH]
        原文指针（Reader 中为空：不存在分段区间）。
        """
        ranges = ",".join(f"{a}-{b}" for a, b, _ in self.segments)
        return f"chars={ranges}" if ranges else ""

    @property
    def degraded(self) -> bool:
        """
        [EN]
        Whether any fallback path was used (always ``False`` for pass-through).
        [ZH]
        是否使用过兜底路径（直通实现恒为 ``False``）。
        """
        return bool(self.failed_chunks or self.timeout_chunks or self.skipped_chunks
                    or self.partial_chunks or self.error)


def split_chunks(text: str, chunk_chars: int, overlap_chars: int) -> List[Tuple[int, int, str]]:
    """
    [EN]
    Split ``text`` into overlapping ``(start, end, piece)`` ranges.

    Kept as a utility for callers that still need deterministic windowing (for
    example diagnostics or future page-level reading). The Reader engine never
    calls it on the document path: the full body is injected at once.

    The split prefers a line break or a sentence end near the nominal boundary and
    guarantees strictly increasing starts, no empty chunk and full coverage of the
    text end.

    [ZH]
    将 ``text`` 切分为带重叠的 ``(start, end, piece)`` 区间。

    保留该工具函数，供仍需要确定性切窗的调用方使用（例如诊断或未来的分页阅读）。
    Reader 引擎在文档链路上不再调用它：正文一次性注入。

    切点优先落在标称边界附近的行尾或句末，并保证起点严格递增、无空段、末段覆盖全文末尾。
    """
    text = text or ""
    total = len(text)
    if total <= 0:
        return []
    size = max(1, int(chunk_chars or 1))
    overlap = max(0, min(int(overlap_chars or 0), size - 1))
    if total <= size:
        return [(0, total, text)]

    breaks = "\n。！？；.!?;"
    ranges: List[Tuple[int, int, str]] = []
    start = 0
    while start < total:
        end = min(total, start + size)
        if end < total:
            # [EN] Prefer a natural boundary in the trailing 20% of the window.
            # [ZH] 在窗口末尾 20% 内优先选择自然边界。
            floor = start + max(1, int(size * 0.8))
            for index in range(end, floor, -1):
                if text[index - 1] in breaks:
                    end = index
                    break
        ranges.append((start, end, text[start:end]))
        if end >= total:
            break
        start = max(start + 1, end - overlap)
    return ranges


class DocumentSummarizer:
    """
    [EN]
    Pass-through document text owner.

    The signature of the constructor and of ``summarize`` is unchanged so that
    ``main.py``, ``command_handler.py`` and the self-test keep working. ``engine``
    and ``cfg`` are recorded for diagnostics only; no model call is performed here
    because the Reader context window is sized to the document.

    [ZH]
    直通式文档文本持有者。

    构造函数与 ``summarize`` 的签名保持不变，使 ``main.py``、``command_handler.py``
    与自检继续可用。``engine`` 与 ``cfg`` 仅用于诊断记录；由于 Reader 的上下文窗口按
    文档大小分配，此处不再发起任何模型调用。
    """

    def __init__(self, engine: Any, cfg: Optional[SummarizeConfig] = None,
                 engine_factory: Optional[Callable[[str], Any]] = None) -> None:
        self.engine = engine
        self.cfg = cfg or SummarizeConfig()
        # [EN] Retained for signature compatibility; unused in the Reader profile.
        # [ZH] 为签名兼容而保留；Reader 中不再使用。
        self.engine_factory = engine_factory

    def needs_summary(self, text: str, n_ctx: int) -> bool:
        """
        [EN]
        Always ``False``: the Reader allocates ``n_ctx`` from the document size
        instead of compressing long text.
        [ZH]
        恒为 ``False``：Reader 依据文档大小分配 ``n_ctx``，不再压缩长文本。
        """
        return False

    def should_use_summary(self, text: str, n_ctx: int, force: bool = False) -> bool:
        """
        [EN]
        Always ``False``; kept for callers that still ask the question.
        [ZH]
        恒为 ``False``；保留给仍会询问该问题的调用方。
        """
        return False

    def summarize(
        self,
        text: str,
        source_name: str = "",
        force: bool = True,
        on_progress: Optional[Callable[[Dict], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        conversation: Any = None,
    ) -> SummaryResult:
        """
        [EN]
        Return the input text unchanged, marked as not summarized.

        ``on_progress`` receives a single ``done`` event so a caller that renders
        progress still receives a terminal update; the remaining arguments are
        ignored.

        [ZH]
        原样返回输入文本，并标记为未摘要。

        ``on_progress`` 会收到一个 ``done`` 事件，使渲染进度的调用方仍能收到终态更新；
        其余参数一律忽略。
        """
        body = text or ""
        if on_progress is not None:
            try:
                on_progress({"phase": "done", "status": "ok", "chars": len(body),
                             "elapsed": 0.0, "stream": False})
            except Exception:  # noqa: BLE001 - progress reporting must never fail a job
                log.debug("摘要进度回调失败（忽略）", exc_info=True)
        return SummaryResult(text=body, origin_chars=len(body), status="ok",
                             summarized=False)
