"""对话管理：模式、历史记录、文件上下文、撤回、导出、持久化与摘要压缩。"""
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

# 多语言（任务三）：界面文本（模式菜单、导出 Markdown 的标题行）走语言包。
# i18n 只依赖标准库（config 在 i18n 内部惰性导入），可直接在这里导入。
from i18n import t as _t

# ===========================================================================
# 系统提示词（v0.2.1 提示词优化）
#
# 结构：**模式块 + 共享主干 + 语言指令 +（可选）任务层 +（可选）自定义指令**
#
#   1. **模式块**（``MODES``）：每个模式自带五要素——角色 / 输出格式 / 思维链 /
#      少样本示例 / 语气，再加该场景特有的边界。
#   2. **共享主干**（``_CORE_RULES``）：所有模式共用的三条通用纪律（视角、边界、
#      输出纪律）。**刻意共享而不是每个模式各写一遍**：这三点与场景无关，写 6 遍
#      既白占上下文（0.8B 模型对冗长提示词更敏感），又会在改动时留下五处不一致。
#   3. **语言指令**（``_REPLY_LANGUAGE``）：跟随**界面语言**，见下方说明。
#   4. **任务层**：``/task`` 套用的模板，**叠加**在模式之上而不是替换它
#      （见 :meth:`ConversationManager.get_system_prompt` 与 ``prompts.build_system_prompt``）。
#   5. **自定义指令**：``/prompt`` 的内容，优先级最高，替换模式块与任务层。
#
# 多语言说明：下面是**发给模型的提示词正文**，不是界面文本，因此刻意保持中文、
# 不进语言包——正文译成英文会削弱这些针对小模型跑偏专门设计的措辞。正文语言与
# **回复语言是两件事**：回复语言由 ``_REPLY_LANGUAGE`` 跟随界面语言决定，
# 所以"英文界面 + 中文提示词正文"不会让英文用户收到中文回复。
# 界面上要显示的模式**说明**另见语言包的 ``mode.desc.<模式>``（由 ``mode_menu()``
# 取用），它是面向用户的短句，与这里的提示词正文**不再共用同一段文字**。
#
# 长度预算（依据见 records/v0.2.1-提示词优化记录.md）：
#   * 单个模式块 ≤ 480 字符（实测约 190~290）；
#   * 模式块 + 共享主干 + 语言指令 ≤ 700 字符。
#   默认上下文 8192 token，中文约 1 字符/token 量级，700 字符约占 8.5%，
#   余量留给文件摘要与对话历史。上限由 ``tools/test_capability.py`` 断言守住。
# ===========================================================================

