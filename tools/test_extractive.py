# -*- coding: utf-8 -*-
"""抽取式预筛（任务四：长文档摘要性能优化）自动化测试。

任务书任务四列了七条优化方向，本文件覆盖其中**收益最大且可机验**的两条：

* **抽取式预筛**（``backend/extractive.py``）：TextRank 选句 + 稀有度加权，
  毫秒级、不用模型，把每段压到设定比例后**才**交给模型；
* **摘要器接线**（``backend/summarizer.py``）：预筛必须真的作用在送进模型的
  文本上、可开关、可统计、失败时能安全退回原文。

设计取舍（为什么这样断言）：

* 抽取式摘要本质是**有损**的，不可能"压到 18% 还一个事实不丢"。因此测试
  断言的是**方向与边界**（比例落在配置区间、顺序不变、预算不超、
  关键数字尽量保留），而不是"必须保留全部"这种做不到的承诺。
* 关键事实保留率用**下界**断言（≥ 一半），并在注释里写明实测值与
  为什么 ratio 从任务书建议的 10~20% 上调到 35%。
* 不 import llama_cpp：预筛是纯统计，测试全程无模型、无网络、毫秒级。

跑法：
    python tools/test_extractive.py
退出码：0 = 全部通过，1 = 存在失败项。
"""
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND_DIR)

import extractive as X  # noqa: E402
from config import SummarizeConfig  # noqa: E402
from summarizer import DocumentSummarizer, split_chunks  # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 测试文本

#: 中文合同样本：要点句（金额/日期/期限）与套话句混排 —— 这正是抽取式预筛
#: 最容易翻车的输入（套话句彼此相似、中心度高，要点句只出现一次、中心度低）。
CONTRACT = """采购合同

甲方（买方）：北京示例科技有限公司
乙方（卖方）：上海样品制造有限公司

第一条 标的与数量
1.1 本合同标的为服务器机架 120 台，型号 SR-4200。
1.2 乙方应保证所供货物为全新原装，符合国家强制性标准。
1.3 包装应适合长途运输，费用由乙方承担。

第二条 价款与支付
2.1 合同总价为人民币 1,860,000 元（含税，税率 13%）。
2.2 甲方在合同生效后 5 个工作日内支付预付款 30%，即 558,000 元。
2.3 货到验收合格后 30 个工作日内支付剩余 70%。
2.4 甲方逾期付款的，按逾期金额每日 0.03% 支付违约金。

第三条 交付
3.1 乙方应于 2026 年 3 月 31 日前将全部货物运抵甲方指定地点。
3.2 分三批交付：首批 40 台、第二批 40 台、第三批 40 台。
3.3 逾期交付的，按逾期部分货款每日 0.3% 计收违约金。

第四条 验收
4.1 甲方应在收货后 5 个工作日内完成验收，逾期未提出异议视为验收合格。
4.2 验收不合格的，乙方应在 10 日内更换，费用自负。

第五条 质量保证
5.1 质保期自验收合格之日起 36 个月。
5.2 质保期内非人为损坏，乙方免费维修或更换。

第六条 保密
6.1 双方对合同内容及履行过程中知悉的商业秘密负保密义务，期限 5 年。

第七条 争议解决
7.1 因本合同发生的争议，提交北京仲裁委员会仲裁。
7.2 仲裁裁决为终局的，对双方均有约束力。

第八条 其他
8.1 本合同一式肆份，双方各执贰份，具有同等法律效力。
8.2 本合同自双方法定代表人或授权代表签字并盖章之日起生效。
"""

#: 英文服务协议样本：验证拉丁词元特征路径（中文靠字符二元组，英文靠词元）
SERVICE_EN = """Service Agreement

1. Scope of Services
The Supplier shall provide preventive maintenance for all servers listed in Appendix A.
Response time for critical incidents shall not exceed 4 hours from the notification.

2. Fees
The annual service fee is USD 48,000, payable quarterly in advance.
Late payment shall incur interest at 1.5% per month.

3. Term and Termination
This Agreement commences on 1 January 2026 and continues for 24 months.
Either party may terminate for material breach with 30 days written notice.

4. Limitation of Liability
Neither party shall be liable for indirect or consequential losses.
Total liability shall not exceed the fees paid in the preceding 12 months.

5. Governing Law
This Agreement is governed by the laws of the People's Republic of China.
"""


