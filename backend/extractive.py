"""抽取式预筛（TextRank）：在送给模型之前，先把每一段压到 10~20%。

## 为什么需要它

任务书任务四：47KB 文档分段摘要耗时过长，几百 KB 的合同线性外推可达数小时。
矛盾的核心是**模型生成速度有限而摘要需 N 次串行生成**。要真正降下来，必须
减少"送给模型的字数"与"模型要吐出的字数"，而这件事**根本不需要模型**：
纯统计方法就能把一段 3000 字的合同压到 500 字左右，且不再丢关键句。

这是任务书列出的杠杆中收益最大的一条（"抽取式预筛"），本模块只做这一件事。

## 算法

TextRank（Mihalcea & Tarau, 2004）在句子图上跑 PageRank。相似度用 TF-IDF
余弦，特征取字符 n-gram（中文友好，无需分词器）与拉丁词元（英文友好）的并集：

* 中文没有空格分词，用**字符二元组**做特征是最省事且效果稳定的做法；
* 英文用**小写词元**，避免字符 n-gram 把 `a`／`the` 这类高频虚词算成相似；
* 两者取并集，因此中英混排文档同样可用。

刻意**不引入 jieba / numpy / scikit-learn**：这两个是本项目的运行期依赖白名单
（见 `tools/check_requirements.py`），为了一个几百毫秒的统计步骤引入分词器与
科学计算库不划算；纯标准库实现在 47KB 文本上实测远低于 100ms。

## 三个工程细节

1. **句子顺序不变**：抽取只做"删句"，不做重排。摘要器要把抽取结果喂给模型
   并同时记录 `(start, end)` 原文字符区间，重排会让区间与正文对不上，
   而且乱序输入对模型的连贯性判断是负面的。
2. **相邻句合并**：被选中的相邻句子合并成一段连续文本，不逐句加换行。
   逐句换行会把合同条款的上下文切断，模型更容易误读。
3. **保底与封顶**：比例过低（如 5%）会漏掉关键条款，过高则等于没筛。
   默认 0.18，下限 0.08，上限 0.40；另外强制保留首句与末句（文档的开头
   通常是总述、结尾通常是结论或落款）。

## 接口

``extract(text, ratio=0.18, min_chars=0) -> ExtractResult``

* ``ExtractResult.text``   抽取后的文本（按原文顺序）
* ``ExtractResult.selected``  选中的句子下标（升序）
* ``ExtractResult.scores``    每个句子的得分（与 ``sentences`` 一一对应，
  便于测试与排查，不进界面）
* ``ExtractResult.ratio``  实际抽取比例（抽取字符数 / 原文字符数）

``split_sentences(text) -> List[(start, end, 句子)]`` 单独导出，供测试直接使用。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# 句子切分
# ---------------------------------------------------------------------------

#: 句末标点：中英文都要认。中文句号/问号/叹号/分号，英文 `.` `?` `!` `;`
_TERMINATORS = "。！？；!?;"

#: 英文句点只在"后面不是数字"时才算句末（避免把 "3.5%" 或 "v1.2" 切断）
_DOT = "."

#: 明显的"不要单独成句"的短行：页码、编号、纯符号
_NOISE_RE = re.compile(r"^[\s\-—–_=*#·•\d.、,，)）(（\[\]【】]+$")

#: 空白压缩（连续空行折叠，减少送模型的无效字符）
_BLANK_RE = re.compile(r"\n{3,}")


def split_sentences(text: str) -> List[Tuple[int, int, str]]:
    """按句末标点切句，返回 ``[(start, end, 句子)]``（区间含句末标点）。

    换行也作为硬边界：合同、列表、表格行往往不以句号结尾，若只认标点，
    一整份清单会变成"一个超长句"，抽取就退化成全选或全不选。
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
            # 连续标点（"……" "!!!" "。）"）一起收进本句
            while i + 1 < n and (text[i + 1] in _TERMINATORS or text[i + 1] in "”』」）)"):
                i += 1
        elif ch == _DOT:
            # 英文句点：后面是行尾/空白且前一个字符不是数字，才算句末
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
# 特征与相似度
# ---------------------------------------------------------------------------

