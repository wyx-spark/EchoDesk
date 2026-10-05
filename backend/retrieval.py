"""本地文档检索（v0.2.1 能力扩展-1 任务六：RAG 深度）。

方案选择与理由（任务书要求"由 DSH 评估后给出选择与理由"）
=========================================================

**结论：关键词检索（BM25）+ 段落定位，零新依赖；不引入向量库、不引入嵌入模型。**

理由：
1. **体积**：发布物体积是被任务书与门禁同时盯着的指标（安装后 381 MB，
   区间 350–400 MB）。向量库（faiss/hnswlib/chromadb）与 torch/sentence-transformers
   起步就是几十到几百 MB，而且还要**再带一个嵌入模型**（GGUF 或 ONNX，几十 MB 起）。
   这与"安装后不超过 400 MB"直接冲突。
2. **数据一致性**：llama.cpp 自带 ``create_embedding``，理论上可以零依赖算向量
   （审计确认），但那要求所选对话模型自身是 embedding 模型或另配一个 —— 用户换模型时
   整个索引就失效。关键词索引对模型无关，换模型不动索引。
3. **收益边界**：本工具面向**单机小模型**，用户上传的通常是同一件事的几份材料
   （合同、纪要、报告）。检索的意义是"别把 47 KB 全文塞进 8192 窗口"，
   BM25 在中英文混合的办公文档上足够完成这件事；语义检索的优势主要体现在
   同义改写与跨语言召回，而这类问题在本地 0.6B 模型上本来就答不好。
4. **可解释与可核对**：关键词命中可以直接告诉用户"因为出现了这几个词"，
   而向量相似度只能给一个数字。任务书要求"引用溯源，用户可核对"，
   关键词检索的溯源更可信。

如果将来要上向量：``RetrievalConfig.mode`` 已经预留了 ``hybrid``/``vector`` 两个取值，
扩展点是 :meth:`Retriever.search`（返回 ``List[ScoredChunk]`` 即可，上层不用改）。

**与分段摘要的关系**（任务书要求明确"什么场景用哪个"）：

| 场景 | 用哪个 | 为什么 |
| :--- | :--- | :--- |
| 短文档（全文能进窗口） | **全文注入**，不检索 | 材料够短时全文最准，检索只会丢信息 |
| 长文档已生成摘要 + 具体问题 | **摘要（全局）+ 检索片段（细节）** | 摘要负责"这份材料讲什么"，检索负责"30 个工作日那条在哪" |
| 长文档已生成摘要 + 泛问 | 摘要为主，检索只补 top-k | 泛问题没有可检索的关键词，硬检索会引入噪声 |

也就是说：**摘要是压缩，检索是定位，两者互补而不是替代** —— 这一条也写进了
``USER_GUIDE`` 与 ``DEVELOPER``。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ===========================================================================
# 分词（中英混合办公文档）
# ===========================================================================

#: 中文切 2 字滑窗、英文数字按词。**刻意不用 jieba**（体积 + 词典冷启动），
#: 与 ``extractive`` 的取舍一致：小模型场景下 2-gram 的召回足够，且零依赖。
_ASCII_WORD = re.compile(r"[a-z0-9_]+")
_HAN_RUN = re.compile(r"[\u4e00-\u9fff]+")
#: 数值词元（含小数与百分号）。**必须单独抓**：首轮实测漏了它，于是"0.3%"被
#: ``[a-z0-9_]+`` 切成 "0" 与 "3" 两个单字符、又被"长度 > 1"过滤掉，
#: 结果"违约金比例是多少"这类**带数字的关键事实问题检索不到**（6 个关键事实只命中 4 个）。
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?\s*%?")

#: 无信息量的英文词（不参与 BM25，避免"the 出现了 30 次"这种噪声命中）
_STOPWORDS = frozenset("""
a an the and or of to in on for with by is are was were be been being this that these those
it its as at from into over under than then so such not no do does did has have had will would
""".split())


def tokenize(text: str) -> List[str]:
    """把一段文本切成检索用的词元（小写化；中文按 2 字、英文按词、数值按整体）。"""
    lowered = (text or "").lower()
    terms: List[str] = []
    for raw in _NUMBER.findall(lowered):
        piece = raw.replace(" ", "")
        if piece and piece not in terms:
            terms.append(piece)
    terms.extend(w for w in _ASCII_WORD.findall(lowered)
                 if len(w) > 1 and w not in _STOPWORDS)
    for run in _HAN_RUN.findall(lowered):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    return terms


# ===========================================================================
# 分块（句边界优先，带重叠，保留字符区间供溯源）
# ===========================================================================

#: 句子边界。中文标点与英文标点都算，句末标点跟随前一句（切分后不丢标点）。
_SENTENCE_END = re.compile(r"[^。！？；!?;\n]*[。！？；!?;\n]")


def split_sentences(text: str) -> List[Tuple[int, int, str]]:
    """切句并**保留字符区间**：``[(start, end, sentence)]``。

    区间是引用溯源的基础 —— 用户要能拿着"字符 210-480"回去核对原文，
    只给句子文本是不够的（同样的句子可能在文档里出现多次）。
    """
    out: List[Tuple[int, int, str]] = []
    for match in _SENTENCE_END.finditer(text or ""):
        piece = match.group(0)
        if piece.strip():
            out.append((match.start(), match.end(), piece))
    return out


def chunk_text(text: str, chunk_chars: int = 600, overlap_chars: int = 100
               ) -> List[Tuple[int, int, str]]:
    """按句边界把长文切成 ``(start, end, chunk)`` 列表。

    三条规则：
      * **不切断句子**（除非单句比 chunk_chars 还长，那只能硬切）；
      * **保留重叠**：上一块的末尾若干句子带进下一块，避免"答案横跨块边界"查不到；
      * 区间是**原文的绝对偏移**，重叠部分在两个块里出现两次，溯源仍然准确。
    """
    body = text or ""
    if not body.strip():
        return []
    chunk_chars = max(120, int(chunk_chars or 600))
    overlap_chars = max(0, min(int(overlap_chars or 0), chunk_chars // 2))
    sentences = split_sentences(body)
    if not sentences:
        # 没有任何句末标点（例如一整段代码或表格）：按长度硬切
        out = []
        step = max(1, chunk_chars - overlap_chars)
        for start in range(0, len(body), step):
            piece = body[start:start + chunk_chars]
            if piece.strip():
                out.append((start, start + len(piece), piece))
        return out

    chunks: List[Tuple[int, int, str]] = []
    current: List[Tuple[int, int, str]] = []

    def flush() -> None:
        if not current:
            return
        start = current[0][0]
        end = current[-1][1]
        chunks.append((start, end, body[start:end]))

    for sentence in sentences:
        if sentence[1] - sentence[0] > chunk_chars:
            # 超长单句：先结算当前块，再把这一句按长度切开
            flush()
            current = []
            piece_start = sentence[0]
            while piece_start < sentence[1]:
                piece_end = min(piece_start + chunk_chars, sentence[1])
                chunks.append((piece_start, piece_end, body[piece_start:piece_end]))
                piece_start = piece_end
            continue
        if current and (sentence[1] - current[0][0]) > chunk_chars:
            flush()
            # 用末尾若干句子做重叠
            tail: List[Tuple[int, int, str]] = []
            tail_len = 0
            for item in reversed(current):
                if tail_len >= overlap_chars:
                    break
                tail.insert(0, item)
                tail_len += item[1] - item[0]
            current = tail
        current.append(sentence)
    flush()
    return chunks


# ===========================================================================
# BM25 索引
# ===========================================================================

@dataclass
class Chunk:
    """一个可检索的片段（自带溯源信息）。"""

    file: str
    index: int          # 该文件内的第几块（从 1 开始，给用户看的段号）
    start: int          # 原文起始字符偏移
    end: int            # 原文结束字符偏移
    text: str
    length: int = 0     # 词元数（BM25 的长度归一化用）

    def __post_init__(self) -> None:
        if not self.length:
            self.length = max(1, len(tokenize(self.text)))

    def locate(self) -> str:
        """人类可读的位置描述（界面上"文件 + 字符区间"就是这么显示的）。"""
        return f"{self.file}（字符 {self.start}-{self.end}）"


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float
    matched: List[str] = field(default_factory=list)   # 命中的词元（可解释性）


#: BM25 标准参数。k1 控制词频饱和、b 控制长度归一化；1.5/0.75 是文献默认值，
#: 办公文档上没有必要重新调参（调了也说不清是文档变了还是参数变了）。
_BM25_K1 = 1.5
_BM25_B = 0.75


class BM25Index:
    """一个文件（或若干文件）的 BM25 索引。"""

    def __init__(self) -> None:
        self.chunks: List[Chunk] = []
        self._df: Dict[str, int] = {}          # 词元 → 出现在多少个块里
        self._tf: List[Dict[str, int]] = []    # 每个块的词频
        self._total_len = 0

    # ---------- 构建 ----------

    def add_chunks(self, chunks: Sequence[Chunk]) -> None:
        for chunk in chunks:
            terms = tokenize(chunk.text)
            counts: Dict[str, int] = {}
            for term in terms:
                counts[term] = counts.get(term, 0) + 1
            self.chunks.append(chunk)
            self._tf.append(counts)
            self._total_len += max(1, len(terms))
            for term in counts:
                self._df[term] = self._df.get(term, 0) + 1

    @property
    def avg_len(self) -> float:
        return (self._total_len / len(self.chunks)) if self.chunks else 1.0

    # ---------- 检索 ----------

    def search(self, query: str, top_k: int = 4, min_score: float = 0.0
               ) -> List[ScoredChunk]:
        """返回按 BM25 得分降序的片段（得分 ≤ 0 的不返回）。"""
        terms = tokenize(query)
        if not terms or not self.chunks:
            return []
        total = len(self.chunks)
        avg_len = self.avg_len or 1.0
        scores: List[Tuple[float, int, List[str]]] = []
        unique_terms = list(dict.fromkeys(terms))
        for position, counts in enumerate(self._tf):
            score = 0.0
            matched: List[str] = []
            length = max(1, sum(counts.values()))
            for term in unique_terms:
                freq = counts.get(term, 0)
                if not freq:
                    continue
                df = self._df.get(term, 0)
                # BM25 的 idf（加 0.5 平滑；df 达到全部块时取一个极小的正值，
                # 而不是 0 —— 否则"每块都出现的词"会完全丧失区分度）
                idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
                denom = freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / avg_len)
                score += idf * (freq * (_BM25_K1 + 1)) / denom
                matched.append(term)
            if score > 0:
                scores.append((score, position, matched))
        scores.sort(key=lambda item: -item[0])
        out: List[ScoredChunk] = []
        for score, position, matched in scores[:max(1, top_k)]:
            if score < min_score:
                continue
            out.append(ScoredChunk(chunk=self.chunks[position], score=score, matched=matched))
        return out


# ===========================================================================
# 检索器（多文件 + 增量）
# ===========================================================================

@dataclass
class RetrievalConfigView:
    """从 config.RetrievalConfig 抽出来的只读视图（避免本模块依赖 config）。"""

    enabled: bool = True
    mode: str = "keyword"
    chunk_chars: int = 600
    overlap_chars: int = 100
    top_k: int = 4
    max_context_chars: int = 2400
    min_score: float = 0.0


class Retriever:
    """多文件增量检索器。

    **增量**的含义：``add_file`` 只切分与索引那一份文件，已有文件一个字节都不动
    （``_chunks_by_file`` 里各自一份，删除也是整份摘除）。所以"新上传的文件自动
    加入索引，不必重建全部"是天然成立的，不需要额外的增量算法。
    """

    def __init__(self, config: Optional[RetrievalConfigView] = None) -> None:
        self.config = config or RetrievalConfigView()
        self._chunks_by_file: Dict[str, List[Chunk]] = {}
        self._index = BM25Index()
        self.indexed_chars = 0
        self.rebuilds = 0          # 诊断用：0 表示一直是增量添加

    # ---------- 构建 ----------

    def add_file(self, name: str, text: str) -> int:
        """索引（或**替换**）一份文件，返回块数。同名文件按最新内容重建。"""
        key = str(name or "")
        if key in self._chunks_by_file:
            self.remove_file(key)
        pieces = chunk_text(text, self.config.chunk_chars, self.config.overlap_chars)
        chunks = [Chunk(file=key, index=index, start=start, end=end, text=piece)
                  for index, (start, end, piece) in enumerate(pieces, 1)]
        self._chunks_by_file[key] = chunks
        self._index.add_chunks(chunks)
        self.indexed_chars += len(text or "")
        return len(chunks)

    def remove_file(self, name: str) -> bool:
        key = str(name or "")
        if key not in self._chunks_by_file:
            return False
        del self._chunks_by_file[key]
        # BM25 的 df/长度统计是增量的，删一份文件后最稳妥的做法是重建索引
        # （重建是纯内存操作，47 KB 文档实测 < 10 ms；换来的是统计量永远正确）。
        self.rebuilds += 1
        self._rebuild()
        return True

    def _rebuild(self) -> None:
        self._index = BM25Index()
        self.indexed_chars = 0
        for chunks in self._chunks_by_file.values():
            self._index.add_chunks(chunks)
            self.indexed_chars += sum(len(chunk.text) for chunk in chunks)

    def clear(self) -> None:
        self._chunks_by_file.clear()
        self._index = BM25Index()
        self.indexed_chars = 0

    # ---------- 查询 ----------

    @property
    def files(self) -> List[str]:
        return list(self._chunks_by_file)

    def stats(self) -> Dict[str, object]:
        return {
            "enabled": bool(self.config.enabled),
            "mode": self.config.mode,
            "files": len(self._chunks_by_file),
            "chunks": len(self._index.chunks),
            "indexed_chars": self.indexed_chars,
            "rebuilds": self.rebuilds,
            "chunk_chars": self.config.chunk_chars,
            "top_k": self.config.top_k,
        }

    def search(self, query: str, top_k: Optional[int] = None,
               files: Optional[Iterable[str]] = None) -> List[ScoredChunk]:
        """跨文件检索（``files`` 可以限定只看某几份，多文档问答就是不带它）。"""
        if not self.config.enabled:
            return []
        hits = self._index.search(query, top_k=top_k or self.config.top_k,
                                  min_score=self.config.min_score)
        if files is None:
            return hits
        wanted = {str(x) for x in files}
        return [hit for hit in hits if hit.chunk.file in wanted]

    def as_context(self, query: str, top_k: Optional[int] = None,
                   budget_chars: Optional[int] = None) -> str:
        """把检索结果渲染成可注入上下文的文本（**带编号与位置，供引用溯源**）。

        返回空串表示没有可用结果 —— 调用方据此**不要注入空块**（与搜索链路的
        "没有结果就不注入"是同一条纪律）。

        **重叠块去重**（真模型实测发现的）：分块本身带 100 字符重叠，而相邻块又常常
        一起入选，于是上下文里会出现两段几乎一样的内容（实测 0.6B 那次，
        [1] 与 [2] 是同一份合同里相隔不到 600 字符的两块，内容大半重复）。
        对模型来说这是白占窗口、还容易被当成"两处不同的约定"。
        规则：同一文件里与已选块**区间重叠超过较小块一半**的候选直接跳过；
        另外**内容开头 200 字符完全相同**的候选也跳过 —— 文档里大段重复（模板化合同、
        同一份材料的多个副本）时，区间并不重叠但内容是一样的，只按区间判重会漏掉。
        """
        hits = self.search(query, top_k=top_k)
        if not hits:
            return ""
        budget = int(budget_chars or self.config.max_context_chars)
        lines = ["【检索到的相关片段】（回答时请用 [编号] 标注依据；没有依据的内容不要写成引用）"]
        used = 0
        picked: List[ScoredChunk] = []
        seen_heads: List[str] = []
        for hit in hits:
            if any(self._overlaps(hit.chunk, other.chunk) for other in picked):
                continue
            head_key = hit.chunk.text.strip()[:200]
            if head_key and head_key in seen_heads:
                continue
            body = hit.chunk.text.strip()
            head = f"[{len(picked) + 1}] {hit.chunk.locate()}"
            block = f"{head}\n{body}"
            if used + len(block) > budget and picked:
                break
            lines.append(block)
            used += len(block)
            picked.append(hit)
            seen_heads.append(head_key)
        if not picked:
            return ""
        return "\n".join(lines)

    @staticmethod
    def _overlaps(first: Chunk, second: Chunk) -> bool:
        """两个块是否属于同一文件且区间重叠过半（较小块的一半）。"""
        if first.file != second.file:
            return False
        overlap = min(first.end, second.end) - max(first.start, second.start)
        if overlap <= 0:
            return False
        smaller = min(first.end - first.start, second.end - second.start)
        return smaller > 0 and overlap * 2 >= smaller


def config_view(cfg: object) -> RetrievalConfigView:
    """把 ``config.RetrievalConfig``（或任何同名属性的对象）转成只读视图。"""
    def pick(name: str, default: object) -> object:
        return getattr(cfg, name, default) if cfg is not None else default

    return RetrievalConfigView(
        enabled=bool(pick("enabled", True)),
        mode=str(pick("mode", "keyword")),
        chunk_chars=int(pick("chunk_chars", 600) or 600),
        overlap_chars=int(pick("overlap_chars", 100) or 0),
        top_k=int(pick("top_k", 4) or 4),
        max_context_chars=int(pick("max_context_chars", 2400) or 2400),
        min_score=float(pick("min_score", 0.0) or 0.0),
    )
