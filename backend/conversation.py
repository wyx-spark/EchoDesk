"""对话管理：模式、历史记录、文件上下文、撤回、导出、持久化与摘要压缩。"""
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

# 各模式对应的系统提示词（插入顺序即编号顺序：1..N）
MODES: Dict[str, str] = {
    "general": "你是一个通用AI助手，可以回答各种问题。",
    "summary": "你是一个专业的摘要专家，擅长简洁准确地总结文本、会议记录、文章等。请提供清晰、有层次的摘要。",
    "translate": "你是一个翻译专家，精通多国语言。请根据用户要求进行准确流畅的翻译。",
    "code": "你是一个编程专家，擅长多种编程语言。请提供代码、解释、调试建议等。",
    "contract": "你是一个专业的合同审查与摘要助手。请根据用户需求，结合提供的文件内容进行回答。",
    "creative": "你是一个创意写作助手，擅长生成故事、诗歌、剧本等创意内容。",
}

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
        self.undo_stack: List[dict] = []
        self.custom_prompt = ""
        self.summary = ""  # 早期对话的自动摘要（长会话压缩产物）

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
        """生成带编号、缩写与温度预设的模式清单文本。"""
        lines = []
        for idx, (name, desc) in enumerate(MODES.items(), 1):
            temp = MODE_TEMPERATURES[name]
            lines.append(f"{idx}. {name} ({MODE_SHORT_NAMES[name]}) — {desc}（温度 {temp}）")
        return "\n".join(lines)

    # ---------- 历史记录 ----------

    def add_message(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content, "timestamp": datetime.now().isoformat()})
        # 硬上限保护（正常由后端把超限部分压缩为摘要）
        if len(self.history) > self.max_history * 4:
            self.history = self.history[-self.max_history * 4 :]

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
        self.summary = ""

    def export_markdown(self) -> str:
        """将对话历史导出为 Markdown 文本。"""
        lines = ["# EchoDesk 对话记录", f"- 模式：{self.mode}", ""]
        for msg in self.history:
            icon = "👤 用户" if msg["role"] == "user" else "🤖 助手"
            lines.append(f"**{icon}** ({msg['timestamp']})")
            lines.append(msg["content"])
            lines.append("")
        return "\n".join(lines)

    # ---------- 自定义提示词 ----------

    def set_custom_prompt(self, text: str) -> None:
        self.custom_prompt = text.strip()

    # ---------- 文件上下文 ----------

    def set_file(self, filepath: str, content: str) -> None:
        self.file_name = filepath
        self.file_content = content

    # ---------- Prompt 构建 ----------

    def get_system_prompt(self) -> str:
        if self.custom_prompt:
            # 自定义指令优先：以更强的措辞包裹，帮助小模型严格遵守
            prompt = (
                f"[自定义指令]\n{self.custom_prompt}\n\n"
                f"[执行要求] 你必须严格遵守上述自定义指令，直接输出用户要求的内容，不要解释，不要反问，不要偏离。"
            )
        else:
            prompt = MODES.get(self.mode, MODES["general"])
        # 角色澄清：小模型容易混淆"你"与"用户"的视角
        prompt += (
            "\n\n对话规则：这里的'用户'指正在提问的人类，'你'是助手。"
            "当用户问'我刚才说了什么'或要求复述时，应引用用户最近一次输入的内容，而不是你自己的回复。"
        )
        # 始终使用简体中文输出（0.8B 模型偶发输出繁体字）
        prompt += "\n请始终使用简体中文回复。"
        if self.summary:
            prompt += f"\n\n【早期对话摘要】\n{self.summary}"
        if self.file_content:
            # 只保留文件名指针，完整内容在用户提问与文件相关时随问题注入
            prompt += f"\n\n【已上传文件】: {self.file_name}\n（如需基于文件内容回答，请在问题中提及）"
        return prompt

    def build_messages(self, user_query: str, search_context: str = "", include_file: bool = False) -> List[dict]:
        """构造发送给模型的完整消息列表（系统提示 + 历史 + 当前问题）。

        include_file=True 且已上传文件时，把文件全文注入到当前问题之前。
        """
        messages: List[dict] = [{"role": "system", "content": self.get_system_prompt()}]
        for msg in self.history:
            messages.append({"role": msg["role"], "content": msg["content"]})

        user_content = f"参考以下网络信息：\n{search_context}\n\n问题：{user_query}" if search_context else user_query
        if include_file and self.file_content:
            user_content = f"【文件内容】\n{self.file_content}\n\n{user_content}"
        messages.append({"role": "user", "content": user_content})
        return messages

    # ---------- 会话持久化 ----------

    def save_session(self, path: str, model_path: str = "") -> None:
        """把模式、提示词、摘要、文件、模型与历史保存到 JSON 文件。"""
        data = {
            "mode": self.mode,
            "custom_prompt": self.custom_prompt,
            "summary": self.summary,
            "model_path": model_path,
            "file_name": self.file_name,
            "file_content": self.file_content,
            "history": self.history,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_session(self, path: str):
        """从 JSON 文件恢复会话。

        返回 (成功且有历史, 上次使用的模型路径)。
        """
        if not os.path.exists(path):
            return False, ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("mode") in MODES:
                self.mode = data["mode"]
            self.custom_prompt = data.get("custom_prompt", "")
            self.summary = data.get("summary", "")
            self.file_name = data.get("file_name", "")
            self.file_content = data.get("file_content", "")
            self.history = data.get("history", [])
            return bool(self.history), str(data.get("model_path", ""))
        except Exception:
            return False, ""