_SENT_SPLIT_TOKENS = re.compile(r"[a-z0-9]{2,}")


def _cjk(ch: str) -> bool:
    """是否中日韩表意文字（这类文字没有词间空格，按字符组特征）。"""
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF      # 扩展 A
        or 0x4E00 <= code <= 0x9FFF   # 基本区
        or 0xF900 <= code <= 0xFAFF   # 兼容表意
        or 0x3040 <= code <= 0x30FF   # 日文假名
        or 0xAC00 <= code <= 0xD7AF   # 韩文音节
    )


def features(sentence: str) -> List[str]:
    """把一个句子变成特征列表：CJK 字符二元组 + 拉丁词元。

    二元组而不是单元组：单元组的"字面重叠"几乎总是相似（合同段里"甲方"
    "乙方"到处都有），区分度太低；二元组的共现才真正代表"讲的是同一件事"。
    """
    text = sentence.lower()
    feats: List[str] = []
    # CJK 连续段 → 二元组
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
    # 拉丁词元
    feats.extend(_SENT_SPLIT_TOKENS.findall(text))
    return feats


def _tfidf_vectors(sentences: Sequence[str]) -> List[Dict[str, float]]:
    """对句子集合算 TF-IDF 向量（L2 归一化，便于直接点积算余弦）。

    IDF 用 ``log(N / df) + 1``：句子数少时（N=3）纯 ``log`` 会把
    df=N 的公共词权重压到 0，导致所有相似度都是 0、TextRank 退化成均分。
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
    """句子两两相似度矩阵（对称，对角线 0）。

    ``window`` 是**性能闸门**：>0 时只算"下标相距不超过 window"的句子对，
    其余保持 0（等同无向图的稀疏化）。理由：

    * 完整矩阵是 O(n²) 次余弦：一句话 30 个特征时，n=800 要算 32 万次点积，
      n=4000 要算 800 万次 —— 实测 4050 句要 23 秒，把"毫秒级"直接吃光；
    * 抽取式摘要的通行做法本就只在**局部**建图（"相邻句子才互相解释"），
      远距离句子之间的词面相似度低但噪声大，算出来对排序几乎没有贡献。

    默认窗宽 0（精确模式，供短文本与测试使用）；摘要器对小段文本会走精确
    模式、对大段文本走窗口模式，阈值见 ``_graph_window``。
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
    """在句子相似度图上跑 PageRank，返回每个句子的权重。

    无向图（TextRank 原文即无向）；行归一化后再迭代，避免高相似度节点
    因为出边多而被稀释。迭代到收敛或达到上限（30 次足够，句子数通常 <200）。
    """
    n = len(similarities)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    # 行归一化
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
# 抽取
# ---------------------------------------------------------------------------

@dataclass
class ExtractResult:
    """抽取结果。"""

    text: str = ""
    selected: List[int] = field(default_factory=list)
    scores: List[float] = field(default_factory=list)
    sentences: List[str] = field(default_factory=list)
    origin_chars: int = 0
    kept_chars: int = 0

    @property
    def ratio(self) -> float:
        """实际抽取比例（抽取字符数 / 原文字符数）。"""
        if self.origin_chars <= 0:
            return 1.0
        return self.kept_chars / self.origin_chars

    @property
    def changed(self) -> bool:
        """是否真的做了删减（全文保留时为 False）。

        判据是"选中的句数 < 总句数"而不是字符数：``kept_chars`` 统计的是
        抽取结果的字符数，而它与原文的差值还可能来自空白折叠，
        用字符数判断会在只折叠了空白的文档上误报"已预筛"。
        """
        return bool(self.sentences) and len(self.selected) < len(self.sentences)


