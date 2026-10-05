# -*- coding: utf-8 -*-
"""系统提示词重构：对比测试的**无模型**部分（v0.2.1 系统提示词重构）。

`tools/prompt_compare.py` 用真实模型跑新旧提示词的对比，那一步需要几百 MB 的
GGUF 与数分钟，不适合放进 CI/门禁。本文件覆盖同一套逻辑里**不需要模型**的部分，
让"对比测试"这件事在门禁里也真的被执行，而不是只写在记录里：

1. **客观指标函数本身正确**：分点识别（含"代码块里的 1. 不算分点"）、
   引导话术判据（承诺型条目 / 回编号 / 问句条目）、防编造判据、摘录重合度。
   这些函数是报告的判据来源，它们错了整份报告就是错的——所以单独测。
2. **场景表覆盖任务书要求的五类场景 + 一组防退化问题**（数量与关键场景都在）。
3. **模糊提问话术的新旧对比真的成立**：直接调 `prompt_compare.py --dry-run`
   （子进程，退出码即结论），断言它判定"新话术达到客观判据"。
   旧版从 `--old-ref`（默认本轮重构前的提交）里取；**浅克隆取不到历史时跳过这一项
   并明确说明**，而不是假装通过。
4. **机器可读格式不放松**：`extract` 模板必须带 ``relax=False``，
   结尾行是"严格遵守"而不是"详略仍按复杂度判断"。

跑法：
    python tools/test_prompt_compare.py
退出码：0 = 全部通过，1 = 存在失败项。
"""
import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND_DIR)

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


