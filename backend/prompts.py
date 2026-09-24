"""任务型提示词模板（任务书修复四 方向一：输入侧优化）。

小模型对提示词高度敏感：同一句话，加不加角色设定、要不要示例、有没有输出格式
约束，结果差距能到「能用/不能用」的程度。这里把经过验证的结构化提示词固化成模板，
让用户一句 ``/task 合同审查`` 就能用上，而不必自己研究提示词工程。

四个要素（模板里尽量齐备）：
  1. **具体角色**——不是「你是助手」，而是「你是审查采购合同条款的财务专员」；
  2. **输出格式约束**——明确要求纯文本/字段列表/JSON，避免模型自由发挥；
  3. **思维链引导**——推理类任务要求「先列依据再给结论」（小模型更需要外部脚手架）；
  4. **少样本示例**——给 1 个输入输出样例，显著提升格式遵循度。

注意：模板是**附加在系统提示词后面**的，不替换对话模式；用户仍可用
``/prompt <文本>`` 覆盖一切（自定义提示词优先级最高）。

多语言（任务三）
----------------
* **label / note 是界面文本**（``/task list``、``/status`` 与设置界面会显示），
  一律从语言包取（键 ``task.<id>.label`` / ``task.<id>.note``），所以模板表里
  不再存这两项；取值时经 :func:`task_label` / :func:`task_note` 解析。
* **role / format / cot / examples 保持中文，不进语言包**：它们是发给模型的
  **提示词工程内容**，不是界面文本。本项目 ``conversation.py`` 的系统提示词里
  要求「请始终使用简体中文回复」，把模板正文译成英文会让"中文界面 + 英文提示词"
  这种组合失效；而且这些措辞是针对小模型幻觉专门设计的（例如「摘录必须与原文
  逐字一致」），翻译会削弱效果。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 多语言（任务三）：i18n 只依赖标准库（config 是惰性导入的），可直接在这里导入
from i18n import t as _t

#: 任务模板：id → {role, format, cot, examples}
#: label / note 见文件头说明：那是界面文本，存在语言包 task.<id>.* 里。
TASK_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "summary": {
        "role": "你是资深会议记录员，擅长把冗长材料压缩成不丢关键信息的要点。",
        "format": (
            "输出要求：\n"
            "1. 先用一句话给出全文结论；\n"
            "2. 再用不超过 8 条要点列出关键信息，每条不超过 40 字；\n"
            "3. 数字、日期、金额、当事人名称必须原样保留；\n"
            "4. 只输出纯文本，不要加开场白、不要复述我的要求。"
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
            "输出要求（逐项列出，不要写小作文）：\n"
            "【付款条件】【交付期限】【违约责任】【争议解决】【缺失条款】\n"
            "每条格式为「原文摘录 → 风险点 → 修改建议」，摘录必须与原文逐字一致。\n"
            "找不到对应条款时写「未见相关约定」，不要臆测。"
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
            "4. 无法确定译法的术语，用「译文（原文）」标注。"
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
            "1. 先给结论（问题出在哪一行/哪个函数）；\n"
            "2. 再给修改后的最小代码片段（带语言标记的代码块）；\n"
            "3. 最后说明如何验证（一条可执行的命令或一个测试输入）；\n"
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
            "输出要求：\n"
            "【已知条件】逐条列出（只列我提供的信息）\n"
            "【推理过程】一步步写，每步不超过一句\n"
            "【结论】一句话\n"
            "【不确定之处】如果信息不足，写清缺什么，不要硬给答案"
        ),
        "cot": "严禁跳步。信息不足时先追问，而不是猜一个答案。",
        "examples": [
            {"in": "甲比乙大 3 岁，乙比丙大 2 岁，丙 10 岁，甲多大？",
             "out": "【已知条件】甲=乙+3；乙=丙+2；丙=10\n"
                    "【推理过程】乙=10+2=12；甲=12+3=15\n【结论】甲 15 岁\n【不确定之处】无"},
        ],
    },
}


def task_label(tid: str) -> str:
    """任务模板的显示名（**界面文本**，按当前语言从语言包取）。"""
    return _t(f"task.{tid}.label")


def task_note(tid: str) -> str:
    """任务模板的一句话说明（**界面文本**，按当前语言从语言包取）。"""
    return _t(f"task.{tid}.note")


def _view(tid: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """模板的"可显示副本"：补上本地化的 label / note。

    刻意做成**取值时才解析**（而不是在 ``TASK_TEMPLATES`` 里直接写 ``_t(...)``）：
    模块级字典在导入时求值，会把语言固化在导入那一刻，之后 ``set_language``
    切到英文也不生效。
    """
    out = dict(item)
    out["id"] = tid
    out["label"] = task_label(tid)
    out["note"] = task_note(tid)
    return out


def _find(name: str) -> Optional[str]:
    """按 id / 当前语言的标签 / 前缀找到模板 id；找不到返回 None。"""
    key = (name or "").strip()
    if not key:
        return None
    if key in TASK_TEMPLATES:
        return key
    lowered = key.lower()
    for tid in TASK_TEMPLATES:
        if tid.lower() == lowered or task_label(tid) == key:
            return tid
    # 前缀匹配，方便用户少打字
    for tid in TASK_TEMPLATES:
        if tid.startswith(lowered):
            return tid
    return None


def template_ids() -> List[str]:
    return list(TASK_TEMPLATES.keys())


def get_template(name: str) -> Optional[Dict[str, Any]]:
    """按 id 或界面语言下的标签取模板（返回值里带本地化的 label / note）。"""
    tid = _find(name)
    return None if tid is None else _view(tid, TASK_TEMPLATES[tid])


def build_system_prompt(name: str, include_examples: bool = True) -> str:
    """把模板拼成一段可直接塞进系统提示词的中文说明。

    返回空串表示模板不存在（调用方据此给出「未找到模板」的提示）。

    拼接用的框架文字（``[任务模式]`` / ``[推理要求]`` / ``[示例]`` / ``[重要]``）
    与模板正文一样属于**提示词内容**而不是界面文本，因此保持中文、不进语言包；
    模板名取当前语言的 label，方便用户把界面上的名字和提示词对上。
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
    parts.append("[重要] 严格遵守上述输出格式；不要输出与本任务无关的内容。")
    return "\n\n".join(parts)


