# -*- coding: utf-8 -*-
"""
[EN]
Skill runtime loader and local plugin interface.

Responsibilities

1. Discovery: scan three plugin sources, ``skills_dev/`` (development),
   ``skills_local.json`` (explicit manifest), and ``skills/`` (installed), and
   parse each ``skill.json``.
2. Loading: dynamically import the entry module, call its ``register(api)``
   function, and attach declared commands to the ``command_handler`` command
   table with the same behaviour as built-in commands.
3. Isolation: exceptions from any single skill's import, registration, or
   execution are captured and logged without affecting other skills or
   application startup.
4. Hot reload: unload the old module and registrations, then re-import without
   restarting the application.
5. Local plugin interface: manifest, scaffolding, folder installation, ZIP
   export, trust confirmation, and environment sanitization.

Security boundaries

* Development directories may omit ``echodesk.bundle``; only validation
  strictness is relaxed, not the permission boundary.
* Before executing a skill command, environment variables whose names contain
  KEY, SECRET, TOKEN, or PASSWORD are removed.
* Normal loading accepts only skills recorded in ``skills_registry.json`` and
  marked ``verified=true`` in the marketplace verification cache. Local and
  development sources use a separate relaxed path and are labelled by source.

[ZH]
技能运行期加载器与本地插件接口。

职责

1. 发现：扫描三个插件来源 ``skills_dev/``（开发）、``skills_local.json``（显式
   清单）与 ``skills/``（已安装），并解析各自的 ``skill.json``。
2. 加载：动态导入入口模块，调用其 ``register(api)`` 函数，并将声明的指令挂到
   ``command_handler`` 指令表，行为与内置指令一致。
3. 隔离：单个技能的导入、注册或执行异常会被捕获并记录，不影响其他技能，也不影响
   应用启动。
4. 热重载：卸载旧模块与注册项后重新导入，无需重启应用。
5. 本地插件接口：清单、脚手架、文件夹安装、ZIP 导出、信任确认与环境变量清洗。

安全边界

* 仅放宽验证严格度：开发目录允许缺少 ``echodesk.bundle``，权限边界不变。
* 执行技能指令前，移除名称包含 KEY、SECRET、TOKEN 或 PASSWORD 的环境变量。
* 正常加载仅接受 ``skills_registry.json`` 中记录、且在市场验证缓存中标记为
  ``verified=true`` 的技能。本地与开发来源使用独立的放宽路径，并按来源标注。
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

from i18n import t as _t
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from i18n import get as _i18n
from i18n import t as _t
from version import APP_VERSION

#: [EN] Supported skill API version. Matching major versions are compatible.
#: [ZH] 当前支持的技能 API 版本。主版本号相同即兼容。
SKILL_API_VERSION = "1"

#: [EN] Current schema version of the local plugin manifest
#: [EN] (``skills_local.json``). Version 2 adds the denied list; version 1 files
#: [EN] remain readable and missing fields are treated as empty.
#: [ZH] 本地插件清单（``skills_local.json``）的当前 schema 版本。版本 2 新增
#: [ZH] denied 列表；版本 1 文件仍可读取，缺失字段按空处理。
MANIFEST_SCHEMA = 2

#: [EN] Automation stages to which hooks may attach.
#: [ZH] 自动化钩子可挂接的阶段。
HOOK_STAGES: Tuple[str, ...] = ("before_task", "after_download", "after_summary", "after_task")

#: [EN] Environment-variable name fragments that cause temporary removal.
#: [ZH] 环境变量名称中命中这些片段时会临时移除该变量。
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")

#: [EN] Permissions that may be declared, shown in the interface for user review.
#: [ZH] 允许声明的权限，在界面中展示以供用户审查。
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
    """
    [EN]
    Sanitize a user-supplied name into a directory-safe plugin id.

    [ZH]
    将用户提供的名称安全化为可安全用作目录名的插件 id。
    """
    text = str(raw or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return text[:64] or fallback


def parse_manifest(manifest: Any) -> Dict[str, Any]:
    """
    [EN]
    Parse the fields required by the loader from ``skill.json``, accepting
    multiple supported forms.

    [ZH]
    从 ``skill.json`` 解析加载器所需字段，兼容多种受支持写法。
    """
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
        # [EN] Inter-plugin dependencies accept two forms: a list such as
        # [EN] ["other-plugin"] or ["other-plugin>=1.2"], and a dictionary such
        # [EN] as {"other-plugin": ">=1.2"}. Both normalize to
        # [EN] {plugin_id: version_requirement}.
        # [ZH] 插件间依赖支持两种形式：列表（如 ["other-plugin"] 或
        # [ZH] ["other-plugin>=1.2"]）与字典（如 {"other-plugin": ">=1.2"}）。
        # [ZH] 两者均归一化为 {插件 id: 版本要求}。
        "dependencies": _parse_dependencies(data.get("dependencies")),
    }


def _parse_dependencies(value: Any) -> Dict[str, str]:
    """
    [EN]
    Normalize ``dependencies`` into ``{plugin_id: version_requirement}``. The
    requirement may be an empty string.

    Without normalization, dependencies are copied into the registry by the
    marketplace but never evaluated at runtime. A plugin declaring a dependency
    would be installed without validation or a warning, and the user would see a
    missing command. Normalization lets :meth:`SkillLoader.load_all` report
    which dependency is missing and which version is required during loading.

    [ZH]
    将 ``dependencies`` 归一化为 ``{插件 id: 版本要求}``。版本要求可为空串。

    若不做归一化，依赖只会由市场侧复制进注册表，运行期不会解析。插件声明的依赖
    因此既不会校验也不会提示，用户只会看到“已安装但没有命令”。归一化后，
    :meth:`SkillLoader.load_all` 可在加载期报告缺失的依赖及其所需版本。
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
    """
    [EN]
    Split ``">=1.2"``, ``"==1.0"``, ``"1.2"``, or ``">=v1.2"`` into
    ``(operator, version)``.

    The version segment accepts an optional ``v`` prefix. Without this, a
    requirement such as ``">=v99.0.0"`` fails to parse, and
    ``version_satisfies`` would silently accept it, bypassing the minimum-version
    check. Manifests written by small models and plugins that copy GitHub tag
    names commonly include this prefix.

    [ZH]
    将 ``">=1.2"``、``"==1.0"``、``"1.2"`` 或 ``">=v1.2"`` 拆分为
    ``(运算符, 版本)``。

    版本段允许可选 ``v`` 前缀。缺少该支持时，``">=v99.0.0"`` 等要求无法解析，
    ``version_satisfies`` 会静默放行，从而绕过最低版本校验。小模型编写的清单以及
    照抄 GitHub tag 名称的插件常带有该前缀。
    """
    text = str(spec or "").strip()
    match = re.match(r"^(>=|<=|==|=|>|<|\^|~)?\s*[vV]?\s*([0-9][0-9A-Za-z.\-]*)$", text)
    if not match:
        return "", ""
    # [EN] A bare version means "at least this version", the most common intent.
    # [ZH] 仅写版本号时按“至少该版本”理解，这是最常见的意图。
    op = match.group(1) or ">="
    return (">=" if op in ("=", "^", "~") else op), match.group(2)


