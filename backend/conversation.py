"""
[EN]
Conversation management: modes, history, file context, undo, export,
persistence, and summary compaction.

[ZH]
对话管理：模式、历史记录、文件上下文、撤回、导出、持久化与摘要压缩。
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

# [EN] Interface text (mode menu and Markdown export headings) is resolved from
# [EN] the language pack. i18n depends only on the standard library, so it can be
# [EN] imported directly here.
# [ZH] 界面文本（模式菜单与 Markdown 导出标题）通过语言包解析。i18n 仅依赖
# [ZH] 标准库，因此可在此直接导入。
from i18n import t as _t

# ===========================================================================
# [EN] System prompt composition:
# [EN] mode block + shared rules + language instruction + optional task layer +
# [EN] optional custom instruction.
#
# [EN] 1. Mode blocks (``MODES``): three modes, each defining role, output format,
# [EN]    level of detail, reasoning procedure, examples, tone, and scenario
# [EN]    boundaries. The level-of-detail element returns control over response
# [EN]    length to the model instead of forcing every answer into a template.
# [EN]    The mode set was reduced from seven to three (chat, contract, agent);
# [EN]    migration details are documented next to ``MODE_ALIASES``.
# [EN] 2. Shared rules (``_CORE_RULES``): seven invariants common to every mode
# [EN]    (perspective, boundaries, verbosity, answer-before-question,
# [EN]    explicit assumptions, guidance, and output discipline). They are shared
# [EN]    because duplicating them in every mode wastes context and creates three
# [EN]    independent sources of truth.
# [EN] 3. Language instruction (``_REPLY_LANGUAGE``): follows the interface locale.
# [EN] 4. Task layer: a template applied by ``/task`` and layered on top of the
# [EN]    mode block rather than replacing it.
# [EN] 5. Custom instruction: content set by ``/prompt``; it has the highest
# [EN]    priority and replaces both the mode block and the task layer.
#
# [EN] The prompt body is sent to the model, not displayed to the user, so it
# [EN] intentionally remains Chinese. Translating it would weaken wording designed
# [EN] for small models. The prompt language and the reply language are separate:
# [EN] replies follow ``_REPLY_LANGUAGE``. User-visible mode descriptions remain in
# [EN] the language pack under ``mode.desc.<mode>``.
#
# [EN] Length budget: a mode block is at most 480 characters; the mode block plus
# [EN] shared rules and language instruction is at most 700 characters. The default
# [EN] context is 8192 tokens. ``tools/test_capability.py`` asserts these limits;
# [EN] later revisions must compress existing wording rather than raise them.
#
# [ZH] 系统提示词组合：模式块、共享规则、语言指令、可选任务层与可选自定义指令。
#
# [ZH] 1. 模式块（``MODES``）：三个模式，分别定义角色、输出格式、详略程度、推理步骤、
# [ZH]    示例、语气与场景边界。“详略程度”将回答长度的控制权交还模型，避免所有回答
# [ZH]    被模板化。模式集合由七个收敛为三个（chat、contract、agent）；迁移细节见
# [ZH]    ``MODE_ALIASES`` 附近的说明。
# [ZH] 2. 共享规则（``_CORE_RULES``）：所有模式共用的七条约束（视角、边界、详略、
# [ZH]    先答后问、显式假设、引导与输出纪律）。共享的目的是避免在每个模式中重复，
# [ZH]    既节省上下文，也避免出现三份彼此不一致的规则。
# [ZH] 3. 语言指令（``_REPLY_LANGUAGE``）：跟随界面语言。
# [ZH] 4. 任务层：由 ``/task`` 套用的模板，叠加在模式块之上，而不是替换模式块。
# [ZH] 5. 自定义指令：由 ``/prompt`` 设置，优先级最高，替换模式块与任务层。
#
# [ZH] 提示词正文发送给模型而不是展示给用户，因此刻意保持中文。翻译会削弱针对小型
# [ZH] 模型设计的措辞。提示词语言与回复语言是两个独立问题：回复语言由
# [ZH] ``_REPLY_LANGUAGE`` 决定。用户可见的模式说明仍位于语言包的
# [ZH] ``mode.desc.<模式>`` 下。
#
# [ZH] 长度预算：单个模式块不超过 480 字符；模式块、共享规则与语言指令合计不超过
# [ZH] 700 字符。默认上下文为 8192 token。``tools/test_capability.py`` 对这些上限
# [ZH] 进行断言；后续修改必须压缩既有措辞，不得提高上限。
# ===========================================================================

MODES: Dict[str, str] = {
    "chat": (
        "【角色】你是通用问答助手，面向中文办公场景，回答简洁、直给结论。\n"
        "【输出格式】纯文本；需要并列信息时才写「1. 2. 3.」，每条不超过两行；不复述我的问题。\n"
        "【详略】能一句话答完就一句话，不要为了显得完整硬凑三条；只有确实需要列举时才分点。\n"
        "【思维】先判断我真正想问什么；一句话有多种理解时，按最可能的一种回答并说明另一种理解是什么。\n"
        "【示例】问：报销单丢了怎么办？→ 先按财务要求补开证明，再走补录流程；具体以你单位制度为准。\n"
        "【语气】平稳、口语化，不堆敬语，不写开场白与自我说明。"
    ),
    "contract": (
        "【角色】你是审查采购合同条款的财务专员，只关注钱、时间、责任三类风险。\n"
        "【输出格式】每条风险写成「原文摘录 → 风险点 → 修改建议」，摘录必须与原文逐字一致；"
        "涉及重大金额、付款或责任承担的结论，后面必须跟一句「建议人工复核」。\n"
        "【详略】还没给材料时，先列这类合同最常见的风险（付款／交付／违约／争议／缺失条款）"
        "各一句，但不要写成「原文摘录」形式（没有原文就没有摘录）；给了材料才逐条摘录。\n"
        "【思维】先摘录原文，再判断风险。\n"
        "【示例】「验收合格后 30 个工作日内支付」→ 未约定验收期限 → 建议补验收期限。\n"
        "【边界】没有原文依据的结论不写；找不到的条款写「未见相关约定」，不要臆测；"
        "金额、比例、期限照抄不换算。\n"
        "【语气】严谨保守，不夸大。"
    ),
    "agent": (
        "【角色】你是能调用工具的智能体：自己决定用哪个工具、按什么顺序，直到把任务做完。\n"
        "【输出格式】需要外部信息或计算时，按下面「工具协议」输出一行 JSON；"
        "任务完成时以「最终答案：」开头给结论。\n"
        "【详略】工具能查到的别凭记忆写；一次只调一个工具，看到结果再决定下一步。\n"
        "【思维】判断缺什么 → 挑工具 → 看结果 → 不够就换工具或改参数重试 → 收敛到结论。\n"
        "【示例】要算 3000×7.2 时输出 "
        '{"tool": "calc", "args": {"expr": "3000*7.2"}, "why": "算金额"}。\n'
        "【边界】工具缺失、失败或结果为空时如实说明并给出基于已知信息的结论，"
        "绝不编造工具返回内容。\n"
        "【语气】每一步一句话说明在做什么，不复述工具结果全文。"
    ),
}

#: [EN] Shared rules for every mode: perspective, boundaries, verbosity, and
#: [EN] output discipline. They are scenario-independent, so a single shared copy
#: [EN] is used instead of repetition in each mode; see the length-budget note at
#: [EN] the top of the file.
#: [EN]
#: [EN] Rules 4-6 deliberately relax earlier over-constraining instructions:
#: [EN] answer the most likely interpretation first, then offer alternatives;
#: [EN] state assumptions explicitly; and let the model decide the level of
#: [EN] detail. Rule 2 (no fabrication) and the hard per-mode format requirements
#: [EN] remain unchanged; they are safety invariants, not over-constraints.
#: [ZH] 所有模式共用的规则：视角、边界、详略与输出纪律。这些规则与场景无关，
#: [ZH] 因此只保留一份共享副本，而不是在每个模式中重复；参见文件顶部的长度预算
#: [ZH] 说明。
#: [ZH]
#: [ZH] 第 4–6 条有意放宽了此前的过度约束：先回答最可能的解释，再提供替代方案；
#: [ZH] 显式说明假设；由模型决定详略程度。第 2 条（不得编造）与各模式的硬性格式
#: [ZH] 要求保持不变；这些是安全约束，而非过度约束。
_CORE_RULES = (
    "\n\n[通用要求]\n"
    "1. 视角：「用户」指正在提问的人，「你」是助手；要求复述时引用他最近一次输入，而非你的回复。\n"
    "2. 边界：不知道就说不知道；不编造事实、数据、法条、条款或出处。\n"
    "3. 详略自定：能一句话答完就一句话，复杂才分点，别为凑格式硬分点。\n"
    "4. 先答后问：信息不全时先按最可能的一种理解作答，说明假设，"
    "再问要不要按其它情况展开；不要只回「请提供更多信息」。\n"
    "5. 摊开推测：只有当结论确实取决于用户没给的信息时才用，写成"
    "「如果是 A，建议 X；如果是 B，建议 Y」并标成推测。\n"
    "6. 引导：问题含糊（如「帮我看看」）时，列出 2~3 种可能的方向各配一句做法让他挑；"
    "毫无线索才追问。\n"
    "7. 输出纪律：纯文本，不要开场白与自我说明、不复述用户的话；多方案时给推荐和一句理由。"
)

#: [EN] Reply-language instruction: follows the interface locale rather than
#: [EN] hard-coding Chinese.
#: [EN]
#: [EN] Simplified Chinese requests Simplified Chinese and explicitly excludes
#: [EN] Traditional Chinese output, which small models occasionally produce.
#: [EN] English requests English. Locales without a built-in instruction fall
#: [EN] through to ``_REPLY_LANGUAGE_FALLBACK`` and reply in the question's
#: [EN] language. The table is an instruction sent to the model, not interface
#: [EN] text, so it is not stored in the language pack.
#: [ZH] 回复语言指令：跟随界面语言，而不是硬编码中文。
#: [ZH]
#: [ZH] 简体中文界面要求使用简体中文，并显式排除繁体中文输出（小型模型偶尔会
#: [ZH] 输出繁体）。英文界面要求使用英文。没有内置指令的语言使用
#: [ZH] ``_REPLY_LANGUAGE_FALLBACK``，以提问语言回复。该表是发送给模型的指令，
#: [ZH] 不是界面文本，因此不存放在语言包中。
_REPLY_LANGUAGE: Dict[str, str] = {
    "zh-Hans": "\n请始终使用简体中文回复，不要输出繁体字。",
    "en": "\nAlways reply in English.",
}

#: [EN] Fallback for locales without a built-in reply instruction: follow the
#: [EN] question's language rather than choosing one for the user.
#: [ZH] 没有内置回复指令的语言所使用的回退规则：跟随提问语言，而不是替用户选择。
_REPLY_LANGUAGE_FALLBACK = "\n请使用与用户提问相同的语言回复。"


def reply_language_instruction() -> str:
    """
    [EN]
    Return the reply-language instruction for the active interface locale. See
    ``_REPLY_LANGUAGE``.

    [ZH]
    返回当前界面语言对应的回复语言指令。参见 ``_REPLY_LANGUAGE``。
    """
    try:
        from i18n import get as _i18n_get

        locale = _i18n_get().locale
    # [EN] Prompt construction must remain functional when i18n is unavailable.
    # [ZH] i18n 不可用时，提示词构造仍须保持可用。
    except Exception:
        locale = "zh-Hans"
    return _REPLY_LANGUAGE.get(locale, _REPLY_LANGUAGE_FALLBACK)

# ===========================================================================
# [EN] Mode simplification: three modes with distinct semantics.
# [EN]
# [EN] chat: general conversation; contract: clause review; agent: tool-using
# [EN] agent. The criterion is whether a user can identify the appropriate mode
# [EN] without reasoning about prompt design.
# [EN]
# [EN] The previous seven modes (general, summary, translate, code, contract,
# [EN] creative, guide) were reduced for these reasons:
# [EN] * summary, translate, code, and creative differ in output instructions,
# [EN]   not in conversation semantics. They are temporary tasks within one
# [EN]   conversation and belong to ``/task`` templates. Their prompt bodies were
# [EN]   preserved in the corresponding task templates.
# [EN] * guide's "list two or three possible directions" is now rule 6 of the
# [EN]   shared rules, and its model-driven form moved to ``/task guide``.
# [EN] * contract remains a separate mode because it defines a specialist role
# [EN]   and strict output requirements (verbatim excerpts and mandatory human
# [EN]   review), producing distinct tone and boundaries.
# [EN] * agent is a new semantic category: the model selects tools
# [EN]   autonomously, which is distinct from predefined batch automation.
# [EN]
# [EN] Legacy mode names remain available through ``LEGACY_MODES``. For example,
# [EN] ``/mode summary`` switches to chat, applies the summary template, and keeps
# [EN] the legacy temperature preset. Behaviour is preserved without consuming a
# [EN] separate mode slot.
# [ZH] 模式简化：三个具有不同语义的模式。
#
# [ZH] chat：通用对话；contract：条款审查；agent：可调用工具的智能体。判定标准是
# [ZH] 用户能否在不理解提示词设计的前提下确定应使用哪个模式。
#
# [ZH] 此前的七个模式（general、summary、translate、code、contract、creative、
# [ZH] guide）按以下理由收敛：
# [ZH] * summary、translate、code、creative 的差别在于输出指令，而非对话语义。
# [ZH]   它们是同一场对话中的一次性任务，应由 ``/task`` 模板承担。其提示词正文已保留
# [ZH]   在对应任务模板中。
# [ZH] * guide 的“列出两到三个可能方向”已写入共享规则第 6 条；其模型驱动形式迁移至
# [ZH]   ``/task guide``。
# [ZH] * contract 保留为独立模式，因为它定义专业角色与严格输出要求（逐字摘录与强制
# [ZH]   人工复核），语气与边界均不同。
# [ZH] * agent 是新的语义类别：由模型自主选择工具，与预定义流程的批量自动化不同。
#
# [ZH] 旧模式名仍可通过 ``LEGACY_MODES`` 使用。例如 ``/mode summary`` 会切换到 chat、
# [ZH] 套用 summary 模板并沿用旧温度预设。行为得以保留，同时不再占用独立模式位。

#: [EN] Mode aliases: numeric identifiers, abbreviations, full names, and legacy
#: [EN] mode names are accepted.
#: [EN]
#: [EN] Numeric identifiers are only 1/2/3 (chat/contract/agent). The previous
#: [EN] 4/5/6/7 values are not mapped because their semantics conflict with the
#: [EN] new numbering. ``/mode 4`` therefore reports a migration hint instead of
#: [EN] silently selecting a different mode.
#: [ZH] 模式别名：接受数字标识、缩写、全称与旧模式名。
#: [ZH]
#: [ZH] 数字标识仅包括 1/2/3（chat、contract、agent）。旧的 4/5/6/7 不再映射，
#: [ZH] 因为其语义与新编号冲突。``/mode 4`` 会返回迁移提示，而不会静默选择
#: [ZH] 语义不同的模式。
MODE_ALIASES: Dict[str, str] = {
    "1": "chat", "c": "chat", "chat": "chat",
    "2": "contract", "ct": "contract", "contract": "contract",
    "3": "agent", "ag": "agent", "agent": "agent",
    # [EN] ---- Previous-version mode names: still available; switching mode also applies
    # [EN] the template of the same name (see ``LEGACY_MODES``). ----
    # [ZH] ---- 上一版的模式名：仍然可用，会自动切模式并套上同名模板（见 LEGACY_MODES） ----
    "general": "chat", "g": "chat",
    "summary": "chat", "s": "chat", "sum": "chat",
    "translate": "chat", "t": "chat", "tr": "chat",
    "code": "chat",
    "creative": "chat", "cr": "chat",
    "guide": "chat", "gd": "chat",
}

#: [EN] Legacy mode name to the task template applied during migration. An empty
#: [EN] value means mode switching only, with no template.
#: [ZH] 旧模式名到迁移时自动套用的任务模板 id 的映射。空值表示仅切换模式，
#: [ZH] 不套用模板。
LEGACY_MODES: Dict[str, str] = {
    "summary": "summary",
    "translate": "translate",
    "code": "code",
    "creative": "creative",
    "guide": "guide",
    "general": "",
}

#: [EN] Current numeric identifiers (1/2/3). Kept separate so that the distinction
#: [EN] between current and legacy numbers has a single source of truth.
#: [ZH] 当前数字标识（1/2/3）。单独维护，使“当前编号与旧编号”的判定只有一个
#: [ZH] 事实来源。
MODE_NUMBERS: Dict[str, str] = {"1": "chat", "2": "contract", "3": "agent"}

#: [EN] Legacy numeric identifier to legacy mode name. This mapping is used only
#: [EN] to produce a helpful error; it does not switch modes or apply templates.
#: [EN] In the previous numbering, 2 meant summary and 6 meant creative; in the
#: [EN] current numbering, 2 means contract and 3 means agent. Automatic migration
#: [EN] of numbers would produce a state matching neither semantics. Numbers are
#: [EN] therefore accepted only for the current numbering; legacy numbers report a
#: [EN] migration hint, while unambiguous legacy mode names migrate automatically.
#: [ZH] 旧数字标识到旧模式名的映射。该映射仅用于生成明确的错误提示，不用于切换
#: [ZH] 模式或套用模板。旧编号中 2 表示 summary、6 表示 creative；当前编号中
#: [ZH] 2 表示 contract、3 表示 agent。自动迁移数字会产生既不匹配旧语义也不匹配
#: [ZH] 新语义的状态。因此数字仅接受当前编号；旧编号返回迁移提示，而无歧义的旧
#: [ZH] 模式名可自动迁移。
LEGACY_NUMBERS: Dict[str, str] = {
    "1": "general", "2": "summary", "3": "translate", "4": "code",
    "5": "contract", "6": "creative", "7": "guide",
}

#: [EN] Legacy mode name to temperature preset. Migration preserves the preset so
#: [EN] that response style does not change unexpectedly.
#: [ZH] 旧模式名到温度预设的映射。迁移时保留原预设，避免回答风格意外改变。
LEGACY_TEMPERATURES: Dict[str, float] = {
    "summary": 0.4, "translate": 0.3, "code": 0.2, "creative": 0.9, "guide": 0.6,
}

# [EN] Display abbreviation for each mode, used by /mode list.
# [ZH] 每个模式的展示缩写，用于 /mode list。
MODE_SHORT_NAMES: Dict[str, str] = {
    "chat": "c", "contract": "ct", "agent": "ag",
}

# [EN] Mode temperature presets. A manual /temp setting takes precedence.
# [ZH] 模式温度预设。用户通过 /temp 手动设置的值优先。
MODE_TEMPERATURES: Dict[str, float] = {
    "chat": 0.7,
    # [EN] Contract review prioritizes stability over creativity.
    # [ZH] 合同审查优先保证稳定性，而非创造性。
    "contract": 0.3,
    # [EN] The agent should use tools but not act erratically: 0.5 is more
    # [EN] exploratory than code (0.2) and more controlled than creative (0.9).
    # [ZH] 智能体应使用工具但不得行为失稳：0.5 比 code（0.2）更具探索性，
    # [ZH] 比 creative（0.9）更受约束。
    "agent": 0.5,
}


def canonical_mode(name: str) -> str:
    """
    [EN]
    Resolve any mode spelling to its canonical mode name, defaulting to chat.

    [ZH]
    把任意写法（编号/缩写/全称/旧模式名）解析成正式模式名；无效则回 chat。

    配置里可能存着旧名字（``default_mode: "summary"``），所以**加载路径也走它**，
    而不是只在 ``/mode`` 里迁移。
    """
    key = (name or "").strip().lower()
    return MODE_ALIASES.get(key, key if key in MODES else "chat")


def legacy_template(name: str) -> str:
    """
    [EN]
    Return the task template to apply for a legacy mode name.

    [ZH]
    旧**模式名**对应要套用的任务模板 id；不是旧名字则返回空串。

    刻意**不认旧编号**（见 ``LEGACY_NUMBERS`` 的说明）：编号在两套编号体系里会歧义，
    由 ``command_handler._mode`` 单独报错指路。
    """
    return LEGACY_MODES.get((name or "").strip().lower(), "")


def legacy_number_hint(token: str) -> str:
    """
    [EN]
    Return a replacement hint for a legacy numeric mode identifier.

    [ZH]
    旧编号 → 建议的替代写法（不是旧编号则返回空串）。

    ``/mode 6`` 这样的旧写法会得到"旧编号 6 曾指创意写作，现在请用 /task creative"，
    而不是默默切到一个语义不同的模式。
    """
    key = (token or "").strip().lower()
    if key not in LEGACY_NUMBERS:
        return ""
    old_name = LEGACY_NUMBERS[key]
    template = LEGACY_MODES.get(old_name, "")
    if old_name == "general":
        return "chat"
    if old_name == "contract":
        return "contract"
    return f"/task {template}" if template else ""


def legacy_note() -> str:
    """
    [EN]
    Return the migration note listing legacy mode names and templates.

    [ZH]
    ``/mode list`` 末尾的迁移说明（界面文本，由调用方取语言包渲染）。
    """
    return ", ".join(f"{name}→{template}" for name, template in LEGACY_MODES.items() if template)

# [EN] Intent detection for questions such as "what did I just say?".
# [ZH] “我刚才说了什么”等问题的意图识别。
REPEAT_INTENT = re.compile(
    r"(刚才|刚刚|之前|上一条|上句|前面).{0,8}(说|讲|问|输入|发).{0,6}(什么|啥)"
    r"|我说过什么|我说了什么|我刚刚说了什么",
    re.S,
)

# [EN] Legacy file-related intent pattern. File bodies are injected whenever a
# [EN] file is present; this pattern must no longer gate injection.
# [EN]
# [EN] The original gate caused a class of failures in which a user uploaded a
# [EN] file but the model reported that no file content was available. Natural
# [EN] questions such as "summarize this" or "who is the buyer" did not match
# [EN] the pattern, so the body was never injected. The current injection rule is
# [EN] "inject whenever ``conv.has_file()`` is true", bounded by
# [EN] ``config.files.total_chars``. The pattern is retained only for backwards
# [EN] compatibility with existing references.
# [ZH] 旧的文件意图正则。只要存在文件就注入正文；该正则不得再用于控制注入。
# [ZH]
# [ZH] 原闸门曾导致用户上传文件后模型声称看不到文件内容。诸如“帮我总结一下”
# [ZH] 或“甲方是谁”的自然提问不匹配该正则，正文因此从未被注入。当前规则为
# [ZH] “只要 ``conv.has_file()`` 为真就注入”，并由 ``config.files.total_chars``
# [ZH] 限制总长度。该正则仅为兼容既有引用而保留。
FILE_INTENT = re.compile(
    r"文件|文档|合同|条款|内容|里面|这个|上面|上文|上述|上传|审阅|基于|根据|摘要一下|读一下|看看",
    re.S,
)


# ===========================================================================
# [EN] Multi-file context
# [ZH] 多文件上下文
# ===========================================================================
#
# [EN] The implementation injects every file, subject to the
# [EN] ``config.files.total_chars`` budget. When the budget is insufficient,
# [EN] files uploaded later take precedence and the user is explicitly told
# [EN] which files were not injected. Local small models cannot reliably judge
# [EN] relevance, and using the model for relevance selection adds unstable
# [EN] inference cost. Files uploaded together usually belong to the same task,
# [EN] so complete injection with an explicit notice is preferable to guessing.
# [ZH] 实现采用“全部注入，但受 ``config.files.total_chars`` 预算约束”的策略。
# [ZH] 预算不足时，后上传的文件优先注入，并明确告知用户哪些文件未进入上下文。
# [ZH] 本地小型模型无法可靠判断相关性，使用模型进行相关性筛选会增加不稳定的
# [ZH] 推理成本。一次上传的文件通常属于同一任务，因此完整注入并明确提示，
# [ZH] 优于依赖猜测。

#: [EN] Files larger than this threshold are stored on disk; only a file pointer
#: [EN] and summary remain in the context. Large content should not be placed
#: [EN] verbatim in the context window, but a retrievable pointer must remain.
#: [ZH] 超过该阈值的文件会落盘保存；上下文中仅保留文件指针与摘要。大段内容
#: [ZH] 不应原样放入上下文窗口，但必须保留可回查的指针。
_DISK_THRESHOLD_CHARS = 20000


@dataclass
class FileEntry:
    """
    [EN]
    A loaded file: content, summary, and storage location.

    [ZH]
    已加载文件：内容、摘要与存储位置。
    """

    # [EN] File name used for display and context pointers.
    # [ZH] 用于展示与上下文指针的文件名。
    name: str = ""
    # [EN] Original path. It may be empty when a duplicate file reuses the first entry.
    # [ZH] 原始路径。同名文件去重后可能沿用首个条目，此时为空。
    path: str = ""
    # [EN] Full text. Empty when the content has been stored on disk.
    # [ZH] 全文。内容已落盘时为空。
    content: str = ""
    # [EN] Chunk summary. When present, the summary is injected in preference to the full text.
    # [ZH] 分段摘要。存在时优先注入摘要而非全文。
    summary: str = ""
    summary_meta: Dict = field(default_factory=dict)
    # [EN] Character count of the original text, still available after disk storage.
    # [ZH] 原文字符数，落盘后仍可显示体积。
    chars: int = 0
    # [EN] Disk path. Empty means the content remains in memory.
    # [ZH] 落盘路径。为空表示内容仍在内存中。
    stored_at: str = ""

    def inject_body(self) -> str:
        """
        [EN]
        Return the body used for context injection: summary first, then full
        text, then a disk pointer.

        [ZH]
        返回用于上下文注入的正文：优先摘要，其次全文，最后为磁盘指针。
        """
        if self.summary:
            return self.summary
        if self.content:
            return self.content
        if self.stored_at:
            return (f"（文件正文过大，已保存到 {self.stored_at}；"
                    f"共 {self.chars} 字符，需要原文时请让用户用 /file 重新上传或直接指定该路径）")
        return ""

    def to_dict(self) -> Dict:
        """
        [EN]
        Return the dictionary used for session persistence. The body is omitted;
        it is stored on disk or can be re-read from the source file.

        [ZH]
        返回用于会话持久化的字典。正文不写入；正文已落盘或可由源文件重新读取。
        """
        return {
            "name": self.name,
            "path": self.path,
            "summary": self.summary,
            "summary_meta": self.summary_meta,
            "chars": self.chars,
            "stored_at": self.stored_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "FileEntry":
        return cls(
            name=str(data.get("name", "")),
            path=str(data.get("path", "")),
            summary=str(data.get("summary", "")),
            summary_meta=data.get("summary_meta", {}) or {},
            chars=int(data.get("chars", 0) or 0),
            stored_at=str(data.get("stored_at", "")),
        )


class ConversationManager:
    """
    [EN]
    Manage multi-turn history, mode switching, file context, custom prompts, and
    session persistence.

    [ZH]
    管理多轮对话历史、模式切换、文件上下文、自定义提示词与会话持久化。
    """

    def __init__(self, max_history: int = 10, default_mode: str = "chat") -> None:
        self.max_history = max_history
        # [EN] Use canonical_mode rather than direct comparison: configuration and
        # [EN] session files may contain legacy mode names. A single migration
        # [EN] path is shared by loading and the /mode command.
        # [ZH] 使用 canonical_mode 而不是直接比较：配置与会话文件可能保存旧模式名。
        # [ZH] 加载路径与 /mode 命令共用同一条迁移逻辑。
        self.mode = canonical_mode(default_mode)
        self.history: List[dict] = []
        # [EN] ---------- Multiple files ----------
        # [ZH] ---------- 多文件 ----------
        # [EN] files is an ordered list in upload order; active_index identifies
        # [EN] the file used by /summarize, /status, and context pointers.
        # [ZH] files 为按上传顺序排列的有序列表；active_index 指向 /summarize、
        # [ZH] /status 与上下文指针所使用的当前文件。
        self.files: List[FileEntry] = []
        self.active_index: int = -1
        self.undo_stack: List[dict] = []
        # [EN] /prompt content: highest priority; replaces mode and task layers.
        # [ZH] /prompt 内容：优先级最高，替换模式层与任务层。
        self.custom_prompt = ""
        # [EN] /task task-layer text: layered on top of the mode block.
        # [ZH] /task 任务层正文：叠加在模式块之上。
        self.task_prompt = ""
        # [EN] Current task-template id, used by /status and guidance short-circuit checks.
        # [ZH] 当前任务模板 id，供 /status 与引导短路判断使用。
        self.task_template = ""
        # [EN] Automatic summary of earlier turns, produced by long-session compaction.
        # [ZH] 早期对话的自动摘要，由长会话压缩生成。
        self.summary = ""
        # [EN] ---------- Reasoning content ----------
        # [ZH] ---------- 思考内容 ----------
        # [EN] Backend injects these settings from config.reasoning:
        # [EN] persist_reasoning=False omits reasoning from history;
        # [EN] reasoning_max_chars limits retained reasoning per turn (0 = unlimited).
        # [ZH] Backend 根据 config.reasoning 注入这两个设置：
        # [ZH] persist_reasoning=False 时不将思考写入历史；
        # [ZH] reasoning_max_chars 限制每轮保留的思考长度（0 表示不限制）。
        self.persist_reasoning = True
        self.reasoning_max_chars = 8000

    # [EN] ---------- Legacy single-file compatibility proxies ----------
    # [ZH] ---------- 单文件兼容代理 ----------
    #
    # [EN] file_name, file_content, file_summary, and file_summary_meta are
    # [EN] referenced by commands, status display, summary triggers, session I/O,
    # [EN] and tests. Rewriting every call site would risk a runtime failure on
    # [EN] any missed reference. These properties map the legacy names to the
    # [EN] active file, preserving single-file semantics; new code uses files.
    # [ZH] file_name、file_content、file_summary 与 file_summary_meta 被指令、
    # [ZH] 状态显示、摘要触发、会话读写与测试引用。逐一改写调用点会因遗漏而引发
    # [ZH] 运行时错误。以下属性将旧字段映射到当前活动文件，保持单文件语义；
    # [ZH] 新代码应使用 files 列表。

    @property
    def _active(self) -> Optional[FileEntry]:
        if 0 <= self.active_index < len(self.files):
            return self.files[self.active_index]
        return None

    @property
    def file_name(self) -> str:
        entry = self._active
        return entry.path or entry.name if entry else ""

    @file_name.setter
    def file_name(self, value: str) -> None:
        # [EN] Support direct assignment from legacy callers and tests. When no
        # [EN] active file exists, create an empty entry for field-by-field setup.
        # [ZH] 兼容旧调用方与测试的直接赋值。没有活动文件时创建空条目，
        # [ZH] 以便逐字段构造。
        if self._active is None:
            self.files.append(FileEntry(name=os.path.basename(value or ""), path=value or ""))
            self.active_index = len(self.files) - 1
        else:
            self._active.path = value or ""
            self._active.name = os.path.basename(value or "")

    @property
    def file_content(self) -> str:
        entry = self._active
        return entry.content if entry else ""

    @file_content.setter
    def file_content(self, value: str) -> None:
        if self._active is None:
            self.files.append(FileEntry())
            self.active_index = len(self.files) - 1
        self._active.content = value or ""
        self._active.chars = len(self._active.content)

    @property
    def file_summary(self) -> str:
        entry = self._active
        return entry.summary if entry else ""

    @file_summary.setter
    def file_summary(self, value: str) -> None:
        if self._active is None:
            self.files.append(FileEntry())
            self.active_index = len(self.files) - 1
        self._active.summary = value or ""

    @property
    def file_summary_meta(self) -> Dict:
        entry = self._active
        return entry.summary_meta if entry else {}

    @file_summary_meta.setter
    def file_summary_meta(self, value: Dict) -> None:
        if self._active is None:
            self.files.append(FileEntry())
            self.active_index = len(self.files) - 1
        self._active.summary_meta = value or {}

    # [EN] ---------- Multi-file API ----------
    # [ZH] ---------- 多文件 API ----------

    def add_file(self, filepath: str, content: str) -> FileEntry:
        """
        [EN]
        Register a file and make it the active file.

        A file with the same name is treated as a re-upload: its content is
        replaced in place rather than appended as a second entry. Otherwise the
        same document could appear twice in the context, wasting window space
        and confusing the model. Replacement preserves the original position and
        moves the active pointer to it.

        [ZH]
        登记文件，并将其设为当前活动文件。

        同名文件视为重复上传：就地替换内容，而不是追加第二个条目。否则同一文档
        会在上下文中出现两次，浪费窗口并造成模型混淆。替换时保留原位置，并将
        活动指针指向该条目。
        """
        name = os.path.basename(filepath or "") or (filepath or "未命名")
        entry = FileEntry(name=name, path=filepath or "", content=content or "",
                          chars=len(content or ""))
        for index, existing in enumerate(self.files):
            if existing.name == name:
                self.files[index] = entry
                self.active_index = index
                return entry
        self.files.append(entry)
        self.active_index = len(self.files) - 1
        return entry

    def remove_file(self, index: int) -> bool:
        """
        [EN]
        Remove the file at ``index``. Individual files can be removed without
        clearing the rest of the context.

        [ZH]
        移除索引 ``index`` 处的文件。可单独移除某个文件，而不清空其余上下文。
        """
        if not (0 <= index < len(self.files)):
            return False
        self.files.pop(index)
        if not self.files:
            self.active_index = -1
        elif index < self.active_index:
            self.active_index -= 1
        elif index == self.active_index:
            self.active_index = min(index, len(self.files) - 1)
        return True

    def remove_file_by_name(self, name: str) -> bool:
        for index, entry in enumerate(self.files):
            if entry.name == name:
                return self.remove_file(index)
        return False

    def select_file(self, index: int) -> bool:
        """
        [EN]
        Select the active file used by commands such as /summarize.

        [ZH]
        切换当前活动文件；/summarize 等指令作用于该文件。
        """
        if 0 <= index < len(self.files):
            self.active_index = index
            return True
        return False

    def file_names(self) -> List[str]:
        return [entry.name for entry in self.files]

    def total_chars(self) -> int:
        return sum(entry.chars for entry in self.files)

    def has_file(self) -> bool:
        """
        [EN]
        Return whether at least one file was uploaded. This legacy API is
        equivalent to ``bool(self.files)``.

        [ZH]
        返回是否至少上传了一份文件。该旧 API 等价于 ``bool(self.files)``。
        """
        return bool(self.files)

    def set_entry_summary(self, entry: FileEntry, summary: str,
                          meta: Optional[Dict] = None) -> None:
        """
        [EN]
        Write a summary for the specified file. In multi-file mode, the target
        file must be explicit.

        [ZH]
        为指定文件写入摘要。在多文件模式下，必须显式指定目标文件。
        """
        entry.summary = summary or ""
        entry.summary_meta = meta or {}

    # [EN] ---------- Modes ----------
    # [ZH] ---------- 模式 ----------

    def set_mode(self, mode: str) -> bool:
        """
        [EN]
        Switch modes by number, abbreviation, or full name. Legacy mode names
        remain valid; see ``LEGACY_MODES``.

        [ZH]
        按编号、缩写或全称切换模式。旧模式名仍然有效；参见 ``LEGACY_MODES``。
        """
        key = (mode or "").strip().lower()
        if key not in MODE_ALIASES and key not in MODES:
            return False
        self.mode = canonical_mode(key)
        return True

    def migrated_template(self, mode: str) -> str:
        """
        [EN]
        Return the task-template id to apply together with a legacy mode name,
        or an empty string for a non-legacy name.

        ``/mode summary`` means "switch to chat and apply the summary template";
        the caller uses this value to decide whether to set the task layer.

        [ZH]
        返回与旧模式名一并套用的任务模板 id；非旧模式名返回空串。

        ``/mode summary`` 的语义是“切换到 chat 并套用 summary 模板”；调用方
        据此决定是否设置任务层。
        """
        return legacy_template(mode)

    @staticmethod
    def legacy_number_hint(token: str) -> str:
        """
        [EN]
        Return the replacement syntax for a legacy numeric identifier, used when
        reporting an error for forms such as ``/mode 4``.

        [ZH]
        返回旧数字标识的替代写法，用于 ``/mode 4`` 等旧写法的错误提示。
        """
        return legacy_number_hint(token)

    def get_mode(self) -> str:
        return self.mode

    @staticmethod
    def available_modes() -> Dict[str, str]:
        return dict(MODES)

    @staticmethod
    def mode_menu() -> str:
        """
        [EN]
        Build the mode list with numbers, abbreviations, and temperature presets.

        User-visible mode descriptions come from the language pack under
        ``mode.desc.<mode>``. The prompt bodies in MODES are sent to the model and
        intentionally remain Chinese; see the file header.

        [ZH]
        生成包含编号、缩写与温度预设的模式清单。

        用户可见的模式说明来自语言包的 ``mode.desc.<模式>``。MODES 中的提示词
        正文发送给模型，按设计保持中文；参见文件头部说明。
        """
        lines = []
        for idx, name in enumerate(MODES, 1):
            temp = MODE_TEMPERATURES[name]
            lines.append(_t("mode.menu_item", index=idx, id=name,
                            short=MODE_SHORT_NAMES[name],
                            desc=_t(f"mode.desc.{name}"), temp=temp))
        return "\n".join(lines)

    @staticmethod
    def legacy_mode_note() -> str:
        """
        [EN]
        Return the end-of-list note stating that legacy mode names remain valid.
        The text is user-visible and comes from the language pack.

        [ZH]
        返回列表末尾关于旧模式名仍然有效的提示。该文本面向用户，来自语言包。
        """
        pairs = ", ".join(f"{name} → /task {template}"
                          for name, template in LEGACY_MODES.items() if template)
        return _t("cmd.resp.mode_legacy", pairs=pairs)

    # [EN] ---------- History ----------
    # [ZH] ---------- 历史记录 ----------

    def add_message(self, role: str, content: str, reasoning: str = "") -> None:
        """
        [EN]
        Append a history record.

        ``reasoning`` is the reasoning content produced for this assistant turn.
        It is stored in a separate field rather than concatenated into
        ``content`` so that persisted sessions preserve the distinction and the
        reasoning does not become part of the answer or the model context (see
        :meth:`build_messages`).

        Empty reasoning is not written. Legacy sessions and sessions from models
        without reasoning remain unchanged, and session.json does not grow from
        empty string fields.

        [ZH]
        追加一条历史记录。

        ``reasoning`` 是该轮助手回复的思考内容。它存放在独立字段中，而不是拼接
        进 ``content``，以便持久化会话保留区分，且思考不会进入回答文本或模型
        上下文（参见 :meth:`build_messages`）。

        空思考内容不写入。旧会话与无思考模型的会话保持不变，session.json 也
        不会因空字符串字段而增大。
        """
        record: Dict[str, object] = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        trimmed = self._trim_reasoning(reasoning)
        if trimmed and self.persist_reasoning:
            record["reasoning"] = trimmed
        self.history.append(record)
        # [EN] Hard upper bound; under normal operation the backend compacts the
        # [EN] overflow into a summary first.
        # [ZH] 硬上限保护；正常情况下后端会先将超出部分压缩为摘要。
        if len(self.history) > self.max_history * 4:
            self.history = self.history[-self.max_history * 4 :]

    def _trim_reasoning(self, reasoning: str) -> str:
        """
        [EN]
        Truncate reasoning according to configuration, retaining the beginning
        and appending an elision marker.

        [ZH]
        按配置截断思考内容，保留开头并附加省略标记。
        """
        text = (reasoning or "").strip()
        limit = int(self.reasoning_max_chars or 0)
        if not text or limit <= 0 or len(text) <= limit:
            return text
        return text[:limit] + _t("export.reasoning_truncated")

    def last_user_message(self) -> Optional[str]:
        """
        [EN]
        Return the most recent user message, used by questions such as "what did
        I just say?".

        [ZH]
        返回最近一条用户消息，用于“我刚才说了什么”等问题。
        """
        for msg in reversed(self.history):
            if msg["role"] == "user":
                return msg["content"]
        return None

    def undo(self) -> bool:
        """
        [EN]
        Undo the last conversation turn, removing the user and assistant
        messages.

        [ZH]
        撤回最后一轮对话，移除用户消息与助手消息。
        """
        if len(self.history) < 2:
            return False
        self.undo_stack.append(self.history.pop())
        self.undo_stack.append(self.history.pop())
        return True

    def clear(self) -> None:
        self.history.clear()
        self.undo_stack.clear()
        self.files.clear()
        self.active_index = -1
        self.summary = ""

    def export_markdown(self) -> str:
        """
        [EN]
        Export conversation history as Markdown. Headings follow the active
        interface language.

        Reasoning is exported as a collapsible ``<details>`` block. The export
        must be complete while keeping reasoning visually distinct from the
        answer; concatenating reasoning before the answer makes the export read
        as off-topic, and omitting it silently removes content the user saw.

        [ZH]
        将对话历史导出为 Markdown。标题随当前界面语言变化。

        思考内容导出为可折叠的 ``<details>`` 块。导出必须完整，同时使思考与回答
        在视觉上可区分；将思考拼接在回答之前会使导出内容显得离题，而省略思考
        则等于静默删除用户看过的内容。
        """
        lines = [_t("export.title"), _t("export.mode", mode=self.mode), ""]
        for msg in self.history:
            icon = _t("export.user") if msg["role"] == "user" else _t("export.assistant")
            lines.append(f"**{icon}** ({msg['timestamp']})")
            thinking = str(msg.get("reasoning") or "")
            if thinking:
                lines.append("")
                lines.append(f"<details><summary>{_t('export.reasoning')}</summary>")
                lines.append("")
                lines.append(thinking)
                lines.append("")
                lines.append("</details>")
                lines.append("")
            lines.append(msg["content"])
            lines.append("")
        return "\n".join(lines)

    # [EN] ---------- Custom prompt and task layer ----------
    # [ZH] ---------- 自定义提示词与任务层 ----------

    def set_custom_prompt(self, text: str) -> None:
        """
        [EN]
        Set the ``/prompt`` custom instruction. It has the highest priority and
        replaces the mode block and task layer.

        [ZH]
        设置 ``/prompt`` 自定义指令。该指令优先级最高，替换模式块与任务层。
        """
        self.custom_prompt = text.strip()

    def set_task_prompt(self, text: str, template: str = "") -> None:
        """
        [EN]
        Set the ``/task`` task-layer text. It is layered after the mode block and
        does not replace the mode.

        Keeping it separate from ``set_custom_prompt`` ensures that ``/task off``
        removes only the task layer and does not clear a custom instruction set
        through ``/prompt``.

        ``template`` is the template id, such as ``"guide"``. After mode
        simplification, the active task template must be known so that
        ``/task guide`` behaves consistently with the former guide mode.

        [ZH]
        设置 ``/task`` 任务层正文。该文本叠加在模式块之后，不替换模式。

        与 ``set_custom_prompt`` 分开保存，使 ``/task off`` 仅移除任务层，不会
        清除用户通过 ``/prompt`` 设置的自定义指令。

        ``template`` 是模板 id，例如 ``"guide"``。模式简化后，必须知道当前任务
        模板，才能使 ``/task guide`` 与原有 guide 模式行为一致。
        """
        self.task_prompt = (text or "").strip()
        self.task_template = (template or "").strip() if self.task_prompt else ""

    # [EN] ---------- File context ----------
    # [ZH] ---------- 文件上下文 ----------

    def set_file(self, filepath: str, content: str) -> None:
        """
        [EN]
        Register and activate a file. This legacy API is equivalent to
        :meth:`add_file` and is retained for existing callers such as ``/file``,
        backend loading paths, and tests.

        [ZH]
        登记并激活文件。该旧 API 等价于 :meth:`add_file`，保留以兼容 ``/file``、
        后端加载路径与测试等既有调用方。
        """
        self.add_file(filepath, content)

    def set_file_summary(self, summary: str, meta: Optional[Dict] = None) -> None:
        """
        [EN]
        Record the chunk summary and metadata (chunk count, source character
        count, source pointer) for the active file.

        [ZH]
        记录当前活动文件的分段摘要与元信息（段数、原文字符数、原文指针）。
        """
        entry = self._active
        if entry is None:
            self.files.append(FileEntry(name="", path=""))
            self.active_index = len(self.files) - 1
            entry = self._active
        self.set_entry_summary(entry, summary, meta)

    # [EN] ---------- Prompt construction ----------
    # [ZH] ---------- 提示词构建 ----------

    def get_system_prompt(self) -> str:
        """
        [EN]
        Compose the effective system prompt. Layers are ordered by priority:

        1. ``/prompt`` custom instruction, which replaces the mode block and the
           task layer.
        2. Mode block (``MODES``).
        3. ``/task`` task layer, layered after the mode block and not replacing
           the mode.
        4. Shared rules (``_CORE_RULES``).
        5. Language instruction from ``reply_language_instruction()``.
        6. Early-conversation summary and uploaded-file pointers.

        The Chinese strings below are prompt content sent to the model, not
        interface text, so they intentionally remain outside the language pack.
        Only layer 5 follows the interface locale.

        [ZH]
        组合当前生效的系统提示词。各层按优先级排列：

        1. ``/prompt`` 自定义指令，替换模式块与任务层。
        2. 模式块（``MODES``）。
        3. ``/task`` 任务层，叠加在模式块之后，不替换模式。
        4. 共享规则（``_CORE_RULES``）。
        5. 来自 ``reply_language_instruction()`` 的语言指令。
        6. 早期对话摘要与已上传文件指针。

        下方中文字符串是发送给模型的提示词内容，而非界面文本，因此刻意不放入
        语言包。只有第 5 层跟随界面语言。
        """
        if self.custom_prompt:
            # [EN] Custom instructions take precedence and are wrapped in
            # [EN] stronger wording to improve adherence by small models. The
            # [EN] instruction deliberately preserves an exception: when
            # [EN] information is insufficient, answer the most likely
            # [EN] interpretation first and then ask for clarification, matching
            # [EN] shared rule 4.
            # [ZH] 自定义指令优先，并使用更明确的措辞，以提高小型模型的遵循度。
            # [ZH] 指令刻意保留例外：信息不足时，先回答最可能的解释，再请求澄清，
            # [ZH] 与共享规则第 4 条保持一致。
            prompt = (
                f"[自定义指令]\n{self.custom_prompt}\n\n"
                f"[执行要求] 你必须严格遵守上述自定义指令，直接输出用户要求的内容，"
                f"不要解释、不要偏离；但用户提供的信息不足时，也要先给出最可能的答案"
                f"并说明基于什么假设，再问要不要按其它情况展开。"
            )
        else:
            prompt = MODES.get(self.mode, MODES["chat"])
            if self.task_prompt:
                # [EN] The task layer is layered on the mode, not a replacement:
                # [EN] the mode defines scenario and tone, while the task layer
                # [EN] defines this turn's output format.
                # [ZH] 任务层叠加在模式之上，而不是替换模式：模式定义场景与语气，
                # [ZH] 任务层定义本轮的具体输出格式。
                prompt += "\n\n" + self.task_prompt
        prompt += _CORE_RULES
        prompt += reply_language_instruction()
        if self.summary:
            prompt += f"\n\n【早期对话摘要】\n{self.summary}"
        if self.files:
            # [EN] Keep only the file-name inventory, including the active file.
            # [EN] Full content is injected with the current question; the
            # [EN] inventory becomes multi-line in multi-file mode.
            # [ZH] 仅保留文件名清单，并标注当前活动文件。完整内容随当前问题注入；
            # [ZH] 多文件模式下清单为多行。
            prompt += self._file_pointer()
        return prompt

    def _file_pointer(self) -> str:
        """
        [EN]
        Return the uploaded-file inventory for the system prompt. Multi-file
        mode lists every file and marks the active file.

        [ZH]
        返回系统提示中的已上传文件清单。多文件模式逐条列出文件并标注当前活动文件。
        """
        if not self.files:
            return ""
        if len(self.files) == 1:
            entry = self.files[0]
            pointer = f"\n\n【已上传文件】: {entry.path or entry.name}"
            if entry.summary:
                meta = entry.summary_meta or {}
                chunks = meta.get("chunks")
                chars = meta.get("origin_chars")
                detail = f"（原文 {chars} 字符，已分 {chunks} 段摘要）" if chunks and chars else ""
                pointer += f"\n【长文档摘要已就绪】{detail} 该文档过长，提问时将注入分段摘要而非全文。"
            # [EN] File bodies are injected automatically with each question.
            # [EN] The prompt therefore states that the content is available
            # [EN] rather than instructing the model to ask the user to name the
            # [EN] file. The previous wording combined with an overly narrow
            # [EN] intent pattern and caused the model to claim that no content
            # [EN] had been provided.
            # [ZH] 文件正文会随每次提问自动注入。因此提示词直接说明内容可用，
            # [ZH] 而不是要求用户点名文件。此前措辞与过窄的意图正则叠加，曾导致
            # [ZH] 模型声称没有收到文件内容。
            pointer += "\n（该文件内容会随提问一并提供给你，直接按内容回答，不要反问用户是否上传过文件）"
            return pointer
        lines = [f"\n\n【已上传文件】共 {len(self.files)} 份（提问时如涉及文件内容会一并注入）："]
        for index, entry in enumerate(self.files):
            mark = "（当前文件）" if index == self.active_index else ""
            tail = ""
            if entry.summary:
                tail = "，已生成分段摘要"
            elif entry.stored_at:
                tail = "，正文过大已存盘"
            lines.append(f"{index + 1}. {entry.name}（{entry.chars} 字符{tail}）{mark}")
        lines.append("（如需基于某一份文件回答，请在问题里点名文件名）")
        return "\n".join(lines)

    def build_messages(self, user_query: str, search_context: str = "", include_file: bool = False,
                       retrieval_context: str = "") -> List[dict]:
        """
        [EN]
        Build the complete message list sent to the model: system prompt,
        history, and the current question.

        When ``include_file`` is true and files are present, file content is
        injected before the current question. A chunk summary is used when
        available; otherwise the full text is injected.

        Multi-file mode injects files individually, each preceded by a file label
        and constrained by ``config.files.total_chars``. Files uploaded later are
        placed first because they are usually the ones needed for the current
        request. Files that do not fit are omitted, and the caller reports them
        through :meth:`last_injection_note`. Silently omitting files is worse
        than reporting the omission.

        ``retrieval_context`` contains retrieved passages and locations produced
        by ``retrieval.Retriever`` and is placed before file blocks. The caller
        passes it only when retrieval is enabled and the document is long
        (stored on disk or summarized). Full-text injection is more accurate for
        short documents, and retrieval would discard information. This division
        of responsibility is documented in ``backend/retrieval.py``.

        The ``【{label}】`` and ``参考以下网络信息：`` prefixes are prompt content,
        not interface text, so they remain Chinese and outside the language pack.

        Reasoning is deliberately excluded from the model context. The
        ``reasoning`` field is for interface replay only:

        1. Reasoning is often longer than the answer; feeding it back consumes
           context window without helping the next turn, because the conclusion
           is already present in ``content``.
        2. The DeepSeek API explicitly requires that ``reasoning_content`` not be
           sent back in the next turn.
        3. DeepSeek-R1 distilled GGUF chat templates apply the same rule and
           accept only the answer portion.

        [ZH]
        构造发送给模型的完整消息列表：系统提示、历史记录与当前问题。

        ``include_file`` 为真且存在文件时，文件内容会注入到当前问题之前。有分段
        摘要时使用摘要，否则注入全文。

        多文件模式逐份注入文件，每份前带文件标签，并受
        ``config.files.total_chars`` 限制。后上传的文件优先放入，因为它们通常是
        当前请求所需材料。无法放入的文件会被省略，调用方通过
        :meth:`last_injection_note` 如实报告。静默省略文件比报告省略更糟。

        ``retrieval_context`` 包含 ``retrieval.Retriever`` 产出的相关片段与位置，
        位于文件块之前。仅当启用检索且文档为长文档（已落盘或已有摘要）时，调用方
        才传入该参数。短文档全文注入更准确，检索反而会丢弃信息。该分工记录在
        ``backend/retrieval.py`` 中。

        ``【{label}】`` 与 ``参考以下网络信息：`` 前缀属于提示词内容，不是界面
        文本，因此保持中文且不进入语言包。

        思考内容刻意不进入模型上下文。``reasoning`` 字段仅用于界面回放：

        1. 思考通常长于答案；回灌会占用上下文窗口，却对下一轮帮助很小，因为结论
           已存在于 ``content`` 中。
        2. DeepSeek API 明确要求下一轮不得回传 ``reasoning_content``。
        3. DeepSeek-R1 蒸馏版 GGUF 对话模板采用同一规则，只接受回答部分。
        """
        messages: List[dict] = [{"role": "system", "content": self.get_system_prompt()}]
        for msg in self.history:
            # [EN] Only role and content are model inputs; timestamp and
            # [EN] reasoning are excluded.
            # [ZH] 仅 role 与 content 属于模型输入；timestamp 与 reasoning 不参与。
            messages.append({"role": msg["role"], "content": msg["content"]})

        user_content = f"参考以下网络信息：\n{search_context}\n\n问题：{user_query}" if search_context else user_query
        if include_file and self.files:
            self._injection_note = ""
            blocks: List[str] = []
            budget = int(getattr(self, "files_total_chars", 0) or 0)
            used = 0
            skipped: List[str] = []
            total = len(self.files)
            # [EN] Fill from the most recent upload backwards so that newly
            # [EN] uploaded files enter the context first.
            # [ZH] 从最近上传的文件开始向前填充，使新上传文件优先进入上下文。
            order = list(range(total - 1, -1, -1))
            injected: Dict[int, str] = {}
            for index in order:
                entry = self.files[index]
                body = entry.inject_body()
                if not body:
                    continue
                label = ("文件内容（长文档摘要）" if entry.summary else "文件内容")
                head = f"【{label} {index + 1}/{total}：{entry.name}】" if total > 1 else f"【{label}】"
                block = f"{head}\n{body}"
                if budget > 0 and used + len(block) > budget and injected:
                    skipped.append(entry.name)
                    continue
                injected[index] = block
                used += len(block)
            for index in sorted(injected):
                blocks.append(injected[index])
            if skipped:
                self._injection_note = _t("sys.files_partial",
                                          total=total, injected=len(injected),
                                          skipped="、".join(skipped))
            elif total > 1:
                self._injection_note = _t("sys.files_all_injected",
                                          total=total, chars=used)
            if blocks:
                user_content = "\n\n".join(blocks) + "\n\n" + user_content
        if retrieval_context:
            # [EN] Retrieved passages precede all other material: the model sees
            # [EN] the located evidence first, then file summaries or full text,
            # [EN] and finally the question. This ordering supports citation
            # [EN] traceability.
            # [ZH] 检索片段位于所有前置材料之前：模型先看到定位到的依据，再看文件
            # [ZH] 摘要或全文，最后看到问题。该顺序支持引用溯源。
            user_content = f"{retrieval_context}\n\n{user_content}"
        messages.append({"role": "user", "content": user_content})
        return messages

    def last_injection_note(self) -> str:
        """
        [EN]
        Return the notice produced by the most recent multi-file injection in
        :meth:`build_messages`, or an empty string if none.

        When the total content exceeds the window, the implementation informs the
        user about omitted files instead of silently dropping them.

        [ZH]
        返回上一次 :meth:`build_messages` 多文件注入产生的提示；没有则返回空串。

        当总内容超出窗口时，实现会告知用户哪些文件被省略，而不是静默丢弃。
        """
        return str(getattr(self, "_injection_note", "") or "")

    # [EN] ---------- Session persistence ----------
    # [ZH] ---------- 会话持久化 ----------

    def save_session(self, path: str, model_path: str = "") -> None:
        """
        [EN]
        Save mode, prompts, summary, files, model, and history to a JSON file.

        In multi-file mode, ``files`` is stored as a list of dictionaries
        without file bodies. File bodies can be very large and would inflate
        session.json to many megabytes, while they can always be re-parsed from
        the source files. The persistent state is which files were uploaded and
        what summaries were produced.

        Legacy ``file_name`` and ``file_*`` fields remain for the active file so
        that older and newer versions can read each other's sessions.

        [ZH]
        将模式、提示词、摘要、文件、模型与历史保存到 JSON 文件。

        多文件模式下，``files`` 保存为字典列表，不写入文件正文。正文可能非常
        庞大，写入 session.json 会使文件膨胀到数十 MB，而正文始终可从源文件重新
        解析。需要持久化的是用户上传过哪些文件以及生成了哪些摘要。

        同时保留指向当前活动文件的 ``file_name`` 与 ``file_*`` 旧字段，使新旧
        版本能够互相读取会话。
        """
        data = {
            "mode": self.mode,
            "custom_prompt": self.custom_prompt,
            "task_prompt": self.task_prompt,
            "summary": self.summary,
            "model_path": model_path,
            "files": [entry.to_dict() for entry in self.files],
            "active_file": self.active_index,
            "file_name": self.file_name,
            "file_content": self.file_content,
            "file_summary": self.file_summary,
            "file_summary_meta": self.file_summary_meta,
            "history": self.history,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_session(self, path: str):
        """
        [EN]
        Restore a session from a JSON file.

        Returns (has_history, last_model_path).

        The file is read with ``utf-8-sig`` so that a BOM inserted by a text
        editor does not cause a decoding failure. Session files written by the
        application have no BOM, but manual editing may add one.

        Multi-file compatibility: new sessions contain a ``files`` list; legacy
        sessions contain scalar fields such as ``file_name`` and
        ``file_content``. Both forms are accepted, and legacy sessions are
        upgraded to a single-file list. File bodies are not persisted, so restored
        entries contain only name, size, and summary. Questions involving file
        content use the summary; if no summary was generated, the user must
        upload the file again.

        [ZH]
        从 JSON 文件恢复会话。

        返回 (是否存在历史, 上次使用的模型路径)。

        使用 ``utf-8-sig`` 读取，避免文本编辑器添加的 BOM 导致解码失败。应用写入
        的会话文件不带 BOM，但手工编辑可能引入 BOM。

        多文件兼容：新会话包含 ``files`` 列表；旧会话包含
        ``file_name``、``file_content`` 等标量字段。两种形式均被接受，旧会话会
        升级为单文件列表。文件正文不持久化，因此恢复后的条目仅包含名称、体积与
        摘要。涉及文件内容的提问使用摘要；若未生成摘要，则需要用户重新上传文件。
        """
        if not os.path.exists(path):
            return False, ""
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if data.get("mode") in MODES:
                self.mode = data["mode"]
            self.custom_prompt = data.get("custom_prompt", "")
            # [EN] Legacy sessions have no task_prompt field; task templates were
            # [EN] previously stored in custom_prompt. A missing field defaults to
            # [EN] an empty string, so legacy sessions still load correctly.
            # [ZH] 旧会话没有 task_prompt 字段；任务模板此前存放在
            # [ZH] custom_prompt 中。字段缺失时默认为空串，旧会话仍可正常加载。
            self.task_prompt = data.get("task_prompt", "")
            self.summary = data.get("summary", "")
            self.files = []
            raw_files = data.get("files")
            if isinstance(raw_files, list) and raw_files:
                for item in raw_files:
                    if isinstance(item, dict):
                        self.files.append(FileEntry.from_dict(item))
            else:
                name = str(data.get("file_name", "") or "")
                if name:
                    self.files.append(FileEntry(
                        name=os.path.basename(name), path=name,
                        summary=str(data.get("file_summary", "") or ""),
                        summary_meta=data.get("file_summary_meta", {}) or {},
                        chars=len(str(data.get("file_content", "") or "")),
                    ))
            active = data.get("active_file", -1)
            try:
                self.active_index = int(active)
            except Exception:
                self.active_index = -1
            if self.files and not (0 <= self.active_index < len(self.files)):
                self.active_index = 0
            if not self.files:
                self.active_index = -1
            self.history = data.get("history", [])
            return bool(self.history), str(data.get("model_path", ""))
        except Exception:
            return False, ""
