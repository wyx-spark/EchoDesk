"""指令解析：处理以 '/' 开头的用户指令。

v0.2.1（任务书 任务三 3.4 / 3.7）：所有面向用户的文案改为从语言包取用
（``backend/lang/<locale>.json``），并按"当前语言 → en → zh-Hans → 键名"回退。

v0.2.1（任务书 任务二 2.2）：指令表支持运行期注册外部指令（技能/本地插件），
行为与内置指令完全一致；``COMMAND_SPEC`` 同时是 /help 与前端指令面板的数据源，
因此技能指令会自动出现在输入框的候选列表里。
"""
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import Config
from conversation import ConversationManager, MODE_TEMPERATURES
from download_utils import find_best_model
from file_parser import is_parse_error, localize_error, parse_file_with_wps
from gguf_meta import resolve_context_window
from i18n import get as _i18n
from i18n import t as _t
from version import APP_NAME, APP_VERSION

# 返回：反馈文本 + 附加动作（dict，如 {"action": "exit"} / {"action": "confirm_model", ...}）
Response = Tuple[str, Optional[dict]]


def t(key: str, **kwargs: Any) -> str:
    """本模块内取文案的快捷方式。"""
    return _t(key, **kwargs)


# ---------------------------------------------------------------------------
# 指令元数据（顺序 = /help 与前端指令面板的展示顺序）
# ---------------------------------------------------------------------------
# 字段说明：
#   name        指令名（同时决定语言包键 cmd.items.<name>）
#   category    分组，前端据此排序/分组
#   destructive 有副作用且不可逆，前端执行前二次确认（v0.2.0 修复二）
#   hidden      不在 /help 与指令面板中展示（如别名）
COMMAND_SPEC: List[Dict[str, Any]] = [
    {"name": "/help", "category": "core"},
    {"name": "/status", "category": "core"},
    {"name": "/about", "category": "core"},
    {"name": "/mode", "category": "chat"},
    {"name": "/task", "category": "chat"},
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

#: 别名 → 主指令名（不单独展示）
COMMAND_ALIASES: Dict[str, str] = {"/quit": "/exit"}


def describe_commands(extra: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """返回本地化后的指令元数据列表（前端指令面板 / 帮助的数据源）。

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
    """构建 /help 的完整文案（含技能指令，任务书任务二 2.6）。"""
    # 出品方名称走语言包（任务书「工作室名称敲定」）：中文界面显示「熵寂工作室」、
    # 英文界面显示 "Void Entropia Studio"。**不要**在这里直接用 version.STUDIO ——
    # 那是英文常量，中文界面会跟着显示英文名（`{studio}` 占位符的值由调用方给）。
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
        # 技能/本地插件注册的外部指令：{"/hello": {...}}（任务二 2.2）
        self._external: Dict[str, Dict[str, Any]] = {}

        self._handlers: Dict[str, Callable[[list], Response]] = {
            "/exit": lambda _: self._exit(),
            "/quit": lambda _: self._exit(),
            "/help": self._help,
            "/mode": self._mode,
            "/task": self._task,
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
        # 由 main 注入：返回技能已接管的文件扩展名（/model、/status 里展示）
        self.skill_parsers_provider: Optional[Callable[[], Dict[str, str]]] = None
        # 由 main 注入：手动压缩上下文（修复四 基础要求）。
        # 压缩需要模型参与（把早期对话总结成摘要），而 LLM 的调用与历史管理都在
        # main 里，因此这里只留一个回调，避免把 main 的逻辑抄一份进来。
        self.compact_provider: Optional[Callable[[bool], Dict[str, Any]]] = None
        # 由 main 注入：模型能力画像（修复四 方向六/七，/status 与 /preset 都要用）
        self.profile_provider: Optional[Callable[[], Dict[str, Any]]] = None
        # 由 main 注入：清空分段摘要缓存并返回清掉的条目数（/clearcache 用）
        self.cache_clear_provider: Optional[Callable[[], int]] = None
        # 当前生效的任务模板 id（修复四 方向一）；空串表示未启用
        self.task_template = ""

    # ---------- 运行期指令注册（任务二 2.2 / 2.4） ----------

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
        """注册一个外部指令（技能/本地插件）。

        * 指令名统一小写、必须以 ``/`` 开头；
        * **不能覆盖内置指令**（返回 False），保证主程序行为可预测；
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
        """卸载某个技能注册的全部指令，返回移除数量（2.4 卸载热重载）。"""
        doomed = [k for k, v in self._external.items() if v.get("skill_id") == skill_id]
        for key in doomed:
            self._external.pop(key, None)
        return len(doomed)

    def external_commands(self) -> List[Dict[str, Any]]:
        """技能指令的元数据（供 /help 与前端指令面板）。"""
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
        """内置 + 技能指令的完整元数据（前端指令面板用）。

        技能指令在 ``register_command`` 时就进了 ``_external``，
        因此这里不需要再去问加载器，避免了双重数据源不一致的风险。
        """
        return describe_commands(self.external_commands())

    def handle(self, cmd_line: str) -> Response:
        """解析并执行指令，返回 (反馈文本, 附加动作)。"""
        parts = cmd_line.strip().split()
        if not parts:
            return "", None
        name = parts[0].lower()
        handler = self._handlers.get(name)
        if handler is not None:
            return handler(parts[1:])

        # 技能/本地插件指令：异常必须被隔离（任务二 2.4）
        external = self._external.get(name)
        if external is not None:
            try:
                result = external["handler"](parts[1:])
            except Exception as e:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                skill = external.get("skill_name") or external.get("skill_id") or "?"
                return t("cmd.disabled", skill=skill, name=name, reason=f"{type(e).__name__}: {e}"), None
            return self._coerce_external(name, result)

        return t("cmd.unknown", name=parts[0]), None

    @staticmethod
    def _coerce_external(name: str, result: Any) -> Response:
        """把技能处理函数的返回值统一成 (文本, 动作)。

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

    # ---------- 指令实现 ----------

    def _help(self, _args: list) -> Response:
        # 技能指令也要出现在 /help 里（任务书 2.6）
        return help_text(self.external_commands()), None

    @staticmethod
    def _exit() -> Response:
        return t("cmd.resp.exit"), {"action": "exit"}

    def _mode(self, args: list) -> Response:
        if not args:
            return t("cmd.resp.mode_current", mode=self.conv_mgr.get_mode()), None
        if args[0].lower() == "list":
            return t("cmd.resp.mode_list", menu=self.conv_mgr.mode_menu()), None
        if self.conv_mgr.set_mode(args[0]):
            mode = self.conv_mgr.get_mode()
            self.config.default_mode = mode
            self.config.save()
            self.llm_engine.clear_cache()  # 系统提示词变化，旧 KV 缓存失效
            # 未手动设置过温度时，自动套用该模式的温度预设
            preset = MODE_TEMPERATURES.get(mode)
            if not self.temp_manual and preset is not None:
                self.temperature = preset
                return t("cmd.resp.mode_set_preset", mode=mode, preset=preset), None
            return t("cmd.resp.mode_set", mode=mode), None
        return t("cmd.resp.mode_unknown", name=args[0]), None

    def _file(self, args: list) -> Response:
        if not args:
            return t("cmd.resp.file_need_path"), None
        filepath = args[0]
        if not os.path.exists(filepath):
            return t("cmd.resp.file_missing", path=filepath), None
        # 统一走后端的加载入口：过长文档自动分段摘要后再注入
        if self.file_loader is not None:
            return self.file_loader(filepath), None
        content = parse_file_with_wps(filepath, self.config.file_max_chars)
        if is_parse_error(content):
            return t("cmd.resp.file_load_failed", content=localize_error(content)), None
        self.conv_mgr.set_file(filepath, content)
        return t("cmd.resp.file_loaded", name=os.path.basename(filepath), chars=len(content)), None

    def _summarize(self, args: list) -> Response:
        """手动触发全文摘要（长文档注入前先分段摘要）。"""
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
        # toggle：供输入框下方的状态区一键切换（优化五），等价于 on/off 的取反
        if args[0] in ("on", "off", "toggle"):
            self.search_enabled = (not self.search_enabled) if args[0] == "toggle" else (args[0] == "on")
            self.config.search_default = self.search_enabled
            self.config.save()
            return t("cmd.resp.search_on" if self.search_enabled else "cmd.resp.search_off"), None
        return t("cmd.resp.search_usage"), None

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
            return t("cmd.resp.model_profile_missing", name=name, path=path), None

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
            return t("cmd.resp.model_ambiguous", items="\n".join(f"  {f}" for f in matches)), None

        # 4) 本地缺失 → 查询仓库（需要网络），征得同意后下载
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
        """切换并重新加载模型；档案可携带独立 context_length / temperature。

        上下文窗口按新模型的训练窗口与 config.max_context 收敛，防止超限。
        """
        requested = int(profile["context_length"]) if profile and profile.get("context_length") else None
        train = resolve_context_window(path, 0)
        n_ctx = self.config.context_for_model(path, train, requested)
        try:
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
        """清 KV 缓存**以及**分段摘要缓存（修复四的 SummaryCache）。

        摘要缓存的文档字符串一直写着"/clearcache 也会清它"，但此前只调了
        `llm_engine.clear_cache()`（KV 缓存），两处说法矛盾。按"清缓存就该清干净"
        实现，而不是把注释改小 —— 用户敲 /clearcache 的意图就是把缓存倒掉。
        """
        ok = self.llm_engine.clear_cache()
        cleared = 0
        if self.cache_clear_provider is not None:
            try:
                cleared = int(self.cache_clear_provider() or 0)
            except Exception:  # noqa: BLE001 - 摘要缓存清理失败不影响 KV 缓存清理结果
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
            # 自定义指令被清掉后，任务层（若已套用）会重新生效，要说清楚
            if self.conv_mgr.task_prompt:
                return (t("cmd.resp.prompt_reset")
                        + "\n" + t("cmd.resp.prompt_reset_restores_task",
                                   name=self.task_template or t("cmd.resp.task_none"))), None
            return t("cmd.resp.prompt_reset"), None
        text = " ".join(args)
        self.conv_mgr.set_custom_prompt(text)
        self.llm_engine.clear_cache()
        reply = t("cmd.resp.prompt_set", chars=len(text))
        # 自定义指令优先级最高：它会盖住模式提示词与任务层，必须明说
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

    # ---------- 修复四：任务模板 / 生成预设 / 手动压缩上下文 ----------

    def _task(self, args: list) -> Response:
        """``/task``：任务型提示词模板（方向一）。

        * 无参数或 ``list``：列出全部模板及其适用场景；
        * ``<id|标签>``：套用模板，写入**任务层**——叠加在当前模式提示词之上；
        * ``off`` / ``reset``：只取消任务层（**不动** ``/prompt`` 设的自定义指令）。

        v0.2.1 提示词优化前的行为：模板被写进 ``custom_prompt``，于是它**替换**掉了
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
            self.llm_engine.clear_cache()  # 系统提示词变了，KV 缓存必须失效
            # 自定义指令仍在生效时说明清楚，避免用户以为"取消了却没变化"
            if self.conv_mgr.custom_prompt:
                return (t("cmd.resp.task_cleared")
                        + "\n" + t("cmd.resp.task_cleared_keeps_prompt")), None
            return t("cmd.resp.task_cleared"), None

        text = prompt_lib.build_system_prompt(name)
        if not text:
            return t("cmd.resp.task_not_found", name=name, ids=", ".join(prompt_lib.template_ids())), None
        template = prompt_lib.get_template(name) or {}
        self.task_template = str(template.get("label") or name)
        self.conv_mgr.set_task_prompt(text)
        self.llm_engine.clear_cache()  # 同上：提示词变化后旧缓存不再适用
        reply = t("cmd.resp.task_applied", name=self.task_template) + "\n" + t("cmd.resp.task_hint")
        # 自定义指令优先级最高，会盖住任务层：必须明说，否则用户会以为套用没生效
        if self.conv_mgr.custom_prompt:
            reply += "\n" + t("cmd.resp.task_overridden_by_prompt")
        return reply, None

    def _preset(self, args: list) -> Response:
        """``/preset``：轻量 / 均衡 / 高质量三档生成参数（方向六）。

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
        self.temp_manual = False   # 回到"预设说了算"，这样切模式时温度也会跟着模式走
        self.llm_engine.clear_cache()
        return (t("cmd.resp.preset_applied", name=preset["label"], temp=preset["temperature"],
                  top_p=preset["top_p"], penalty=preset["repeat_penalty"],
                  max_tokens=preset["max_tokens"])
                + "\n" + str(preset["note"])), None

    def _compact(self, args: list) -> Response:
        """``/compact [auto|off] [focus on <重点>]``：手动压缩上下文（修复四；任务九扩展）。

        与自动压缩的区别：自动压缩只在达到 ``compact.trigger_ratio``（默认 70%）时
        触发，用户想在还有余量时提前"腾地方"（例如准备上传一个大文件）就得手动触发。

        任务九（优化已知问题-2）新增 ``focus on <文字>``：把用户指定的重点
        （如"合同金额"）追加进摘要提示词，让压缩器优先保住那部分信息。
        解析刻意宽松：``focus on X`` / ``focus X`` / ``on X`` 都认，
        且**只在第一个参数是 focus/on 时才当重点**，否则按旧行为压缩。
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
            # 兼容只接受 force 的旧 provider（第三方/测试替身）
            result = self.compact_provider(force)
        except Exception as e:  # noqa: BLE001 - 压缩失败不能让指令崩溃
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
        # 估算当前上下文占用（系统提示 + 历史）
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
        # 修复四（方向七：能力透明化）：把模型能力画像拼进 /status。
        # 这些字段以前散在日志里，用户看不到"模型有多大、窗口实际开了多少、跑得多快"。
        lines.extend(self._capability_lines())
        return "\n".join(lines), None

    def _capability_lines(self) -> List[str]:
        """模型能力段落（参数量 / 量化 / 训练窗口 / 实际窗口 / KV Cache / 速度 / 提示）。"""
        if self.profile_provider is None:
            return []
        try:
            profile = self.profile_provider() or {}
        except Exception:  # noqa: BLE001 - 能力画像失败不应影响 /status
            return []
        if not profile:
            return []
        lines = [
            "",
            t("cmd.status.capability_header"),
            t("cmd.status.parameters", value=profile.get("parameters_text") or "未知",
              source=profile.get("parameters_source") or "未知"),
            t("cmd.status.quantization", value=profile.get("quantization") or "未知",
              source=profile.get("quantization_source") or "未知"),
            t("cmd.status.window_detail",
              train=profile.get("train_context") or "未知",
              usable=profile.get("usable_context") or "未知"),
            t("cmd.status.speed", value=profile.get("speed_text") or "尚未统计"),
            t("cmd.status.hardware",
              ram=(f"{profile['ram_gb']} GB" if profile.get("ram_gb") else "未知"),
              cores=profile.get("cpu_cores") or "未知"),
            t("cmd.status.context_mode", mode=self.config.context_mode),
            t("cmd.status.preset", preset=self.config.preset or "balanced"),
        ]
        if self.task_template:
            lines.append(t("cmd.status.task_template", name=self.task_template))
        # 自定义指令会盖住模式块与任务层，属于"当前实际生效的是什么"的关键信息
        if self.conv_mgr.custom_prompt:
            lines.append(t("cmd.status.custom_prompt", chars=len(self.conv_mgr.custom_prompt)))
        notes = profile.get("warnings") or []
        if notes:
            lines.append(t("cmd.status.capability_notes"))
            lines.extend(f"  • {note}" for note in notes)
        return lines

    @staticmethod
    def _about(_args: list) -> Response:
        """``/about``：版本号、出品方与合规文档（任务书修复五要求"关于页面展示许可证链接"）。

        许可证与隐私文档**随发布包一起分发**（外层根目录的 LICENSE/NOTICE 与 docs\\ 下的
        PRIVACY/EULA/THIRD_PARTY_LICENSES/SBOM），因此这里给出**本机实际路径**，
        用户点不动链接时也能自己在文件管理器里找到；同时给出仓库地址供在线查阅。
        """
        from config import program_dir, program_root

        try:
            data_root = program_root()
            program = program_dir()
        except Exception:  # noqa: BLE001 - 路径解析失败不影响 /about
            data_root = program = ""
        lines = [
            f"✨ {APP_NAME} {APP_VERSION}",
            # /about 是"何处出现完整署名"的规范位置，用**首次完整形式**
            # （中文：熵寂工作室（VoidEntropiaStudio））；其余位置用简称。
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
