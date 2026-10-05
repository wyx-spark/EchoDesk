"""对话管理：模式、历史记录、文件上下文、撤回、导出、持久化与摘要压缩。"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

# 多语言（任务三）：界面文本（模式菜单、导出 Markdown 的标题行）走语言包。
# i18n 只依赖标准库（config 在 i18n 内部惰性导入），可直接在这里导入。
from i18n import t as _t

# ===========================================================================
# 系统提示词（v0.2.1 提示词优化 → 提示词重构 → 能力扩展-1 模式简化）
#
# 结构：**模式块 + 共享主干 + 语言指令 +（可选）任务层 +（可选）自定义指令**
#
#   1. **模式块**（``MODES``）：**3 个**模式，各带六要素——角色 / 输出格式 /
#      **详略判断** / 思维链 / 少样本示例 / 语气，再加该场景特有的边界。
#      详略那一项是"提示词重构"轮加的：上一轮补齐六要素后约束加码过多，
#      模型变成"只按模板办事"（简单问题也列三条、信息不全就拒绝回答）；
#      详略把"该说多少"的判断权交回模型。
#      **模式数量**是"能力扩展-1 任务一"重新定的：从 7 个收敛到 3 个
#      （chat / contract / agent），理由与迁移方案见下面 MODE_ALIASES 上方的长注释。
#   2. **共享主干**（``_CORE_RULES``）：所有模式共用的七条通用纪律（视角、边界、
#      详略、先答后问、摊开推测、引导、输出纪律）。**刻意共享而不是每个模式各写一遍**：
#      这些与场景无关，写 3 遍既白占上下文（0.8B 模型对冗长提示词更敏感），
#      又会在改动时留下三处不一致。
#   3. **语言指令**（``_REPLY_LANGUAGE``）：跟随**界面语言**，见下方说明。
#   4. **任务层**：``/task`` 套用的模板，**叠加**在模式之上而不是替换它
#      （见 :meth:`ConversationManager.get_system_prompt` 与 ``prompts.build_system_prompt``）。
#      **被移除的四个模式（summary/translate/code/creative）与 guide 的能力就住在这里。**
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
#   * 单个模式块 ≤ 480 字符；
#   * 模式块 + 共享主干 + 语言指令 ≤ 700 字符。
#   默认上下文 8192 token，中文约 1 字符/token 量级，700 字符约占 8.5%，
#   余量留给文件摘要与对话历史。上限由 ``tools/test_capability.py`` 断言守住。
#   **两次重构都没有放宽这两条上限**：新增内容是靠压缩原有措辞腾出空间的。
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

#: 所有模式共用的主干（视角 / 边界 / 判断详略 / 输出纪律）。
#: 与场景无关，故共享一份而不是在每个模式里重复——见文件头"长度预算"说明。
#:
#: **v0.2.1 系统提示词重构**：上一轮给每个模式补齐六要素后，约束加码过多，模型变得
#: 「只按模板办事、没有判断力」（任务书三个现象：一律分点、信息不全就拒绝回答、
#: 用户说不清时帮不上忙）。第 4~6 条就是这次的"放松"层：**把顺序反过来**
#: （先给最可能的答案 → 再问要不要展开）、把推测摊开、把详略判断权下放。
#: 放松的同时第 2 条"不编造"与各模式里的硬格式要求（摘录逐字一致、免责提示）
#: 一字未动 —— 那是底线，不是约束过头。
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

# ===========================================================================
# 对话模式简化（v0.2.1 能力扩展-1 任务一）
#
# **三个模式，三种语义**：chat（通用对话）/ contract（条款审查）/ agent（可调工具的智能体）。
# 判定标准取自任务书：用户要能一眼说清"我现在该用哪个"，而不是"这个任务该配哪段提示词"。
#
# 为什么现在是 3 个而不是上一版的 7 个（general/summary/translate/code/contract/creative/guide）：
#   * summary / translate / code / creative 的差别**只是提示词不同**，不是语义不同——
#     它们是"同一场对话里的临时任务"，本来就该由 ``/task`` 模板承担（模板本来就是
#     "本轮输出格式"的层）。这四个模式块的正文没有丢，全部搬进了对应的任务模板。
#   * guide（引导）同理：它的"列 2~3 种可能方向"已经写进共享主干第 6 条（所有模式通用），
#     模型驱动的版本搬进 ``/task guide``。
#   * contract 保留为独立模式：它带专业角色与**硬输出要求**（原文逐字一致、必须提示
#     人工复核），是"对话语气与边界都不同"的场景，合并会丢能力（任务书明确要求保留）。
#   * agent 是新增的第三种语义：模型自主决定调用工具（与"预定义流程的批量执行"
#     的自动化是两件事）。
#
# **旧名字仍然可用**（``LEGACY_MODES``）：``/mode summary`` 会切到 chat 并把 summary
# 模板套上，同时把温度预设也沿用旧值 —— 用户不用改习惯，能力一点没少，只是不再占一个模式位。
# ===========================================================================

#: 模式别名：编号 / 缩写 / 全称 / **旧模式名**均可切换。
#:
#: **编号只认新的 1/2/3**（chat/contract/agent）。上一版的 4/5/6/7 不再映射到任何模式 ——
#: 它们与新编号在语义上会打架（旧 `/mode 2` 是 summary，新 `/mode 2` 是 contract），
#: 与其让"同一个数字在两类用户眼里指两件事"，不如明确报错并告诉用户该用哪个模板
#: （见 `LEGACY_NUMBERS` 与 `command_handler._mode`）。
MODE_ALIASES: Dict[str, str] = {
    "1": "chat", "c": "chat", "chat": "chat",
    "2": "contract", "ct": "contract", "contract": "contract",
    "3": "agent", "ag": "agent", "agent": "agent",
    # ---- 上一版的模式名：仍然可用，会自动切模式并套上同名模板（见 LEGACY_MODES） ----
    "general": "chat", "g": "chat",
    "summary": "chat", "s": "chat", "sum": "chat",
    "translate": "chat", "t": "chat", "tr": "chat",
    "code": "chat",
    "creative": "chat", "cr": "chat",
    "guide": "chat", "gd": "chat",
}

#: 旧模式名 → 迁移后要自动套用的任务模板 id（``""`` 表示只切模式、不套模板）。
#: ``/mode summary`` 因此等于"切到 chat + /task summary"，能力不丢、概念少一个。
LEGACY_MODES: Dict[str, str] = {
    "summary": "summary",
    "translate": "translate",
    "code": "code",
    "creative": "creative",
    "guide": "guide",
    "general": "",
}

#: 当前编号（1/2/3）。单独抽出来是为了让"数字属于新编号还是旧编号"这件事**只有一处判据**：
#: `command_handler._mode` 用它决定"这是新编号（切模式）还是旧编号（报错指路）"。
MODE_NUMBERS: Dict[str, str] = {"1": "chat", "2": "contract", "3": "agent"}

#: 旧编号 → 旧模式名。**只用来给出有用的报错**，不再用来切模式或套模板：
#: 上一版 ``/mode 2`` 是 summary、``/mode 6`` 是 creative，而新编号里 ``/mode 2``
#: 是 contract、``/mode 3`` 是 agent —— 同一个数字在两套编号里指不同东西。
#: 让数字继续"自动迁移"会出现"我按旧习惯敲 /mode 2，结果进了合同审查模式还套了摘要模板"
#: 这种既不是旧语义也不是新语义的状态（文档子代理按实测发现过这个组合）。
#: 所以：数字只认新编号，旧编号直接报错并指路；旧**模式名**（不带歧义）才自动迁移。
LEGACY_NUMBERS: Dict[str, str] = {
    "1": "general", "2": "summary", "3": "translate", "4": "code",
    "5": "contract", "6": "creative", "7": "guide",
}

#: 旧模式名 → 温度预设。迁移时沿用，避免"合并之后回答风格也变了"。
LEGACY_TEMPERATURES: Dict[str, float] = {
    "summary": 0.4, "translate": 0.3, "code": 0.2, "creative": 0.9, "guide": 0.6,
}

# 每个模式的展示缩写（用于 /mode list）
MODE_SHORT_NAMES: Dict[str, str] = {
    "chat": "c", "contract": "ct", "agent": "ag",
}

# 模式 × 温度预设（用户手动 /temp 后以手动值为准）
MODE_TEMPERATURES: Dict[str, float] = {
    "chat": 0.7,
    # 审查要稳：宁可保守也不要发挥。
    "contract": 0.3,
    # 智能体要"会用工具"但别乱调：0.5 比 code(0.2) 敢试、比 creative(0.9) 收得住。
    "agent": 0.5,
}


def canonical_mode(name: str) -> str:
    """把任意写法（编号/缩写/全称/旧模式名）解析成正式模式名；无效则回 chat。

    配置里可能存着旧名字（``default_mode: "summary"``），所以**加载路径也走它**，
    而不是只在 ``/mode`` 里迁移。
    """
    key = (name or "").strip().lower()
    return MODE_ALIASES.get(key, key if key in MODES else "chat")


def legacy_template(name: str) -> str:
    """旧**模式名**对应要套用的任务模板 id；不是旧名字则返回空串。

    刻意**不认旧编号**（见 ``LEGACY_NUMBERS`` 的说明）：编号在两套编号体系里会歧义，
    由 ``command_handler._mode`` 单独报错指路。
    """
    return LEGACY_MODES.get((name or "").strip().lower(), "")


def legacy_number_hint(token: str) -> str:
    """旧编号 → 建议的替代写法（不是旧编号则返回空串）。

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
    """``/mode list`` 末尾的迁移说明（界面文本，由调用方取语言包渲染）。"""
    return ", ".join(f"{name}→{template}" for name, template in LEGACY_MODES.items() if template)

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


