"""
[EN]
Prompt template library (v0.2.6 Part 3, task 3.4).

The library has two layers:

* **Built-in scenarios.** Thirty-plus validated templates grouped into
  categories (coding, writing, office, learning, life, general). The eight
  templates that predate this module are reused from :mod:`prompts` rather than
  copied, so there is exactly one definition of each of them; they are assigned
  categories here.
* **User templates.** Free-form templates authored in the interface, carrying
  their own label and category. They are persisted as one JSON file in the state
  directory, which keeps the built-in table read-only and versionable while the
  user data stays separate.

Localization: built-in labels and notes are UI text and are read from the
language pack (``promptlib.<id>.label`` / ``promptlib.<id>.note``; legacy
templates keep their ``task.<id>.*`` keys). Template bodies are prompt content
sent to the model and remain Chinese, for the reasons documented at the top of
:mod:`prompts`.

[ZH]
提示词模板库（v0.2.6 任务 3.4）。

模板库分两层：

* **内置场景。** 三十余个经过校验的模板，按分类组织（编程、写作、办公、学习、
  生活、通用）。本模块出现之前已有的八个模板直接复用 :mod:`prompts` 中的定义而
  不复制，保证每个模板只有一处定义；此处只为它们指定分类。
* **用户模板。** 在界面中自行编写的模板，自带名称与分类。它们以单个 JSON 文件
  持久化到状态目录，使内置表保持只读、可版本化，用户数据则彼此独立。

多语言：内置模板的名称与说明属于界面文本，从语言包读取
（``promptlib.<id>.label`` / ``promptlib.<id>.note``；旧模板继续使用
``task.<id>.*``）。模板正文是发送给模型的提示词内容，按 :mod:`prompts` 文件头
所述理由保持中文。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from i18n import t as _t
import prompts as _prompts

#: [EN] Category identifiers in display order. Labels come from the language pack
#: [EN] under ``promptlib.category.<id>``.
#: [ZH] 按显示顺序排列的分类标识。名称取自语言包 ``promptlib.category.<id>``。
CATEGORY_IDS = ("coding", "writing", "office", "learning", "life", "general")

#: [EN] Category assignment of the eight templates that predate this module. The
#: [EN] bodies stay in :mod:`prompts`; only the grouping lives here.
#: [ZH] 本模块出现之前已有八个模板的分类归属。正文仍位于 :mod:`prompts`，此处只
#: [ZH] 记录分组。
_LEGACY_CATEGORIES = {
    "summary": "office",
    "contract": "office",
    "extract": "office",
    "translate": "writing",
    "code": "coding",
    "reason": "learning",
    "creative": "writing",
    "guide": "general",
}

#: [EN] Built-in scenario templates added in v0.2.6. Each entry follows the same
#: [EN] four-element structure as :data:`prompts.TASK_TEMPLATES` (role, format,
#: [EN] cot, optional examples); label and note are not stored here because they
#: [EN] are UI text supplied by the language pack.
#: [ZH] v0.2.6 新增的内置场景模板。每个条目沿用与 :data:`prompts.TASK_TEMPLATES`
#: [ZH] 相同的四要素结构（role、format、cot、可选 examples）；名称与说明不存放于
#: [ZH] 此，因为它们是由语言包提供的界面文本。
BUILTIN_SCENARIOS: Dict[str, Dict[str, Any]] = {
    # [EN] ---------- Coding ----------
    # [ZH] ---------- 编程 ----------
    "refactor": {
        "category": "coding",
        "role": "你是负责长期维护的资深工程师，重构目标是不改变外部行为。",
        "format": (
            "输出要求：\n"
            "1. 先一句话说明现状问题（重复、耦合、命名、边界中的哪一类）；\n"
            "2. 给出重构后的完整代码块，保留原有公开接口与调用方式；\n"
            "3. 逐条列出行为等价性依据：输入相同、异常相同、副作用相同；\n"
            "4. 无法确认行为等价时明确写出「需要补充测试覆盖」，不要假定。"
        ),
        "cot": "先列出函数的外部契约（输入、输出、异常、副作用），再动手改内部结构。",
    },
    "code_review": {
        "category": "coding",
        "role": "你是只关注缺陷与风险的代码审查者，不评价个人风格偏好。",
        "format": (
            "输出要求：按严重程度分三档，每条格式为「位置 → 问题 → 后果 → 修改建议」：\n"
            "【必须修改】会导致错误结果、崩溃或安全问题；\n"
            "【建议修改】可维护性、性能或可读性问题；\n"
            "【可以忽略】纯风格差异。\n"
            "没有发现问题时写「未发现必须修改的问题」，不要为凑数而编造。"
        ),
        "cot": "先通读入口与边界条件，再逐段核对错误处理与资源释放。",
    },
    "unit_test": {
        "category": "coding",
        "role": "你是测试工程师，目标是让测试能真正发现回归，而不是刷覆盖率。",
        "format": (
            "输出要求：\n"
            "1. 先列测试点（正常路径、边界值、异常路径各至少一条）；\n"
            "2. 给出可直接运行的测试代码块；\n"
            "3. 标出每个用例对应的断言目的；\n"
            "4. 依赖外部环境的用例单独说明如何替换为桩。"
        ),
        "cot": "先确定被测函数的输入域与边界，再设计断言，最后才写测试代码。",
    },
    "code_explain": {
        "category": "coding",
        "role": "你是耐心的代码讲解者，面向能读代码但首次接触该项目的人。",
        "format": (
            "输出要求：\n"
            "1. 先用三句话说明这段代码的职责、输入与输出；\n"
            "2. 再按执行顺序逐段说明关键语句，引用原文片段；\n"
            "3. 指出至少一处容易误读或隐含假设的地方；\n"
            "4. 不要逐行翻译语法。"
        ),
        "cot": "先追踪数据从入口到出口的流动，再解释每一步变换。",
    },
    "debug": {
        "category": "coding",
        "role": "你是排查故障的工程师，先定位再给方案，不猜 API 行为。",
        "format": (
            "输出要求：\n"
            "1. 先给出最可能的 2~3 个原因，按可能性排序，每个附一句判断依据；\n"
            "2. 给出验证顺序：先做哪个检查、期望看到什么结果；\n"
            "3. 确认原因后再给修复代码；未确认时只给验证步骤；\n"
            "4. 信息不足时明确列出还需要哪些信息。"
        ),
        "cot": "从报错信息与发生时机反推触发路径，优先排除最近改动与配置差异。",
    },
    "perf": {
        "category": "coding",
        "role": "你是性能优化工程师，先测量后优化。",
        "format": (
            "输出要求：\n"
            "1. 先指出性能瓶颈所在层次（算法、I/O、内存、并发、序列化）；\n"
            "2. 给出可测量的验证方式（计时点、规模、期望量级）；\n"
            "3. 优化方案按收益/风险排序，每项说明代价；\n"
            "4. 未测量的推测必须标注为推测。"
        ),
        "cot": "先确认复杂度与调用频次，再判断是否值得优化。",
    },
    "api_doc": {
        "category": "coding",
        "role": "你是接口文档作者，输出面向调用方的准确说明。",
        "format": (
            "输出要求：\n"
            "1. 按「用途、参数、返回值、异常、示例」五节输出；\n"
            "2. 参数表列名称、类型、是否必填、默认值与含义；\n"
            "3. 示例必须与签名一致，不得编造不存在的参数；\n"
            "4. 未在代码中确认的行为写「未定义」。"
        ),
        "cot": "先读取签名与实现中的分支，再整理成文档，不凭命名推断行为。",
    },
    # [EN] ---------- Writing ----------
    # [ZH] ---------- 写作 ----------
    "xiaohongshu": {
        "category": "writing",
        "role": "你是熟悉小红书平台语感的内容编辑，擅长用具体场景代替形容词。",
        "format": (
            "输出要求：\n"
            "1. 先给 3 个标题候选，每个不超过 20 字，带真实的场景或数字；\n"
            "2. 正文 200~400 字，短句分段落，开头两句必须抓住痛点；\n"
            "3. 结尾给 3~5 个话题标签；\n"
            "4. 不使用夸大功效与绝对化用语，不编造使用体验。"
        ),
        "cot": "先确定目标读者与他们的具体困扰，再选择切入场景。",
    },
    "wechat_article": {
        "category": "writing",
        "role": "你是公众号长文作者，重视结构与信息密度。",
        "format": (
            "输出要求：\n"
            "1. 先给标题与三句话导语；\n"
            "2. 正文分 3~5 个小节，每节带小标题，段落不超过四行；\n"
            "3. 每个论点必须有事实、数据或具体案例支撑；没有依据时明确写「待补充数据」；\n"
            "4. 结尾给一句可被记住的结论。"
        ),
        "cot": "先列论点顺序，再为每个论点配证据，最后写导语与结尾。",
    },
    "business_mail": {
        "category": "writing",
        "role": "你是商务沟通顾问，邮件必须让对方在三十秒内知道要做什么。",
        "format": (
            "输出要求：\n"
            "1. 主题行一句话说清事项与需要对方做的事；\n"
            "2. 正文先结论后背景，分点列出需要对方确认的事项与截止时间；\n"
            "3. 语气克制、不卑不亢，不使用夸张客套；\n"
            "4. 涉及金额、日期、责任时与原文一致，不得改动。"
        ),
        "cot": "先判断收件人最关心什么，再决定结论与背景的顺序。",
    },
    "weekly_report": {
        "category": "writing",
        "role": "你是项目经理的助理，周报只写可核对的事实。",
        "format": (
            "输出要求：\n"
            "【本周完成】按结果写，每条带可验证的产出，不写「推进中」；\n"
            "【问题与风险】写清影响范围与需要谁决策；\n"
            "【下周计划】不超过 5 条，按优先级排序；\n"
            "没有内容的部分写「无」，不要用套话填充。"
        ),
        "cot": "先把素材按「完成/进行中/未开始」归类，再压缩成结果句。",
    },
    "resume_polish": {
        "category": "writing",
        "role": "你是招聘方的简历审阅者，只保留能被追问验证的内容。",
        "format": (
            "输出要求：\n"
            "1. 逐条改写为「动作 + 方法 + 可量化结果」；\n"
            "2. 删去无信息量的自我评价；\n"
            "3. 标出缺少数据支撑、可能被追问的条目；\n"
            "4. 不得编造经历、数字或证书。"
        ),
        "cot": "先判断目标岗位看重的能力，再据此重排条目顺序。",
    },
    "speech_draft": {
        "category": "writing",
        "role": "你是演讲稿撰写者，稿件用于口头表达而非阅读。",
        "format": (
            "输出要求：\n"
            "1. 按「开场钩子、三个要点、收尾行动号召」组织；\n"
            "2. 句子短、可一口气读完，避免书面长句与括号；\n"
            "3. 每个要点配一个具体例子；\n"
            "4. 标注预计时长与可删减的段落。"
        ),
        "cot": "先确定听众身份与他们听完要做的动作，再组织内容。",
    },
    "product_copy": {
        "category": "writing",
        "role": "你是产品详情页文案，写清适用人群与不适用场景。",
        "format": (
            "输出要求：\n"
            "1. 一句话卖点，必须来自已提供的参数；\n"
            "2. 按「解决什么问题、怎么解决、适合谁、不适合谁」四段输出；\n"
            "3. 参数原样引用，不得夸大或改写；\n"
            "4. 未提供的参数写「未提供」，不要编造。"
        ),
        "cot": "先从参数中找出与同类产品不同的点，再写卖点。",
    },
    # [EN] ---------- Office ----------
    # [ZH] ---------- 办公 ----------
    "meeting_minutes": {
        "category": "office",
        "role": "你是会议记录员，只记录会上明确说过的内容。",
        "format": (
            "输出要求：\n"
            "【会议信息】时间、参与人（原文没有则写「未提及」）；\n"
            "【结论】逐条列出已达成的决定；\n"
            "【待办】每条格式为「事项 / 负责人 / 截止时间」，缺项写「未明确」；\n"
            "【未决事项】列出仍有分歧的点；\n"
            "不得补充会上没说过的内容。"
        ),
        "cot": "先区分「决定」「待办」「讨论」三类语句，再分别归档。",
    },
    "contract_translate": {
        "category": "office",
        "role": "你是法律文本译者，术语一致优先于文采。",
        "format": (
            "输出要求：\n"
            "1. 只输出译文，保留原文的条款编号与段落结构；\n"
            "2. 术语首次出现时用「译文（原文）」标注，之后统一使用同一译法；\n"
            "3. 金额、日期、百分比、主体名称不得改动；\n"
            "4. 有歧义或无法确定的条款单独列出，不做意译猜测。"
        ),
        "cot": "先通读全文统一定价、期限、责任三类术语，再逐条翻译。",
    },
    "table_clean": {
        "category": "office",
        "role": "你是数据整理员，输出可直接导入表格的规整数据。",
        "format": (
            "输出要求：\n"
            "1. 先指出原数据的问题（缺列、单位不一致、重复行、格式混乱）；\n"
            "2. 以 Markdown 表格输出整理后的数据，列名与单位写清；\n"
            "3. 无法确定的单元格填「待确认」，不要猜测；\n"
            "4. 删除或修改的行单独列出并说明理由。"
        ),
        "cot": "先确定每列的含义与单位，再处理重复与缺失。",
    },
    "data_report": {
        "category": "office",
        "role": "你是数据分析师，结论必须有数据支撑。",
        "format": (
            "输出要求：\n"
            "1. 先给三条以内的核心结论，每条附对应数据；\n"
            "2. 再说明数据口径、时间范围与已知缺口；\n"
            "3. 区分「数据表明」与「推测」，推测必须标明；\n"
            "4. 不建议基于缺失数据做决策。"
        ),
        "cot": "先核对口径与总量，再计算占比与变化，最后才下结论。",
    },
    "policy_review": {
        "category": "office",
        "role": "你是制度条款审查者，关注可执行性与责任边界。",
        "format": (
            "输出要求：按【适用对象】【执行流程】【责任与处罚】【例外情形】【缺失条款】逐项写，"
            "每条格式为「原文摘录 → 问题 → 修改建议」；摘录必须与原文一致；"
            "找不到对应条款时写「未见相关约定」。"
        ),
        "cot": "先确认条款由谁执行、何时执行，再检查是否有对应的责任与救济。",
    },
    # [EN] ---------- Learning ----------
    # [ZH] ---------- 学习 ----------
    "concept_explain": {
        "category": "learning",
        "role": "你是善于用类比解释抽象概念的教师，但类比之后必须回到准确定义。",
        "format": (
            "输出要求：\n"
            "1. 先用一句话给出准确定义；\n"
            "2. 再用一个生活化类比说明，并指出类比的局限；\n"
            "3. 给一个最小例子；\n"
            "4. 最后列出两个常见误解。"
        ),
        "cot": "先确定该概念要解决什么问题，再组织定义、类比与例子。",
    },
    "study_plan": {
        "category": "learning",
        "role": "你是学习规划师，计划必须能在给定时间内真正执行。",
        "format": (
            "输出要求：\n"
            "1. 先确认目标、可用时间与当前基础；信息不足时按最常见情况假设并标注；\n"
            "2. 输出按周划分的计划表，列「阶段 / 内容 / 产出物 / 自检方式」；\n"
            "3. 每周投入时间不超过给定上限；\n"
            "4. 给出一个两周后可执行的调整规则。"
        ),
        "cot": "先按目标倒推所需能力，再切分到可在一周内完成的单元。",
    },
    "quiz_generate": {
        "category": "learning",
        "role": "你是出题老师，题目必须能从给定材料中找到依据。",
        "format": (
            "输出要求：\n"
            "1. 出 5 道题，题型覆盖选择、填空、简答；\n"
            "2. 每题标注考查点与难度；\n"
            "3. 答案与解析单独列出，解析须引用材料原文；\n"
            "4. 不得考查材料之外的知识。"
        ),
        "cot": "先提取材料中的关键事实与因果关系，再据此设计题目。",
    },
    "paper_summary": {
        "category": "learning",
        "role": "你是论文审读者，区分作者的主张与证据。",
        "format": (
            "输出要求：\n"
            "【研究问题】【方法】【数据与样本】【主要结论】【局限】各一节；\n"
            "结论必须区分「作者主张」与「数据支持」；\n"
            "原文未说明的部分写「未说明」，不要补全。"
        ),
        "cot": "先找研究问题与假设，再核对方法与结论是否匹配。",
    },
    "english_polish": {
        "category": "learning",
        "role": "你是英语写作教师，改错同时说明原因。",
        "format": (
            "输出要求：\n"
            "1. 先给修改后的全文；\n"
            "2. 再逐条列出修改点：原文 → 修改 → 语法或搭配原因；\n"
            "3. 保持作者原有语气与观点，不重写成另一篇文章；\n"
            "4. 不确定是否为错误的用法不要改动，单独列出。"
        ),
        "cot": "先判断文体与目标读者，再处理语法、搭配与衔接。",
    },
    # [EN] ---------- Life ----------
    # [ZH] ---------- 生活 ----------
    "travel_plan": {
        "category": "life",
        "role": "你是行程规划师，计划要考虑体力、交通与备选方案。",
        "format": (
            "输出要求：\n"
            "1. 按天输出行程表，列「时段 / 地点 / 交通 / 备注」；\n"
            "2. 每天安排留出至少一段机动时间；\n"
            "3. 为天气或闭馆给出一个备选方案；\n"
            "4. 门票、价格、营业时间未确认时标注「需现场确认」，不要编造。"
        ),
        "cot": "先按地理位置聚类景点，再排时间，避免往返折返。",
    },
    "recipe": {
        "category": "life",
        "role": "你是家常菜谱作者，步骤按实际操作顺序写。",
        "format": (
            "输出要求：\n"
            "【用料】写清分量；\n"
            "【步骤】每步一个动作，注明火候与时间；\n"
            "【关键点】列出最容易失败的 2~3 处；\n"
            "【替代方案】缺少用料时给出可行替代。"
        ),
        "cot": "先确定成品口味与口感，再倒推火候与下料顺序。",
    },
    "fitness_plan": {
        "category": "life",
        "role": "你是健身教练，方案以可持续与不受伤为前提。",
        "format": (
            "输出要求：\n"
            "1. 先确认目标、每周可训练次数与器械条件；信息不足时按最常见情况假设并标注；\n"
            "2. 输出按周训练表，列「动作 / 组数 / 次数 / 组间休息」；\n"
            "3. 标注动作要点与常见错误；\n"
            "4. 明确写出「如有伤病请先咨询医生」。"
        ),
        "cot": "先安排大肌群复合动作，再补充辅助动作，最后留出恢复日。",
    },
    "gift_advice": {
        "category": "life",
        "role": "你是送礼顾问，建议必须落在给定预算与关系内。",
        "format": (
            "输出要求：\n"
            "1. 先确认对象、场合、预算与关系；信息不足时列出假设；\n"
            "2. 给 3 个方案，每个写明适合的理由与不合适的情形；\n"
            "3. 标注需要提前准备的时间；\n"
            "4. 不推荐涉及医疗、宗教或隐私敏感的物品。"
        ),
        "cot": "先判断这份礼物要传达什么，再在预算内选择载体。",
    },
}

#: [EN] Ending appended to built-in scenario prompts. It mirrors the relaxed
#: [EN] ending in :mod:`prompts`: format takes precedence, but the level of
#: [EN] detail follows the question's complexity.
#: [ZH] 追加到内置场景提示词末尾的结束语。与 :mod:`prompts` 的宽松结尾一致：
#: [ZH] 格式优先，但详略随问题复杂度判断。
_SCENARIO_ENDING = (
    "[重要] 本任务的格式要求优先；但详略仍按问题复杂度判断——"
    "简单问题可以只答一句话，不必凑满上面每一条。"
)


def default_library_path() -> str:
    """
    [EN]
    Default location of the user template file: ``<state dir>/prompts/user.json``.

    [ZH]
    用户模板文件的默认位置：``<state dir>/prompts/user.json``。
    """
    import config  # noqa: PLC0415 - [EN] lazy import avoids a module import cycle
                        # [ZH] 惰性导入，避免模块级循环依赖

    return os.path.join(config.state_dir(), "prompts", "user.json")


def category_label(category_id: str) -> str:
    """
    [EN]
    Localized display name of one category.

    [ZH]
    单个分类的本地化显示名。
    """
    return _t(f"promptlib.category.{category_id}")


def categories() -> List[Dict[str, str]]:
    """
    [EN]
    Category list for the interface selector, in display order.

    [ZH]
    供界面选择器使用的分类清单，按显示顺序排列。
    """
    return [{"id": item, "label": category_label(item)} for item in CATEGORY_IDS]


def _legacy_entry(template_id: str) -> Dict[str, Any]:
    """
    [EN]
    View of one pre-existing task template in library form. The body is taken
    from :mod:`prompts` so that the two paths cannot diverge.

    [ZH]
    既有任务模板在模板库视图中的条目。正文取自 :mod:`prompts`，使两条路径不会
    产生分歧。
    """
    template = _prompts.TASK_TEMPLATES[template_id]
    return {
        "id": template_id,
        "category": _LEGACY_CATEGORIES.get(template_id, "general"),
        "label": _prompts.task_label(template_id),
        "note": _prompts.task_note(template_id),
        "role": str(template.get("role") or ""),
        "format": str(template.get("format") or ""),
        "cot": str(template.get("cot") or ""),
        "example_count": len(template.get("examples") or []),
        "source": "builtin",
        "editable": False,
    }


def _scenario_entry(template_id: str) -> Dict[str, Any]:
    """
    [EN]
    View of one v0.2.6 built-in scenario in library form.

    [ZH]
    v0.2.6 内置场景在模板库视图中的条目。
    """
    template = BUILTIN_SCENARIOS[template_id]
    return {
        "id": template_id,
        "category": str(template.get("category") or "general"),
        "label": _t(f"promptlib.{template_id}.label"),
        "note": _t(f"promptlib.{template_id}.note"),
        "role": str(template.get("role") or ""),
        "format": str(template.get("format") or ""),
        "cot": str(template.get("cot") or ""),
        "example_count": 0,
        "source": "builtin",
        "editable": False,
    }


def builtin_entries() -> List[Dict[str, Any]]:
    """
    [EN]
    Every built-in template, legacy templates first, then the v0.2.6 scenarios
    in declaration order. Resolution of localized text happens on each call, so
    a language switch is reflected without restarting.

    [ZH]
    全部内置模板：先是旧模板，随后按声明顺序排列 v0.2.6 场景。本地化文本在每次
    调用时解析，因此切换语言无需重启即可生效。
    """
    entries = [_legacy_entry(tid) for tid in _prompts.TASK_TEMPLATES]
    entries.extend(_scenario_entry(sid) for sid in BUILTIN_SCENARIOS)
    return entries


def _assemble(entry: Dict[str, Any], include_examples: bool = True) -> str:
    """
    [EN]
    Assemble a scenario or user template into a system-prompt block.

    The framing text is the same as :func:`prompts.build_system_prompt`; keeping
    the framing identical means the model cannot tell the two origins apart,
    and the behaviour established for task templates carries over.

    [ZH]
    将场景或用户模板拼接为系统提示词块。

    框架文字与 :func:`prompts.build_system_prompt` 完全一致；保持框架一致意味着
    模型无法区分两者来源，任务模板已确立的行为得以延续。
    """
    parts: List[str] = [
        f"[任务模式] {entry.get('label') or entry.get('id')}",
        str(entry.get("role") or ""),
        str(entry.get("format") or ""),
    ]
    cot = str(entry.get("cot") or "").strip()
    if cot:
        parts.append(f"[推理要求] {cot}")
    examples = entry.get("examples") or []
    if include_examples and examples:
        lines = ["[示例]"]
        for index, example in enumerate(examples, 1):
            lines.append(f"输入{index}：{example.get('in', '')}")
            lines.append(f"输出{index}：{example.get('out', '')}")
        parts.append("\n".join(lines))
    parts.append(
        "[优先级] 本节是本次任务的具体要求：如果它与前面模式要求的输出格式冲突，"
        "以本节为准；模式的角色、语气与边界要求继续有效。"
    )
    parts.append(_SCENARIO_ENDING)
    return "\n\n".join(part for part in parts if part)


class PromptLibrary:
    """
    [EN]
    Read view over the built-in templates plus a user template file.

    The user file is loaded lazily on first access and reloaded whenever its
    modification time changes, so an external edit (or a second process) is
    picked up without a restart. Every mutation rewrites the file atomically.

    [ZH]
    内置模板与用户模板文件的读取视图。

    用户文件在首次访问时惰性载入，并在修改时间变化时重新载入，使外部编辑（或
    另一个进程的修改）无需重启即可生效。每次修改都以原子方式重写文件。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        """
        [EN]
        Bind the library to a user template file.

        [ZH]
        将模板库绑定到用户模板文件。
        """
        self.path = str(path or default_library_path())
        self._lock = threading.RLock()
        self._entries: List[Dict[str, Any]] = []
        self._mtime = -1.0

    # [EN] ---------- User template persistence ----------
    # [ZH] ---------- 用户模板持久化 ----------

    def _load(self, force: bool = False) -> None:
        """
        [EN]
        Reload the user file when it changed. A missing or malformed file is an
        empty library, not an error: built-in templates remain usable.

        [ZH]
        用户文件变化时重新载入。文件缺失或格式错误视为空库而非错误：内置模板仍可
        使用。
        """
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = -1.0
        with self._lock:
            if not force and mtime == self._mtime:
                return
            self._mtime = mtime
            self._entries = []
            if mtime < 0:
                return
            try:
                with open(self.path, "r", encoding="utf-8-sig") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                return
            raw_items = payload.get("templates") if isinstance(payload, dict) else payload
            if not isinstance(raw_items, list):
                return
            for item in raw_items:
                normalized = self._normalize_user_entry(item)
                if normalized is not None:
                    self._entries.append(normalized)

    def _save(self) -> None:
        """
        [EN]
        Write the user template file atomically.

        [ZH]
        以原子方式写入用户模板文件。
        """
        with self._lock:
            entries = [dict(entry) for entry in self._entries]
        directory = os.path.dirname(self.path)
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {"version": 1, "updated_at": time.time(), "templates": entries}
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        except OSError:
            # [EN] A read-only state directory disables user templates but must
            # [EN] not break the template list.
            # [ZH] 只读状态目录使用户模板失效，但不得使模板列表不可用。
            return
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = -1.0

    def _normalize_user_entry(self, item: Any) -> Optional[Dict[str, Any]]:
        """
        [EN]
        Validate one stored user template. Entries missing a label or a body are
        dropped rather than repaired, because a template without an instruction
        would silently inject an empty task layer.

        [ZH]
        校验一条已存用户模板。缺少名称或正文的条目直接丢弃而不修补 —— 没有指令的
        模板会静默注入一个空任务层。
        """
        if not isinstance(item, dict):
            return None
        label = str(item.get("label") or "").strip()
        body = str(item.get("format") or "").strip()
        if not label or not body:
            return None
        category = str(item.get("category") or "general").strip().lower()
        template_id = str(item.get("id") or "").strip()
        if not template_id:
            return None
        examples = item.get("examples")
        if not isinstance(examples, list):
            examples = []
        return {
            "id": template_id,
            "category": category if category in CATEGORY_IDS else "general",
            "label": label[:80],
            "note": str(item.get("note") or "").strip()[:200],
            "role": str(item.get("role") or "").strip()[:400],
            "format": body[:6000],
            "cot": str(item.get("cot") or "").strip()[:800],
            "examples": [dict(example) for example in examples
                         if isinstance(example, dict)][:3],
            "created_at": float(item.get("created_at") or time.time()),
            "updated_at": float(item.get("updated_at") or time.time()),
            "source": "user",
            "editable": True,
        }

    # [EN] ---------- Queries ----------
    # [ZH] ---------- 查询 ----------

    def list(self, category: str = "") -> List[Dict[str, Any]]:
        """
        [EN]
        Every template, optionally filtered by category. The returned mapping is
        a display copy: the stored entries are not exposed for mutation.

        [ZH]
        全部模板，可按分类过滤。返回的是显示副本：不暴露存储条目供外部修改。
        """
        self._load()
        wanted = (category or "").strip().lower()
        with self._lock:
            user_entries = [self._view_user(entry) for entry in self._entries]
        entries = builtin_entries() + user_entries
        if wanted:
            entries = [entry for entry in entries if entry["category"] == wanted]
        return entries

    def _view_user(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Display copy of one user template, including its body for editing.

        [ZH]
        单条用户模板的显示副本，含正文以便编辑。
        """
        return {
            "id": entry["id"],
            "category": entry["category"],
            "label": entry["label"],
            "note": entry["note"],
            "role": entry["role"],
            "format": entry["format"],
            "cot": entry["cot"],
            "examples": list(entry.get("examples") or []),
            "example_count": len(entry.get("examples") or []),
            "created_at": entry["created_at"],
            "updated_at": entry["updated_at"],
            "source": "user",
            "editable": True,
        }

    def get(self, template_id: str, include_body: bool = True) -> Optional[Dict[str, Any]]:
        """
        [EN]
        One template by identifier, built-in or user. Returns ``None`` when the
        identifier is unknown.

        [ZH]
        按标识取单个模板（内置或用户）。标识未知时返回 ``None``。
        """
        target = str(template_id or "").strip()
        if not target:
            return None
        for entry in self.list():
            if entry["id"] != target:
                continue
            if include_body:
                return entry
            copy = dict(entry)
            copy.pop("format", None)
            copy.pop("role", None)
            copy.pop("cot", None)
            return copy
        return None

    def stats(self) -> Dict[str, Any]:
        """
        [EN]
        Counts by origin and the user-file location, for diagnostics.

        [ZH]
        按来源统计的数量与用户文件位置，供诊断使用。
        """
        self._load()
        with self._lock:
            user_count = len(self._entries)
        return {"builtin": len(_prompts.TASK_TEMPLATES) + len(BUILTIN_SCENARIOS),
                "user": user_count, "path": self.path,
                "categories": list(CATEGORY_IDS)}

    # [EN] ---------- Mutations ----------
    # [ZH] ---------- 修改 ----------

    def save(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Create or update one user template and return its display copy.

        ``payload`` keys: ``id`` (empty for a new template), ``label``,
        ``category``, ``format`` (the instruction itself), and the optional
        ``role`` / ``note`` / ``cot`` / ``examples``. ``label`` and ``format`` are
        mandatory; a built-in identifier cannot be overwritten, so an attempt to
        reuse one is rejected with :class:`ValueError`.

        [ZH]
        新建或更新一条用户模板并返回其显示副本。

        ``payload`` 字段：``id``（新建时为空）、``label``、``category``、
        ``format``（指令正文），以及可选的 ``role`` / ``note`` / ``cot`` /
        ``examples``。``label`` 与 ``format`` 为必填；内置标识不得被覆盖，复用
        内置标识将以 :class:`ValueError` 拒绝。
        """
        if not isinstance(payload, dict):
            raise ValueError("template payload must be an object")
        label = str(payload.get("label") or "").strip()
        body = str(payload.get("format") or "").strip()
        if not label:
            raise ValueError("template label is required")
        if not body:
            raise ValueError("template instruction is required")
        self._load(force=True)
        template_id = str(payload.get("id") or "").strip()
        builtin_ids = {entry["id"] for entry in builtin_entries()}
        if template_id and template_id in builtin_ids:
            raise ValueError("built-in templates cannot be overwritten")
        if not template_id:
            template_id = "user-" + format(int(time.time() * 1000), "x")
        now = time.time()
        with self._lock:
            previous = next((entry for entry in self._entries
                             if entry["id"] == template_id), None)
            entry = self._normalize_user_entry({
                "id": template_id,
                "category": payload.get("category") or (previous or {}).get("category") or "general",
                "label": label,
                "note": payload.get("note") or (previous or {}).get("note") or "",
                "role": payload.get("role") or (previous or {}).get("role") or "",
                "format": body,
                "cot": payload.get("cot") or (previous or {}).get("cot") or "",
                "examples": payload.get("examples") or (previous or {}).get("examples") or [],
                "created_at": (previous or {}).get("created_at") or now,
                "updated_at": now,
            })
            if entry is None:
                raise ValueError("template content is invalid")
            if previous is None:
                self._entries.append(entry)
            else:
                index = next(i for i, item in enumerate(self._entries)
                             if item["id"] == template_id)
                self._entries[index] = entry
        self._save()
        return self._view_user(entry)

    def delete(self, template_id: str) -> bool:
        """
        [EN]
        Delete one user template. Returns whether it existed; built-in templates
        cannot be deleted and yield ``False``.

        [ZH]
        删除一条用户模板。返回其是否存在；内置模板不可删除，返回 ``False``。
        """
        target = str(template_id or "").strip()
        self._load(force=True)
        with self._lock:
            index = next((i for i, entry in enumerate(self._entries)
                          if entry["id"] == target), None)
            if index is None:
                return False
            self._entries.pop(index)
        self._save()
        return True

    def system_prompt(self, template_id: str, include_examples: bool = True) -> str:
        """
        [EN]
        Build the system-prompt block for one template.

        Legacy templates delegate to :func:`prompts.build_system_prompt` so the
        established wording is reused verbatim; scenarios and user templates use
        :func:`_assemble`. An empty string means the template does not exist.

        [ZH]
        为单个模板构造系统提示词块。

        旧模板委托给 :func:`prompts.build_system_prompt`，原样复用既有措辞；场景
        与用户模板使用 :func:`_assemble`。返回空串表示模板不存在。
        """
        target = str(template_id or "").strip()
        if target in _prompts.TASK_TEMPLATES:
            return _prompts.build_system_prompt(target, include_examples)
        if target in BUILTIN_SCENARIOS:
            return _assemble(_scenario_entry(target), include_examples)
        self._load()
        with self._lock:
            entry = next((item for item in self._entries if item["id"] == target), None)
        if entry is None:
            return ""
        return _assemble(entry, include_examples)
