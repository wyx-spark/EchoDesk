"""指令解析：处理以 '/' 开头的用户指令。"""
import os
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from config import Config
from conversation import ConversationManager, MODE_TEMPERATURES
from download_utils import find_best_model
from file_parser import parse_file_with_wps
from gguf_meta import resolve_context_window
from version import APP_NAME, APP_VERSION, STUDIO

HELP_TEXT = f"""\
📖 EchoDesk {APP_VERSION} 完整指令介绍（Powered by {STUDIO}）

/help — 显示此指令介绍
  用法：/help
  作用：展示全部指令的用法与作用，即本帮助。

/mode <编号|缩写|全称> — 切换对话模式
  用法：/mode 2 或 /mode s 或 /mode summary；/mode list 查看全部
  作用：切换 AI 回答风格。模式有：
    general 通用助手 / summary 摘要专家 / translate 翻译专家 /
    code 编程专家 / contract 合同审查 / creative 创意写作

/file <路径> — 上传文件
  用法：/file C:\\文档\\合同.docx
  作用：把文件内容加入上下文，模型将基于文件回答。
  支持 Office（doc/docx/xls/xlsx/ppt/pptx）、TXT、PDF 等；
  加密 Office 文档在装有 WPS 的机器上可自动解密，否则回退通用解析。
  长文档（估算 token 超过上下文窗口的 50%）会自动先分段摘要再注入，
  而不是直接截断，摘要中保留原文指针便于回查。

/summarize — 手动触发全文摘要
  用法：/summarize
  作用：对当前已加载的文件强制做一次分段摘要（逐段摘要后合并），
       用摘要替代全文注入上下文，适合合同等超长文档；摘要进度在对话框中实时显示。

/search on|off — 联网搜索开关
  用法：/search on
  作用：开启后每次提问会先搜索网络参考信息再回答（需要联网）。

/model [名称|别名|list] — 查看或切换模型
  用法：/model             查看当前模型信息（架构/量化/上下文/GPU 等）
       /model list         列出 models/ 目录与配置档案中的模型
       /model 模型名.gguf   切换本地模型（支持前缀模糊匹配）
       /model 别名         切换 config.models 档案中的模型（自动套用其上下文/温度）
  本地缺失时从仓库匹配最接近的文件，并在你确认后才开始下载（可能耗时较长）。

/clearcache — 清空 KV 缓存
  用法：/clearcache
  作用：清空推理引擎的 KV 缓存与输入历史；模式/提示词/历史变化时系统会自动清理，
        手动清理可用于排查异常输出。

/prompt <文本>|reset — 自定义系统提示词
  用法：/prompt 你是我的法律顾问；/prompt reset 清除
  作用：覆盖当前模式的默认提示词，临时改变助手角色。

/temp <0.0~1.0>|reset — 调整生成温度
  用法：/temp 0.9；/temp reset 恢复模式预设
  作用：温度越高回答越发散，越低越保守。
  提示：切换模式时会自动套用该模式的温度预设（code 0.2 / translate 0.3 /
       contract 0.3 / summary 0.4 / general 0.7 / creative 0.9），
       手动设置后以手动值为准，/temp reset 恢复预设。

/stop — 停止当前生成
  用法：/stop（或前端 ⏹ 停止 按钮）
  作用：中断正在进行的流式回复。

/snippet <add|remove|list> — 常用语（前端管理）
  用法：/snippet list；/snippet add 名称 文本；/snippet remove 名称
  作用：管理对话输入框可一键插入的常用语模板（由前端保存在 snippets.json）。

/darkmode on|off — 切换主题
  用法：/darkmode on
  作用：切换深色/浅色界面主题（由前端即时渲染）。

/status — 查看当前状态
  用法：/status
  作用：显示模型、模式、温度、搜索状态、对话轮数与已加载文件。

/clear — 清空对话
  用法：/clear
  作用：清空历史记录与文件上下文，重新开始。

/undo — 撤回
  用法：/undo
  作用：撤回最近一轮问答（各一条）。

/save <文件名> — 导出对话
  用法：/save 会议记录
  作用：把当前对话导出为 Markdown 文件（自动补 .md 后缀）。

/about — 关于
  用法：/about
  作用：显示版本号与出品方信息。

/exit — 退出程序
  用法：/exit
  作用：显示感谢语后，前端将在 10 秒内自动关闭程序。
"""

