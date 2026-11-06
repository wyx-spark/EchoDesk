"""
[EN]
Task-oriented prompt templates for input-side optimization.

Small models are highly sensitive to prompt wording: for the same request, the
presence of a role definition, of examples, and of output-format constraints can
determine whether the result is usable at all. This module freezes validated
structured prompts into templates so that a single ``/task contract review``
invocation is sufficient, without requiring the user to study prompt engineering.

Four elements, provided wherever possible:
  1. A specific role, such as "a finance specialist reviewing procurement contract
     terms", rather than a generic "you are an assistant";
  2. Output-format constraints: plain text, field lists, or JSON explicitly
     required, so the model cannot improvise;
  3. Chain-of-thought guidance: reasoning tasks require listing evidence before
     drawing a conclusion, because small models benefit more from explicit
     scaffolding;
  4. Few-shot examples: one input/output pair, which measurably improves format
     adherence.

The template is appended after the mode prompt and does not replace the
conversation mode: the mode defines scenario and tone, while the template defines
the concrete output format for this turn. The template takes precedence on
conflict, and the ``[优先级]`` line at the end of ``build_system_prompt`` states
this explicitly, so the two layers do not issue conflicting instructions. The user
can still override everything with ``/prompt <text>``, since a custom prompt has
the highest priority.

Constraint strength
-------------------
Format requirements in the template body are conditional: short material does not
need bullet points, an API-usage question does not require code, and a contract
question without the contract text should first list common risks instead of
requesting the document. The closing line also has two variants
(``_RELAXED_ENDING`` / ``_STRICT_ENDING``): except for machine-readable templates
with ``relax=False`` (``extract``), the model is told explicitly that the level of
detail follows question complexity and need not satisfy every bullet. This is not
a weakening of the requirements; it corrects a side effect of the previous
version, where a single "strictly follow the format" instruction caused small
models such as 0.6B and 0.8B to apply the full template even to simple questions.

Localization
------------
* ``label`` and ``note`` are UI text shown by ``/task list``, ``/status``, and the
  settings UI. They are always read from the language pack under
  ``task.<id>.label`` / ``task.<id>.note``, so the template table does not store
  them; they are resolved through :func:`task_label` / :func:`task_note`.
* ``role`` / ``format`` / ``cot`` / ``examples`` remain in Chinese and are not
  localized: they are prompt-engineering content sent to the model, not UI text.
  The system prompt in ``conversation.py`` requires replies in Simplified Chinese,
  and translating template bodies into English would break the combination of a
  Chinese UI with English prompts. These phrasings are also designed specifically
  to suppress small-model hallucination (for example, "excerpts must match the
  source verbatim"), and translation weakens that effect.

[ZH]
任务型提示词模板（输入侧优化）。

小模型对提示词高度敏感：同一句话，是否包含角色设定、是否给出示例、是否有输出格式
约束，结果差距可能达到"可用/不可用"的程度。本模块把经过验证的结构化提示词固化为
模板，使用户通过一条 ``/task 合同审查`` 即可使用，无需自行研究提示词工程。

四个要素（模板中尽量齐备）：
  1. 具体角色：不是"你是助手"，而是"你是审查采购合同条款的财务专员"；
  2. 输出格式约束：明确要求纯文本、字段列表或 JSON，避免模型自由发挥；
  3. 思维链引导：推理类任务要求"先列依据再给结论"，小模型更需要外部脚手架；
  4. 少样本示例：给出 1 个输入输出样例，可显著提升格式遵循度。

注意：模板叠加在模式提示词之后，不替换对话模式：模式负责"场景与语气"，模板负责
"本轮的具体输出格式"；两者冲突时以模板为准（``build_system_prompt`` 末尾的
``[优先级]`` 行已显式写明，因此两层同时存在不会产生互相冲突的指令）。用户仍可用
``/prompt <文本>`` 覆盖全部提示词（自定义提示词优先级最高）。

约束强度
--------
模板正文中的格式要求改为条件式：材料较短时免于分点、询问 API 用法时不必给出代码、
尚未提供合同时先列出常见风险而不是索要材料。结尾行分为两档
（``_RELAXED_ENDING`` / ``_STRICT_ENDING``）：除 ``relax=False`` 的机器可读模板
（``extract``）外，均显式告知模型"详略按问题复杂度判断，不必凑满每一条"。这并非削弱
要求，而是修正上一版的副作用：仅有"严格遵守格式"一句时，0.6B/0.8B 等小模型会把
"简单问题"也套入完整模板。

多语言
------
* label / note 属于界面文本（``/task list``、``/status`` 与设置界面会显示），
  一律从语言包读取（键 ``task.<id>.label`` / ``task.<id>.note``），因此模板表中不再
  存储这两项；取值时经 :func:`task_label` / :func:`task_note` 解析。
* role / format / cot / examples 保持中文，不进入语言包：它们是发送给模型的提示词
  工程内容，而非界面文本。本项目 ``conversation.py`` 的系统提示词要求"请始终使用
  简体中文回复"，将模板正文译为英文会使"中文界面 + 英文提示词"的组合失效；这些措辞
  也是针对小模型幻觉专门设计的（例如"摘录必须与原文逐字一致"），翻译会削弱效果。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# [EN] Localization: i18n depends only on the standard library (config is imported
# lazily), so it can be imported directly here.
# [ZH] 多语言：i18n 仅依赖标准库（config 为惰性导入），可直接在此处导入。
from i18n import t as _t

#: [EN] Task templates: id -> {role, format, cot, examples}. For label / note, see
#: the module docstring: they are UI text stored in the language pack under
#: task.<id>.*.
#: [ZH] 任务模板：id → {role, format, cot, examples}。label / note 见文件头说明：
#: 它们属于界面文本，存放在语言包的 task.<id>.* 中。
TASK_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "summary": {
        "role": "你是资深会议记录员，擅长把冗长材料压缩成不丢关键信息的要点。",
        "format": (
            "输出要求：\n"
            "1. 材料短（200 字以内）时用一段话说清结论即可，不必分点；\n"
            "2. 长材料先一句话给出全文结论，再用不超过 8 条要点列出关键信息，每条不超过 40 字；\n"
            "3. 数字、日期、金额、当事人名称必须原样保留；\n"
            "4. 材料太短或信息不足时直接说「内容不足以摘要」，不要硬凑；\n"
            "5. 只输出纯文本，不要加开场白、不要复述我的要求。"
        ),
        "cot": "先在心里按「时间—主体—动作—结果」梳理，再输出结论与要点。",
        "examples": [
            {"in": "（一段 3000 字的采购合同正文）",
             "out": "结论：本单为分三批交付的设备采购，逾期按日 0.3% 计违约金。\n"
                    "1. 买方：××公司；卖方：××科技\n"
                    "2. 金额：人民币 128.6 万元\n"
                    "3. 交付：分三批，首批不晚于 2026-03-15\n"
                    "4. 违约金：每逾期一日按合同额 0.3% 计\n"
                    "5. 质保期：验收后 24 个月"},
        ],
    },
    "contract": {
        "role": "你是审查采购合同条款的财务专员，只关注钱、时间与责任三类风险。",
        "format": (
            "输出要求：\n"
            "1. 我还没给合同正文时，先列出这类合同最常见的 3~5 条风险点（各一句），"
            "再问我是哪一条或哪种情况；不要只回「请提供合同全文」；\n"
            "2. 有正文时按【付款条件】【交付期限】【违约责任】【争议解决】【缺失条款】逐项写，"
            "每条格式为「原文摘录 → 风险点 → 修改建议」，摘录必须与原文逐字一致；\n"
            "3. 涉及重大金额、付款或责任承担时，必须提示「建议人工复核」；\n"
            "4. 找不到对应条款时写「未见相关约定」，不要臆测。"
        ),
        "cot": "先通读并定位上述五类条款，逐条摘录原文，最后再判断风险；没有原文依据的结论一律不写。",
        "examples": [
            {"in": "乙方应在收到全部货物并验收合格后 30 个工作日内支付款项。",
             "out": "【付款条件】原文摘录：验收合格后 30 个工作日内支付 → 风险点："
                    "付款前置条件包含「验收合格」但未约定验收期限 → 修改建议："
                    "补充「甲方应在收货后 5 个工作日内完成验收，逾期视为合格」。"},
        ],
    },
    "extract": {
        "role": "你是发票与单据信息抽取器，只做抄录与归类，不做任何推断。",
        "format": (
            "输出要求：只输出 JSON，不要任何解释文字、不要 Markdown 代码块标记。\n"
            '字段固定为：{"金额": "", "币种": "", "日期": "", "编号": "", "对方名称": ""}\n'
            "原文没有的字段填 null，绝不猜测。"
        ),
        "cot": "先定位数字与日期所在句子，再逐字段抄录，抄完检查是否原样一致。",
        # [EN] Machine-readable formats are not relaxed: this JSON is parsed by
        # programs, and allowing the model to choose the level of detail by question
        # complexity would permit prose output.
        # [ZH] 机器可读格式不作放松：此处的 JSON 会被程序解析，允许模型"按复杂度自行
        # 判断详略"等同于允许其输出散文。
        "relax": False,
        "examples": [
            {"in": "发票号码 00123456，价税合计人民币 12,860.00 元，开票日期 2026-03-01。",
             "out": '{"金额": "12860.00", "币种": "CNY", "日期": "2026-03-01", '
                    '"编号": "00123456", "对方名称": null}'},
        ],
    },
    "translate": {
        "role": "你是专业笔译，母语级的简体中文与英文，重视术语一致与语气贴合。",
        "format": (
            "输出要求：\n"
            "1. 只输出译文，不要解释、不要加引号；\n"
            "2. 保留原文的段落结构与编号；\n"
            "3. 专有名词、代码、公式原样保留；\n"
            "4. 无法确定译法的术语，用「译文（原文）」标注；\n"
            "5. 数字、金额、日期不得改动；\n"
            "6. 语气随文体分层：合同严谨、技术中性、口语自然；\n"
            "7. 除非我明确问，不要解释你为什么这样译。"
        ),
        "cot": "先判断文体（合同/技术文档/口语），再统一术语，最后落笔。",
        "examples": [
            {"in": "The Buyer shall pay within 30 days after acceptance.",
             "out": "买方应在验收后 30 日内付款。"},
        ],
    },
    "code": {
        "role": "你是严谨的工程师，回答以「可运行、可验证」为准。",
        "format": (
            "输出要求：\n"
            "1. 先给结论（问题出在哪一行/哪个函数，或这个 API 怎么用）；\n"
            "2. 问用法、参数含义、报错原因这类问题时，一两句答完即可，不必给代码；\n"
            "3. 需要改动时，再给修改后的最小代码片段（带语言标记的代码块）+ 一条可执行的验证方法；\n"
            "4. 不确定的地方明确写「需要确认」，不要编 API。"
        ),
        "cot": "先按输入→处理→输出逐步复述代码在做什么，再定位偏差。",
        "examples": [
            {"in": "for i in range(len(a)):\n    print(a[i+1])",
             "out": "结论：索引越界，最后一次循环取到 a[len(a)]。\n"
                    "```python\nfor i in range(len(a) - 1):\n    print(a[i + 1])\n```\n"
                    "验证：a = [1, 2]，修改后输出 2，不再抛 IndexError。"},
        ],
    },
    "reason": {
        "role": "你是逻辑严谨的分析助手，结论必须能追溯到给出的依据。",
        "format": (
            "输出要求（信息不足时也要先给一个带假设的回答，不要只追问）：\n"
            "【已知条件】逐条列出（只列我提供的信息）\n"
            "【假设】信息不全时，写明你按什么假设作答；信息完整时写「无」\n"
            "【推理过程】一步步写，每步不超过一句\n"
            "【结论】一句话\n"
            "【不确定之处】写清缺什么信息、补上后结论会怎么变；标明哪些是推测"
        ),
        "cot": "严禁跳步。信息不足时先按最可能的情况给出结论并标注假设，最后再问是否需要按其它情况重算，"
               "而不是把问题退回去。",
        "examples": [
            {"in": "甲比乙大 3 岁，乙比丙大 2 岁，丙 10 岁，甲多大？",
             "out": "【已知条件】甲=乙+3；乙=丙+2；丙=10\n"
                    "【假设】无\n"
                    "【推理过程】乙=10+2=12；甲=12+3=15\n【结论】甲 15 岁\n【不确定之处】无"},
        ],
    },
    # ---- [EN] The next two templates were migrated from removed modes. ----
    # The summary, translate, and code templates already existed (their content was
    # completed with the wording of the removed modes); creative and guide had no
    # templates, so they are defined here to ensure no capability is lost after the
    # mode merge.
    # ---- [ZH] 下面两个模板由被移除的模式迁移而来。----
    # summary/translate/code 三个模板原先已存在（内容已按被移除模式的措辞补全）；
    # creative 与 guide 原先没有模板，故在此新建，以保证模式合并后能力没有缺失。
    "creative": {
        "role": "你是创意写作伙伴，擅长故事、诗歌、文案与剧本，敢于给有辨识度的写法。",
        "format": (
            "输出要求：\n"
            "1. 直接给成稿，不写创作说明（除非我明确问）；\n"
            "2. 需要多个方案时给 2~3 个就收住，不要一路发散，每个用一句话标出特色；\n"
            "3. 不抄袭、不套用知名作品的句子；涉及真实人物或事件时只做虚构化处理；\n"
            "4. 内容不合适时说明原因并给替代写法，而不是照做。"
        ),
        "cot": "先定体裁、视角与情绪基调，再动笔；避开套话与陈词。",
        "examples": [
            {"in": "写一句咖啡广告",
             "out": "醒着的理由，不止一个。"},
        ],
    },
    "guide": {
        "role": "你是需求澄清助手：用户说不清自己想要什么时，帮他找到方向。",
        "format": (
            "输出要求：\n"
            "1. 列出 2~3 种最可能的理解，每种配一句「你会怎么处理」；\n"
            "2. 每种只说一句话，不在用户选定前展开完整方案；\n"
            "3. 最后问用户选哪一个，不替他决定；\n"
            "4. 方向必须来自用户确实提到的线索，不要编造他没说过的事。"
        ),
        "cot": "先从用户的只言片语里提取可能的目标、对象和想要的产出形式，再据此列方向。",
        "examples": [
            {"in": "帮我看看这个",
             "out": "1) 看文件内容有没有问题 2) 看某段文字怎么改 3) 看代码为什么报错。你指哪一种？"},
        ],
    },
}

#: [EN] Ending for machine-readable templates (``relax=False``): it omits the
#: "detail level is discretionary" sentence and continues to require strict format
#: adherence. Extracted values are consumed by programs and must not vary with model
#: discretion.
#: [ZH] 机器可读格式模板（``relax=False``）的结尾：不写"详略自定"那一句，而是继续
#: 要求严格遵守格式。抽取结果由程序解析，不能随模型的自由裁量而变形。
_STRICT_ENDING = (
    "[重要] 严格遵守上述输出格式；不要输出与本任务无关的内容。"
)
#: [EN] Ending for the remaining templates: format takes precedence, but the level
#: of detail remains a model judgment. The previous version contained only "strictly
#: follow the output format above", which caused small models to apply the full
#: format to every question (a question about API usage also received a code block
#: plus verification steps).
#: [ZH] 其余模板的结尾：格式优先，但详略仍由模型判断。上一版此处仅有"严格遵守上述
#: 输出格式"一句，小模型据此对任何问题都套用完整格式（用户询问"这个 API 怎么用"时
#: 也会返回一段代码及验证步骤）。
_RELAXED_ENDING = (
    "[重要] 本任务的格式要求优先；但详略仍按问题复杂度判断——"
    "简单问题可以只答一句话，不必凑满上面每一条。"
)


def task_label(tid: str) -> str:
    """
    [EN]
    Display name of a task template. This is UI text resolved from the language pack
    for the active language.
    [ZH]
    任务模板的显示名（界面文本，按当前语言从语言包读取）。
    """
    return _t(f"task.{tid}.label")


def task_note(tid: str) -> str:
    """
    [EN]
    One-line description of a task template. This is UI text resolved from the
    language pack for the active language.
    [ZH]
    任务模板的一句话说明（界面文本，按当前语言从语言包读取）。
    """
    return _t(f"task.{tid}.note")


def _view(tid: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """
    [EN]
    Display copy of a template with localized label and note added.

    Resolution is deliberately deferred to access time rather than calling
    ``_t(...)`` inside ``TASK_TEMPLATES``: a module-level dictionary is evaluated at
    import time, which would freeze the language at that moment, so a later
    ``set_language`` call switching to English would have no effect.

    [ZH]
    模板的可显示副本，补充本地化的 label / note。

    刻意在取值时才解析，而不是在 ``TASK_TEMPLATES`` 中直接写 ``_t(...)``：模块级字典
    在导入时求值，会把语言固化在导入时刻，之后 ``set_language`` 切换到英文也不会生效。
    """
    out = dict(item)
    out["id"] = tid
    out["label"] = task_label(tid)
    out["note"] = task_note(tid)
    return out


def _find(name: str) -> Optional[str]:
    """
    [EN]
    Resolve a template id by exact id, by label in the active language, or by
    prefix. Returns None when no template matches.
    [ZH]
    按 id、当前语言下的标签或前缀查找模板 id；找不到时返回 None。
    """
    key = (name or "").strip()
    if not key:
        return None
    if key in TASK_TEMPLATES:
        return key
    lowered = key.lower()
    for tid in TASK_TEMPLATES:
        if tid.lower() == lowered or task_label(tid) == key:
            return tid
    # [EN] Prefix matching reduces the amount of typing required.
    # [ZH] 前缀匹配，减少用户输入量。
    for tid in TASK_TEMPLATES:
        if tid.startswith(lowered):
            return tid
    return None


def template_ids() -> List[str]:
    return list(TASK_TEMPLATES.keys())


def get_template(name: str) -> Optional[Dict[str, Any]]:
    """
    [EN]
    Return a template by id or by its label in the active UI language. The returned
    mapping includes localized label and note.
    [ZH]
    按 id 或当前界面语言下的标签获取模板；返回值包含本地化的 label / note。
    """
    tid = _find(name)
    return None if tid is None else _view(tid, TASK_TEMPLATES[tid])


def build_system_prompt(name: str, include_examples: bool = True) -> str:
    """
    [EN]
    Assemble a template into a Chinese instruction block that can be inserted
    directly into the system prompt.

    An empty string means the template does not exist; callers use it to emit a
    "template not found" message.

    The framing text (``[任务模式]`` / ``[推理要求]`` / ``[示例]`` / ``[重要]``),
    like the template body, is prompt content rather than UI text. It therefore
    stays in Chinese and is not localized. The template name uses the label for the
    active language so that users can match the UI name with the prompt content.

    The closing line is one of :data:`_RELAXED_ENDING` or :data:`_STRICT_ENDING`;
    machine-readable templates are not relaxed, as described at
    ``_STRICT_ENDING``.

    [ZH]
    将模板拼接为一段可直接插入系统提示词的中文说明。

    返回空串表示模板不存在（调用方据此给出"未找到模板"的提示）。

    拼接使用的框架文字（``[任务模式]`` / ``[推理要求]`` / ``[示例]`` / ``[重要]``）
    与模板正文一样属于提示词内容而非界面文本，因此保持中文、不进入语言包；模板名取
    当前语言的 label，便于用户将界面上的名称与提示词对应。

    结尾行从 :data:`_RELAXED_ENDING` / :data:`_STRICT_ENDING` 中二选一（机器可读
    格式的模板不作放松，见 ``_STRICT_ENDING`` 的说明）。
    """
    tid = _find(name)
    if tid is None:
        return ""
    template = TASK_TEMPLATES[tid]
    parts: List[str] = [f"[任务模式] {task_label(tid)}", str(template["role"]), str(template["format"])]
    cot = str(template.get("cot") or "").strip()
    if cot:
        parts.append(f"[推理要求] {cot}")
    if include_examples:
        examples = template.get("examples") or []
        if examples:
            lines = ["[示例]"]
            for index, example in enumerate(examples, 1):
                lines.append(f"输入{index}：{example.get('in', '')}")
                lines.append(f"输出{index}：{example.get('out', '')}")
            parts.append("\n".join(lines))
    parts.append(
        "[优先级] 本节是本次任务的具体要求：如果它与前面模式要求的输出格式冲突，"
        "以本节为准；模式的角色、语气与边界要求继续有效。"
    )
    parts.append(_STRICT_ENDING if template.get("relax") is False else _RELAXED_ENDING)
    return "\n\n".join(parts)


def menu_text() -> str:
    """
    [EN]
    List displayed by ``/task list``. Label and note follow the active UI language.
    [ZH]
    ``/task list`` 的展示清单（label / note 随界面语言切换）。
    """
    lines = []
    for tid in TASK_TEMPLATES:
        lines.append(_t("task.menu_item", id=tid, label=task_label(tid), note=task_note(tid)))
    return "\n".join(lines)


def public_list() -> List[Dict[str, Any]]:
    """
    [EN]
    Machine-readable list for the settings window and frontend dropdowns.

    Label and note are localized UI text; role, format, and cot are prompt bodies
    and remain in Chinese by design (see the module docstring).

    [ZH]
    机器可读清单（供设置窗口与前端下拉框使用）。

    label / note 为本地化后的界面文本；role / format / cot 为提示词正文，按设计保持
    中文（见文件头说明）。
    """
    return [
        {
            "id": tid,
            "label": task_label(tid),
            "role": item["role"],
            "format": item["format"],
            "cot": item.get("cot", ""),
            "note": task_note(tid),
            "example_count": len(item.get("examples") or []),
            # [EN] Machine-readable formats (such as the extract JSON) do not take
            # part in the "detail level is discretionary" relaxation, so the UI and
            # diagnostics can identify templates with a strict format.
            # [ZH] 机器可读格式（如 extract 的 JSON）不参与"详略自定"的放松，界面与
            # 诊断信息据此可识别哪些模板使用硬性格式。
            "relax": item.get("relax") is not False,
        }
        for tid, item in TASK_TEMPLATES.items()
    ]


# ---------------------------------------------------------------------------
# [EN] Vague-question detection: ask for clarification when information is
# insufficient instead of guessing. Clarification is presented as guidance that
# lists two or three possible directions for the user to choose from.
# [ZH] 模糊提问检测：信息不足时追问，而不是勉强回答。追问以引导形式呈现，
# 列出 2~3 种可能方向供用户选择。
# ---------------------------------------------------------------------------

#: [EN] Characteristics of clearly under-specified questions: very short and
#: consisting only of pronouns or generic verbs. These are matching rules over user
#: input, comparable to a regular expression, not display text, so they are not
#: localized. What the UI actually shows is the guidance text itself, under the key
#: task.clarify_prompt.
#: [ZH] 明显信息不足的提问特征（很短，且仅含指代词或泛化动词）。这是对用户输入的
#: 匹配规则（与正则同类），并非显示文本，因此不进入语言包；界面上实际展示的是引导
#: 话术本身（键 task.clarify_prompt）。
_VAGUE_PATTERNS = (
    "这个", "那个", "它", "怎么办", "怎么弄", "搞一下", "看一下", "帮我弄",
    "弄一下", "处理下", "处理一下", "优化下", "改一下",
)

_MIN_MEANINGFUL_CHARS = 6


def is_vague_question(text: str) -> bool:
    """
    [EN]
    Decide whether a question is under-specified enough to require clarification.

    The check is deliberately conservative: a question is considered vague only when
    it is both very short and contains a pronoun or generic verb, so that normal
    short questions (for example "1+1=?") are not intercepted. When this returns
    True, the caller should ask for clarification instead of answering.

    [ZH]
    判断提问是否信息不足到必须先追问的程度。

    判断刻意保守：仅在同时满足"很短"与"含指代词/泛化动词"时才判定为模糊，避免把
    正常短问题（如"1+1=?"）也拦截下来。返回 True 时，调用方应追问而不是勉强回答。
    """
    stripped = (text or "").strip()
    if len(stripped) > 20:
        return False
    core = "".join(ch for ch in stripped if ch.isalnum())
    if len(core) > _MIN_MEANINGFUL_CHARS:
        return False
    return any(pattern in stripped for pattern in _VAGUE_PATTERNS)


#: [EN] Guidance directions: (id, trigger keywords). Keywords for different
#: languages are mixed together because users may write in Chinese and English, while
#: the direction text always comes from the language pack under
#: ``task.guide_dir.<id>``, so no Chinese leaks into an English UI (asserted by
#: ``tools/test_capability.py``).
#:
#: Keywords are used instead of asking the model to generate directions because this
#: path does not invoke the model (see the vague-question short circuit in
#: ``main.py``) and is a zero-cost, deterministic fallback. Model-generated
#: directions belong to the guide mode (``conversation.MODES["guide"]``); the
#: division of responsibility is documented in the records.
#: [ZH] 引导方向：(id, 触发关键词)。不同语言的关键词混合存放，因为用户可能中英混写；
#: 而方向文案一律从语言包读取（键 ``task.guide_dir.<id>``），因此英文界面下不会出现
#: 中文（``tools/test_capability.py`` 有断言保障）。
#:
#: 使用关键词而不是让模型即时生成方向的原因：该路径不调用模型（见 ``main.py`` 的
#: 模糊提问短路逻辑），是零成本、确定性的兜底方案；由模型生成方向的实现保留在引导
#: 模式（``conversation.MODES["guide"]``）中，两者的职责划分记录在 records 文档中。
_GUIDE_DIRECTIONS = (
    ("object", ("文件", "文档", "合同", "表格", "图片", "段落", "条款",
                "file", "doc", "pdf", "excel", "word")),
    ("diagnose", ("报错", "错误", "失败", "打不开", "崩溃", "卡", "闪退", "乱码",
                  "error", "fail", "crash", "bug")),
    ("judge", ("有没有问题", "有问题", "行不行", "合不合适", "风险", "靠谱", "对不对",
               "ok", "risk", "valid")),
    ("write", ("写", "起草", "文案", "邮件", "总结", "周报", "润色", "翻译",
               "write", "draft", "mail", "translate")),
    ("howto", ("怎么办", "怎么做", "怎么弄", "如何", "步骤", "流程", "教程",
               "how", "step")),
)

#: [EN] Fallback direction order when no keyword matches, covering the three most
#: common request categories.
#: [ZH] 关键词未命中时使用的兜底方向顺序（覆盖面最广的三类诉求）。
_GUIDE_DEFAULT = ("object", "howto", "diagnose")

#: [EN] Number of directions listed in the guidance text: three, which provides
#: adequate coverage without overwhelming the user.
#: [ZH] 引导话术中列出的方向数量：取 3 种，既能覆盖主要可能，又不至于让用户难以选择。
_GUIDE_MAX = 3


def guide_direction_ids(text: str, limit: int = _GUIDE_MAX) -> List[str]:
    """
    [EN]
    Select the direction ids to display for the user's message. Keyword matches take
    precedence, and the default order fills any remaining slots.

    Returns exactly ``limit`` distinct ids. This is a pure function, which keeps it
    straightforward to unit test.

    [ZH]
    按用户输入挑选需要展示的方向 id（关键词命中优先，数量不足时用默认顺序补齐）。

    返回恰好 limit 个不重复的 id；为纯函数，便于单元测试。
    """
    lowered = (text or "").lower()
    picked: List[str] = []
    for direction_id, keywords in _GUIDE_DIRECTIONS:
        if any(keyword in lowered for keyword in keywords):
            picked.append(direction_id)
    for fallback in _GUIDE_DEFAULT:
        if len(picked) >= limit:
            break
        if fallback not in picked:
            picked.append(fallback)
    for direction_id, _ in _GUIDE_DIRECTIONS:
        if len(picked) >= limit:
            break
        if direction_id not in picked:
            picked.append(direction_id)
    return picked[:limit]


def clarification_prompt(text: str) -> str:
    """
    [EN]
    Generate the guidance text that lists two or three possible interpretations and
    asks the user to choose one.

    This text is displayed as an assistant message in the conversation, so it is
    taken entirely from the language pack: the opening and closing text is
    ``task.clarify_prompt`` (with a ``{directions}`` placeholder), and each
    direction is ``task.guide_dir.<id>``.

    The previous version asked the user to supply three items (object, output
    format, constraints), which placed the burden of articulating the request back
    on a user who could not do so. Presenting concrete directions reduces the
    response to a single number.

    [ZH]
    生成引导话术：列出 2~3 种可能的理解方向，让用户选择其中一个。

    该文本作为助手消息显示在对话框中，因此整段取自语言包：开场与收尾为
    ``task.clarify_prompt``（带 ``{directions}`` 占位符），每条方向为
    ``task.guide_dir.<id>``。

    上一版要求用户补充三点信息（对象/输出形式/约束），实际上把组织语言的责任又推回给
    表达不清的用户。改为给出具体方向后，用户只需回复一个数字。
    """
    directions = guide_direction_ids(text)
    lines = [f"{index}. {_t(f'task.guide_dir.{direction_id}')}"
             for index, direction_id in enumerate(directions, 1)]
    return _t("task.clarify_prompt", directions="\n".join(lines))