def version_satisfies(version: str, spec: str) -> bool:
    """
    [EN]
    Return whether a version satisfies a requirement. Empty or unparseable
    requirements are accepted to avoid false rejections.

    [ZH]
    返回版本是否满足要求。要求为空或无法解析时一律放行，以避免误拒。
    """
    op, want = parse_requirement(spec)
    if not op:
        return True
    have = _version_tuple(version)
    target = _version_tuple(want)
    if not have or not target:
        return True
    # [EN] Pad both tuples to the same length before comparison: (1, 0, 0) and
    # [EN] (1, 0) differ as tuples but represent the same version.
    # [ZH] 比较前将两个元组补齐到相同长度：(1, 0, 0) 与 (1, 0) 在元组层面不同，
    # [ZH] 但表示同一版本。
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
    """
    [EN]
    Convert ``"1.2.3"`` to ``(1, 2, 3)``. Return an empty tuple when no digits
    are present, which callers treat as acceptance.

    [ZH]
    将 ``"1.2.3"`` 转换为 ``(1, 2, 3)``。无法提取数字时返回空元组，调用方按放行处理。
    """
    parts = re.findall(r"\d+", str(text or ""))
    if not parts:
        return ()
    return tuple(int(part) for part in parts[:4])


def _license_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("spdx_id") or value.get("type") or value.get("name") or ""
    return str(value or "").strip()


def api_version_ok(want: str) -> bool:
    """
    [EN]
    Validate the API version. Matching major versions are compatible, and an
    undeclared version is accepted for development compatibility.

    [ZH]
    校验 API 版本。主版本号一致即兼容；未声明版本时按兼容处理，以支持开发模式。
    """
    text = str(want or "").strip()
    if not text:
        return True
    return text.split(".")[0] == SKILL_API_VERSION


# ---------------------------------------------------------------------------
# [EN] API available to skills / [ZH] 技能可用的 API
# ---------------------------------------------------------------------------


