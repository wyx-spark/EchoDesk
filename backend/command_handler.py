"""
[EN]
Command parsing: handles user commands that start with '/'.

v0.2.1 (task specification 3.4 / 3.7): all user-facing text is resolved from the
language pack (``backend/lang/<locale>.json``) with the fallback chain
"current locale -> en -> zh-Hans -> key".

v0.2.1 (task specification 2.2): the command table supports runtime registration
of external commands (skills and local plug-ins) with behaviour identical to
built-in commands. ``COMMAND_SPEC`` is also the data source for /help and the
front-end command palette, so skill commands appear automatically in the input
completion list.

[ZH]
指令解析：处理以 '/' 开头的用户指令。

v0.2.1（任务书 任务三 3.4 / 3.7）：所有面向用户的文案改为从语言包取用
（``backend/lang/<locale>.json``），并按"当前语言 → en → zh-Hans → 键名"回退。

v0.2.1（任务书 任务二 2.2）：指令表支持运行期注册外部指令（技能/本地插件），
行为与内置指令完全一致；``COMMAND_SPEC`` 同时是 /help 与前端指令面板的数据源，
因此技能指令会自动出现在输入框的候选列表里。
"""
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import Config
from conversation import ConversationManager, MODE_TEMPERATURES
from download_utils import find_best_model
import hardware_profiler
from file_parser import is_parse_error, localize_error, parse_file_with_wps
from gguf_meta import resolve_context_window
from i18n import get as _i18n
from i18n import t as _t
from version import APP_NAME, APP_VERSION

# [EN] Return value: feedback text plus an optional action dict, e.g.
# [EN] {"action": "exit"} or {"action": "confirm_model", ...}.
# [ZH] 返回：反馈文本 + 附加动作（dict，如 {"action": "exit"} /
# [ZH] {"action": "confirm_model", ...}）。
Response = Tuple[str, Optional[dict]]


def t(key: str, **kwargs: Any) -> str:
    """
    [EN]
    Shorthand for resolving a localized string within this module.

    [ZH]
    本模块内取文案的快捷方式。
    """
    return _t(key, **kwargs)


# ---------------------------------------------------------------------------
# [EN] Command metadata (order defines display order in /help and the front-end
# [EN] command palette)
# [ZH] 指令元数据（顺序 = /help 与前端指令面板的展示顺序）
# ---------------------------------------------------------------------------
# [EN] Field description:
# [EN]   name        command name (also determines the language-pack key
# [EN]               cmd.items.<name>)
# [EN]   category    group used by the front end for sorting and grouping
# [EN]   destructive has side effects and is irreversible; the front end asks for
# [EN]               confirmation before executing (v0.2.0 fix 2)
# [EN]   hidden      not listed in /help or the command palette (for aliases)
# [ZH] 字段说明：
# [ZH]   name        指令名（同时决定语言包键 cmd.items.<name>）
# [ZH]   category    分组，前端据此排序/分组
# [ZH]   destructive 有副作用且不可逆，前端执行前二次确认（v0.2.0 兼容性修正）
# [ZH]   hidden      不在 /help 与指令面板中展示（如别名）
COMMAND_SPEC: List[Dict[str, Any]] = [
    {"name": "/help", "category": "core"},
    {"name": "/status", "category": "core"},
    {"name": "/about", "category": "core"},
    {"name": "/mode", "category": "chat"},
    {"name": "/task", "category": "chat"},
    {"name": "/agent", "category": "chat"},
    {"name": "/mcp", "category": "chat"},
    {"name": "/preset", "category": "chat"},
    {"name": "/compact", "category": "chat"},
    {"name": "/search", "category": "chat"},
    {"name": "/prompt", "category": "chat"},
    {"name": "/temp", "category": "chat"},
    {"name": "/stop", "category": "chat"},
    {"name": "/file", "category": "file"},
    {"name": "/summarize", "category": "file"},
    {"name": "/save", "category": "file"},
    {"name": "/model", "category": "model"},
    {"name": "/clearcache", "category": "model", "destructive": True},
    {"name": "/clear", "category": "core", "destructive": True},
    {"name": "/undo", "category": "core"},
    {"name": "/darkmode", "category": "ui"},
    {"name": "/snippet", "category": "ui"},
    {"name": "/exit", "category": "core"},
]

#: [EN] Alias mapped to the primary command name (not displayed separately).
#: [ZH] 别名映射到主指令名（不单独展示）。
COMMAND_ALIASES: Dict[str, str] = {"/quit": "/exit"}