# 返回：反馈文本 + 附加动作（dict，如 {"action": "exit"} / {"action": "confirm_model", ...}）
Response = Tuple[str, Optional[dict]]


class CommandHandler:
    """负责 '/' 指令的解析与分发。"""

    def __init__(
        self,
        conv_mgr: ConversationManager,
        search_engine,
        llm_engine,
        config: Config,
        summarizer=None,
        file_loader: Optional[Callable[[str], str]] = None,
        summarize_file: Optional[Callable[..., str]] = None,
    ) -> None:
        self.conv_mgr = conv_mgr
        self.search_engine = search_engine
        self.llm_engine = llm_engine
        self.config = config
        self.summarizer = summarizer
        self.file_loader = file_loader
        self.summarize_file = summarize_file
        self.search_enabled = config.search_default
        self.temperature = config.temperature
        self.temp_manual = False  # 用户手动 /temp 后，切换模式不再套用温度预设
        self.context_length = config.context_length

        self._handlers: Dict[str, Callable[[list], Response]] = {
            "/exit": lambda _: self._exit(),
            "/quit": lambda _: self._exit(),
            "/help": lambda _: (HELP_TEXT, None),
            "/mode": self._mode,
            "/file": self._file,
            "/summarize": self._summarize,
            "/search": self._search,
            "/model": self._model,
            "/clearcache": self._clearcache,
            "/prompt": self._prompt,
            "/darkmode": self._darkmode,
            "/stop": self._stop,
            "/snippet": self._snippet,
            "/status": self._status,
            "/about": self._about,
            "/clear": self._clear,
            "/undo": self._undo,
            "/save": self._save,
            "/temp": self._temp,
        }

    def handle(self, cmd_line: str) -> Response:
        """解析并执行指令，返回 (反馈文本, 附加动作)。"""
        parts = cmd_line.strip().split()
        if not parts:
            return "", None
        handler = self._handlers.get(parts[0].lower())
        if handler is None:
            return f"未知指令: {parts[0]}（输入 /help 查看全部指令）", None
        return handler(parts[1:])

    # ---------- 指令实现 ----------

    @staticmethod
    def _exit() -> Response:
        return "👋 退出程序，再见！", {"action": "exit"}

    def _mode(self, args: list) -> Response:
        if not args:
            return f"当前模式: {self.conv_mgr.get_mode()}（/mode list 查看全部）", None
        if args[0].lower() == "list":
            return f"可用模式（编号 / 缩写 / 全称均可切换）：\n{self.conv_mgr.mode_menu()}", None
        if self.conv_mgr.set_mode(args[0]):
            mode = self.conv_mgr.get_mode()
            self.config.default_mode = mode
            self.config.save()
            self.llm_engine.clear_cache()  # 系统提示词变化，旧 KV 缓存失效
            # 未手动设置过温度时，自动套用该模式的温度预设
            preset = MODE_TEMPERATURES.get(mode)
            if not self.temp_manual and preset is not None:
                self.temperature = preset
                return f"✅ 已切换模式为: {mode}（温度预设 {preset}）", None
            return f"✅ 已切换模式为: {mode}", None
        return f"❌ 未知模式: {args[0]}（/mode list 查看）", None

    def _file(self, args: list) -> Response:
        if not args:
            return "请提供文件路径，例如: /file C:\\dir\\文档.docx", None
        filepath = args[0]
        if not os.path.exists(filepath):
            return f"文件不存在: {filepath}", None
        # 统一走后端的加载入口：过长文档自动分段摘要后再注入
        if self.file_loader is not None:
            return self.file_loader(filepath), None
        content = parse_file_with_wps(filepath, self.config.file_max_chars)
        if content.startswith(("WPS读取失败", "解析失败", "文件内容为空", "不支持的文件类型")):
            return f"❌ 读取失败: {content}", None
        self.conv_mgr.set_file(filepath, content)
        return f"✅ 已加载文件: {os.path.basename(filepath)} ({len(content)} 字符)", None

    def _summarize(self, args: list) -> Response:
        """手动触发全文摘要（长文档注入前先分段摘要）。"""
        if self.summarize_file is None:
            return "❌ 当前不支持手动摘要", None
        if not self.conv_mgr.file_content:
            return "❌ 当前没有已加载的文件，请先用 /file 上传", None
        name = os.path.basename(self.conv_mgr.file_name or "")
        try:
            return self.summarize_file(True, name, "手动"), None
        except Exception as e:
            return f"❌ 摘要失败: {e}", None

    def _search(self, args: list) -> Response:
        if not args:
            return f"搜索状态: {'开启' if self.search_enabled else '关闭'}", None
        # toggle：供输入框下方的状态区一键切换（优化五），等价于 on/off 的取反
        if args[0] in ("on", "off", "toggle"):
            self.search_enabled = (not self.search_enabled) if args[0] == "toggle" else (args[0] == "on")
            self.config.search_default = self.search_enabled
            self.config.save()
            return f"🔍 搜索已{'开启' if self.search_enabled else '关闭'}", None
        return "用法: /search on/off", None

    def _model(self, args: list) -> Response:
        """查看/切换模型：支持档案别名、本地文件、前缀匹配与下载征询。"""
        if not args:
            return self._model_info(), None
        if args[0].lower() == "list":
            return self._model_list(), None

        name = args[0]

        # 1) 档案别名（config.models 注册表）
        profile = self.config.models.get(name)
        if profile:
            path = profile.get("path", name)
            resolved = path if os.path.exists(path) else os.path.join("models", path)
            if os.path.exists(resolved):
                return self._apply_model(resolved, path, profile)
            return f"❌ 档案 '{name}' 指向的文件不存在: {path}", None

        # 2) 本地严格匹配（绝对路径或 models/ 下）
        candidates = []
        if os.path.isabs(name) or "/" in name or "\\" in name:
            candidates.append(os.path.abspath(name))
        candidates.append(os.path.join("models", name))
        local = next((p for p in candidates if os.path.exists(p)), None)
        if local:
            return self._apply_model(local, name, None)

        # 3) 本地前缀/模糊匹配 models/ 中的 .gguf
        matches = [f for f in self._local_ggufs() if name.lower() in f.lower()]
        if len(matches) == 1:
            return self._apply_model(os.path.join("models", matches[0]), matches[0], None)
        if len(matches) > 1:
            return "❓ 本地有多个匹配：\n" + "\n".join(f"  {f}" for f in matches) + "\n请指定完整文件名", None

        # 4) 本地缺失 → 查询仓库（需要网络），征得同意后下载
        try:
            best, size = find_best_model(self.config.model_repo, name, hf_endpoint=self.config.hf_endpoint)
        except Exception as e:
            return f"❌ 本地没有模型 '{name}'，且无法查询仓库（{e}）。\n请把模型文件放入 models/ 目录后重试。", None

        size_mb = f"{size / 1024 / 1024:.0f} MB" if size else "未知大小"
        return (
            f"模型 '{name}' 未在本地找到。\n"
            f"仓库 {self.config.model_repo} 中最匹配的是:\n"
            f"  {best}（约 {size_mb}）\n"
            f"⚠️ 下载需要联网且可能耗时较长，是否确认下载？",
            {"action": "confirm_model", "model": best},
        )

    def _local_ggufs(self) -> list:
        if not os.path.isdir("models"):
            return []
        return sorted(f for f in os.listdir("models") if f.lower().endswith(".gguf"))

    def _model_info(self) -> str:
        info = self.llm_engine.model_info()
        train = self.llm_engine.n_ctx_train
        lines = [
            f"🧠 当前模型: {self.config.model_path}",
            f"🏗️  架构: {info.get('general.architecture', '未知')} · 量化: {info.get('general.file_type', '未知')}",
            f"🪟 上下文: {self.llm_engine.n_ctx} tokens"
            + (f"（模型训练窗口 {train}，上限 max_context={self.config.max_context}）" if train else
               f"（上限 max_context={self.config.max_context}）"),
            f"💾 KV Cache 估算: {self.llm_engine.kv_cache_text()}（fp16，随上下文长度线性增长）",
            f"🧵 线程: {self.llm_engine.n_threads} · batch {self.llm_engine.n_batch}",
            f"🎮 GPU 层: {self.llm_engine.n_gpu_layers}（0=纯 CPU）",
            f"📁 本地模型: {len(self._local_ggufs())} 个",
        ]
        if self.conv_mgr.file_content:
            summarized = "已分段摘要" if self.conv_mgr.file_summary else "按原文注入"
            lines.append(f"📄 已加载文件: {os.path.basename(self.conv_mgr.file_name)}（{summarized}）")
        if self.config.models:
            lines.append("🗂️  档案别名: " + ", ".join(self.config.models.keys()))
        lines.append("（/model list 查看本地模型；/model <别名|文件名|前缀> 切换；缺失时征询下载）")
        return "\n".join(lines)

    def _model_list(self) -> str:
        lines = [f"🧠 当前模型: {self.config.model_path}", "", "📁 models/ 目录:"]
        local = self._local_ggufs()
        if local:
            lines += [f"  {f}" for f in local]
        else:
            lines.append("  （空）")
        if self.config.models:
            lines.append("")
            lines.append("🗂️  档案（config.models）:")
            for alias, p in self.config.models.items():
                lines.append(f"  {alias} → {p.get('path', '?')}")
        lines.append("")
        lines.append("用法: /model <别名|文件名|前缀> 切换；缺失时从仓库匹配并征询下载")
        return "\n".join(lines)

    def _apply_model(self, path: str, display: str, profile: Optional[dict]) -> Response:
        """切换并重新加载模型；档案可携带独立 context_length / temperature。

        上下文窗口按新模型的训练窗口与 config.max_context 收敛，防止超限。
        """
        requested = int(profile["context_length"]) if profile and profile.get("context_length") else None
        train = resolve_context_window(path, 0)
        n_ctx = self.config.context_for_model(path, train, requested)
        try:
            self.llm_engine.reload(path, n_ctx)
        except Exception as e:
            return f"❌ 模型加载失败: {e}", None
        self.config.model_path = display if not os.path.isabs(display) else os.path.basename(display)
        extra = f"（上下文 {n_ctx}"
        if train and n_ctx < train:
            extra += f"，模型训练窗口 {train}"
        extra += f"，KV Cache 约 {self.llm_engine.kv_cache_text()}）"
        if profile and profile.get("temperature") is not None and not self.temp_manual:
            self.temperature = float(profile["temperature"])
            extra += f"（温度预设 {self.temperature}）"
        self.config.save()
        return f"✅ 已切换模型: {self.config.model_path}{extra}", None

    def _clearcache(self, _args: list) -> Response:
        ok = self.llm_engine.clear_cache()
        return ("✅ 已清空 KV 缓存" if ok else "⚠️ 清空缓存失败（引擎不支持）"), None

    def _prompt(self, args: list) -> Response:
        if not args:
            return f"当前自定义提示词:\n{self.conv_mgr.custom_prompt or '（未设置，使用模式默认提示词）'}", None
        if args[0].lower() == "reset":
            self.conv_mgr.set_custom_prompt("")
            self.llm_engine.clear_cache()
            return "🗑️ 已清除自定义提示词", None
        text = " ".join(args)
        self.conv_mgr.set_custom_prompt(text)
        self.llm_engine.clear_cache()
        return f"✅ 已设置自定义提示词（{len(text)} 字）", None

    @staticmethod
    def _darkmode(args: list) -> Response:
        if not args:
            return "用法: /darkmode on/off（主题由前端渲染）", None
        mode = "深色" if args[0].lower() == "on" else "浅色" if args[0].lower() == "off" else None
        if mode is None:
            return "用法: /darkmode on/off", None
        return f"🎨 已请求切换为{mode}模式", None

    @staticmethod
    def _stop(_args: list) -> Response:
        return "⏹ 已收到停止指令（流式生成由协议 action=stop 中断，前端 ⏹ 按钮等效）", None

    @staticmethod
    def _snippet(_args: list) -> Response:
        return "常用语由前端管理：请在应用内使用 /snippet add 名称 文本 / remove 名称 / list", None

    def _status(self, _args: list) -> Response:
        # 估算当前上下文占用（系统提示 + 历史）
        try:
            msgs = self.conv_mgr.build_messages("")
            used = sum(self.llm_engine.count_tokens(m["content"]) for m in msgs)
            limit = int(self.llm_engine.n_ctx * 0.8)
            ctx_line = f"📏 上下文: 约 {used}/{limit} tokens（{self.llm_engine.n_ctx} 窗口的 80% 自动摘要线）"
        except Exception:
            ctx_line = "📏 上下文: 未知"
        lines = [
            f"🏷️  {APP_NAME} {APP_VERSION} — Powered by {STUDIO}",
            f"🧠 模型: {self.config.model_path}",
            f"🏗️  架构: {self.llm_engine.model_info().get('general.architecture', '未知')}",
            f"🛠️  模式: {self.conv_mgr.get_mode()}",
            f"🌡️  温度: {self.temperature}{'（手动）' if self.temp_manual else '（模式预设）'}",
            f"⚙️  线程: {getattr(self.llm_engine, 'n_threads', '?')} · batch {getattr(self.llm_engine, 'n_batch', 512)} · GPU 层 {getattr(self.llm_engine, 'n_gpu_layers', 0)}",
            f"💾 KV Cache: 约 {self.llm_engine.kv_cache_text()}",
            ctx_line,
            f"🔍 联网搜索: {'开启' if self.search_enabled else '关闭'}",
            f"💬 对话轮数: {len(self.conv_mgr.history) // 2}",
            f"📝 早期摘要: {'有' if self.conv_mgr.summary else '无'}",
            f"📄 文件: {os.path.basename(self.conv_mgr.file_name) if self.conv_mgr.file_name else '无'}"
            + (f"（已分段摘要 {self.conv_mgr.file_summary_meta.get('chunks', '?')} 段）" if self.conv_mgr.file_summary else ""),
        ]
        return "\n".join(lines), None

    @staticmethod
    def _about(_args: list) -> Response:
        return (
            f"✨ {APP_NAME} {APP_VERSION}\n"
            f"Powered by {STUDIO}\n"
            f"本地 AI 桌面助手：Python 后端 + C# WPF 前端，全离线推理。",
            None,
        )

    def _clear(self, _args: list) -> Response:
        self.conv_mgr.clear()
        self.llm_engine.clear_cache()
        return "🗑️ 对话已清空", None

    def _undo(self, _args: list) -> Response:
        if self.conv_mgr.undo():
            self.llm_engine.clear_cache()
            return "↩️ 已撤回", None
        return "没有可撤回的消息", None

    def _save(self, args: list) -> Response:
        if not args:
            return "请提供文件名，例如: /save 对话记录", None
        filename = args[0] if args[0].endswith(".md") else args[0] + ".md"
        try:
            Path(filename).write_text(self.conv_mgr.export_markdown(), encoding="utf-8")
            return f"✅ 已保存到 {filename}", None
        except OSError as e:
            return f"❌ 保存失败: {e}", None

    def _temp(self, args: list) -> Response:
        if not args:
            return f"当前温度: {self.temperature}{'（手动）' if self.temp_manual else '（模式预设）'}\n（/temp reset 恢复为模式预设）", None
        if args[0].lower() == "reset":
            self.temp_manual = False
            preset = MODE_TEMPERATURES.get(self.conv_mgr.get_mode(), self.config.temperature)
            self.temperature = preset
            self.config.temperature = preset
            self.config.save()
            return f"✅ 温度已恢复为模式预设: {preset}", None
        try:
            value = float(args[0])
        except ValueError:
            return "请输入有效的数字", None
        if not 0.0 <= value <= 1.0:
            return "温度值应在 0.0~1.0 之间", None
        self.temp_manual = True
        self.temperature = value
        self.config.temperature = value
        self.config.save()
        return f"✅ 温度已设为: {value}（手动）", None