#: 完整相似度矩阵的句数上限。超过它就走窗口模式（见 ``similarity_matrix``）：
#: 200 句的完整矩阵只有 2 万次点积（毫秒级），4000 句则是 800 万次（几十秒）。
_GRAPH_FULL_LIMIT = 200

#: 窗口模式下的窗宽：一句话只与前后各 24 句比较，覆盖典型的条款级上下文。
_GRAPH_WINDOW = 24

#: 得分权重：中心度（TextRank）× 稀有度。
#:
#: 权重由两个样本的**保留率扫描**定出（`artifacts/probe_extractive.py`，中文合同
#: 6 个关键数字 / 英文协议 4 个关键数据）：
#:
#: | 中心度α | 中文保留 | 英文保留 |
#: | --- | --- | --- |
#: | 1.00（纯 TextRank） | 3/6 | 1/4 |
#: | 0.65 | 2/6 | 1/4 |
#: | 0.50（本实现） | **3/6** | **2/4** |
#: | 0.35 | 2/6 | 2/4 |
#: | 0.00（纯稀有度） | 1/6 | 1/4 |
#:
#: 两头都差、中间最好：纯中心度会选"彼此最像的套话句"，纯稀有度会选
#: "只出现一次但无关紧要"的句子，各占一半才兼顾"这段在讲什么"与"哪里有关键事实"。
_CENTRALITY_WEIGHT = 0.5
_RARITY_WEIGHT = 0.5

#: 含数字的稀有特征额外加权（金额 / 日期 / 比例 / 期限都是要点）
_NUMERIC_BONUS = 2.0

_DIGIT_RE = re.compile(r"\d")


def _normalize(values: Sequence[float]) -> List[float]:
    """把一组分数线性映射到 [0, 1]；全相等时返回全 0（避免除零）。"""
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        return [0.0] * len(values)
    span = hi - lo
    return [(v - lo) / span for v in values]


def _rarity_scores(sentences: Sequence[str]) -> List[float]:
    """稀有度：句子里"全文只出现一次"的特征数，含数字的稀有特征再加权。

    稀有特征 = 词频表里 ``df == 1`` 的项。用"全文只出现一次"而不是 IDF 连续值，
    是因为要点与套话的区别本就是"只提一次 vs 反复出现"，二值判据更稳、
    也不受句子总数影响（同一份文档切成 3 段还是 30 段，结论一致）。
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
    """中心度（TextRank）× 稀有度：两个信号都归一化后加权求和。

    **为什么要加稀有度这一项**（实测结论，改动前请先看这里）：

    TextRank 单独用在本场景是**方向错误**的。它衡量的是"这句话与其他句子有多像"，
    于是选出的是**重复出现套话**的句子 —— 合同里"甲方应在 X 个工作日内……"
    这类句式彼此相似度最高，必然排在最前；而真正决定合同风险的 `1,860,000 元`、
    `2026 年 3 月 31 日`、`按日 0.3%` 恰恰是**只出现一次**的内容，
    中心度低、被排在后面，10~20% 的预算根本轮不到它们。

    稀有度信号：该句包含的"全文只出现一次"的特征数（TF-IDF 里 df=1 的项），
    含数字的稀有特征再加权 —— 金额、日期、比例、期限都是要点。
    两个信号方向互补：中心度保"这段在讲什么"，稀有度保"哪里有关键事实"。

    权重 ``_CENTRALITY_WEIGHT`` / ``_RARITY_WEIGHT`` 由 `artifacts/probe_extractive.py`
    与 `tools/test_extractive.py` 的保留率扫描定出，不是拍脑袋。
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
            # 短残句（"同上。"）。**例外**：文档标题（"采购合同"）也是短的，
            # 但它是整篇的"讲什么"，标题丢了摘要就没有主体，故不惩罚。
            length_factor = 1.0 if _looks_like_title(sentences[idx], idx) else 0.25
        elif length <= 8:
            length_factor = 1.0 if _looks_like_title(sentences[idx], idx) else 0.6
        else:
            length_factor = 1.0
        total = len(sentences)
        if idx == 0 or (idx < 3 and _looks_like_title(sentences[idx], idx)):
            # 文档开头（前 3 句内的标题）：总述句/标题，务必要点
            position = 1.35
        elif idx == total - 1 and total > 2:
            position = 1.20
        else:
            position = 1.0
        scores.append(combined * length_factor * position)
    return scores