def describe_commands(extra: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    [EN]
    Return the localized command metadata list (data source for the front-end
    command palette and help).

    Built-in commands come first, skill commands afterwards; each item contains
    name / usage / desc / detail / category / source / destructive and can be
    serialized to the front end directly.

    [ZH]
    返回本地化后的指令元数据列表（前端指令面板与帮助的数据源）。

    内置指令在前、技能指令在后；每项包含 name / usage / desc / detail /
    category / source / destructive，可直接序列化给前端。
    """
    items: List[Dict[str, Any]] = []
    for spec in COMMAND_SPEC:
        name = spec["name"]
        key = f"cmd.items.{name}"
        items.append({
            "name": name,
            "label": t(f"{key}.name"),
            "usage": t(f"{key}.usage"),
            "desc": t(f"{key}.desc"),
            "detail": t(f"{key}.detail") if _i18n().has(f"{key}.detail") else "",
            "category": spec.get("category", "core"),
            "source": "builtin",
            "destructive": bool(spec.get("destructive")),
        })
    for item in extra or []:
        items.append(item)
    return items


def help_text(extra: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    [EN]
    Build the complete /help text, including skill commands (task specification
    2.6).

    [ZH]
    构建 /help 的完整文案（含技能指令，任务书任务二 2.6）。
    """
    # [EN] The studio name is resolved from the language pack (task specification
    # [EN] "studio name finalized"): the Chinese UI shows 熵寂工作室 and the English UI
    # [EN] shows "Void Entropia Studio". Do NOT use version.STUDIO here directly:
    # [EN] that is the English constant, so the Chinese UI would display the English
    # [EN] name. The caller supplies the value of the `{studio}` placeholder.
    # [ZH] 出品方名称走语言包（设计规范「工作室名称敲定」）：中文界面显示「熵寂工作室」、
    # [ZH] 英文界面显示 "Void Entropia Studio"。不要在这里直接使用 version.STUDIO ——
    # [ZH] 那是英文常量，中文界面会跟着显示英文名（`{studio}` 占位符的值由调用方提供）。
    lines: List[str] = [t("cmd.help.header", version=APP_VERSION, studio=t("app.studio")), ""]
    for item in describe_commands(extra):
        lines.append(f"{item['usage']} — {item.get('label') or item['name']}")
        lines.append(f"  {t('cmd.help.usage_label')}{item['usage']}")
        desc = item.get("desc") or ""
        if desc:
            first, _, rest = desc.partition("\n")
            lines.append(f"  {t('cmd.help.desc_label')}{first}")
            if rest:
                lines.append(rest)
        detail = item.get("detail") or ""
        if detail:
            lines.append(detail)
        lines.append("")

    skills = [i for i in (extra or []) if i.get("source") != "builtin"]
    if skills:
        lines.append(t("cmd.help.skill_section"))
        for item in skills:
            lines.append(f"  {item['usage']} — {item['name']}（{item.get('skill_name') or item.get('skill_id', '')}）")
        lines.append("")
        lines.append(t("cmd.help.skill_footer"))
        lines.append("")

    lines.append(t("cmd.help.footer"))
    return "\n".join(lines)



class CommandHandler:
    """
    [EN]
    Parses and dispatches '/' commands.

    [ZH]
    负责 '/' 指令的解析与分发。
    """

    def __init__(
        self,
        conv_mgr: ConversationManager,
        search_engine,
        llm_engine,
        config: Config,
        summarizer=None,
        file_loader: Optional[Callable[[str], str]] = None,
        summarize_file: Optional[Callable[..., str]] = None,
        files_loader: Optional[Callable[..., dict]] = None,
    ) -> None:
        self.conv_mgr = conv_mgr
        self.search_engine = search_engine
        self.llm_engine = llm_engine
        self.config = config
        self.summarizer = summarizer
        self.file_loader = file_loader
        self.summarize_file = summarize_file
        # [EN] Multi-file entry point (task 8): accepts a list of paths and returns an
        # [EN] aggregate dict containing text/files/names. The single-file entry point
        # [EN] (file_loader) is unchanged; `/file` with one path still uses it so the
        # [EN] acknowledgement text stays byte-identical.
        # [ZH] 多文件入口（相关设计）：接收路径列表，返回含 text/files/names 的汇总字典。
        # [ZH] 单文件入口（file_loader）保持不变，`/file` 单路径时仍走它 —— 回执逐字一致。
        self.files_loader = files_loader
        self.search_enabled = config.search_default
        self.temperature = config.temperature
        self.temp_manual = False  # [EN] After an explicit /temp, mode switches no longer apply temperature presets.
                                  # [ZH] 用户手动 /temp 后，切换模式不再套用温度预设。
        self.context_length = config.context_length
        # [EN] Externally registered commands from skills and local plug-ins:
        # [EN] {"/hello": {...}} (task 2, item 2.2).
        # [ZH] 技能/本地插件注册的外部指令：{"/hello": {...}}（相关设计 2.2）。
        self._external: Dict[str, Dict[str, Any]] = {}

        self._handlers: Dict[str, Callable[[list], Response]] = {
            "/exit": lambda _: self._exit(),
            "/quit": lambda _: self._exit(),
            "/help": self._help,
            "/mode": self._mode,
            "/task": self._task,
            "/agent": self._agent,
            "/mcp": self._mcp,
            "/preset": self._preset,
            "/compact": self._compact,
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
        # [EN] Injected by main: returns the file extensions already handled by
        # [EN] skills (shown by /model and /status).
        # [ZH] 由 main 注入：返回技能已接管的文件扩展名（/model、/status 里展示）。
        self.skill_parsers_provider: Optional[Callable[[], Dict[str, str]]] = None
        # [EN] Injected by main: manual context compaction (fix 4, baseline
        # [EN] requirement). Compaction requires the model (early conversation is
        # [EN] summarized), and both LLM invocation and history management live in
        # [EN] main; only a callback is kept here to avoid duplicating main's logic.
        # [ZH] 由 main 注入：手动压缩上下文（兼容性修正 基础要求）。
        # [ZH] 压缩需要模型参与（把早期对话总结成摘要），而 LLM 的调用与历史管理都在
        # [ZH] main 里，因此这里只留一个回调，避免把 main 的逻辑复制一份进来。
        self.compact_provider: Optional[Callable[[bool], Dict[str, Any]]] = None
        # [EN] Injected by main: model capability profile (fix 4, directions 6/7;
        # [EN] used by both /status and /preset).
        # [ZH] 由 main 注入：模型能力画像（兼容性修正 方向六/七，/status 与 /preset 都要用）。
        self.profile_provider: Optional[Callable[[], Dict[str, Any]]] = None
        # [EN] Injected by main: clears the segmented-summary cache and returns the
        # [EN] number of entries removed (used by /clearcache).
        # [ZH] 由 main 注入：清空分段摘要缓存并返回清掉的条目数（/clearcache 用）。
        self.cache_clear_provider: Optional[Callable[[], int]] = None
        # [EN] Injected by main: reads Agent state (no argument) or adjusts it via
        # [EN] ``writer(tool_name, allowed)``; when tool_name is ``"__steps__"`` the
        # [EN] second argument is the new step limit. The Agent registry and its
        # [EN] configuration live in main, so only a callback is kept here to avoid
        # [EN] maintaining the tool list in two places.
        # [ZH] 由 main 注入：读取 Agent 状态（无参）或调整它（``writer(工具名, 是否允许)``；
        # [ZH] 工具名传 ``"__steps__"`` 时第二参是新的步数上限）。Agent 的注册表与配置
        # [ZH] 都在 main 手里，这里只留回调，避免在两边各维护一份工具清单。
        self.agent_configurator: Optional[Callable[..., Dict[str, Any]]] = None
        # [EN] Injected by main: MCP control callback. No argument = read state;
        # [EN] ("enable"|"disable", name) = toggle; ("connect",) = launch the process
        # [EN] and fetch its tool list. A single entry point again avoids maintaining
        # [EN] two parallel implementations.
        # [ZH] 由 main 注入：MCP 控制回调。无参 = 读状态；("enable"|"disable", 名称)
        # [ZH] = 启停；("connect",) = 拉起进程并拉取工具清单。同样只保留一个入口，
        # [ZH] 避免两边各维护一套实现。
        self.mcp_control: Optional[Callable[..., Dict[str, Any]]] = None
        # [EN] Currently active task template id (fix 4, direction 1); empty means
        # [EN] none is active.
        # [ZH] 当前生效的任务模板 id（兼容性修正 方向一）；空串表示未启用。
        self.task_template = ""

    # ---------- [EN] Runtime command registration (task 2, items 2.2 / 2.4) ----------
    # ---------- [ZH] 运行期指令注册（相关设计 2.2 / 2.4） ----------

    def register_command(
        self,
        name: str,
        handler: Callable[[list], Any],
        *,
        skill_id: str = "",
        skill_name: str = "",
        label: str = "",
        usage: str = "",
        description: str = "",
        destructive: bool = False,
    ) -> bool:
        """
        [EN]
        Register an external command (skill or local plug-in).

        * Command names are lowercased and must start with ``/``;
        * Built-in commands cannot be overridden (returns False), which keeps the
          main program's behaviour predictable;
        * Exceptions raised by the handler are caught centrally in ``handle`` and
          cannot affect the main program (item 2.4, exception isolation).

        [ZH]
        注册一个外部指令（技能/本地插件）。

        * 指令名统一小写、必须以 ``/`` 开头；
        * 不能覆盖内置指令（返回 False），保证主程序行为可预测；
        * 处理函数的异常由 ``handle`` 统一捕获，不会影响主程序（2.4 异常隔离）。
        """
        key = str(name or "").strip().lower()
        if not key:
            return False
        if not key.startswith("/"):
            key = "/" + key
        if key in self._handlers:
            return False
        self._external[key] = {
            "handler": handler,
            "skill_id": skill_id,
            "skill_name": skill_name or skill_id,
            "label": label,
            "usage": usage or key,
            "description": description,
            "destructive": destructive,
        }
        return True

    def unregister_command(self, name: str) -> bool:
        key = str(name or "").strip().lower()
        if key and not key.startswith("/"):
            key = "/" + key
        return self._external.pop(key, None) is not None

    def unregister_skill_commands(self, skill_id: str) -> int:
        """
        [EN]
        Unregister all commands registered by one skill and return the number
        removed (item 2.4, unload and hot reload).

        [ZH]
        卸载某个技能注册的全部指令，返回移除数量（2.4 卸载热重载）。
        """
        doomed = [k for k, v in self._external.items() if v.get("skill_id") == skill_id]
        for key in doomed:
            self._external.pop(key, None)
        return len(doomed)

    def external_commands(self) -> List[Dict[str, Any]]:
        """
        [EN]
        Metadata for skill commands (used by /help and the front-end command
        palette).

        [ZH]
        技能指令的元数据（供 /help 与前端指令面板）。
        """
        items: List[Dict[str, Any]] = []
        for key, meta in self._external.items():
            items.append({
                "name": key,
                "label": meta.get("label") or key,
                "usage": meta.get("usage") or key,
                "desc": meta.get("description") or "",
                "detail": "",
                "category": "skill",
                "source": "skill",
                "skill_id": meta.get("skill_id", ""),
                "skill_name": meta.get("skill_name", ""),
                "destructive": bool(meta.get("destructive")),
            })
        return items

    def all_commands(self) -> List[Dict[str, Any]]:
        """
        [EN]
        Complete metadata for built-in plus skill commands (used by the front-end
        command palette).

        Skill commands already enter ``_external`` in ``register_command``, so the
        loader does not need to be queried again here; this avoids the risk of two
        data sources diverging.

        [ZH]
        内置 + 技能指令的完整元数据（前端指令面板用）。

        技能指令在 ``register_command`` 时就进了 ``_external``，
        因此这里不需要再去问加载器，避免了双重数据源不一致的风险。
        """
        return describe_commands(self.external_commands())

    #: [EN] Argument tokenizer: quoted arguments are supported (此前版本 known-issue
    #: [EN] optimization, task 8).
    #:
    #: [EN] Why quoting is required: a path containing spaces such as
    #: [EN] `/file C:\My Documents\contract 2026.docx` is split into four tokens by
    #: [EN] `str.split()`. In the single-file era `_file` used only args[0], so
    #: [EN] "any path with spaces failed to load"; the multi-file era is worse,
    #: [EN] because four tokens become four paths and the result is "0 files loaded,
    #: [EN] 4 failed". This is a pre-existing defect exposed when multi-file path
    #: [EN] parsing was introduced (it went unreported because the error message
    #: [EN] pointed at a missing file and users assumed a typo on their side).
    #: [EN] Usage: `/file "C:\My Documents\contract 2026.docx"`.
    #: [ZH] 指令参数的分词：支持引号包裹（此前版本 优化已知问题-2 相关设计）。
    #:
    #: [ZH] 为什么必须支持：`/file C:\My Documents\合同 2026.docx` 这类带空格的路径，
    #: [ZH] 用 `str.split()` 会被切成 4 段 —— 单文件时代 `_file` 只取 args[0]，
    #: [ZH] 于是"路径含空格就加载失败"，而多文件时代更糟：4 段会被当成 4 个路径，
    #: [ZH] 报出"加载了 0 个文件、失败 4 个"。这是打开多文件路径解析时暴露出来的
    #: [ZH] 既有缺陷（此前无人报告，是因为报错信息指向"文件不存在"，用户以为是自己写错了）。
    #: [ZH] 用法：`/file "C:\My Documents\合同 2026.docx"`。
    _ARG_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|(\S+)')

    @classmethod
    def split_args(cls, text: str) -> list:
        """
        [EN]
        Split arguments according to the quoting rules (both single and double
        quotes are recognized; spaces inside quotes are preserved).

        [ZH]
        按引号规则切分参数（双引号与单引号都识别，引号内保留空格）。
        """
        args = []
        for match in cls._ARG_RE.finditer(text or ""):
            value = match.group(1)
            if value is None:
                value = match.group(2)
            if value is None:
                value = match.group(3)
            if value:
                args.append(value)
        return args

    def handle(self, cmd_line: str) -> Response:
        """
        [EN]
        Parse and execute a command, returning (feedback text, optional action).

        [ZH]
        解析并执行指令，返回 (反馈文本, 附加动作)。
        """
        stripped = cmd_line.strip()
        if not stripped:
            return "", None
        # [EN] Everything after the command name is handled by the quote-aware
        # [EN] tokenizer (see the note on _ARG_RE).
        # [ZH] 指令名之后的部分交给引号感知的分词（见 _ARG_RE 的说明）。
        head, _, rest = stripped.partition(" ")
        name = head.lower()
        args = self.split_args(rest)
        handler = self._handlers.get(name)
        if handler is not None:
            return handler(args)

        # [EN] Skill and local plug-in commands: exceptions must be isolated
        # [EN] (task 2, item 2.4).
        # [ZH] 技能/本地插件指令：异常必须被隔离（相关设计 2.4）。
        external = self._external.get(name)
        if external is not None:
            try:
                result = external["handler"](args)
            except BaseException as e:  # noqa: BLE001
                # [EN] Catch BaseException rather than Exception (audit conclusion of
                # [EN] capability extension 1, task 4): a single `sys.exit()` or
                # [EN] `raise SystemExit` in a plug-in would bypass Exception and take
                # [EN] the backend process down, so the user would see "the command
                # [EN] made the program disappear". The isolation layer must be wider
                # [EN] than that, which is also the established practice for the other
                # [EN] plug-in entry points in this file (hooks, panels).
                # [EN] KeyboardInterrupt is intercepted as well: the backend is a
                # [EN] headless process, so Ctrl+C is handled by the main loop.
                # [ZH] 捕获 BaseException 而不是 Exception（后续版本 相关设计的审计结论）：
                # [ZH] 插件里一句 `sys.exit()` 或 `raise SystemExit` 会直接穿透 Exception，
                # [ZH] 把后端进程带走 —— 用户看到的是"点了指令程序就没了"。
                # [ZH] 隔离层必须比它更宽，这也是本文件其它插件入口（钩子、面板）的既有做法。
                # [ZH] KeyboardInterrupt 同样被拦下：后端是无界面进程，Ctrl+C 由主循环处理。
                import traceback

                traceback.print_exc()
                skill = external.get("skill_name") or external.get("skill_id") or "?"
                return t("cmd.disabled", skill=skill, name=name,
                         reason=f"{type(e).__name__}: {e}"), None
            return self._coerce_external(name, result)

        return t("cmd.unknown", name=head), None

    @staticmethod
    def _coerce_external(name: str, result: Any) -> Response:
        """
        [EN]
        Normalize the return value of a skill handler to (text, action).

        A skill may return:
          * a string -> text feedback;
          * ``(text, action dict)`` -> identical to built-in commands;
          * a dict containing a ``text`` key -> the text, with all other keys used
            as the action.

        [ZH]
        把技能处理函数的返回值统一成 (文本, 动作)。

        技能可以返回：
          * 字符串 → 文本反馈；
          * ``(文本, 动作dict)`` → 与内置指令一致；
          * 含 ``text`` 键的 dict → 文本 + 其它键作为动作。
        """
        if isinstance(result, tuple) and len(result) == 2:
            text, action = result
            return ("" if text is None else str(text)), (action if isinstance(action, dict) else None)
        if isinstance(result, dict):
            text = result.get("text") or result.get("content") or ""
            action = {k: v for k, v in result.items() if k not in ("text", "content")}
            return (str(text), action or None)
        if result is None:
            return "", None
        return str(result), None

    # ---------- [EN] Command implementations ----------
    # ---------- [ZH] 指令实现 ----------

    def _help(self, _args: list) -> Response:
        # [EN] Skill commands must also appear in /help (task specification 2.6).
        # [ZH] 技能指令也要出现在 /help 里（设计规范 2.6）。
        return help_text(self.external_commands()), None

    @staticmethod
    def _exit() -> Response:
        return t("cmd.resp.exit"), {"action": "exit"}

    def _mode(self, args: list) -> Response:
        """
        [EN]
        ``/mode``: switch the conversation mode.

        After the mode simplification (capability extension 1, task 1) only three
        modes remain, but the previous mode names and numbers are still accepted:
        ``/mode summary`` switches to chat and automatically applies the task
        template of the same name, while the temperature follows the legacy preset.
        Users therefore do not need to change their habits, and the requirement
        "capabilities are not lost through the merge" is satisfied here.

        [ZH]
        ``/mode``：切换对话模式。

        模式简化（能力扩展-1 任务一）之后只有 3 个模式，但上一版的模式名与编号
        仍然可用：``/mode summary`` 会切成 chat 并自动套用同名的任务模板，
        温度也沿用旧预设 —— 用户不用改习惯，"能力不因合并而丢失"这条要求就落在这里。
        """
        from conversation import LEGACY_TEMPERATURES

        if not args:
            return t("cmd.resp.mode_current", mode=self.conv_mgr.get_mode()), None
        if args[0].lower() == "list":
            return t("cmd.resp.mode_list", menu=self.conv_mgr.mode_menu(),
                     legacy=self.conv_mgr.legacy_mode_note()), None
        requested = args[0].strip().lower()
        # [EN] Legacy numbers (4-7, plus 2/3 whose semantics changed) are NOT
        # [EN] migrated automatically: the same digits mean different things in the
        # [EN] two numbering schemes, and migration would yield combinations that
        # [EN] match neither the old nor the new semantics (the documentation
        # [EN] subagent observed `/mode 2` becoming "contract review mode + summary
        # [EN] template"). An explicit error is returned instead, together with what
        # [EN] the user should type; the new numbers 1/2/3 switch modes as before
        # [EN] (MODE_NUMBERS is the single source of truth).
        # [ZH] 旧编号（4~7，以及语义变了的 2/3）不再自动迁移：数字在两套编号里指不同东西，
        # [ZH] 迁移会产生"既不是旧语义也不是新语义"的组合（文档审查验证到 `/mode 2`
        # [ZH] 变成"合同审查模式 + 摘要模板"）。这里直接报错并告诉用户该写什么；
        # [ZH] 新编号 1/2/3 照常切模式（判据只有 MODE_NUMBERS 一处）。
        from conversation import MODE_NUMBERS

        hint = self.conv_mgr.legacy_number_hint(requested)
        if hint and requested not in MODE_NUMBERS:
            return t("cmd.resp.mode_legacy_number", number=requested, instead=hint), None
        if not self.conv_mgr.set_mode(requested):
            return t("cmd.resp.mode_unknown", name=args[0]), None
        mode = self.conv_mgr.get_mode()
        self.config.default_mode = mode
        self.config.save()
        self.llm_engine.clear_cache()  # [EN] The system prompt changed, so the old KV cache is stale.
                                       # [ZH] 系统提示词变化，旧 KV 缓存失效。

        # [EN] Legacy mode name -> automatically apply the corresponding task
        # [EN] template (summary/translate/code/creative/guide).
        # [ZH] 旧模式名 → 自动套上对应任务模板（summary/translate/code/creative/guide）。
        import prompts as prompt_lib

        migrated = self.conv_mgr.migrated_template(requested)
        if migrated:
            text = prompt_lib.build_system_prompt(migrated)
            if text:
                self.conv_mgr.set_task_prompt(text, migrated)
                self.task_template = str((prompt_lib.get_template(migrated) or {}).get("label") or migrated)

        # [EN] When the temperature has not been set manually, apply the preset
        # [EN] automatically, preferring the legacy preset of the legacy mode name.
        # [ZH] 未手动设置过温度时，自动套用温度预设：优先沿用旧模式名的旧预设
        preset = LEGACY_TEMPERATURES.get(requested) if migrated else None
        if preset is None:
            preset = MODE_TEMPERATURES.get(mode)
        if migrated and not self.temp_manual and preset is not None:
            self.temperature = preset
            return (t("cmd.resp.mode_set_preset", mode=mode, preset=preset)
                    + "\n" + t("cmd.resp.mode_migrated", old=requested, template=migrated)), None
        if migrated:
            return (t("cmd.resp.mode_set", mode=mode)
                    + "\n" + t("cmd.resp.mode_migrated", old=requested, template=migrated)), None
        if not self.temp_manual and preset is not None:
            self.temperature = preset
            return t("cmd.resp.mode_set_preset", mode=mode, preset=preset), None
        return t("cmd.resp.mode_set", mode=mode), None

    def _file(self, args: list) -> Response:
        """
        [EN]
        ``/file <path> [more paths...]``: load one or more files (multi-file support:
        task 8).

        Multiple files go through ``files_loader`` (the backend batch entry point):
        each file is processed independently, a single failure does not affect the
        others, and an "N files loaded" summary is returned; a single file keeps the
        legacy semantics (the acknowledgement matches older versions).

        [ZH]
        ``/file <路径> [更多路径…]``：加载一份或多份文件（多文件：任务八）。

        多文件时走 ``files_loader``（后端的批量入口）：逐份独立处理、单份失败不影响
        其他，并给出"已加载 N 个文件"的汇总；单文件时保持旧语义（回执与老版本一致）。
        """
        if not args:
            return t("cmd.resp.file_need_path"), None
        paths = [str(a) for a in args if str(a).strip()]
        existing = [p for p in paths if os.path.exists(p)]
        missing = [p for p in paths if not os.path.exists(p)]
        if not existing:
            return t("cmd.resp.file_missing", path=missing[0] if missing else ""), None
        if len(existing) == 1 and not missing:
            filepath = existing[0]
            # [EN] Always go through the backend load entry point: over-long
            # [EN] documents are segmented and summarized before injection.
            # [ZH] 统一走后端的加载入口：过长文档自动分段摘要后再注入
            if self.file_loader is not None:
                return self.file_loader(filepath), None
            content = parse_file_with_wps(filepath, self.config.file_max_chars)
            if is_parse_error(content):
                return t("cmd.resp.file_load_failed", content=localize_error(content)), None
            self.conv_mgr.set_file(filepath, content)
            return t("cmd.resp.file_loaded", name=os.path.basename(filepath),
                     chars=len(content)), None
        # [EN] Multiple files: delegate to the backend batch entry point (getattr
        # [EN] keeps compatibility with substitutes and test doubles built via
        # [EN] __new__).
        # [ZH] 多文件：交给后端的批量入口（用 getattr 兼容 __new__ 构造的替身/测试对象）
        files_loader = getattr(self, "files_loader", None)
        if files_loader is not None:
            result = files_loader(existing)
            text = str((result or {}).get("text", ""))
            if missing:
                text += "\n" + t("cmd.resp.file_missing", path=missing[0])
            return text, None
        # [EN] No batch entry point (third-party or test double): fall back to
        # [EN] calling the single-file entry point once per file.
        # [ZH] 没有批量入口（第三方/测试替身）：退化成逐份调用单文件入口
        replies = []
        for path in existing:
            if self.file_loader is not None:
                replies.append(str(self.file_loader(path)))
                continue
            content = parse_file_with_wps(path, self.config.file_max_chars)
            if is_parse_error(content):
                replies.append(t("cmd.resp.file_load_failed",
                                 content=localize_error(content)))
                continue
            self.conv_mgr.set_file(path, content)
            replies.append(t("cmd.resp.file_loaded", name=os.path.basename(path),
                             chars=len(content)))
        if missing:
            replies.append(t("cmd.resp.file_missing", path=missing[0]))
        return "\n".join(replies), None

    def _summarize(self, args: list) -> Response:
        """
        [EN]
        Manually trigger a full-text summary (long documents are segmented and
        summarized before injection).

        [ZH]
        手动触发全文摘要（长文档注入前先分段摘要）。
        """
        if self.summarize_file is None:
            return t("cmd.resp.summarize_unsupported"), None
        if not self.conv_mgr.file_content:
            return t("cmd.resp.summarize_no_file"), None
        name = os.path.basename(self.conv_mgr.file_name or "")
        try:
            return self.summarize_file(True, name, t("cmd.resp.summarize_trigger")), None
        except Exception as e:
            return t("cmd.resp.summarize_failed", error=e), None

    def _search(self, args: list) -> Response:
        if not args:
            state = t("cmd.status.search_on") if self.search_enabled else t("cmd.status.search_off")
            return t("cmd.resp.search_status", state=state), None
        # [EN] toggle: allows the status area below the input field to flip the state
        # [EN] with one click (optimization 5); equivalent to inverting on/off.
        # [ZH] toggle：供输入框下方的状态区一键切换（优化五），等价于 on/off 的取反
        if args[0] in ("on", "off", "toggle"):
            self.search_enabled = (not self.search_enabled) if args[0] == "toggle" else (args[0] == "on")
            self.config.search_default = self.search_enabled
            self.config.save()
            return t("cmd.resp.search_on" if self.search_enabled else "cmd.resp.search_off"), None
        return t("cmd.resp.search_usage"), None

    def _model(self, args: list) -> Response:
        """
        [EN]
        View or switch the model: supports profile aliases, local files, prefix
        matching, and download confirmation.

        [ZH]
        查看/切换模型：支持档案别名、本地文件、前缀匹配与下载征询。
        """
        if not args:
            return self._model_info(), None
        if args[0].lower() == "list":
            return self._model_list(), None

        name = args[0]

        # [EN] 1) Profile alias (the config.models registry).
        # [ZH] 1) 档案别名（config.models 注册表）
        profile = self.config.models.get(name)
        if profile:
            path = profile.get("path", name)
            resolved = path if os.path.exists(path) else os.path.join("models", path)
            if os.path.exists(resolved):
                return self._apply_model(resolved, path, profile)
            return t("cmd.resp.model_profile_missing", name=name, path=path), None

        # [EN] 2) Exact local match (absolute path or under models/).
        # [ZH] 2) 本地严格匹配（绝对路径或 models/ 下）
        candidates = []
        if os.path.isabs(name) or "/" in name or "\\" in name:
            candidates.append(os.path.abspath(name))
        candidates.append(os.path.join("models", name))
        local = next((p for p in candidates if os.path.exists(p)), None)
        if local:
            return self._apply_model(local, name, None)

        # [EN] 3) Local prefix/fuzzy match against .gguf files under models/.
        # [ZH] 3) 本地前缀/模糊匹配 models/ 中的 .gguf
        matches = [f for f in self._local_ggufs() if name.lower() in f.lower()]
        if len(matches) == 1:
            return self._apply_model(os.path.join("models", matches[0]), matches[0], None)
        if len(matches) > 1:
            return t("cmd.resp.model_ambiguous", items="\n".join(f"  {f}" for f in matches)), None

        # [EN] 4) Not found locally -> query the repository (requires network) and
        # [EN] download after confirmation.
        # [ZH] 4) 本地缺失 → 查询仓库（需要网络），征得同意后下载
        try:
            best, size = find_best_model(self.config.model_repo, name, hf_endpoint=self.config.hf_endpoint)
        except Exception as e:
            return t("cmd.resp.model_query_failed", name=name, error=e), None

        size_mb = f"{size / 1024 / 1024:.0f} MB" if size else t("cmd.resp.model_size_unknown")
        return (
            t("cmd.resp.model_confirm", name=name, repo=self.config.model_repo,
              best=best, size=size_mb),
            {"action": "confirm_model", "model": best},
        )

    def _local_ggufs(self) -> list:
        if not os.path.isdir("models"):
            return []
        return sorted(f for f in os.listdir("models") if f.lower().endswith(".gguf"))

    def _model_info(self) -> str:
        info = self.llm_engine.model_info()
        train = self.llm_engine.n_ctx_train
        unknown = t("cmd.status.unknown")
        lines = [
            t("cmd.model_info.current", model=self.config.model_path),
            t("cmd.model_info.arch",
              arch=info.get("general.architecture", unknown),
              quant=info.get("general.file_type", unknown)),
            t("cmd.model_info.context", n_ctx=self.llm_engine.n_ctx)
            + (t("cmd.model_info.context_train", train=train, max=self.config.max_context) if train
               else t("cmd.model_info.context_limit", max=self.config.max_context)),
            t("cmd.model_info.kv", kv=self.llm_engine.kv_cache_text()),
            t("cmd.model_info.threads", threads=self.llm_engine.n_threads, batch=self.llm_engine.n_batch),
            t("cmd.model_info.gpu", gpu=self.llm_engine.n_gpu_layers),
            t("cmd.model_info.locals", count=len(self._local_ggufs())),
        ]
        if self.conv_mgr.file_content:
            key = "cmd.model_info.file_summarized" if self.conv_mgr.file_summary else "cmd.model_info.file_raw"
            lines.append(t(key, name=os.path.basename(self.conv_mgr.file_name)))
        if self.config.models:
            lines.append(t("cmd.model_info.profiles", names=", ".join(self.config.models.keys())))
        if self.skill_parsers_provider is not None:
            try:
                owned = self.skill_parsers_provider() or {}
            except Exception:
                owned = {}
            if owned:
                detail = ", ".join(f"{ext} ← {sid}" for ext, sid in sorted(owned.items()))
                lines.append(t("cmd.model_info.skill_formats", formats=detail))
        lines.append(t("cmd.model_info.hint"))
        return "\n".join(lines)

    def _model_list(self) -> str:
        lines = [
            t("cmd.model_info.list_header", model=self.config.model_path),
            "",
            t("cmd.model_info.list_dir"),
        ]
        local = self._local_ggufs()
        if local:
            lines += [f"  {f}" for f in local]
        else:
            lines.append(t("cmd.model_info.list_empty"))
        if self.config.models:
            lines.append("")
            lines.append(t("cmd.model_info.list_profiles"))
            for alias, p in self.config.models.items():
                lines.append(t("cmd.model_info.list_profile_item", alias=alias, path=p.get("path", "?")))
        lines.append("")
        lines.append(t("cmd.model_info.list_footer"))
        return "\n".join(lines)

    def _apply_model(self, path: str, display: str, profile: Optional[dict]) -> Response:
        """
        [EN]
        Switch and reload the model; a profile may carry its own context_length and
        temperature.

        The context window is clamped to the new model's training window and
        config.max_context to prevent overflow.

        [ZH]
        切换并重新加载模型；档案可携带独立 context_length / temperature。

        上下文窗口按新模型的训练窗口与 config.max_context 收敛，防止超限。
        """
        # [EN] A model switch is a hardware decision as well: the accelerator,
        # [EN] thread count and context ceiling are recomputed for the new model
        # [EN] before the reload, so the previous model's tuning is not retained.
        # [ZH] 切换模型同样属于硬件决策：重新加载之前会为新模型重新计算加速器、
        # [ZH] 线程数与上下文上限，避免沿用上一个模型的调优参数。
        requested = int(profile["context_length"]) if profile and profile.get("context_length") else None
        train = resolve_context_window(path, 0)
        plan = hardware_profiler.plan_inference(
            profile=hardware_profiler.probe(),
            model_path=path,
            train_context=train,
            requested_context=int(requested or self.config.context_length or 0),
            max_context=int(self.config.max_context or hardware_profiler.DEFAULT_MAX_CONTEXT),
            requested_gpu_layers=int(self.config.n_gpu_layers or 0),
            n_threads_override=int(self.config.n_threads or 0),
            inference_profile=str(getattr(self.config, "inference_profile", "")),
            requested_n_batch=int(getattr(self.config, "n_batch", 0) or 0),
        )
        n_ctx = plan.n_ctx_initial
        try:
            self.llm_engine.apply_plan(plan)
            self.llm_engine.reload(path, n_ctx)
        except Exception as e:
            return t("cmd.resp.model_apply_failed", error=e), None
        self.config.model_path = display if not os.path.isabs(display) else os.path.basename(display)
        extra = t(
            "cmd.resp.model_applied_ctx",
            n_ctx=n_ctx,
            train=(t("cmd.resp.model_applied_train", train=train) if train and n_ctx < train else ""),
            kv=self.llm_engine.kv_cache_text(),
        )
        if profile and profile.get("temperature") is not None and not self.temp_manual:
            self.temperature = float(profile["temperature"])
            extra += t("cmd.resp.model_applied_temp", temp=self.temperature)
        self.config.save()
        return t("cmd.resp.model_applied", model=self.config.model_path) + extra, None

    def _clearcache(self, _args: list) -> Response:
        """
        [EN]
        Clear the KV cache and the segmented-summary cache (the SummaryCache from fix
        4).

        The summary cache docstring had always stated that "/clearcache also clears
        it", yet only `llm_engine.clear_cache()` (the KV cache) was called, so the two
        statements contradicted each other. The implementation now follows "clearing
        the cache should clear everything" rather than weakening the comment: the
        user who types /clearcache intends to discard the caches.

        [ZH]
        清 KV 缓存以及分段摘要缓存（修复四的 SummaryCache）。

        摘要缓存的文档字符串一直写着"/clearcache 也会清它"，但此前只调了
        `llm_engine.clear_cache()`（KV 缓存），两处说法矛盾。按"清缓存就该清干净"
        实现，而不是把注释改小 —— 用户敲 /clearcache 的意图就是把缓存倒掉。
        """
        ok = self.llm_engine.clear_cache()
        cleared = 0
        if self.cache_clear_provider is not None:
            try:
                cleared = int(self.cache_clear_provider() or 0)
            except Exception:  # noqa: BLE001 - [EN] a summary-cache failure does not affect the KV cache result
                               # [ZH] 摘要缓存清理失败不影响 KV 缓存清理结果
                cleared = 0
        if not ok:
            return t("cmd.resp.cache_clear_unsupported"), None
        if cleared:
            return t("cmd.resp.cache_cleared_with_summaries", count=cleared), None
        return t("cmd.resp.cache_cleared"), None

    def _prompt(self, args: list) -> Response:
        if not args:
            return t("cmd.resp.prompt_current",
                     text=self.conv_mgr.custom_prompt or t("cmd.resp.prompt_unset")), None
        if args[0].lower() == "reset":
            self.conv_mgr.set_custom_prompt("")
            self.llm_engine.clear_cache()
            # [EN] After the custom instruction is cleared, the task layer (if
            # [EN] applied) becomes active again; this must be stated explicitly.
            # [ZH] 自定义指令被清掉后，任务层（若已套用）会重新生效，要说清楚
            if self.conv_mgr.task_prompt:
                return (t("cmd.resp.prompt_reset")
                        + "\n" + t("cmd.resp.prompt_reset_restores_task",
                                   name=self.task_template or t("cmd.resp.task_none"))), None
            return t("cmd.resp.prompt_reset"), None
        text = " ".join(args)
        self.conv_mgr.set_custom_prompt(text)
        self.llm_engine.clear_cache()
        reply = t("cmd.resp.prompt_set", chars=len(text))
        # [EN] The custom instruction has the highest priority: it overrides both the
        # [EN] mode prompt and the task layer, which must be stated explicitly.
        # [ZH] 自定义指令优先级最高：它会盖住模式提示词与任务层，必须明说
        if self.conv_mgr.task_prompt:
            reply += "\n" + t("cmd.resp.prompt_overrides_task",
                              name=self.task_template or t("cmd.resp.task_none"))
        return reply, None

    @staticmethod
    def _darkmode(args: list) -> Response:
        if not args:
            return t("cmd.resp.darkmode_usage"), None
        key = {"on": "cmd.resp.darkmode_on", "off": "cmd.resp.darkmode_off"}.get(args[0].lower())
        if key is None:
            return t("cmd.resp.darkmode_usage"), None
        return t(key), None

    @staticmethod
    def _stop(_args: list) -> Response:
        return t("cmd.resp.stop_ack"), None

    @staticmethod
    def _snippet(_args: list) -> Response:
        return t("cmd.resp.snippet_hint"), None

    # ---------- [EN] Fix 4: task templates / generation presets / manual context compaction ----------
    # ---------- [ZH] 兼容性修正：任务模板 / 生成预设 / 手动压缩上下文 ----------

    def _task(self, args: list) -> Response:
        """
        [EN]
        ``/task``: task-oriented prompt templates (direction 1).

        * No argument or ``list``: list all templates and their intended scenarios;
        * ``<id|label>``: apply a template by writing it into the task layer, which
          is stacked on top of the current mode prompt;
        * ``off`` / ``reset``: remove only the task layer (does not touch a custom
          instruction set by ``/prompt``).

        Behaviour before the v0.2.1 prompt optimization: the template was written
        into ``custom_prompt``, so it replaced the mode prompt (contradicting the note
        in ``prompts.py`` that it is appended to the system prompt rather than
        replacing the conversation mode), and ``/task off`` also cleared the
        instruction the user had set with ``/prompt``. Task layer and custom
        instruction are now stored separately, which eliminates both problems.

        [ZH]
        ``/task``：任务型提示词模板（方向一）。

        * 无参数或 ``list``：列出全部模板及其适用场景；
        * ``<id|标签>``：套用模板，写入任务层——叠加在当前模式提示词之上；
        * ``off`` / ``reset``：只取消任务层（不动 ``/prompt`` 设的自定义指令）。

        v0.2.1 提示词优化前的行为：模板被写进 ``custom_prompt``，于是它替换掉了
        模式提示词（与 ``prompts.py`` 里"附加在系统提示词后面，不替换对话模式"的
        说明矛盾），并且 ``/task off`` 会把用户用 ``/prompt`` 设的指令一起清掉。
        现在任务层与自定义指令分开存放，两处问题都消除。
        """
        import prompts as prompt_lib

        name = " ".join(args).strip() if args else ""
        if not name or name.lower() in ("list", "?", "help"):
            current = self.task_template or t("cmd.resp.task_none")
            return (t("cmd.resp.task_list_header", current=current) + "\n"
                    + prompt_lib.menu_text() + "\n\n"
                    + t("cmd.resp.task_usage")), None

        if name.lower() in ("off", "reset", "none", "clear"):
            self.task_template = ""
            self.conv_mgr.set_task_prompt("")
            self.llm_engine.clear_cache()  # [EN] The system prompt changed, so the KV cache must be invalidated.
                                           # [ZH] 系统提示词变了，KV 缓存必须失效。
            # [EN] When a custom instruction is still active, explain that clearly so
            # [EN] the user does not think cancellation had no effect.
            # [ZH] 自定义指令仍在生效时说明清楚，避免用户以为"取消了却没变化"
            if self.conv_mgr.custom_prompt:
                return (t("cmd.resp.task_cleared")
                        + "\n" + t("cmd.resp.task_cleared_keeps_prompt")), None
            return t("cmd.resp.task_cleared"), None

        text = prompt_lib.build_system_prompt(name)
        if not text:
            return t("cmd.resp.task_not_found", name=name, ids=", ".join(prompt_lib.template_ids())), None
        template = prompt_lib.get_template(name) or {}
        self.task_template = str(template.get("label") or name)
        # [EN] Record the template id as well, so that `/task guide` and the legacy
        # [EN] guide mode behave identically (see main._vague_guide: deterministic
        # [EN] guidance phrasing is not injected when the guide template is active).
        # [ZH] 同时记下模板 id：/task guide 与旧的 guide 模式行为要一致
        # [ZH] （见 main._vague_guide：套了 guide 模板时不再插确定性引导话术）。
        self.conv_mgr.set_task_prompt(text, str(template.get("id") or name))
        self.llm_engine.clear_cache()  # [EN] Same as above: the old cache is invalid after the prompt changes.
                                       # [ZH] 同上：提示词变化后旧缓存不再适用。
        reply = t("cmd.resp.task_applied", name=self.task_template) + "\n" + t("cmd.resp.task_hint")
        # [EN] The custom instruction has the highest priority and overrides the task
        # [EN] layer; this must be stated, otherwise the user assumes the template did
        # [EN] not take effect.
        # [ZH] 自定义指令优先级最高，会盖住任务层：必须明说，否则用户会以为套用没生效。
        if self.conv_mgr.custom_prompt:
            reply += "\n" + t("cmd.resp.task_overridden_by_prompt")
        return reply, None

    def _agent(self, args: list) -> Response:
        """
        [EN]
        ``/agent``: inspect and adjust the Agent platform (capability extension 1,
        task 2).

        * No argument / ``list``: current mode, step limit, and tool list (including
          permission tier and enabled state);
        * ``allow <tool>`` / ``deny <tool>``: pre-authorization switches. Sensitive
          tools (web search, local file reading) are disabled by default and enabled
          explicitly here; the setting is persisted in config.json. This removes the
          need to confirm every single invocation: the task specification permits a
          session-level authorization tier, and it suits the fast loop of a local
          small model better than per-call confirmation.
        * ``steps <1-8>``: tool-call step limit.
        * ``on`` / ``off``: equivalent to ``/mode agent`` and ``/mode chat`` (Agent is
          an interaction form, so switching mode is the most intuitive control).

        [ZH]
        ``/agent``：查看与调整 Agent 平台（能力扩展-1 任务二）。

        * 无参数 / ``list``：当前模式、步数上限、工具清单（含权限与启用状态）；
        * ``allow <工具>`` / ``deny <工具>``：预授权开关。敏感工具（联网搜索、
          读本机文件）默认关闭，用户在这里显式打开；写进 config.json 持久生效。
          这样"每一次调用都弹窗确认"就不需要了 —— 任务书允许"会话级授权"这一档，
          而它比每次确认更适合本地小模型的快节奏循环。
        * ``steps <1-8>``：工具调用步数上限。
        * ``on`` / ``off``：等价于 ``/mode agent`` 与 ``/mode chat``（Agent 是一种
          交互形态，用模式切换最直观）。
        """
        writer = getattr(self, "agent_configurator", None)
        state = writer() if writer is not None else {}
        sub = (args[0].strip().lower() if args else "list")
        rest = args[1:]

        if sub in ("on", "off"):
            target = "agent" if sub == "on" else "chat"
            if not self.conv_mgr.set_mode(target):
                return t("cmd.resp.mode_unknown", name=target), None
            self.config.default_mode = self.conv_mgr.get_mode()
            self.config.save()
            self.llm_engine.clear_cache()
            return t("cmd.resp.agent_mode", state=sub, mode=self.conv_mgr.get_mode()), None

        if sub in ("allow", "deny") and rest:
            name = rest[0].strip()
            if writer is None:
                return t("cmd.resp.agent_unavailable"), None
            result = writer(name, sub == "allow")
            if not result.get("ok"):
                return t("cmd.resp.agent_unknown_tool", name=name,
                         tools=", ".join(result.get("tools", []) or [])), None
            return t("cmd.resp.agent_permission", tool=name,
                     state=t("cmd.status.search_on") if sub == "allow"
                     else t("cmd.status.search_off")), None

        if sub in ("steps", "max") and rest:
            if writer is None:
                return t("cmd.resp.agent_unavailable"), None
            try:
                value = int(rest[0])
            except (TypeError, ValueError):
                return t("cmd.resp.agent_bad_steps"), None
            result = writer("__steps__", value)
            return t("cmd.resp.agent_steps", steps=result.get("max_steps", "")), None

        if sub not in ("list", "?", "help", ""):
            return t("cmd.resp.agent_usage"), None
        if not state:
            return t("cmd.resp.agent_unavailable"), None
        lines = [t("cmd.resp.agent_state", mode=state.get("mode", ""),
                   steps=state.get("max_steps", ""),
                   enabled=len(state.get("enabled_tools", []) or []) or 0,
                   total=len(state.get("tools", []) or []) or 0)]
        for tool in state.get("tools", []) or []:
            mark = "✅" if tool.get("enabled") else "🔒"
            lines.append(t("cmd.resp.agent_tool_line", mark=mark, name=tool.get("name", ""),
                           risk=tool.get("risk", ""), desc=tool.get("description", "")))
        lines.append(t("cmd.resp.agent_hint"))
        return "\n".join(lines), None

    def _mcp(self, args: list) -> Response:
        """
        [EN]
        ``/mcp``: inspect and start/stop MCP servers (capability extension 1, task
        3).

        * No argument / ``list``: start/stop state, connection state, tool count, and
          error for every server;
        * ``enable <name>`` / ``disable <name>``: takes effect immediately, no restart
          required;
        * ``connect``: actually launches the process and fetches its tool list (a
          costly action, so it is not performed automatically).
        Permissions are not handled here: MCP tools are always treated as sensitive,
        and authorization goes through ``/agent allow``.

        [ZH]
        ``/mcp``：查看与启停 MCP Server（能力扩展-1 任务三）。

        * 无参数 / ``list``：每个 server 的启停状态、连接状态、工具数与错误；
        * ``enable <名称>`` / ``disable <名称>``：立即生效（无需重启）；
        * ``connect``：真的拉起进程并拉取工具清单（这是有代价的动作，所以不自动做）。
        权限不在这里管：MCP 工具一律按敏感工具处理，授权走 ``/agent allow``。
        """
        control = getattr(self, "mcp_control", None)
        if control is None:
            return t("cmd.resp.mcp_unavailable"), None
        state = control() or {}
        sub = (args[0].strip().lower() if args else "list")
        rest = args[1:]

        if sub in ("enable", "disable") and rest:
            name = rest[0].strip()
            result = control("enable" if sub == "enable" else "disable", name)
            if not result.get("ok"):
                return t("cmd.resp.mcp_unknown_server", name=name), None
            return t("cmd.resp.mcp_toggled", name=name,
                     state=t("cmd.status.search_on") if sub == "enable"
                     else t("cmd.status.search_off")), None

        if sub == "connect":
            result = control("connect") or {}
            state = result.get("state") or state
            if result.get("registered"):
                return t("cmd.resp.mcp_connected", count=result.get("registered")), None
            return t("cmd.resp.mcp_connect_failed",
                     error=state.get("error") or t("cmd.resp.mcp_no_tools")), None

        if sub not in ("list", "?", "help", ""):
            return t("cmd.resp.mcp_usage"), None

        if not state.get("enabled"):
            return t("cmd.resp.mcp_disabled"), None
        servers = state.get("servers") or []
        if not servers:
            return t("cmd.resp.mcp_no_servers"), None
        lines = [t("cmd.resp.mcp_state", count=len(servers),
                   tools=len(state.get("tools") or []))]
        for server in servers:
            mark = "✅" if server.get("connected") else ("⏸" if not server.get("enabled") else "⚠️")
            detail = t("cmd.resp.mcp_server_line",
                       mark=mark, name=server.get("name", ""),
                       state=t("cmd.status.search_on") if server.get("enabled")
                       else t("cmd.status.search_off"),
                       tools=server.get("tool_count", 0))
            lines.append(detail)
            if server.get("error"):
                lines.append("    ⚠️ " + str(server.get("error")))
            for tool in (server.get("tools") or [])[:8]:
                lines.append("    - " + str(tool))
        lines.append(t("cmd.resp.mcp_hint"))
        return "\n".join(lines), None

    def _preset(self, args: list) -> Response:
        """
        [EN]
        ``/preset``: three generation parameter presets (light / balanced / high
        quality, direction 6).

        It sets temperature / top_p / repeat_penalty / max_tokens together and records
        them in the configuration, so they survive a restart; a later manual
        temperature change via ``/temp`` takes precedence.

        [ZH]
        ``/preset``：轻量 / 均衡 / 高质量三档生成参数（方向六）。

        会同时设置 temperature / top_p / repeat_penalty / max_tokens，并记入配置，
        因此重启后仍然生效；用户随后用 ``/temp`` 手动改温度时以手动值为准。
        """
        import model_profile as profile_lib

        name = " ".join(args).strip().lower() if args else ""
        if not name or name in ("list", "?", "help"):
            lines = [t("cmd.resp.preset_list_header", current=self.config.preset or "balanced")]
            for item in profile_lib.preset_menu():
                lines.append(f"  /preset {item['id']}  — {item['label']}：{item['note']}")
            return "\n".join(lines), None

        preset = profile_lib.apply_preset(name)
        if preset is None:
            return t("cmd.resp.preset_not_found", name=name,
                     ids=", ".join(p["id"] for p in profile_lib.preset_menu())), None
        self.config.preset = name
        self.config.temperature = float(preset["temperature"])
        self.config.top_p = float(preset["top_p"])
        self.config.repeat_penalty = float(preset["repeat_penalty"])
        self.config.max_tokens = int(preset["max_tokens"])
        self.temperature = float(preset["temperature"])
        self.temp_manual = False   # [EN] Return to "the preset decides", so mode switches apply mode temperatures again.
                                   # [ZH] 回到"预设说了算"，这样切模式时温度也会跟着模式走。
        self.llm_engine.clear_cache()
        return (t("cmd.resp.preset_applied", name=preset["label"], temp=preset["temperature"],
                  top_p=preset["top_p"], penalty=preset["repeat_penalty"],
                  max_tokens=preset["max_tokens"])
                + "\n" + str(preset["note"])), None

    def _compact(self, args: list) -> Response:
        """
        [EN]
        ``/compact [auto|off] [focus on <topic>]``: manual context compaction (fix 4;
        extended by task 9).

        Difference from automatic compaction: automatic compaction triggers only when
        ``compact.trigger_ratio`` (70% by default) is reached, so a user who wants to
        free space early while tokens remain (for example before uploading a large
        file) must trigger it manually.

        Task 9 (known-issue optimization 2) adds ``focus on <text>``: the user's
        focus (for example "contract amount") is appended to the summary prompt so
        the compactor preserves that information first. Parsing is deliberately
        lenient: ``focus on X`` / ``focus X`` / ``on X`` are all accepted, and the
        focus is recognized only when the first argument is focus/on; otherwise the
        legacy compaction behaviour applies.

        [ZH]
        ``/compact [auto|off] [focus on <重点>]``：手动压缩上下文（修复四；任务九扩展）。

        与自动压缩的区别：自动压缩只在达到 ``compact.trigger_ratio``（默认 70%）时
        触发，用户想在还有余量时提前"腾地方"（例如准备上传一个大文件）就得手动触发。

        任务九（优化已知问题-2）新增 ``focus on <文字>``：把用户指定的重点
        （如"合同金额"）追加进摘要提示词，让压缩器优先保住那部分信息。
        解析刻意宽松：``focus on X`` / ``focus X`` / ``on X`` 都认，
        且只在第一个参数是 focus/on 时才当重点，否则按旧行为压缩。
        """
        if self.compact_provider is None:
            return t("cmd.resp.compact_unavailable"), None

        force = True
        focus = ""
        rest = list(args)
        if rest and str(rest[0]).lower() in ("auto", "off"):
            force = False
            rest = rest[1:]
        if rest and str(rest[0]).lower() in ("focus", "on", "重点"):
            rest = rest[1:]
            if rest and str(rest[0]).lower() == "on":
                rest = rest[1:]
            focus = " ".join(str(part) for part in rest).strip()

        try:
            result = self.compact_provider(force, focus) if focus else self.compact_provider(force)
        except TypeError:
            # [EN] Compatibility with older providers that accept only force
            # [EN] (third-party or test doubles).
            # [ZH] 兼容只接受 force 的旧 provider（第三方/测试替身）
            result = self.compact_provider(force)
        except Exception as e:  # noqa: BLE001 - [EN] a compaction failure must not crash the command
                               # [ZH] 压缩失败不能让指令崩溃
            return t("cmd.resp.compact_failed", error=e), None
        if not result:
            result = {}
        if not result.get("compressed"):
            return t("cmd.resp.compact_skipped",
                     used=result.get("used", "?"), limit=result.get("limit", "?")), None
        reply = t("cmd.resp.compact_done",
                  dropped=result.get("dropped", 0), kept=result.get("kept", 0),
                  before=result.get("used_before", "?"), after=result.get("used_after", "?"))
        if focus:
            reply += "\n" + t("cmd.resp.compact_focus", focus=focus)
        return reply, None

    def _status(self, _args: list) -> Response:
        # [EN] Estimate current context usage (system prompt + history).
        # [ZH] 估算当前上下文占用（系统提示 + 历史）
        try:
            msgs = self.conv_mgr.build_messages("")
            used = sum(self.llm_engine.count_tokens(m["content"]) for m in msgs)
            limit = int(self.llm_engine.n_ctx * 0.8)
            ctx_line = t("cmd.status.context", used=used, limit=limit, n_ctx=self.llm_engine.n_ctx)
        except Exception:
            ctx_line = t("cmd.status.context_unknown")
        unknown = t("cmd.status.unknown")
        manual_tag = t("cmd.resp.temp_manual_tag") if self.temp_manual else t("cmd.resp.temp_preset_tag")
        file_line = t("cmd.status.file", name=(
            os.path.basename(self.conv_mgr.file_name) if self.conv_mgr.file_name
            else t("cmd.status.file_none")))
        if self.conv_mgr.file_summary:
            file_line += t("cmd.status.file_summarized",
                           chunks=self.conv_mgr.file_summary_meta.get("chunks", "?"))
        lines = [
            t("cmd.status.header", app=APP_NAME, version=APP_VERSION, studio=t("app.studio")),
            t("cmd.status.model", model=self.config.model_path),
            t("cmd.status.arch", arch=self.llm_engine.model_info().get("general.architecture", unknown)),
            t("cmd.status.mode", mode=self.conv_mgr.get_mode()),
            t("cmd.status.temperature", temp=self.temperature, manual=manual_tag),
            t("cmd.status.threads",
              threads=getattr(self.llm_engine, "n_threads", "?"),
              batch=getattr(self.llm_engine, "n_batch", 512),
              gpu=getattr(self.llm_engine, "n_gpu_layers", 0)),
            t("cmd.status.kv", kv=self.llm_engine.kv_cache_text()),
            ctx_line,
            t("cmd.status.search", state=(
                t("cmd.status.search_on") if self.search_enabled else t("cmd.status.search_off"))),
            t("cmd.status.turns", turns=len(self.conv_mgr.history) // 2),
            t("cmd.status.summary", state=(
                t("cmd.status.summary_yes") if self.conv_mgr.summary else t("cmd.status.summary_no"))),
            file_line,
        ]
        # [EN] Fix 4 (direction 7: capability transparency): append the model
        # [EN] capability profile to /status. These fields previously lived only in
        # [EN] logs, so the user could not see how large the model is, how much of the
        # [EN] window is actually open, or how fast it runs.
        # [ZH] 兼容性修正（方向七：能力透明化）：把模型能力画像拼进 /status。
        # [ZH] 这些字段以前散在日志里，用户看不到"模型有多大、窗口实际开了多少、跑得多快"。
        lines.extend(self._capability_lines())
        return "\n".join(lines), None

    def _capability_lines(self) -> List[str]:
        """
        [EN]
        Model capability section (parameter count / quantization / training window /
        usable window / KV cache / speed / hints).

        [ZH]
        模型能力段落（参数量 / 量化 / 训练窗口 / 实际窗口 / KV Cache / 速度 / 提示）。
        """
        if self.profile_provider is None:
            return []
        try:
            profile = self.profile_provider() or {}
        except Exception:  # noqa: BLE001 - [EN] a profile failure must not affect /status
                           # [ZH] 能力画像失败不应影响 /status
            return []
        if not profile:
            return []
        lines = [
            "",
            t("cmd.status.capability_header"),
            t("cmd.status.parameters", value=profile.get("parameters_text") or t("cmd.status.unknown"),
              source=profile.get("parameters_source") or t("cmd.status.unknown")),
            t("cmd.status.quantization", value=profile.get("quantization") or t("cmd.status.unknown"),
              source=profile.get("quantization_source") or t("cmd.status.unknown")),
            t("cmd.status.window_detail",
              train=profile.get("train_context") or t("cmd.status.unknown"),
              usable=profile.get("usable_context") or t("cmd.status.unknown")),
            t("cmd.status.speed", value=profile.get("speed_text") or t("cmd.status.not_measured")),
            t("cmd.status.hardware",
              ram=(f"{profile['ram_gb']} GB" if profile.get("ram_gb")
                   else t("cmd.status.unknown")),
              cores=profile.get("cpu_cores") or t("cmd.status.unknown")),
            t("cmd.status.context_mode", mode=self.config.context_mode),
            t("cmd.status.preset", preset=self.config.preset or "balanced"),
        ]
        if self.task_template:
            lines.append(t("cmd.status.task_template", name=self.task_template))
        # [EN] A custom instruction overrides both the mode block and the task layer,
        # [EN] so it is key information about what is actually in effect.
        # [ZH] 自定义指令会盖住模式块与任务层，属于"当前实际生效的是什么"的关键信息
        if self.conv_mgr.custom_prompt:
            lines.append(t("cmd.status.custom_prompt", chars=len(self.conv_mgr.custom_prompt)))
        notes = profile.get("warnings") or []
        if notes:
            lines.append(t("cmd.status.capability_notes"))
            lines.extend(f"  • {note}" for note in notes)
        return lines

    @staticmethod
    def _about(_args: list) -> Response:
        """
        [EN]
        ``/about``: version, publisher, and compliance documents (task specification
        fix 5 requires the about page to show license links).

        The license and privacy documents are distributed with the release package
        (LICENSE/NOTICE at the package root and PRIVACY/EULA/THIRD_PARTY_LICENSES/
        SBOM under docs\\), so the actual local paths are shown here as well; if the
        links do not work, the user can still find the files in the file manager.
        The repository address is also given for online reference.

        [ZH]
        ``/about``：版本号、出品方与合规文档（任务书修复五要求"关于页面展示许可证链接"）。

        许可证与隐私文档随发布包一起分发（外层根目录的 LICENSE/NOTICE 与 docs\\ 下的
        PRIVACY/EULA/THIRD_PARTY_LICENSES/SBOM），因此这里给出本机实际路径，
        用户点不动链接时也能自己在文件管理器里找到；同时给出仓库地址供在线查阅。
        """
        from config import program_dir, program_root

        try:
            data_root = program_root()
            program = program_dir()
        except Exception:  # noqa: BLE001 - [EN] a path-resolution failure must not affect /about
                           # [ZH] 路径解析失败不影响 /about
            data_root = program = ""
        lines = [
            f"✨ {APP_NAME} {APP_VERSION}",
            # [EN] /about is the canonical place for the complete attribution: use the
            # [EN] full form on first occurrence (Chinese: 熵寂工作室（Void-Entropia-Studio）);
            # [EN] all other locations use the short form.
            # [ZH] /about 是"何处出现完整署名"的规范位置，用首次完整形式
            # [ZH] （中文：熵寂工作室（Void-Entropia-Studio））；其余位置用简称。
            f"Powered by {t('app.studio_full')}",
            t("app.about"),
            "",
            t("app.about.legal_header"),
        ]
        candidates = [
            ("LICENSE", os.path.join(data_root, "LICENSE") if data_root else ""),
            ("NOTICE", os.path.join(data_root, "NOTICE") if data_root else ""),
            ("PRIVACY.md", os.path.join(data_root, "docs", "PRIVACY.md") if data_root else ""),
            ("EULA.md", os.path.join(data_root, "docs", "EULA.md") if data_root else ""),
            ("THIRD_PARTY_LICENSES.md",
             os.path.join(data_root, "docs", "THIRD_PARTY_LICENSES.md") if data_root else ""),
            ("SBOM.json", os.path.join(data_root, "docs", "SBOM.json") if data_root else ""),
        ]
        for label, path in candidates:
            if path and os.path.isfile(path):
                lines.append(f"  • {label}: {path}")
            else:
                lines.append("  • " + t("app.about.legal_missing", name=label))
        if program:
            lines.append(t("app.about.program_dir", path=program))
        lines.append(t("app.about.repo"))
        return "\n".join(lines), None

    def _clear(self, _args: list) -> Response:
        self.conv_mgr.clear()
        self.llm_engine.clear_cache()
        return t("cmd.resp.clear_done"), None

    def _undo(self, _args: list) -> Response:
        if self.conv_mgr.undo():
            self.llm_engine.clear_cache()
            return t("cmd.resp.undo_done"), None
        return t("cmd.resp.undo_none"), None

    def _save(self, args: list) -> Response:
        if not args:
            return t("cmd.resp.save_need_name"), None
        filename = args[0] if args[0].endswith(".md") else args[0] + ".md"
        try:
            Path(filename).write_text(self.conv_mgr.export_markdown(), encoding="utf-8")
            return t("cmd.resp.save_done", path=filename), None
        except OSError as e:
            return t("cmd.resp.save_failed", error=e), None

    def _temp(self, args: list) -> Response:
        if not args:
            manual = t("cmd.resp.temp_manual_tag") if self.temp_manual else t("cmd.resp.temp_preset_tag")
            return t("cmd.resp.temp_current", temp=self.temperature, manual=manual), None
        if args[0].lower() == "reset":
            self.temp_manual = False
            preset = MODE_TEMPERATURES.get(self.conv_mgr.get_mode(), self.config.temperature)
            self.temperature = preset
            self.config.temperature = preset
            self.config.save()
            return t("cmd.resp.temp_reset", temp=preset), None
        try:
            value = float(args[0])
        except ValueError:
            return t("cmd.resp.temp_invalid"), None
        if not 0.0 <= value <= 1.0:
            return t("cmd.resp.temp_range"), None
        self.temp_manual = True
        self.temperature = value
        self.config.temperature = value
        self.config.save()
        return t("cmd.resp.temp_set", temp=value), None
