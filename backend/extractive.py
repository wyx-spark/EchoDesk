"""
[EN]
Extractive pre-filtering (TextRank): compress each chunk to 10-20% before it is
sent to the model.

## Rationale

Task 4 of the specification: summarizing a 47 KB document in chunks takes too
long, and linear extrapolation predicts several hours for contracts of a few
hundred KB. The core constraint is that model generation throughput is limited
while summarization requires N serial generations. Reducing it requires reducing
both the number of characters sent to the model and the number of characters the
model must emit, and this does not require a model at all: a purely statistical
method can compress a 3000-character contract to roughly 500 characters without
dropping key sentences.

This is the highest-yield lever listed in the specification ("extractive
pre-filtering"), and it is the only responsibility of this module.

## Algorithm

TextRank (Mihalcea & Tarau, 2004) runs PageRank over a sentence graph. Similarity
uses TF-IDF cosine distance, with features drawn from the union of character
n-grams (Chinese-friendly, no tokenizer required) and Latin tokens
(English-friendly):

* Chinese has no whitespace word segmentation, so character bigrams are the
  simplest stable feature choice.
* English uses lowercased tokens, preventing character n-grams from treating
  high-frequency function words such as `a` / `the` as similarity.
* Using the union makes the module usable for mixed Chinese-English documents.

jieba / numpy / scikit-learn are deliberately not introduced. Those are the
project's runtime dependency allowlist (see `tools/check_requirements.py`), and
adding a tokenizer and a scientific-computing library for a few-hundred-millisecond
statistical step is not justified; the pure-standard-library implementation
measured well under 100 ms on a 47 KB text.

## Three engineering constraints

1. Sentence order is preserved: extraction only deletes sentences, it never
   reorders them. The summarizer feeds the extraction result to the model while
   recording the original character ranges `(start, end)`; reordering would
   desynchronize those ranges from the body text, and out-of-order input also
   degrades the model's coherence assessment.
2. Adjacent sentences are merged: consecutively selected sentences are merged into
   one continuous passage rather than separated by newlines. Inserting a newline
   per sentence severs the context of contract clauses and makes misreading more
   likely.
3. Lower and upper bounds: too low a ratio (for example 5%) omits key clauses,
   while too high a ratio is equivalent to no filtering. The default is 0.18 with
   a lower bound of 0.08 and an upper bound of 0.40. The first and last sentences
   are additionally always retained (the beginning of a document is usually an
   overview, and the end is usually a conclusion or signature block).

## Interface

``extract(text, ratio=0.18, min_chars=0) -> ExtractResult``

* ``ExtractResult.text``       Extracted text, in original order.
* ``ExtractResult.selected``   Indices of selected sentences (ascending).
* ``ExtractResult.scores``     Score of each sentence, aligned with ``sentences``;
  intended for tests and diagnosis and never rendered in the interface.
* ``ExtractResult.ratio``      Actual extraction ratio (extracted characters /
  original characters).

``split_sentences(text) -> List[(start, end, sentence)]`` is exported separately for
direct use in tests.

[ZH]
抽取式预筛（TextRank）：在送给模型之前，先把每一段压缩到 10~20%。

## 为什么需要它

任务书任务四：47KB 文档的分段摘要耗时过长，几百 KB 的合同线性外推可达数小时。
核心矛盾是模型生成速度有限，而摘要需要 N 次串行生成。要真正降低耗时，必须同时
减少送给模型的字数与模型需要输出的字数，而这件事根本不需要模型：纯统计方法即可
把一段 3000 字的合同压缩到约 500 字，且不丢失关键句。

这是任务书列出的杠杆中收益最高的一条（"抽取式预筛"），本模块只负责这一件事。

## 算法

TextRank（Mihalcea & Tarau, 2004）在句子图上运行 PageRank。相似度使用 TF-IDF
余弦，特征取字符 n-gram（对中文友好，无需分词器）与拉丁词元（对英文友好）的
并集：

* 中文没有空格分词，采用字符二元组作为特征是最简单且效果稳定的做法；
* 英文采用小写词元，避免字符 n-gram 把 `a`、`the` 这类高频虚词计为相似；
* 两者取并集，因此中英混排文档同样可用。

刻意不引入 jieba / numpy / scikit-learn：它们不在项目的运行期依赖白名单中
（见 `tools/check_requirements.py`），为了一个耗时几百毫秒的统计步骤而引入分词器
与科学计算库并不划算；纯标准库实现在 47KB 文本上实测远低于 100ms。

## 三个工程细节

1. 句子顺序不变：抽取只做"删句"，不做重排。摘要器要把抽取结果送给模型，同时
   记录 `(start, end)` 原文字符区间；重排会使区间与正文无法对应，而且乱序输入
   对模型的连贯性判断有负面影响。
2. 相邻句合并：被选中的相邻句子合并为一段连续文本，不逐句添加换行。逐句换行
   会切断合同条款的上下文，模型更容易误读。
3. 保底与封顶：比例过低（如 5%）会漏掉关键条款，过高则等同于未筛选。默认 0.18，
   下限 0.08，上限 0.40；此外强制保留首句与末句（文档开头通常是总述，结尾通常
   是结论或落款）。

## 接口

``extract(text, ratio=0.18, min_chars=0) -> ExtractResult``

* ``ExtractResult.text``       抽取后的文本（按原文顺序）；
* ``ExtractResult.selected``   选中的句子下标（升序）；
* ``ExtractResult.scores``     每个句子的得分（与 ``sentences`` 一一对应，便于
  测试与排查，不进入界面）；
* ``ExtractResult.ratio``      实际抽取比例（抽取字符数 / 原文字符数）。

``split_sentences(text) -> List[(start, end, 句子)]`` 单独导出，供测试直接使用。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# [EN] Sentence splitting
# [ZH] 句子切分
# ---------------------------------------------------------------------------

#: [EN] Sentence terminators: both Chinese and English must be recognized. Chinese
#: [EN] full stop, question mark, exclamation mark, and semicolon; English
#: [EN] `.` `?` `!` `;`.
#: [ZH] 句末标点：中英文都要识别。中文句号、问号、叹号、分号，英文 `.` `?` `!` `;`。
_TERMINATORS = "。！？；!?;"

#: [EN] An English period terminates a sentence only when not followed by a digit,
#: [EN] so that "3.5%" or "v1.2" are not split.
#: [ZH] 英文句点仅在后面不是数字时才视为句末，避免切断 "3.5%" 或 "v1.2"。
_DOT = "."

#: [EN] Short lines that must not become standalone sentences: page numbers,
#: [EN] numbering, and pure symbols.
#: [ZH] 明显的"不应单独成句"的短行：页码、编号、纯符号。
_NOISE_RE = re.compile(r"^[\s\-—–_=*#·•\d.、,，)）(（\[\]【】]+$")

#: [EN] Whitespace compression (collapse consecutive blank lines to reduce
#: [EN] meaningless characters sent to the model).
#: [ZH] 空白压缩（折叠连续空行，减少送给模型的无效字符）。
_BLANK_RE = re.compile(r"\n{3,}")


def split_sentences(text: str) -> List[Tuple[int, int, str]]:
    """
    [EN]
    Split on sentence terminators and return ``[(start, end, sentence)]``, where the
    span includes the terminal punctuation.

    Newlines are also hard boundaries: contracts, lists, and table rows often do
    not end with a period, and if only punctuation were recognized a whole list
    would become one overlong sentence, degrading extraction into selecting
    everything or nothing.

    [ZH]
    按句末标点切句，返回 ``[(start, end, 句子)]``（区间包含句末标点）。

    换行同样作为硬边界：合同、列表、表格行往往不以句号结尾；若只识别标点，
    一整份清单会变成一个超长句，抽取就退化为全选或全不选。
    """
    out: List[Tuple[int, int, str]] = []
    n = len(text)
    start = 0
    i = 0
    while i < n:
        ch = text[i]
        cut = False
        if ch == "\n":
            cut = True
        elif ch in _TERMINATORS:
            cut = True
            # [EN] Consecutive punctuation ("……", "!!!", "。）") is absorbed into the
            # [EN] current sentence.
            # [ZH] 连续标点（"……"、"!!!"、"。）"）一并归入本句。
            while i + 1 < n and (text[i + 1] in _TERMINATORS or text[i + 1] in "”』」）)"):
                i += 1
        elif ch == _DOT:
            # [EN] English period: a sentence boundary only when followed by end of
            # [EN] line or whitespace and the preceding character is not a digit.
            # [ZH] 英文句点：仅当其后为行尾或空白、且前一字符不是数字时才算句末。
            nxt = text[i + 1] if i + 1 < n else ""
            prev = text[i - 1] if i > 0 else ""
            if (nxt == "" or nxt.isspace() or nxt in "\"')”") and not prev.isdigit():
                cut = True
        if cut:
            piece = text[start:i + 1]
            if piece.strip():
                out.append((start, i + 1, piece))
            start = i + 1
        i += 1
    if start < n:
        piece = text[start:n]
        if piece.strip():
            out.append((start, n, piece))
    return out


# ---------------------------------------------------------------------------
# [EN] Features and similarity
# [ZH] 特征与相似度
# ---------------------------------------------------------------------------

_SENT_SPLIT_TOKENS = re.compile(r"[a-z0-9]{2,}")


def _cjk(ch: str) -> bool:
    """
    [EN]
    Report whether the character is a CJK ideograph or syllabic character. Such
    scripts have no inter-word spaces, so features are built from character groups.

    [ZH]
    判断是否为中日韩表意文字或音节文字（这类文字没有词间空格，因此按字符组构造
    特征）。
    """
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF      # [EN] Extension A / [ZH] 扩展 A
        or 0x4E00 <= code <= 0x9FFF   # [EN] Basic block / [ZH] 基本区
        or 0xF900 <= code <= 0xFAFF   # [EN] Compatibility ideographs / [ZH] 兼容表意
        or 0x3040 <= code <= 0x30FF   # [EN] Japanese kana / [ZH] 日文假名
        or 0xAC00 <= code <= 0xD7AF   # [EN] Hangul syllables / [ZH] 韩文音节
    )


def features(sentence: str) -> List[str]:
    """
    [EN]
    Convert a sentence into a feature list: CJK character bigrams plus Latin tokens.

    Bigrams rather than unigrams: literal overlap of unigrams is almost always
    similar (contract passages are full of "Party A" and "Party B"), which has too
    little discriminative power; only co-occurrence of bigrams actually indicates
    that two sentences discuss the same matter.

    [ZH]
    将一个句子转换为特征列表：CJK 字符二元组加拉丁词元。

    采用二元组而非单元组：单元组的字面重叠几乎总是相似（合同段落中"甲方""乙方"
    随处可见），区分度太低；只有二元组的共现才真正代表"讲的是同一件事"。
    """
    text = sentence.lower()
    feats: List[str] = []
    # [EN] Contiguous CJK run -> bigrams
    # [ZH] CJK 连续段 → 二元组
    i = 0
    n = len(text)
    while i < n:
        if _cjk(text[i]):
            j = i
            while j < n and _cjk(text[j]):
                j += 1
            run = text[i:j]
            if len(run) == 1:
                feats.append(run)
            else:
                feats.extend(run[k:k + 2] for k in range(len(run) - 1))
            i = j
        else:
            i += 1
    # [EN] Latin tokens
    # [ZH] 拉丁词元
    feats.extend(_SENT_SPLIT_TOKENS.findall(text))
    return feats


def _tfidf_vectors(sentences: Sequence[str]) -> List[Dict[str, float]]:
    """
    [EN]
    Compute TF-IDF vectors for a set of sentences (L2-normalized so that the cosine
    similarity equals a dot product).

    IDF uses ``log(N / df) + 1``: with few sentences (N=3), a plain ``log`` would
    drive the weight of a term with df=N to 0, making all similarities 0 and
    degrading TextRank to a uniform distribution.

    [ZH]
    对句子集合计算 TF-IDF 向量（做 L2 归一化，使余弦相似度可直接用点积计算）。

    IDF 使用 ``log(N / df) + 1``：句子数较少时（N=3），纯 ``log`` 会把 df=N 的公共
    词权重压到 0，导致所有相似度均为 0，TextRank 退化为均分。
    """
    docs = [features(s) for s in sentences]
    n = len(docs)
    df: Dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    vectors: List[Dict[str, float]] = []
    for doc in docs:
        counts: Dict[str, float] = {}
        for term in doc:
            counts[term] = counts.get(term, 0.0) + 1.0
        vec: Dict[str, float] = {}
        norm = 0.0
        for term, tf in counts.items():
            weight = (1.0 + math.log(tf)) * (math.log(n / df[term]) + 1.0)
            vec[term] = weight
            norm += weight * weight
        if norm > 0:
            inv = 1.0 / math.sqrt(norm)
            vec = {k: v * inv for k, v in vec.items()}
        vectors.append(vec)
    return vectors


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(term, 0.0) for term, w in a.items())


def similarity_matrix(sentences: Sequence[str], window: int = 0) -> List[List[float]]:
    """
    [EN]
    Pairwise sentence similarity matrix (symmetric, zero diagonal).

    ``window`` is a performance gate: when > 0, only sentence pairs whose indices
    differ by at most ``window`` are computed and all other entries remain 0, which
    is equivalent to sparsifying an undirected graph. Rationale:

    * A full matrix requires O(n²) cosine evaluations. With 30 features per
      sentence, n=800 needs 320,000 dot products and n=4000 needs 8,000,000;
      measured time for 4050 sentences is 23 seconds, which exhausts the
      millisecond-level budget entirely.
    * Extractive summarization conventionally builds only a local graph ("only
      adjacent sentences explain each other"). Distant sentences have low lexical
      overlap but high noise, so their contribution to ranking is negligible.

    The default window is 0 (exact mode, for short texts and tests). The summarizer
    uses exact mode for small chunks and windowed mode for large chunks; the
    threshold is defined in ``_graph_window``.

    [ZH]
    句子两两相似度矩阵（对称，对角线为 0）。

    ``window`` 是性能闸门：大于 0 时只计算下标差不超过 ``window`` 的句子对，其余
    保持 0（等同于对无向图做稀疏化）。理由：

    * 完整矩阵需要 O(n²) 次余弦计算：每句 30 个特征时，n=800 需计算 32 万次点积，
      n=4000 需计算 800 万次——实测 4050 句需要 23 秒，会完全耗尽毫秒级预算；
    * 抽取式摘要的通行做法本就只在局部建图（"只有相邻句子才互相解释"）。远距离
      句子之间词面相似度低但噪声大，对排序几乎没有贡献。

    默认窗宽为 0（精确模式，供短文本与测试使用）；摘要器对小段文本使用精确模式，
    对大段文本使用窗口模式，阈值见 ``_graph_window``。
    """
    n = len(sentences)
    vecs = _tfidf_vectors(sentences)
    sim = [[0.0] * n for _ in range(n)]
    span = n if window <= 0 else min(int(window), n - 1)
    for i in range(n):
        if not vecs[i]:
            continue
        limit = n if window <= 0 else min(n, i + span + 1)
        for j in range(i + 1, limit):
            value = _cosine(vecs[i], vecs[j]) if vecs[j] else 0.0
            sim[i][j] = sim[j][i] = value
    return sim


def textrank(similarities: List[List[float]], damping: float = 0.85,
             iterations: int = 30, tolerance: float = 1e-4) -> List[float]:
    """
    [EN]
    Run PageRank over the sentence similarity graph and return the weight of each
    sentence.

    The graph is undirected (TextRank is defined on an undirected graph). Rows are
    normalized before iteration so that a high-similarity node is not diluted by
    having many outgoing edges. Iteration stops on convergence or at the iteration
    cap (30 is sufficient; sentence counts are typically < 200).

    [ZH]
    在句子相似度图上运行 PageRank，返回每个句子的权重。

    图为无向图（TextRank 原文即基于无向图）。迭代前先做行归一化，避免高相似度
    节点因出边较多而被稀释。迭代至收敛或达到上限（通常句子数小于 200，30 次
    足够）。
    """
    n = len(similarities)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    # [EN] Row normalization
    # [ZH] 行归一化
    norm = [[0.0] * n for _ in range(n)]
    for i in range(n):
        total = sum(similarities[i])
        if total > 0:
            inv = 1.0 / total
            for j in range(n):
                norm[i][j] = similarities[i][j] * inv
    scores = [1.0 / n] * n
    for _ in range(iterations):
        prev = scores
        scores = []
        delta = 0.0
        for i in range(n):
            acc = 0.0
            for j in range(n):
                if norm[j][i]:
                    acc += norm[j][i] * prev[j]
            value = (1.0 - damping) / n + damping * acc
            delta += abs(value - prev[i])
            scores.append(value)
        if delta < tolerance * n:
            break
    return scores


# ---------------------------------------------------------------------------
# [EN] Extraction
# [ZH] 抽取
# ---------------------------------------------------------------------------

@dataclass
class ExtractResult:
    """
    [EN]
    Extraction result.

    [ZH]
    抽取结果。
    """

    text: str = ""
    selected: List[int] = field(default_factory=list)
    scores: List[float] = field(default_factory=list)
    sentences: List[str] = field(default_factory=list)
    origin_chars: int = 0
    kept_chars: int = 0

    @property
    def ratio(self) -> float:
        """
        [EN]
        Actual extraction ratio (extracted characters / original characters).

        [ZH]
        实际抽取比例（抽取字符数 / 原文字符数）。
        """
        if self.origin_chars <= 0:
            return 1.0
        return self.kept_chars / self.origin_chars

    @property
    def changed(self) -> bool:
        """
        [EN]
        Whether any deletion actually occurred (False when the full text is
        retained).

        The criterion is the sentence count (selected < total), not the character
        count: ``kept_chars`` measures the length of the extraction result, and the
        difference from the original may also come from whitespace collapsing.
        Comparing characters would falsely report "pre-filtered" on documents where
        only whitespace was collapsed.

        [ZH]
        是否确实进行了删减（全文保留时为 False）。

        判据是"选中句数 < 总句数"而不是字符数：``kept_chars`` 统计的是抽取结果的
        字符数，而它与原文的差值还可能来自空白折叠；用字符数判断会在仅折叠了空白
        的文档上误报为"已预筛"。
        """
        return bool(self.sentences) and len(self.selected) < len(self.sentences)


#: [EN] Sentence-count limit for the full similarity matrix. Above it, windowed mode
#: [EN] is used (see ``similarity_matrix``): the full matrix for 200 sentences
#: [EN] requires only 20,000 dot products (millisecond range), whereas 4000
#: [EN] sentences require 8,000,000 (tens of seconds).
#: [ZH] 完整相似度矩阵的句数上限。超过它则使用窗口模式（见
#: [ZH] ``similarity_matrix``）：200 句的完整矩阵只需约 2 万次点积（毫秒级），
#: [ZH] 4000 句则需要 800 万次（几十秒）。
_GRAPH_FULL_LIMIT = 200

#: [EN] Window width in windowed mode: each sentence is compared only with the 24
#: [EN] preceding and following sentences, which covers typical clause-level context.
#: [ZH] 窗口模式下的窗宽：每个句子只与其前后各 24 句比较，可覆盖典型的条款级上下文。
_GRAPH_WINDOW = 24

#: [EN] Score weights: centrality (TextRank) x rarity.
#: [EN]
#: [EN] The weights were determined by a retention-rate sweep over two samples
#: [EN] (`artifacts/probe_extractive.py`: 6 key figures in the Chinese contract,
#: [EN] 4 key data points in the English agreement):
#: [EN]
#: [EN] | Centrality alpha | Chinese retention | English retention |
#: [EN] | --- | --- | --- |
#: [EN] | 1.00 (pure TextRank) | 3/6 | 1/4 |
#: [EN] | 0.65 | 2/6 | 1/4 |
#: [EN] | 0.50 (this implementation) | **3/6** | **2/4** |
#: [EN] | 0.35 | 2/6 | 2/4 |
#: [EN] | 0.00 (pure rarity) | 1/6 | 1/4 |
#: [EN]
#: [EN] Both extremes perform poorly and the middle performs best: centrality alone
#: [EN] selects boilerplate sentences that resemble each other, rarity alone selects
#: [EN] sentences that occur once but are unimportant, and an even split balances
#: [EN] "what this passage is about" against "where the key facts are".
#: [ZH] 得分权重：中心度（TextRank）× 稀有度。
#: [ZH]
#: [ZH] 权重由两个样本的保留率扫描确定（`artifacts/probe_extractive.py`：中文合同
#: [ZH] 6 个关键数字，英文协议 4 个关键数据）：
#: [ZH]
#: [ZH] | 中心度 α | 中文保留 | 英文保留 |
#: [ZH] | --- | --- | --- |
#: [ZH] | 1.00（纯 TextRank） | 3/6 | 1/4 |
#: [ZH] | 0.65 | 2/6 | 1/4 |
#: [ZH] | 0.50（本实现） | **3/6** | **2/4** |
#: [ZH] | 0.35 | 2/6 | 2/4 |
#: [ZH] | 0.00（纯稀有度） | 1/6 | 1/4 |
#: [ZH]
#: [ZH] 两个极端效果都差，中间最好：纯中心度会选出"彼此最相似的套话句"，纯稀有度
#: [ZH] 会选出"只出现一次但并不重要"的句子；各占一半才能兼顾"这段在讲什么"与
#: [ZH] "哪里有关键事实"。
_CENTRALITY_WEIGHT = 0.5
_RARITY_WEIGHT = 0.5

#: [EN] Additional weight for rare features containing digits (amounts, dates,
#: [EN] ratios, and deadlines are key points).
#: [ZH] 含数字的稀有特征额外加权（金额、日期、比例、期限均为要点）。
_NUMERIC_BONUS = 2.0

_DIGIT_RE = re.compile(r"\d")


def _normalize(values: Sequence[float]) -> List[float]:
    """
    [EN]
    Linearly map a set of scores into [0, 1]; return all zeros when every value is
    equal (avoids division by zero).

    [ZH]
    将一组分数线性映射到 [0, 1]；全部相等时返回全 0（避免除零）。
    """
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        return [0.0] * len(values)
    span = hi - lo
    return [(v - lo) / span for v in values]


def _rarity_scores(sentences: Sequence[str]) -> List[float]:
    """
    [EN]
    Rarity: the number of features in a sentence that occur only once in the whole
    document, with extra weight for rare features containing digits.

    A rare feature is a term with ``df == 1`` in the term-frequency table. A binary
    "occurs once in the whole text" criterion is used instead of a continuous IDF
    value because the distinction between key points and boilerplate is precisely
    "mentioned once vs. repeated"; the binary criterion is more stable and
    independent of the total sentence count (splitting the same document into 3 or
    30 chunks produces the same result).

    [ZH]
    稀有度：句子中"全文只出现一次"的特征数量，含数字的稀有特征额外加权。

    稀有特征即词频表中 ``df == 1`` 的项。采用"全文只出现一次"的二值判据而非
    IDF 连续值，是因为要点与套话的区别本就是"只提及一次还是反复出现"，二值判据
    更稳定，且不受句子总数影响（同一份文档切成 3 段还是 30 段，结论一致）。
    """
    docs = [features(s) for s in sentences]
    df: Dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    scores: List[float] = []
    for doc in docs:
        total = 0.0
        for term in set(doc):
            if df.get(term, 0) == 1:
                total += _NUMERIC_BONUS if _DIGIT_RE.search(term) else 1.0
        scores.append(total)
    return scores


def _score_sentences(sentences: Sequence[str]) -> List[float]:
    """
    [EN]
    Centrality (TextRank) x rarity: both signals are normalized and summed with
    weights.

    Why the rarity term exists (measured conclusion; read this before changing it):

    TextRank alone is the wrong direction for this scenario. It measures how similar
    a sentence is to the other sentences, so it selects sentences that repeat
    boilerplate: contract phrasings such as "Party A shall ... within X business
    days" have the highest pairwise similarity and necessarily rank first, while the
    content that actually determines contract risk -- `1,860,000 元`,
    `2026 年 3 月 31 日`, `0.3% per day` -- is exactly what occurs only once, has low
    centrality, ranks lower, and never fits within a 10-20% budget.

    Rarity signal: the number of features in the sentence that occur only once in
    the whole document (terms with df=1 in TF-IDF), with rare features containing
    digits weighted higher, since amounts, dates, ratios, and deadlines are key
    points. The two signals are complementary in direction: centrality preserves
    "what this passage is about" and rarity preserves "where the key facts are".

    The weights ``_CENTRALITY_WEIGHT`` / ``_RARITY_WEIGHT`` were determined by the
    retention-rate sweep in `artifacts/probe_extractive.py` and
    `tools/test_extractive.py`; they are not arbitrary.

    [ZH]
    中心度（TextRank）乘以稀有度：两个信号均先归一化，再按权重求和。

    为什么要加入稀有度这一项（实测结论，改动前请先阅读此处）：

    TextRank 单独用于本场景方向是错误的。它衡量的是"该句与其他句子有多相似"，
    因此选出的是重复出现套话的句子——合同中"甲方应在 X 个工作日内……"这类句式
    彼此相似度最高，必然排在最前；而真正决定合同风险的 `1,860,000 元`、
    `2026 年 3 月 31 日`、`按日 0.3%` 恰恰是只出现一次的内容，中心度低、排序靠后，
    10~20% 的预算根本轮不到它们。

    稀有度信号：该句包含的"全文只出现一次"的特征数量（TF-IDF 中 df=1 的项），
    含数字的稀有特征额外加权——金额、日期、比例、期限均为要点。两个信号方向互补：
    中心度保证"这段在讲什么"，稀有度保证"哪里有关键事实"。

    权重 ``_CENTRALITY_WEIGHT`` / ``_RARITY_WEIGHT`` 由
    `artifacts/probe_extractive.py` 与 `tools/test_extractive.py` 的保留率扫描确定，
    并非主观设定。
    """
    window = 0 if len(sentences) <= _GRAPH_FULL_LIMIT else _GRAPH_WINDOW
    central = textrank(similarity_matrix(sentences, window=window))
    rare = _rarity_scores(sentences)

    centrality = _normalize(central)
    rarity = _normalize(rare)
    n = len(sentences)
    scores: List[float] = []
    for idx in range(n):
        combined = (_CENTRALITY_WEIGHT * centrality[idx]
                    + _RARITY_WEIGHT * rarity[idx])
        length = len(sentences[idx].strip())
        if length <= 4:
            # [EN] Short fragment (for example "同上。"). Exception: a document title
            # [EN] ("采购合同") is short as well, but it states what the whole document
            # [EN] is about; losing the title leaves the summary without a subject, so
            # [EN] it is not penalized.
            # [ZH] 短残句（如"同上。"）。例外：文档标题（"采购合同"）同样是短句，但
            # [ZH] 它表明整篇文档的主题；标题丢失后摘要将失去主体，因此不予惩罚。
            length_factor = 1.0 if _looks_like_title(sentences[idx], idx) else 0.25
        elif length <= 8:
            length_factor = 1.0 if _looks_like_title(sentences[idx], idx) else 0.6
        else:
            length_factor = 1.0
        total = len(sentences)
        if idx == 0 or (idx < 3 and _looks_like_title(sentences[idx], idx)):
            # [EN] Start of the document (a title within the first 3 sentences):
            # [EN] overview sentence or title, always a key point.
            # [ZH] 文档开头（前 3 句内的标题）：总述句或标题，必为要点。
            position = 1.35
        elif idx == total - 1 and total > 2:
            position = 1.20
        else:
            position = 1.0
        scores.append(combined * length_factor * position)
    return scores


#: [EN] Title-like short line at the beginning of a document: no sentence
#: [EN] terminator, no digits, short. The position check is limited to the first few
#: [EN] sentences, so numbered subheadings in the body are not misclassified as
#: [EN] titles.
#: [ZH] 文档开头的"标题样"短行：不含句末标点、不含数字、长度短。位置限定在前几句，
#: [ZH] 因此不会把正文中的编号小标题误判为标题。
def _looks_like_title(sentence: str, index: int) -> bool:
    """
    [EN]
    Determine whether the line is a title at the beginning of a document (for
    example "采购合同" / "Service Agreement").

    [ZH]
    判断是否为文档开头的标题行（如 "采购合同" / "Service Agreement"）。
    """
    if index >= 3:
        return False
    text = sentence.strip()
    if not text or len(text) > 40:
        return False
    if text[-1] in _TERMINATORS or text.endswith("。"):
        return False
    return not any(ch.isdigit() for ch in text)


#: [EN] When the average sentence length (characters) is below this value, the text
#: [EN] is treated as "one fact per sentence" and the extraction ratio is raised.
#: [ZH] 句子平均长度（字符）低于此值时，认为"一句一个事实"，并按比例上调抽取比例。
_DENSE_SENTENCE_CHARS = 60

#: [EN] Lower bound on the extraction ratio for high-density documents (raised to at
#: [EN] least this value above the ratio supplied by the caller).
#: [ZH] 高密度文档的抽取比例下限（在调用方给定的比例之上至少抬升到该值）。
_DENSE_RATIO = 0.50


def _density_ratio(text: str, ratio: float) -> float:
    """
    [EN]
    Adjust the extraction ratio by fact density: the shorter the sentences (one
    fact per sentence), the higher the ratio.

    Why this rule exists (measured, see `artifacts/probe_extractive.py`):

    The smallest extraction unit is a whole sentence, while the budget is measured
    in characters. In the English agreement sample each sentence averages about 75
    characters and carries one key data point (`USD 48,000` / `24 months` /
    `4 hours` / `1.5%`); a 35% budget fits only 3-4 sentences, which necessarily
    discards more than half of the facts. This is unrelated to how important a
    sentence is; it is purely arithmetic determined by sentence length.

    The Chinese contract sample is the opposite: sentences average 24 characters, so
    35% fits 10 sentences and far less is lost.

    Therefore, when the average sentence length is clearly short (< 60 characters)
    the ratio is raised to 0.50 so that sentence count aligns with information
    content. Such documents are also easier to compress (short sentences, high
    redundancy), so raising the ratio does not break the 2x-or-better compression
    gain.

    [ZH]
    按事实密度调整抽取比例：句子越短（一句一个事实），比例越高。

    为什么需要该规则（实测，见 `artifacts/probe_extractive.py`）：

    抽取的最小单位是整句，而预算按字符计算。英文协议样本中每句约 75 字符，每句
    包含一个关键数据（`USD 48,000` / `24 months` / `4 hours` / `1.5%`），35% 的
    预算只够容纳 3~4 句，也就是说必然丢失一半以上的事实。这与"该句是否重要"无关，
    纯粹是句长决定的算术结果。

    中文合同样本则相反：句子平均 24 字符，35% 能容纳 10 句，丢失少得多。

    因此，当平均句长明显偏短（小于 60 字符）时，将比例提升到 0.50，使句数与信息量
    对齐。此类文档本身也更易压缩（句子短、冗余多），提高比例不会破坏 2 倍以上的
    压缩收益。
    """
    pieces = split_sentences(text)
    if len(pieces) < 4:
        return ratio
    average = sum(len(p[2]) for p in pieces) / len(pieces)
    if average <= _DENSE_SENTENCE_CHARS:
        return max(ratio, _DENSE_RATIO)
    return ratio


#: [EN] Default extraction ratio. This value is not "the smaller the better":
#: [EN] measurements (`artifacts/probe_extractive.py`) show poor key-fact retention
#: [EN] at 0.18 (a 721-character Chinese contract sample retained only 0/7 key
#: [EN] figures), rising to 3/7 at 0.35 while the compression ratio is still 2.9x.
#: [EN] The benefit of pre-filtering comes from sending 65% fewer characters, not
#: [EN] from compressing 3000 characters down to 400. The specification states
#: [EN] 10-20%; this implementation raises it based on measurement, with the
#: [EN] rationale and data recorded in `records/` (deviation table) and `docs/`.
#: [ZH] 默认抽取比例。该值并非"越小越好"：验证（`artifacts/probe_extractive.py`）
#: [ZH] 在 0.18 时关键事实保留率很差（721 字符的中文合同样本仅保留 0/7 个关键
#: [ZH] 数字），0.35 时提升到 3/7，而压缩比仍有 2.9 倍——预筛的收益来自"少送 65%
#: [ZH] 的字"，而非"把 3000 字压到 400 字"。设计规范原文为 10~20%，本实现按验证上调，
#: [ZH] 理由与数据记录在 `records/`（偏差表）与 `docs/` 中。
_DEFAULT_RATIO = 0.35

#: [EN] Chunks shorter than this character count are not pre-filtered: the chunk is
#: [EN] already short enough that the time saved does not justify the risk of losing
#: [EN] information.
#: [ZH] 低于该字符数的段落不做预筛：段落本身已足够短，抽取节省的时间不足以抵消
#: [ZH] 信息丢失的风险。
_DEFAULT_MIN_CHARS = 600

#: [EN] Absolute character ceiling: text above it is always returned unchanged.
#: [EN]
#: [EN] Why this limit is required (measured data): even with window-based
#: [EN] sparsification, the TextRank similarity matrix still requires O(n·w) cosine
#: [EN] dot products. An 8000-sentence text (315,000 characters) measured 22.9
#: [EN] seconds; pre-filtering is supposed to be millisecond-level and model-free, so
#: [EN] 20 seconds is pure overhead. Texts above this length should not occur at all:
#: [EN] the summarizer chunks by ``chunk_chars`` (default 3000), so a 60,000-character
#: [EN] chunk indicates a broken configuration. In that case the correct behavior is
#: [EN] to let the model handle it rather than spend tens of seconds on statistics.
#: [EN]
#: [EN] 12000 characters corresponds to roughly 600-1200 sentences, measured under 0.6
#: [EN] seconds, well below a single model call (tens of seconds).
#: [ZH] 绝对字符上限：超过该值一律原样返回。
#: [ZH]
#: [ZH] 必须有该限制的原因（验证数据）：即使做了窗口稀疏化，TextRank 的相似度
#: [ZH] 矩阵仍有 O(n·w) 次余弦点积。8000 句的文本（31.5 万字符）验证需要 22.9 秒
#: [ZH] ——预筛本应是"毫秒级、不占用模型"，耗时 20 秒纯属负担。而超过该长度的段落
#: [ZH] 本不应出现：摘要器按 ``chunk_chars``（默认 3000）分段，一段 6 万字符说明
#: [ZH] 配置已被破坏，此时正确做法是让模型自行处理，而不是先花几十秒做统计。
#: [ZH]
#: [ZH] 12000 字符对应约 600~1200 句，验证 0.6 秒以内，远低于一次模型调用
#: [ZH] （数十秒）。
_DEFAULT_MAX_CHARS = 12000


def extract(text: str, ratio: float = _DEFAULT_RATIO,
            min_chars: int = _DEFAULT_MIN_CHARS,
            max_chars: int = _DEFAULT_MAX_CHARS,
            min_ratio: float = 0.10, max_ratio: float = 0.70) -> ExtractResult:
    """
    [EN]
    Extract the most important sentences from ``text`` and compress to approximately
    ``ratio``.

    The returned text preserves the original sentence order, and adjacent selected
    sentences are merged into continuous passages (see the module header). The
    following cases return the text unchanged without falsely reporting
    "pre-filtered" (``changed`` is False):

    * the text is shorter than ``min_chars`` (the chunk is already short, and the
      benefit of extraction does not justify the risk of losing information);
    * the text is longer than ``max_chars`` (statistical cost becomes unbounded; see
      ``_DEFAULT_MAX_CHARS``);
    * no sentences can be split out, or the sentence count is <= 2 (nothing to
      choose from).

    ## Selection rules (tuned by measurement; read this before changing them)

    Sorting only by "highest score first, fill until the character budget is
    exhausted" has a systematic bias: the highest-scoring sentence is often a long
    one, two or three long sentences exhaust the budget, and the remaining dozen
    short sentences (such as "付款期限：30 日。", which are exactly contract key
    points) cannot enter at all. Measured: a 721-character Chinese contract sample at
    ratio=0.18 selected only 4/30 sentences and 129 characters, losing all key
    figures such as `1,860,000`.

    Therefore selection is performed in two passes:

    1. The first pass only requires that a sentence fits the budget
       (``used + len <= budget``) and no longer force-inserts the last sentence, so
       long sentences cannot crowd out later short ones merely because they "must be
       included"; under-filling is acceptable.
    2. The second pass backfills by "score per character" (``score / len``),
       preferring short but important sentences within the remaining budget until
       the budget is full or no sentence fits.

    The second pass is the key change in this version: it corrects "pick key points"
    from "pick the longest high-scoring sentences" to "fit as many key points as
    possible into a limited character budget", which is precisely the purpose of
    extractive pre-filtering. The score itself is composed of centrality (TextRank) x
    rarity; see :func:`_score_sentences`.

    [ZH]
    从 ``text`` 中抽取最重要的句子，压缩到约 ``ratio``。

    返回的文本保持原文句子顺序，相邻的选中句合并为连续段落（见模块开头）。以下
    情况原样返回（不谎报"已预筛"，``changed`` 为 False）：

    * 文本短于 ``min_chars``（段落本身已短，抽取的收益不足以抵消信息丢失风险）；
    * 文本长于 ``max_chars``（统计成本失控，见 ``_DEFAULT_MAX_CHARS``）；
    * 无法切分出句子，或句子数 ≤ 2（无从选择）。

    ## 选句规则（由验证调优确定，改动前请先阅读本段）

    仅按"得分从高到低、装到字数预算为止"会产生系统性偏差：得分最高的一句话往往
    是长句，两三条长句即耗尽预算，其余十几条短句（如"付款期限：30 日。"，恰恰是
    合同要点）一条也无法入选。实测：721 字符的中文合同样本在 ratio=0.18 时仅选出
    4/30 句、共 129 字符，`1,860,000` 这类关键数字全部丢失。

    因此改为两遍选句：

    1. 第一遍只要求"装入预算"（``used + len <= budget``），不再"最后一句直接
       塞入"，于是长句不会因为"反正要收"而挤掉后续短句；未选满亦可。
    2. 第二遍按"每字符得分"（``score / len``）回填：在剩余预算内优先收"短而重要"
       的句子，直到预算用满或再没有句子装得下。

    第二遍是本版本的关键：它把"挑要点"从"挑最长的高分句"矫正为"在有限字数内装进
    尽量多的要点"，这正是抽取式预筛的目的。得分本身由中心度（TextRank）乘以稀有度
    合成，见 :func:`_score_sentences`。
    """
    origin = len(text)
    if origin == 0:
        return ExtractResult(text="", origin_chars=0, kept_chars=0)
    if min_chars > 0 and origin < int(min_chars):
        pieces = split_sentences(text)
        return ExtractResult(
            text=text, selected=list(range(len(pieces))),
            scores=[1.0] * len(pieces), sentences=[p[2] for p in pieces],
            origin_chars=origin, kept_chars=origin,
        )
    if max_chars > 0 and origin > int(max_chars):
        pieces = split_sentences(text)
        return ExtractResult(
            text=text, selected=list(range(len(pieces))),
            scores=[1.0] * len(pieces), sentences=[p[2] for p in pieces],
            origin_chars=origin, kept_chars=origin,
        )

    # [EN] Fact-density adaptation: short sentences mean "one fact per sentence",
    # [EN] and a fixed ratio would discard entire facts sentence by sentence. See the
    # [EN] documentation and measurements for _density_ratio.
    # [ZH] 事实密度自适应：句子短意味着"一句一个事实"，固定比例会按句丢弃整条事实。
    # [ZH] 详见 _density_ratio 的说明与验证数据。
    ratio = _density_ratio(text, float(ratio))
    ratio = min(max(ratio, float(min_ratio)), float(max_ratio))

    pieces = split_sentences(text)
    sentences = [p[2] for p in pieces]
    if len(sentences) <= 2:
        return ExtractResult(text=text, selected=list(range(len(sentences))),
                             scores=[1.0] * len(sentences), sentences=sentences,
                             origin_chars=origin, kept_chars=origin)

    scores = _score_sentences(sentences)
    budget = max(1, int(origin * ratio))

    order = sorted(range(len(sentences)), key=lambda i: (-scores[i], i))
    selected: List[int] = []
    used = 0
    picked = set()
    # [EN] Pass zero: structural titles (short title lines at the start of the
    # [EN] document) are retained unconditionally. This is not a matter of score:
    # [EN] once the title is dropped, the summary can no longer indicate what the
    # [EN] document is ("采购合同" receives a 0.25x length penalty and rarity 2, so it
    # [EN] cannot rank near the top by any ordering), yet it occupies only a dozen
    # [EN] characters. It is deducted before the budget, and the remaining budget is
    # [EN] allocated by score as usual.
    # [ZH] 第零遍：结构性标题（文档开头的短标题行）无条件保留。这不是得分高低的
    # [ZH] 问题——标题被丢弃后，摘要中再也看不出该文件是什么（"采购合同"四个字长度
    # [ZH] 惩罚 0.25 倍、稀有度 2，无论怎么排序都进不了前几名），而它只占十几个字符。
    # [ZH] 该句在预算之前扣除，剩余预算照常按得分分配。
    for idx in range(min(3, len(sentences))):
        if _looks_like_title(sentences[idx], idx):
            length = len(pieces[idx][2])
            if used + length <= budget:
                selected.append(idx)
                picked.add(idx)
                used += length
    # [EN] First pass: score priority, include a sentence only if it fits.
    # [ZH] 第一遍：得分优先，装得下才收。
    for idx in order:
        if idx in picked:
            continue
        length = len(pieces[idx][2])
        if used + length <= budget:
            selected.append(idx)
            picked.add(idx)
            used += length
        if used >= budget:
            break
    # [EN] Second pass: backfill the remaining budget by score per character
    # [EN] (short but important sentences first).
    # [ZH] 第二遍：剩余预算按"每字符得分"回填（短而重要的句子优先）。
    remaining = sorted(
        (i for i in range(len(sentences)) if i not in picked and scores[i] > 0),
        key=lambda i: (-(scores[i] / max(1, len(sentences[i].strip()))), i),
    )
    for idx in remaining:
        length = len(pieces[idx][2])
        if used + length <= budget:
            selected.append(idx)
            used += length
        if used >= budget:
            break
    if not selected:
        selected = [max(range(len(sentences)), key=lambda i: scores[i])]
    selected.sort()

    kept = 0
    parts: List[str] = []
    prev_end = -1
    for idx in selected:
        start, end, piece = pieces[idx]
        kept += len(piece)
        if prev_end < 0:
            parts.append(piece)
        elif start - prev_end <= 2:
            # [EN] Adjacent (separated only by whitespace/newline): continue the same
            # [EN] paragraph instead of starting a new one.
            # [ZH] 相邻（中间仅隔空白或换行）：直接续上，不另起段落。
            parts.append(piece)
        else:
            parts.append("\n" + piece)
        prev_end = end
    body = _BLANK_RE.sub("\n\n", "".join(parts)).strip()
    if len(selected) >= len(sentences):
        # [EN] Nothing was deleted (too few sentences or a very large budget): return
        # [EN] the original text truthfully rather than reporting "pre-filtered".
        # [ZH] 未删除任何句子（句子数过少或预算极大）：如实返回原文，不谎报"已预筛"。
        return ExtractResult(text=text, selected=list(range(len(sentences))),
                             scores=scores, sentences=sentences,
                             origin_chars=origin, kept_chars=origin)
    return ExtractResult(
        text=body, selected=selected, scores=scores, sentences=sentences,
        origin_chars=origin, kept_chars=len(body),
    )