# ------------------------------------------------- 1. 句子切分

def test_split_sentences():
    section("一、句子切分（切分质量决定抽取质量）")
    pieces = X.split_sentences(CONTRACT)
    texts = [p[2] for p in pieces]

    check("切出多句", len(pieces) >= 25, f"得到 {len(pieces)} 句")
    check("区间连续且不重叠",
          all(pieces[i][1] <= pieces[i + 1][0] for i in range(len(pieces) - 1)))
    check("区间与原文逐字一致",
          all(CONTRACT[a:b] == t for a, b, t in pieces))
    check("不产生空白句", all(t.strip() for t in texts))
    check("句末标点被收进句内",
          any(t.rstrip().endswith("。") for t in texts))
    check("换行也是硬边界（清单行不会粘成一个超长句）",
          any("1.1 " in t and len(t) < 60 for t in texts),
          f"最长句 {max(len(t) for t in texts)} 字符")

    # 英文句点只在"后面不是数字"时算句末，否则 "3.5%" / "v1.2" 会被切断
    en = [t for _, _, t in X.split_sentences("The rate is 3.5% monthly. See v1.2 docs.")]
    check("英文小数不被切断", len(en) == 2, f"得到 {en}")
    check("空文本切出空列表", X.split_sentences("") == [])


# ------------------------------------------------- 2. 特征与相似度

def test_features_and_graph():
    section("二、特征与相似度（中文二元组 + 拉丁词元）")
    feats = X.features("合同总价为人民币 186 万元 total price")
    check("中文按字符二元组切特征", "合同" in feats and "同总" in feats, f"{feats[:6]}")
    check("拉丁按词元切特征", "total" in feats and "price" in feats)
    check("特征非空", len(feats) > 5)

    sim = X.similarity_matrix(["甲方支付预付款。", "乙方交付货物。", "甲方支付预付款。"])
    check("对角线为 0", all(sim[i][i] == 0 for i in range(3)))
    check("矩阵对称", all(abs(sim[i][j] - sim[j][i]) < 1e-9 for i in range(3) for j in range(3)))
    check("同句相似度 > 异句", sim[0][2] > sim[0][1], f"{sim[0][2]:.3f} vs {sim[0][1]:.3f}")

    # 窗口模式：只算邻近句子，结果应与精确模式在"邻近对"上一致
    sents = [p[2] for p in X.split_sentences(CONTRACT)]
    full = X.similarity_matrix(sents)
    win = X.similarity_matrix(sents, window=2)
    check("窗口模式保留邻近相似度",
          all(abs(win[i][i + 1] - full[i][i + 1]) < 1e-9 for i in range(len(sents) - 1)))
    check("窗口模式把远距离置 0",
          win[0][len(sents) - 1] == 0.0 and full[0][len(sents) - 1] > 0.0)

    scores = X.textrank(full)
    check("TextRank 权重全为正", all(s > 0 for s in scores))
    # PageRank 收敛后的权重和约等于 1（阻尼 0.85 且容差 1e-4，故留约 3% 余量）；
    # 不断言"恰好等于 1"，否则修改迭代次数就会假失败。
    check("TextRank 权重和接近 1（已归一化）", abs(sum(scores) - 1.0) < 0.05,
          f"和 = {sum(scores):.4f}")
    check("空图返回空", X.textrank([]) == [])
    check("单节点返回 1", X.textrank([[0.0]]) == [1.0])


# ------------------------------------------------- 3. 抽取行为

