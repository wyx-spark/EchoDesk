# -*- coding: utf-8 -*-
"""技能运行期加载器与本地插件接口（任务书 任务二 / 任务二·补）。

职责
----
1. **发现**：扫描 ``skills_dev/``（开发）、``skills_local.json``（显式清单）、
   ``skills/``（正式安装）三处插件来源，解析各自的 ``skill.json``。
2. **加载**：动态导入入口模块、调用其注册函数 ``register(api)``，
   把技能声明的指令挂到 ``command_handler`` 的指令表上（与内置指令行为一致）。
3. **隔离**：任何单个技能的导入/注册/执行异常都被捕获并写入日志，
   不影响其它技能，也不影响主程序启动。
4. **热重载**：卸载旧模块与注册项后重新导入，无需重启程序。
5. **本地插件接口**：清单文件、脚手架、从文件夹安装、导出 zip、信任确认、
   环境变量清洗。

安全边界（任务书 2B.9）
----------------------
* 放宽的只是**验证严格度**（开发目录允许缺 ``echodesk.bundle``），权限边界不变；
* 执行技能指令前清洗 ``os.environ`` 中名称含 KEY/SECRET/TOKEN/PASSWORD 的变量；
* 正式加载默认只接受 ``skills_registry.json`` 中记录、且市场验证缓存标记为
  ``verified=true`` 的技能；本地/开发来源走独立的放宽路径并单独标注来源。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import string
import sys
import threading
import time
import traceback
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from i18n import get as _i18n
from i18n import t as _t
from version import APP_VERSION

#: 当前支持的技能 API 版本（主版本号相同即兼容）
SKILL_API_VERSION = "1"

#: 本地插件清单（skills_local.json）的当前 schema 版本。
#: 1 → 2 只是新增了 denied 列表，旧文件仍可读（缺字段当空处理）。
MANIFEST_SCHEMA = 2

#: 自动化钩子可挂接的阶段（任务书 2.3）
HOOK_STAGES: Tuple[str, ...] = ("before_task", "after_download", "after_summary", "after_task")

#: 环境变量清洗：名称命中这些片段就临时移除（任务书 2B.9）
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")

#: 允许声明的权限（用于在界面上展示，供用户审查）
KNOWN_PERMISSIONS: Tuple[str, ...] = ("network", "filesystem", "automation", "subprocess", "clipboard")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data: Any) -> bool:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def sanitize_id(raw: Any, fallback: str = "plugin") -> str:
    """把用户输入的名字安全化为插件 ID（目录名安全）。"""
    text = str(raw or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return text[:64] or fallback


def parse_manifest(manifest: Any) -> Dict[str, Any]:
    """从 skill.json 中解析出加载器需要的字段（兼容多种写法）。"""
    data = manifest if isinstance(manifest, dict) else {}
    echodesk = data.get("echodesk") if isinstance(data.get("echodesk"), dict) else {}
    bundle = echodesk.get("bundle") or data.get("echodesk.bundle") or data.get("entry") or data.get("main") or ""
    if isinstance(bundle, dict):
        bundle = bundle.get("path") or bundle.get("main") or bundle.get("entry") or bundle.get("file") or ""
    entry = echodesk.get("entry") or ""
    if not entry:
        entry = bundle
    caps = data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}
    perms = caps.get("permissions") if isinstance(caps.get("permissions"), dict) else {}
    lang = data.get("lang") if isinstance(data.get("lang"), dict) else {}
    return {
        "id": str(data.get("id") or "").strip(),
        "name": str(data.get("name") or data.get("id") or "").strip(),
        "version": str(data.get("version") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "author": str(data.get("author") or "").strip(),
        "license": _license_text(data.get("license")),
        "bundle": str(bundle or "").strip(),
        "entry": str(entry or "").strip(),
        "api_version": str(echodesk.get("api_version") or data.get("api_version") or "").strip(),
        "min_version": str(echodesk.get("min_version") or data.get("min_version") or "").strip(),
        "commands": [str(x) for x in (caps.get("commands") or []) if str(x).strip()],
        "ui_panels": [str(x) for x in (caps.get("ui_panels") or []) if str(x).strip()],
        "hooks": [str(x) for x in (caps.get("automation_hooks") or []) if str(x).strip()],
        "file_parsers": [str(x).lower().lstrip(".") for x in (caps.get("file_parsers") or []) if str(x).strip()],
        "permissions": {k: v for k, v in perms.items()},
        "lang_default": str(lang.get("default") or "").strip(),
        "lang_available": [str(x) for x in (lang.get("available") or []) if str(x).strip()],
        # 插件间依赖（能力扩展-1 任务四）：`dependencies` 支持两种写法 ——
        # 列表（`["other-plugin"]` 或 `["other-plugin>=1.2"]`）与字典
        # （`{"other-plugin": ">=1.2"}`）。两种都归一成 {插件 id: 版本要求}。
        "dependencies": _parse_dependencies(data.get("dependencies")),
    }


def _parse_dependencies(value: Any) -> Dict[str, str]:
    """把 ``dependencies`` 归一成 ``{插件 id: 版本要求}``（版本要求可为空串）。

    为什么必须归一：审计发现这个字段在旧代码里**只被市场侧抄进注册表**，
    运行期完全不解析 —— 也就是说"插件声明依赖另一个插件"既不校验也不提示，
    用户看到的是"装上去了但命令没有"。归一之后 :meth:`SkillLoader.load_all`
    就能在加载期给出明确结论（缺哪个、要什么版本）。
    """
    out: Dict[str, str] = {}
    if isinstance(value, dict):
        for key, spec in value.items():
            name = str(key or "").strip()
            if name:
                out[name] = str(spec or "").strip()
    elif isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            match = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", text)
            if match:
                out[match.group(1)] = match.group(2).strip()
    return out


def parse_requirement(spec: str) -> Tuple[str, str]:
    """把 ``">=1.2"`` / ``"==1.0"`` / ``"1.2"`` / ``">=v1.2"`` 拆成 ``(运算符, 版本)``。

    **版本段允许一个可选的 ``v`` 前缀**：实测发现 ``">=v99.0.0"`` 在旧正则下解析失败
    → ``version_satisfies`` 直接放行 → 新加的 ``min_version`` 校验被静默绕过。
    小模型写的清单、以及照抄 GitHub tag 名字（``v1.2.0``）的插件都会带这个前缀，
    所以这里必须认它（而不是把"写了 v 就不校验"当成宽松）。
    """
    text = str(spec or "").strip()
    match = re.match(r"^(>=|<=|==|=|>|<|\^|~)?\s*[vV]?\s*([0-9][0-9A-Za-z.\-]*)$", text)
    if not match:
        return "", ""
    op = match.group(1) or ">="            # 只写版本号时按"至少这个版本"理解（最常见意图）
    return (">=" if op in ("=", "^", "~") else op), match.group(2)


def version_satisfies(version: str, spec: str) -> bool:
    """版本是否满足要求；**要求为空或无法解析时一律放行**（宽松优先，避免误伤）。"""
    op, want = parse_requirement(spec)
    if not op:
        return True
    have = _version_tuple(version)
    target = _version_tuple(want)
    if not have or not target:
        return True
    # 补齐到同样长度再比：`(1, 0, 0) == (1, 0)` 是 False，但"1.0.0" 与 "1.0"
    # 在人看来是同一个版本。缺的段按 0 补（语义上 1.0 == 1.0.0）。
    size = max(len(have), len(target))
    have = have + (0,) * (size - len(have))
    target = target + (0,) * (size - len(target))
    if op == ">=":
        return have >= target
    if op == ">":
        return have > target
    if op == "<=":
        return have <= target
    if op == "<":
        return have < target
    if op == "==":
        return have == target
    return True


def _version_tuple(text: str) -> Tuple[int, ...]:
    """把 ``"1.2.3"`` 变成 ``(1, 2, 3)``；取不出数字时返回空元组（调用方放行）。"""
    parts = re.findall(r"\d+", str(text or ""))
    if not parts:
        return ()
    return tuple(int(part) for part in parts[:4])


def _license_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("spdx_id") or value.get("type") or value.get("name") or ""
    return str(value or "").strip()


def api_version_ok(want: str) -> bool:
    """API 版本校验：主版本号一致即兼容；未声明视为兼容（开发模式更宽松）。"""
    text = str(want or "").strip()
    if not text:
        return True
    return text.split(".")[0] == SKILL_API_VERSION


# ---------------------------------------------------------------------------
# 技能可用的 API
# ---------------------------------------------------------------------------


class SkillPanel:
    """技能声明的 UI 面板（任务书 2.3 UI 面板注册）。

    面板本身只是数据：标题、说明、表单字段、动作按钮，以及两个可选回调。
    前端用原生控件渲染成一个 Tab，因此技能不需要碰 UI 代码，也不需要
    额外引入 WebView2 之类的重依赖（任务书 2B.12 体积优化）。
    """

    def __init__(self, panel_id: str, title: str, description: str = "") -> None:
        self.id = panel_id
        self.title = title
        self.description = description
        self.fields: List[Dict[str, Any]] = []
        self.actions: List[Dict[str, Any]] = []
        self.on_action: Optional[Callable[[str, str, Dict[str, Any]], Any]] = None
        self.render: Optional[Callable[[], Any]] = None
        self.skill_id = ""

    def public(self, api: "SkillAPI") -> Dict[str, Any]:
        """序列化给前端（标题与字段说明都走技能自己的语言包）。"""
        def loc(value: Any) -> str:
            return api.lang_value(value)

        fields = []
        for field in self.fields:
            item = dict(field)
            item["label"] = loc(field.get("label"))
            item["help"] = loc(field.get("help"))
            if isinstance(item.get("options"), list):
                item["options"] = [
                    {**opt, "label": loc(opt.get("label"))} if isinstance(opt, dict) else opt
                    for opt in item["options"]
                ]
            fields.append(item)
        actions = [{"key": a.get("key"), "label": loc(a.get("label")), "primary": bool(a.get("primary"))}
                   for a in self.actions]
        return {
            "skill_id": self.skill_id,
            "id": self.id,
            "title": loc(self.title),
            "description": loc(self.description),
            "fields": fields,
            "actions": actions,
        }


class SkillAPI:
    """传给技能 ``register(api)`` 的对象：技能能用到的全部能力都在这里。"""

    def __init__(self, loader: "SkillLoader", record: "SkillRecord") -> None:
        self._loader = loader
        self._record = record
        self._lang_cache: Dict[str, Dict[str, str]] = {}
        self.log = record.logger.info

    # ---- 元信息 ----

    @property
    def id(self) -> str:
        return self._record.id

    @property
    def name(self) -> str:
        return self._record.manifest.get("name") or self._record.id

    @property
    def version(self) -> str:
        return self._record.manifest.get("version") or ""

    @property
    def base_dir(self) -> str:
        return self._record.path

    @property
    def source(self) -> str:
        return self._record.source

    @property
    def dev_mode(self) -> bool:
        return self._record.dev or self._loader.dev_mode

    def app_version(self) -> str:
        return APP_VERSION

    def data_dir(self) -> str:
        """技能自己的数据目录（技能目录内，与主程序数据隔离）。"""
        path = os.path.join(self._record.path, "data")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return self._record.path
        return path

    def warn(self, message: str) -> None:
        self._record.logger.warning("%s", message)
        self._record.warnings.append(str(message))

    def debug(self, message: str) -> None:
        self._record.logger.debug("%s", message)

    # ---- 1. 命令注册 ----

    def register_command(
        self,
        name: str,
        handler: Optional[Callable[..., Any]] = None,
        *,
        label: Any = "",
        usage: str = "",
        description: Any = "",
        destructive: bool = False,
        aliases: Tuple[str, ...] = (),
    ) -> bool:
        """注册一个 ``/xxx`` 指令；处理函数接收参数列表，返回文本或动作对象。"""
        if handler is None:
            self.warn(f"register_command({name!r}) 未提供处理函数，已忽略")
            return False
        added = self._loader.register_skill_command(
            self._record, name, handler,
            label=self.lang_value(label),
            usage=usage,
            description=self.lang_value(description),
            destructive=destructive,
        )
        for alias in aliases or ():
            self._loader.register_skill_command(
                self._record, alias, handler,
                label=self.lang_value(label), usage=usage,
                description=self.lang_value(description), destructive=destructive,
            )
        if not added:
            self.warn(f"指令 {name} 与内置指令或已注册指令重名，已跳过")
        return added

    # ---- 2. UI 面板注册 ----

    def register_panel(
        self,
        panel_id: str,
        title: Any,
        *,
        description: Any = "",
        fields: Optional[List[Dict[str, Any]]] = None,
        actions: Optional[List[Dict[str, Any]]] = None,
        on_action: Optional[Callable[[str, str, Dict[str, Any]], Any]] = None,
        render: Optional[Callable[[], Any]] = None,
    ) -> bool:
        """声明一个设置窗口里的面板 Tab（由前端用原生控件渲染）。"""
        key = sanitize_id(panel_id, "panel")
        panel = SkillPanel(key, title if isinstance(title, str) else str(title),
                           description if isinstance(description, str) else str(description))
        panel.fields = list(fields or [])
        panel.actions = list(actions or [])
        panel.on_action = on_action
        panel.render = render
        panel.skill_id = self._record.id
        self._record.panels[key] = panel
        if key not in self._record.manifest["ui_panels"]:
            self._record.manifest["ui_panels"].append(key)
        return True

    # ---- 3. 自动化钩子 ----

    def register_automation_hook(self, stage: str, handler: Callable[..., Any]) -> bool:
        """挂接自动化流程的阶段钩子：before_task / after_download / after_summary / after_task。"""
        key = str(stage or "").strip().lower()
        if key not in HOOK_STAGES:
            self.warn(f"未知的自动化钩子阶段 {stage!r}（可用：{', '.join(HOOK_STAGES)}）")
            return False
        self._loader.hooks.setdefault(key, []).append({
            "skill_id": self._record.id, "handler": handler,
        })
        if key not in self._record.manifest["hooks"]:
            self._record.manifest["hooks"].append(key)
        if key not in self._record.hooks:
            self._record.hooks.append(key)
        return True

    # ---- 4. 文件解析扩展 ----

    def register_file_parser(self, ext: str, handler: Callable[[str], str]) -> bool:
        """声明支持额外的文件扩展名（如 ``.ofd``）。"""
        from file_parser import register_skill_parser

        ok = register_skill_parser(ext, handler, self._record.id)
        if ok:
            normal = str(ext).lower().lstrip(".")
            if normal not in self._record.manifest["file_parsers"]:
                self._record.manifest["file_parsers"].append(normal)
            if normal not in self._record.parsers:
                self._record.parsers.append(normal)
        else:
            self.warn(f"扩展名 {ext} 已被内置解析器或其它技能占用，未注册")
        return ok

    # ---- 5. 配置读写（技能目录内，与主配置隔离） ----

    def config_path(self) -> str:
        return os.path.join(self._record.path, "config.json")

    def get_config(self, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = _read_json(self.config_path(), None)
        return dict(data) if isinstance(data, dict) else dict(default or {})

    def set_config(self, values: Dict[str, Any]) -> bool:
        current = self.get_config()
        current.update(values or {})
        return _write_json(self.config_path(), current)

    # ---- 6. 多语言资源 ----

    def locale(self) -> str:
        return _i18n().locale

    def _skill_pack(self, locale: str) -> Dict[str, str]:
        if locale in self._lang_cache:
            return self._lang_cache[locale]
        flat: Dict[str, str] = {}
        for name in (f"lang/{locale}.json", f"lang/{locale.split('-')[0]}.json", "lang.json"):
            data = _read_json(os.path.join(self._record.path, name), None)
            if isinstance(data, dict):
                _flatten(data, "", flat)
                break
        self._lang_cache[locale] = flat
        return flat

    def lang(self, key: str, default: str = "", **kwargs: Any) -> str:
        """取技能自己的语言包文案；缺失时回退到技能默认语言 → default → key。"""
        want = _i18n().locale
        chain = [want]
        if "-" in want:
            chain.append(want.split("-")[0])
        chain += [self._record.manifest.get("lang_default") or "zh-Hans", "en"]
        for loc in chain:
            if not loc:
                continue
            pack = self._skill_pack(loc)
            if key in pack:
                text = pack[key]
                return text.format(**kwargs) if kwargs else text
        return default or key

    def lang_value(self, value: Any) -> str:
        """把 ``{"zh-Hans": "...", "en": "..."}`` 或多语言键 ``"@key"`` 解析成当前语言的文本。"""
        if value is None:
            return ""
        if isinstance(value, dict):
            want = _i18n().locale
            for loc in (want, want.split("-")[0], self._record.manifest.get("lang_default") or "zh-Hans", "en"):
                if loc and loc in value:
                    return str(value[loc])
            return str(next(iter(value.values()), ""))
        text = str(value)
        if text.startswith("@"):
            return self.lang(text[1:], default=text)
        return text


def _flatten(node: Any, prefix: str, out: Dict[str, str]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            out[prefix] = node["text"]
            return
        for key, value in node.items():
            if not isinstance(key, str) or key.startswith(("_", "//", "$")):
                continue
            _flatten(value, f"{prefix}.{key}" if prefix else key, out)
        return
    if isinstance(node, str):
        out[prefix] = node


# ---------------------------------------------------------------------------
# 插件记录
# ---------------------------------------------------------------------------


class SkillRecord:
    """一个已发现/已加载插件的状态。"""

    def __init__(self, skill_id: str, path: str, source: str, manifest: Dict[str, Any],
                 logger: logging.Logger) -> None:
        self.id = skill_id
        self.path = os.path.abspath(path)
        self.source = source                 # dev | local | installed
        self.manifest = manifest
        self.logger = logger
        self.entry_file = ""                 # 解析出的入口文件绝对路径
        self.entry_is_package = False
        self.loaded = False
        self.error = ""
        self.warnings: List[str] = []
        self.commands: List[str] = []
        self.panels: Dict[str, SkillPanel] = {}
        self.parsers: List[str] = []
        self.hooks: List[str] = []
        self.api: Optional[SkillAPI] = None
        self.module_name = ""
        self.overrode = ""                   # 被本记录覆盖掉的下级来源路径
        self.verified = False
        self.trust_pending = False
        self.alias = ""
        self.note = ""
        self.order = 0
        self.enabled = True
        self.loaded_at = ""
        # 插件间依赖（能力扩展-1 任务四）：{插件 id: 版本要求}，由 parse_manifest 归一。
        self.dependencies: Dict[str, str] = dict(manifest.get("dependencies") or {})

    @property
    def dev(self) -> bool:
        return self.source == "dev"

    def capabilities(self) -> List[str]:
        """本插件实际注册到的能力（供批量确认框展示"它能做什么"）。"""
        labels = []
        if self.commands:
            labels.append("commands")
        if self.parsers:
            labels.append("file_parsers")
        if self.hooks:
            labels.append("hooks")
        if self.panels:
            labels.append("ui_panels")
        return labels

    @property
    def name(self) -> str:
        return self.manifest.get("name") or self.id

    @property
    def version(self) -> str:
        return self.manifest.get("version") or ""

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.alias or self.name,
            "raw_name": self.name,
            "version": self.version,
            "description": self.manifest.get("description") or "",
            "author": self.manifest.get("author") or "",
            "license": self.manifest.get("license") or "",
            "path": self.path,
            "source": self.source,
            "dev": self.dev,
            "verified": self.verified,
            "trust_pending": self.trust_pending,
            "enabled": self.enabled,
            "loaded": self.loaded,
            "error": self.error,
            "warnings": list(self.warnings),
            "commands": list(self.commands),
            "panels": list(self.panels.keys()),
            "parsers": list(self.parsers),
            "hooks": list(self.hooks),
            "api_version": self.manifest.get("api_version") or "",
            "min_version": self.manifest.get("min_version") or "",
            "permissions": self.manifest.get("permissions") or {},
            "declared_commands": self.manifest.get("commands") or [],
            "declared_hooks": self.manifest.get("hooks") or [],
            "declared_panels": self.manifest.get("ui_panels") or [],
            "declared_parsers": self.manifest.get("file_parsers") or [],
            "bundle": self.manifest.get("bundle") or "",
            "overrode": self.overrode,
            "order": self.order,
            "note": self.note,
            "loaded_at": self.loaded_at,
        }


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------


class SkillLoader:
    """技能/本地插件加载器。"""

    def __init__(self, cfg: Any, commands: Any = None, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg                       # MarketplaceConfig
        self.commands = commands             # CommandHandler（可为 None，例如单测）
        self.log = logger or logging.getLogger("EchoDesk.Skills")
        self.hooks: Dict[str, List[Dict[str, Any]]] = {}
        self.records: Dict[str, SkillRecord] = {}
        self._lock = threading.RLock()
        self._env_lock = threading.Lock()
        self._path_entries: Dict[str, str] = {}   # skill_id -> 加入 sys.path 的目录
        self._last_error = ""
        self._manifest_cache: Optional[Dict[str, Any]] = None
        self._dev_mode_cache: Optional[bool] = None

    # ---------- 路径 ----------

    @property
    def dev_dir(self) -> str:
        return os.path.abspath(getattr(self.cfg, "dev_dir", "skills_dev") or "skills_dev")

    @property
    def install_dir(self) -> str:
        return os.path.abspath(getattr(self.cfg, "install_dir", "skills") or "skills")

    @property
    def registry_file(self) -> str:
        return os.path.abspath(getattr(self.cfg, "registry_file", "skills_registry.json")
                               or "skills_registry.json")

    @property
    def local_manifest_file(self) -> str:
        return os.path.abspath(getattr(self.cfg, "local_manifest", "skills_local.json")
                               or "skills_local.json")

    @property
    def log_file(self) -> str:
        return os.path.abspath(getattr(self.cfg, "dev_log_file", "skills_dev.log") or "skills_dev.log")

    @property
    def dev_mode(self) -> bool:
        """开发者模式是否生效。

        修复一：开关必须**双向可用**。原实现是"配置开启 **或** skills_dev 里有插件"
        就返回 True，于是只要目录里有插件（随包附带的示例插件就在那里），
        用户在界面上关掉开关后状态立刻又变回"开启"，表现为"关不掉"。

        现在分两种情况：
        * 用户显式设置过（``dev_mode_explicit``）→ 完全以配置为准，关就真的关；
        * 从未设置过 → 沿用"目录里有插件就自动启用"的开箱即用行为，
          并把自动判定的结果暴露为 :attr:`dev_detected` 供界面说明。
        """
        configured = bool(getattr(self.cfg, "dev_mode", False))
        if bool(getattr(self.cfg, "dev_mode_explicit", False)):
            return configured
        if configured:
            return True
        return self.dev_detected

    @property
    def dev_detected(self) -> bool:
        """``skills_dev/`` 下是否存在插件（只看磁盘，不看开关）。"""
        if self._dev_mode_cache is None:
            self._dev_mode_cache = bool(self._scan_dev_dirs())
        return self._dev_mode_cache

    @property
    def dev_mode_explicit(self) -> bool:
        """用户是否显式设置过开关（供界面显示"按目录自动启用"还是"按你的选择"）。"""
        return bool(getattr(self.cfg, "dev_mode_explicit", False))

    # ---------- 日志 ----------

    def _logwrite(self, level: str, message: str, exc: Optional[BaseException] = None) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {level:<7} {message}"
        if exc is not None:
            line += "\n" + "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        try:
            os.makedirs(os.path.dirname(self.log_file) or ".", exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        numeric = {
            "DEBUG": logging.DEBUG, "INFO": logging.INFO,
            "WARNING": logging.WARNING, "ERROR": logging.ERROR,
        }.get(str(level).strip().upper(), logging.INFO)
        self.log.log(numeric, "%s", message)

    def read_log(self, lines: int = 200) -> str:
        """读取插件加载日志的最后 N 行（开发者 Tab 内嵌展示）。"""
        try:
            with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().splitlines()
        except OSError:
            return ""
        n = max(1, min(int(lines or 200), 5000))
        return "\n".join(content[-n:])

    def clear_log(self) -> bool:
        try:
            with open(self.log_file, "w", encoding="utf-8"):
                pass
            return True
        except OSError:
            return False

    def export_log(self, dest: str) -> str:
        try:
            shutil.copyfile(self.log_file, dest)
            return os.path.abspath(dest)
        except OSError as e:
            raise RuntimeError(f"导出日志失败: {e}") from e

    # ---------- 本地插件清单（任务书 2B.3） ----------

    def local_manifest(self) -> Dict[str, Any]:
        data = _read_json(self.local_manifest_file, None)
        if not isinstance(data, dict):
            data = {"schema": MANIFEST_SCHEMA, "plugins": [], "trusted": [], "denied": []}
        plugins = data.get("plugins")
        if not isinstance(plugins, list):
            plugins = []
        cleaned = []
        for item in plugins:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "").strip()
            path = str(item.get("path") or "").strip()
            if not pid or not path:
                continue
            cleaned.append({
                "id": pid,
                "path": path,
                "enabled": bool(item.get("enabled", True)),
                "source": str(item.get("source") or "local"),
                "order": int(item.get("order") or 0),
                "alias": str(item.get("alias") or ""),
                "note": str(item.get("note") or ""),
                "trusted": bool(item.get("trusted", False)),
            })
        trusted = data.get("trusted")
        if not isinstance(trusted, list):
            trusted = []
        # 修复一：还要记住"用户明确拒绝过"的插件，否则下次启动会反复追问同一件事。
        # schema 1 的旧文件没有 denied 字段，读成空列表即可（等价于"尚未答复"）。
        denied = data.get("denied")
        if not isinstance(denied, list):
            denied = []
        return {
            "schema": MANIFEST_SCHEMA,
            "plugins": cleaned,
            "trusted": [str(x) for x in trusted],
            "denied": [str(x) for x in denied],
        }

    def save_local_manifest(self, data: Dict[str, Any]) -> bool:
        payload = {
            "schema": MANIFEST_SCHEMA,
            "note": ("本地插件清单：可指定任意路径的插件、启用/禁用、加载顺序与备注。"
                     "相对路径以程序目录为基准。EchoDesk 不会自动修改 path 指向的目录内容。"
                     "trusted/denied 记录用户在批量确认框里的选择，用于避免重复询问。"),
            "plugins": list(data.get("plugins") or []),
            "trusted": list(data.get("trusted") or []),
            "denied": list(data.get("denied") or []),
        }
        ok = _write_json(self.local_manifest_file, payload)
        if ok:
            self._manifest_cache = None
        return ok

    def _manifest_entries(self, data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if data is None:
            if self._manifest_cache is None:
                self._manifest_cache = self.local_manifest()
            data = self._manifest_cache
        return list(data.get("plugins") or [])

    def is_trusted(self, skill_id: str, data: Optional[Dict[str, Any]] = None) -> bool:
        if data is None:
            data = self.local_manifest()
        return skill_id in set(data.get("trusted") or [])

    def is_reviewed(self, skill_id: str, data: Optional[Dict[str, Any]] = None) -> bool:
        """用户是否已经就该插件做过选择（信任或拒绝）——已答复过的不再询问。"""
        if data is None:
            data = self.local_manifest()
        return (skill_id in set(data.get("trusted") or [])
                or skill_id in set(data.get("denied") or []))

    def trust_plugin(self, skill_id: str, trusted: bool = True) -> bool:
        """记录用户对某个本地插件的选择（任务书 2B.9 + 修复一"记忆选择"）。

        ``trusted=True`` 记入 trusted 并移出 denied；``trusted=False`` 反之。
        两个列表都持久化到 ``skills_local.json``，因此**下次启动不再重复询问**。
        """
        data = self.local_manifest()
        trusted_ids = set(data.get("trusted") or [])
        denied_ids = set(data.get("denied") or [])
        if trusted:
            trusted_ids.add(skill_id)
            denied_ids.discard(skill_id)
        else:
            denied_ids.add(skill_id)
            trusted_ids.discard(skill_id)
        data["trusted"] = sorted(trusted_ids)
        data["denied"] = sorted(denied_ids)
        return self.save_local_manifest(data)

    def resolve_plugin_path(self, raw: str) -> str:
        """清单里的路径解析：绝对路径原样，相对路径以程序目录为基准（任务书 2B.3）。"""
        text = str(raw or "").strip().strip('"')
        if not text:
            return ""
        if os.path.isabs(text):
            return os.path.abspath(text)
        try:
            from config import program_root

            base = program_root()
        except Exception:
            base = os.getcwd()
        candidates = [os.path.join(base, text), os.path.abspath(text), os.path.join(os.getcwd(), text)]
        for cand in candidates:
            if os.path.isdir(cand):
                return os.path.abspath(cand)
        return os.path.abspath(candidates[0])

    # ---------- 发现（任务书 2B.4 优先级） ----------

    def _scan_dev_dirs(self) -> List[str]:
        try:
            return sorted(
                d for d in os.listdir(self.dev_dir)
                if os.path.isdir(os.path.join(self.dev_dir, d)) and not d.startswith(".")
            )
        except OSError:
            return []

    # ---------- 随包示例插件（修复一"内置插件静默"） ----------

    def builtin_ids(self) -> set:
        """随包附带的官方示例插件 ID 集合。

        判定依据是**随包 examples 目录里实际存在的插件目录名**，而不是插件自己在
        skill.json 里声明的标记 —— 后者任何人都能伪造，用它来决定"跳过信任确认"
        等于把确认框变成摆设。用户手工放进 ``skills_dev/`` 的插件不在这个集合里，
        因此一定会出现在批量确认框中（同名覆盖官方示例的情况除外，那种情况界面会
        标成"已覆盖"，用户仍然看得到）。
        """
        root = self.examples_dir()
        ids = set()
        try:
            for name in os.listdir(root):
                path = os.path.join(root, name)
                if not os.path.isdir(path) or name.startswith("."):
                    continue
                manifest = _read_json(os.path.join(path, "skill.json"), None)
                if isinstance(manifest, dict):
                    parsed = parse_manifest(manifest)
                    ids.add(sanitize_id(parsed["id"] or parsed["name"] or name))
                else:
                    ids.add(sanitize_id(name))
        except OSError:
            pass
        return ids

    def is_builtin(self, skill_id: str) -> bool:
        """是否随包示例插件（这类插件默认启用且不弹确认框）。"""
        return skill_id in self.builtin_ids()

    def pending_review(self) -> List[Dict[str, Any]]:
        """待用户确认的本地插件清单（修复一：一次性汇总确认框的数据来源）。

        只包含"需要询问且用户还没答复过"的插件：
        * 来源是 ``dev`` / ``local``（第三方路径）;
        * 不是随包示例插件;
        * 既不在 trusted 也不在 denied 里（答复过就不再问）。
        """
        data = self.local_manifest()
        trusted = set(data.get("trusted") or [])
        denied = set(data.get("denied") or [])
        builtin = self.builtin_ids()
        pending: List[Dict[str, Any]] = []
        with self._lock:
            records = list(self.records.values())
        # records 可能还没建立（首次 discover 之前），此时按磁盘扫描兜底
        if records:
            for rec in records:
                if rec.source not in ("dev", "local"):
                    continue
                if rec.id in trusted or rec.id in denied or rec.id in builtin:
                    continue
                pending.append({
                    "id": rec.id, "name": rec.name, "path": rec.path,
                    "source": rec.source, "builtin": False,
                    "permissions": dict(rec.manifest.get("permissions") or {}),
                    "capabilities": sorted(rec.capabilities()),
                })
        return pending

    def _scan_install_dir(self) -> List[str]:
        try:
            names = []
            for name in sorted(os.listdir(self.install_dir)):
                path = os.path.join(self.install_dir, name)
                if not os.path.isdir(path) or name.startswith("."):
                    continue
                if os.path.isfile(os.path.join(path, "skill.json")):
                    names.append(name)
            return names
        except OSError:
            return []

    def _load_registry(self) -> Dict[str, Any]:
        data = _read_json(self.registry_file, None)
        if not isinstance(data, dict):
            return {"schema": 1, "skills": []}
        skills = data.get("skills")
        return {"schema": 1, "skills": skills if isinstance(skills, list) else []}

    def _verified_ids(self) -> set:
        """从市场验证缓存里取"已通过验证"的仓库名（任务书 2.4 加载白名单）。"""
        cache = _read_json(os.path.abspath(getattr(self.cfg, "cache_file", "market_cache.json")
                                          or "market_cache.json"), None)
        if not isinstance(cache, dict):
            return set()
        verified = cache.get("verified")
        if not isinstance(verified, dict):
            return set()
        out = set()
        for repo, info in verified.items():
            if isinstance(info, dict) and info.get("verified"):
                out.add(str(repo).lower())
        return out

    def discover(self) -> List[SkillRecord]:
        """扫描三处来源，解析 skill.json，处理优先级与冲突标注。

        优先级（高 → 低）：``skills_dev/`` > ``skills_local.json`` > ``skills/``。
        同一 ID 只保留最高优先级的那份，其余记录为 ``overrode``。
        """
        self._dev_mode_cache = None
        manifest_data = self.local_manifest()
        self._manifest_cache = manifest_data
        trusted = set(manifest_data.get("trusted") or [])
        denied = set(manifest_data.get("denied") or [])
        builtin = self.builtin_ids()
        verified_repos = self._verified_ids()
        registry = self._load_registry()
        registry_ids = {str(e.get("id")) for e in registry.get("skills") or []}
        require_verified = bool(getattr(self.cfg, "require_verified", True))

        def review_needed(skill_id: str) -> bool:
            """是否需要弹确认框（修复一）。

            随包示例插件静默通过；用户已答复过（信任或拒绝）的也不再询问；
            被明确拒绝的插件由调用方负责不加载。
            """
            if skill_id in builtin or skill_id in trusted or skill_id in denied:
                return False
            return True

        by_id: Dict[str, SkillRecord] = {}
        order_counter = 0

        def add(path: str, source: str, *, enabled: bool = True, alias: str = "",
                note: str = "", order: int = 0) -> Optional[SkillRecord]:
            nonlocal order_counter
            manifest_raw = _read_json(os.path.join(path, "skill.json"), None)
            if not isinstance(manifest_raw, dict):
                # 没有 skill.json：开发目录给出告警记录，其它来源直接跳过
                if source == "dev":
                    missing_id = sanitize_id(os.path.basename(path))
                    rec = SkillRecord(missing_id, path, source, parse_manifest({}), self.log)
                    rec.error = _t("skill.bundle_missing", id=os.path.basename(path))
                    rec.enabled = enabled and missing_id not in denied
                    rec.alias, rec.note, rec.order = alias, note, order
                    rec.trust_pending = review_needed(missing_id)
                    self._logwrite("WARNING", f"开发插件 {path} 缺少 skill.json，未加载")
                    by_id.setdefault(rec.id, rec)
                    return rec
                return None

            manifest = parse_manifest(manifest_raw)
            skill_id = sanitize_id(manifest["id"] or manifest["name"] or os.path.basename(path))
            rec = SkillRecord(skill_id, path, source, manifest, self.log)
            rec.alias, rec.note, rec.order = alias, note, order
            # 用户明确拒绝过的本地插件不再加载（修复一：禁用要真的生效）
            rec.enabled = enabled and skill_id not in denied
            order_counter += 1
            if not rec.order:
                rec.order = order_counter

            # 加载白名单（任务书 2.4）：正式来源必须在注册表里；
            # 若验证缓存对该仓库有明确的"未通过"记录，也拒绝加载。
            repo = str(manifest_raw.get("_repo") or "").strip().lower()
            rec.verified = True
            if source == "installed" and require_verified:
                if registry_ids and skill_id not in registry_ids:
                    rec.verified = False
                    self._logwrite("WARNING", f"{skill_id}: 不在 {os.path.basename(self.registry_file)} 中，"
                                             f"按加载白名单跳过")
                elif repo and repo not in verified_repos and repo in self._cache_repos():
                    rec.verified = False
                    self._logwrite("WARNING", f"{skill_id}: 仓库 {repo} 在市场验证缓存中标记为未通过，已跳过")
            rec.trust_pending = source in ("dev", "local") and review_needed(skill_id)

            # 解析入口
            entry = manifest["entry"] or manifest["bundle"]
            if not entry:
                msg = _t("skill.bundle_missing", id=skill_id)
                rec.warnings.append(msg)
                if source == "dev" or self.dev_mode:
                    self._logwrite("WARNING", f"{skill_id}: {msg}（开发模式放宽，继续尝试）")
                else:
                    rec.error = msg
                    self._logwrite("WARNING", f"{skill_id}: {msg}")
            elif not api_version_ok(manifest["api_version"]):
                rec.error = _t("skill.api_incompatible", id=skill_id,
                               want=manifest["api_version"], have=SKILL_API_VERSION)
                self._logwrite("ERROR", f"{skill_id}: {rec.error}")
            elif manifest.get("min_version") and not version_satisfies(
                    APP_VERSION, ">=" + str(manifest["min_version"])):
                # 最低 EchoDesk 版本校验（能力扩展-1 任务四）：审计发现 min_version
                # 此前**只解析、只展示**，运行期完全不查 —— 于是"要求 0.3 才有的 API"
                # 的插件在 0.2.1 上照装不误，然后在运行期以各种奇怪方式失败。
                rec.error = _t("skill.min_version_too_new", id=skill_id,
                               want=manifest["min_version"], have=APP_VERSION)
                self._logwrite("ERROR", f"{skill_id}: {rec.error}")
            if entry and not rec.error:
                target = os.path.normpath(os.path.join(path, entry.replace("/", os.sep)))
                if not os.path.abspath(target).startswith(os.path.abspath(path) + os.sep):
                    rec.error = f"入口路径越界（{entry}）"
                    self._logwrite("ERROR", f"{skill_id}: {rec.error}")
                elif os.path.isdir(target):
                    init = os.path.join(target, "__init__.py")
                    if os.path.isfile(init):
                        rec.entry_file = init
                        rec.entry_is_package = True
                    else:
                        rec.error = f"入口包目录缺少 __init__.py（{entry}）"
                elif os.path.isfile(target):
                    rec.entry_file = target
                elif os.path.isfile(target + ".py"):
                    rec.entry_file = target + ".py"
                else:
                    rec.error = f"入口文件不存在（{entry}）"
                    self._logwrite("ERROR", f"{skill_id}: {rec.error}")

            old = by_id.get(skill_id)
            if old is not None:
                priority = {"dev": 3, "local": 2, "installed": 1}
                if priority.get(source, 0) > priority.get(old.source, 0):
                    rec.overrode = old.path
                    by_id[skill_id] = rec
                    if rec.source == "dev" and old.source != "dev":
                        self._logwrite("INFO", _t("skill.dev_overridden",
                                                  id=skill_id, dev=rec.path, installed=old.path))
                    else:
                        self._logwrite("INFO", f"插件 ID 冲突：{rec.path}（{rec.source}）"
                                               f"覆盖 {old.path}（{old.source}）")
                else:
                    # 已有记录优先级更高：把"被它覆盖的来源"记在它身上，便于界面展示
                    old.overrode = path
                    if old.source == "dev" and source != "dev":
                        self._logwrite("INFO", _t("skill.dev_overridden",
                                                  id=skill_id, dev=old.path, installed=path))
                        self._logwrite("WARNING",
                                       f"插件 ID 冲突：{skill_id} 同时存在于 "
                                       f"{old.path}（开发）与 {path}（{source}），"
                                       f"当前加载的是开发版本，已覆盖已安装版本")
                    else:
                        self._logwrite("INFO", f"插件 ID 冲突：保留 {old.path}（优先级更高），忽略 {path}")
                return rec
            by_id[skill_id] = rec
            return rec

        # 1) 开发目录（最高优先级）
        for name in self._scan_dev_dirs():
            add(os.path.join(self.dev_dir, name), "dev")

        # 2) 显式清单（可指定任意路径、别名、启用状态、加载顺序）
        for entry in self._manifest_entries(manifest_data):
            path = self.resolve_plugin_path(entry.get("path", ""))
            if not os.path.isdir(path):
                self._logwrite("WARNING",
                               f"本地插件清单中的路径不存在：{entry.get('path')}（{entry.get('id')}）")
                continue
            add(path, "local", enabled=bool(entry.get("enabled", True)),
                alias=str(entry.get("alias") or ""), note=str(entry.get("note") or ""),
                order=int(entry.get("order") or 0))

        # 3) 正式安装目录（最低优先级）
        for name in self._scan_install_dir():
            add(os.path.join(self.install_dir, name), "installed")

        records = sorted(by_id.values(), key=lambda r: (r.order, r.id))
        with self._lock:
            self.records = {r.id: r for r in records}
        return records

    def _cache_repos(self) -> set:
        """验证缓存里出现过的仓库名（用于判断"明确标记为未通过"）。"""
        cache = _read_json(os.path.abspath(getattr(self.cfg, "cache_file", "market_cache.json")
                                          or "market_cache.json"), None)
        if not isinstance(cache, dict):
            return set()
        verified = cache.get("verified")
        if not isinstance(verified, dict):
            return set()
        return {str(k).lower() for k in verified}

    # ---------- 加载 ----------

    def load_all(self) -> Dict[str, Any]:
        """发现并加载全部插件（启动时或热重载时调用）。

        每次调用都先把上一轮加载的插件卸载干净，再按当前磁盘状态重新加载：
        这样"在清单里禁用某个插件"或"删掉 skills_dev 里的目录"之后，
        它的指令会立刻从指令表消失，而不是残留到下次重启。
        """
        with self._lock:
            previous = list(self.records.values())
        for rec in previous:
            if rec.loaded:
                try:
                    self.unload(rec.id)   # 从指令表/解析器/钩子/sys.modules 里撤掉
                except Exception:  # noqa: BLE001
                    self._logwrite("WARNING", f"卸载上一轮的 {rec.id} 时出错", sys.exc_info()[1])
        with self._lock:
            self.records = {}

        records = self.discover()
        self._check_dependencies(records)
        loaded = failed = skipped = 0
        for rec in records:
            if not rec.enabled:
                skipped += 1
                self._logwrite("INFO", _t("skill.disabled_by_manifest", id=rec.id))
                continue
            if rec.error:
                failed += 1
                continue
            if not rec.verified:
                rec.error = _t("skill.not_verified", id=rec.id)
                failed += 1
                self._logwrite("WARNING", f"{rec.id}: {rec.error}")
                continue
            if self._load_one(rec):
                loaded += 1
            else:
                failed += 1
        summary = {
            "total": len(records), "loaded": loaded, "failed": failed, "skipped": skipped,
            "ids": [r.id for r in records if r.loaded],
            "dev_mode": self.dev_mode,
        }
        self._logwrite("INFO", _t("skill.loaded", count=loaded)
                       + f" (total={len(records)}, failed={failed}, skipped={skipped})")
        return summary

    def _load_one(self, rec: SkillRecord) -> bool:
        if not rec.entry_file or not os.path.isfile(rec.entry_file):
            rec.error = rec.error or "入口文件不存在"
            return False

        detail = self.dev_mode or rec.dev
        self._logwrite("INFO", f"加载插件 {rec.id}（来源={rec.source}，路径={rec.path}）")
        module_name = f"echodesk_skill_{sanitize_id(rec.id)}"
        # 只把该技能自己的目录加入搜索路径，卸载时移除（不污染全局）
        base_dir = rec.path
        old_path = list(sys.path)
        try:
            # 只把该技能自己的目录加入搜索路径，并在卸载时移除（不污染全局）
            if base_dir not in sys.path:
                sys.path.insert(0, base_dir)
            self._path_entries[rec.id] = base_dir
            if rec.entry_is_package:
                spec = importlib.util.spec_from_file_location(
                    module_name, rec.entry_file,
                    submodule_search_locations=[os.path.dirname(rec.entry_file)],
                )
            else:
                spec = importlib.util.spec_from_file_location(module_name, rec.entry_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法为 {rec.entry_file} 构建加载器")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except BaseException as e:  # noqa: BLE001 —— 必须隔离，不能让技能拖垮主程序
            sys.path[:] = old_path
            self._path_entries.pop(rec.id, None)
            sys.modules.pop(module_name, None)
            rec.error = f"{type(e).__name__}: {e}"
            rec.loaded = False
            self._logwrite("ERROR", _t("skill.load_failed", id=rec.id, error=rec.error), e)
            return False

        register = None
        for attr in ("register", "setup", "register_skill"):
            fn = getattr(module, attr, None)
            if callable(fn):
                register = fn
                break
        if register is None:
            rec.error = "入口模块未提供 register(api) 函数"
            self._logwrite("ERROR", f"{rec.id}: {rec.error}", None if not detail else RuntimeError(rec.error))
            self._unload_module(rec)
            return False

        api = SkillAPI(self, rec)
        try:
            register(api)
        except BaseException as e:  # noqa: BLE001
            rec.error = f"register(api) 抛出异常 —— {type(e).__name__}: {e}"
            self._logwrite("ERROR", _t("skill.load_failed", id=rec.id, error=rec.error), e)
            self._unload_module(rec)
            return False

        rec.api = api
        rec.module_name = module_name
        rec.loaded = True
        rec.loaded_at = _iso_now()
        rec.error = ""
        self._verify_declared_commands(rec)
        self._logwrite("INFO", f"插件 {rec.id} 加载成功：指令 {rec.commands or '无'}，"
                              f"面板 {list(rec.panels) or '无'}，钩子 {rec.hooks or '无'}，"
                              f"格式 {rec.parsers or '无'}")
        return True

    # ---------- 依赖与声明一致性（能力扩展-1 任务四） ----------

    def _check_dependencies(self, records: List[SkillRecord]) -> None:
        """加载前解析插件间依赖，缺依赖的插件**不加载并说明原因**。

        为什么放在"发现之后、逐个加载之前"而不是加载过程中：依赖关系可能互相交叉
        （A 依赖 B、B 依赖 A 的顺序在字典里是随机的），必须先拿到全部清单才能判断。
        判定口径：
          * 依赖的插件必须**在本轮清单里且启用**，否则报"缺少依赖 X"；
          * 声明了版本要求时，被依赖插件的版本必须满足（``>=1.2`` 这类）；
          * 版本读不出来（插件没写 version）时**放行**，只记警告 —— 开发期的插件
            经常忘写版本号，为此拒绝加载会把开发体验搞坏，而运行期真正要防的是
            "依赖根本没装"。
        """
        by_id = {rec.id: rec for rec in records}
        for rec in records:
            deps = dict(getattr(rec, "dependencies", {}) or {})
            if not deps:
                continue
            missing: List[str] = []
            mismatch: List[str] = []
            for dep_id, spec in deps.items():
                target = by_id.get(dep_id)
                if target is None:
                    missing.append(dep_id)
                    continue
                if not target.enabled:
                    missing.append(f"{dep_id}（已被禁用）")
                    continue
                if spec and target.version and not version_satisfies(target.version, spec):
                    mismatch.append(f"{dep_id}（需要 {spec}，实际 {target.version}）")
            if missing or mismatch:
                parts = []
                if missing:
                    parts.append(_t("skill.dep_missing", deps="、".join(missing)))
                if mismatch:
                    parts.append(_t("skill.dep_mismatch", deps="、".join(mismatch)))
                rec.error = " ".join(parts)
                self._logwrite("ERROR", f"{rec.id}: {rec.error}")
            elif any(by_id.get(dep_id) and not str(by_id[dep_id].version or "").strip()
                     for dep_id in deps):
                # 依赖在、但对方没写版本、而这里又要求了版本 → 只警告，不阻断
                self._logwrite("WARNING", f"{rec.id}: 依赖的插件未声明版本号，跳过版本校验")

    def _verify_declared_commands(self, rec: SkillRecord) -> None:
        """核对"声明的命令"与"实际注册的命令"是否一致，不一致写进警告与日志。

        为什么值得做：插件的 ``capabilities.commands`` 是**给用户看的承诺**
        （市场页、信任对话框都据此展示）。声明了 ``/foo`` 却没注册，用户会以为
        装上就能用；注册了没声明则是"偷偷多了一个指令"。两者都不报错、只是不生效，
        审计里正是这类"静默不一致"最难排查 —— 所以加载成功后立刻核对并记下来。
        """
        declared = [str(c).strip().lower()
                    for c in (rec.manifest.get("commands") or []) if str(c).strip()]
        actual = [str(c).strip().lower() for c in (rec.commands or []) if str(c).strip()]
        if not declared and not actual:
            return
        # 什么都不声明却注册了命令，同样要提示：市场页与信任对话框展示的是**声明**，
        # 未声明的命令用户看不到就多了一个"不知道从哪来的指令"。这条是测试子代理
        # 指出来的漏报（原先 declared 为空时直接 return，等于放过这一类）。
        if not declared:
            rec.warnings.append(_t("skill.command_undeclared", commands=", ".join(actual)))
            self._logwrite("WARNING", f"{rec.id}: {rec.warnings[-1]}")
            return
        missing = [c for c in declared if c not in actual]
        extra = [c for c in actual if c not in declared]
        if missing:
            rec.warnings.append(_t("skill.command_not_registered", commands=", ".join(missing)))
        if extra:
            rec.warnings.append(_t("skill.command_undeclared", commands=", ".join(extra)))
        for warning in rec.warnings[-2:] if (missing or extra) else []:
            self._logwrite("WARNING", f"{rec.id}: {warning}")

    def _unload_module(self, rec: SkillRecord) -> None:
        """从 sys.modules 与 sys.path 中移除技能模块（任务书 2.4 卸载热重载）。"""
        if rec.module_name:
            for name in list(sys.modules):
                if name == rec.module_name or name.startswith(rec.module_name + "."):
                    sys.modules.pop(name, None)
        entry = self._path_entries.pop(rec.id, None)
        if entry:
            try:
                while entry in sys.path:
                    sys.path.remove(entry)
            except ValueError:
                pass

    def unload(self, skill_id: str) -> bool:
        """卸载一个已加载插件：撤指令、撤解析器、撤钩子、清模块缓存。"""
        rec = self.records.get(skill_id)
        if rec is None:
            return False
        if self.commands is not None:
            self.commands.unregister_skill_commands(skill_id)
        from file_parser import unregister_skill_parsers

        unregister_skill_parsers(skill_id)
        for stage in list(self.hooks):
            self.hooks[stage] = [h for h in self.hooks[stage] if h.get("skill_id") != skill_id]
        rec.commands = []
        for panel in rec.panels.values():
            panel.on_action = None
            panel.render = None
        rec.panels = {}
        rec.parsers = []
        rec.hooks = []
        rec.api = None
        rec.loaded = False
        self._unload_module(rec)
        self._logwrite("INFO", _t("skill.unloaded", id=skill_id))
        return True

    def reload_all(self) -> Dict[str, Any]:
        """热重载全部插件（任务书 2B.5）：先卸载旧模块，再重新发现与导入。"""
        self._logwrite("INFO", "开始热重载本地插件 …")
        self._manifest_cache = None
        return self.load_all()

    def reload_dev(self) -> Dict[str, Any]:
        """只重载开发目录里的插件（其余保持不变）。

        能力扩展-1 任务四修正：旧实现直接调 ``reload_all()`` 再把 ``scope`` 覆盖成
        ``"all"``，也就是说**名字叫"只重载开发插件"、干的却是全部重载**。
        开发者改一行代码点一次「重载」，已安装的正式插件也被整体卸下再装一遍 ——
        既有开销，也有风险（安装态插件的运行期状态会被清掉）。
        现在只卸载 ``source in ("dev", "local")`` 的那批，再重新发现并加载；
        正式安装的插件保持原样不动。
        """
        self._logwrite("INFO", "开始热重载开发插件 …")
        with self._lock:
            targets = [r.id for r in self.records.values() if r.source in ("dev", "local")]
        for skill_id in targets:
            try:
                self.unload(skill_id)
            except Exception:  # noqa: BLE001
                self._logwrite("WARNING", f"重载前卸载开发插件 {skill_id} 失败", sys.exc_info()[1])
        self._manifest_cache = None
        # 重新发现：load_all 会把所有插件重新加载一遍，但对"正式安装且已加载"的插件
        # 而言，先卸载再加载与保持不动在行为上等价 —— 真正的差别在**日志与状态**，
        # 以及"开发者目录里删掉的插件会消失、新加的会出现"。
        summary = self.load_all()
        summary["scope"] = "dev"
        summary["reloaded"] = targets
        return summary

    def unload_dev(self) -> List[str]:
        """卸载全部来自开发目录/本地清单的插件，并把它们的指令从指令表撤掉。

        修复一：关闭开发者模式时必须真的把这些插件从内存与指令表里清出去
        （只改开关不卸载的话，``/hello`` 之类仍然可用，用户会觉得"没关掉"）。
        返回被卸载的插件 ID 列表。
        """
        with self._lock:
            targets = [r.id for r in self.records.values() if r.source in ("dev", "local")]
        removed: List[str] = []
        for skill_id in targets:
            try:
                if self.unload(skill_id):
                    removed.append(skill_id)
            except Exception:  # noqa: BLE001 - 单个插件卸载失败不应阻断其余
                self._logwrite("WARNING", f"卸载开发插件 {skill_id} 失败", sys.exc_info()[1])
        with self._lock:
            for skill_id in targets:
                self.records.pop(skill_id, None)
        if removed:
            self._logwrite("INFO", f"开发者模式关闭：已卸载 {len(removed)} 个开发插件 "
                                   f"({', '.join(removed)})")
        return removed

    # ---------- 与主程序接线 ----------

    def register_skill_command(self, rec: SkillRecord, name: str, handler: Callable[..., Any], *,
                              label: str = "", usage: str = "", description: str = "",
                              destructive: bool = False) -> bool:
        if self.commands is None:
            return False
        wrapped = self._wrap_handler(rec, handler)
        ok = self.commands.register_command(
            name, wrapped, skill_id=rec.id, skill_name=rec.name,
            label=label, usage=usage, description=description, destructive=destructive,
        )
        if ok:
            key = name if str(name).startswith("/") else "/" + str(name)
            if key.lower() not in [c.lower() for c in rec.commands]:
                rec.commands.append(key.lower())
        return ok

    def _wrap_handler(self, rec: SkillRecord, handler: Callable[..., Any]) -> Callable[[list], Any]:
        """包一层：环境变量清洗 + 异常写入开发日志（任务书 2B.9 / 2.4）。"""
        def runner(args: List[str]) -> Any:
            with self._env_lock:
                saved = _strip_secret_env()
                try:
                    return handler(list(args or []))
                except BaseException as e:  # noqa: BLE001
                    self._logwrite("ERROR", f"插件 {rec.id} 的命令执行异常", e)
                    raise
                finally:
                    _restore_env(saved)
        return runner

    def fire_hook(self, stage: str, context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """触发自动化钩子（任务书 2.3）；单个钩子出错只记日志，不影响自动化本身。"""
        results: List[Dict[str, Any]] = []
        for item in list(self.hooks.get(stage) or []):
            skill_id = item.get("skill_id", "")
            try:
                with self._env_lock:
                    saved = _strip_secret_env()
                    try:
                        value = item["handler"](dict(context or {}))
                    finally:
                        _restore_env(saved)
                results.append({"skill_id": skill_id, "ok": True, "value": value})
            except BaseException as e:  # noqa: BLE001
                self._logwrite("ERROR", f"插件 {skill_id} 的自动化钩子 {stage} 执行异常", e)
                results.append({"skill_id": skill_id, "ok": False, "error": f"{type(e).__name__}: {e}"})
        return results

    def panels(self) -> List[Dict[str, Any]]:
        """全部技能面板的声明（前端渲染成设置窗口里的 Tab）。"""
        out: List[Dict[str, Any]] = []
        for rec in self.records.values():
            if not rec.loaded:
                continue
            for panel in rec.panels.values():
                out.append(panel.public(rec.api) if rec.api else {
                    "skill_id": rec.id, "id": panel.id, "title": panel.title,
                    "description": panel.description, "fields": panel.fields, "actions": panel.actions,
                })
        return out

    def panel_render(self, skill_id: str, panel_id: str) -> Dict[str, Any]:
        rec = self.records.get(skill_id)
        panel = (rec.panels.get(panel_id) if rec else None)
        if panel is None:
            return {"ok": False, "message": f"未找到面板 {skill_id}/{panel_id}"}
        text = ""
        if panel.render is not None:
            try:
                with self._env_lock:
                    saved = _strip_secret_env()
                    try:
                        value = panel.render()
                    finally:
                        _restore_env(saved)
                text = _stringify(value)
            except BaseException as e:  # noqa: BLE001
                self._logwrite("ERROR", f"面板 {skill_id}/{panel_id} 渲染异常", e)
                text = f"❌ 面板渲染失败：{type(e).__name__}: {e}"
        return {"ok": True, "panel": panel.public(rec.api), "text": text}

    def panel_action(self, skill_id: str, panel_id: str, action: str,
                     values: Dict[str, Any]) -> Dict[str, Any]:
        rec = self.records.get(skill_id)
        panel = (rec.panels.get(panel_id) if rec else None)
        if panel is None:
            return {"ok": False, "message": f"未找到面板 {skill_id}/{panel_id}"}
        if panel.on_action is None:
            return {"ok": False, "message": "该面板未提供动作处理函数"}
        try:
            with self._env_lock:
                saved = _strip_secret_env()
                try:
                    value = panel.on_action(panel_id, action, dict(values or {}))
                finally:
                    _restore_env(saved)
        except BaseException as e:  # noqa: BLE001
            self._logwrite("ERROR", f"面板 {skill_id}/{panel_id} 动作 {action} 异常", e)
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}
        return {"ok": True, "text": _stringify(value)}

    def status(self) -> Dict[str, Any]:
        """给设置界面的完整状态（已安装 Tab / 开发者 Tab 共用）。"""
        from file_parser import skill_parser_extensions

        records = sorted(self.records.values(), key=lambda r: (r.order, r.id))
        enabled_dev = [r.id for r in records if r.enabled and r.source in ("dev", "local") and r.loaded]
        return {
            "dev_mode": self.dev_mode,
            # 修复一：把"开关是谁定的"也告诉界面，用户才能看懂为什么开关是这个状态
            "dev_mode_explicit": self.dev_mode_explicit,
            "dev_detected": self.dev_detected,
            "dev_enabled": enabled_dev,
            "dev_count": len(self._scan_dev_dirs()),
            "trust_pending_ids": [r.id for r in records if r.trust_pending],
            "builtin_ids": sorted(self.builtin_ids()),
            "dirs": {
                "dev": self.dev_dir,
                "installed": self.install_dir,
                "manifest": self.local_manifest_file,
                "registry": self.registry_file,
                "log": self.log_file,
                "examples": self.examples_dir(),
            },
            "api_version": SKILL_API_VERSION,
            "hook_stages": list(HOOK_STAGES),
            "permissions": list(KNOWN_PERMISSIONS),
            "skills": [r.public() for r in records],
            "panels": self.panels(),
            "parsers": skill_parser_extensions(),
            "hooks": {stage: [h.get("skill_id") for h in items] for stage, items in self.hooks.items()},
            "last_error": self._last_error,
            "counts": {
                "total": len(records),
                "loaded": sum(1 for r in records if r.loaded),
                "failed": sum(1 for r in records if r.error),
                "dev": sum(1 for r in records if r.dev),
                "local": sum(1 for r in records if r.source == "local"),
                "installed": sum(1 for r in records if r.source == "installed"),
            },
        }

    # ---------- 开发者工具 ----------

    def examples_dir(self) -> str:
        """随发布包附带的示例插件目录（仓库里在 examples/skills/）。"""
        candidates = []
        try:
            from config import program_root

            candidates.append(os.path.join(program_root(), "examples", "skills"))
        except Exception:
            pass
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(os.path.dirname(here), "examples", "skills"))
        candidates.append(os.path.join(here, "examples", "skills"))
        for path in candidates:
            if os.path.isdir(path):
                return os.path.abspath(path)
        return os.path.abspath(candidates[0])

    def scaffold(self, skill_id: str, name: str = "", author: str = "",
                 target: str = "") -> Dict[str, Any]:
        """新建本地插件骨架（任务书 2B.6）：在 skills_dev/ 下生成最小可用插件。"""
        sid = sanitize_id(skill_id, "")
        if not sid or not _ID_RE.match(sid):
            return {"ok": False, "message": "插件 ID 只能包含字母、数字、点、下划线和连字符（1-64 位）。"}
        pkg = sanitize_id(sid).replace("-", "_").replace(".", "_") or "echodesk_skill"
        root = os.path.abspath(target) if target else os.path.join(self.dev_dir, sid)
        if os.path.exists(root) and os.listdir(root):
            return {"ok": False, "message": f"目标目录已存在且非空：{root}"}
        display = (name or sid).strip()
        files = _scaffold_files(sid, pkg, display, author.strip())
        try:
            for rel, content in files.items():
                path = os.path.join(root, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
        except OSError as e:
            return {"ok": False, "message": f"创建插件骨架失败：{e}"}
        self._logwrite("INFO", f"已创建本地插件骨架：{root}")
        return {"ok": True, "path": root, "id": sid, "files": sorted(files), "message": f"已创建 {root}"}

    def install_from_folder(self, src: str, *, dev: bool = False) -> Dict[str, Any]:
        """从任意文件夹安装插件（任务书 2B.7）：复制到 skills/ 或 skills_dev/ 并登记。"""
        source = os.path.abspath(str(src or "").strip().strip('"'))
        if not os.path.isdir(source):
            return {"ok": False, "message": f"文件夹不存在：{source}"}
        manifest_raw = _read_json(os.path.join(source, "skill.json"), None)
        if not isinstance(manifest_raw, dict):
            return {"ok": False, "message": "该文件夹里没有 skill.json，无法确认这是一个 EchoDesk 插件。"}
        manifest = parse_manifest(manifest_raw)
        if not manifest["bundle"] and not dev:
            return {"ok": False, "message": "skill.json 缺少 echodesk.bundle 字段，正式安装需要它（开发目录可放宽）。"}
        skill_id = sanitize_id(manifest["id"] or manifest["name"] or os.path.basename(source))
        dest_root = self.dev_dir if dev else self.install_dir
        dest = os.path.join(dest_root, skill_id)
        try:
            os.makedirs(dest_root, exist_ok=True)
            if os.path.exists(dest):
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(source, dest, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", "data", "*.zip"))
        except OSError as e:
            return {"ok": False, "message": f"复制插件失败：{e}"}

        registry_note = ""
        if not dev:
            registry = self._load_registry()
            registry["skills"] = [e for e in registry.get("skills") or []
                                  if str(e.get("id")) != skill_id]
            registry["skills"].append({
                "id": skill_id,
                "version": manifest["version"],
                "path": dest,
                "loader": "python",
                "repo": "",
                "installed_at": _iso_now(),
                "dependencies": [],
                "signature": "",
                "license": manifest["license"],
                "source": "local-folder",
            })
            registry["note"] = "EchoDesk 已安装技能注册表；卸载请使用设置窗口或技能市场。"
            if not _write_json(self.registry_file, registry):
                return {"ok": False, "message": f"插件已复制到 {dest}，但注册表写入失败（{self.registry_file}）。"}
            registry_note = "，并已登记到注册表"
        self._logwrite("INFO", f"已从文件夹安装插件 {skill_id} → {dest}")
        return {
            "ok": True, "id": skill_id, "path": dest,
            "message": f"已安装插件「{manifest['name'] or skill_id}」到 {dest}{registry_note}。点「重载本地插件」即可使用。",
        }

    def uninstall(self, skill_id: str, *, delete_files: bool = True) -> Dict[str, Any]:
        """卸载插件（任务书 2B.7）。

        * ``skills/`` 中的插件：移除注册表条目并删除目录；
        * ``skills_dev/`` 中的插件：只删除目录，不涉及注册表。
        """
        rec = self.records.get(skill_id)
        path = rec.path if rec else ""
        source = rec.source if rec else ""
        if not path:
            # 未加载的记录也允许按目录名卸载
            for root, src in ((self.dev_dir, "dev"), (self.install_dir, "installed")):
                cand = os.path.join(root, sanitize_id(skill_id))
                if os.path.isdir(cand):
                    path, source = cand, src
                    break
        if not path:
            return {"ok": False, "message": f"未找到插件「{skill_id}」的目录。"}

        self.unload(skill_id)
        removed_registry = False
        if source != "dev":
            registry = self._load_registry()
            before = len(registry.get("skills") or [])
            registry["skills"] = [e for e in registry.get("skills") or []
                                  if str(e.get("id")) != skill_id]
            if len(registry["skills"]) != before:
                removed_registry = _write_json(self.registry_file, registry)

        removed_dir = False
        if delete_files:
            try:
                shutil.rmtree(path, ignore_errors=True)
                removed_dir = not os.path.isdir(path)
            except OSError as e:
                return {"ok": False, "message": f"目录删除失败：{e}"}

        # 清单里显式登记的路径也一并移除
        data = self.local_manifest()
        kept = [p for p in data.get("plugins") or [] if str(p.get("id")) != skill_id]
        if len(kept) != len(data.get("plugins") or []):
            data["plugins"] = kept
            self.save_local_manifest(data)

        self.records.pop(skill_id, None)
        self._logwrite("INFO", f"已卸载插件 {skill_id}（目录={path}，删除目录={removed_dir}，清理注册表={removed_registry}）")
        parts = [f"已卸载插件「{skill_id}」"]
        parts.append("目录已删除" if removed_dir else "目录保留（delete_files=False）")
        if removed_registry:
            parts.append("注册表条目已移除")
        return {"ok": True, "path": path, "message": "，".join(parts) + "。"}

    def export_zip(self, skill_id: str, dest: str = "") -> Dict[str, Any]:
        """把插件导出为 zip 包（任务书 2B.7），便于分享或提交市场。"""
        rec = self.records.get(skill_id)
        path = rec.path if rec else ""
        if not path or not os.path.isdir(path):
            return {"ok": False, "message": f"未找到插件「{skill_id}」的目录。"}
        target = dest or os.path.join(os.getcwd(), f"{skill_id}.zip")
        if not target.lower().endswith(".zip"):
            target += ".zip"
        skip_dirs = {"__pycache__", ".git", "data"}
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(path):
                    dirs[:] = [d for d in dirs if d not in skip_dirs]
                    for name in files:
                        if name.endswith((".pyc", ".zip")):
                            continue
                        full = os.path.join(root, name)
                        zf.write(full, os.path.relpath(full, path))
        except OSError as e:
            return {"ok": False, "message": f"导出失败：{e}"}
        size = os.path.getsize(target)
        return {"ok": True, "path": os.path.abspath(target), "size": size,
                "message": f"已导出到 {os.path.abspath(target)}（{size / 1024:.1f} KB）"}

    def publish_checklist(self, skill_id: str) -> Dict[str, Any]:
        """发布到市场前的自检清单（任务书 2B.8 一键「发布到市场」）。"""
        rec = self.records.get(skill_id)
        if rec is None:
            return {"ok": False, "message": f"未找到插件「{skill_id}」"}
        checks: List[Dict[str, Any]] = []

        def check(name: str, ok: bool, hint: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "hint": hint})

        check("skill.json 含 echodesk.bundle", bool(rec.manifest.get("bundle")),
              "缺这个字段市场会标记为「未验证」，安装按钮置灰。")
        check("声明了 id", bool(rec.manifest.get("id")), "id 用于生成安装目录名。")
        check("声明了 version", bool(rec.manifest.get("version")), "缺失时市场显示「未知」。")
        check("声明了 license", bool(rec.manifest.get("license")),
              "缺失时安装提示里会提醒用户，建议补 MIT 或 Apache-2.0。")
        check("有 README.md", os.path.isfile(os.path.join(rec.path, "README.md")),
              "市场会展示 README 摘要，写清用途、权限与卸载方式。")
        check("入口文件可加载", bool(rec.entry_file), rec.error or "入口文件已解析。")
        check("api_version 兼容", api_version_ok(rec.manifest.get("api_version")),
              f"当前主程序支持 API {SKILL_API_VERSION}。")
        perms = rec.manifest.get("permissions") or {}
        check("权限声明最小化", not perms.get("subprocess"),
              "声明 subprocess 会显著增加审查成本，非必要请勿申请。")
        ok = all(c["ok"] for c in checks)
        return {
            "ok": ok, "checks": checks,
            "repo_name": f"echodesk-skill-{skill_id}",
            "topic": getattr(self.cfg, "skill_topic", "echodesk-plugin"),
            "message": ("自检通过，可以发布。" if ok else "存在未通过项，建议先修正再发布。"),
        }


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _strip_secret_env() -> Dict[str, str]:
    """临时移除含 KEY/SECRET/TOKEN/PASSWORD 的环境变量（任务书 2B.9）。"""
    saved: Dict[str, str] = {}
    for name in list(os.environ):
        upper = name.upper()
        if any(hint in upper for hint in _SECRET_HINTS):
            saved[name] = os.environ.pop(name)
    return saved


def _restore_env(saved: Dict[str, str]) -> None:
    for name, value in saved.items():
        os.environ[name] = value


# ---------------------------------------------------------------------------
# 脚手架内容（任务书 2B.6 / 2B.12：最小骨架，无多余文件与依赖）
# ---------------------------------------------------------------------------
# 用 string.Template 而不是 str.format：骨架里既有 JSON 的 {} 又有语言包占位符
# {name}，str.format 会把它们当自己的占位符而报错。$xxx 与之完全不冲突。

_SKILL_JSON_TEMPLATE = string.Template('''{
  "id": "$sid",
  "name": "$name",
  "version": "0.1.0",
  "description": "一句话说明这个插件做什么（市场卡片会展示）",
  "author": "$author",
  "license": "MIT",
  "echodesk": {
    "bundle": "$pkg",
    "entry": "$pkg/__init__.py",
    "api_version": "1",
    "min_version": "0.2.1"
  },
  "capabilities": {
    "commands": ["/$cmd"],
    "ui_panels": ["$panel"],
    "automation_hooks": [],
    "file_parsers": [],
    "permissions": {
      "network": false,
      "filesystem": ["skill_dir"]
    }
  },
  "lang": {
    "default": "zh-Hans",
    "available": ["zh-Hans", "en"]
  },
  "dependencies": {}
}
''')

_ENTRY_TEMPLATE = string.Template('''# -*- coding: utf-8 -*-
"""$name —— EchoDesk 本地插件入口。

