"""长文档分段摘要（map-reduce），带过程反馈、心跳、超时与中断。

当待注入的文件全文超出上下文窗口一定比例时，不再直接截断，而是：

1. **分段（map）**：按 ``chunk_chars`` 切分，段间保留 ``overlap_chars`` 重叠，
   切点优先落在段落/句号边界，避免把一句话劈开；
2. **逐段摘要**：每段交给 LLM 生成不超过 ``max_summary_tokens`` 的要点；
3. **合并（reduce）**：段摘要合计超过 ``merge_max_tokens`` 时分组再摘要，
   递归到长度达标为止（带深度上限与截断兜底）；
4. **保留原文指针**：输出中记录源文件名与每段对应的原文字符区间，便于回查全文。

摘要模型默认复用当前对话模型；``summarize.model`` 指定了其他本地模型时按需加载。

--------------------------------------------------------------------------
v0.2.1 综合优化（任务四）：过程反馈与故障排查
--------------------------------------------------------------------------
此前只把"正在摘要第 i/N 段"打到 **stderr**，前端对话框里看不到；小模型 + 纯 CPU 下
一段要跑几十秒，长文档十几段就是十几分钟，用户无法区分"正在跑"与"已经卡死"。

现在改为：

* **过程可见**：每一步都通过 ``on_progress(event)`` 回调抛出**结构化事件**
  （phase / status / index / total / elapsed / chars / tokens / slow / 计数），
  由调用方（``main.py``）补上语言包文案后发给前端。本模块刻意不含任何界面文案，
  也不 import i18n —— 摘要算法不应该关心当前是哪种界面语言。
* **不让用户干等**：单段生成走 ``engine.chat_stream``，逐 token 迭代，
  每隔 ``heartbeat_seconds`` 至少发一次心跳（含"已生成多少字符"），
  因此"长时间没有任何输出"这种情况在设计上就不会出现。
* **可中断**：同样是逐 token 迭代，``should_stop()`` 一旦为真立刻停止本轮生成，
  不会出现"点了停止还要等整段跑完"。用非流式引擎（没有 ``chat_stream``）时
  只能在调用返回后判断，此时会在事件里标注 ``stream=False``。
* **超时**：``chunk_timeout_seconds``（0 = 不限）按"该段从开始到现在"的墙钟时间判定，
  超时行为由 ``chunk_timeout_action`` 决定：``skip``（跳过并继续）/ ``retry``（重试一次，
  再超时按 skip）/ ``abort``（中止整个摘要）。
* **区分"慢"与"坏"**：慢只提示（``slow=True`` + ``slow_chunks`` 计数），不打断；
  真正的失败（模型调用异常 / 超时 / 全部段落失败）才给出错误与恢复建议。
* **日志脱敏**：只记录段号、字符数、耗时、状态与控制字符，**从不记录文档正文**
  （正文可能含合同金额、当事人等敏感信息）。

回调事件字段（都是结构化数据，无文案）：

``phase``   ``prepare`` / ``map`` / ``reduce`` / ``done`` / ``aborted`` / ``failed``
``status``  ``start`` / ``running`` / ``ok`` / ``failed`` / ``timeout`` / ``skipped`` / ``retry``
``index`` / ``total``      当前段号与总段数（reduce 阶段表示第几组）
``chars``                  该段原文字符数
``tokens``                 该段已生成的字符数（心跳时用它判断"还在产出"）
``elapsed``                整个流程已耗时（秒）
``chunk_elapsed``          当前段已耗时（秒）
``slow``                   当前段是否已超过 ``slow_chunk_seconds``（慢 ≠ 坏）
``stream``                 是否走了流式（流式才支持段内中断与心跳）
``failed_chunks`` / ``timeout_chunks`` / ``skipped_chunks`` / ``slow_chunks``  累计计数
``error``                  失败时的简短原因（不含正文）
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from config import SummarizeConfig

log = logging.getLogger("EchoDesk.Summarizer")

# 分段摘要的系统提示（v0.2.1 提示词优化：补齐「输出格式 / 思维 / 语气」，
# 与 conversation.MODES 用同一套六要素写法）。
#
# 【刻意保持简短】这段提示词会**随每一个分段**一起送进模型，写长等于按分段数
# 放大上下文占用（3000 字符一段的长文档会有十几段）；所以只保留对摘要质量
# 真正有约束力的几条，不做角色扮演式的铺陈。
_CHUNK_SYSTEM = (
    "【角色】你是文档分段摘要器，只提炼、不发挥。\n"
    "【输出格式】纯文本要点，每条一行，不加标题、不加评论。\n"
    "【思维】先扫出数字、日期、金额、百分比、当事人名称、义务与结论，再压缩其余内容。\n"
    "【边界】只写本段出现过的信息；本段确实没有实质内容时，只输出「本段无实质内容」；"
    "绝不补充原文没有的内容。\n"
    "【语气】中性、书面，用简体中文。"
)
# 合并阶段的系统提示：在压缩的同时防止关键要素丢失。
# 新增一条「表述不一致时两者都保留」：合同/报告里同一事项在不同段落写法不同
# 是常见情况，此前的提示词只要求"合并重复"，模型容易把其中一种说法丢掉。
_MERGE_SYSTEM = (
    "【角色】你是文档摘要合并器，把多段要点并成一份完整摘要。\n"
    "【输出格式】先一段总述，再按主题分组列出要点；合并重复项，不新增条目。\n"
    "【思维】先归并同类项，再回头检查数字、日期、金额、当事人、义务与结论有无遗漏。\n"
    "【边界】只合并已给出的要点，不引入新信息；不同段落对同一事项表述不一致时，"
    "两种都保留并标注「原文表述不一致」。\n"
    "【语气】中性、书面，用简体中文。"
)

# reduce 递归深度上限（防止病态输入导致无限归并）
_MAX_REDUCE_DEPTH = 4

#: 段摘要失败/超时/跳过时，用该段前若干字符兜底（保证最终摘要仍覆盖该段内容）
_FALLBACK_MIN_CHARS = 200


@dataclass
class SummaryResult:
    """分段摘要结果（含任务四的过程统计）。"""

    text: str                       # 最终注入上下文的摘要文本（含原文指针）
    chunk_count: int = 0            # 分段数量
    segments: List[Tuple[int, int, str]] = field(default_factory=list)  # (起, 止, 段摘要)
    origin_chars: int = 0           # 原文总字符数
    summarized: bool = False        # 是否真的做了摘要（否则为原样/截断）
    # ---------- 任务四：过程与故障统计 ----------
    status: str = "ok"              # ok / partial / failed / aborted
    ok_chunks: int = 0              # 由模型正常产出摘要的段数
    failed_chunks: int = 0          # 模型调用异常、改用截断兜底的段数
    partial_chunks: int = 0         # 流式生成中途断开、但已产出可用内容的段数
    timeout_chunks: int = 0         # 触发单段超时并按其策略处理的段数
    skipped_chunks: int = 0         # 超时/失败后按策略跳过的段数
    slow_chunks: int = 0            # 单段耗时超过 slow_chunk_seconds 的段数
    aborted: bool = False           # 用户主动中止
    elapsed: float = 0.0            # 整个流程耗时（秒）
    error: str = ""                 # status=failed 时的简短原因（不含正文）

    @property
    def pointer(self) -> str:
        """原文指针：源文件 + 各段字符区间。"""
        ranges = ",".join(f"{a}-{b}" for a, b, _ in self.segments)
        return f"chars={ranges}" if ranges else ""

    @property
    def degraded(self) -> bool:
        """是否有段落走了兜底（用于在结果里如实说明，而不是假装一切正常）。"""
        return bool(self.failed_chunks or self.timeout_chunks or self.skipped_chunks
                    or self.partial_chunks)


def split_chunks(text: str, chunk_chars: int, overlap_chars: int) -> List[Tuple[int, int, str]]:
    """把长文本切成带重叠的段落，返回 [(起始字符, 结束字符, 段文本)]。

    切点优先选段落分隔、换行或句末标点；找不到合适边界时按长度硬切。
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
            # 从后往前找自然的切分点（至少用到半段，避免碎成片段）
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
    """map-reduce 分段摘要器。"""

    def __init__(
        self,
        engine,
        cfg: SummarizeConfig,
        engine_factory: Optional[Callable[[str], object]] = None,
    ) -> None:
        self.engine = engine
        self.cfg = cfg
        self._engine_factory = engine_factory
        self._dedicated = None      # 专用摘要模型引擎（懒加载）
        self._dedicated_path = ""

    # ---------- 触发判断 ----------

    def needs_summary(self, text: str, n_ctx: int) -> bool:
        """判断是否需要对文档做分段摘要。

        依据：文本长度达到 ``min_chars``，且估算 token 数超过窗口的 ``trigger_ratio``。
        """
        if not self.cfg.enabled or n_ctx <= 0:
            return False
        if len(text) < self.cfg.min_chars:
            return False
        tokens = self.engine.count_tokens(text)
        return tokens > max(1, int(n_ctx * self.cfg.trigger_ratio))

    def should_use_summary(self, text: str, n_ctx: int, force: bool = False) -> bool:
        """force=True 时无条件摘要（供用户手动触发全文摘要）。"""
        if force:
            return bool(text) and self.cfg.enabled
        return self.needs_summary(text, n_ctx)

    # ---------- 摘要主流程 ----------

    def summarize(
        self,
        text: str,
        source_name: str = "",
        force: bool = False,
        on_progress: Optional[Callable[[Dict], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> SummaryResult:
        """对文档做分段摘要；文本较短或功能关闭时原样返回。

        ``on_progress`` 收到结构化事件（字段见模块头部注释），无文案；
        ``should_stop`` 每次为真即中止（任务四：手动停止要能正确中断并清理）。
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
        # 日志脱敏：只记段数/字符数/耗时，正文一律不进日志
        log.info(
            "开始分段摘要: %s（%d 字符 → %d 段，段长 %d，重叠 %d，模型 %s，流式 %s）",
            source_name or "(未命名)", origin_chars, total,
            self.cfg.chunk_chars, self.cfg.overlap_chars, self._engine_label(),
            "是" if stream_capable else "否",
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
                "chunk_elapsed": 0.0, "stream": stream_capable, **self._counters(counters),
            })

            attempt = 0
            summary = ""
            status = "failed"
            while True:
                attempt += 1
                summary, status = self._generate_chunk(
                    chunk=chunk, index=index, total=total, start=start, end=end,
                    engine=engine, on_progress=on_progress, should_stop=should_stop,
                    run_started=started, counters=counters,
                )
                if status == "aborted":
                    aborted = True
                    break
                if (status == "timeout" and attempt == 1
                        and str(self.cfg.chunk_timeout_action).lower() == "retry"):
                    # retry：只重试一次；再超时按 skip 处理，避免无限重试把时间耗光
                    self._emit(on_progress, {
                        "phase": "map", "status": "retry", "index": index, "total": total,
                        "chars": len(chunk), "elapsed": time.perf_counter() - started,
                        "chunk_elapsed": 0.0, "stream": stream_capable, **self._counters(counters),
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
                        "stream": stream_capable, **self._counters(counters),
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
                    "stream": stream_capable, "fallback": True, **self._counters(counters),
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
                "stream": stream_capable, **self._counters(counters),
            })

        # ---------- 收尾 ----------
        elapsed = time.perf_counter() - started
        if aborted:
            log.info("分段摘要已中止: %s（已完成 %d/%d 段，耗时 %.1f 秒）",
                     source_name or "(未命名)", len(segments), total, elapsed)
            self._emit(on_progress, {
                "phase": "aborted", "status": "failed", "index": len(segments), "total": total,
                "elapsed": elapsed, "stream": stream_capable, **self._counters(counters),
            })
            # 已终止：不产出半份摘要（半份摘要静默进入上下文比失败更危险），
            # 由调用方提示用户可重试；原文件内容仍完整保留在会话里。
            return SummaryResult(
                text="", chunk_count=total, segments=segments, origin_chars=origin_chars,
                summarized=False, status="aborted", aborted=True,
                ok_chunks=counters["ok"], failed_chunks=counters["failed"],
                partial_chunks=counters["partial"], timeout_chunks=counters["timeout"],
                skipped_chunks=counters["skipped"], slow_chunks=counters["slow"],
                elapsed=elapsed, error="aborted",
            )

        if counters["ok"] + counters["partial"] == 0:
            # 全部段落都没能产出摘要：这是真故障（模型不可用/内存不足/引擎异常），
            # 如实报错并保留重试路径，而不是注入一份"全是截断"的假摘要。
            log.error("分段摘要失败: %s（%d 段全部未能产出摘要，耗时 %.1f 秒）",
                      source_name or "(未命名)", total, elapsed)
            self._emit(on_progress, {
                "phase": "failed", "status": "failed", "index": 0, "total": total,
                "elapsed": elapsed, "error": "all_chunks_failed",
                "stream": stream_capable, **self._counters(counters),
            })
            return SummaryResult(
                text="", chunk_count=total, segments=segments, origin_chars=origin_chars,
                summarized=False, status="failed", ok_chunks=0,
                failed_chunks=counters["failed"], partial_chunks=counters["partial"],
                timeout_chunks=counters["timeout"], skipped_chunks=counters["skipped"],
                slow_chunks=counters["slow"], elapsed=elapsed, error="all_chunks_failed",
            )

        self._emit(on_progress, {
            "phase": "reduce", "status": "start", "index": 1, "total": 1,
            "elapsed": time.perf_counter() - started, "stream": stream_capable,
            **self._counters(counters),
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
        )
        log.info(
            "分段摘要完成: %s（%d/%d 段正常，失败 %d，超时 %d，跳过 %d，慢段 %d，"
            "摘要 %d 字，占用约 %d tokens，耗时 %.1f 秒）",
            source_name or "(未命名)", counters["ok"] + counters["partial"], total,
            counters["failed"], counters["timeout"], counters["skipped"], counters["slow"],
            len(body), self.engine.count_tokens(body), elapsed,
        )
        self._emit(on_progress, {
            "phase": "done", "status": "ok", "index": total, "total": total,
            "summary_chars": len(body), "elapsed": elapsed, "stream": stream_capable,
            **self._counters(counters),
        })
        return result

    # ---------- 单段生成（含心跳 / 超时 / 中断） ----------

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
    ) -> Tuple[str, str]:
        """生成一段摘要，返回 ``(文本, 状态)``；状态 ∈ ok/partial/failed/timeout/aborted。"""
        messages = [
            {"role": "system", "content": _CHUNK_SYSTEM},
            {"role": "user", "content": f"【第 {index}/{total} 段，原文第 {start}-{end} 字符】\n{chunk}"},
        ]
        return self._call_model(
            messages=messages, max_tokens=self.cfg.max_summary_tokens, engine=engine,
            on_progress=on_progress, should_stop=should_stop, run_started=run_started,
            phase="map", index=index, total=total, chars=len(chunk), counters=counters,
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
        """调用模型生成一段文本；流式优先，从而支持心跳、段内超时与段内中断。

        非流式引擎（没有 ``chat_stream``，例如测试用的假引擎或第三方实现）退化为
        "调用返回后再判定"：心跳与中断只能在段与段之间生效，事件里会带 ``stream=False``，
        调用方据此如实提示，而不是假装能中途打断。
        """
        timeout = int(getattr(self.cfg, "chunk_timeout_seconds", 0) or 0)
        heartbeat = float(getattr(self.cfg, "heartbeat_seconds", 5.0) or 5.0)
        slow_after = int(getattr(self.cfg, "slow_chunk_seconds", 60) or 60)
        started = time.perf_counter()
        slow_reported = False

        def _tick(produced: int, chunk_elapsed: float) -> None:
            """心跳：如实报告"还在产出多少字符"，让用户能看出是否真有进展。"""
            nonlocal slow_reported
            is_slow = chunk_elapsed >= slow_after
            if is_slow:
                slow_reported = True
            self._emit(on_progress, {
                "phase": phase, "status": "running", "index": index, "total": total,
                "chars": chars, "tokens": produced, "chunk_elapsed": chunk_elapsed,
                "elapsed": time.perf_counter() - run_started,
                "tps": (produced / chunk_elapsed) if chunk_elapsed > 0 else 0.0,
                "slow": is_slow, "stream": True, **self._counters(counters),
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
                    # 流式中途断开：已产出的内容仍可用，但如实标注为 partial
                    log.warning("第 %d/%d 段流式生成中断（已产出 %d 字符）",
                                index, total, produced, exc_info=True)
                    if slow_reported or (time.perf_counter() - started) >= slow_after:
                        counters["slow"] += 1
                    text = "".join(pieces).strip()
                    return (text, "partial") if text else ("", "failed")

        # ---- 非流式兜底 ----
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
                "slow": True, "stream": False, **self._counters(counters),
            })
        if should_stop is not None and should_stop():
            return text, "aborted"
        if timeout and chunk_elapsed >= timeout:
            return text, "timeout"
        return (text, "ok") if text else ("", "failed")

    def _fallback_text(self, chunk: str) -> str:
        """段摘要失败时的兜底文本（截断该段前若干字符）。"""
        limit = max(_FALLBACK_MIN_CHARS, int(self.cfg.max_summary_tokens) * 2)
        return chunk[:limit] + ("……" if len(chunk) > limit else "")

    def _counters(self, counters: Dict[str, int]) -> Dict[str, int]:
        """把累计计数展开成事件字段（键名与 SummaryResult 对齐，便于前端直接读）。"""
        return {
            "ok_chunks": counters["ok"],
            "partial_chunks": counters["partial"],
            "failed_chunks": counters["failed"],
            "timeout_chunks": counters["timeout"],
            "skipped_chunks": counters["skipped"],
            "slow_chunks": counters["slow"],
        }

    def _emit(self, on_progress: Optional[Callable[[Dict], None]], event: Dict) -> None:
        """抛出进度事件；回调自身抛异常不影响摘要流程（进度不能反过来搞挂主流程）。"""
        if on_progress is None:
            return
        if not bool(getattr(self.cfg, "progress_events", True)):
            return
        try:
            on_progress(event)
        except Exception:
            log.debug("进度回调异常（已忽略）", exc_info=True)

    # ---------- 内部实现 ----------

    def _pick_engine(self):
        """选择摘要模型：summarize.model 指定且存在时加载专用引擎，否则复用当前模型。"""
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
        """对一段内容调用模型生成摘要；失败时退回该段前若干字符。

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
        """把多段摘要递归归并到 merge_max_tokens 以内。"""
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
                # 归并阶段被中止：已归并的部分照常返回（段摘要本身是完整的，
                # 归并只是二次压缩，截断兜底不会丢内容）
                break
            self._emit(on_progress, {
                "phase": "reduce", "status": "running", "index": i, "total": len(groups),
                "chars": sum(len(x) for x in group),
                "elapsed": (time.perf_counter() - started) if started else 0.0,
                "stream": False,
                **(self._counters(counters) if counters is not None else {}),
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
        """按 token 预算把条目分组，尽量填满每组。"""
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
        """按 token 上限截断（按字符比例近似，避免逐字二分带来的额外开销）。"""
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
        """拼接最终注入上下文的长文档摘要（含原文指针）。"""
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