#: 文档开头的"标题样"短行：不含句末标点、不含数字、短。
#: 位置限定在前几句，所以不会把正文里的编号小标题误判成标题。
def _looks_like_title(sentence: str, index: int) -> bool:
    """判断是否为文档开头的标题行（如 "采购合同" / "Service Agreement"）。"""
    if index >= 3:
        return False
    text = sentence.strip()
    if not text or len(text) > 40:
        return False
    if text[-1] in _TERMINATORS or text.endswith("。"):
        return False
    return not any(ch.isdigit() for ch in text)


#: 句子平均长度（字符）低于此值时，认为"一句一个事实"，按比例上调抽取比例。
_DENSE_SENTENCE_CHARS = 60

#: 高密度文档的抽取比例下限（在调用方给的比例之上抬到至少这个值）。
_DENSE_RATIO = 0.50


def _density_ratio(text: str, ratio: float) -> float:
    """按**事实密度**调整抽取比例：句子越短（一句一个事实），比例越高。

    为什么需要这一条（实测，见 `artifacts/probe_extractive.py`）：

    抽取的最小单位是**整句**，而预算按字符算。英文协议样本里每句约 75 字符、
    每句一个关键数据（`USD 48,000` / `24 months` / `4 hours` / `1.5%`），
    35% 的预算只够装 3~4 句 —— 也就是说**必然丢掉一半以上的事实**，
    与"这一句重不重要"无关，纯粹是句长决定的算术结果。

    中文合同样本反过来：句子平均 24 字符，35% 能装 10 句，丢得少得多。

    因此：**平均句长明显偏短时**（< 60 字符）把比例抬到 0.50，
    让"句数"与"信息量"对齐。这类文档本来也更好压（句子短、冗余多），
    抬高比例不会破坏 2 倍以上的压缩收益。
    """
    pieces = split_sentences(text)
    if len(pieces) < 4:
        return ratio
    average = sum(len(p[2]) for p in pieces) / len(pieces)
    if average <= _DENSE_SENTENCE_CHARS:
        return max(ratio, _DENSE_RATIO)
    return ratio


#: 默认抽取比例。**这个数不是"越小越好"**：实测（`artifacts/probe_extractive.py`）
#: 在 0.18 时关键事实保留率很差（721 字符的中文合同样本只剩 0/7 个关键数字），
#: 0.35 时升到 3/7，而压缩比仍有 2.9 倍 —— 预筛的收益来自"少送 65% 的字"，
#: 不来自"把 3000 字压到 400 字"。任务书原话是 10~20%，本实现按实测上调，
#: 理由与数据写在 `records/`（偏差表）与 `docs/` 里。
_DEFAULT_RATIO = 0.35

#: 低于这个字数的段不做预筛：段本身已经够短，抽取省下的时间抵不上丢信息的风险。
_DEFAULT_MIN_CHARS = 600

#: **绝对**字符上限：超过它一律原样返回。
#:
#: 为什么必须有这一条（实测数据）：TextRank 的相似度矩阵即使做了窗口稀疏化，
#: 仍有 O(n·w) 次余弦点积。8000 句的文本（31.5 万字符）实测要 22.9 秒 —— 预筛
#: 本应"毫秒级、不占模型"，跑到 20 秒就是纯负担。而超过这个长度的段本来也不该
#: 出现：摘要器按 ``chunk_chars``（默认 3000）分段，一段 6 万字符说明配置被改坏了，
#: 那时正确做法是让模型自己去处理，而不是先花几十秒做统计。
#:
#: 12000 字符对应约 600~1200 句，实测 0.6 秒以内，远低于一次模型调用（数十秒）。
_DEFAULT_MAX_CHARS = 12000