def menu_text() -> str:
    """``/task list`` 展示用清单（label / note 随界面语言切换）。"""
    lines = []
    for tid in TASK_TEMPLATES:
        lines.append(_t("task.menu_item", id=tid, label=task_label(tid), note=task_note(tid)))
    return "\n".join(lines)


def public_list() -> List[Dict[str, Any]]:
    """机器可读清单（供设置窗口/前端下拉框）。

    label / note 是本地化后的界面文本；role / format / cot 是提示词正文，
    按设计保持中文（见文件头说明）。
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
        }
        for tid, item in TASK_TEMPLATES.items()
    ]


# ---------------------------------------------------------------------------
# 方向七：模糊提问检测（信息不足时追问，而不是硬答）
# ---------------------------------------------------------------------------

#: 明显信息不足的提问特征（很短、且只有指代词/单字动词）
#: 这是**对用户输入的匹配规则**（跟正则同类），不是显示文本，故不进语言包；
#: 界面上真正会被看到的是追问话术本身（键 task.clarify_prompt）。
_VAGUE_PATTERNS = (
    "这个", "那个", "它", "怎么办", "怎么弄", "搞一下", "看一下", "帮我弄",
    "弄一下", "处理下", "处理一下", "优化下", "改一下",
)

_MIN_MEANINGFUL_CHARS = 6


def is_vague_question(text: str) -> bool:
    """判断提问是否信息不足到「必须先追问」的程度。

    刻意保守：只在**同时**满足「很短」和「含指代/泛动词」时才判为模糊，
    避免把正常短问题（「1+1=?」）也拦下来。返回 True 时调用方应追问而不是硬答。
    """
    stripped = (text or "").strip()
    if len(stripped) > 20:
        return False
    core = "".join(ch for ch in stripped if ch.isalnum())
    if len(core) > _MIN_MEANINGFUL_CHARS:
        return False
    return any(pattern in stripped for pattern in _VAGUE_PATTERNS)


def clarification_prompt(text: str) -> str:
    """生成追问话术（模板化，避免模型自己发挥出新的追问）。

    这段文本会作为**助手消息**显示在对话框里，因此整段取自语言包
    （键 ``task.clarify_prompt``）；``text`` 参数保留是为了不动调用方签名。
    """
    return _t("task.clarify_prompt")