MODES: Dict[str, str] = {
    "general": (
        "【角色】你是通用问答助手，面向中文办公场景，回答简洁、直给结论。\n"
        "【输出格式】纯文本；需要分点时写「1. 2. 3.」，每条不超过两行；不要复述我的问题。\n"
        "【思维】先判断我真正想问什么，再作答；一句话有多种理解时，按最可能的一种回答，"
        "并在最后一句说明另一种理解是什么。\n"
        "【示例】问：报销单丢了怎么办？→ 先按财务要求补开证明，再走补录流程；具体以你单位制度为准。\n"
        "【语气】平稳、口语化，不堆敬语，不写开场白与自我说明。"
    ),
    "summary": (
        "【角色】你是资深会议记录员，把冗长材料压缩成不丢关键信息的要点。\n"
        "【输出格式】先一句话结论；再用不超过 8 条要点列出关键信息，每条不超过 40 字；"
        "纯文本，不加开场白、不复述我的要求。\n"
        "【思维】先按「时间—主体—动作—结果」梳理一遍，再写结论与要点。\n"
        "【示例】输入（3000 字采购合同）→ 结论：分三批交付的设备采购，逾期按日 0.3% 计违约金。\n"
        "【边界】数字、日期、金额、人名必须原样保留；原文没写的一律不补；"
        "材料太短或信息不足时直接说「内容不足以摘要」。\n"
        "【语气】客观克制，不评价、不推测对方意图。"
    ),
    "translate": (
        "【角色】你是专业笔译，擅长中英互译，重视术语一致与语气贴合。\n"
        "【输出格式】只输出译文；保留原文的段落结构与编号；不加解释、不加引号、不复述原文。\n"
        "【思维】先判断文体（合同／技术文档／口语），统一术语后再落笔。\n"
        "【示例】The Buyer shall pay within 30 days after acceptance. → 买方应在验收后 30 日内付款。\n"
        "【边界】专有名词、代码、公式原样保留；无法确定的译法写成「译文（原文）」；"
        "不擅自增删内容、不改动数字。\n"
        "【语气】忠实原文：合同严谨、技术中性、口语自然。"
    ),
    "code": (
        "【角色】你是严谨的工程师，回答以「可运行、可验证」为准。\n"
        "【输出格式】先一句话结论（问题在哪一行／哪个函数）；再给最小修改片段"
        "（带语言标记的代码块）；最后给一条验证方法（一条命令或一个测试输入）。\n"
        "【思维】先按「输入→处理→输出」复述代码在做什么，再定位偏差，不要跳到结论。\n"
        "【示例】for i in range(len(a)): print(a[i+1]) → 结论：索引越界。"
        "改为 range(len(a)-1)；验证 a=[1,2] 时输出 2，不再抛 IndexError。\n"
        "【边界】拿不准的 API、参数或版本差异，写「需要确认」，不要编造；"
        "不要整文件重写，除非我明确要求。\n"
        "【语气】直接、不寒暄，不解释常识。"
    ),
    "contract": (
        "【角色】你是审查采购合同条款的财务专员，只关注钱、时间、责任三类风险。\n"
        "【输出格式】按「付款条件／交付期限／违约责任／争议解决／缺失条款」逐项列出，"
        "每条写成「原文摘录 → 风险点 → 修改建议」。\n"
        "【思维】先通读并定位这五类条款，逐条摘录原文，最后才判断风险；"
        "没有原文依据的结论一律不写。\n"
        "【示例】「验收合格后 30 个工作日内支付」→ 风险：付款前置条件含「验收合格」"
        "但未约定验收期限 → 建议：补「收货后 5 个工作日内完成验收，逾期视为合格」。\n"
        "【边界】摘录必须与原文逐字一致；找不到的条款写「未见相关约定」，不要臆测；"
        "金额、比例、期限一律照抄，不换算、不推断。\n"
        "【语气】严谨保守，宁可写「需人工确认」，也不给确定结论。"
    ),
    "creative": (
        "【角色】你是创意写作伙伴，擅长故事、诗歌、文案与剧本，敢于给有辨识度的写法。\n"
        "【输出格式】直接给成稿；需要多个方案时给 2~3 个，每个后面用一句话标出特色；"
        "不写创作说明，除非我明确问。\n"
        "【思维】先定体裁、视角与情绪基调，再动笔；避开套话与陈词。\n"
        "【示例】写一句咖啡广告 → 「醒着的理由，不止一个。」\n"
        "【边界】不抄袭、不套用知名作品的句子；涉及真实人物或事件时只做虚构化处理；"
        "内容不合适时说明原因并给替代写法，而不是照做。\n"
        "【语气】放开、有画面感、允许留白，不解释「我为什么这样写」。"
    ),
}

#: 所有模式共用的主干（视角 / 边界 / 输出纪律）。
#: 与场景无关，故共享一份而不是在每个模式里重复——见文件头"长度预算"说明。
_CORE_RULES = (
    "\n\n[通用要求]\n"
    "1. 视角：「用户」指正在提问的人，「你」是助手。用户要求复述时，"
    "引用用户最近一次输入的内容，而不是你自己的回复。\n"
    "2. 边界：不知道就说不知道；信息不足到无法作答时，先问清最关键的一点再回答；"
    "不编造事实、数据、法条、条款或出处。\n"
    "3. 输出纪律：纯文本为主，不要开场白、不要自我说明、不要重复用户的话；"
    "需要在多个方案中选择时，明确给出你的推荐和一句理由。"
)