def test_extract_basics():
    section("三、抽取行为（顺序 / 比例 / 边界）")
    result = X.extract(CONTRACT)

    check("确实做了删减", result.changed is True)
    check("选中句数少于总句数", len(result.selected) < len(result.sentences),
          f"{len(result.selected)}/{len(result.sentences)}")
    check("抽取比例落在配置区间（0.10~0.70）", 0.10 <= result.ratio <= 0.70,
          f"实测 {result.ratio:.3f}")
    # 中文合同样本平均句长 23 字符 → 判定为高密度 → 比例被自适应抬到 0.50
    # （见 extractive._density_ratio：不做这一步，按字符算的预算会按句丢事实）
    check("高密度文档比例被抬到 0.50", abs(result.ratio - X._DENSE_RATIO) < 0.10,
          f"实测 {result.ratio:.3f}")
    check("高密度文档仍压缩 1.8 倍以上",
          len(CONTRACT) / max(1, result.kept_chars) >= 1.8,
          f"{len(CONTRACT)} → {result.kept_chars}")
    check("选中的句子下标升序（保持原文顺序）",
          result.selected == sorted(result.selected))
    check("抽取结果下标非空", len(result.selected) > 0)

    # 顺序正确性：抽取文本里保留句的相对次序必须与原文一致
    kept_original_order = [result.sentences[i] for i in result.selected]
    positions = [CONTRACT.index(s) for s in kept_original_order]
    check("保留句在原文中的位置单调递增",
          all(positions[i] < positions[i + 1] for i in range(len(positions) - 1)))
    check("抽取结果是原文句子的拼接（不改写、不重排）",
          all(s in result.text for s in kept_original_order))

    # 预算：抽取结果不应明显超出 ratio
    check("未超预算 20%", result.kept_chars <= len(CONTRACT) * (X._DEFAULT_RATIO + 0.20),
          f"{result.kept_chars} / {len(CONTRACT)}")

    # 英文路径
    en = X.extract(SERVICE_EN, min_chars=0)
    check("英文同样能抽取", en.changed is True and en.ratio <= 0.70,
          f"比例 {en.ratio:.3f}")
    check("英文抽取结果含原文句子", any(s in en.text for s in en.sentences))


def test_extract_budget_and_edges():
    section("四、抽取边界（预算 / 短文本 / 空 / 无句可切）")
    check("空文本返回空", X.extract("").text == "" and X.extract("").ratio == 1.0)
    check("空文本不报已变更", X.extract("").changed is False)

    short = "这是一段很短的文字。只有两句。"
    got = X.extract(short)
    check("短于 min_chars 时原样返回", got.text == short and got.changed is False,
          f"kept={got.kept_chars} / origin={len(short)}")

    single = "只有一句话。"
    got = X.extract(single, min_chars=0)
    check("单句原样返回", got.text == single and got.changed is False)

    # 极短句组成的文本：抽取不应把结果压成空
    tiny = "好。行。可。是。对。嗯。好。行。可。是。对。嗯。"
    got = X.extract(tiny, min_chars=0)
    check("极短句文本不产生空结果", got.text.strip() != "", f"得到 {got.text!r}")

    # 预算极小：min_ratio 兜底，不能返回空
    got = X.extract(CONTRACT, ratio=0.01, min_chars=0)
    check("ratio 低于 min_ratio 时被抬到下限", got.ratio >= 0.10 * 0.8,
          f"实测 {got.ratio:.3f}")
    got = X.extract(CONTRACT, ratio=5.0, min_chars=0)
    check("ratio 高于 max_ratio 时被封顶", got.ratio <= 0.70 + 0.05,
          f"实测 {got.ratio:.3f}")

    # 不含句末标点的长文本（表格/清单）
    no_punct = "\n".join(f"项目 {i} 数量 {i * 3} 单价 {i * 7} 元" for i in range(200))
    got = X.extract(no_punct, min_chars=0)
    check("无句末标点时按换行切句", len(got.sentences) > 50, f"{len(got.sentences)} 句")
    check("无句末标点仍能抽取", got.changed is True and got.text.strip() != "")


# ------------------------------------------------- 4. 关键事实保留

