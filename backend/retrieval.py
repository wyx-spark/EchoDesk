"""
[EN]
Local document retrieval: BM25 keyword retrieval with passage localization.

Decision: use keyword retrieval (BM25) with passage localization and no new
dependencies; do not introduce a vector database or an embedding model.

Rationale:

1. Distribution size. The release size is constrained by both design and release
   gates (approximately 381 MB installed, with a 350-400 MB target). Vector
   libraries such as faiss, hnswlib, or chromadb, together with
   torch/sentence-transformers, add tens to hundreds of megabytes and require a
   separate embedding model (GGUF or ONNX). This directly conflicts with the
   size target.
2. Data consistency. llama.cpp exposes ``create_embedding`` and could compute
   vectors without a new runtime dependency, but the selected chat model would
   also have to be an embedding model or a second model would be required. The
   index would then become invalid whenever the user switches models. A keyword
   index is model-independent.
3. Benefit boundary. The product targets single-machine small models. Users
   typically upload several documents about the same matter (contracts, meeting
   notes, reports). Retrieval exists to avoid placing a 47 KB document into an
   8192-token window. BM25 is sufficient for mixed Chinese/English office
   documents. Semantic retrieval mainly improves synonym and cross-language
   recall, which small local models do not handle reliably anyway.
4. Explainability and verifiability. Keyword hits can be explained by the actual
   terms that matched, whereas vector similarity yields only a number. Citation
   traceability benefits from explicit term-level evidence.

If vector retrieval is added later, ``RetrievalConfig.mode`` already reserves the
``hybrid`` and ``vector`` values. The extension point is
:meth:`Retriever.search`, which returns ``List[ScoredChunk]``; callers do not
need to change.

Relationship to chunk summarization:

| Scenario | Mechanism | Reason |
| :--- | :--- | :--- |
| Short document fits in the window | Inject the full text; do not retrieve | Full text is most accurate for short material; retrieval would discard information |
| Long document with a summary and a specific question | Summary for global context plus retrieved passages for detail | The summary explains the document; retrieval locates the specific clause |
| Long document with a summary and a broad question | Summary first; retrieval only supplements top-k | Broad questions lack useful keywords; forced retrieval adds noise |

Summarization compresses; retrieval localizes. They are complementary, not
substitutes. The same distinction is documented in ``USER_GUIDE`` and
``DEVELOPER``.

[ZH]
本地文档检索：基于 BM25 的关键词检索与段落定位。

决策：采用关键词检索（BM25）与段落定位，不引入新依赖；不引入向量数据库或
嵌入模型。

理由：

1. 分发体积。发布体积同时受设计与发布门禁约束（安装后约 381 MB，目标区间
   350–400 MB）。faiss、hnswlib、chromadb 等向量库以及
   torch/sentence-transformers 会增加数十至数百 MB，并且还需要额外的嵌入模型
   （GGUF 或 ONNX）。这与体积目标直接冲突。
2. 数据一致性。llama.cpp 提供 ``create_embedding``，可在不增加运行时依赖的
   情况下计算向量，但所选对话模型必须同时是嵌入模型，或额外配置第二个模型。
   用户切换模型后索引将失效。关键词索引与模型无关。
3. 收益边界。产品面向单机小模型。用户通常上传围绕同一事项的多份文档（合同、
   会议纪要、报告）。检索用于避免将 47 KB 文档放入 8192 token 窗口。BM25
   足以处理中英混合办公文档。语义检索主要改善同义改写与跨语言召回，而小型
   本地模型本身也难以可靠处理这类问题。
4. 可解释与可核对。关键词命中可以直接说明匹配了哪些词，而向量相似度只能给出
   一个数值。引用溯源更依赖显式的词级证据。

若将来增加向量检索，``RetrievalConfig.mode`` 已预留 ``hybrid`` 与 ``vector``
两个取值。扩展点为 :meth:`Retriever.search`，其返回 ``List[ScoredChunk]``；
调用方无需修改。

与分段摘要的关系：

| 场景 | 机制 | 原因 |
| :--- | :--- | :--- |
| 短文档可放入窗口 | 注入全文，不检索 | 短材料全文最准确；检索会丢弃信息 |
| 长文档已有摘要且问题具体 | 摘要提供全局上下文，检索提供细节 | 摘要说明文档内容，检索定位具体条款 |
| 长文档已有摘要且问题宽泛 | 以摘要为主，检索仅补充 top-k | 宽泛问题缺少有效关键词，强制检索会引入噪声 |

摘要是压缩，检索是定位；两者互补而非替代。同一区分也记录在 ``USER_GUIDE``
与 ``DEVELOPER`` 中。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ===========================================================================
# [EN] Tokenization for mixed Chinese/English office documents
# [ZH] 中英混合办公文档分词
# ===========================================================================

#: [EN] Chinese uses two-character sliding windows; English and numeric tokens
#: [EN] are split by word. jieba is deliberately not used because of its size and
#: [EN] dictionary cold-start cost. Two-character grams provide sufficient recall
#: [EN] for small-model scenarios and introduce no dependency.
#: [ZH] 中文采用两字滑窗；英文与数字按词切分。刻意不使用 jieba，原因是体积与
#: [ZH] 词典冷启动成本。两字词元在小模型场景下召回足够，且不引入依赖。
_ASCII_WORD = re.compile(r"[a-z0-9_]+")
_HAN_RUN = re.compile(r"[\u4e00-\u9fff]+")
#: [EN] Numeric tokens, including decimals and percent signs, must be captured
#: [EN] separately. Otherwise a value such as "0.3%" is split into "0" and "3"
#: [EN] by ``[a-z0-9_]+``, both are discarded by the minimum-length filter, and
#: [EN] numeric factual questions such as "what is the penalty rate" cannot be
#: [EN] retrieved.
#: [ZH] 数值词元（含小数与百分号）必须单独捕获。否则“0.3%”会被
#: [ZH] ``[a-z0-9_]+`` 拆成“0”与“3”两个单字符，又被最小长度过滤掉，导致
#: [ZH] “违约金比例是多少”这类带数字的事实性问题无法检索。
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?\s*%?")

#: [EN] Low-information English words excluded from BM25 to avoid noise such as
#: [EN] "the" appearing 30 times.
#: [ZH] 不参与 BM25 的无信息量英文词，避免“the 出现 30 次”这类噪声命中。
_STOPWORDS = frozenset("""
a an the and or of to in on for with by is are was were be been being this that these those
it its as at from into over under than then so such not no do does did has have had will would
""".split())


def tokenize(text: str) -> List[str]:
    """
    [EN]
    Tokenize text for retrieval. Text is lower-cased; Chinese uses two-character
    windows, English uses words, and numeric values are kept as whole tokens.

    [ZH]
    将文本切分为检索词元。文本转为小写；中文使用两字滑窗，英文按词切分，
    数值保留为完整词元。
    """
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
# [EN] Chunking: sentence boundaries first, with overlap and source offsets
# [ZH] 分块：优先句边界，带重叠并保留源字符区间
# ===========================================================================

#: [EN] Sentence boundary. Both Chinese and English terminal punctuation are
#: [EN] recognized; terminal punctuation stays with the preceding sentence.
#: [ZH] 句子边界。中英文句末标点均被识别；句末标点保留在前一句中。
_SENTENCE_END = re.compile(r"[^。！？；!?;\n]*[。！？；!?;\n]")


def split_sentences(text: str) -> List[Tuple[int, int, str]]:
    """
    [EN]
    Split text into sentences while preserving character offsets:
    ``[(start, end, sentence)]``.

    Offsets are the basis of citation traceability. A user must be able to use a
    range such as "characters 210-480" to inspect the source text; sentence text
    alone is insufficient because the same sentence can occur multiple times.

    [ZH]
    按句子切分并保留字符区间：``[(start, end, sentence)]``。

    字符区间是引用溯源的基础。用户需要能够依据“字符 210–480”回查原文；仅提供
    句子文本不够，因为同一句子可能在文档中出现多次。
    """
    out: List[Tuple[int, int, str]] = []
    for match in _SENTENCE_END.finditer(text or ""):
        piece = match.group(0)
        if piece.strip():
            out.append((match.start(), match.end(), piece))
    return out


def chunk_text(text: str, chunk_chars: int = 600, overlap_chars: int = 100
               ) -> List[Tuple[int, int, str]]:
    """
    [EN]
    Split a long document into ``(start, end, chunk)`` entries by sentence
    boundaries.

    Rules:

    * Do not split sentences unless a single sentence exceeds ``chunk_chars``;
      such a sentence must be split by length.
    * Preserve overlap: trailing sentences of one chunk are carried into the
      next chunk so that an answer spanning a boundary remains retrievable.
    * Offsets are absolute positions in the source text. Overlapping text occurs
      in both chunks, but traceability remains exact.

    [ZH]
    按句子边界将长文档切分为 ``(start, end, chunk)`` 列表。

    规则：

    * 不切断句子，除非单句长度超过 ``chunk_chars``；此时只能按长度切分。
    * 保留重叠：将上一块末尾的若干句子带入下一块，使跨块答案仍可检索。
    * 区间为源文本中的绝对位置；重叠文本会出现在两个块中，但溯源仍然准确。
    """
    body = text or ""
    if not body.strip():
        return []
    chunk_chars = max(120, int(chunk_chars or 600))
    overlap_chars = max(0, min(int(overlap_chars or 0), chunk_chars // 2))
    sentences = split_sentences(body)
    if not sentences:
        # [EN] No terminal punctuation (for example, a code block or table):
        # [EN] split by length.
        # [ZH] 没有句末标点（例如代码块或表格）：按长度切分。
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
            # [EN] Oversized sentence: flush the current chunk, then split the
            # [EN] sentence by length.
            # [ZH] 超长单句：先结算当前块，再按长度切分该句。
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
            # [EN] Use trailing sentences for overlap.
            # [ZH] 使用末尾若干句子构成重叠。
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
# [EN] BM25 index
# [ZH] BM25 索引
# ===========================================================================

@dataclass
class Chunk:
    """
    [EN]
    A retrievable chunk carrying its own traceability metadata.

    [ZH]
    可检索片段，携带自身的溯源元数据。
    """

    file: str
    # [EN] One-based chunk index within the file, displayed to users as a passage number.
    # [ZH] 文件内从 1 开始的块序号，作为用户可见的段号。
    index: int
    # [EN] Absolute starting character offset in the source text.
    # [ZH] 源文本中的绝对起始字符偏移。
    start: int
    # [EN] Absolute ending character offset in the source text.
    # [ZH] 源文本中的绝对结束字符偏移。
    end: int
    text: str
    # [EN] Token count used for BM25 length normalization.
    # [ZH] 用于 BM25 长度归一化的词元数。
    length: int = 0

    def __post_init__(self) -> None:
        if not self.length:
            self.length = max(1, len(tokenize(self.text)))

    def locate(self) -> str:
        """
        [EN]
        Return a human-readable location description. The interface displays it
        as "file + character range".

        [ZH]
        返回人类可读的位置描述。界面以“文件 + 字符区间”的形式显示。
        """
        return f"{self.file}（字符 {self.start}-{self.end}）"


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float
    # [EN] Matched terms, retained for explainability.
    # [ZH] 命中的词元，用于可解释性展示。
    matched: List[str] = field(default_factory=list)


#: [EN] Standard BM25 parameters. k1 controls term-frequency saturation and b
#: [EN] controls length normalization. The values 1.5 and 0.75 are the literature
#: [EN] defaults; retuning for office documents is unnecessary and would make it
#: [EN] difficult to distinguish document changes from parameter changes.
#: [ZH] BM25 标准参数。k1 控制词频饱和，b 控制长度归一化。1.5 与 0.75 为文献
#: [ZH] 默认值；办公文档无需重新调参，否则难以区分文档变化与参数变化。
_BM25_K1 = 1.5
_BM25_B = 0.75


class BM25Index:
    """
    [EN]
    BM25 index for one or more files.

    [ZH]
    一个或多个文件的 BM25 索引。
    """

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
        """
        [EN]
        Return chunks ordered by descending BM25 score. Chunks with a
        non-positive score are omitted.

        [ZH]
        按 BM25 得分降序返回片段。得分非正的片段不会返回。
        """
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
                # [EN] BM25 idf with 0.5 smoothing. When df equals the total
                # [EN] number of chunks, a small positive value is used instead
                # [EN] of zero so that ubiquitous terms retain some
                # [EN] discriminative value.
                # [ZH] BM25 idf，使用 0.5 平滑。当 df 等于块总数时使用一个极小
                # [ZH] 正值而非 0，使无处不在的词仍保留一定区分度。
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
# [EN] Retriever: multiple files with incremental updates
# [ZH] 检索器：多文件与增量更新
# ===========================================================================

@dataclass
class RetrievalConfigView:
    """
    [EN]
    Read-only view extracted from ``config.RetrievalConfig`` so that this module
    does not depend on ``config``.

    [ZH]
    从 ``config.RetrievalConfig`` 提取的只读视图，使本模块不依赖 ``config``。
    """

    enabled: bool = True
    mode: str = "keyword"
    chunk_chars: int = 600
    overlap_chars: int = 100
    top_k: int = 4
    max_context_chars: int = 2400
    min_score: float = 0.0


class Retriever:
    """
    [EN]
    Incremental retriever for multiple files.

    Incrementality means that ``add_file`` only chunks and indexes the newly
    supplied file; existing files are not touched. Each file has its own entry
    in ``_chunks_by_file``, and removal deletes only that file's entry. Newly
    uploaded files therefore join the index without a full rebuild.

    [ZH]
    面向多文件的增量检索器。

    增量更新意味着 ``add_file`` 仅对新文件执行分块与索引，不修改已有文件。
    每个文件在 ``_chunks_by_file`` 中拥有独立条目，删除时仅移除该文件条目。
    因此新上传文件可加入索引而无需全量重建。
    """

    def __init__(self, config: Optional[RetrievalConfigView] = None) -> None:
        self.config = config or RetrievalConfigView()
        self._chunks_by_file: Dict[str, List[Chunk]] = {}
        self._index = BM25Index()
        self.indexed_chars = 0
        self.rebuilds = 0          # 诊断用：0 表示一直是增量添加

    # ---------- 构建 ----------

    def add_file(self, name: str, text: str) -> int:
        """
        [EN]
        Index or replace a file and return the number of chunks. A file with the
        same name is rebuilt from its latest content.

        [ZH]
        索引或替换文件，并返回块数。同名文件将按最新内容重建。
        """
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
        # [EN] BM25 document-frequency and length statistics are incremental.
        # [EN] After removing a file, rebuilding the index is the safest way to
        # [EN] keep those statistics correct. Rebuilding is an in-memory
        # [EN] operation and is inexpensive for ordinary office documents.
        # [ZH] BM25 的文档频率与长度统计是增量维护的。删除文件后，重建索引是
        # [ZH] 保持统计正确的最稳妥方式。重建为纯内存操作，对普通办公文档开销很低。
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
        """
        [EN]
        Search across files. ``files`` can restrict the search to selected
        documents; multi-document question answering omits it.

        [ZH]
        跨文件检索。``files`` 可将检索限定到指定文档；多文档问答不传该参数。
        """
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
        """
        [EN]
        Render retrieval results as context text with numbering and source
        locations for citation traceability.

        An empty string means no usable result; callers must not inject an empty
        block. This follows the same rule as the search pipeline: no result, no
        injection.

        Overlapping chunks are deduplicated. Chunking intentionally introduces
        overlap, and adjacent chunks are often selected together, so the same
        content can appear twice in the context. This wastes context window and
        may be interpreted as two separate provisions.

        Deduplication rules:

        * Skip a candidate whose source range overlaps a selected chunk from the
          same file by more than half of the smaller chunk.
        * Also skip a candidate whose first 200 characters exactly match an
          already selected chunk. Templated contracts and duplicate copies can
          contain identical content with different ranges; range-only
          deduplication would miss those cases.

        [ZH]
        将检索结果渲染为可注入上下文的文本，并附带编号与来源位置，以支持引用溯源。

        返回空串表示没有可用结果；调用方不得注入空块。该规则与搜索链路一致：
        没有结果就不注入。

        重叠块需要去重。分块本身会引入重叠，相邻块又常被同时选中，导致相同内容
        在上下文中重复出现。这会浪费上下文窗口，并可能被模型理解为两条不同的约定。

        去重规则：

        * 若候选与同一文件中已选块的源区间重叠超过较小块的一半，则跳过该候选。
        * 若候选开头 200 个字符与已选块完全相同，也跳过。模板化合同或重复副本可能
          在不同区间包含相同内容；仅按区间去重会漏掉这些情况。
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
        """
        [EN]
        Return whether two chunks belong to the same file and their ranges
        overlap by more than half of the smaller chunk.

        [ZH]
        返回两个块是否属于同一文件，且区间重叠超过较小块的一半。
        """
        if first.file != second.file:
            return False
        overlap = min(first.end, second.end) - max(first.start, second.start)
        if overlap <= 0:
            return False
        smaller = min(first.end - first.start, second.end - second.start)
        return smaller > 0 and overlap * 2 >= smaller


def config_view(cfg: object) -> RetrievalConfigView:
    """
    [EN]
    Convert ``config.RetrievalConfig`` or any object with the same attributes
    into a read-only view.

    [ZH]
    将 ``config.RetrievalConfig`` 或任何具有相同属性的对象转换为只读视图。
    """
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