def load_compare_module():
    """按路径加载 ``tools/prompt_compare.py``（tools 不是包，不能直接 import）。

    必须先注册进 ``sys.modules``：那个模块用了 ``from __future__ import annotations``
    且含 dataclass，dataclasses 在解析字符串注解时会回头查 ``sys.modules[__module__]``，
    没注册就会抛 ``AttributeError: 'NoneType' object has no attribute '__dict__'``。
    """
    path = os.path.join(ROOT, "tools", "prompt_compare.py")
    spec = importlib.util.spec_from_file_location("prompt_compare", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["prompt_compare"] = module
    spec.loader.exec_module(module)
    return module


pc = load_compare_module()

print("=== 1. 客观指标函数（报告的判据来源）===")
check("普通编号行算分点", pc.count_bullets("1. 甲\n2. 乙") == 2)
check("项目符号算分点", pc.count_bullets("- 甲\n• 乙") == 2)
check("全角括号编号算分点", pc.count_bullets("（1）甲\n(2) 乙") == 2)
check("代码块里的编号不算分点",
      pc.count_bullets("说明如下：\n```python\n1. 这不是分点\n2. 也不是\n```\n1. 这才是") == 1,
      str(pc.count_bullets("说明如下：\n```python\n1. 这不是分点\n2. 也不是\n```\n1. 这才是")))
check("一段话没有分点", pc.count_bullets("这事一段话就能说清，不必列条目。") == 0)
check("分点行可回读原文", pc.bullet_lines("1. 甲\n正文\n2. 乙") == ["1. 甲", "2. 乙"])

check("引导话术：问号在行中间也计入",
      pc.items_containing_question("1. 你要处理的对象是什么？（文件/代码）") == 1)
check("引导话术：承诺型条目按第一人称判定",
      pc.items_with_commitment("1. 我直接给步骤\n2. 你要先给材料") == 1)
check("引导话术：没有编号行就没有方向",
      pc.items_with_commitment("你可以补充更多信息。") == 0)
# 行内编号也要算方向：0.6B 实测会把三个方向挤在一行
check("方向计数：行内编号也计入",
      pc.direction_count("1) 看文件有没有问题；2) 看某段文字怎么改；3) 看代码为什么报错") == 3,
      str(pc.direction_count("1) 看文件有没有问题；2) 看某段文字怎么改；3) 看代码为什么报错")))
check("方向计数：多行编号也计入", pc.direction_count("1. 甲\n2. 乙\n3. 丙") == 3)
check("方向计数：纯正文不算方向", pc.direction_count("一段话里没有编号。") == 0)
check("方向计数：只有承诺才算「配了做法」",
      pc.direction_count_with_commitment("1) 我会先问清对象；2) 你也可以直接说") == 1)

# 判据必须能区分"追问三点"与"引导方向"，否则整份对比报告没有意义
OLD_CLARIFY = ("你的问题信息还不够，我需要确认几点才能给出有用的回答：\n"
               "1. 你要处理的**具体对象**是什么？（哪份文件 / 哪段代码）\n"
               "2. 你期望的**输出形式**是什么？（结论 / 列表）\n"
               "3. 有没有**约束条件**？（字数、期限）\n\n"
               "补充这三点后我再作答；也可以直接用 /task 选一个任务模板。")
NEW_CLARIFY = ("你的问题比较短，我先按最可能的意思列出几种情况，你回一个编号就行：\n"
               "1. 你手上有份东西要我处理 —— 把它发我，我针对它作答\n"
               "2. 你要的是一套做法或步骤 —— 我直接给可执行的步骤\n"
               "3. 有个现象或报错要排查 —— 把报错原文告诉我\n\n"
               "如果都不是，直接补一句具体情况我也能接着做。")
_old = pc.measure(OLD_CLARIFY)
_new = pc.measure(NEW_CLARIFY)
check("旧话术被识别为「把问题抛回用户」", _old["question_items"] == 3 and _old["commitment"] == 0,
      str(_old))
check("新话术被识别为「给出方向并让用户挑」",
      _new["commitment"] == 3 and _new["choice"] and _new["question_items"] == 0, str(_new))
check("旧话术末尾的「选一个任务模板」不误判为引导", _old["choice"] is False)
check("先答后问：首个句子是问号判为反问",
      pc.first_sentence_is_question("请问你能提供更多材料吗？") and
      not pc.first_sentence_is_question("常见风险有三条。你属于哪一种？"))
check("索要材料识别（中英）",
      pc.asks_for_more_info("请提供更多信息") and pc.asks_for_more_info("Please provide more details"))
check("风险清单按种类计数", pc.risk_word_hits("付款、交付、违约金") == 3)
check("免责要求识别", pc.has_review_advice("涉及重大金额，建议人工复核") and
      pc.has_review_advice("human review is advised"))
check("说不知道识别（中英）",
      pc.admits_uncertainty("我不知道，也没有查到这个法条") and
      pc.admits_uncertainty("I cannot confirm that"))
check("摘录重合度：逐字一致算 1.0",
      pc.quote_overlap("「乙方应于合同生效后 60 日内交付」", "第三条 乙方应于合同生效后 60 日内交付。") == 1.0)
check("摘录重合度：改写后不算一致",
      pc.quote_overlap("「乙方应在 60 日内交付全部设备」", "乙方应于合同生效后 60 日内运抵。") == 0.0)
check("摘录重合度：没有引号时返回 -1（无从判断）", pc.quote_overlap("没有引用的回答", "原文") == -1.0)

print("=== 2. 场景表覆盖（任务书「验证方式」的五类 + 防退化）===")
_keys = [s.key for s in pc.SCENARIOS]
for _need in ("simple", "incomplete", "vague_script", "vague_model", "complex", "contract_decision"):
    check(f"场景表含 {_need}", _need in _keys, str(_keys))
check("每个场景都写明了期望的改善", all(s.expect for s in pc.SCENARIOS))
check("每个场景都有客观判据或明确标注无需模型",
      all(s.validate is not None for s in pc.SCENARIOS))
check("防退化场景 ≥ 3 个（法条 / 数字 / 论文）", len(pc.FABRICATION) >= 3,
      str([s.key for s in pc.FABRICATION]))
check("防退化的判据要求「不编造 + 明确说不知道」两个条件",
      all(s.validate is pc._validate_uncertain for s in pc.FABRICATION))
check("防退化判据：编了内容却没说不知道 → 未通过",
      not pc._validate_uncertain({"fabricates": False, "uncertain": False}))
check("防退化判据：说了不知道且没编造 → 通过",
      pc._validate_uncertain({"fabricates": False, "uncertain": True}))
check("防退化判据：编了具体细节（即使补了一句不知道）→ 未通过",
      not pc._validate_uncertain({"fabricates": True, "uncertain": True}))
check("「尚未颁布 / 未公开 / 未发现」也算承认不知道",
      pc.admits_uncertainty("该法目前尚未正式颁布实施")
      and pc.admits_uncertainty("目前没有公开的财务数据")
      and pc.admits_uncertainty("根据现有记录，未发现这篇论文"))
check("编造物证识别：卷期页码",
      pc.fabricates_specifics("期刊：《量子通信》第5卷第3期，共 12 页")
      and not pc.fabricates_specifics("我不知道，也没有查到这个法条"))
check("编造物证识别：金额量级",
      pc.fabricates_specifics("2025 年营业收入约 3.2 亿元")
      and not pc.fabricates_specifics("无法提供具体数字"))
check("信息不全场景：有风险清单但假摘录 → 判未通过",
      not pc._validate_incomplete({"risk_words": 6, "quote_overlap": 0.0}))
check("信息不全场景：有风险清单且没假摘录 → 判通过",
      pc._validate_incomplete({"risk_words": 6, "quote_overlap": -1.0}))
check("信息不全场景：只回「未见相关约定」→ 判未通过",
      not pc._validate_incomplete({"risk_words": 0, "quote_overlap": -1.0}))
check("模糊提问场景：索要材料的编号条目不算方向",
      not pc.SCENARIOS[3].validate({"directions": 3, "asks_more": True})
      and pc.SCENARIOS[3].validate({"directions": 3, "asks_more": False}))
check("旧提示词来源是固定提交（不是 HEAD/标签，避免对比对象漂移）",
      len(pc.DEFAULT_OLD_REF) == 40 and all(c in "0123456789abcdef" for c in pc.DEFAULT_OLD_REF),
      pc.DEFAULT_OLD_REF)
check("缺省模型指向随包发布的默认档",
      pc.DEFAULT_MODEL.endswith(os.path.join("models", "Qwen3-0.6B-Q6_K.gguf")), pc.DEFAULT_MODEL)

print("=== 3. 机器可读格式不放松（extract 的 JSON）===")
import prompts  # noqa: E402

check("extract 模板标注了 relax=False", prompts.TASK_TEMPLATES["extract"].get("relax") is False)
_extract = prompts.build_system_prompt("extract")
check("extract 结尾仍是「严格遵守输出格式」", prompts._STRICT_ENDING in _extract, _extract[-60:])
check("extract 不含「详略按复杂度判断」", "详略仍按问题复杂度判断" not in _extract)
_relaxed = prompts.build_system_prompt("contract")
check("普通模板结尾换成条件式措辞", prompts._RELAXED_ENDING in _relaxed, _relaxed[-80:])
check("普通模板仍保留格式优先", "本任务的格式要求优先" in _relaxed)
check("机器可读清单暴露 relax 标志",
      any(x["id"] == "extract" and x["relax"] is False for x in prompts.public_list()),
      str(prompts.public_list()[0]))

print("=== 4. 模糊提问的真实链路（真 Backend + 桩引擎）===")
# 这一段测的是"引导"在**产品里**的行为，而不只是文案：main.py 的短路分支、
# 引导模式是否绕开短路、以及模型实际收到的系统提示词是哪一份。
import main as backend_main  # noqa: E402
from config import Config  # noqa: E402


def make_backend(clarify_vague=True, mode="chat"):
    """真 Backend + 桩引擎（只替换"模型怎么吐字"）。"""
    config = Config()
    config.model_path = "stub.gguf"
    config.speed_hint = False
    config.clarify_vague = bool(clarify_vague)
    seen = {}

    class StubLlama:
        def tokenize(self, data, **kwargs):
            return list(range(max(1, len(data) // 3)))

        def create_chat_completion(self, messages=None, stream=False, **kwargs):
            seen["messages"] = messages
            if not stream:
                return {"choices": [{"message": {"content": "答"}}]}
            return iter([{"choices": [{"delta": {"content": "答"}}]}])

    class StubEngine(backend_main.LLMEngine):
        def __init__(self):
            self.llm = StubLlama()
            self.reasoning_enabled = False
            self.reasoning_tags = []
            self.model_path = "stub.gguf"
            self.n_ctx = 4096
            self.n_ctx_train = 4096
            self.kv_cache_bytes = 0
            self.last_tps = 0.0
            self.last_elapsed = 0.0
            self.last_tokens = 0

        @property
        def ready(self):
            return True

        def speed_hint(self, *a, **k):
            return ""

        def kv_cache_text(self):
            return "0 B"

    backend = backend_main.Backend(config)
    backend.llm = StubEngine()
    backend.conv.set_mode(mode)
    return backend, seen


def _run(backend, message):
    return list(backend.stream({"action": "chat_stream", "message": message}))


_general, _seen_general = make_backend(mode="chat")
_lines = _run(_general, "这个怎么办")
_text = "".join(str(x.get("content", "")) for x in _lines)
_marked = any(x.get("clarify") for x in _lines)
check("普通模式下模糊提问走确定性引导（带 clarify 标记）", _marked, _text[:80])
check("引导话术给了可选方向（≥2 个编号条目）", pc.direction_count(_text) >= 2, _text[:120])
check("引导话术让用户回编号", "编号" in _text, _text[:60])
check("走了短路就不调用模型", "messages" not in _seen_general)

_guide, _seen_guide = make_backend(mode="chat")
_guide.conv.set_task_prompt("（引导模板正文）", "guide")
_guide_lines = _run(_guide, "这个怎么办")
_guide_text = "".join(str(x.get("content", "")) for x in _guide_lines)
check("套用 /task guide 后不走短路（交给模型现列方向）",
      not any(x.get("clarify") for x in _guide_lines), _guide_text[:80])
check("套用 /task guide 后模型确实被调用", "messages" in _seen_guide)
_guide_system = str((_seen_guide.get("messages") or [{}])[0].get("content", ""))
check("套用 /task guide 后系统提示词里带上了任务层", "引导模板正文" in _guide_system,
      _guide_system[:80])

_agent, _seen_agent = make_backend(mode="agent")
_agent_lines = _run(_agent, "这个怎么办")
check("agent 模式也不走模糊提问短路（模型本来就要自己决定做什么）",
      not any(x.get("clarify") for x in _agent_lines),
      "".join(str(x.get("content", "")) for x in _agent_lines)[:80])

_off, _seen_off = make_backend(clarify_vague=False)
_off_lines = _run(_off, "这个怎么办")
check("关掉 clarify_vague 后不再拦截", not any(x.get("clarify") for x in _off_lines))
check("关掉 clarify_vague 后交给模型", "messages" in _seen_off)

_hist, _ = make_backend(mode="general")
_hist.conv.add_message("user", "先前的提问")
_hist.conv.add_message("assistant", "先前的回答")
check("有历史时不再触发引导（那时的「这个」指上文）",
      not any(x.get("clarify") for x in _run(_hist, "这个怎么办")))

_specific, _ = make_backend(mode="general")
check("具体问题不会被拦成引导",
      not any(x.get("clarify") for x in _run(_specific, "请帮我审查这份采购合同的付款条款")))

print("=== 5. 新旧提示词对比（子进程跑 prompt_compare.py --dry-run）===")
_compare = os.path.join(ROOT, "tools", "prompt_compare.py")
_ref = pc.DEFAULT_OLD_REF
_probe = subprocess.run(["git", "-C", ROOT, "cat-file", "-e", f"{_ref}^{{commit}}"],
                        capture_output=True, text=True)
if _probe.returncode != 0:
    print(f"  [SKIP] 本地没有旧提示词提交 {_ref[:8]}（浅克隆），跳过对比；"
          f"完整仓库或 --old-ref 指向可用提交时会执行")
else:
    _run = subprocess.run([sys.executable, _compare, "--dry-run", "--old-ref", _ref],
                          capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    check("prompt_compare --dry-run 退出码为 0（新话术达到客观判据）",
          _run.returncode == 0, (_run.stdout or "")[-400:] + (_run.stderr or "")[-200:])
    _out = _run.stdout or ""
    check("对比报告里出现旧模式表（6 个）与新模式表（3 个）",
          "6 个）" in _out and "3 个）" in _out, _out[:300])
    check("对比报告给出「被移除的模式」与迁移映射（能力不丢的证据）",
          "被移除的模式" in _out and "summary → /task summary" in _out, _out[:400])
    check("对比报告含最长系统提示词的新旧对照", "最长系统提示词" in _out)
    check("对比报告给出客观判据结论", "通过" in _out and "承诺型条目" in _out)

print()
print("=" * 60)
print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
if FAILED:
    for name in FAILED:
        print(f"  - {name}")
sys.exit(0 if not FAILED else 1)