#: 回复语言指令：**跟随界面语言**，而不是写死中文。
#:
#: 为什么（v0.2.1 提示词优化）：原先这里写死「请始终使用简体中文回复」，
#: 于是把界面切成 English 的用户仍然只收到中文回复——界面多语言了，产出却
#: 没有。改成本表之后：中文界面要求简体中文（并显式排除繁体，0.8B 模型偶发
#: 输出繁体），英文界面要求英文，其它语言（社区语言包）要求"与提问同语言"。
#:
#: 只有两种内置语言，所以这张表很小；社区新增语言包时无需改代码，
#: 会自然落到 _REPLY_LANGUAGE_FALLBACK。这里的内容是**发给模型的指令**，
#: 不是界面文本，故按本项目约定不以语言包键存放（见文件头）。
_REPLY_LANGUAGE: Dict[str, str] = {
    "zh-Hans": "\n请始终使用简体中文回复，不要输出繁体字。",
    "en": "\nAlways reply in English.",
}

#: 未内置回复指令的语言：让模型跟随提问语言，避免替用户选错语言。
_REPLY_LANGUAGE_FALLBACK = "\n请使用与用户提问相同的语言回复。"


def reply_language_instruction() -> str:
    """按当前界面语言给出"用什么语言回复"的指令（见 ``_REPLY_LANGUAGE``）。"""
    try:
        from i18n import get as _i18n_get

        locale = _i18n_get().locale
    except Exception:  # i18n 不可用时不能连累出提示词
        locale = "zh-Hans"
    return _REPLY_LANGUAGE.get(locale, _REPLY_LANGUAGE_FALLBACK)

# 模式别名：编号 / 缩写 / 全称均可切换
MODE_ALIASES: Dict[str, str] = {
    "1": "general", "g": "general", "general": "general",
    "2": "summary", "s": "summary", "sum": "summary", "summary": "summary",
    "3": "translate", "t": "translate", "tr": "translate", "translate": "translate",
    "4": "code", "c": "code", "code": "code",
    "5": "contract", "ct": "contract", "contract": "contract",
    "6": "creative", "cr": "creative", "creative": "creative",
}

# 每个模式的展示缩写（用于 /mode list）
MODE_SHORT_NAMES: Dict[str, str] = {
    "general": "g", "summary": "s", "translate": "t",
    "code": "c", "contract": "ct", "creative": "cr",
}

# 模式 × 温度预设（用户手动 /temp 后以手动值为准）
MODE_TEMPERATURES: Dict[str, float] = {
    "general": 0.7,
    "summary": 0.4,
    "translate": 0.3,
    "code": 0.2,
    "contract": 0.3,
    "creative": 0.9,
}

# 用户询问"我刚才说了什么"时的意图识别
REPEAT_INTENT = re.compile(
    r"(刚才|刚刚|之前|上一条|上句|前面).{0,8}(说|讲|问|输入|发).{0,6}(什么|啥)"
    r"|我说过什么|我说了什么|我刚刚说了什么",
    re.S,
)

# 提问与已上传文件相关时，把文件全文注入上下文（其余时候只保留文件名指针，省上下文）
FILE_INTENT = re.compile(
    r"文件|文档|合同|条款|内容|里面|这个|上面|上文|上述|上传|审阅|基于|根据|摘要一下|读一下|看看",
    re.S,
)