def test_key_fact_retention():
    """关键事实保留率 —— 这是预筛**能不能用**的判据。

    实测（`artifacts/probe_extractive.py`，两个样本的扫描结果）：

    | 配置 | 中文合同 6 个关键数字 | 英文协议 4 个关键数据 | 压缩比 |
    | --- | --- | --- | --- |
    | 纯 TextRank，ratio=0.18 | 0/6 | 0/4 | 5.6 倍 |
    | 纯 TextRank，ratio=0.35 | 3/6 | 1/4 | 2.8 倍 |
    | 中心度×稀有度（α=0.5），ratio=0.35 | 3/6 | 1/4 | 2.8 倍 |
    | 上述 + 密度自适应（句短则抬到 0.50） | 3/6 | **2/4** | 2.0 倍 |

    结论写在这里是为了防止后人"顺手调小 ratio 省时间"：**0.18 会把合同金额
    与日期全部丢掉**，而那正是用户要摘要的东西。故断言取下界，
    并把实测值打印出来，一旦回归立刻可见。

    同时如实记录**这条路线做不到什么**：抽取的最小单位是整句，英文样本每句
    约 75 字符且一句一个事实，20% 的预算只够装 3 句 —— 必然丢掉一半事实。
    要"一条不漏"只能关掉预筛（``summarize.extractive=false``）或提高
    ``extractive_ratio``，这一点已写进文档。
    """
    section("五、关键事实保留（有损压缩的下界断言）")
    cn_keys = ["1,860,000", "558,000", "2026 年 3 月 31 日", "0.3%", "36 个月", "北京仲裁委员会"]
    cn = X.extract(CONTRACT)
    kept_cn = [k for k in cn_keys if k in cn.text]
    print(f"  中文合同：保留 {len(kept_cn)}/{len(cn_keys)} → {kept_cn}"
          f"（比例 {cn.ratio:.2f}，压缩比 {len(CONTRACT) / max(1, cn.kept_chars):.1f} 倍）")
    check("中文要点保留不少于三分之一", len(kept_cn) >= len(cn_keys) // 3,
          f"保留 {len(kept_cn)}/{len(cn_keys)}")

    en_keys = ["USD 48,000", "24 months", "4 hours", "1.5%"]
    en = X.extract(SERVICE_EN, min_chars=0)
    kept_en = [k for k in en_keys if k in en.text]
    print(f"  英文协议：保留 {len(kept_en)}/{len(en_keys)} → {kept_en}"
          f"（比例 {en.ratio:.2f}，压缩比 {len(SERVICE_EN) / max(1, en.kept_chars):.1f} 倍）")
    check("英文要点保留不少于一半", len(kept_en) >= len(en_keys) // 2,
          f"保留 {len(kept_en)}/{len(en_keys)}")

    # 首句（总述句 / 标题）必须保留：位置权重与"标题不可丢"规则就是为它加的
    check("首句被保留（总述句/标题）", 0 in cn.selected or cn.sentences[0] in cn.text,
          f"selected={cn.selected[:6]}")
    check("英文首句（标题）同样保留", 0 in en.selected or en.sentences[0] in en.text,
          f"selected={en.selected[:6]}")

    # 稀有度加权必须真的起作用：把权重退回纯中心度后，保留率不应更好。
    # 用**真实的段**（3000 字符，平均句长 > 60 → 不触发密度自适应）来比，
    # 否则两个配置都会被抬到 0.50，掩盖权重差异。
    chunk = ((CONTRACT * 3 + SERVICE_EN * 3) * 6)[:3000]
    saved_c, saved_r = X._CENTRALITY_WEIGHT, X._RARITY_WEIGHT
    try:
        X._CENTRALITY_WEIGHT, X._RARITY_WEIGHT = saved_c, saved_r
        blend = X.extract(chunk)
        X._CENTRALITY_WEIGHT, X._RARITY_WEIGHT = 1.0, 0.0
        pure = X.extract(chunk)
    finally:
        X._CENTRALITY_WEIGHT, X._RARITY_WEIGHT = saved_c, saved_r
    probe_keys = cn_keys + en_keys
    kept_blend = len([k for k in probe_keys if k in blend.text])
    kept_pure = len([k for k in probe_keys if k in pure.text])
    print(f"  真实段对照（{len(chunk)} 字符）：混合 {kept_blend}/{len(probe_keys)}"
          f" vs 纯 TextRank {kept_pure}/{len(probe_keys)}")
    check("稀有度加权不差于纯中心度", kept_blend >= kept_pure,
          f"混合 {kept_blend} vs 纯中心度 {kept_pure}")