# ===========================================================================
# 多文件上下文（v0.2.1 优化已知问题-2 任务八）
# ===========================================================================
#
# 任务书要求支持一次上传多个文件，并把"多个文件内容如何注入"作为需要明确的点：
# "全部注入、按相关性选择、或用户指定"。
#
# **本实现的决策：全部注入，但总长受 ``config.files.total_chars`` 约束**，
# 超出时按"后上传的优先"注入并如实告知用户哪些文件没进上下文。
# 理由：本地小模型没有可靠的"哪份文件相关"判断能力，用模型做相关性筛选要额外
# 推理且不稳定；而用户一次拖进来的文件通常本来就是同一件事的资料。
# 与其猜，不如全部给模型 + 明确告知取舍。

#: 单个文件超过这个字符数就**落盘**保存，上下文里只放"文件指针 + 摘要"。
#: 任务书任务九的"工具输出单独处理"在文件侧的同一条思路：大块内容不该整段
#: 塞进上下文，但要留下可回查的指针。
_DISK_THRESHOLD_CHARS = 20000


@dataclass
class FileEntry:
    """一份已加载文件：内容、摘要与"内容放在哪"。"""

    name: str = ""                       # 文件名（用于展示与上下文指针）
    path: str = ""                       # 原始路径（可能为空：同名文件去重后沿用首个）
    content: str = ""                    # 全文（落在磁盘时为空串）
    summary: str = ""                    # 分段摘要（有值时优先注入摘要）
    summary_meta: Dict = field(default_factory=dict)
    chars: int = 0                       # 原文字符数（落盘后仍要能显示体积）
    stored_at: str = ""                  # 落盘路径（空 = 内容在内存里）

    def inject_body(self) -> str:
        """注入上下文时用的正文（摘要优先，落盘时给指针）。"""
        if self.summary:
            return self.summary
        if self.content:
            return self.content
        if self.stored_at:
            return (f"（文件正文过大，已保存到 {self.stored_at}；"
                    f"共 {self.chars} 字符，需要原文时请让用户用 /file 重新上传或直接指定该路径）")
        return ""

    def to_dict(self) -> Dict:
        """会话持久化用的字典（**不写正文**：正文落盘或由文件名回指）。"""
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
    """管理多轮对话历史、模式切换、文件上下文、自定义提示词与会话持久化。"""

    def __init__(self, max_history: int = 10, default_mode: str = "chat") -> None:
        self.max_history = max_history
        # 走 canonical_mode 而不是直接比对：**配置与会话里可能存着旧模式名**
        # （`default_mode: "general"`、会话文件里的 `"mode": "summary"`），
        # 迁移逻辑只有一份，加载路径与 /mode 命令都用它。
        self.mode = canonical_mode(default_mode)
        self.history: List[dict] = []
        # ---------- 多文件（任务八） ----------
        # files 是**有序**列表（上传顺序），active_index 指向"当前操作的那一份"
        # （/summarize、/status、上下文指针都以它为准）。
        # file_name / file_content / file_summary / file_summary_meta 四个老字段
        # 保留为**只读属性代理**（见下方 property），因此既有调用方与测试不用改。
        self.files: List[FileEntry] = []
        self.active_index: int = -1
        self.undo_stack: List[dict] = []
        self.custom_prompt = ""   # /prompt 的内容：优先级最高，替换模式块与任务层
        self.task_prompt = ""     # /task 套用的任务层正文：叠加在模式块之上
        self.task_template = ""   # 当前任务层的模板 id（供 /status 与引导短路判断）
        self.summary = ""  # 早期对话的自动摘要（长会话压缩产物）
        # ---------- 思考内容（v0.2.1 模型思考内容分离） ----------
        # 这两个开关由 Backend 按 config.reasoning 注入：
        #   persist_reasoning=False 时不把思考写进历史（老会话的字段缺失照旧兼容）；
        #   reasoning_max_chars 限制每条思考的保留长度（0 = 不限制）。
        self.persist_reasoning = True
        self.reasoning_max_chars = 8000

    # ---------- 多文件：老字段的兼容代理（任务八） ----------
    #
    # 为什么要留这四个属性：`file_name` / `file_content` / `file_summary` /
    # `file_summary_meta` 在指令、状态显示、摘要触发、会话读写、测试里被引用了
    # 几十处。改成列表后如果逐一改写调用点，任何一处漏改都是运行时崩溃；
    # 用属性代理把它们统一映射到"当前活动文件"，语义与单文件时代完全一致，
    # 新代码走 files 列表即可。

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
        # 兼容直接赋值（老代码/测试）：没有活动文件时补一条空壳，便于逐字段构造
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

    # ---------- 多文件：真正的 API（任务八） ----------

    def add_file(self, filepath: str, content: str) -> FileEntry:
        """登记一份文件并把**它**设为当前活动文件。

        同名文件（按文件名比较）视为重复上传：**替换**内容而不是追加第二条 ——
        否则同一份合同拖两次就会在上下文里出现两遍，白占窗口且让模型困惑。
        替换时保留原有位置，活动指针指过去。
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
        """移除第 ``index`` 份文件（任务书要求"用户可单独移除某个文件"）。"""
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
        """切换"当前活动文件"（/summarize 等指令作用于它）。"""
        if 0 <= index < len(self.files):
            self.active_index = index
            return True
        return False

    def file_names(self) -> List[str]:
        return [entry.name for entry in self.files]

    def total_chars(self) -> int:
        return sum(entry.chars for entry in self.files)

    def has_file(self) -> bool:
        """是否至少上传了一份文件（老 API；多文件下等价于 ``bool(self.files)``）。"""
        return bool(self.files)

    def set_entry_summary(self, entry: FileEntry, summary: str,
                          meta: Optional[Dict] = None) -> None:
        """给**指定**文件写摘要（多文件下摘要作用于哪一份必须显式指定）。"""
        entry.summary = summary or ""
        entry.summary_meta = meta or {}

    # ---------- 模式 ----------

    def set_mode(self, mode: str) -> bool:
        """按编号 / 缩写 / 全称切换模式（**旧模式名同样有效**，见 ``LEGACY_MODES``）。"""
        key = (mode or "").strip().lower()
        if key not in MODE_ALIASES and key not in MODES:
            return False
        self.mode = canonical_mode(key)
        return True

    def migrated_template(self, mode: str) -> str:
        """旧模式名需要一并套用的任务模板 id（不是旧名字则返回空串）。

        ``/mode summary`` 的语义 = "切到 chat + 套用 summary 模板"，
        所以调用方（``command_handler._mode``）用它决定要不要设任务层。
        """
        return legacy_template(mode)

    @staticmethod
    def legacy_number_hint(token: str) -> str:
        """旧编号的替代写法建议（供 ``/mode 4`` 这类旧写法报错时指路）。"""
        return legacy_number_hint(token)

    def get_mode(self) -> str:
        return self.mode

    @staticmethod
    def available_modes() -> Dict[str, str]:
        return dict(MODES)

    @staticmethod
    def mode_menu() -> str:
        """生成带编号、缩写与温度预设的模式清单文本（模式简化后只剩 3 个）。

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

    @staticmethod
    def legacy_mode_note() -> str:
        """``/mode list`` 末尾的"旧模式名仍可用"提示（界面文本，走语言包）。"""
        pairs = ", ".join(f"{name} → /task {template}"
                          for name, template in LEGACY_MODES.items() if template)
        return _t("cmd.resp.mode_legacy", pairs=pairs)

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
        self.files.clear()
        self.active_index = -1
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

    def set_task_prompt(self, text: str, template: str = "") -> None:
        """设置 ``/task`` 的任务层正文（叠加在模式块之后，不替换模式）。

        与 ``set_custom_prompt`` 分开存，是为了让 ``/task off`` 只取消任务层、
        不会把用户用 ``/prompt`` 设的自定义指令一起清掉 —— v0.2.1 提示词优化
        之前两者共用 ``custom_prompt``，``/task off`` 会连自定义指令一起清空。

        ``template`` 是模板 id（如 ``"guide"``）：模式简化之后需要知道"当前任务层
        是哪一份"，才能让 ``/task guide`` 与旧的 guide 模式行为一致（见
        ``main._vague_guide``：套了 guide 模板时不再插确定性引导话术）。
        """
        self.task_prompt = (text or "").strip()
        self.task_template = (template or "").strip() if self.task_prompt else ""

    # ---------- 文件上下文 ----------

    def set_file(self, filepath: str, content: str) -> None:
        """登记并激活一份文件（老 API；等价于 :meth:`add_file`）。

        保留它是为了不改既有调用方（``/file``、后端加载路径、测试）。
        多文件下语义是"再加一份并激活它"，与单文件时代"覆盖当前文件"一致。
        """
        self.add_file(filepath, content)

    def set_file_summary(self, summary: str, meta: Optional[Dict] = None) -> None:
        """记录**当前活动文件**的分段摘要与其元信息（段数/原文字符数/原文指针）。"""
        entry = self._active
        if entry is None:
            self.files.append(FileEntry(name="", path=""))
            self.active_index = len(self.files) - 1
            entry = self._active
        self.set_entry_summary(entry, summary, meta)

    # ---------- Prompt 构建 ----------

    def get_system_prompt(self) -> str:
        """拼出当前生效的系统提示词（层次结构见文件头）。

        层次（优先级从高到低）：
          1. ``/prompt`` 的自定义指令 —— **替换**模式块与任务层（用户明确要接管）；
          2. 模式块（``MODES``）；
          3. ``/task`` 的任务层 —— **叠加**在模式块之后（不再是替换模式）；
          4. 共享主干 ``_CORE_RULES``（视角 / 边界 / 详略 / 先答后问 / 引导 / 输出纪律）；
          5. 语言指令 ``reply_language_instruction()``（跟随界面语言）；
          6. 早期对话摘要与已上传文件指针。

        多语言说明：下面所有中文字符串（``[自定义指令]`` / ``[通用要求]`` /
        ``【早期对话摘要】`` / ``【已上传文件】`` 等）都是**发给模型的提示词内容**，
        不是界面文本，故刻意不进语言包；真正随界面语言变化的是第 5 层。
        """
        if self.custom_prompt:
            # 自定义指令优先：以更强的措辞包裹，帮助小模型严格遵守。
            # 「不要反问」后面刻意留了例外：用户信息不足时需要先答后问，
            # 写死"不许反问"会让提示词自相矛盾 —— v0.2.1 提示词优化时发现并修掉；
            # v0.2.1 系统提示词重构又把这条例外从"先问清最关键的一点"改成
            # "先给最可能的答案再问"，与共享主干的第 4 条保持一致。
            prompt = (
                f"[自定义指令]\n{self.custom_prompt}\n\n"
                f"[执行要求] 你必须严格遵守上述自定义指令，直接输出用户要求的内容，"
                f"不要解释、不要偏离；但用户提供的信息不足时，也要先给出最可能的答案"
                f"并说明基于什么假设，再问要不要按其它情况展开。"
            )
        else:
            prompt = MODES.get(self.mode, MODES["chat"])
            if self.task_prompt:
                # 任务层**叠加**而不是替换模式：两层各自负责不同粒度
                # （模式=场景与语气，任务=本轮的具体输出格式），
                # prompts.build_system_prompt 已在末尾给出「冲突时以本节为准」的优先级行。
                prompt += "\n\n" + self.task_prompt
        prompt += _CORE_RULES
        prompt += reply_language_instruction()
        if self.summary:
            prompt += f"\n\n【早期对话摘要】\n{self.summary}"
        if self.files:
            # 只保留文件名清单（含"哪一份是当前文件"），完整内容在用户提问与
            # 文件相关时随问题注入 —— 与单文件时代同一策略，只是清单变成多行。
            prompt += self._file_pointer()
        return prompt

    def _file_pointer(self) -> str:
        """系统提示里的"已上传文件"清单（多文件：逐条列出 + 标注当前文件）。"""
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
            pointer += "\n（如需基于文件内容回答，请在问题中提及）"
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
        """构造发送给模型的完整消息列表（系统提示 + 历史 + 当前问题）。

        include_file=True 且已上传文件时，把文件内容注入到当前问题之前：
        有分段摘要则注入摘要（含原文指针），否则注入全文。

        **多文件（任务八）**：逐份注入，每份前带文件名标签（``【文件 N/共 M：名字】``），
        并按 ``config.files.total_chars`` 限制总长；从**后上传的开始装**（用户刚拖进来
        的通常就是这次要用的），装不下的文件不注入，由调用方按返回的
        :meth:`last_injection_note` 如实告知用户 —— 静默少注入几份文件比报错更糟。

        **检索增强（能力扩展-1 任务六）**：``retrieval_context`` 是 ``retrieval.Retriever``
        产出的"相关片段 + 位置"，排在文件块**之前**。它由调用方（``Backend``）决定要不要
        传：只有在"启用了检索、且文件是长文档（已落盘或有摘要）"时才传 —— 短文档全文
        注入本来就更准，检索只会丢信息。这条分工见 ``backend/retrieval.py`` 的模块说明。

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
        if include_file and self.files:
            self._injection_note = ""
            blocks: List[str] = []
            budget = int(getattr(self, "files_total_chars", 0) or 0)
            used = 0
            skipped: List[str] = []
            total = len(self.files)
            # 从后往前装：刚上传的优先进入上下文
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
            # 检索片段排在**所有前置材料的最前面**：模型先看到"定位到的依据"，
            # 再看整份文件的摘要/全文，最后才是问题 —— 与"引用要能对上"的顺序一致。
            # （首轮实测把它放在了文件块**之后**，因为文件块是往 user_content 前面
            # 拼接的；顺序反了会让小模型把检索片段当成附注而忽略引用要求。）
            user_content = f"{retrieval_context}\n\n{user_content}"
        messages.append({"role": "user", "content": user_content})
        return messages

    def last_injection_note(self) -> str:
        """上一次 :meth:`build_messages` 注入多文件时的提示（无则空串）。

        任务书要求"总内容超出窗口时触发分段摘要，或提示用户选择部分文件"——
        本实现选后者（提示），把取舍如实说出来，而不是悄悄丢掉几份文件。
        """
        return str(getattr(self, "_injection_note", "") or "")

    # ---------- 会话持久化 ----------

    def save_session(self, path: str, model_path: str = "") -> None:
        """把模式、提示词、摘要、文件、模型与历史保存到 JSON 文件。

        **多文件（任务八）**：``files`` 写成字典列表（**不写正文**，只写文件名、
        摘要、体积与落盘路径）。理由：正文动辄几十万字，写进 session.json 会让
        会话文件膨胀到几十 MB，而它随时可以从原文件重新解析；
        真正需要持久化的是"用户上传过哪些文件、它们的摘要是什么"。
        同时保留 ``file_name``/``file_*`` 旧字段（指向当前活动文件），
        使**旧版本读新会话**或**新版本读旧会话**都不会缺字段。
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
        """从 JSON 文件恢复会话。

        返回 (成功且有历史, 上次使用的模型路径)。

        用 ``utf-8-sig`` 读：会话文件由本程序写入（无 BOM），但用户手工编辑过之后
        记事本可能加上 BOM —— 老代码会在这里抛异常并被下面的 ``except`` 吞掉，
        表现为"历史莫名其妙没了"，而不是给出原因（v0.2.1 模型思考内容分离时
        顺带加固，与 config.py 的处理保持一致）。

        **多文件兼容（任务八）**：新会话有 ``files`` 列表；旧会话只有
        ``file_name``/``file_content``/``file_summary`` 三个标量字段 —— 两者都认，
        旧会话被自动升级成"只有一份文件的列表"。正文不落盘，因此恢复后的文件
        条目**没有正文**，只有文件名、体积与摘要；用户再次提问涉及文件内容时
        会按摘要注入（若当时没生成摘要，提示需要重新上传 —— 见 Backend 的说明）。
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