class SkillPanel:
    """
    [EN]
    UI panel declared by a skill.

    A panel is data only: title, description, form fields, action buttons, and
    two optional callbacks. The frontend renders it as a native tab, so skills
    do not touch UI code and do not require a heavy dependency such as WebView2.

    [ZH]
    技能声明的 UI 面板。

    面板仅包含数据：标题、说明、表单字段、动作按钮与两个可选回调。前端将其渲染
    为原生 Tab，因此技能无需接触 UI 代码，也无需引入 WebView2 等重型依赖。
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
        """
        [EN]
        Serialize the panel for the frontend. Titles and field descriptions use
        the skill's own language pack.

        [ZH]
        将面板序列化供前端使用。标题与字段说明使用技能自己的语言包。
        """
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
    """
    [EN]
    Object passed to a skill's ``register(api)`` function. It exposes every
    capability available to the skill.

    [ZH]
    传给技能 ``register(api)`` 函数的对象，提供技能可用的全部能力。
    """

    def __init__(self, loader: "SkillLoader", record: "SkillRecord") -> None:
        self._loader = loader
        self._record = record
        self._lang_cache: Dict[str, Dict[str, str]] = {}
        self.log = record.logger.info

    # ---- [EN] Metadata / [ZH] 元信息 ----

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
        """
        [EN]
        Return the skill's own data directory inside the skill folder, isolated
        from application data.

        [ZH]
        返回技能目录内的独立数据目录，与主程序数据隔离。
        """
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

    # ---- [EN] 1. Command registration / [ZH] 1. 命令注册 ----

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
        """
        [EN]
        Register a ``/xxx`` command. The handler receives the argument list and
        returns text or an action object.

        [ZH]
        注册 ``/xxx`` 指令。处理函数接收参数列表，并返回文本或动作对象。
        """
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

    # ---- [EN] 2. UI panel registration / [ZH] 2. UI 面板注册 ----

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
        """
        [EN]
        Declare a settings-window tab rendered by the frontend with native
        controls.

        [ZH]
        声明设置窗口中的面板 Tab，由前端使用原生控件渲染。
        """
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

    # ---- [EN] 3. Automation hooks / [ZH] 3. 自动化钩子 ----

    def register_automation_hook(self, stage: str, handler: Callable[..., Any]) -> bool:
        """
        [EN]
        Attach a hook to an automation stage: before_task, after_download,
        after_summary, or after_task.

        [ZH]
        将钩子挂接到自动化阶段：before_task、after_download、after_summary 或
        after_task。
        """
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

    # ---- [EN] 4. File-parser extension / [ZH] 4. 文件解析扩展 ----

    def register_file_parser(self, ext: str, handler: Callable[[str], str]) -> bool:
        """
        [EN]
        Declare support for an additional file extension, such as ``.ofd``.

        [ZH]
        声明支持额外的文件扩展名，例如 ``.ofd``。
        """
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

    # ---- [EN] 5. Configuration I/O inside the skill directory / [ZH] 5. 技能目录内的配置读写 ----

    def config_path(self) -> str:
        return os.path.join(self._record.path, "config.json")

    def get_config(self, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = _read_json(self.config_path(), None)
        return dict(data) if isinstance(data, dict) else dict(default or {})

    def set_config(self, values: Dict[str, Any]) -> bool:
        current = self.get_config()
        current.update(values or {})
        return _write_json(self.config_path(), current)

    # ---- [EN] 6. Localization resources / [ZH] 6. 多语言资源 ----

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
        """
        [EN]
        Resolve text from the skill's own language pack. Missing keys fall back
        to the skill's default language, then ``default``, then the key.

        [ZH]
        从技能自己的语言包获取文案。缺失时依次回退到技能默认语言、``default``
        与 key。
        """
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
        """
        [EN]
        Resolve a ``{"zh-Hans": "...", "en": "..."}`` mapping or an ``"@key"``
        localization key to text in the current language.

        [ZH]
        将 ``{"zh-Hans": "...", "en": "..."}`` 映射或 ``"@key"`` 本地化键解析为
        当前语言的文本。
        """
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
# [EN] Plugin record / [ZH] 插件记录
# ---------------------------------------------------------------------------


class SkillRecord:
    """
    [EN]
    State of a discovered or loaded plugin.

    [ZH]
    已发现或已加载插件的状态。
    """

    def __init__(self, skill_id: str, path: str, source: str, manifest: Dict[str, Any],
                 logger: logging.Logger) -> None:
        self.id = skill_id
        self.path = os.path.abspath(path)
        # [EN] Source: dev, local, or installed.
        # [ZH] 来源：dev、local 或 installed。
        self.source = source
        self.manifest = manifest
        self.logger = logger
        # [EN] Absolute path of the resolved entry file.
        # [ZH] 解析后的入口文件绝对路径。
        self.entry_file = ""
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
        # [EN] Path of the lower-priority source overridden by this record.
        # [ZH] 被本记录覆盖的低优先级来源路径。
        self.overrode = ""
        self.verified = False
        self.trust_pending = False
        self.alias = ""
        self.note = ""
        self.order = 0
        self.enabled = True
        self.loaded_at = ""
        # [EN] Inter-plugin dependencies: {plugin_id: version_requirement},
        # [EN] normalized by parse_manifest.
        # [ZH] 插件间依赖：{插件 id: 版本要求}，由 parse_manifest 归一化。
        self.dependencies: Dict[str, str] = dict(manifest.get("dependencies") or {})

    @property
    def dev(self) -> bool:
        return self.source == "dev"

    def capabilities(self) -> List[str]:
        """
        [EN]
        Return the capabilities actually registered by this plugin, used by the
        bulk confirmation dialog to show what it can do.

        [ZH]
        返回本插件实际注册的能力，用于批量确认框展示其可执行的操作。
        """
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
# [EN] Loader / [ZH] 加载器
# ---------------------------------------------------------------------------


class SkillLoader:
    """
    [EN]
    Loader for skills and local plugins.

    [ZH]
    技能与本地插件加载器。
    """

    def __init__(self, cfg: Any, commands: Any = None, logger: Optional[logging.Logger] = None) -> None:
        # [EN] Marketplace configuration.
        # [ZH] 市场配置。
        self.cfg = cfg
        # [EN] Command handler, which may be None in unit tests.
        # [ZH] 指令处理器，在单元测试中可为 None。
        self.commands = commands
        self.log = logger or logging.getLogger("EchoDesk.Skills")
        self.hooks: Dict[str, List[Dict[str, Any]]] = {}
        self.records: Dict[str, SkillRecord] = {}
        self._lock = threading.RLock()
        self._env_lock = threading.Lock()
        # [EN] skill_id to the directory added to sys.path.
        # [ZH] skill_id 到已加入 sys.path 的目录的映射。
        self._path_entries: Dict[str, str] = {}
        self._last_error = ""
        self._manifest_cache: Optional[Dict[str, Any]] = None
        self._dev_mode_cache: Optional[bool] = None

    # ---------- [EN] Paths / [ZH] 路径 ----------

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
        """
        [EN]
        Return whether developer mode is active.

        The switch must work in both directions. The previous implementation
        returned True when the configuration enabled it **or** a plugin existed
        in ``skills_dev/``. Because bundled examples live there, turning the
        switch off immediately produced "enabled" again, so it could not be
        disabled.

        Current behaviour:
        * If the user set the switch explicitly (``dev_mode_explicit``), the
          configuration is authoritative.
        * If the user never set it, plugins in the directory enable developer
          mode automatically, and :attr:`dev_detected` exposes the automatic
          result for display.

        [ZH]
        返回开发者模式是否生效。

        开关必须双向可用。原实现只要配置开启 **或** ``skills_dev/`` 中存在插件
        就返回 True。由于随包示例插件位于该目录，用户关闭开关后状态立即再次变为
        “开启”，表现为无法关闭。

        当前行为：
        * 若用户显式设置过开关（``dev_mode_explicit``），以配置为准。
        * 若从未设置，目录中存在插件时自动启用开发者模式，并通过
          :attr:`dev_detected` 暴露自动判定结果供界面显示。
        """
        configured = bool(getattr(self.cfg, "dev_mode", False))
        if bool(getattr(self.cfg, "dev_mode_explicit", False)):
            return configured
        if configured:
            return True
        return self.dev_detected

    @property
    def dev_detected(self) -> bool:
        """
        [EN]
        Return whether any plugin exists under ``skills_dev/``, based only on
        disk contents and not on the switch.

        [ZH]
        返回 ``skills_dev/`` 下是否存在插件，仅依据磁盘内容，不考虑开关状态。
        """
        if self._dev_mode_cache is None:
            self._dev_mode_cache = bool(self._scan_dev_dirs())
        return self._dev_mode_cache

    @property
    def dev_mode_explicit(self) -> bool:
        """
        [EN]
        Return whether the user explicitly set the switch, so the interface can
        distinguish automatic directory-based enabling from an explicit choice.

        [ZH]
        返回用户是否显式设置过开关，使界面能够区分按目录自动启用与用户明确选择。
        """
        return bool(getattr(self.cfg, "dev_mode_explicit", False))

    # ---------- [EN] Logging / [ZH] 日志 ----------

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
        """
        [EN]
        Read the last N lines of the plugin loader log for display in the
        developer tab.

        [ZH]
        读取插件加载日志的最后 N 行，用于开发者 Tab 展示。
        """
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
            raise RuntimeError(_t("skill.log_export_failed", error=e)) from e

    # ---------- [EN] Local plugin manifest / [ZH] 本地插件清单 ----------

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
        # [EN] The manifest must remember plugins the user explicitly denied;
        # [EN] otherwise the same prompt reappears on every start. Schema 1
        # [EN] files have no denied field, which reads as an empty list and
        # [EN] means "not yet answered".
        # [ZH] 清单必须记住用户明确拒绝的插件；否则每次启动都会重复询问。schema 1
        # [ZH] 文件没有 denied 字段，读取为空列表，表示“尚未答复”。
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
        """
        [EN]
        Return whether the user has already chosen to trust or deny this plugin.
        Answered plugins are not prompted again.

        [ZH]
        返回用户是否已选择信任或拒绝该插件。已答复的插件不再询问。
        """
        if data is None:
            data = self.local_manifest()
        return (skill_id in set(data.get("trusted") or [])
                or skill_id in set(data.get("denied") or []))

    def trust_plugin(self, skill_id: str, trusted: bool = True) -> bool:
        """
        [EN]
        Record the user's choice for a local plugin.

        ``trusted=True`` adds the plugin to trusted and removes it from denied;
        ``trusted=False`` does the reverse. Both lists are persisted in
        ``skills_local.json`` so the prompt is not repeated on the next start.

        [ZH]
        记录用户对本地插件的选择。

        ``trusted=True`` 将插件加入 trusted 并从 denied 移除；``trusted=False``
        则相反。两个列表均持久化到 ``skills_local.json``，下次启动不再重复询问。
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
        """
        [EN]
        Resolve a manifest path: absolute paths are kept, and relative paths
        are resolved against the program directory.

        [ZH]
        解析清单中的路径：绝对路径保持不变，相对路径以程序目录为基准。
        """
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

    # ---------- [EN] Discovery and source priority / [ZH] 发现与来源优先级 ----------

    def _scan_dev_dirs(self) -> List[str]:
        try:
            return sorted(
                d for d in os.listdir(self.dev_dir)
                if os.path.isdir(os.path.join(self.dev_dir, d)) and not d.startswith(".")
            )
        except OSError:
            return []

    # ---------- [EN] Bundled example plugins / [ZH] 随包示例插件 ----------

    def builtin_ids(self) -> set:
        """
        [EN]
        Return the ids of official example plugins bundled with the application.

        The set is derived from directory names that actually exist in the
        bundled examples directory, not from a marker declared in ``skill.json``,
        which any plugin could forge. User plugins placed manually in
        ``skills_dev/`` are not in this set and therefore appear in the bulk
        confirmation dialog. A user plugin that overrides a bundled example is
        marked as overridden and remains visible.

        [ZH]
        返回随应用附带的官方示例插件 id 集合。

        集合依据随包 examples 目录中实际存在的目录名，而不是 ``skill.json`` 中
        声明的标记，因为后者可被任意伪造。用户手工放入 ``skills_dev/`` 的插件不在
        该集合中，因此会出现在批量确认框中。覆盖官方示例的用户插件会被标记为已覆盖，
        并且仍然可见。
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
        """
        [EN]
        Return whether this is a bundled example plugin. These are enabled by
        default and do not trigger a confirmation dialog.

        [ZH]
        返回是否为随包示例插件。此类插件默认启用，且不弹出确认框。
        """
        return skill_id in self.builtin_ids()

    def pending_review(self) -> List[Dict[str, Any]]:
        """
        [EN]
        Return local plugins awaiting user confirmation.

        Only plugins that require a prompt and have not been answered are
        included:
        * source is ``dev`` or ``local``;
        * not a bundled example plugin;
        * present in neither trusted nor denied.

        [ZH]
        返回等待用户确认的本地插件。

        仅包含需要询问且尚未答复的插件：
        * 来源为 ``dev`` 或 ``local``；
        * 不是随包示例插件；
        * 既不在 trusted 也不在 denied 中。
        """
        data = self.local_manifest()
        trusted = set(data.get("trusted") or [])
        denied = set(data.get("denied") or [])
        builtin = self.builtin_ids()
        pending: List[Dict[str, Any]] = []
        with self._lock:
            records = list(self.records.values())
        # [EN] Records may not exist before the first discover call; the
        # [EN] disk scan below provides a fallback.
        # [ZH] 首次 discover 之前记录可能尚不存在；以下磁盘扫描提供回退。
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
        """
        [EN]
        Return repository names marked as verified in the marketplace
        verification cache. These form the loading allowlist.

        [ZH]
        返回市场验证缓存中标记为已通过验证的仓库名，构成加载白名单。
        """
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
        """
        [EN]
        Scan the three sources, parse ``skill.json``, and apply priority and
        conflict annotations.

        Priority, highest to lowest: ``skills_dev/``, ``skills_local.json``,
        then ``skills/``. For each id, only the highest-priority record is kept;
        the others are recorded in ``overrode``.

        [ZH]
        扫描三个来源、解析 ``skill.json``，并处理优先级与冲突标注。

        优先级从高到低为 ``skills_dev/``、``skills_local.json``、``skills/``。
        同一 id 仅保留最高优先级记录，其余记录在 ``overrode`` 中。
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
            """
            [EN]
            Return whether a confirmation dialog is required.

            Bundled example plugins pass silently. Plugins the user already
            trusted or denied are not prompted again. Plugins explicitly denied
            are not loaded by the caller.

            [ZH]
            返回是否需要弹出确认框。

            随包示例插件静默通过。用户已信任或拒绝的插件不再询问。被明确拒绝的插件
            由调用方负责不加载。
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
                # [EN] No skill.json: development sources produce a warning
                # [EN] record; other sources are skipped.
                # [ZH] 没有 skill.json：开发来源生成告警记录，其他来源直接跳过。
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
            # [EN] Local plugins explicitly denied by the user are not loaded.
            # [ZH] 用户明确拒绝的本地插件不会加载。
            rec.enabled = enabled and skill_id not in denied
            order_counter += 1
            if not rec.order:
                rec.order = order_counter

            # [EN] Loading allowlist: installed sources must be present in the
            # [EN] registry, and a repository explicitly marked as unverified in
            # [EN] the verification cache is also rejected.
            # [ZH] 加载白名单：正式来源必须存在于注册表中；验证缓存中明确标记为
            # [ZH] 未通过的仓库同样拒绝加载。
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

            # [EN] Resolve the entry file.
            # [ZH] 解析入口文件。
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
                # [EN] Minimum EchoDesk version check. Previously min_version was
                # [EN] parsed and displayed but never evaluated at runtime, so a
                # [EN] plugin requiring a newer API could install and then fail
                # [EN] unpredictably.
                # [ZH] 最低 EchoDesk 版本校验。此前 min_version 仅被解析与展示，
                # [ZH] 运行期从不检查，导致要求较新 API 的插件仍可安装，并在运行期
                # [ZH] 以不可预期的方式失败。
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
                    # [EN] The existing record has higher priority. Record the
                    # [EN] overridden source on it for display.
                    # [ZH] 已有记录优先级更高。将被覆盖的来源记录在该记录上，便于界面
                    # [ZH] 展示。
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

        # [EN] 1) Development directory, highest priority.
        # [ZH] 1) 开发目录，最高优先级。
        for name in self._scan_dev_dirs():
            add(os.path.join(self.dev_dir, name), "dev")

        # [EN] 2) Explicit manifest: arbitrary paths, aliases, enabled state, and
        # [EN] load order.
        # [ZH] 2) 显式清单：可指定任意路径、别名、启用状态与加载顺序。
        for entry in self._manifest_entries(manifest_data):
            path = self.resolve_plugin_path(entry.get("path", ""))
            if not os.path.isdir(path):
                self._logwrite("WARNING",
                               f"本地插件清单中的路径不存在：{entry.get('path')}（{entry.get('id')}）")
                continue
            add(path, "local", enabled=bool(entry.get("enabled", True)),
                alias=str(entry.get("alias") or ""), note=str(entry.get("note") or ""),
                order=int(entry.get("order") or 0))

        # [EN] 3) Installed directory, lowest priority.
        # [ZH] 3) 正式安装目录，最低优先级。
        for name in self._scan_install_dir():
            add(os.path.join(self.install_dir, name), "installed")

        records = sorted(by_id.values(), key=lambda r: (r.order, r.id))
        with self._lock:
            self.records = {r.id: r for r in records}
        return records

    def _cache_repos(self) -> set:
        """
        [EN]
        Return repository names present in the verification cache, used to
        detect an explicit unverified marker.

        [ZH]
        返回验证缓存中出现过的仓库名，用于判断是否明确标记为未通过。
        """
        cache = _read_json(os.path.abspath(getattr(self.cfg, "cache_file", "market_cache.json")
                                          or "market_cache.json"), None)
        if not isinstance(cache, dict):
            return set()
        verified = cache.get("verified")
        if not isinstance(verified, dict):
            return set()
        return {str(k).lower() for k in verified}

    # ---------- [EN] Loading / [ZH] 加载 ----------

    def load_all(self) -> Dict[str, Any]:
        """
        [EN]
        Discover and load all plugins, called at startup or during hot reload.

        Each call first unloads plugins from the previous round, then reloads
        from the current disk state. Disabling a plugin in the manifest or
        deleting a directory under ``skills_dev/`` therefore removes its commands
        immediately instead of leaving them until the next restart.

        [ZH]
        发现并加载全部插件，在启动或热重载时调用。

        每次调用先卸载上一轮插件，再按当前磁盘状态重新加载。因此在清单中禁用插件或
        删除 ``skills_dev/`` 下的目录后，其指令会立即从指令表移除，而不是残留到
        下次重启。
        """
        with self._lock:
            previous = list(self.records.values())
        for rec in previous:
            if rec.loaded:
                try:
                    # [EN] Remove from commands, parsers, hooks, and sys.modules.
                    # [ZH] 从指令表、解析器、钩子与 sys.modules 中撤掉。
                    self.unload(rec.id)
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
        # [EN] Add only this skill's directory to the search path and remove it
        # [EN] on unload, avoiding global contamination.
        # [ZH] 仅将该技能自己的目录加入搜索路径，并在卸载时移除，避免污染全局。
        base_dir = rec.path
        old_path = list(sys.path)
        try:
            # [EN] Add only this skill's directory and remove it on unload.
            # [ZH] 仅添加该技能自己的目录，并在卸载时移除。
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
                raise ImportError(_t("skill.loader_build_failed", entry=rec.entry_file))
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except BaseException as e:  # noqa: BLE001
            # [EN] Exceptions must be isolated so a skill cannot crash the host.
            # [ZH] 必须隔离异常，避免技能拖垮主程序。
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

    # ---------- [EN] Dependency and declaration consistency / [ZH] 依赖与声明一致性 ----------

    def _check_dependencies(self, records: List[SkillRecord]) -> None:
        """
        [EN]
        Resolve inter-plugin dependencies before loading. Plugins with missing
        dependencies are not loaded, and the reason is reported.

        The check runs after discovery and before loading because dependencies
        may be cyclic; the full record set is required before evaluating them.
        Rules:
        * A dependency must be present in this round's records and enabled;
          otherwise it is reported as missing.
        * When a version requirement is declared, the dependency's version must
          satisfy it.
        * An unreadable version is accepted with a warning. Development plugins
          often omit versions, and rejecting them would harm the development
          workflow. The runtime check primarily guards against missing
          dependencies.

        [ZH]
        加载前解析插件间依赖。缺少依赖的插件不会加载，并报告原因。

        检查安排在发现之后、加载之前，因为依赖可能循环，必须先获得完整记录集合。
        判定规则：
        * 依赖必须存在于本轮记录中且已启用，否则报告为缺失。
        * 声明版本要求时，依赖版本必须满足要求。
        * 无法读取版本时放行并记录警告。开发期插件经常省略版本号，拒绝加载会破坏
          开发体验；运行期主要防止依赖根本未安装。

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
                # [EN] The dependency exists but declares no version while a
                # [EN] version is required; warn without blocking.
                # [ZH] 依赖存在但未声明版本，而此处要求了版本；仅警告，不阻断。
                self._logwrite("WARNING", f"{rec.id}: 依赖的插件未声明版本号，跳过版本校验")

    def _verify_declared_commands(self, rec: SkillRecord) -> None:
        """
        [EN]
        Compare declared commands with commands actually registered, recording
        mismatches in warnings and the log.

        ``capabilities.commands`` is a promise shown to the user in the
        marketplace and trust dialog. A declared command that is not registered
        makes the user expect functionality that does not exist; a registered
        command that was not declared is an unreported extra command. Both fail
        silently, so the check runs immediately after a successful load.

        [ZH]
        比较声明的命令与实际注册的命令，并将不一致写入警告与日志。

        ``capabilities.commands`` 是向用户展示的承诺（市场页与信任对话框据此展示）。
        声明了命令却未注册，会让用户期待不存在的功能；注册了未声明的命令，则是未报告
        的额外指令。两者都静默失败，因此在加载成功后立即检查。

        """
        declared = [str(c).strip().lower()
                    for c in (rec.manifest.get("commands") or []) if str(c).strip()]
        actual = [str(c).strip().lower() for c in (rec.commands or []) if str(c).strip()]
        if not declared and not actual:
            return
        # [EN] Registering commands without declaring any is also reported. The
        # [EN] marketplace and trust dialog display declarations, so an
        # [EN] undeclared command would be invisible to the user.
        # [ZH] 未声明任何命令却注册了命令也会报告。市场页与信任对话框展示的是声明，
        # [ZH] 未声明的命令对用户不可见。
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
        """
        [EN]
        Remove the skill module from ``sys.modules`` and ``sys.path`` during
        unload or hot reload.

        [ZH]
        在卸载或热重载时从 ``sys.modules`` 与 ``sys.path`` 中移除技能模块。
        """
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
        """
        [EN]
        Unload a loaded plugin: remove commands, parsers, hooks, and module
        cache entries.

        [ZH]
        卸载已加载插件：移除指令、解析器、钩子与模块缓存。
        """
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
        """
        [EN]
        Hot-reload all plugins: unload old modules, then rediscover and import
        them again.

        [ZH]
        热重载全部插件：卸载旧模块，然后重新发现并导入。
        """
        self._logwrite("INFO", "开始热重载本地插件 …")
        self._manifest_cache = None
        return self.load_all()

    def reload_dev(self) -> Dict[str, Any]:
        """
        [EN]
        Reload only plugins from development sources; installed plugins remain
        unchanged.

        The previous implementation called ``reload_all()`` and then overwrote
        ``scope`` with ``"all"``, so a method named "reload development plugins"
        actually reloaded everything. It now unloads only ``source`` values of
        ``dev`` or ``local``, then rediscovers and loads them. Installed plugins
        are left untouched.

        [ZH]
        仅重载开发来源的插件，正式安装插件保持不变。

        此前的实现调用 ``reload_all()`` 后将 ``scope`` 覆盖为 ``"all"``，名为
        “只重载开发插件”却执行了全部重载。现在仅卸载 ``source`` 为 ``dev`` 或
        ``local`` 的插件，再重新发现并加载；正式安装插件不受影响。
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
        # [EN] Rediscovery reloads every plugin. For an installed plugin that is
        # [EN] already loaded, unloading and reloading is behaviourally
        # [EN] equivalent to leaving it alone; the difference is in logs and
        # [EN] state, and in removed development plugins disappearing while new
        # [EN] ones appear.
        # [ZH] 重新发现会加载所有插件。对于已加载的正式安装插件，卸载后重新加载与
        # [ZH] 保持不变在行为上等价；差别在于日志与状态，以及开发目录中删除的插件会
        # [ZH] 消失、新增的插件会出现。
        summary = self.load_all()
        summary["scope"] = "dev"
        summary["reloaded"] = targets
        return summary

    def unload_dev(self) -> List[str]:
        """
        [EN]
        Unload all plugins from development directories and the local manifest,
        removing their commands from the command table.

        Disabling developer mode must actually remove these plugins from memory
        and the command table; changing only the switch would leave commands such
        as ``/hello`` usable. Returns the ids of unloaded plugins.

        [ZH]
        卸载来自开发目录与本地清单的全部插件，并从指令表移除其指令。

        关闭开发者模式必须真正将这些插件从内存与指令表移除；仅修改开关会使
        ``/hello`` 等命令仍可使用。返回已卸载插件的 id 列表。
        """
        with self._lock:
            targets = [r.id for r in self.records.values() if r.source in ("dev", "local")]
        removed: List[str] = []
        for skill_id in targets:
            try:
                if self.unload(skill_id):
                    removed.append(skill_id)
            except Exception:  # noqa: BLE001
                # [EN] Failure to unload one plugin must not block the others.
                # [ZH] 单个插件卸载失败不应阻断其余插件。
                self._logwrite("WARNING", f"卸载开发插件 {skill_id} 失败", sys.exc_info()[1])
        with self._lock:
            for skill_id in targets:
                self.records.pop(skill_id, None)
        if removed:
            self._logwrite("INFO", f"开发者模式关闭：已卸载 {len(removed)} 个开发插件 "
                                   f"({', '.join(removed)})")
        return removed

    # ---------- [EN] Integration with the host application / [ZH] 与主程序接线 ----------

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
        """
        [EN]
        Wrap a handler with environment sanitization and exception logging.

        [ZH]
        包装处理函数，加入环境变量清洗与异常日志。
        """
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
        """
        [EN]
        Fire automation hooks. A failing hook is logged and does not affect the
        automation flow.

        [ZH]
        触发自动化钩子。单个钩子失败仅记录日志，不影响自动化流程。
        """
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
        """
        [EN]
        Return all skill panel declarations rendered as settings tabs by the
        frontend.

        [ZH]
        返回全部技能面板声明，由前端渲染为设置窗口中的 Tab。
        """
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
            return {"ok": False, "message": _t("skill.panel_not_found", skill=skill_id,
                                                panel=panel_id)}
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
            return {"ok": False, "message": _t("skill.panel_not_found", skill=skill_id,
                                                panel=panel_id)}
        if panel.on_action is None:
            return {"ok": False, "message": _t("skill.panel_no_action")}
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
        """
        [EN]
        Return the complete status used by the installed and developer settings
        tabs.

        [ZH]
        返回设置界面使用的完整状态，供已安装 Tab 与开发者 Tab 共用。
        """
        from file_parser import skill_parser_extensions

        records = sorted(self.records.values(), key=lambda r: (r.order, r.id))
        enabled_dev = [r.id for r in records if r.enabled and r.source in ("dev", "local") and r.loaded]
        return {
            "dev_mode": self.dev_mode,
            # [EN] Report whether the switch was set explicitly so the interface
            # [EN] can explain its current state.
            # [ZH] 报告开关是否为显式设置，使界面能够解释当前状态。
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

    # ---------- [EN] Developer tools / [ZH] 开发者工具 ----------

    def examples_dir(self) -> str:
        """
        [EN]
        Return the bundled example-plugin directory (``examples/skills/`` in the
        repository).

        [ZH]
        返回随发布包附带的示例插件目录（仓库中的 ``examples/skills/``）。
        """
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
        """
        [EN]
        Create a local plugin scaffold: generate a minimal working plugin under
        ``skills_dev/``.

        [ZH]
        创建本地插件骨架：在 ``skills_dev/`` 下生成最小可用插件。
        """
        sid = sanitize_id(skill_id, "")
        if not sid or not _ID_RE.match(sid):
            return {"ok": False, "message": _t("skill.bad_id")}
        pkg = sanitize_id(sid).replace("-", "_").replace(".", "_") or "echodesk_skill"
        root = os.path.abspath(target) if target else os.path.join(self.dev_dir, sid)
        if os.path.exists(root) and os.listdir(root):
            return {"ok": False, "message": _t("skill.dir_not_empty", path=root)}
        display = (name or sid).strip()
        files = _scaffold_files(sid, pkg, display, author.strip())
        try:
            for rel, content in files.items():
                path = os.path.join(root, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
        except OSError as e:
            return {"ok": False, "message": _t("skill.scaffold_failed", error=e)}
        self._logwrite("INFO", f"已创建本地插件骨架：{root}")
        return {"ok": True, "path": root, "id": sid, "files": sorted(files),
                "message": _t("skill.scaffold_created", path=root)}

    def install_from_folder(self, src: str, *, dev: bool = False) -> Dict[str, Any]:
        """
        [EN]
        Install a plugin from an arbitrary folder: copy it to ``skills/`` or
        ``skills_dev/`` and register it.

        [ZH]
        从任意文件夹安装插件：复制到 ``skills/`` 或 ``skills_dev/`` 并登记。
        """
        source = os.path.abspath(str(src or "").strip().strip('"'))
        if not os.path.isdir(source):
            return {"ok": False, "message": _t("skill.folder_missing", path=source)}
        manifest_raw = _read_json(os.path.join(source, "skill.json"), None)
        if not isinstance(manifest_raw, dict):
            return {"ok": False, "message": _t("skill.no_skill_json")}
        manifest = parse_manifest(manifest_raw)
        if not manifest["bundle"] and not dev:
            return {"ok": False, "message": _t("skill.install_bundle_missing")}
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
            return {"ok": False, "message": _t("skill.copy_failed", error=e)}

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
                return {"ok": False,
                        "message": _t("skill.copied_not_registered", path=dest,
                                      registry=self.registry_file)}
            registry_note = _t("skill.registry_registered")
        self._logwrite("INFO", f"已从文件夹安装插件 {skill_id} → {dest}")
        return {
            "ok": True, "id": skill_id, "path": dest,
            "message": _t("skill.installed", name=manifest["name"] or skill_id, path=dest,
                          registry_note=registry_note),
        }

    def uninstall(self, skill_id: str, *, delete_files: bool = True) -> Dict[str, Any]:
        """
        [EN]
        Uninstall a plugin.

        * Plugins under ``skills/``: remove the registry entry and delete the
          directory.
        * Plugins under ``skills_dev/``: delete the directory only.

        [ZH]
        卸载插件。

        * ``skills/`` 中的插件：移除注册表条目并删除目录。
        * ``skills_dev/`` 中的插件：仅删除目录。
        """
        rec = self.records.get(skill_id)
        path = rec.path if rec else ""
        source = rec.source if rec else ""
        if not path:
            # [EN] Unloaded records may also be uninstalled by directory name.
            # [ZH] 未加载的记录也可按目录名卸载。
            for root, src in ((self.dev_dir, "dev"), (self.install_dir, "installed")):
                cand = os.path.join(root, sanitize_id(skill_id))
                if os.path.isdir(cand):
                    path, source = cand, src
                    break
        if not path:
            return {"ok": False, "message": _t("skill.dir_not_found", id=skill_id)}

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
                return {"ok": False, "message": _t("skill.delete_failed", error=e)}

        # [EN] Remove an explicitly registered manifest path as well.
        # [ZH] 同时移除清单中显式登记的路径。
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
        """
        [EN]
        Export a plugin as a ZIP archive for sharing or marketplace submission.

        [ZH]
        将插件导出为 ZIP 包，便于分享或提交到市场。
        """
        rec = self.records.get(skill_id)
        path = rec.path if rec else ""
        if not path or not os.path.isdir(path):
            return {"ok": False, "message": _t("skill.dir_not_found", id=skill_id)}
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
            return {"ok": False, "message": _t("skill.export_failed", error=e)}
        size = os.path.getsize(target)
        return {"ok": True, "path": os.path.abspath(target), "size": size,
                "message": _t("skill.exported", path=os.path.abspath(target),
                          size=f"{size / 1024:.1f}")}

    def publish_checklist(self, skill_id: str) -> Dict[str, Any]:
        """
        [EN]
        Return the pre-publication checklist for marketplace submission.

        [ZH]
        返回提交市场前的自检清单。
        """
        rec = self.records.get(skill_id)
        if rec is None:
            return {"ok": False, "message": _t("skill.not_found", id=skill_id)}
        checks: List[Dict[str, Any]] = []

        def check(name: str, ok: bool, hint: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "hint": hint})

        check(_t("skill.chk_bundle_name"), bool(rec.manifest.get("bundle")),
              _t("skill.chk_bundle_hint"))
        check(_t("skill.chk_id_name"), bool(rec.manifest.get("id")), _t("skill.chk_id_hint"))
        check(_t("skill.chk_version_name"), bool(rec.manifest.get("version")),
              _t("skill.chk_version_hint"))
        check(_t("skill.chk_license_name"), bool(rec.manifest.get("license")),
              _t("skill.chk_license_hint"))
        check(_t("skill.chk_readme_name"), os.path.isfile(os.path.join(rec.path, "README.md")),
              _t("skill.chk_readme_hint"))
        check(_t("skill.chk_entry_name"), bool(rec.entry_file),
              rec.error or _t("skill.chk_entry_hint_ok"))
        check(_t("skill.chk_api_name"), api_version_ok(rec.manifest.get("api_version")),
              _t("skill.chk_api_hint", version=SKILL_API_VERSION))
        perms = rec.manifest.get("permissions") or {}
        check(_t("skill.chk_perm_name"), not perms.get("subprocess"),
              _t("skill.chk_perm_hint"))
        ok = all(c["ok"] for c in checks)
        return {
            "ok": ok, "checks": checks,
            "repo_name": f"echodesk-skill-{skill_id}",
            "topic": getattr(self.cfg, "skill_topic", "echodesk-plugin"),
            "message": _t("skill.selfcheck_ok") if ok else _t("skill.selfcheck_bad"),
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
    """
    [EN]
    Temporarily remove environment variables whose names contain KEY, SECRET,
    TOKEN, or PASSWORD.

    [ZH]
    临时移除名称包含 KEY、SECRET、TOKEN 或 PASSWORD 的环境变量。
    """
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
# [EN] Scaffold contents: minimal skeleton with no extra files or dependencies
# [ZH] 脚手架内容：最小骨架，无额外文件或依赖
# ---------------------------------------------------------------------------
# [EN] string.Template is used instead of str.format because the scaffold
# [EN] contains both JSON braces and language-pack placeholders such as {name};
# [EN] str.format would treat them as its own placeholders. $xxx does not
# [EN] conflict with either.
# [ZH] 使用 string.Template 而不是 str.format：骨架中既有 JSON 花括号，又有语言包
# [ZH] 占位符 {name}；str.format 会将它们误当作自身占位符。$xxx 与两者均不冲突。

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