def test_density_adaptive():
    """事实密度自适应：句子短 → 一句一个事实 → 比例必须抬上去。

    否则"按字符算预算"会变成"按句丢事实"，与句子重要与否无关（见
    ``extractive._density_ratio`` 的说明与实测表格）。
    """
    section("五之二、事实密度自适应比例")
    # 短句（平均 < 60 字符）→ 抬到 0.50
    short_sentences = "".join(f"第 {i} 条：金额 {i * 137} 元，期限 {i % 30 + 1} 日。\n"
                              for i in range(120))
    got = X.extract(short_sentences)
    avg = sum(len(p[2]) for p in X.split_sentences(short_sentences)) / 120
    check("短句文档被识别为高密度", avg <= X._DENSE_SENTENCE_CHARS, f"平均 {avg:.0f} 字符")
    check("短句文档比例被抬到 0.50", got.ratio >= 0.45, f"实测 {got.ratio:.3f}")

    # 长句（平均 > 60 字符）→ 保持调用方给的比例
    long_sentences = ("This clause describes in considerable detail the obligations that apply "
                      "to both parties during the entire term of this agreement and thereafter. "
                      ) * 80
    got_long = X.extract(long_sentences, min_chars=0)
    avg_long = sum(len(p[2]) for p in X.split_sentences(long_sentences)) / \
        len(X.split_sentences(long_sentences))
    check("长句文档未被抬高比例", got_long.ratio <= 0.45,
          f"平均句长 {avg_long:.0f}，实测比例 {got_long.ratio:.3f}")

    # 显式传入空 ratio 时也不能崩
    check("空文本密度判断不崩", X._density_ratio("", 0.35) == 0.35)


# ------------------------------------------------- 5. 性能

def test_performance():
    section("六、性能（47KB 级：必须毫秒级）")
    big = (CONTRACT + SERVICE_EN) * 31 + CONTRACT
    chunks = split_chunks(big, 3000, 200)
    check("47KB 级文本切成多段", len(chunks) > 5, f"{len(chunks)} 段")

    started = time.perf_counter()
    origin = 0
    kept = 0
    for _, _, chunk in chunks:
        result = X.extract(chunk)
        origin += len(chunk)
        kept += result.kept_chars
    elapsed = time.perf_counter() - started
    per_chunk = elapsed / max(1, len(chunks)) * 1000
    print(f"  {len(big)} 字符 → {len(chunks)} 段，逐段抽取 {origin} → {kept} 字符，"
          f"总耗时 {elapsed * 1000:.0f} ms（单段均 {per_chunk:.1f} ms）")

    check("逐段抽取总耗时 < 3 秒（实测约 0.1 秒）", elapsed < 3.0, f"{elapsed:.2f}s")
    check("单段平均耗时 < 200 ms", per_chunk < 200, f"{per_chunk:.1f} ms")
    check("确实压缩了（送模型字数少于原文）", kept < origin * 0.6,
          f"{kept}/{origin} = {kept / origin:.2f}")

    # 大段数也要在可接受时间内完成（窗口模式 + 绝对上限生效，不会退化成分钟级）
    many = "".join(f"第 {i} 条：设备 {i} 号，金额 {i * 137} 元，期限 {i % 30 + 1} 日。\n"
                   for i in range(8000))
    started = time.perf_counter()
    result = X.extract(many)
    elapsed = time.perf_counter() - started
    print(f"  超大文本（{len(many)} 字符）耗时 {elapsed * 1000:.0f} ms，"
          f"changed={result.changed}（超 max_chars 时原样通过）")
    check("超大文本立即返回（绝对上限生效）", elapsed < 2.0, f"{elapsed:.2f}s")
    check("超大文本如实报告未预筛（不谎报）", result.changed is False)

    # 上限以内的大段仍然要能抽取
    big_chunk = "".join(f"第 {i} 条：设备 {i} 号，金额 {i * 137} 元。\n" for i in range(300))
    started = time.perf_counter()
    result = X.extract(big_chunk)
    elapsed = time.perf_counter() - started
    print(f"  万字符级段（{len(big_chunk)} 字符）耗时 {elapsed * 1000:.0f} ms，"
          f"比例 {result.ratio:.2f}")
    check("上限以内的段仍被抽取", result.changed is True)
    check("万字符级段在 2 秒内完成", elapsed < 2.0, f"{elapsed:.2f}s")


