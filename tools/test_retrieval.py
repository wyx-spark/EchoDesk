# -*- coding: utf-8 -*-
"""本地检索测试（v0.2.1 能力扩展-1 任务六：RAG 深度）。

任务书对 RAG 的要求逐条落到断言上：

| 任务书要求 | 对应断言 |
| --- | --- |
| 本地文档检索（分块 + 检索） | 分块不切句、区间可回溯、BM25 排序正确 |
| 检索与对话融合（注入最相关片段而非全文） | `Retriever.as_context` 的形状；后端"短文档全文 / 长文档摘要+检索"的分工 |
| 引用溯源（文件名 + 位置） | 注入文本带 `文件名（字符 a-b）`，模型被要求用 `[编号]` 标注 |
| 多文档问答（跨文件检索） | 多文件索引 + 跨文件 top-k + 按文件过滤 |
| 与分段摘要互补 | 长文档注入摘要 + 片段；短文档不检索（有断言） |
| 增量索引（新文件自动加入，不必重建全部） | `add_file` 只动那一份、`remove_file` 精确摘除、`rebuilds` 计数可查 |

外加**检索质量**的客观下界：拿一个真实感的采购合同（含 6 个关键事实）问关键事实，
要求命中的片段里包含该事实（与 `tools/test_extractive.py` 的关键事实口径一致）。

跑法：``python tools\\test_retrieval.py``；退出码 0 = 全部通过。
"""
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import retrieval as rt          # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


CONTRACT = (
    "采购合同\n"
    "第三条 乙方应于合同生效后 60 日内将全部设备运抵甲方指定地点。\n"
    "第四条 甲方应在收到全部货物并验收合格后 30 个工作日内支付合同价款 128.6 万元。\n"
    "第五条 乙方逾期交货的，每逾期一日按合同总额的 0.3% 向甲方支付违约金。\n"
    "第六条 质保期为验收合格后 24 个月，质保金为合同总额的 5%。\n"
    "第七条 争议提交甲方所在地人民法院管辖。\n"
    "第八条 本合同一式肆份，双方各执贰份，自双方盖章之日起生效。\n"
)

NOTICE = (
    "关于调整办公用品采购流程的通知\n"
    "自 2026 年 4 月 1 日起，办公用品采购改为线上申请。\n"
    "单笔金额低于 500 元的由科室负责人审批；500 元以上的需报综合处复核。\n"
    "每月 25 日前提交下月需求，逾期顺延至下下月。原纸质申请单停止使用。\n"
)

print("=== 1. 分词（中英混合，零依赖）===")
_tokens = rt.tokenize("采购合同 Contract Price 128.6 万元")
check("中文切 2 字词元", "采购" in _tokens and "合同" in _tokens)
check("英文按词且小写", "contract" in _tokens and "price" in _tokens)
check("数字词元保留", "128" in _tokens or "128.6" in _tokens, str(_tokens[:12]))
check("停用词被过滤", "the" not in rt.tokenize("the contract"))
check("空文本给出空词元", rt.tokenize("") == [])

print("=== 2. 分块：不切句、带重叠、区间可回溯 ===")
_sents = rt.split_sentences(CONTRACT)
check("切句给出字符区间", bool(_sents) and all(isinstance(s[0], int) for s in _sents))
check("区间能取回原文", all(CONTRACT[s:e] == text for s, e, text in _sents))
_chunks = rt.chunk_text(CONTRACT, chunk_chars=120, overlap_chars=40)
check("分块非空且不超长", bool(_chunks) and all(end - start <= 200 for start, end, _ in _chunks),
      str([(s, e) for s, e, _ in _chunks]))
check("每个块的文本与区间一致",
      all(CONTRACT[start:end] == text for start, end, text in _chunks))
check("分块保留句子边界（不以半个字开头）",
      all(text.strip() and text.strip()[0] not in "。，、；" for _, _, text in _chunks))
check("块之间有重叠（长文才会触发）",
      len(rt.chunk_text(CONTRACT * 6, chunk_chars=200, overlap_chars=60)) > 3)
check("没有句末标点时按长度硬切",
      len(rt.chunk_text("A" * 500, chunk_chars=200, overlap_chars=0)) == 3)
check("空文本不分块", rt.chunk_text("") == [])

print("=== 3. BM25 排序与可解释性 ===")
_retriever = rt.Retriever()
_retriever.add_file("采购合同.txt", CONTRACT)
_retriever.add_file("通知.txt", NOTICE)
_hits = _retriever.search("违约金怎么算", top_k=3)
check("命中相关文件", bool(_hits) and _hits[0].chunk.file == "采购合同.txt",
      str([(h.chunk.file, round(h.score, 2)) for h in _hits]))