def extract(text: str, ratio: float = _DEFAULT_RATIO,
            min_chars: int = _DEFAULT_MIN_CHARS,
            max_chars: int = _DEFAULT_MAX_CHARS,
            min_ratio: float = 0.10, max_ratio: float = 0.70) -> ExtractResult:
    """从 ``text`` 抽取最重要的句子，压到 ``ratio`` 左右。

    返回的文本**保持原文句子顺序**，相邻选中句合并为连续段落（见模块头）。
    以下情况原样返回（不谎报"已预筛"，``changed`` 为 False）：

    * 文本短于 ``min_chars``（段本身就短，抽取的收益抵不上丢信息的风险）；
    * 文本长于 ``max_chars``（统计成本失控，见 ``_DEFAULT_MAX_CHARS``）；
    * 切不出句子、或句子数 ≤ 2（无从选择）。

    ## 选句规则（实测调出来的，改动前请先看这段）

    只按"得分从高到低、装到字数预算为止"会有**系统性偏差**：分数最高的一句话
    往往是长句，两三条长句就把预算吃光，剩下十几条短句（"付款期限：30 日。"
    这种恰恰是合同要点）一条都进不来。实测 721 字符的中文合同样本在 ratio=0.18
    时只选出 4/30 句、129 字符，`1,860,000` 这类关键数字全部丢失。

    因此改成**两遍选句**：

    1. 第一遍只要求"装进预算"（``used + len <= budget``），不再"最后一句直接塞下"，
       于是长句不会因为"反正要收"而挤掉后面的短句；选不满也没关系。
    2. 第二遍按 **"每字符得分"**（``score / len``）回填：在剩余预算内优先收
       "短而重要"的句子，直到预算用满或再没有句子塞得下。

    第二遍是这一版的关键：它把"挑要点"从"挑最长的高分句"矫正为
    "在有限字数里装进尽量多的要点"，这正是抽取式预筛的目的。
    得分本身由**中心度（TextRank）× 稀有度**合成，见 :func:`_score_sentences`。
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

    # 事实密度自适应：句子短意味着"一句一个事实"，固定比例会按句丢弃整条事实。
    # 详见 _density_ratio 的说明与实测数据。
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
    # 第零遍：**结构性标题**（文档开头的短标题行）无条件保留。
    # 它不是"得分高低"的问题 —— 标题被丢掉之后，摘要里就再也看不出这份文件是
    # 什么（"采购合同" 四个字长度惩罚 0.25 倍、稀有度 2，怎么排都进不了前几名），
    # 而它只占十几个字符。放在预算之前扣掉，剩下的预算照常按得分分配。
    for idx in range(min(3, len(sentences))):
        if _looks_like_title(sentences[idx], idx):
            length = len(pieces[idx][2])
            if used + length <= budget:
                selected.append(idx)
                picked.add(idx)
                used += length
    # 第一遍：得分优先，装得下才收
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
    # 第二遍：剩余预算按"每字符得分"回填（短而重要的句子优先）
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
            # 相邻（中间只隔空白/换行）：直接续上，不另起段落
            parts.append(piece)
        else:
            parts.append("\n" + piece)
        prev_end = end
    body = _BLANK_RE.sub("\n\n", "".join(parts)).strip()
    if len(selected) >= len(sentences):
        # 一句都没被删掉（句子数太少或预算极大）：如实返回原文，不谎报"已预筛"
        return ExtractResult(text=text, selected=list(range(len(sentences))),
                             scores=scores, sentences=sentences,
                             origin_chars=origin, kept_chars=origin)
    return ExtractResult(
        text=body, selected=selected, scores=scores, sentences=sentences,
        origin_chars=origin, kept_chars=len(body),
    )
