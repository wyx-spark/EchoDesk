"""长文档分段摘要（map-reduce）。

当待注入的文件全文超出上下文窗口一定比例时，不再直接截断，而是：

1. **分段（map）**：按 ``chunk_chars`` 切分，段间保留 ``overlap_chars`` 重叠，
   切点优先落在段落/句号边界，避免把一句话劈开；
2. **逐段摘要**：每段交给 LLM 生成不超过 ``max_summary_tokens`` 的要点；
3. **合并（reduce）**：段摘要合计超过 ``merge_max_tokens`` 时分组再摘要，
   递归到长度达标为止（带深度上限与截断兜底）；
4. **保留原文指针**：输出中记录源文件名与每段对应的原文字符区间，便于回查全文。

摘要模型默认复用当前对话模型；``summarize.model`` 指定了其他本地模型时按需加载。
"""
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

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


@dataclass
class SummaryResult:
    """分段摘要结果。"""

    text: str                       # 最终注入上下文的摘要文本（含原文指针）
    chunk_count: int = 0            # 分段数量
    segments: List[Tuple[int, int, str]] = field(default_factory=list)  # (起, 止, 段摘要)
    origin_chars: int = 0           # 原文总字符数
    summarized: bool = False        # 是否真的做了摘要（否则为原样/截断）

    @property
    def pointer(self) -> str:
        """原文指针：源文件 + 各段字符区间。"""
        ranges = ",".join(f"{a}-{b}" for a, b, _ in self.segments)
        return f"chars={ranges}" if ranges else ""


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
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> SummaryResult:
        """对文档做分段摘要；文本较短或功能关闭时原样返回。"""
        origin_chars = len(text)
        if not text:
            return SummaryResult(text="", origin_chars=0)
        if not self.cfg.enabled:
            return SummaryResult(text=text, origin_chars=origin_chars)
        if not force and len(text) < self.cfg.min_chars:
            return SummaryResult(text=text, origin_chars=origin_chars)

        engine = self._pick_engine()
        chunks = split_chunks(text, self.cfg.chunk_chars, self.cfg.overlap_chars)
        total = len(chunks)
        log.info(
            "开始分段摘要: %s（%d 字符 → %d 段，段长 %d，重叠 %d，模型 %s）",
            source_name or "(未命名)", origin_chars, total,
            self.cfg.chunk_chars, self.cfg.overlap_chars, self._engine_label(),
        )

        segments: List[Tuple[int, int, str]] = []
        for index, (start, end, chunk) in enumerate(chunks, 1):
            if on_progress:
                try:
                    on_progress(index, total, f"正在摘要第 {index}/{total} 段")
                except Exception:
                    pass
            summary = self._summarize_block(
                chunk,
                system=_CHUNK_SYSTEM,
                header=f"【第 {index}/{total} 段，原文第 {start}-{end} 字符】",
                max_tokens=self.cfg.max_summary_tokens,
                engine=engine,
            )
            segments.append((start, end, summary))

        merged = self._reduce([s for _, _, s in segments], engine)
        body = self._format(source_name, origin_chars, segments, merged)
        result = SummaryResult(
            text=body, chunk_count=total, segments=segments,
            origin_chars=origin_chars, summarized=True,
        )
        log.info(
            "分段摘要完成: %s（摘要 %d 字，占用约 %d tokens）",
            source_name or "(未命名)", len(body), self.engine.count_tokens(body),
        )
        return result

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
        """对一段内容调用模型生成摘要；失败时退回该段前若干字符。"""
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
            log.warning("分段摘要调用失败，改用截断兜底", exc_info=True)
        fallback = max(200, self.cfg.max_summary_tokens * 2)
        return content[:fallback] + ("……" if len(content) > fallback else "")

    def _reduce(self, summaries: List[str], engine, depth: int = 0) -> str:
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
        merged = [
            self._summarize_block(
                "\n".join(f"- {s}" for s in group),
                system=_MERGE_SYSTEM,
                header=f"【合并第 {i}/{len(groups)} 组】",
                max_tokens=self.cfg.max_summary_tokens,
                engine=engine,
            )
            for i, group in enumerate(groups, 1)
        ]
        return self._reduce(merged, engine, depth + 1)

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