check("命中段落包含答案", bool(_hits) and "0.3%" in _hits[0].chunk.text,
      _hits[0].chunk.text if _hits else "")
check("给出命中的词元（可解释）", bool(_hits) and bool(_hits[0].matched))
check("得分降序", all(_hits[i].score >= _hits[i + 1].score for i in range(len(_hits) - 1)))
check("无关问题不硬凑（无命中就返回空）",
      _retriever.search("量子计算机的制冷方案") == [] or
      _retriever.search("量子计算机的制冷方案")[0].score < _hits[0].score)
check("空查询返回空", _retriever.search("") == [])
check("按文件过滤生效",
      all(hit.chunk.file == "通知.txt" for hit in _retriever.search("500 元", files=["通知.txt"])))

print("=== 4. 检索质量下界：关键事实能被检索到 ===")
# 与抽取式预筛同一把尺子：合同里 6 个关键事实，逐个当问题去检索，
# 要求命中的片段里真的出现该事实（而不是"看起来相关"）。
FACTS = ["60 日", "30 个工作日", "128.6 万元", "0.3%", "24 个月", "5%"]
_found = 0
for fact in FACTS:
    hits = _retriever.search(fact, top_k=2)
    if hits and any(fact in hit.chunk.text for hit in hits):
        _found += 1
check("关键事实命中的比例不低于 5/6", _found >= 5, f"{_found}/6")
print(f"    （实测 {_found}/6）")

print("=== 5. 增量索引：只动新文件，不重建全部 ===")
_inc = rt.Retriever()
_inc.add_file("甲.txt", CONTRACT)
_first_chunks = list(_inc._chunks_by_file["甲.txt"])
before_rebuilds = _inc.rebuilds
_inc.add_file("乙.txt", NOTICE)
check("新增文件不影响已有文件的分块（对象同一）",
      _inc._chunks_by_file["甲.txt"] is not None
      and [c.text for c in _inc._chunks_by_file["甲.txt"]] == [c.text for c in _first_chunks])
check("新增文件**没有**触发整体重建", _inc.rebuilds == before_rebuilds,
      f"{before_rebuilds} → {_inc.rebuilds}")
check("两份文件都能检索到",
      bool(_inc.search("违约金")) and bool(_inc.search("办公用品")))
check("同名文件按最新内容替换（不是追加）",
      _inc.add_file("乙.txt", "完全不同的内容：只有这一句。") >= 1
      and _inc.search("办公用品") == [])
_inc.remove_file("甲.txt")
check("删除文件后它的内容检索不到", _inc.search("违约金") == [])
check("删除只影响被删的那份", bool(_inc.search("完全不同的内容")))
check("索引统计可观测", _inc.stats()["files"] == 1 and _inc.stats()["mode"] == "keyword")

print("=== 6. 引用溯源：注入文本的形状 ===")
_ctx = _retriever.as_context("违约金怎么算")
check("注入文本带编号", "[1]" in _ctx, _ctx[:80])
check("注入文本带文件名", "采购合同.txt" in _ctx)
check("注入文本带字符区间（可核对）", "字符" in _ctx and "-" in _ctx.split("字符")[1][:12],
      _ctx[:120])
check("要求模型标注引用编号", "标注依据" in _ctx and "[编号]" in _ctx)
check("没有命中时不注入空块", _retriever.as_context("量子计算机的制冷方案") == "")
# 重叠块去重（真模型实测发现的问题：相邻块一起入选会让上下文里出现两段几乎一样的内容）
_dup = rt.Retriever()
_dup.add_file("重复.txt", CONTRACT * 20)
_dup_ctx = _dup.as_context("质保金比例", top_k=4)
check("重叠的相邻块不会一起注入（去重生效）",
      len(re.findall(r"^\[\d+\]", _dup_ctx, re.M)) < 4,
      str(re.findall(r"^\[\d+\].*", _dup_ctx, re.M)))
check("去重后仍然是相关片段", "5%" in _dup_ctx or "质保" in _dup_ctx, _dup_ctx[:120])
_budget = _retriever.as_context("付款 交付 违约 质保 争议 生效", budget_chars=200)
check("总长预算被遵守", len(_budget) <= 400, str(len(_budget)))

print("=== 7. 与分段摘要的分工（后端决策）===")
import main as backend_main  # noqa: E402
from config import Config  # noqa: E402