class ConversationManager:
    """管理多轮对话历史、模式切换、文件上下文、自定义提示词与会话持久化。"""

    def __init__(self, max_history: int = 10, default_mode: str = "general") -> None:
        self.max_history = max_history
        self.mode = default_mode if default_mode in MODES else "general"
        self.history: List[dict] = []
        self.file_name = ""
        self.file_content = ""
        self.file_summary = ""          # 长文档分段摘要（有值时优先注入摘要而非全文）
        self.file_summary_meta: Dict = {}  # 摘要元信息：段数、原文字符数、原文指针等
        self.undo_stack: List[dict] = []
        self.custom_prompt = ""   # /prompt 的内容：优先级最高，替换模式块与任务层
        self.task_prompt = ""     # /task 套用的任务层正文：叠加在模式块之上
        self.summary = ""  # 早期对话的自动摘要（长会话压缩产物）
        # ---------- 思考内容（v0.2.1 模型思考内容分离） ----------
        # 这两个开关由 Backend 按 config.reasoning 注入：
        #   persist_reasoning=False 时不把思考写进历史（老会话的字段缺失照旧兼容）；
        #   reasoning_max_chars 限制每条思考的保留长度（0 = 不限制）。
        self.persist_reasoning = True
        self.reasoning_max_chars = 8000

    # ---------- 模式 ----------

    def set_mode(self, mode: str) -> bool:
        """按编号 / 缩写 / 全称切换模式。"""
        canonical = MODE_ALIASES.get(mode.strip().lower(), mode.strip().lower())
        if canonical in MODES:
            self.mode = canonical
            return True
        return False

    def get_mode(self) -> str:
        return self.mode

    @staticmethod
    def available_modes() -> Dict[str, str]:
        return dict(MODES)

    @staticmethod
    def mode_menu() -> str:
        """生成带编号、缩写与温度预设的模式清单文本。

        模式**说明**取自语言包（``mode.desc.<模式>``），它是界面文本；
        MODES 里的系统提示词正文是发给模型的内容，按设计保持中文（见文件头）。
        """
        lines = []
        for idx, name in enumerate(MODES, 1):
            temp = MODE_TEMPERATURES[name]
            lines.append(_t("mode.menu_item", index=idx, id=name,
                            short=MODE_SHORT_NAMES[name],
                            desc=_t(f"mode.desc.{name}"), temp=temp))
        return "\n".join(lines)

    # ---------- 历史记录 ----------

    def add_message(self, role: str, content: str, reasoning: str = "") -> None:
        """追加一条历史记录。

        ``reasoning``（v0.2.1 模型思考内容分离）是**该轮助手回复的思考内容**，
        单独存字段而不是拼进 ``content``：

        * 任务书任务五要求"重新加载含思考内容的历史会话，确认区分正确" ——
          不持久化就无从区分，所以默认存进历史；
        * 但它**不参与** :meth:`build_messages` 构造的上下文（见那里的说明），
          所以既不会污染回答文本，也不会白占上下文窗口。

        空思考内容不写字段：老会话（没有这个字段）与普通模型的会话保持原样，
        session.json 也不会因为一堆空字符串而变大。
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
        # 硬上限保护（正常由后端把超限部分压缩为摘要）
        if len(self.history) > self.max_history * 4:
            self.history = self.history[-self.max_history * 4 :]

    def _trim_reasoning(self, reasoning: str) -> str:
        """按配置截断思考内容（超长时保留头部并加省略说明）。"""
        text = (reasoning or "").strip()
        limit = int(self.reasoning_max_chars or 0)
        if not text or limit <= 0 or len(text) <= limit:
            return text
        return text[:limit] + _t("export.reasoning_truncated")

    def last_user_message(self) -> Optional[str]:
        """返回最近一条用户消息（用于"我刚才说了什么"等场景）。"""
        for msg in reversed(self.history):
            if msg["role"] == "user":
                return msg["content"]
        return None

    def undo(self) -> bool:
        """撤回最后一轮对话（user + assistant 两条）。"""
        if len(self.history) < 2:
            return False
        self.undo_stack.append(self.history.pop())
        self.undo_stack.append(self.history.pop())
        return True

    def clear(self) -> None:
        self.history.clear()
        self.undo_stack.clear()
        self.file_name = ""
        self.file_content = ""
        self.file_summary = ""
        self.file_summary_meta = {}
        self.summary = ""

    def export_markdown(self) -> str:
        """将对话历史导出为 Markdown 文本（标题行随界面语言切换）。

        思考内容单独作为可折叠块导出（``<details>``，GitHub / VS Code / Typora
        都能折叠）：导出要"完整"但也要"一眼分得清哪部分是思考"，直接拼在答案
        前面会让导出件读起来像跑题；丢掉又等于悄悄删了用户看过的内容。
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

    # ---------- 自定义提示词 / 任务层 ----------

    def set_custom_prompt(self, text: str) -> None:
        """设置 ``/prompt`` 的自定义指令（优先级最高，替换模式块与任务层）。"""
        self.custom_prompt = text.strip()

    def set_task_prompt(self, text: str) -> None:
        """设置 ``/task`` 的任务层正文（叠加在模式块之后，不替换模式）。

        与 ``set_custom_prompt`` 分开存，是为了让 ``/task off`` 只取消任务层、
        不会把用户用 ``/prompt`` 设的自定义指令一起清掉 —— v0.2.1 提示词优化
        之前两者共用 ``custom_prompt``，``/task off`` 会连自定义指令一起清空。
        """
        self.task_prompt = (text or "").strip()

    # ---------- 文件上下文 ----------

    def set_file(self, filepath: str, content: str) -> None:
        self.file_name = filepath
        self.file_content = content
        # 换文件时清掉旧摘要，避免张冠李戴
        self.file_summary = ""
        self.file_summary_meta = {}

    def set_file_summary(self, summary: str, meta: Optional[Dict] = None) -> None:
        """记录长文档分段摘要与其元信息（段数/原文字符数/原文指针）。"""
        self.file_summary = summary or ""
        self.file_summary_meta = meta or {}

    def has_file(self) -> bool:
        return bool(self.file_content)

    # ---------- Prompt 构建 ----------

    def get_system_prompt(self) -> str:
        """拼出当前生效的系统提示词（层次结构见文件头）。

        层次（优先级从高到低）：
          1. ``/prompt`` 的自定义指令 —— **替换**模式块与任务层（用户明确要接管）；
          2. 模式块（``MODES``）；
          3. ``/task`` 的任务层 —— **叠加**在模式块之后（不再是替换模式）；
          4. 共享主干 ``_CORE_RULES``（视角 / 边界 / 输出纪律）；
          5. 语言指令 ``reply_language_instruction()``（跟随界面语言）；
          6. 早期对话摘要与已上传文件指针。

        多语言说明：下面所有中文字符串（``[自定义指令]`` / ``[通用要求]`` /
        ``【早期对话摘要】`` / ``【已上传文件】`` 等）都是**发给模型的提示词内容**，
        不是界面文本，故刻意不进语言包；真正随界面语言变化的是第 5 层。
        """
        if self.custom_prompt:
            # 自定义指令优先：以更强的措辞包裹，帮助小模型严格遵守。
            # 「不要反问」后面刻意留了例外：用户信息不足时需要先追问
            # （指向用户的 task.clarify_prompt 与会话层的模糊提问检测都依赖追问），
            # 写死"不许反问"会让提示词自相矛盾 —— v0.2.1 提示词优化时发现并修掉。
            prompt = (
                f"[自定义指令]\n{self.custom_prompt}\n\n"
                f"[执行要求] 你必须严格遵守上述自定义指令，直接输出用户要求的内容，"
                f"不要解释、不要偏离；但用户提供的信息不足以作答时，应先问清最关键的一点。"
            )
        else:
            prompt = MODES.get(self.mode, MODES["general"])
            if self.task_prompt:
                # 任务层**叠加**而不是替换模式：两层各自负责不同粒度
                # （模式=场景与语气，任务=本轮的具体输出格式），
                # prompts.build_system_prompt 已在末尾给出「冲突时以本节为准」的优先级行。
                prompt += "\n\n" + self.task_prompt
        prompt += _CORE_RULES
        prompt += reply_language_instruction()
        if self.summary:
            prompt += f"\n\n【早期对话摘要】\n{self.summary}"
        if self.file_content:
            # 只保留文件名指针，完整内容在用户提问与文件相关时随问题注入
            pointer = f"\n\n【已上传文件】: {self.file_name}\n（如需基于文件内容回答，请在问题中提及）"
            if self.file_summary:
                meta = self.file_summary_meta or {}
                chunks = meta.get("chunks")
                chars = meta.get("origin_chars")
                detail = ""
                if chunks and chars:
                    detail = f"（原文 {chars} 字符，已分 {chunks} 段摘要）"
                pointer += f"\n【长文档摘要已就绪】{detail} 该文档过长，提问时将注入分段摘要而非全文。"
            prompt += pointer
        return prompt

    def build_messages(self, user_query: str, search_context: str = "", include_file: bool = False) -> List[dict]:
        """构造发送给模型的完整消息列表（系统提示 + 历史 + 当前问题）。

        include_file=True 且已上传文件时，把文件内容注入到当前问题之前：
        有分段摘要则注入摘要（含原文指针），否则注入全文。

        这里拼接的 ``【{label}】`` / ``参考以下网络信息：`` 同属提示词内容，
        按文件头说明保持中文、不进语言包。

        **思考内容刻意不进上下文**（v0.2.1 模型思考内容分离的自行判断项）：
        历史记录里的 ``reasoning`` 字段只用于界面回放，不拼进 messages。理由有三：

        1. 思考通常比答案还长，重新喂回去会成倍占用上下文窗口，而它对"下一轮
           该说什么"几乎没有帮助（结论已经在 ``content`` 里了）；
        2. DeepSeek 官方接口明确要求不要把 ``reasoning_content`` 传回下一轮；
        3. DeepSeek-R1 蒸馏版的 GGUF 对话模板自己就在做同一件事 ——
           ``{% if '</think>' in content %}{% set content = content.split('</think>')[-1] %}``
           （实测见 records/v0.2.1-模型思考内容分离记录.md）：模型侧也只接受答案。
        """
        messages: List[dict] = [{"role": "system", "content": self.get_system_prompt()}]
        for msg in self.history:
            # 只取 role/content：timestamp 与 reasoning 都不是模型输入
            messages.append({"role": msg["role"], "content": msg["content"]})

        user_content = f"参考以下网络信息：\n{search_context}\n\n问题：{user_query}" if search_context else user_query
        if include_file and self.file_content:
            body = self.file_summary or self.file_content
            label = "文件内容（长文档摘要）" if self.file_summary else "文件内容"
            user_content = f"【{label}】\n{body}\n\n{user_content}"
        messages.append({"role": "user", "content": user_content})
        return messages

    # ---------- 会话持久化 ----------

    def save_session(self, path: str, model_path: str = "") -> None:
        """把模式、提示词、摘要、文件、模型与历史保存到 JSON 文件。"""
        data = {
            "mode": self.mode,
            "custom_prompt": self.custom_prompt,
            "task_prompt": self.task_prompt,
            "summary": self.summary,
            "model_path": model_path,
            "file_name": self.file_name,
            "file_content": self.file_content,
            "file_summary": self.file_summary,
            "file_summary_meta": self.file_summary_meta,
            "history": self.history,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_session(self, path: str):
        """从 JSON 文件恢复会话。

        返回 (成功且有历史, 上次使用的模型路径)。

        用 ``utf-8-sig`` 读：会话文件由本程序写入（无 BOM），但用户手工编辑过之后
        记事本可能加上 BOM —— 老代码会在这里抛异常并被下面的 ``except`` 吞掉，
        表现为"历史莫名其妙没了"，而不是给出原因（v0.2.1 模型思考内容分离时
        顺带加固，与 config.py 的处理保持一致）。
        """
        if not os.path.exists(path):
            return False, ""
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if data.get("mode") in MODES:
                self.mode = data["mode"]
            self.custom_prompt = data.get("custom_prompt", "")
            # 旧会话没有 task_prompt 字段（v0.2.1 提示词优化之前任务模板写在
            # custom_prompt 里）——缺字段时自然取默认空串，老会话仍能正常恢复。
            self.task_prompt = data.get("task_prompt", "")
            self.summary = data.get("summary", "")
            self.file_name = data.get("file_name", "")
            self.file_content = data.get("file_content", "")
            self.file_summary = data.get("file_summary", "")
            self.file_summary_meta = data.get("file_summary_meta", {}) or {}
            self.history = data.get("history", [])
            return bool(self.history), str(data.get("model_path", ""))
        except Exception:
            return False, ""