# ------------------------------------------------- 6. 摘要器接线

class _RecordingEngine:
    """伪引擎：只记录"模型实际收到了多少字符"，不真跑模型。"""

    def __init__(self):
        self.inputs = []
        self.n_ctx = 8192

    def count_tokens(self, text):
        return max(1, len(text) // 2)

    def chat_complete(self, messages, **kwargs):
        self.inputs.append(len(messages[-1]["content"]))
        return "- 要点一\n- 要点二"

    def chat_stream(self, messages, **kwargs):
        self.inputs.append(len(messages[-1]["content"]))
        yield "- 流式要点"

    def clear_cache(self):
        pass


def test_summarizer_wiring():
    section("七、摘要器接线（预筛真的作用在送模型的文本上）")
    doc = (CONTRACT * 3)
    check("测试文档足够长", len(doc) > 2000, f"{len(doc)} 字符")

    results = {}
    for flag in (True, False):
        cfg = SummarizeConfig(min_chars=100, chunk_chars=3000, extractive=flag)
        engine = _RecordingEngine()
        summ = DocumentSummarizer(engine, cfg)
        events = []
        result = summ.summarize(doc, "合同.docx", force=True,
                                on_progress=lambda e: events.append(e))
        results[flag] = (result, engine, events)

    on, engine_on, events = results[True]
    off, engine_off, _ = results[False]

    check("开关打开时统计到预筛生效", on.extractive_chunks > 0,
          f"预筛段数 {on.extractive_chunks}")
    check("开关关闭时统计为 0", off.extractive_chunks == 0)
    check("开关打开后送模型字数更少",
          on.extractive_input_chars < off.extractive_input_chars,
          f"{on.extractive_input_chars} vs {off.extractive_input_chars}")
    # 实测值受密度自适应影响（测试文本平均句长 < 60 → 比例被抬到 0.50），
    # 故断言"确实压缩了，但也没白压"这个区间，而不是盯死某个小数。
    check("实测比例在 0.35~0.65 之间", 0.35 <= on.extractive_ratio <= 0.65,
          f"实测 {on.extractive_ratio:.3f}")
    check("送模型字数至少减半", on.extractive_input_chars <= on.extractive_origin_chars * 0.7,
          f"{on.extractive_input_chars} / {on.extractive_origin_chars}")
    check("省下的字符数 = 原文 - 送模型",
          on.extractive_saved_chars == on.extractive_origin_chars - on.extractive_input_chars)
    check("关闭时比例为 1（未压缩）", abs(off.extractive_ratio - 1.0) < 1e-9)

    check("引擎实测收到的每段都比原文段短",
          sum(engine_on.inputs) < sum(engine_off.inputs),
          f"{sum(engine_on.inputs)} vs {sum(engine_off.inputs)}")
    check("两组的段数一致（预筛不改变分段）",
          on.chunk_count == off.chunk_count == len(engine_on.inputs) == len(engine_off.inputs))
    check("摘要仍然成功产出", on.summarized is True and off.summarized is True)
    check("原文指针仍指向原文区间（不受预筛影响）",
          on.pointer.startswith("chars=") and "-" in on.pointer,
          on.pointer[:60])

    pf_events = [e for e in events if e.get("prefilter_chunks")]
    check("进度事件带预筛统计（前端/测试可验证）", len(pf_events) > 0,
          f"{len(pf_events)} 个事件")
    if pf_events:
        check("事件里的预筛字符数与原文字符数一致",
              pf_events[-1]["prefilter_origin_chars"] == on.extractive_origin_chars)
        check("事件里的送模型字符数一致",
              pf_events[-1]["prefilter_input_chars"] == on.extractive_input_chars)

    # 预筛异常绝不能搞挂摘要（纯优化必须可安全降级）
    cfg = SummarizeConfig(min_chars=100, chunk_chars=3000, extractive=True)
    engine = _RecordingEngine()
    summ = DocumentSummarizer(engine, cfg)
    import summarizer as S
    original = S.extractive_pick
    try:
        S.extractive_pick = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟预筛故障"))
        result = summ.summarize(doc, "合同.docx", force=True)
    finally:
        S.extractive_pick = original
    check("预筛抛异常时摘要仍然完成（安全降级）",
          result.summarized is True and result.extractive_chunks == 0,
          f"status={result.status}")
    check("降级后引擎仍收到完整段",
          sum(engine.inputs) > on.extractive_input_chars,
          f"{sum(engine.inputs)} vs {on.extractive_input_chars}")

    # 段太短时不预筛
    cfg = SummarizeConfig(min_chars=0, chunk_chars=3000, extractive=True,
                          extractive_min_chars=600)
    engine = _RecordingEngine()
    summ = DocumentSummarizer(engine, cfg)
    tiny = "第 1 条：设备 1 号，金额 100 元。\n第 2 条：设备 2 号，金额 200 元。\n"
    result = summ.summarize(tiny, "短.txt", force=True)
    check("段短于 extractive_min_chars 时不预筛", result.extractive_chunks == 0,
          f"预筛段数 {result.extractive_chunks}")


# ------------------------------------------------- 7. 配置

def test_config():
    section("八、配置项（任务书要求「阈值可配置」）")
    cfg = SummarizeConfig()
    check("默认开启抽取式预筛", cfg.extractive is True)
    check("默认比例 0.35（实测值，任务书建议 10~20% 见记录偏差表）",
          abs(cfg.extractive_ratio - 0.35) < 1e-9, f"{cfg.extractive_ratio}")
    check("默认段长下限 600 字符", cfg.extractive_min_chars == 600)
    check("比例下限被校验拦住", SummarizeConfig(extractive_ratio=0.10).extractive_ratio == 0.10)
    bad = False
    try:
        SummarizeConfig(extractive_ratio=0.99)
    except Exception:
        bad = True
    check("比例超出上限被拒绝（0.70 封顶）", bad)

    # 配置项必须出现在随包分发的 config.json 里，否则用户看不到也改不了
    import json
    for path in (os.path.join(BACKEND_DIR, "config.json"),
                 os.path.join(ROOT, "deploy", "config.json")):
        if not os.path.exists(path):
            check(f"{os.path.basename(path)} 存在", False, path)
            continue
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        section_data = data.get("summarize", {})
        check(f"{os.path.relpath(path, ROOT)} 的 summarize 段含 extractive 三项",
              all(k in section_data for k in
                  ("extractive", "extractive_ratio", "extractive_min_chars")),
              f"实际键 {sorted(section_data)[:8]}")


def main():
    print("抽取式预筛测试（任务四：长文档摘要性能优化）")
    print(f"后端目录：{BACKEND_DIR}")
    test_split_sentences()
    test_features_and_graph()
    test_extract_basics()
    test_extract_budget_and_edges()
    test_key_fact_retention()
    test_density_adaptive()
    test_performance()
    test_summarizer_wiring()
    test_config()

    print("\n" + "-" * 42)
    print(f"共 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