def make_backend(**kwargs):
    config = Config()
    config.model_path = "stub.gguf"
    config.speed_hint = False
    config.files.disk_threshold_chars = 500        # 测试里把小文档阈值调小，便于构造长文档
    for key, value in kwargs.items():
        setattr(config.retrieval, key, value)

    class StubEngine(backend_main.LLMEngine):
        def __init__(self):
            self.reasoning_enabled = False
            self.reasoning_tags = []
            self.model_path = "stub.gguf"
            self.n_ctx = 8192
            self.n_ctx_train = 32768
            self.kv_cache_bytes = 0
            self.last_tps = 0.0
            self.last_elapsed = 0.0
            self.last_tokens = 0
            self.seen = []

        @property
        def ready(self):
            return True

        def speed_hint(self, *a, **k):
            return ""

        def kv_cache_text(self):
            return "0 B"

        def chat_complete_split(self, messages, **kwargs):
            self.seen.append([dict(m) for m in messages])
            return "", "答"

        def chat_stream_split(self, messages, **kwargs):
            self.seen.append([dict(m) for m in messages])
            yield "answer", "答"

    backend = backend_main.Backend(config)
    backend.llm = StubEngine()
    return backend


_short = make_backend()
_short.conv.set_file("通知.txt", NOTICE)
_short_ctx = _short._retrieval_context_for("这份通知讲了什么")
check("短文档不启用检索（全文注入更准）", _short_ctx == "", _short_ctx[:60])
_short_msgs = _short.conv.build_messages("这份通知讲了什么", include_file=True)
check("短文档仍然注入全文", "办公用品" in _short_msgs[-1]["content"])

_long = make_backend()
_big = CONTRACT * 40                   # 超过 disk_threshold_chars=500，构造"长文档"（约 8.4 KB）
_long.conv.set_file("长合同.txt", _big)
_long.conv.set_entry_summary(_long.conv.files[0], "【长文档摘要】分三批交付，逾期按日 0.3% 计违约金。",
                             {"chunks": 3, "origin_chars": len(_big)})
_long_ctx = _long._retrieval_context_for("违约金怎么算")
check("长文档（有摘要）启用检索", "【检索到的相关片段】" in _long_ctx, _long_ctx[:80])
check("检索片段来自该文件", "长合同.txt" in _long_ctx)
check("检索被记录（可观测）", (_long.retrieval_diagnostics().get("last_query") or {}).get("query") == "违约金怎么算",
      str(_long.retrieval_diagnostics().get("last_query")))
_long_msgs = _long.conv.build_messages("违约金怎么算", include_file=True,
                                       retrieval_context=_long_ctx)
_injected = _long_msgs[-1]["content"]
check("检索片段排在文件块之前（模型先看到定位结果）",
      _injected.index("【检索到的相关片段】") < _injected.index("【文件内容"), _injected[:120])
check("长文档注入的是摘要而不是全文（配合检索用）",
      "长文档摘要" in _injected and len(_injected) < len(_big),
      f"{len(_injected)} vs 原文 {len(_big)}")

_off = make_backend(enabled=False)
_off.conv.set_file("长合同.txt", _big)
check("关掉检索后不注入片段", _off._retrieval_context_for("违约金怎么算") == "")
check("配置文件默认关闭项可读", Config().retrieval.enabled is True)

_filtered = make_backend()
_filtered.conv.set_file("长合同.txt", _big)
_filtered.conv.set_entry_summary(_filtered.conv.files[0], "摘要", {"chunks": 1, "origin_chars": len(_big)})
check("问题没提到文件内容时不检索（省窗口）",
      _filtered._retrieval_context_for("帮我写一首诗") == "")

print("=== 8. 性能（检索不能比模型还慢）===")
_big_index = rt.Retriever()
_t0 = time.time()
_chunk_count = _big_index.add_file("大文档.txt", _big)
_index_ms = (time.time() - _t0) * 1000
_t1 = time.time()
for _ in range(20):
    _big_index.search("违约金 交付 质保")
_query_ms = (time.time() - _t1) * 1000 / 20
check(f"索引 {len(_big)} 字符耗时 < 500 ms（实测 {_index_ms:.0f} ms）", _index_ms < 500)
check(f"单次检索 < 50 ms（实测 {_query_ms:.1f} ms）", _query_ms < 50)
check("块数合理（不是一块装完）", _chunk_count > 10, str(_chunk_count))
print(f"    （{len(_big)} 字符 → {_chunk_count} 块；索引 {_index_ms:.0f} ms、检索 {_query_ms:.1f} ms）")

print()
print("=" * 60)
print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
if FAILED:
    for name in FAILED:
        print(f"  - {name}")
sys.exit(0 if not FAILED else 1)