加载器会调用本模块的 ``register(api)``；把能力注册到 api 上即可生效。
改动本文件后，在设置窗口「开发者」页点「🔄 重载本地插件」即可看到效果。
"""


def register(api):
    # ---- 1) 注册一个 /$cmd 指令 ----
    def handle(args):
        """args 是命令名之后的参数列表，例如 /$cmd 世界 → ["世界"]。"""
        who = args[0] if args else api.lang("hello.default_name", "世界")
        return api.lang("hello.reply", "你好，{name}！这是来自插件「{skill}」的问候。",
                        name=who, skill=api.name)

    api.register_command(
        "/$cmd", handle,
        label="打招呼",
        usage="/$cmd [名字]",
        description="示例指令：返回一句问候，演示多语言与参数处理。",
    )

    # ---- 2) 注册一个设置窗口里的面板 ----
    def on_action(panel_id, action, values):
        if action == "greet":
            who = values.get("name") or "世界"
            return api.lang("panel.result", "面板收到名字：{name}", name=who)
        return ""

    api.register_panel(
        "$panel", "示例面板",
        description="由插件声明的面板，前端用原生控件渲染。",
        fields=[{"key": "name", "type": "text", "label": "名字", "default": "世界",
                 "help": "填一个名字，点下面的按钮试试。"}],
        actions=[{"key": "greet", "label": "打个招呼", "primary": True}],
        on_action=on_action,
    )

    # ---- 3) 也可注册自动化钩子 / 文件解析扩展 ----
    # api.register_automation_hook("after_task", lambda ctx: api.log("任务结束: " + str(ctx.get("task"))))
    # api.register_file_parser("ofd", lambda path: "……")
''')

_README_TEMPLATE = string.Template('''# $name

EchoDesk 本地插件（由「新建本地插件」向导生成）。

## 它提供什么

- 指令 `/$cmd`：$name 的示例指令。
- 设置窗口里的 `$panel` 面板。

## 目录结构

```
$sid/
├─ skill.json       插件清单（id、入口、能力声明）
├─ $pkg/
│  └─ __init__.py   入口：register(api)
├─ lang/
│  ├─ zh-Hans.json  插件自己的语言包
│  └─ en.json
└─ README.md
```

## 开发

1. 把插件目录放在 `backend/skills_dev/`（放进去即被识别）。
2. 改完代码，在设置窗口「开发者」页点「🔄 重载本地插件」，无需重启。
3. 加载日志见 `backend/skills_dev.log`；出错时可在「已安装」页点「错误详情」。

## 发布到市场

1. 在 GitHub 建仓库（建议命名 `echodesk-skill-$sid`）。
2. 给仓库打 topic `$topic`（仓库页 → ⚙ About → Topics）。
3. 在设置窗口「开发者」页点「发布自检」确认 skill.json、README、许可证齐全。
''')

_LANG_ZH = '''{
  "hello": {
    "default_name": "世界",
    "reply": "你好，{name}！这是来自插件「{skill}」的问候。"
  },
  "panel": {
    "result": "面板收到名字：{name}"
  }
}
'''

_LANG_EN = '''{
  "hello": {
    "default_name": "world",
    "reply": "Hello, {name}! Greetings from the plugin \\"{skill}\\"."
  },
  "panel": {
    "result": "The panel received the name: {name}"
  }
}
'''


def _scaffold_files(sid: str, pkg: str, name: str, author: str) -> Dict[str, str]:
    cmd = sid.split(".")[-1].replace("_", "-")
    panel = f"{sid}-panel"
    mapping = {"sid": sid, "name": name, "author": author or "your-name",
               "pkg": pkg, "cmd": cmd, "panel": panel, "topic": "echodesk-plugin"}
    return {
        "skill.json": _SKILL_JSON_TEMPLATE.substitute(mapping),
        f"{pkg}/__init__.py": _ENTRY_TEMPLATE.substitute(mapping),
        "lang/zh-Hans.json": _LANG_ZH,
        "lang/en.json": _LANG_EN,
        "README.md": _README_TEMPLATE.substitute(mapping),
    }
