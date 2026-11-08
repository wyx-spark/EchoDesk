"""
[EN]
Long-document segmented summarization (map-reduce) with progress feedback,
heartbeats, timeouts, and interruption.

When the full text to be injected exceeds a configured fraction of the context
window, it is no longer truncated directly. Instead:
1. Chunking (map): split by ``chunk_chars`` with ``overlap_chars`` overlap between
   chunks, preferring paragraph or sentence boundaries so that sentences are not
   cut in half;
2. Per-chunk summarization: each chunk is summarized by the LLM into bullet points
   of at most ``max_summary_tokens``;
3. Merge (reduce): when the combined chunk summaries exceed ``merge_max_tokens``,
   they are grouped and summarized again, recursively, until the length fits, with
   a depth cap and truncation fallback;
4. Source pointers: the output records the source file name and the original
   character range of each chunk for later lookup.

The summarization model defaults to the active conversation model; when
``summarize.model`` names another local model, it is loaded on demand.

--------------------------------------------------------------------------
Progress feedback and failure diagnosis
--------------------------------------------------------------------------
Previously, only "summarizing chunk i/N" was written to stderr and never appeared
in the UI. On CPU-only hardware a single chunk takes tens of seconds, and a long
document with a dozen chunks takes tens of minutes, so the user could not
distinguish "still running" from "stalled".

The behavior is now:
* Process visibility: every step emits a structured event through the
  ``on_progress(event)`` callback (phase / status / index / total / elapsed /
  chars / tokens / slow / counters). The caller (``main.py``) attaches localized
  text and forwards it to the frontend. This module deliberately contains no UI
  copy and does not import i18n: the summarization algorithm should not depend on
  the active UI language.
* No long silent waits: single-chunk generation uses ``engine.chat_stream`` and
  iterates token by token, emitting a heartbeat at least every
  ``heartbeat_seconds``, including the number of characters generated so far. A
  long period with no output therefore cannot occur by construction.
* Interruptibility: the same token-by-token iteration stops the current generation
  as soon as ``should_stop()`` returns true, so pressing stop does not wait for the
  current chunk to finish. With a non-streaming engine that lacks ``chat_stream``,
  the check can only happen after the call returns; such events carry
  ``stream=False``.
* Timeout: ``chunk_timeout_seconds`` (0 means unlimited) is measured as wall-clock
  time since the chunk started. The behavior is controlled by
  ``chunk_timeout_action``: ``skip`` (skip and continue), ``retry`` (retry once,
  then skip if it times out again), or ``abort`` (abort the whole summarization).
* Slow versus failed: slowness only produces a hint (``slow=True`` and a
  ``slow_chunks`` count) and never interrupts. Only actual failures (model call
  exceptions, timeouts, or all chunks failing) produce an error and recovery
  guidance.
* Log sanitization: only chunk numbers, character counts, durations, status, and
  control characters are logged. Document bodies are never logged, because they may
  contain contract amounts, party names, and other sensitive information.

Callback event fields (structured data only, no copy):

``phase``   ``prepare`` / ``map`` / ``reduce`` / ``done`` / ``aborted`` / ``failed``
``status``  ``start`` / ``running`` / ``ok`` / ``failed`` / ``timeout`` / ``skipped`` / ``retry``
``index`` / ``total``      Current chunk index and total chunk count (during reduce,
                           the index of the current group)
``chars``                  Original character count of the chunk
``tokens``                 Characters generated so far for the chunk, used by
                           heartbeats to show that progress continues
``elapsed``                Elapsed time of the whole flow, in seconds
``chunk_elapsed``          Elapsed time of the current chunk, in seconds
``slow``                   Whether the current chunk exceeded
                           ``slow_chunk_seconds`` (slow is not failed)
``stream``                 Whether the streaming path is active; only streaming
                           supports intra-chunk interruption and heartbeats
``failed_chunks`` / ``timeout_chunks`` / ``skipped_chunks`` / ``slow_chunks``
                           Cumulative counters
``error``                  Short failure reason on failure (no document body)

[ZH]
长文档分段摘要（map-reduce），带过程反馈、心跳、超时与中断。

当待注入的文件全文超出上下文窗口一定比例时，不再直接截断，而是：

1. 分段（map）：按 ``chunk_chars`` 切分，段间保留 ``overlap_chars`` 重叠，切点优先
   落在段落或句号边界，避免把句子劈开；
2. 逐段摘要：每段交给 LLM 生成不超过 ``max_summary_tokens`` 的要点；
3. 合并（reduce）：段摘要合计超过 ``merge_max_tokens`` 时分组再摘要，递归到长度
   达标为止（带深度上限与截断兜底）；
4. 保留原文指针：输出中记录源文件名与每段对应的原文字符区间，便于回查全文。

摘要模型默认复用当前对话模型；``summarize.model`` 指定了其他本地模型时按需加载。

--------------------------------------------------------------------------
过程反馈与故障排查
--------------------------------------------------------------------------
此前只把"正在摘要第 i/N 段"输出到 stderr，前端对话框中不可见；小模型 + 纯 CPU 环境
下单段需要数十秒，长文档十几段即需十几分钟，用户无法区分"正在运行"与"已经卡死"。

当前行为：

* 过程可见：每一步都通过 ``on_progress(event)`` 回调抛出结构化事件（phase / status /
  index / total / elapsed / chars / tokens / slow / 计数），由调用方（``main.py``）
  补充语言包文案后发送给前端。本模块刻意不包含任何界面文案，也不 import i18n：
  摘要算法不应依赖当前界面语言。
* 避免用户长时间干等：单段生成使用 ``engine.chat_stream``，逐 token 迭代，每隔
  ``heartbeat_seconds`` 至少发送一次心跳（包含"已生成多少字符"），因此"长时间无任何
  输出"在设计上不会出现。
* 可中断：同样逐 token 迭代，``should_stop()`` 一旦为真立即停止本轮生成，不会出现
  "点击停止后仍需等待整段运行完毕"。使用非流式引擎（没有 ``chat_stream``）时只能在
  调用返回后判断，此时会在事件中标注 ``stream=False``。
* 超时：``chunk_timeout_seconds``（0 表示不限）按"该段从开始到当前"的墙钟时间判定，
  超时行为由 ``chunk_timeout_action`` 决定：``skip``（跳过并继续）、``retry``（重试
  一次，再次超时按 skip 处理）、``abort``（中止整个摘要）。
* 区分"慢"与"故障"：慢只提示（``slow=True`` + ``slow_chunks`` 计数），不打断；真正的
  失败（模型调用异常、超时或全部段落失败）才给出错误与恢复建议。
* 日志脱敏：只记录段号、字符数、耗时、状态与控制字符，从不记录文档正文（正文可能
  包含合同金额、当事人等敏感信息）。

回调事件字段（均为结构化数据，不含文案）：

``phase``   ``prepare`` / ``map`` / ``reduce`` / ``done`` / ``aborted`` / ``failed``
``status``  ``start`` / ``running`` / ``ok`` / ``failed`` / ``timeout`` / ``skipped`` / ``retry``
``index`` / ``total``      当前段号与总段数（reduce 阶段表示第几组）
``chars``                  该段原文字符数
``tokens``                 该段已生成的字符数（心跳时用于判断是否仍在产出）
``elapsed``                整个流程已耗时（秒）
``chunk_elapsed``          当前段已耗时（秒）
``slow``                   当前段是否已超过 ``slow_chunk_seconds``（慢不等于故障）
``stream``                 是否使用流式路径（流式才支持段内中断与心跳）
``failed_chunks`` / ``timeout_chunks`` / ``skipped_chunks`` / ``slow_chunks``  累计计数
``error``                  失败时的简短原因（不含正文）
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from config import SummarizeConfig
from extractive import extract as extractive_pick

log = logging.getLogger("EchoDesk.Summarizer")

# [EN] System prompt for chunk summarization. It follows the same six-element
# structure as conversation.MODES, including output format, reasoning, and tone.
#
# Deliberately kept short: this prompt accompanies every chunk sent to the model, so
# a longer prompt multiplies context usage by the chunk count (a long document of
# 3000-character chunks may have a dozen chunks). Only constraints that materially
# affect summary quality are kept, with no elaborate role-play framing.
# [ZH] 分段摘要的系统提示（与 conversation.MODES 使用同一套六要素写法，补齐输出格式、
# [ZH] 思维与语气）。
#
# [ZH] 刻意保持简短：该提示词会随每一个分段一起送入模型，写得越长，上下文占用按分段数
# [ZH] 放大（3000 字符一段的长文档会有十几段）；因此只保留对摘要质量真正有约束力的几条，
# [ZH] 不做角色扮演式的铺陈。
_CHUNK_SYSTEM = (
    "【角色】你是文档分段摘要器，只提炼、不发挥。\n"
    "【输出格式】纯文本要点，每条一行，不加标题、不加评论。\n"
    "【思维】先扫出数字、日期、金额、百分比、当事人名称、义务与结论，再压缩其余内容。\n"
    "【边界】只写本段出现过的信息；本段确实没有实质内容时，只输出「本段无实质内容」；"
    "绝不补充原文没有的内容。\n"
    "【语气】中性、书面，用简体中文。"
)
# [EN] System prompt for the merge phase: compress while preventing loss of key
# elements. It adds the rule that inconsistent descriptions are both retained: in
# contracts and reports, the same item is commonly worded differently across
# sections, and the previous prompt only required merging duplicates, so the model
# frequently dropped one of the formulations.
# [ZH] 合并阶段的系统提示：在压缩的同时防止关键要素丢失。新增"表述不一致时两者都
# [ZH] 保留"一条：合同或报告中同一事项在不同段落的写法不同属于常见情况，此前的提示词仅
# [ZH] 要求"合并重复"，模型容易丢弃其中一种说法。
_MERGE_SYSTEM = (
    "【角色】你是文档摘要合并器，把多段要点并成一份完整摘要。\n"
    "【输出格式】先一段总述，再按主题分组列出要点；合并重复项，不新增条目。\n"
    "【思维】先归并同类项，再回头检查数字、日期、金额、当事人、义务与结论有无遗漏。\n"
    "【边界】只合并已给出的要点，不引入新信息；不同段落对同一事项表述不一致时，"
    "两种都保留并标注「原文表述不一致」。\n"
    "【语气】中性、书面，用简体中文。"
)

# [EN] Maximum reduce recursion depth, preventing pathological inputs from causing
# unbounded merging.
# [ZH] reduce 递归深度上限（防止病态输入导致无限归并）。
_MAX_REDUCE_DEPTH = 4

#: [EN] When a chunk summary fails, times out, or is skipped, fall back to the first
#: characters of the chunk so that the final summary still covers that content.
#: [ZH] 段摘要失败、超时或跳过时，用该段前若干字符兜底，以保证最终摘要仍覆盖该段
#: [ZH] 内容。
_FALLBACK_MIN_CHARS = 200


@dataclass
class SummaryResult:
    """
    [EN]
    Segmented summarization result, including process statistics.
    [ZH]
    分段摘要结果（含过程统计）。
    """

    # [EN] Final summary text injected into the context, including source pointers.
    # [ZH] 最终注入上下文的摘要文本（含原文指针）。
    text: str
    # [EN] Number of chunks.
    # [ZH] 分段数量。
    chunk_count: int = 0
    # [EN] (start, end, chunk summary) for each chunk.
    # [ZH] (起始, 结束, 段摘要)。
    segments: List[Tuple[int, int, str]] = field(default_factory=list)
    # [EN] Total character count of the original text.
    # [ZH] 原文总字符数。
    origin_chars: int = 0
    # [EN] Whether summarization actually ran; otherwise the text is unchanged or
    # truncated.
    # [ZH] 是否实际执行了摘要（否则为原样或截断）。
    summarized: bool = False
    # [EN] ---------- Process and failure statistics ----------
    # [ZH] ---------- 过程与故障统计 ----------
    # [EN] One of ok / partial / failed / aborted.
    # [ZH] ok / partial / failed / aborted。
    status: str = "ok"
    # [EN] Chunks for which the model produced a summary normally.
    # [ZH] 由模型正常产出摘要的段数。
    ok_chunks: int = 0
    # [EN] Chunks where the model call raised and truncation fallback was used.
    # [ZH] 模型调用异常、改用截断兜底的段数。
    failed_chunks: int = 0
    # [EN] Chunks where streaming stopped early but produced usable content.
    # [ZH] 流式生成中途断开、但已产出可用内容的段数。
    partial_chunks: int = 0
    # [EN] Chunks that hit the per-chunk timeout and were handled by its policy.
    # [ZH] 触发单段超时并按其策略处理的段数。
    timeout_chunks: int = 0
    # [EN] Chunks skipped by policy after a timeout or failure.
    # [ZH] 超时或失败后按策略跳过的段数。
    skipped_chunks: int = 0
    # [EN] Chunks whose duration exceeded ``slow_chunk_seconds``.
    # [ZH] 单段耗时超过 ``slow_chunk_seconds`` 的段数。
    slow_chunks: int = 0
    # [EN] Whether the user aborted the operation.
    # [ZH] 用户是否主动中止。
    aborted: bool = False
    # [EN] Total duration of the flow, in seconds.
    # [ZH] 整个流程耗时（秒）。
    elapsed: float = 0.0
    # [EN] Short reason when status is failed (no document body).
    # [ZH] status=failed 时的简短原因（不含正文）。
    error: str = ""
    # [EN] ---------- Extractive pre-filter statistics ----------
    # [ZH] ---------- 抽取式预筛统计 ----------
    # [EN] Whether the pre-filter took effect cannot be inferred from the final
    # summary alone, so assertable numbers are recorded. The three fields must not be
    # conflated; tests assert their relative magnitudes:
    #   extractive_origin_chars  sum of original characters across all chunks;
    #   extractive_input_chars   sum of characters actually sent to the model after
    #                            pre-filtering;
    #   extractive_saved_chars   origin - input, the characters saved by pre-filtering.
    # Chunks without pre-filtering contribute the same value to origin and input, so
    # the compression ratio ``origin / input`` is directly comparable with the switch
    # on and off.
    # [ZH] 仅看最终摘要无法判断预筛是否生效，因此必须记录可断言的数字。三项口径不可
    # [ZH] 混淆，测试直接断言其相对大小：
    # [ZH]   extractive_origin_chars 所有段原文字符数之和；
    # [ZH]   extractive_input_chars  实际送入模型的字符数之和（预筛后）；
    # [ZH]   extractive_saved_chars  = origin - input，即预筛省下的字符数。
    # [ZH] 未做预筛的段三项均计入（origin == input），因此压缩比 ``origin / input`` 在
    # [ZH] 开关打开前后可直接对比。
    # [EN] Chunks that actually underwent pre-filtering.
    # [ZH] 真正发生预筛的段数。
    extractive_chunks: int = 0
    # [EN] Sum of original characters across chunks.
    # [ZH] 各段原文字符总和。
    extractive_origin_chars: int = 0
    # [EN] Sum of characters actually sent to the model across chunks.
    # [ZH] 各段实际送入模型的字符总和。
    extractive_input_chars: int = 0

    @property
    def extractive_saved_chars(self) -> int:
        """
        [EN]
        Characters saved by pre-filtering (origin - input).
        [ZH]
        预筛省下的字符数（origin - input）。
        """
        return max(0, self.extractive_origin_chars - self.extractive_input_chars)

    @property
    def extractive_ratio(self) -> float:
        """
        [EN]
        Characters actually sent to the model divided by original characters
        (1.0 means no compression).
        [ZH]
        实际送入模型的字数与原文字数之比（1.0 表示未压缩）。
        """
        if self.extractive_origin_chars <= 0:
            return 1.0
        return self.extractive_input_chars / self.extractive_origin_chars

    @property
    def pointer(self) -> str:
        """
        [EN]
        Source pointer: the source file and the character range of each chunk.
        [ZH]
        原文指针：源文件与各段字符区间。
        """
        ranges = ",".join(f"{a}-{b}" for a, b, _ in self.segments)
        return f"chars={ranges}" if ranges else ""

    @property
    def degraded(self) -> bool:
        """
        [EN]
        Whether any chunk used a fallback path, so the result can report that
        truthfully instead of implying normal operation.
        [ZH]
        是否有段落使用了兜底路径，以便在结果中如实说明，而不是默认一切正常。
        """
        return bool(self.failed_chunks or self.timeout_chunks or self.skipped_chunks
                    or self.partial_chunks)


def split_chunks(text: str, chunk_chars: int, overlap_chars: int) -> List[Tuple[int, int, str]]:
    """
    [EN]
    Split long text into overlapping chunks and return
    [(start_char, end_char, chunk_text)].

    Cut points prefer paragraph separators, line breaks, or sentence-ending
    punctuation; when no suitable boundary exists, the text is cut at the length
    limit.

    [ZH]
    将长文本切分为带重叠的段落，返回 [(起始字符, 结束字符, 段文本)]。

    切点优先选择段落分隔、换行或句末标点；找不到合适边界时按长度硬切。
    """
    n = len(text)
    if n == 0:
        return []
    chunk_chars = max(1, chunk_chars)
    overlap_chars = max(0, min(overlap_chars, chunk_chars - 1))
    chunks: List[Tuple[int, int, str]] = []
    start = 0
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            window = text[start:end]
            # [EN] Search backwards for a natural cut point, using at least half of
            # the window so chunks do not fragment.
            # [ZH] 从后向前查找自然的切分点（至少使用半段，避免碎成片段）。
            cut = -1
            for token in ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; "):
                pos = window.rfind(token)
                if pos > cut:
                    cut = pos + len(token)
            if cut > chunk_chars // 2:
                end = start + cut
        chunks.append((start, end, text[start:end]))
        if end >= n:
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


class DocumentSummarizer:
    """
    [EN]
    Map-reduce segmented summarizer.
    [ZH]
    map-reduce 分段摘要器。
    """

    def __init__(
        self,
        engine,
        cfg: SummarizeConfig,
        engine_factory: Optional[Callable[[str], object]] = None,
    ) -> None:
        self.engine = engine
        self.cfg = cfg
        self._engine_factory = engine_factory
        # [EN] Dedicated summarization engine, loaded lazily.
        # [ZH] 专用摘要模型引擎（懒加载）。
        self._dedicated = None
        self._dedicated_path = ""

    # [EN] ---------- Trigger evaluation ----------
    # [ZH] ---------- 触发判断 ----------

    def needs_summary(self, text: str, n_ctx: int) -> bool:
        """
        [EN]
        Decide whether the document requires segmented summarization.

        The text must reach ``min_chars`` and its estimated token count must exceed
        ``trigger_ratio`` of the context window.

        [ZH]
        判断是否需要对文档执行分段摘要。

        条件：文本长度达到 ``min_chars``，且估算 token 数超过窗口的 ``trigger_ratio``。
        """
        if not self.cfg.enabled or n_ctx <= 0:
            return False
        if len(text) < self.cfg.min_chars:
            return False
        tokens = self.engine.count_tokens(text)
        return tokens > max(1, int(n_ctx * self.cfg.trigger_ratio))

    def should_use_summary(self, text: str, n_ctx: int, force: bool = False) -> bool:
        """
        [EN]
        Summarize unconditionally when ``force=True``, which supports a manual
        full-document summarization request.
        [ZH]
        ``force=True`` 时无条件摘要（供用户手动触发全文摘要）。
        """
        if force:
            return bool(text) and self.cfg.enabled
        return self.needs_summary(text, n_ctx)

    # [EN] ---------- Main summarization flow ----------
    # [ZH] ---------- 摘要主流程 ----------

    def summarize(
        self,
        text: str,
        source_name: str = "",
        force: bool = False,
        on_progress: Optional[Callable[[Dict], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> SummaryResult:
        """
        [EN]
        Summarize a document in segments; returns the text unchanged when it is short
        or the feature is disabled.

        ``on_progress`` receives structured events (fields are documented in the
        module docstring) with no display text. ``should_stop`` aborts as soon as it
        returns true, so manual stop interrupts and cleans up correctly.

        [ZH]
        对文档执行分段摘要；文本较短或功能关闭时原样返回。

        ``on_progress`` 接收结构化事件（字段见模块头部注释），不含文案；
        ``should_stop`` 每次为真即中止（手动停止须能正确中断并清理）。
        """
        origin_chars = len(text)
        if not text:
            return SummaryResult(text="", origin_chars=0)
        if not self.cfg.enabled:
            return SummaryResult(text=text, origin_chars=origin_chars)
        if not force and len(text) < self.cfg.min_chars:
            return SummaryResult(text=text, origin_chars=origin_chars)

        started = time.perf_counter()
        engine = self._pick_engine()
        stream_capable = callable(getattr(engine, "chat_stream", None))
        chunks = split_chunks(text, self.cfg.chunk_chars, self.cfg.overlap_chars)
        total = len(chunks)

        counters = {"ok": 0, "failed": 0, "partial": 0, "timeout": 0, "skipped": 0, "slow": 0}
        # [EN] Pre-filter statistics: whether pre-filtering took effect cannot be
        # inferred from the final summary, so numeric counters are required.
        # [ZH] 预筛统计：仅看最终摘要无法判断预筛是否生效，必须记录数字。
        pf_chunks = 0
        pf_saved = 0
        pf_origin = 0
        pf_input = 0

        def pf() -> Dict[str, int]:
            """
            [EN]
            Accumulated pre-filter values carried in progress events. A function is
            used so that each iteration reads the latest values.
            [ZH]
            预筛累计量（供进度事件携带）；采用函数形式以读取每轮的最新值。
            """
            return {
                "prefilter_chunks": pf_chunks,
                "prefilter_saved_chars": pf_saved,
                "prefilter_origin_chars": pf_origin,
                "prefilter_input_chars": pf_input,
            }
        # [EN] Log sanitization: only chunk counts, character counts, and durations
        # are recorded; document bodies never enter the log.
        # [ZH] 日志脱敏：只记录段数、字符数与耗时，正文一律不写入日志。
        log.info(
            "开始分段摘要: %s（%d 字符 → %d 段，段长 %d，重叠 %d，模型 %s，流式 %s，预筛 %s）",
            source_name or "(未命名)", origin_chars, total,
            self.cfg.chunk_chars, self.cfg.overlap_chars, self._engine_label(),
            "是" if stream_capable else "否",
            f"{float(getattr(self.cfg, 'extractive_ratio', 0.35)):.0%}"
            if bool(getattr(self.cfg, "extractive", True)) else "关",
        )

        self._emit(on_progress, {
            "phase": "prepare", "status": "start", "index": 0, "total": total,
            "chars": origin_chars, "elapsed": 0.0, "stream": stream_capable,
        })

        segments: List[Tuple[int, int, str]] = []
        aborted = False

        for index, (start, end, chunk) in enumerate(chunks, 1):
            if should_stop is not None and should_stop():
                aborted = True
                break

            self._emit(on_progress, {
                "phase": "map", "status": "start", "index": index, "total": total,
                "chars": len(chunk), "elapsed": time.perf_counter() - started,
                "chunk_elapsed": 0.0, "stream": stream_capable, **self._counters(counters, pf()),
            })

            # [EN] Extractive pre-filtering: reduce character count before handing
            # the text to the model. It takes milliseconds and does not occupy the
            # model, and any exception falls back to the original text; it must never
            # fail the whole summarization.
            # [ZH] 抽取式预筛：先压缩字数，再交给模型。该步骤为毫秒级，不占用模型；
            # [ZH] 任何异常都退回原文，绝不因此导致整篇摘要失败。
            model_input, raw_chars, kept_chars = self._prefilter(chunk)
            pf_origin += raw_chars
            pf_input += kept_chars
            if kept_chars < raw_chars:
                pf_chunks += 1
                pf_saved += raw_chars - kept_chars
                log.debug("第 %d/%d 段预筛：%d → %d 字符", index, total, raw_chars, kept_chars)

            attempt = 0
            summary = ""
            status = "failed"
            while True:
                attempt += 1
                summary, status = self._generate_chunk(
                    chunk=chunk, index=index, total=total, start=start, end=end,
                    engine=engine, on_progress=on_progress, should_stop=should_stop,
                    run_started=started, counters=counters, model_input=model_input,
                )
                if status == "aborted":
                    aborted = True
                    break
                if (status == "timeout" and attempt == 1
                        and str(self.cfg.chunk_timeout_action).lower() == "retry"):
                    # [EN] retry: retry exactly once; a second timeout is treated as
                    # skip, so unbounded retries cannot consume the time budget.
                    # [ZH] retry：仅重试一次；再次超时按 skip 处理，避免无限重试耗尽时间。
                    self._emit(on_progress, {
                        "phase": "map", "status": "retry", "index": index, "total": total,
                        "chars": len(chunk), "elapsed": time.perf_counter() - started,
                        "chunk_elapsed": 0.0, "stream": stream_capable, **self._counters(counters, pf()),
                    })
                    continue
                break

            if aborted:
                break

            if status == "timeout":
                counters["timeout"] += 1
                action = str(self.cfg.chunk_timeout_action).lower()
                if action == "abort":
                    log.warning("第 %d/%d 段超时（%s 秒），按配置中止整篇摘要",
                                index, total, self.cfg.chunk_timeout_seconds)
                    aborted = True
                    self._emit(on_progress, {
                        "phase": "map", "status": "timeout", "index": index, "total": total,
                        "chars": len(chunk), "elapsed": time.perf_counter() - started,
                        "chunk_elapsed": 0.0, "error": "timeout",
                        "stream": stream_capable, **self._counters(counters, pf()),
                    })
                    break

            if status in ("failed", "timeout"):
                hold = summary or self._fallback_text(chunk)
                counters["skipped"] += 1
                counters["failed"] += 1 if status == "failed" else 0
                segments.append((start, end, hold))
                log.warning("第 %d/%d 段%s（%d 字符），改用截断兜底并继续",
                            index, total, "超时" if status == "timeout" else "失败", len(chunk))
                self._emit(on_progress, {
                    "phase": "map",
                    "status": "timeout" if status == "timeout" else "failed",
                    "index": index, "total": total, "chars": len(chunk),
                    "summary_chars": len(hold),
                    "elapsed": time.perf_counter() - started, "chunk_elapsed": 0.0,
                    "stream": stream_capable, "fallback": True, **self._counters(counters, pf()),
                })
                continue

            if status == "partial":
                counters["partial"] += 1
            if status == "ok":
                counters["ok"] += 1
            segments.append((start, end, summary))
            self._emit(on_progress, {
                "phase": "map", "status": "ok", "index": index, "total": total,
                "chars": len(chunk), "summary_chars": len(summary),
                "elapsed": time.perf_counter() - started, "chunk_elapsed": 0.0,
                "stream": stream_capable, **self._counters(counters, pf()),
            })

        # [EN] ---------- Finalization ----------
        # [ZH] ---------- 收尾 ----------
        elapsed = time.perf_counter() - started
        if aborted:
            log.info("分段摘要已中止: %s（已完成 %d/%d 段，耗时 %.1f 秒）",
                     source_name or "(未命名)", len(segments), total, elapsed)
            self._emit(on_progress, {
                "phase": "aborted", "status": "failed", "index": len(segments), "total": total,
                "elapsed": elapsed, "stream": stream_capable, **self._counters(counters, pf()),
            })
            # [EN] Aborted: no partial summary is produced, because a half summary
            # silently entering the context is more dangerous than a failure. The
            # caller informs the user that a retry is possible, and the original file
            # content remains intact in the session.
            # [ZH] 已中止：不产出半份摘要（半份摘要静默进入上下文比失败更危险），由
            # [ZH] 调用方提示用户可重试；原文件内容仍完整保留在会话中。
            return SummaryResult(
                text="", chunk_count=total, segments=segments, origin_chars=origin_chars,
                summarized=False, status="aborted", aborted=True,
                ok_chunks=counters["ok"], failed_chunks=counters["failed"],
                partial_chunks=counters["partial"], timeout_chunks=counters["timeout"],
                skipped_chunks=counters["skipped"], slow_chunks=counters["slow"],
                elapsed=elapsed, error="aborted",
                extractive_chunks=pf_chunks, extractive_origin_chars=pf_origin,
                extractive_input_chars=pf_input,
            )

        if counters["ok"] + counters["partial"] == 0:
            # [EN] No chunk produced a summary: this is a genuine failure (model
            # unavailable, out of memory, or engine fault). Report it truthfully and
            # keep the retry path, rather than injecting a fake summary composed only
            # of truncations.
            # [ZH] 所有段落均未能产出摘要：这属于真实故障（模型不可用、内存不足或引擎
            # [ZH] 异常）。应如实报错并保留重试路径，而不是注入一份"全是截断"的假摘要。
            log.error("分段摘要失败: %s（%d 段全部未能产出摘要，耗时 %.1f 秒）",
                      source_name or "(未命名)", total, elapsed)
            self._emit(on_progress, {
                "phase": "failed", "status": "failed", "index": 0, "total": total,
                "elapsed": elapsed, "error": "all_chunks_failed",
                "stream": stream_capable, **self._counters(counters, pf()),
            })
            return SummaryResult(
                text="", chunk_count=total, segments=segments, origin_chars=origin_chars,
                summarized=False, status="failed", ok_chunks=0,
                failed_chunks=counters["failed"], partial_chunks=counters["partial"],
                timeout_chunks=counters["timeout"], skipped_chunks=counters["skipped"],
                slow_chunks=counters["slow"], elapsed=elapsed, error="all_chunks_failed",
                extractive_chunks=pf_chunks, extractive_origin_chars=pf_origin,
                extractive_input_chars=pf_input,
            )

        self._emit(on_progress, {
            "phase": "reduce", "status": "start", "index": 1, "total": 1,
            "elapsed": time.perf_counter() - started, "stream": stream_capable,
            **self._counters(counters, pf()),
        })
        merged = self._reduce(
            [s for _, _, s in segments], engine, started=started,
            on_progress=on_progress, should_stop=should_stop, counters=counters,
        )
        body = self._format(source_name, origin_chars, segments, merged)
        elapsed = time.perf_counter() - started
        degraded = bool(counters["failed"] or counters["timeout"] or counters["skipped"]
                        or counters["partial"])
        status = "partial" if degraded else "ok"
        result = SummaryResult(
            text=body, chunk_count=total, segments=segments,
            origin_chars=origin_chars, summarized=True, status=status,
            ok_chunks=counters["ok"], failed_chunks=counters["failed"],
            partial_chunks=counters["partial"], timeout_chunks=counters["timeout"],
            skipped_chunks=counters["skipped"], slow_chunks=counters["slow"],
            elapsed=elapsed,
            extractive_chunks=pf_chunks, extractive_origin_chars=pf_origin,
            extractive_input_chars=pf_input,
        )
        log.info(
            "分段摘要完成: %s（%d/%d 段正常，失败 %d，超时 %d，跳过 %d，慢段 %d，"
            "摘要 %d 字，占用约 %d tokens，耗时 %.1f 秒）；"
            "抽取式预筛 %d/%d 段生效，送模型 %d → %d 字符（省 %d，压缩 %.1f 倍）",
            source_name or "(未命名)", counters["ok"] + counters["partial"], total,
            counters["failed"], counters["timeout"], counters["skipped"], counters["slow"],
            len(body), self.engine.count_tokens(body), elapsed,
            pf_chunks, total, pf_origin, pf_input, pf_saved,
            (pf_origin / pf_input) if pf_input else 1.0,
        )
        self._emit(on_progress, {
            "phase": "done", "status": "ok", "index": total, "total": total,
            "summary_chars": len(body), "elapsed": elapsed, "stream": stream_capable,
            **self._counters(counters, pf()),
        })
        return result

    # [EN] ---------- Extractive pre-filtering ----------
    # [ZH] ---------- 抽取式预筛 ----------

    def _prefilter(self, chunk: str) -> Tuple[str, int, int]:
        """
        [EN]
        Apply extractive pre-filtering to one chunk and return ``(text sent to the
        model, original character count, filtered character count)``.

        Reducing the character count sent to the model has the largest leverage in
        the pipeline. Model generation speed is the bottleneck (about 5-10
        characters per second for a small model on CPU), and segmented summarization
        performs N serial generations. Reducing every chunk input to one third lowers
        prefill and output length together, and this step takes milliseconds without
        occupying the model.

        Three safety fallbacks, all affecting performance only and never
        correctness:
        * Disabled switch, chunk too short, or no sentence boundary: return the input
          unchanged (see ``extractive.extract``);
        * Empty or nearly empty extraction result: return the input unchanged; it is
          better to save no time than to give the model an empty chunk;
        * Extraction raises: log and return the input unchanged. Pre-filtering is a
          pure optimization and must never fail the whole summarization.

        [ZH]
        对一段原文执行抽取式预筛，返回 ``(送入模型的文本, 原字符数, 压缩后字符数)``。

        减少送入模型的字数是该流程中杠杆最大的一步。模型生成速度是瓶颈（纯 CPU 上
        小模型约 5~10 字/秒），而分段摘要是 N 次串行生成；把每段输入压缩到三分之一，
        prefill 与输出长度同步下降，而该步骤为毫秒级、不占用模型。

        安全兜底（三处，均只影响性能，不影响正确性）：

        * 关闭开关、段过短或切不出句子 → 原样返回（见 ``extractive.extract``）；
        * 抽取结果为空或几乎为空 → 原样返回（宁可节省时间为零，也不能让模型收到空段）；
        * 抽取抛出异常 → 记录日志并原样返回（预筛属于纯优化，绝不能因此导致整篇摘要
          失败）。
        """
        if not bool(getattr(self.cfg, "extractive", True)):
            return chunk, len(chunk), len(chunk)
        try:
            picked = extractive_pick(
                chunk,
                ratio=float(getattr(self.cfg, "extractive_ratio", 0.35) or 0.35),
                min_chars=int(getattr(self.cfg, "extractive_min_chars", 600) or 0),
            )
        except Exception:
            log.warning("抽取式预筛失败，本段按原文送模型（%d 字符）", len(chunk), exc_info=True)
            return chunk, len(chunk), len(chunk)
        if not picked.changed or len(picked.text) < 40:
            return chunk, len(chunk), len(chunk)
        return picked.text, len(chunk), len(picked.text)

    # [EN] ---------- Single-chunk generation (heartbeat, timeout, interruption) ----------
    # [ZH] ---------- 单段生成（含心跳、超时与中断） ----------

    def _generate_chunk(
        self,
        chunk: str,
        index: int,
        total: int,
        start: int,
        end: int,
        engine,
        on_progress: Optional[Callable[[Dict], None]],
        should_stop: Optional[Callable[[], bool]],
        run_started: float,
        counters: Dict[str, int],
        model_input: str = "",
    ) -> Tuple[str, str]:
        """
        [EN]
        Generate one chunk summary and return ``(text, status)``, where status is one
        of ok/partial/failed/timeout/aborted.

        ``model_input`` is the text actually sent to the model after extractive
        pre-filtering; it defaults to ``chunk``. Two invariants apply:
        * The character range ``start-end`` in the prompt always refers to the
          original text, because the UI must show which part of the source the
          summary came from. Pre-filtering affects only the character count sent to
          the model, not the provenance;
        * Fallback text uses ``chunk`` (the original) rather than ``model_input``. When
          the model produces nothing for this chunk, falling back to the first
          characters of the original is preferable to presenting the filtered remnant
          as a summary.

        [ZH]
        生成一段摘要，返回 ``(文本, 状态)``；状态为 ok/partial/failed/timeout/aborted
        之一。

        ``model_input`` 是经过抽取式预筛后真正送入模型的文本（默认取 ``chunk``）。
        需注意两点：

        * 提示中的字符区间 ``start-end`` 始终指原文区间：界面需要显示"该段来自原文
          哪一部分"，预筛只影响送入的字数，不改变出处；
        * 兜底文本使用 ``chunk``（原文）而非 ``model_input``：该段模型未给出结果时，
          宁可回退到原文前若干字符，也不能用"预筛后的残段"冒充摘要。
        """
        body = model_input or chunk
        messages = [
            {"role": "system", "content": _CHUNK_SYSTEM},
            {"role": "user", "content": f"【第 {index}/{total} 段，原文第 {start}-{end} 字符】\n{body}"},
        ]
        return self._call_model(
            messages=messages, max_tokens=self.cfg.max_summary_tokens, engine=engine,
            on_progress=on_progress, should_stop=should_stop, run_started=run_started,
            phase="map", index=index, total=total, chars=len(body), counters=counters,
        )

    def _call_model(
        self,
        messages: List[dict],
        max_tokens: int,
        engine,
        on_progress: Optional[Callable[[Dict], None]],
        should_stop: Optional[Callable[[], bool]],
        run_started: float,
        phase: str,
        index: int,
        total: int,
        chars: int,
        counters: Dict[str, int],
    ) -> Tuple[str, str]:
        """
        [EN]
        Call the local model to produce a summary.

        [ZH]
        调用模型生成一段文本；流式优先，从而支持心跳、段内超时与段内中断。

        非流式引擎（没有 ``chat_stream``，例如测试专用的假引擎或第三方实现）退化为
        "调用返回后再判定"：心跳与中断只能在段与段之间生效，事件里会带 ``stream=False``，
        调用方据此如实提示，而不是假装能中途打断。
        """
        timeout = int(getattr(self.cfg, "chunk_timeout_seconds", 0) or 0)
        heartbeat = float(getattr(self.cfg, "heartbeat_seconds", 5.0) or 5.0)
        slow_after = int(getattr(self.cfg, "slow_chunk_seconds", 60) or 60)
        started = time.perf_counter()
        slow_reported = False

        def _tick(produced: int, chunk_elapsed: float) -> None:
            """
            [EN]
            Advance the progress counter.

            [ZH]
            心跳：如实报告"还在产出多少字符"，让用户能看出是否真有进展。
            """
            nonlocal slow_reported
            is_slow = chunk_elapsed >= slow_after
            if is_slow:
                slow_reported = True
            self._emit(on_progress, {
                "phase": phase, "status": "running", "index": index, "total": total,
                "chars": chars, "tokens": produced, "chunk_elapsed": chunk_elapsed,
                "elapsed": time.perf_counter() - run_started,
                "tps": (produced / chunk_elapsed) if chunk_elapsed > 0 else 0.0,
                "slow": is_slow, "stream": True, **self._counters(counters, pf()),
            })

        stream_fn = getattr(engine, "chat_stream", None)
        if callable(stream_fn):
            pieces: List[str] = []
            produced = 0
            try:
                iterator = stream_fn(messages, temperature=0.2, max_tokens=max_tokens)
            except Exception:
                log.warning("第 %d/%d 段流式调用创建失败，改用非流式", index, total, exc_info=True)
                iterator = None
            if iterator is not None:
                last_tick = started
                try:
                    for piece in iterator:
                        pieces.append(piece)
                        produced += len(piece)
                        now = time.perf_counter()
                        if should_stop is not None and should_stop():
                            return "".join(pieces), "aborted"
                        if timeout and (now - started) >= timeout:
                            return "".join(pieces), "timeout"
                        if (now - last_tick) >= heartbeat:
                            _tick(produced, now - started)
                            last_tick = now
                    if slow_reported or (time.perf_counter() - started) >= slow_after:
                        counters["slow"] += 1
                    text = "".join(pieces).strip()
                    return (text, "ok") if text else ("", "failed")
                except Exception:
                    # [EN] The stream broke mid-way: content produced so far is still usable,
                    # [EN] but it is reported truthfully as partial.
                    # [ZH] 流式中途断开：已产出的内容仍可用，但如实标注为 partial
                    log.warning("第 %d/%d 段流式生成中断（已产出 %d 字符）",
                                index, total, produced, exc_info=True)
                    if slow_reported or (time.perf_counter() - started) >= slow_after:
                        counters["slow"] += 1
                    text = "".join(pieces).strip()
                    return (text, "partial") if text else ("", "failed")

        # [EN] ---- Non-streaming fallback ----
        # [ZH] ---- 非流式兜底 ----
        try:
            text = engine.chat_complete(messages, temperature=0.2, max_tokens=max_tokens).strip()
        except Exception:
            log.warning("第 %d/%d 段摘要调用失败", index, total, exc_info=True)
            return "", "failed"
        chunk_elapsed = time.perf_counter() - started
        if chunk_elapsed >= slow_after:
            counters["slow"] += 1
            self._emit(on_progress, {
                "phase": phase, "status": "running", "index": index, "total": total,
                "chars": chars, "tokens": len(text), "chunk_elapsed": chunk_elapsed,
                "elapsed": time.perf_counter() - run_started,
                "tps": (len(text) / chunk_elapsed) if chunk_elapsed > 0 else 0.0,
                "slow": True, "stream": False, **self._counters(counters, pf()),
            })
        if should_stop is not None and should_stop():
            return text, "aborted"
        if timeout and chunk_elapsed >= timeout:
            return text, "timeout"
        return (text, "ok") if text else ("", "failed")

    def _fallback_text(self, chunk: str) -> str:
        """
        [EN]
        Return a deterministic fallback summary when the model is unavailable.

        [ZH]
        段摘要失败时的兜底文本（截断该段前若干字符）。
        """
        limit = max(_FALLBACK_MIN_CHARS, int(self.cfg.max_summary_tokens) * 2)
        return chunk[:limit] + ("……" if len(chunk) > limit else "")

    def _counters(self, counters: Dict[str, int], prefilter: Optional[Dict[str, int]] = None) -> Dict[str, int]:
        """
        [EN]
        Return counters used for progress reporting.

        [ZH]
        把累计计数展开成事件字段（键名与 SummaryResult 对齐，便于前端直接读）。

        ``prefilter`` 是抽取式预筛的累计量（段数 / 省下的字符 / 原本要送的字符），
        带上它之后，前端进度提示与测试都能验证"预筛到底有没有生效"。
        """
        data = {
            "ok_chunks": counters["ok"],
            "partial_chunks": counters["partial"],
            "failed_chunks": counters["failed"],
            "timeout_chunks": counters["timeout"],
            "skipped_chunks": counters["skipped"],
            "slow_chunks": counters["slow"],
        }
        if prefilter:
            data.update(prefilter)
        return data

    def _emit(self, on_progress: Optional[Callable[[Dict], None]], event: Dict) -> None:
        """
        [EN]
        Emit a summarization progress event.

        [ZH]
        抛出进度事件；回调自身抛异常不影响摘要流程（进度不能反过来搞挂主流程）。
        """
        if on_progress is None:
            return
        if not bool(getattr(self.cfg, "progress_events", True)):
            return
        try:
            on_progress(event)
        except Exception:
            log.debug("进度回调异常（已忽略）", exc_info=True)

    # [EN] ---------- Internal implementation ----------
    # [ZH] ---------- 内部实现 ----------

    def _pick_engine(self):
        """
        [EN]
        Select the summarization engine for the current input.

        [ZH]
        选择摘要模型：summarize.model 指定且存在时加载专用引擎，否则复用当前模型。
        """
        want = (self.cfg.model or "").strip()
        if not want or self._engine_factory is None:
            return self.engine
        current = getattr(self.engine, "model_path", "")
        if want == current:
            return self.engine
        if self._dedicated is not None and self._dedicated_path == want:
            return self._dedicated
        try:
            self._dedicated = self._engine_factory(want)
            self._dedicated_path = want
            log.info("已加载专用摘要模型: %s", want)
            return self._dedicated
        except Exception:
            log.warning("专用摘要模型 %s 加载失败，回退当前模型", want, exc_info=True)
            return self.engine

    def _engine_label(self) -> str:
        engine = self._dedicated if self._dedicated is not None else self.engine
        return str(getattr(engine, "model_path", "当前模型"))

    def _summarize_block(self, content: str, system: str, header: str, max_tokens: int, engine) -> str:
        """
        [EN]
        Summarize one block of text.

        [ZH]
        对一段内容调用模型生成摘要；失败时退回该段前若干字符。

        保留这个简化版入口供归并阶段（reduce）使用：归并是在摘要**已经产出**之后的
        二次压缩，失败时用截断兜底即可，不需要再把心跳/超时事件透传给界面
        （界面此时显示的是"正在合并"这一阶段）。
        """
        try:
            text = engine.chat_complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"{header}\n{content}"},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
            ).strip()
            if text:
                return text
        except Exception:
            log.warning("归并阶段调用失败，改用截断兜底", exc_info=True)
        fallback = max(_FALLBACK_MIN_CHARS, self.cfg.max_summary_tokens * 2)
        return content[:fallback] + ("……" if len(content) > fallback else "")

    def _reduce(
        self,
        summaries: List[str],
        engine,
        depth: int = 0,
        started: Optional[float] = None,
        on_progress: Optional[Callable[[Dict], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        counters: Optional[Dict[str, int]] = None,
    ) -> str:
        """
        [EN]
        Reduce partial summaries into a final summary.

        [ZH]
        把多段摘要递归归并到 merge_max_tokens 以内。
        """
        if not summaries:
            return ""
        if len(summaries) == 1:
            text = summaries[0]
            return self._truncate_to_tokens(text, self.cfg.merge_max_tokens, engine)

        joined = "\n".join(f"- {s}" for s in summaries)
        if self._tokens(joined, engine) <= self.cfg.merge_max_tokens or depth >= _MAX_REDUCE_DEPTH:
            return self._truncate_to_tokens(joined, self.cfg.merge_max_tokens, engine)

        groups = self._group_by_tokens(summaries, max(64, self.cfg.merge_max_tokens // 2), engine)
        merged: List[str] = []
        for i, group in enumerate(groups, 1):
            if should_stop is not None and should_stop():
                # [EN] The reduce phase was aborted: whatever was merged so far is
                # [EN] returned as usual (chunk summaries are complete in themselves;
                # [EN] merging is only a second compression pass, so the truncation
                # [EN] fallback does not lose content).
                # [ZH] 归并阶段被中止：已归并的部分照常返回（段摘要本身是完整的，
                # [ZH] 归并只是二次压缩，截断兜底不会丢内容）
                break
            self._emit(on_progress, {
                "phase": "reduce", "status": "running", "index": i, "total": len(groups),
                "chars": sum(len(x) for x in group),
                "elapsed": (time.perf_counter() - started) if started else 0.0,
                "stream": False,
                **(self._counters(counters, pf()) if counters is not None else {}),
            })
            merged.append(self._summarize_block(
                "\n".join(f"- {s}" for s in group),
                system=_MERGE_SYSTEM,
                header=f"【合并第 {i}/{len(groups)} 组】",
                max_tokens=self.cfg.max_summary_tokens,
                engine=engine,
            ))
        if not merged:
            return self._truncate_to_tokens(joined, self.cfg.merge_max_tokens, engine)
        return self._reduce(merged, engine, depth + 1, started, on_progress, should_stop, counters)

    def _group_by_tokens(self, items: List[str], budget: int, engine) -> List[List[str]]:
        """
        [EN]
        Group text into token-bounded blocks.

        [ZH]
        按 token 预算把条目分组，尽量填满每组。
        """
        groups: List[List[str]] = []
        current: List[str] = []
        used = 0
        for item in items:
            size = self._tokens(item, engine)
            if current and used + size > budget:
                groups.append(current)
                current, used = [], 0
            current.append(item)
            used += size
        if current:
            groups.append(current)
        return groups

    def _tokens(self, text: str, engine=None) -> int:
        engine = engine or self.engine
        try:
            return int(engine.count_tokens(text))
        except Exception:
            return len(text) // 2

    def _truncate_to_tokens(self, text: str, max_tokens: int, engine) -> str:
        """
        [EN]
        Truncate text to a token limit.

        [ZH]
        按 token 上限截断（按字符比例近似，避免逐字二分带来的额外开销）。
        """
        total = self._tokens(text, engine)
        if total <= max_tokens or total <= 0:
            return text
        keep = max(1, int(len(text) * max_tokens / total))
        return text[:keep].rstrip() + "……"

    def _format(
        self,
        source_name: str,
        origin_chars: int,
        segments: List[Tuple[int, int, str]],
        merged: str,
    ) -> str:
        """
        [EN]
        Format a summary result for display.

        [ZH]
        拼接最终注入上下文的长文档摘要（含原文指针）。
        """
        title = source_name or "(未命名文档)"
        lines = [
            f"【长文档摘要】{title}",
            f"（原文 {origin_chars} 字符，已按 {len(segments)} 段分段摘要后合并，"
            f"以下是全文要点而非全文）",
            "",
            merged,
        ]
        if self.cfg.keep_pointer:
            ranges = ",".join(f"{a}-{b}" for a, b, _ in segments)
            lines += [
                "",
                f"【原文指针】{title} 字符区间: {ranges}",
                "（如需原文某段的确切表述，请让用户缩小问题范围或改用 /file 查看原文）",
            ]
        return "\n".join(lines)
