"""
[EN]
EchoDesk skill and plugin marketplace backend.

Architecture

The module separates data access from service behaviour so that each layer can be
tested independently:

* Data-source layer: GitHub Search API (topic ``echodesk-plugin``) discovers
  repositories; ``raw.githubusercontent.com`` with the GitHub contents API as a
  fallback reads ``skill.json`` and ``README.md``; ``codeload.github.com``
  downloads source tarballs. Only GitHub is accepted as a source; arbitrary URLs
  and other hosts are rejected.
* Service layer: :class:`Marketplace` exposes ``search``, ``detail``,
  ``install``, ``uninstall``, ``installed``, and ``status`` to the JSON-line
  protocol in ``main.py`` and to the marketplace interface.

Cache strategy

``cache_file`` (default ``market_cache.json``) stores a complete snapshot:

* ``items``: repository entries including name, description, stars, last push
  time, license, topics, and owner avatar.
* ``verified``: ``skill.json`` verification results keyed by ``owner/name``, so
  each refresh does not re-request the same repository.
* ``synced_at`` and ``last_pushed_at``: last synchronization time and the
  greatest ``pushed_at`` observed so far.

The first load fetches up to 100 matching repositories. Later refreshes use the
incremental query ``pushed:>=<last pushed date>`` and merge results by
``full_name``, keeping the entry with the newer ``pushed_at``. GitHub repository
search does not support a ``pushed_at:`` qualifier; only ``pushed:`` accepts a
date or timestamp. Using ``pushed_at:`` would produce a 422 validation error.
If an incremental result fills the single-page limit, the refresh falls back to
a full load because the result may be incomplete.

Within ``cache_ttl_hours`` the cached snapshot is returned without a network
request. Expired or empty caches trigger a refresh. Network failures, timeouts,
and rate limits (HTTP 403/429) degrade to the cached snapshot with
``offline=True`` and an explanatory message. Failed refreshes do not write
``synced_at``, preventing a failure from being recorded as a successful sync.

Security design

* Skills are discovered only through the configured topic. ``detail`` and
  ``install`` accept ``owner/name`` in advanced mode, validated by a strict
  regular expression; URLs and other hosts are rejected.
* ``skill.json`` is read before installation and returned by ``detail`` so the
  frontend can display dependencies and permissions.
* Before extraction, every tarball member is checked. Absolute paths, ``..``
  traversal, out-of-tree symlinks, and device files are rejected. ``filter="data"``
  provides a second layer of protection.
* Prepare scripts are not executed automatically, and pip dependencies are not
  installed automatically. Both are listed in the response for explicit user
  confirmation. :meth:`Marketplace._sanitized_env` returns a copy of the
  environment with sensitive variable names removed, for future execution after
  explicit confirmation.
* The tarball SHA-256 and ``skill.json`` license are recorded in the registry for
  signature verification and license display.
* If ``skill.json`` declares ``echodesk_min_version`` or ``min_version``, it is
  compared with ``version.APP_VERSION``. Incompatibility is reported in the
  response but installation remains permitted; the marketplace should not
  unilaterally block an explicitly confirmed installation.

[ZH]
EchoDesk 技能与插件市场后端。

架构

模块将数据访问与服务行为分离，使两层均可独立测试：

* 数据源层：GitHub Search API（topic ``echodesk-plugin``）发现仓库；
  ``raw.githubusercontent.com`` 读取 ``skill.json`` 与 ``README.md``，失败时回退到
  GitHub contents API；``codeload.github.com`` 下载源码 tarball。仅接受 GitHub
  作为数据源，拒绝任意 URL 与其他主机。
* 服务层：:class:`Marketplace` 向 ``main.py`` 的 JSON 行协议与市场界面提供
  ``search``、``detail``、``install``、``uninstall``、``installed`` 与 ``status``。

缓存策略

``cache_file``（默认 ``market_cache.json``）保存完整快照：

* ``items``：仓库条目，包括名称、描述、Star、最近推送时间、许可证、topic 与头像。
* ``verified``：``skill.json`` 验证结果，以 ``owner/name`` 为键，避免每次刷新重复
  请求同一仓库。
* ``synced_at`` 与 ``last_pushed_at``：上次同步时间与已观察到的最大 ``pushed_at``。

首次加载最多获取 100 个匹配仓库。后续刷新使用 ``pushed:>=<上次推送日期>`` 增量
查询，并按 ``full_name`` 合并，保留 ``pushed_at`` 较新的条目。GitHub 仓库搜索不
支持 ``pushed_at:`` 限定符，仅 ``pushed:`` 接受日期或时间戳；使用 ``pushed_at:``
会产生 422 校验错误。若增量结果达到单页上限，则回退为全量加载，以避免遗漏。

在 ``cache_ttl_hours`` 内直接返回缓存，不发起网络请求。缓存过期或为空时才刷新。
网络故障、超时与限流（HTTP 403/429）降级为缓存快照，并设置 ``offline=True`` 与
说明文本。刷新失败时不写入 ``synced_at``，避免将失败记录为成功同步。

安全设计

* 技能仅通过配置的 topic 发现。``detail`` 与 ``install`` 的高级模式接受
  ``owner/name``，并通过严格正则校验；URL 与其他主机一律拒绝。
* 安装前读取 ``skill.json``，``detail`` 也会返回其内容，供前端展示依赖与权限。
* 解压前逐条检查 tarball 成员。绝对路径、``..`` 穿越、越界符号链接与设备文件
  均被拒绝；``filter="data"`` 提供第二层防护。
* 不自动执行 prepare 脚本，也不自动安装 pip 依赖。两者仅在响应中列出，等待用户
  明确确认。:meth:`Marketplace._sanitized_env` 返回移除敏感变量名后的环境副本，
  供未来在明确确认后执行时使用。
* tarball 的 SHA-256 与 ``skill.json`` 许可证记录到注册表，用于签名校验与许可证
  展示。
* 若 ``skill.json`` 声明 ``echodesk_min_version`` 或 ``min_version``，将与
  ``version.APP_VERSION`` 比较。不兼容会在响应中报告，但仍允许安装；市场不得在
  用户明确确认后单方面阻止安装。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

# [EN] Backend modules use flat imports. Prepending this file's directory to
# [EN] sys.path keeps the version module resolvable regardless of the current
# [EN] working directory.
# [ZH] 后端模块采用平铺导入。将本文件所在目录加入 sys.path 开头，可确保从任意
# [ZH] 工作目录导入时仍能解析 version 模块。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# [EN] Environment-dependent import: a missing version module falls back to a
# [EN] conservative default.
# [ZH] 环境相关导入：version 模块缺失时使用保守默认值。
try:  # pragma: no cover
    from version import APP_VERSION as _APP_VERSION
except Exception:  # noqa: BLE001
    _APP_VERSION = "v0.2.0"

# [EN] urllib3 is installed with requests. If it is unavailable, retries are
# [EN] disabled rather than failing the import.
# [ZH] urllib3 随 requests 安装。若不可用，则禁用重试，而不是使导入失败。
try:  # pragma: no cover
    from urllib3.util.retry import Retry as _Retry
except Exception:  # noqa: BLE001
    _Retry = None  # type: ignore[assignment]

# [EN] i18n is imported only in this module's import section. Its reverse
# [EN] dependency on config is lazy, so no import cycle is created.
# [ZH] i18n 仅在本模块的导入区引用。它对 config 的反向依赖是惰性的，因此不会
# [ZH] 形成导入环。
from i18n import t as _t


__all__ = ["MarketplaceError", "Marketplace", "MAX_TARBALL_BYTES"]

log = logging.getLogger("EchoDesk.Marketplace")


# ---------------------------------------------------------------------------
# [EN] Constants / [ZH] 常量
# ---------------------------------------------------------------------------

#: [EN] Request timeout in seconds. Every network request must have one to avoid
#: [EN] blocking the interface.
#: [ZH] 请求超时（秒）。所有网络请求都必须设置超时，避免界面阻塞。
REQUEST_TIMEOUT = 15
#: [EN] Short timeout for skill.json verification so a slow network does not
#: [EN] delay a full refresh excessively.
#: [ZH] skill.json 验证使用的短超时，避免慢网络过度拖长刷新。
VERIFY_TIMEOUT = 10
#: [EN] Maximum tarball size: 50 MB.
#: [ZH] tarball 最大下载体积：50 MB。
MAX_TARBALL_BYTES = 50 * 1024 * 1024
#: [EN] Verification thread-pool concurrency.
#: [ZH] 验证线程池并发数。
MAX_VERIFY_WORKERS = 8
#: [EN] Total time budget for concurrent verification during a refresh, in seconds.
#: [ZH] 单次刷新中等待并发验证的总预算（秒）。
VERIFY_DEADLINE_REFRESH = 45.0
#: [EN] Shorter verification budget when serving a valid cache, so the interface
#: [EN] remains responsive.
#: [ZH] 命中有效缓存时的较短验证预算，以保证界面响应。
VERIFY_DEADLINE_CACHE = 8.0
#: [EN] Page size and page count for the initial full load.
#: [ZH] 首次全量加载的每页条数与页数。
INITIAL_FETCH_PER_PAGE = 100
INITIAL_FETCH_PAGES = 1
#: [EN] Maximum cached entries. Excess entries are evicted by most recent push time.
#: [ZH] 缓存条目上限。超出时按最近推送时间淘汰。
MAX_CACHE_ITEMS = 200
#: [EN] Maximum verification results retained in the cache.
#: [ZH] 缓存中保留的验证结果条数上限。
MAX_VERIFIED_CACHE = 800
#: [EN] README excerpt length in characters.
#: [ZH] README 摘要长度（字符）。
README_EXCERPT_CHARS = 800
#: [EN] Default User-Agent.
#: [ZH] 默认 User-Agent。
USER_AGENT = "EchoDesk/0.2.0"

#: [EN] Repository full-name format. Advanced mode accepts only owner/name.
#: [ZH] 仓库全名格式。高级模式仅接受 owner/name。
REPO_FULL_NAME_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
#: [EN] Skill-id sanitization: only letters, digits, and -_. are retained.
#: [ZH] 技能 id 安全化：仅保留字母、数字与 -_.。
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9\-_.]")
#: [EN] Windows reserved device names, preventing directories such as CON or PRN.
#: [ZH] Windows 保留设备名，避免创建 CON、PRN 等目录。
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
#: [EN] Environment-variable name fragments that are removed before passing an
#: [EN] environment to third-party code.
#: [ZH] 向第三方代码传递环境前需要移除的环境变量名称片段。
_SENSITIVE_ENV_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")

#: [EN] Cached entry fields. verified and installed are computed on return.
#: [ZH] 缓存条目字段。verified 与 installed 在返回时计算。
ITEM_FIELDS = (
    "full_name", "name", "description", "stars", "pushed_at",
    "license", "topics", "owner_avatar", "version", "html_url",
)


# ---------------------------------------------------------------------------
# [EN] Module-level utilities (pure functions, independently testable)
# [ZH] 模块级工具（纯函数，可独立测试）
# ---------------------------------------------------------------------------

def _cfg_get(cfg: Any, name: str, default: Any) -> Any:
    """
    [EN]
    Read a configuration value by attribute or dictionary key. Missing or
    ``None`` values fall back to the default.

    [ZH]
    按属性或字典键读取配置值。缺失或为 ``None`` 时回退到默认值。
    """
    if isinstance(cfg, dict):
        value = cfg.get(name, default)
    else:
        value = getattr(cfg, name, default)
    return default if value is None else value


def _iso_now() -> str:
    """
    [EN]
    Return the current local time as an ISO string with a time-zone offset.

    [ZH]
    返回带时区偏移的当前本地时间 ISO 字符串。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(text: Any) -> Optional[datetime]:
    """
    [EN]
    Parse an ISO timestamp and return ``None`` on failure. A manually corrupted
    cache must not crash the application.

    [ZH]
    解析 ISO 时间戳；失败时返回 ``None``。手工损坏的缓存不得导致应用崩溃。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_json(path: str, default: Any) -> Any:
    """
    [EN]
    Read JSON defensively. A missing or malformed file returns ``default``; the
    cache is treated as empty and rebuilt on the next write.

    [ZH]
    防御性读取 JSON。文件缺失或格式错误时返回 ``default``；缓存按空处理，并在
    下次写入时重建。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except Exception as exc:  # noqa: BLE001 - 文件损坏不能影响程序启动
        # [EN] Treat a corrupt cache as empty; it will be rebuilt on the next write.
        # [ZH] 将损坏的缓存视为空，并在下次写入时重建。
        log.warning("读取 %s 失败（视为空，将在下次写入时重建）：%s", path, exc)
        return default


def _write_json(path: str, data: Any) -> bool:
    """
    [EN]
    Write JSON atomically by writing a temporary file and replacing the target.
    Failures are logged and do not propagate to the caller.

    [ZH]
    通过写入临时文件再替换目标文件的方式原子写入 JSON。失败仅记录日志，不向
    调用方传播异常。
    """
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("写入 %s 失败：%s", path, exc)
        return False


def _clean_env(env: Dict[str, str]) -> Dict[str, str]:
    """
    [EN]
    Return a copy of the environment with names containing KEY, SECRET, TOKEN,
    or PASSWORD removed.

    [ZH]
    返回环境变量副本，移除名称包含 KEY、SECRET、TOKEN 或 PASSWORD 的变量。
    """
    cleaned: Dict[str, str] = {}
    for name, value in env.items():
        upper = str(name).upper()
        if any(marker in upper for marker in _SENSITIVE_ENV_MARKERS):
            continue
        cleaned[name] = value
    return cleaned


def _safe_filename(raw: Any, fallback: str = "skill") -> str:
    """
    [EN]
    Sanitize a skill id for use as a directory name, retaining only letters,
    digits, and ``-_.``.

    [ZH]
    将技能 id 安全化为目录名，仅保留字母、数字与 ``-_.``。
    """
    text = SAFE_ID_RE.sub("", str(raw or "").strip())
    # [EN] Strip leading and trailing dots to prevent "." and ".." directory names.
    # [ZH] 去除首尾点，避免产生 "." 与 ".." 目录名。
    text = text.strip(".")
    if not text:
        text = SAFE_ID_RE.sub("", str(fallback or "").strip()).strip(".") or "skill"
    if text.upper() in _WINDOWS_RESERVED:
        text = f"skill_{text}"
    return text[:80]


def _is_within(path: str, parent: str) -> bool:
    """
    [EN]
    Return whether ``path`` resides inside ``parent``, preventing directory
    traversal.

    [ZH]
    返回 ``path`` 是否位于 ``parent`` 目录内，用于防止目录穿越。
    """
    try:
        real_path = os.path.realpath(os.path.abspath(path))
        real_parent = os.path.realpath(os.path.abspath(parent))
    except Exception:  # noqa: BLE001
        return False
    if real_path == real_parent:
        return True
    try:
        return os.path.commonpath([real_path, real_parent]) == real_parent
    # [EN] Different drive letters cannot be compared by commonpath.
    # [ZH] 不同盘符无法通过 commonpath 比较。
    except ValueError:
        return False


def _version_tuple(text: Any) -> Tuple[int, ...]:
    """
    [EN]
    Convert a version string such as ``v0.2.0`` or ``0.2`` into a comparable
    numeric tuple.

    [ZH]
    将 ``v0.2.0`` 或 ``0.2`` 等版本字符串转换为可比较的数字元组。
    """
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:4]) if nums else ()


def _as_str_list(value: Any) -> List[str]:
    """
    [EN]
    Flatten lists, dictionaries, and strings into a string list, accepting the
    multiple forms used by ``skill.json``.

    [ZH]
    将列表、字典与字符串拍平为字符串列表，兼容 ``skill.json`` 的多种写法。
    """
    out: List[str] = []

    def _walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            text = node.strip()
            if text:
                out.append(text)
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(item, (list, dict)):
                    _walk(item)
                elif item is None or item is True:
                    # [EN] Forms such as {"requests": null} or {"requests": true}
                    # [EN] contribute only the dependency name.
                    # [ZH] {"requests": null} 或 {"requests": true} 等形式仅取依赖名。
                    text = str(key).strip()
                    if text:
                        out.append(text)
                elif item is False:
                    continue
                else:
                    # [EN] {"requests": ">=2.0"} is treated as a version constraint
                    # [EN] and rendered as requests>=2.0.
                    # [ZH] {"requests": ">=2.0"} 视为版本约束，拼接为 requests>=2.0。
                    value_text = str(item).strip()
                    if value_text[:1] in "<>=!~^":
                        text = f"{key}{value_text}".strip()
                    else:
                        text = f"{key}=={value_text}".strip()
                    if text:
                        out.append(text)
            return
        if isinstance(node, (list, tuple, set)):
            for item in node:
                _walk(item)
            return
        out.append(str(node))

    _walk(value)
    # [EN] Deduplicate while preserving order.
    # [ZH] 去重并保持顺序。
    seen = set()
    result = []
    for item in out:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _extract_license(skill: Dict[str, Any]) -> str:
    """
    [EN]
    Read the license field from ``skill.json``, accepting both string and object
    forms.

    [ZH]
    读取 ``skill.json`` 中的 license 字段，兼容字符串与对象两种形式。
    """
    value = skill.get("license")
    if isinstance(value, dict):
        value = value.get("spdx_id") or value.get("type") or value.get("name") or ""
    return str(value or "").strip()


def _bundle_declared(skill: Dict[str, Any]) -> str:
    """
    [EN]
    Read the ``echodesk.bundle`` field strictly. This field is the primary
    verification signal for a skill bundle.

    Both ``{"echodesk": {"bundle": "main.py"}}`` and
    ``{"echodesk.bundle": "main.py"}`` are accepted. When ``bundle`` is an
    object, its path, main, entry, or file field is used.

    [ZH]
    严格读取 ``echodesk.bundle`` 字段。该字段是技能包验证的主要依据。

    兼容 ``{"echodesk": {"bundle": "main.py"}}`` 与
    ``{"echodesk.bundle": "main.py"}`` 两种形式。``bundle`` 为对象时，使用其
    path、main、entry 或 file 字段。
    """
    direct = skill.get("echodesk.bundle")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    nested = skill.get("echodesk")
    if isinstance(nested, dict):
        bundle = nested.get("bundle")
        if isinstance(bundle, str) and bundle.strip():
            return bundle.strip()
        if isinstance(bundle, dict):
            for key in ("path", "main", "entry", "file"):
                candidate = bundle.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
    return ""


def _extract_entry(skill: Dict[str, Any]) -> str:
    """
    [EN]
    Read the entry-file path. Prefer echodesk.bundle, then fall back to
    entry/main for compatibility.

    [ZH]
    读取入口文件路径。优先使用 echodesk.bundle，兼容情况下回退到 entry/main。
    """
    entry = _bundle_declared(skill)
    if entry:
        return entry
    for key in ("entry", "main"):
        value = skill.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for sub in ("path", "main", "entry", "file"):
                candidate = value.get(sub)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
    return ""


def _extract_dependencies(skill: Dict[str, Any]) -> List[str]:
    """
    [EN]
    Read the dependency list, accepting list and dictionary forms and falling
    back to ``requirements``.

    [ZH]
    读取依赖清单，兼容列表与字典形式，缺失时回退到 ``requirements``。
    """
    deps = _as_str_list(skill.get("dependencies"))
    if not deps:
        deps = _as_str_list(skill.get("requirements"))
    return deps


def _min_version_required(skill: Dict[str, Any]) -> str:
    """
    [EN]
    Read the minimum EchoDesk version declared by the skill, accepting
    ``echodesk_min_version`` or ``min_version``.

    [ZH]
    读取技能声明的最低 EchoDesk 版本，兼容 ``echodesk_min_version`` 与
    ``min_version``。
    """
    for key in ("echodesk_min_version", "min_version"):
        value = skill.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    echodesk = skill.get("echodesk")
    if isinstance(echodesk, dict):
        for key in ("min_version", "min_app_version"):
            value = echodesk.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _publish_sources(skill: Dict[str, Any]) -> Dict[str, str]:
    """
    [EN]
    Parse ``publish.npm`` and ``publish.pypi`` declarations, including top-level
    npm/pypi forms.

    [ZH]
    解析 ``publish.npm`` 与 ``publish.pypi`` 声明，包括顶层 npm/pypi 形式。
    """
    sources: Dict[str, str] = {}
    publish = skill.get("publish")
    if isinstance(publish, dict):
        for kind in ("npm", "pypi"):
            value = publish.get(kind)
            if isinstance(value, dict):
                value = value.get("name") or value.get("package") or ""
            if isinstance(value, str) and value.strip():
                sources[kind] = value.strip()
    for kind in ("npm", "pypi"):
        value = skill.get(kind)
        if isinstance(value, dict):
            value = value.get("name") or value.get("package") or ""
        if isinstance(value, str) and value.strip() and kind not in sources:
            sources[kind] = value.strip()
    return sources


def _coerce_item(raw: Any) -> Optional[Dict[str, Any]]:
    """
    [EN]
    Normalize a possibly corrupted cache entry into a dictionary with fixed
    fields.

    [ZH]
    将可能已损坏的缓存条目规整为具有固定字段的字典。
    """
    if not isinstance(raw, dict):
        return None
    full_name = str(raw.get("full_name") or "").strip()
    if not full_name:
        return None
    try:
        stars = int(raw.get("stars") or 0)
    except (TypeError, ValueError):
        stars = 0
    topics_raw = raw.get("topics")
    topics = [str(t) for t in topics_raw] if isinstance(topics_raw, (list, tuple)) else []
    return {
        "full_name": full_name,
        "name": str(raw.get("name") or full_name.split("/")[-1]),
        "description": str(raw.get("description") or ""),
        "stars": stars,
        "pushed_at": str(raw.get("pushed_at") or ""),
        "license": str(raw.get("license") or ""),
        "topics": topics,
        "owner_avatar": str(raw.get("owner_avatar") or ""),
        "version": str(raw.get("version") or ""),
        "html_url": str(raw.get("html_url") or f"https://github.com/{full_name}"),
    }


def _sort_items(items: Sequence[Dict[str, Any]], sort_key: str) -> List[Dict[str, Any]]:
    """
    [EN]
    Sort entries locally. GitHub's "best match" ranking cannot be reproduced
    offline, so star count and last push time provide an approximation.

    [ZH]
    在本地排序。GitHub 的“最佳匹配”排序无法离线复现，因此使用 Star 数与最近
    推送时间近似。
    """
    if sort_key == "updated":
        return sorted(
            items,
            key=lambda i: (str(i.get("pushed_at") or ""), int(i.get("stars") or 0)),
            reverse=True,
        )
    # [EN] Both "stars" and "best" sort by descending stars, then by descending
    # [EN] last push time.
    # [ZH] “stars”与“best”均按 Star 降序，再按最近推送时间降序排序。
    return sorted(
        items,
        key=lambda i: (int(i.get("stars") or 0), str(i.get("pushed_at") or "")),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# [EN] Exceptions / [ZH] 异常
# ---------------------------------------------------------------------------

class MarketplaceError(Exception):
    """
    [EN]
    Business exception for the marketplace. The message is always a user-facing,
    localized explanation.

    [ZH]
    市场模块的业务异常。消息始终是可直接展示给用户的本地化说明。
    """


# ---------------------------------------------------------------------------
# [EN] Service layer / [ZH] 服务层
# ---------------------------------------------------------------------------

class Marketplace:
    """
    [EN]
    Marketplace service layer: search, detail, install, uninstall, installed
    list, and status.

    Public methods do not raise on transient network failures; network errors
    degrade to a localized message and the cached snapshot. Only clear caller
    errors, such as an invalid repository name, raise :class:`MarketplaceError`.

    [ZH]
    技能市场服务层：搜索、详情、安装、卸载、已安装列表与状态。

    公开方法不会因临时网络故障抛出异常；网络错误降级为本地化消息与缓存快照。
    只有明确的调用方错误（例如仓库名格式非法）才抛出 :class:`MarketplaceError`。
    """

    def __init__(self, cfg: Any, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.log = logger or log

        # ---- [EN] Configuration, read defensively by attribute or key ----
        # ---- [ZH] 配置读取，按属性或键防御性读取 ----
        self.enabled: bool = bool(_cfg_get(cfg, "enabled", True))
        self.github_token: str = str(_cfg_get(cfg, "github_token", "") or "").strip()
        try:
            self.cache_ttl_hours: float = float(_cfg_get(cfg, "cache_ttl_hours", 6))
        except (TypeError, ValueError):
            self.cache_ttl_hours = 6.0
        self.skill_topic: str = str(_cfg_get(cfg, "skill_topic", "echodesk-plugin") or "echodesk-plugin").strip()
        self.auto_update_check: bool = bool(_cfg_get(cfg, "auto_update_check", True))
        try:
            self.per_page: int = max(1, min(100, int(_cfg_get(cfg, "per_page", 20))))
        except (TypeError, ValueError):
            self.per_page = 20
        self.api_base: str = str(_cfg_get(cfg, "api_base", "https://api.github.com") or "").rstrip("/")
        # [EN] Source and tarball base URLs may be overridden for offline or
        # [EN] mirror testing; the official endpoints are the defaults.
        # [ZH] 源码与 tarball 基础地址可覆盖，以便离线或镜像测试；默认使用官方地址。
        self.raw_base: str = str(
            _cfg_get(cfg, "raw_base", "https://raw.githubusercontent.com") or ""
        ).rstrip("/")
        self.codeload_base: str = str(
            _cfg_get(cfg, "codeload_base", "https://codeload.github.com") or ""
        ).rstrip("/")
        try:
            self.timeout: float = float(_cfg_get(cfg, "request_timeout", REQUEST_TIMEOUT))
        except (TypeError, ValueError):
            self.timeout = float(REQUEST_TIMEOUT)

        # ---- [EN] Path resolution: relative paths use the backend working directory ----
        # ---- [ZH] 路径解析：相对路径以后端工作目录为基准 ----
        self.cache_file: str = self._resolve_path(_cfg_get(cfg, "cache_file", "market_cache.json"))
        self.install_dir: str = self._resolve_path(_cfg_get(cfg, "install_dir", "skills"))
        self.registry_file: str = self._resolve_path(_cfg_get(cfg, "registry_file", "skills_registry.json"))

        # ---- [EN] Internal state / [ZH] 内部状态 ----
        # [EN] Protects read-modify-write sequences on cache and registry files.
        # [ZH] 保护缓存与注册表文件的读取—修改—写入序列。
        self._io_lock = threading.RLock()
        # [EN] Allows only one refresh at a time.
        # [ZH] 同一时刻仅允许一个刷新操作。
        self._refresh_lock = threading.Lock()
        # [EN] One requests.Session per thread.
        # [ZH] 每个线程一个 requests.Session。
        self._tls = threading.local()

    # ------------------------------------------------------------------
    # [EN] Infrastructure / [ZH] 基础设施
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_path(value: Any) -> str:
        """
        [EN]
        Resolve a possibly relative configured path to an absolute path.

        [ZH]
        将配置中可能为相对路径的值解析为绝对路径。
        """
        raw = os.path.expanduser(str(value or "").strip())
        if not raw:
            raw = "."
        if not os.path.isabs(raw):
            raw = os.path.join(os.getcwd(), raw)
        return os.path.abspath(raw)

    def _sanitized_env(self) -> Dict[str, str]:
        """
        [EN]
        Return a copy of the environment with all variables whose names contain
        KEY, SECRET, TOKEN, or PASSWORD removed, case-insensitively.

        Third-party ``prepare`` scripts run with the local account's privileges.
        Passing the environment unchanged would disclose API keys, cloud
        credentials, and access tokens. Prepare scripts are not executed
        automatically today; if execution is added after explicit user
        confirmation, this method's result must be used as the child process
        environment.

        [ZH]
        返回环境变量副本，并移除名称包含 KEY、SECRET、TOKEN 或 PASSWORD 的变量
        （大小写不敏感）。

        第三方 ``prepare`` 脚本以本地账户权限运行。原样传递环境变量会泄露 API
        Key、云凭证与访问令牌。当前不会自动执行 prepare 脚本；若未来在用户明确
        确认后执行，必须使用本方法的返回值作为子进程环境。
        """
        return _clean_env(dict(os.environ))

    def _session(self) -> requests.Session:
        """
        [EN]
        Return the current thread's ``requests.Session``. Sessions are not
        strictly thread-safe, so a thread-local instance is used.

        [ZH]
        返回当前线程的 ``requests.Session``。Session 并非严格线程安全，因此使用
        线程局部实例。
        """
        session = getattr(self._tls, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(max_retries=self._retry_policy())
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._tls.session = session
        return session

    @staticmethod
    def _retry_policy() -> Any:
        """
        [EN]
        Return a conservative retry policy: retry connection errors and 5xx
        responses only. Rate-limit responses (403/429) are never retried.

        [ZH]
        返回保守的重试策略：仅重试连接错误与 5xx 响应。限流响应（403/429）绝不
        重试。
        """
        if _Retry is None:
            return 0
        return _Retry(
            total=2,
            connect=2,
            read=0,
            status=2,
            backoff_factor=0.4,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
        )

    def _headers(self, accept: Optional[str] = None) -> Dict[str, str]:
        """
        [EN]
        Build request headers. When a token is configured, add Authorization to
        raise the API rate limit.

        [ZH]
        构造请求头。配置 token 时添加 Authorization，以提高 API 速率限额。
        """
        headers = {
            "Accept": accept or "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    def _request(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        accept: Optional[str] = None,
        streaming: bool = False,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        """
        [EN]
        Perform a GET request.

        Only transport failures and status codes that make the request unusable
        (rate limit, unauthorized, 5xx) raise :class:`MarketplaceError`. Business
        status codes such as 404 are returned to the caller for interpretation.

        [ZH]
        发起 GET 请求。

        仅传输层故障以及使请求不可继续的状态码（限流、未授权、5xx）抛出
        :class:`MarketplaceError`。404 等业务状态码返回给调用方判断。
        """
        try:
            resp = self._session().get(
                url,
                headers=self._headers(accept),
                params=params,
                stream=streaming,
                timeout=timeout or self.timeout,
                allow_redirects=True,
            )
        except requests.exceptions.Timeout as exc:
            raise MarketplaceError(_t("market.net.timeout", url=self._short(url))) from exc
        except requests.exceptions.SSLError as exc:
            raise MarketplaceError(_t("market.net.tls", url=self._short(url))) from exc
        except requests.exceptions.RequestException as exc:
            raise MarketplaceError(_t("market.net.unavailable", kind=type(exc).__name__)) from exc

        status = resp.status_code
        if status in (403, 429) and self._is_rate_limited(resp, status):
            raise MarketplaceError(_t("market.net.rate_limited"))
        if status == 401:
            raise MarketplaceError(_t("market.net.token_invalid"))
        if status == 403:
            raise MarketplaceError(_t("market.net.forbidden"))
        if status >= 500:
            raise MarketplaceError(_t("market.net.server_error", status=status))
        return resp

    @staticmethod
    def _is_rate_limited(resp: requests.Response, status: int) -> bool:
        """
        [EN]
        Return whether the response indicates a GitHub rate limit.

        [ZH]
        返回响应是否表示命中 GitHub 限流。
        """
        if status == 429:
            return True
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        try:
            body = (resp.text or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return "rate limit" in body or "abuse" in body

    @staticmethod
    def _short(url: str, limit: int = 80) -> str:
        """
        [EN]
        Truncate a URL for logs or messages so that an overlong address does not
        flood the interface.

        [ZH]
        截断 URL 以便记录日志或显示提示，避免超长地址占满界面。
        """
        return url if len(url) <= limit else url[: limit - 1] + "…"

    def _emit(self, on_progress: Optional[Callable[[str], None]], text: str) -> None:
        """
        [EN]
        Report an installation stage. An exception in the progress callback must
        never affect the installation flow.

        [ZH]
        上报安装阶段。进度回调中的异常不得影响安装流程。
        """
        self.log.info("%s", text)
        if on_progress is None:
            return
        try:
            on_progress(str(text))
        except Exception as exc:  # noqa: BLE001
            self.log.debug("进度回调异常（已忽略）：%s", exc)

    # ------------------------------------------------------------------
    # [EN] Cache: read and write / [ZH] 缓存：读写
    # ------------------------------------------------------------------

    def _empty_cache(self) -> Dict[str, Any]:
        return {"schema": 1, "synced_at": "", "last_pushed_at": "", "items": [], "verified": {}}

    def _load_cache(self) -> Dict[str, Any]:
        """
        [EN]
        Read the cache file. A missing or corrupted file yields an empty cache
        without raising.

        [ZH]
        读取缓存文件。文件缺失或损坏时返回空缓存，不抛出异常。
        """
        raw = _read_json(self.cache_file, None)
        if not isinstance(raw, dict):
            return self._empty_cache()
        items: List[Dict[str, Any]] = []
        for entry in raw.get("items") or []:
            coerced = _coerce_item(entry)
            if coerced:
                items.append(coerced)
        verified: Dict[str, Dict[str, Any]] = {}
        raw_verified = raw.get("verified")
        if isinstance(raw_verified, dict):
            for key, value in raw_verified.items():
                if isinstance(key, str) and isinstance(value, dict):
                    verified[key] = value
        return {
            "schema": 1,
            "synced_at": str(raw.get("synced_at") or ""),
            "last_pushed_at": str(raw.get("last_pushed_at") or ""),
            "items": items,
            "verified": verified,
        }

    def _save_cache(self, cache: Dict[str, Any]) -> bool:
        """
        [EN]
        Persist the cache, bounding both items and verification records to
        prevent unbounded file growth.

        [ZH]
        将缓存落盘，并限制条目数与验证记录数，防止文件无限增长。
        """
        items = list(cache.get("items") or [])
        items.sort(key=lambda i: str(i.get("pushed_at") or ""), reverse=True)
        items = items[:MAX_CACHE_ITEMS]
        verified = dict(cache.get("verified") or {})
        if len(verified) > MAX_VERIFIED_CACHE:
            ordered = sorted(
                verified.items(),
                key=lambda kv: str((kv[1] or {}).get("checked_at") or ""),
                reverse=True,
            )
            verified = dict(ordered[:MAX_VERIFIED_CACHE])
        payload = {
            "schema": 1,
            "synced_at": str(cache.get("synced_at") or ""),
            "last_pushed_at": str(cache.get("last_pushed_at") or ""),
            "items": items,
            "verified": verified,
            "note": _t("market.cache.note"),
        }
        return _write_json(self.cache_file, payload)

    def _cache_is_fresh(self, cache: Dict[str, Any]) -> bool:
        """
        [EN]
        Return whether the cache is within its TTL. A non-positive TTL disables
        cache reuse, which is useful for forcing refresh in tests.

        [ZH]
        返回缓存是否仍在有效期（TTL）内。TTL 小于等于 0 时禁用缓存复用，便于
        测试中强制刷新。
        """
        if self.cache_ttl_hours <= 0:
            return False
        synced = _parse_iso(cache.get("synced_at"))
        if synced is None:
            return False
        age = (datetime.now(timezone.utc) - synced).total_seconds()
        return age < self.cache_ttl_hours * 3600

    def _verified_entry_valid(self, entry: Optional[Dict[str, Any]]) -> bool:
        """
        [EN]
        Return whether a verification result is still valid, reusing
        ``cache_ttl_hours``.

        [ZH]
        返回验证结果是否仍在有效期内，复用 ``cache_ttl_hours``。
        """
        if not isinstance(entry, dict) or "verified" not in entry:
            return False
        if self.cache_ttl_hours <= 0:
            return False
        checked = _parse_iso(entry.get("checked_at"))
        if checked is None:
            return False
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        return age < self.cache_ttl_hours * 3600

    # ------------------------------------------------------------------
    # [EN] Cache: refresh and merge / [ZH] 缓存：刷新与合并
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_items(
        old_items: Sequence[Dict[str, Any]], new_items: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        [EN]
        Merge entries by ``full_name``, keeping the entry with the newer
        ``pushed_at`` for the same repository.

        [ZH]
        按 ``full_name`` 合并条目；同一仓库保留 ``pushed_at`` 较新的条目。
        """
        merged: Dict[str, Dict[str, Any]] = {}
        for item in list(old_items) + list(new_items):
            coerced = _coerce_item(item)
            if not coerced:
                continue
            key = coerced["full_name"].lower()
            current = merged.get(key)
            if current is None:
                merged[key] = coerced
                continue
            # [EN] Merge strategy: newer data wins, but missing fields retain the
            # [EN] older value.
            # [ZH] 合并策略：新数据优先，缺失字段沿用旧值。
            winner = coerced if str(coerced["pushed_at"]) >= str(current["pushed_at"]) else current
            loser = current if winner is coerced else coerced
            for field in ITEM_FIELDS:
                if not winner.get(field) and loser.get(field):
                    winner[field] = loser[field]
            merged[key] = winner
        return list(merged.values())

    def _github_search(
        self,
        extra_query: str,
        sort_key: str,
        page: int,
        per_page: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        [EN]
        Call the GitHub Search API and return normalized entries and
        ``total_count``.

        The query is always ``topic:<skill_topic> <extra>``. ``extra_query`` may
        be user keywords or a qualifier such as ``pushed:>=YYYY-MM-DD`` used for
        incremental refresh.

        [ZH]
        调用 GitHub Search API，返回规范化条目列表与 ``total_count``。

        查询始终为 ``topic:<skill_topic> <附加条件>``。``extra_query`` 可以是
        用户关键词，也可以是增量刷新使用的 ``pushed:>=YYYY-MM-DD`` 等限定符。
        """
        query = f"topic:{self.skill_topic}"
        extra = str(extra_query or "").strip()
        if extra:
            query = f"{query} {extra}"

        params: Dict[str, Any] = {
            "q": query,
            "per_page": max(1, min(100, int(per_page))),
            "page": max(1, int(page)),
        }
        # [EN] Sort mapping: "stars" maps to stars, "updated" maps to updated,
        # [EN] and "best" omits the sort parameter to use GitHub best match.
        # [ZH] 排序映射：“stars”映射到 stars，“updated”映射到 updated，“best”
        # [ZH] 不传排序参数，使用 GitHub 最佳匹配。
        if sort_key == "stars":
            params["sort"] = "stars"
            params["order"] = "desc"
        elif sort_key == "updated":
            params["sort"] = "updated"
            params["order"] = "desc"

        url = f"{self.api_base}/search/repositories"
        resp = self._request(url, params=params)
        if resp.status_code == 422:
            raise MarketplaceError(_t("market.net.search_rejected"))
        if resp.status_code != 200:
            raise MarketplaceError(_t("market.net.search_failed", status=resp.status_code))
        try:
            data = resp.json()
        except ValueError as exc:
            raise MarketplaceError(_t("market.net.bad_json")) from exc
        if not isinstance(data, dict):
            raise MarketplaceError(_t("market.net.bad_shape"))
        raw_items = data.get("items")
        items = [self._map_repo(r) for r in raw_items] if isinstance(raw_items, list) else []
        items = [i for i in items if i]
        try:
            total = int(data.get("total_count") or 0)
        except (TypeError, ValueError):
            total = len(items)
        return items, total

    @staticmethod
    def _map_repo(raw: Any) -> Optional[Dict[str, Any]]:
        """
        [EN]
        Map a GitHub repository JSON object to the fixed fields consumed by the
        frontend.

        [ZH]
        将 GitHub 仓库 JSON 映射为前端使用的固定字段。
        """
        if not isinstance(raw, dict):
            return None
        full_name = str(raw.get("full_name") or "").strip()
        if not full_name:
            return None
        license_text = ""
        license_raw = raw.get("license")
        if isinstance(license_raw, dict):
            license_text = str(
                license_raw.get("spdx_id") or license_raw.get("key") or license_raw.get("name") or ""
            )
        elif isinstance(license_raw, str):
            license_text = license_raw
        if license_text.upper() in ("NOASSERTION", "NONE", "NULL"):
            license_text = ""
        topics_raw = raw.get("topics")
        topics = [str(t) for t in topics_raw] if isinstance(topics_raw, (list, tuple)) else []
        owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
        try:
            stars = int(raw.get("stargazers_count") or 0)
        except (TypeError, ValueError):
            stars = 0
        return {
            "full_name": full_name,
            "name": str(raw.get("name") or full_name.split("/")[-1]),
            "description": str(raw.get("description") or ""),
            "stars": stars,
            "pushed_at": str(raw.get("pushed_at") or ""),
            "license": license_text,
            "topics": topics,
            "owner_avatar": str((owner or {}).get("avatar_url") or ""),
            "version": "",
            "html_url": str(raw.get("html_url") or f"https://github.com/{full_name}"),
        }

    def _refresh(self) -> Tuple[bool, str, bool]:
        """
        [EN]
        Refresh the cache.

        Returns ``(success, message, offline)``. On failure, ``message`` is
        already a user-facing explanation. The initial load fetches up to 100
        entries; later refreshes use ``pushed:>=`` incremental merging.

        [ZH]
        刷新缓存。

        返回 ``(是否成功, message, offline)``。失败时 ``message`` 已是可直接展示
        给用户的说明。首次加载最多获取 100 条；后续使用 ``pushed:>=`` 增量合并。
        """
        cache = self._load_cache()
        old_items = cache.get("items") or []
        try:
            if not old_items:
                raw_items, _total = self._github_search("", "updated", 1, INITIAL_FETCH_PER_PAGE)
                fresh_items = raw_items
                mode = _t("market.refresh.mode_initial")
            else:
                since_date = str(cache.get("last_pushed_at") or "")[:10]
                if since_date:
                    raw_items, _total = self._github_search(
                        f"pushed:>={since_date}", "updated", 1, INITIAL_FETCH_PER_PAGE
                    )
                    fresh_items = raw_items
                    if len(fresh_items) >= INITIAL_FETCH_PER_PAGE:
                        # [EN] The incremental result reached the single-page
                        # [EN] limit and may be incomplete; fall back to a full
                        # [EN] refresh.
                        # [ZH] 增量结果达到单页上限，可能不完整；回退为全量刷新。
                        raw_items, _total = self._github_search(
                            "", "updated", 1, INITIAL_FETCH_PER_PAGE
                        )
                        fresh_items = raw_items
                        mode = _t("market.refresh.mode_full_overflow")
                    else:
                        mode = _t("market.refresh.mode_incremental", since=since_date)
                else:
                    raw_items, _total = self._github_search(
                        "", "updated", 1, INITIAL_FETCH_PER_PAGE
                    )
                    fresh_items = raw_items
                    mode = _t("market.refresh.mode_full")
        except MarketplaceError as exc:
            # [EN] Network or rate-limit failure: do not modify synced_at;
            # [EN] degrade to the cached snapshot.
            # [ZH] 网络或限流失败：不修改 synced_at；降级为缓存快照。
            return False, str(exc), True
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让刷新把界面打崩
            self.log.warning("刷新市场缓存时发生未预期错误：%s", exc, exc_info=True)
            return False, _t("market.refresh.unexpected", kind=type(exc).__name__), True

        merged = self._merge_items(old_items, fresh_items)
        last_pushed = cache.get("last_pushed_at") or ""
        for item in merged:
            if str(item.get("pushed_at") or "") > str(last_pushed):
                last_pushed = str(item["pushed_at"])
        with self._io_lock:
            cache["items"] = merged
            cache["last_pushed_at"] = last_pushed
            cache["synced_at"] = _iso_now()

        # [EN] Verify skill.json concurrently; results are written back to cache.
        # [ZH] 并发验证 skill.json；结果写回缓存。
        names = [i["full_name"] for i in merged]
        verified = self._verify_repos(names, deadline=VERIFY_DEADLINE_REFRESH)

        with self._io_lock:
            cache = self._load_cache()
            cache["items"] = merged
            cache["last_pushed_at"] = last_pushed
            cache["synced_at"] = _iso_now()
            cache["verified"] = verified
            for item in cache["items"]:
                info = verified.get(item["full_name"]) or {}
                if info.get("version"):
                    item["version"] = str(info["version"])
            self._save_cache(cache)

        mode_text = _t("market.refresh.done", mode=mode, count=len(merged))
        return True, mode_text, False

    def _ensure_fresh(self) -> Tuple[bool, str, bool]:
        """
        [EN]
        Ensure a usable cache is available. Returns
        ``(success, message, offline)``.

        [ZH]
        确保存在可用缓存。返回 ``(是否成功, message, offline)``。
        """
        cache = self._load_cache()
        if self._cache_is_fresh(cache):
            return True, _t("market.cache.hit"), False
        if not self.auto_update_check:
            # [EN] Automatic update checks are disabled: read the cache only and
            # [EN] do not access the network.
            # [ZH] 自动更新检查已禁用：仅读取缓存，不访问网络。
            if cache.get("items"):
                return True, _t("market.cache.offline_disabled"), True
            return False, _t("market.cache.disabled_empty"), True
        with self._refresh_lock:
            cache = self._load_cache()
            if self._cache_is_fresh(cache):
                # [EN] Another thread refreshed the cache while this thread was
                # [EN] waiting for the lock.
                # [ZH] 等待锁期间，其他线程已完成刷新。
                return True, _t("market.cache.hit"), False
            return self._refresh()

    # ------------------------------------------------------------------
    # [EN] skill.json and README retrieval and verification
    # [ZH] skill.json 与 README 读取及验证
    # ------------------------------------------------------------------

    def _fetch_skill_json(self, repo: str) -> Tuple[Optional[Dict[str, Any]], str, bool]:
        """
        [EN]
        Read ``skill.json`` from the repository root.

        Returns ``(content, error_message, transient_failure)``. A transient
        failure means a network or rate-limit problem, and the result must not be
        cached; otherwise one failure would be recorded as "no skill.json".

        ``raw.githubusercontent.com/<repo>/HEAD/skill.json`` is tried first, with
        the contents API as a fallback.

        [ZH]
        读取仓库根目录的 ``skill.json``。

        返回 ``(内容, 错误说明, 是否临时故障)``。临时故障表示网络或限流问题，结果
        不得写入缓存；否则一次失败会被记录为“没有 skill.json”。

        优先使用 ``raw.githubusercontent.com/<repo>/HEAD/skill.json``，失败时回退到
        contents API。
        """
        raw_url = f"{self.raw_base}/{repo}/HEAD/skill.json"
        transient = False
        last_error = ""
        try:
            resp = self._request(raw_url, accept="application/vnd.github.raw")
            if resp.status_code == 200:
                parsed = self._parse_skill_text(resp.content)
                if isinstance(parsed, dict):
                    return parsed, "", False
                return None, str(parsed), False
            if resp.status_code == 404:
                last_error = _t("market.skill.not_found_404")
            else:
                last_error = _t("market.skill.read_failed", status=resp.status_code)
        except MarketplaceError as exc:
            transient = True
            last_error = str(exc)
            self.log.debug("raw 读取 skill.json 失败，改用 contents API：%s", exc)

        api_url = f"{self.api_base}/repos/{repo}/contents/skill.json"
        try:
            resp = self._request(api_url, accept="application/vnd.github.raw")
            if resp.status_code == 200:
                parsed = self._parse_skill_text(resp.content)
                if isinstance(parsed, dict):
                    return parsed, "", False
                return None, str(parsed), False
            if resp.status_code == 404:
                return None, _t("market.skill.missing_in_repo"), False
            return None, _t("market.skill.read_failed", status=resp.status_code), transient
        except MarketplaceError as exc:
            return None, str(exc) or last_error or _t("market.skill.read_failed_plain"), True

    @staticmethod
    def _parse_skill_text(content: bytes) -> Any:
        """
        [EN]
        Parse ``skill.json`` bytes. Returns a dictionary on success or a
        user-facing error message on failure.

        [ZH]
        解析 ``skill.json`` 字节内容。成功返回字典，失败返回可直接展示的错误说明。
        """
        try:
            text = content.decode("utf-8-sig", errors="replace")
        except Exception:  # noqa: BLE001
            return _t("market.skill.bad_encoding")
        try:
            data = json.loads(text)
        except ValueError:
            return _t("market.skill.bad_json")
        if not isinstance(data, dict):
            return _t("market.skill.not_object")
        return data

    def _verify_one(self, repo: str) -> Optional[Dict[str, Any]]:
        """
        [EN]
        Verify one repository. A transient failure returns ``None``, meaning the
        result must not be cached this round.

        [ZH]
        验证单个仓库。临时故障返回 ``None``，表示本轮不写入缓存。
        """
        skill, error, transient = self._fetch_skill_json(repo)
        if transient:
            self.log.debug("验证 %s 时遇到临时故障，本次不缓存结论：%s", repo, error)
            return None
        if skill is None:
            return {
                "verified": False,
                "checked_at": _iso_now(),
                "has_bundle": False,
                "version": "",
                "license": "",
                "note": error or _t("market.skill.no_skill_json"),
            }
        bundle = _bundle_declared(skill)
        verified = bool(bundle)
        return {
            "verified": verified,
            "checked_at": _iso_now(),
            "has_bundle": verified,
            "version": str(skill.get("version") or ""),
            "license": _extract_license(skill),
            "bundle": bundle,
            "entry": _extract_entry(skill),
            "prepare": bool(skill.get("prepare")),
            "note": "" if verified else _t("market.skill.missing_bundle"),
        }

    def _verify_repos(self, repos: Iterable[str], deadline: float = VERIFY_DEADLINE_REFRESH) -> Dict[str, Any]:
        """
        [EN]
        Verify a batch of repositories concurrently and return a mapping from
        ``full_name`` to verification information.

        * Repositories with a valid cached result are not requested again.
        * The total wait is bounded by ``deadline`` (45 seconds by default).
          Repositories that do not finish are skipped this round and retried on
          the next refresh, preventing a long UI stall.

        [ZH]
        并发验证一批仓库，返回 ``full_name`` 到验证信息的映射。

        * 已有有效缓存结果的仓库不会重复请求。
        * 总等待时间受 ``deadline`` 限制（默认 45 秒）。未完成的仓库本轮跳过，
          下次刷新再验证，避免界面长时间无响应。
        """
        names: List[str] = []
        seen = set()
        for name in repos:
            text = str(name or "").strip()
            if text and text not in seen:
                seen.add(text)
                names.append(text)
        with self._io_lock:
            cache = self._load_cache()
            vmap: Dict[str, Any] = dict(cache.get("verified") or {})
        if not names:
            return vmap

        pending = [n for n in names if not self._verified_entry_valid(vmap.get(n))]
        if not pending:
            return vmap

        self.log.debug("开始并发验证 %d 个仓库的 skill.json（并发 %d）", len(pending), MAX_VERIFY_WORKERS)
        results: Dict[str, Any] = {}
        executor = ThreadPoolExecutor(max_workers=MAX_VERIFY_WORKERS, thread_name_prefix="ed-market-verify")
        try:
            futures = {executor.submit(self._verify_one, name): name for name in pending}
            done, not_done = wait(futures, timeout=max(1.0, float(deadline)))
            for future in done:
                name = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - 单个仓库失败不影响其它仓库
                    self.log.debug("验证 %s 时异常：%s", name, exc)
                    continue
                if outcome is not None:
                    results[name] = outcome
            if not_done:
                self.log.info("本轮有 %d 个仓库验证超时，已跳过（下次刷新重试）", len(not_done))
        finally:
            # [EN] Do not wait for in-flight requests; they are bounded by their
            # [EN] own ten-second timeout and the caller must return promptly.
            # [ZH] 不等待仍在执行中的请求；这些请求受各自的十秒超时约束，调用方
            # [ZH] 必须及时返回。
            executor.shutdown(wait=False, cancel_futures=True)

        if results:
            with self._io_lock:
                cache = self._load_cache()
                vmap = dict(cache.get("verified") or {})
                vmap.update(results)
                cache["verified"] = vmap
                # [EN] Backfill versions from skill.json so cards can display
                # [EN] version information directly.
                # [ZH] 将 skill.json 中的版本号回填到条目，便于卡片直接显示版本。
                for item in cache.get("items") or []:
                    info = results.get(item.get("full_name"))
                    if info and info.get("version"):
                        item["version"] = str(info["version"])
                self._save_cache(cache)
        return vmap

    def _fetch_readme(self, repo: str, limit: int = README_EXCERPT_CHARS) -> str:
        """
        [EN]
        Read a README excerpt (the first 800 characters), preferring the raw
        ``HEAD`` URL.

        [ZH]
        读取 README 摘要（前 800 个字符），优先使用 ``HEAD`` 的 raw 地址。
        """
        for name in ("README.md", "readme.md", "README.markdown", "README.rst"):
            url = f"{self.raw_base}/{repo}/HEAD/{name}"
            try:
                resp = self._request(url, accept="text/plain")
            except MarketplaceError as exc:
                self.log.debug("读取 README 失败：%s", exc)
                break
            if resp.status_code == 200:
                text = resp.content.decode("utf-8", errors="replace")
                return text[:limit]
            if resp.status_code not in (404, 400):
                break
        # [EN] Fallback: the contents API. Large files may be returned as base64;
        # [EN] only plain text is used here.
        # [ZH] 回退到 contents API。大文件可能以 base64 返回；此处仅使用纯文本。
        api_url = f"{self.api_base}/repos/{repo}/readme"
        try:
            resp = self._request(api_url, accept="application/vnd.github.raw")
            if resp.status_code == 200:
                return resp.content.decode("utf-8", errors="replace")[:limit]
        except MarketplaceError as exc:
            self.log.debug("contents API 读取 README 失败：%s", exc)
        return ""

    def _repo_meta(self, repo: str) -> Dict[str, Any]:
        """
        [EN]
        Read repository metadata (license, HTML URL, default branch). Returns an
        empty dictionary on failure.

        [ZH]
        读取仓库元信息（许可证、HTML 地址、默认分支）。失败时返回空字典。
        """
        url = f"{self.api_base}/repos/{repo}"
        try:
            resp = self._request(url)
        except MarketplaceError as exc:
            self.log.debug("读取仓库元信息失败：%s", exc)
            return {}
        if resp.status_code != 200:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # [EN] Repository-name validation / [ZH] 仓库名校验
    # ------------------------------------------------------------------

    def _normalize_repo(self, repo: Any) -> str:
        """
        [EN]
        Validate and normalize ``owner/name``. URLs, other hosts, and traversal
        forms are rejected.

        [ZH]
        校验并规范化 ``owner/name``。拒绝 URL、其他主机与目录穿越写法。
        """
        raw = str(repo or "").strip().strip("/")
        if not raw:
            raise MarketplaceError(_t("market.repo.missing"))
        if "://" in raw or raw.startswith("git@") or "@" in raw or ":" in raw:
            raise MarketplaceError(_t("market.repo.url_rejected", raw=raw))
        if raw.lower().endswith(".git"):
            raw = raw[:-4]
        if not REPO_FULL_NAME_RE.match(raw) or raw.count("/") != 1 or len(raw) > 200:
            raise MarketplaceError(_t("market.repo.bad_format", raw=raw))
        owner, name = raw.split("/", 1)
        if owner in (".", "..") or name in (".", ".."):
            raise MarketplaceError(_t("market.repo.relative_rejected", raw=raw))
        return raw

    # ------------------------------------------------------------------
    # [EN] Registry / [ZH] 注册表
    # ------------------------------------------------------------------

    def _empty_registry(self) -> Dict[str, Any]:
        return {"schema": 1, "skills": []}

    def _load_registry(self) -> Dict[str, Any]:
        """
        [EN]
        Read the registry. A corrupted registry is treated as empty and rebuilt
        on the next save.

        [ZH]
        读取注册表。注册表损坏时视为空，并在下次保存时重建。
        """
        raw = _read_json(self.registry_file, None)
        if not isinstance(raw, dict):
            return self._empty_registry()
        skills = raw.get("skills")
        if not isinstance(skills, list):
            skills = []
        cleaned: List[Dict[str, Any]] = []
        for entry in skills:
            if not isinstance(entry, dict):
                continue
            skill_id = str(entry.get("id") or "").strip()
            if not skill_id:
                continue
            deps = entry.get("dependencies")
            cleaned.append({
                "id": skill_id,
                "version": str(entry.get("version") or ""),
                "path": str(entry.get("path") or ""),
                "loader": str(entry.get("loader") or "python"),
                "repo": str(entry.get("repo") or ""),
                "installed_at": str(entry.get("installed_at") or ""),
                "dependencies": [str(d) for d in deps] if isinstance(deps, (list, tuple)) else [],
                "signature": str(entry.get("signature") or ""),
                "license": str(entry.get("license") or ""),
                "source": str(entry.get("source") or "github"),
            })
        return {"schema": 1, "skills": cleaned}

    def _save_registry(self, registry: Dict[str, Any]) -> bool:
        payload = {
            "schema": 1,
            "skills": list(registry.get("skills") or []),
            "note": _t("market.registry.note"),
        }
        return _write_json(self.registry_file, payload)

    def _installed_keys(self) -> set:
        """
        [EN]
        Return the key set used to determine installed status: repository names
        and skill ids.

        [ZH]
        返回用于判定已安装状态的键集合：仓库名与技能 id。
        """
        keys = set()
        for entry in self._load_registry().get("skills") or []:
            repo = str(entry.get("repo") or "").strip().lower()
            if repo:
                keys.add(repo)
            skill_id = str(entry.get("id") or "").strip().lower()
            if skill_id:
                keys.add(skill_id)
        return keys

    # ------------------------------------------------------------------
    # [EN] Public interface 1: search / [ZH] 公开接口 1：搜索
    # ------------------------------------------------------------------

    def search(self, keyword: str = "", sort: str = "best", page: int = 1) -> dict:
        """
        [EN]
        Search skill repositories by topic.

        ``sort`` is ``"best"``, ``"stars"``, or ``"updated"``. Returns
        ``{"items", "page", "per_page", "total_count", "total_pages",
        "offline", "message"}``. Each item contains full_name, name, description,
        stars, pushed_at, license, topics, owner_avatar, verified, version,
        installed, and html_url.

        [ZH]
        按 topic 搜索技能仓库。

        ``sort`` 取值为 ``"best"``、``"stars"`` 或 ``"updated"``。返回
        ``{"items", "page", "per_page", "total_count", "total_pages",
        "offline", "message"}``。每个条目包含 full_name、name、description、
        stars、pushed_at、license、topics、owner_avatar、verified、version、
        installed 与 html_url。
        """
        try:
            page_num = max(1, int(page or 1))
        except (TypeError, ValueError):
            page_num = 1
        per_page = self.per_page
        sort_key = sort if sort in ("best", "stars", "updated") else "best"
        kw = str(keyword or "").strip()

        if not self.enabled:
            return self._result([], page_num, per_page, 0, False, _t("market.search.disabled"))

        try:
            if kw:
                return self._search_online(kw, sort_key, page_num, per_page)
            return self._search_cached(sort_key, page_num, per_page)
        except MarketplaceError as exc:
            return self._degraded_result(sort_key, page_num, per_page, str(exc), kw)
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都不给用户看堆栈
            self.log.warning("搜索市场时发生未预期错误：%s", exc, exc_info=True)
            return self._degraded_result(
                sort_key, page_num, per_page,
                _t("market.search.unexpected", kind=type(exc).__name__), kw,
            )

    def _search_online(self, keyword: str, sort_key: str, page: int, per_page: int) -> dict:
        """
        [EN]
        Keyword search: prefer GitHub online search; keyword filtering is not
        performed locally.

        [ZH]
        关键词搜索：优先使用 GitHub 在线搜索，不在本地执行关键词过滤。
        """
        try:
            items, total = self._github_search(keyword, sort_key, page, per_page)
        except MarketplaceError as exc:
            return self._degraded_result(sort_key, page, per_page, str(exc), keyword)

        # [EN] Merge cached verified/version state and add newly discovered
        # [EN] repositories to the cache.
        # [ZH] 合并缓存中的 verified/version 状态，并将新发现的仓库并入缓存。
        with self._io_lock:
            cache = self._load_cache()
            cached_map = {i["full_name"].lower(): i for i in cache.get("items") or []}
            for item in items:
                known = cached_map.get(item["full_name"].lower())
                if known and not item.get("version") and known.get("version"):
                    item["version"] = str(known["version"])
            cache["items"] = self._merge_items(cache.get("items") or [], items)
            self._save_cache(cache)

        self._verify_repos([i["full_name"] for i in items], deadline=VERIFY_DEADLINE_CACHE)
        public_items = self._to_public(items, self._verification_snapshot())

        total_pages = (total + per_page - 1) // per_page if total > 0 else 0
        message = (_t("market.search.hit", keyword=keyword, total=total) if total
                   else _t("market.search.no_match"))
        return self._result(public_items, page, per_page, total, False, message, total_pages)

    def _search_cached(self, sort_key: str, page: int, per_page: int) -> dict:
        """
        [EN]
        Search without a keyword: serve the cache directly within TTL and
        refresh when it has expired.

        [ZH]
        无关键词搜索：TTL 内直接使用缓存，过期时刷新。
        """
        ok, message, offline = self._ensure_fresh()
        with self._io_lock:
            cache = self._load_cache()
        items = list(cache.get("items") or [])

        if not ok and not items:
            return self._result(
                [], page, per_page, 0, True,
                _t("market.search.empty_cache", reason=message),
            )
        if not ok:
            message = _t("market.search.cache_snapshot", reason=message)

        sorted_items = _sort_items(items, sort_key)
        total = len(sorted_items)
        start = (page - 1) * per_page
        page_items = sorted_items[start:start + per_page]

        # [EN] Verify the current page opportunistically; valid cached results
        # [EN] require no request.
        # [ZH] 顺带验证当前页；已有有效缓存结果时不发起请求。
        self._verify_repos([i["full_name"] for i in page_items], deadline=VERIFY_DEADLINE_CACHE)
        snapshot = self._verification_snapshot()
        with self._io_lock:
            cache = self._load_cache()
        by_name = {i["full_name"].lower(): i for i in cache.get("items") or []}
        for item in page_items:
            fresh = by_name.get(item["full_name"].lower())
            if fresh and fresh.get("version"):
                item["version"] = str(fresh["version"])

        total_pages = (total + per_page - 1) // per_page if total > 0 else 0
        return self._result(self._to_public(page_items, snapshot), page, per_page, total, offline, message, total_pages)

    def _verification_snapshot(self) -> Dict[str, Any]:
        """
        [EN]
        Take a snapshot of verification results for rendering the verified
        field.

        [ZH]
        获取验证结果快照，用于渲染 verified 字段。
        """
        with self._io_lock:
            return dict(self._load_cache().get("verified") or {})

    def _degraded_result(self, sort_key: str, page: int, per_page: int, reason: str, keyword: str) -> dict:
        """
        [EN]
        Degrade to a page sliced from the local cache and explain the cause in
        the message.

        [ZH]
        降级为本地缓存切片，并在 message 中说明原因。
        """
        try:
            with self._io_lock:
                cache = self._load_cache()
                items = list(cache.get("items") or [])
                verified = dict(cache.get("verified") or {})
            if keyword:
                low = keyword.lower()
                items = [
                    i for i in items
                    if low in str(i.get("full_name", "")).lower()
                    or low in str(i.get("name", "")).lower()
                    or low in str(i.get("description", "")).lower()
                    or any(low in str(t).lower() for t in i.get("topics") or [])
                ]
            if not items:
                return self._result(
                    [], page, per_page, 0, True,
                    _t("market.search.empty_cache", reason=reason),
                )
            sorted_items = _sort_items(items, sort_key)
            total = len(sorted_items)
            start = (page - 1) * per_page
            page_items = sorted_items[start:start + per_page]
            total_pages = (total + per_page - 1) // per_page if total > 0 else 0
            if keyword:
                message = _t("market.search.cache_filtered", reason=reason, keyword=keyword,
                             total=total)
            else:
                message = _t("market.search.cache_snapshot_count", reason=reason, total=total)
            return self._result(self._to_public(page_items, verified), page, per_page, total, True, message, total_pages)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("降级读取缓存失败：%s", exc)
            return self._result([], page, per_page, 0, True,
                                _t("market.search.cache_unavailable", reason=reason))

    def _to_public(self, items: Sequence[Dict[str, Any]], verified_map: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        [EN]
        Convert internal entries to the fixed fields consumed by the frontend.
        Key names must not change.

        [ZH]
        将内部条目转换为前端使用的固定字段。字段名不得修改。
        """
        installed = self._installed_keys()
        out: List[Dict[str, Any]] = []
        for item in items:
            full_name = str(item.get("full_name") or "")
            info = verified_map.get(full_name) or {}
            entry = {
                "full_name": full_name,
                "name": str(item.get("name") or full_name.split("/")[-1]),
                "description": str(item.get("description") or ""),
                "stars": int(item.get("stars") or 0),
                "pushed_at": str(item.get("pushed_at") or ""),
                "license": str(item.get("license") or info.get("license") or ""),
                "topics": [str(t) for t in (item.get("topics") or [])],
                "owner_avatar": str(item.get("owner_avatar") or ""),
                "verified": bool(info.get("verified")),
                "version": str(item.get("version") or info.get("version") or ""),
                "installed": full_name.lower() in installed,
                "html_url": str(item.get("html_url") or f"https://github.com/{full_name}"),
            }
            out.append(entry)
        return out

    @staticmethod
    def _result(
        items: List[Dict[str, Any]],
        page: int,
        per_page: int,
        total_count: int,
        offline: bool,
        message: str,
        total_pages: Optional[int] = None,
    ) -> dict:
        if total_pages is None:
            total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 0
        return {
            "items": items,
            "page": int(page),
            "per_page": int(per_page),
            "total_count": int(total_count),
            "total_pages": int(total_pages),
            "offline": bool(offline),
            "message": str(message),
        }

    # ------------------------------------------------------------------
    # [EN] Public interface 2: detail / [ZH] 公开接口 2：详情
    # ------------------------------------------------------------------

    def detail(self, repo: str) -> dict:
        """
        [EN]
        Return repository details: full ``skill.json``, README excerpt,
        dependencies, entry, and prepare flag.

        ``repo`` must be ``owner/name``; an invalid format raises
        :class:`MarketplaceError`. Network failures return degraded information
        with ``offline=True`` and an explanatory message rather than raising.
        The response also contains ``offline`` and ``message`` for frontend
        notices.

        [ZH]
        返回仓库详情：完整 ``skill.json``、README 摘要、依赖、入口与 prepare
        标志。

        ``repo`` 必须为 ``owner/name``；格式非法时抛出 :class:`MarketplaceError`。
        网络故障返回降级信息，包含 ``offline=True`` 与说明文本，而不抛出异常。
        响应还包含 ``offline`` 与 ``message``，供前端提示使用。
        """
        full_name = self._normalize_repo(repo)
        html_url = f"https://github.com/{full_name}"

        skill, error, transient = self._fetch_skill_json(full_name)
        readme = self._fetch_readme(full_name)

        skill_json: Dict[str, Any] = skill if isinstance(skill, dict) else {}
        entry = _extract_entry(skill_json) if skill_json else ""
        bundle = _bundle_declared(skill_json) if skill_json else ""
        license_text = _extract_license(skill_json) if skill_json else ""
        version = str(skill_json.get("version") or "") if skill_json else ""
        dependencies = _extract_dependencies(skill_json) if skill_json else []
        has_prepare = bool(skill_json.get("prepare")) if skill_json else False

        # [EN] Historical cache data provides a fallback so version, license,
        # [EN] and verification state remain visible offline.
        # [ZH] 缓存中的历史数据作为回退，使版本、许可证与验证状态在离线时仍可显示。
        with self._io_lock:
            cache = self._load_cache()
        cached_item = next(
            (i for i in cache.get("items") or [] if i["full_name"].lower() == full_name.lower()), None
        )
        cached_verified = (cache.get("verified") or {}).get(full_name) or {}
        if not version and cached_item:
            version = str(cached_item.get("version") or "")
        if not license_text:
            license_text = str(cached_verified.get("license") or (cached_item or {}).get("license") or "")

        verified = bool(bundle) or bool(cached_verified.get("verified") and not skill)

        offline = False
        message = ""
        if skill is None:
            if transient:
                # [EN] A network or rate-limit failure caused the read to fail;
                # [EN] report it explicitly as degraded operation.
                # [ZH] 网络或限流导致读取失败；明确标记为降级状态。
                offline = True
                message = _t("market.detail.skill_read_failed", error=error)
            else:
                # [EN] The network is available and the repository genuinely has
                # [EN] no skill.json; this is not an offline state.
                # [ZH] 网络正常，但仓库确实没有 skill.json；这不属于离线状态。
                message = _t("market.detail.no_valid_skill", error=error)
        elif not bundle:
            message = _t("market.detail.no_bundle")
        else:
            message = _t("market.detail.ok")

        if not license_text:
            # [EN] Repository metadata may contain a license. Query it only when
        # [EN] skill.json omits the license, saving an API call.
        # [ZH] 仓库元数据可能包含许可证。仅在 skill.json 未提供许可证时查询，
        # [ZH] 以节省一次 API 调用。
            meta = self._repo_meta(full_name)
            if meta:
                license_raw = meta.get("license")
                if isinstance(license_raw, dict):
                    candidate = str(license_raw.get("spdx_id") or license_raw.get("name") or "")
                    license_text = "" if candidate.upper() in ("NOASSERTION", "NONE") else candidate
                html_url = str(meta.get("html_url") or html_url)

        return {
            "full_name": full_name,
            "version": version,
            "html_url": html_url,
            "entry": entry,
            "has_prepare": has_prepare,
            "dependencies": dependencies,
            "readme_excerpt": readme,
            "skill_json": skill_json,
            "verified": verified,
            "license": license_text,
            # [EN] Additional fields for optional frontend display; required
            # [EN] response keys are unaffected.
            # [ZH] 供前端按需显示的额外字段；不影响要求的响应键。
            "offline": offline,
            "message": message,
            "min_version": _min_version_required(skill_json) if skill_json else "",
            "publish": _publish_sources(skill_json) if skill_json else {},
        }

    # ------------------------------------------------------------------
    # [EN] Public interface 3: install / [ZH] 公开接口 3：安装
    # ------------------------------------------------------------------

    def install(self, repo: str, on_progress: Optional[Callable[[str], None]] = None) -> dict:
        """
        [EN]
        Install a skill from its GitHub source.

        Returns ``{"ok", "id", "version", "path", "message"}``. Failures return
        ``ok=False`` with a user-facing message rather than raising.

        [ZH]
        从 GitHub 源码安装技能。

        返回 ``{"ok", "id", "version", "path", "message"}``。失败时返回
        ``ok=False`` 与可直接展示的消息，不抛出异常。
        """
        result = {"ok": False, "id": "", "version": "", "path": "", "message": ""}

        def _fail(text: str) -> dict:
            result["message"] = text
            self._emit(on_progress, text)
            return dict(result)

        if not self.enabled:
            return _fail(_t("market.install.disabled"))

        try:
            full_name = self._normalize_repo(repo)
        except MarketplaceError as exc:
            return _fail(str(exc))

        tmp_dir = ""
        try:
            self._emit(on_progress, _t("market.install.reading", repo=full_name))
            skill, error, transient = self._fetch_skill_json(full_name)
            if skill is None:
                return _fail(_t("market.install.skill_read_failed", repo=full_name, error=error))

            sources = _publish_sources(skill)
            bundle = _bundle_declared(skill)
            source_kind = "github"
            source_notes: List[str] = []
            if sources:
                source_kind = "npm" if "npm" in sources else "pypi"
                detail_text = _t("market.list_sep").join(
                    _t("market.install.publish_pkg", kind=k, version=v) for k, v in sources.items()
                )
                source_notes.append(
                    _t("market.install.publish_source", detail=detail_text, kind=source_kind)
                )
            if not bundle:
                if sources:
                    return _fail(
                        _t("market.install.only_publish_source", repo=full_name,
                           sources=_t("market.list_sep").join(sources))
                    )
                return _fail(_t("market.install.no_bundle", repo=full_name))

            with self._io_lock:
                pre_verified = bool(
                    (self._load_cache().get("verified") or {}).get(full_name, {}).get("verified")
                )
            if not pre_verified:
                source_notes.append(_t("market.install.not_preverified"))

            tmp_dir = tempfile.mkdtemp(prefix="echodesk-market-")
            self._emit(on_progress, _t("market.install.downloading"))
            tar_path, sha256, size = self._download_tarball(full_name, tmp_dir, on_progress)

            self._emit(on_progress, _t("market.install.verifying"))
            extract_dir = os.path.join(tmp_dir, "src")
            os.makedirs(extract_dir, exist_ok=True)
            self._safe_extract(tar_path, extract_dir)

            skill_root = self._locate_skill_root(extract_dir)
            if not skill_root:
                return _fail(_t("market.install.no_skill_in_archive"))
            root_skill = _read_json(os.path.join(skill_root, "skill.json"), None)
            if not isinstance(root_skill, dict):
                return _fail(_t("market.install.archive_bad_json"))
            root_bundle = _bundle_declared(root_skill)
            if not root_bundle:
                return _fail(_t("market.install.archive_no_bundle"))

            skill_name = str(root_skill.get("name") or root_skill.get("id") or full_name.split("/")[-1])
            skill_id = _safe_filename(root_skill.get("id") or root_skill.get("name") or full_name.split("/")[-1])
            version = str(root_skill.get("version") or "")
            license_text = _extract_license(root_skill)
            dependencies = _extract_dependencies(root_skill)
            loader = str(root_skill.get("loader") or root_skill.get("type") or "python")
            has_prepare = bool(root_skill.get("prepare"))

            # ---- [EN] Copy into the installation directory / [ZH] 复制到安装目录 ----
            os.makedirs(self.install_dir, exist_ok=True)
            dest = os.path.join(self.install_dir, skill_id)
            if not _is_within(dest, self.install_dir):
                return _fail(_t("market.install.id_escape", id=skill_id))
            self._emit(on_progress, _t("market.install.installing_to", path=dest))
            if os.path.exists(dest):
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(
                skill_root, dest,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )

            # ---- [EN] Register in the registry / [ZH] 注册到注册表 ----
            entry_record = {
                "id": skill_id,
                "version": version,
                "path": os.path.abspath(dest),
                "loader": loader,
                "repo": full_name,
                "installed_at": _iso_now(),
                "dependencies": dependencies,
                "signature": sha256,
                "license": license_text,
                "source": source_kind,
            }
            with self._io_lock:
                registry = self._load_registry()
                registry["skills"] = [
                    e for e in registry.get("skills") or []
                    if str(e.get("id")) != skill_id and str(e.get("repo")).lower() != full_name.lower()
                ]
                registry["skills"].append(entry_record)
                saved = self._save_registry(registry)

            # ---- [EN] Post-install verification / [ZH] 安装后校验 ----
            notes: List[str] = []
            if not saved:
                notes.append(_t("market.install.registry_write_failed", path=self.registry_file))
            else:
                reread = self._load_registry()
                found = next((e for e in reread.get("skills") or [] if e.get("id") == skill_id), None)
                if not found:
                    notes.append(_t("market.install.registry_reread_missing"))
                else:
                    reread_skill = _read_json(os.path.join(dest, "skill.json"), None)
                    if not isinstance(reread_skill, dict):
                        notes.append(_t("market.install.reread_skill_failed"))
                    else:
                        reread_name = str(reread_skill.get("name") or reread_skill.get("id") or "")
                        if reread_name and reread_name != skill_name:
                            notes.append(_t("market.install.name_mismatch", actual=reread_name,
                                            expected=skill_name))

            importable, import_note = self._check_entry(dest, _extract_entry(root_skill))
            if not importable:
                notes.append(_t("market.install.warning_prefix", note=import_note))
            else:
                self.log.debug("入口检查：%s", import_note)

            # ---- [EN] Dependency, prepare, license, and version notices; none are executed automatically ----
            # ---- [ZH] 依赖、prepare、许可证与版本兼容提示；均不自动执行 ----
            if dependencies:
                preview = _t("market.list_sep").join(dependencies[:10])
                more = (_t("market.install.deps_more", count=len(dependencies))
                        if len(dependencies) > 10 else "")
                notes.append(_t("market.install.deps_pending", preview=preview, more=more))
            if has_prepare:
                # [EN] Prepare scripts are never executed automatically. If
                # [EN] execution is added after explicit confirmation, the
                # [EN] sanitized environment must be used.
                # [ZH] prepare 脚本一律不自动执行。若未来在明确确认后执行，必须使用
                # [ZH] 已清洗的环境变量。
                removed = len(os.environ) - len(self._sanitized_env())
                notes.append(
                    _t("market.install.prepare_skipped")
                    + (_t("market.install.prepare_env_note", count=removed) if removed
                       else _t("market.install.prepare_env_note_plain"))
                )
            if not license_text:
                notes.append(_t("market.install.no_license"))

            min_required = _min_version_required(root_skill)
            if min_required:
                need = _version_tuple(min_required)
                have = _version_tuple(_APP_VERSION)
                if need and have and need > have:
                    # [EN] As documented at module level, report the version
                    # [EN] mismatch without blocking an explicitly confirmed install.
                    # [ZH] 按模块文档说明，仅报告版本不匹配，不阻止用户明确确认的安装。
                    notes.append(_t("market.install.min_version", required=min_required,
                                    current=_APP_VERSION))

            self._emit(on_progress, _t("market.install.done_emit", name=skill_name))
            message_parts = [_t("market.install.success", name=skill_name,
                                version=version or _t("market.install.version_undeclared"),
                                path=dest)]
            message_parts.extend(source_notes)
            message_parts.extend(notes)
            message_parts.append(_t("market.install.origin", repo=full_name, sha256=sha256[:16]))
            result.update({
                "ok": True,
                "id": skill_id,
                "version": version,
                "path": os.path.abspath(dest),
                "message": " ".join(message_parts),
            })
            return dict(result)
        except MarketplaceError as exc:
            return _fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底：安装失败也要给可展示的说明
            self.log.warning("安装 %s 时发生未预期错误：%s", repo, exc, exc_info=True)
            return _fail(_t("market.install.unexpected", kind=type(exc).__name__, error=exc))
        finally:
            # [EN] Temporary files are cleaned in finally so that no residue
            # [EN] remains whether installation succeeds or fails.
            # [ZH] 临时文件在 finally 中清理，无论安装成功或失败都不留残留。
            if tmp_dir and os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _download_tarball(
        self,
        repo: str,
        tmp_dir: str,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, str, int]:
        """
        [EN]
        Download a source tarball to a temporary directory and return
        ``(path, sha256, byte_count)``.

        The size limit is 50 MB. Content-Length is checked first, and the
        running size is enforced while streaming in case the server omits it.

        [ZH]
        将源码 tarball 下载到临时目录，返回 ``(路径, sha256, 字节数)``。

        大小上限为 50 MB。首先检查 Content-Length，并在流式下载过程中持续计数，
        以应对服务端未提供长度的情况。
        """
        url = f"{self.codeload_base}/{repo}/tar.gz/HEAD"
        path = os.path.join(tmp_dir, "source.tar.gz")
        try:
            resp = self._request(url, streaming=True, timeout=max(self.timeout, 20.0))
        except MarketplaceError as exc:
            raise MarketplaceError(_t("market.tarball.download_failed", error=exc)) from exc
        if resp.status_code != 200:
            raise MarketplaceError(_t("market.tarball.http_error", status=resp.status_code))

        declared = resp.headers.get("Content-Length")
        try:
            if declared and int(declared) > MAX_TARBALL_BYTES:
                raise MarketplaceError(
                    _t("market.tarball.too_large_bytes", mb=int(declared) // 1024 // 1024)
                )
        except ValueError:
            pass

        digest = hashlib.sha256()
        size = 0
        last_pct = -1
        try:
            with open(path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_TARBALL_BYTES:
                        raise MarketplaceError(_t("market.tarball.too_large"))
                    digest.update(chunk)
                    fh.write(chunk)
                    pct = int(size * 100 / MAX_TARBALL_BYTES)
                    if pct // 10 != last_pct // 10:
                        last_pct = pct
                        self._emit(on_progress, _t("market.tarball.progress", kb=size // 1024))
        except MarketplaceError:
            raise
        except requests.exceptions.RequestException as exc:
            raise MarketplaceError(_t("market.tarball.network_interrupted",
                                      kind=type(exc).__name__)) from exc
        except OSError as exc:
            raise MarketplaceError(_t("market.tarball.write_failed", error=exc)) from exc
        finally:
            resp.close()

        if size == 0:
            raise MarketplaceError(_t("market.tarball.empty"))
        return path, digest.hexdigest(), size

    @staticmethod
    def _safe_extract(tar_path: str, dest: str) -> None:
        """
        [EN]
        Safely extract a tarball, rejecting absolute paths, ``..`` traversal,
        out-of-tree links, and device files.

        ``tarfile``'s ``filter="data"`` is applied as a second layer on
        Python 3.12 and later. The first layer is the explicit per-member check
        in this function, which produces a specific localized error and also
        works on older Python versions.

        [ZH]
        安全解压 tarball，拒绝绝对路径、``..`` 穿越、越界链接与设备文件。

        在 Python 3.12 及以上版本中，使用 ``tarfile`` 的 ``filter="data"`` 作为
        第二层防护。第一层是本函数对每个成员的显式检查，可给出明确的本地化错误，
        并且在较旧 Python 版本上同样有效。
        """
        dest_real = os.path.realpath(dest)
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                for member in tar.getmembers():
                    name = str(member.name or "").replace("\\", "/")
                    if not name or name == ".":
                        continue
                    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                        raise MarketplaceError(_t("market.extract.abs_member", name=name))
                    if name.startswith("~"):
                        raise MarketplaceError(_t("market.extract.suspicious_member", name=name))
                    parts = [p for p in name.split("/") if p not in ("", ".")]
                    if any(p == ".." for p in parts):
                        raise MarketplaceError(_t("market.extract.parent_member", name=name))
                    target = os.path.realpath(os.path.join(dest_real, *parts)) if parts else dest_real
                    if not _is_within(target, dest_real):
                        raise MarketplaceError(_t("market.extract.escape_member", name=name))
                    if member.isdev() or member.isfifo():
                        raise MarketplaceError(_t("market.extract.device_member", name=name))
                    if member.issym() or member.islnk():
                        link = str(member.linkname or "").replace("\\", "/")
                        if link.startswith("/") or re.match(r"^[A-Za-z]:", link):
                            raise MarketplaceError(_t("market.extract.abs_link", name=name, link=link))
                        link_parts = [p for p in f"{os.path.dirname(name)}/{link}".split("/") if p not in ("", ".")]
                        if any(p == ".." for p in link_parts):
                            resolved = os.path.realpath(os.path.join(dest_real, os.path.dirname(name), link))
                            if not _is_within(resolved, dest_real):
                                raise MarketplaceError(
                                    _t("market.extract.escape_link", name=name, link=link)
                                )
                try:
                    tar.extractall(path=dest, filter="data")
                except TypeError:  # 老版本 Python 不支持 filter 参数
                    tar.extractall(path=dest)
        except MarketplaceError:
            raise
        except tarfile.TarError as exc:
            raise MarketplaceError(_t("market.extract.corrupt", error=exc)) from exc
        except OSError as exc:
            raise MarketplaceError(_t("market.extract.io_failed", error=exc)) from exc

    @staticmethod
    def _locate_skill_root(base: str) -> str:
        """
        [EN]
        Locate the extracted skill root. ``skill.json`` may be at the archive
        root or one or two directory levels down.

        [ZH]
        定位解压后的技能根目录。``skill.json`` 可能位于归档根目录，也可能位于
        一到两层子目录中。
        """
        if os.path.isfile(os.path.join(base, "skill.json")):
            return base
        try:
            children = [d for d in sorted(os.listdir(base)) if os.path.isdir(os.path.join(base, d))]
        except OSError:
            return ""
        for child in children:
            path = os.path.join(base, child)
            if os.path.isfile(os.path.join(path, "skill.json")):
                return path
        for child in children:
            path = os.path.join(base, child)
            try:
                subs = [d for d in sorted(os.listdir(path)) if os.path.isdir(os.path.join(path, d))]
            except OSError:
                continue
            for sub in subs:
                deep = os.path.join(path, sub)
                if os.path.isfile(os.path.join(deep, "skill.json")):
                    return deep
        return ""

    @staticmethod
    def _check_entry(install_path: str, entry: str) -> Tuple[bool, str]:
        """
        [EN]
        Validate the entry file after installation. This is a loadability check
        only; no skill code is executed.

        * File entry: use ``importlib.util.spec_from_file_location`` to confirm
          that a loader can be constructed.
        * Package entry: confirm that ``__init__.py`` exists.

        [ZH]
        安装后校验入口文件。仅检查可加载性，不执行任何技能代码。

        * 入口为文件：使用 ``importlib.util.spec_from_file_location`` 确认可构建
          加载器。
        * 入口为包目录：确认存在 ``__init__.py``。
        """
        if not entry:
            return False, _t("market.entry.undeclared")
        rel = str(entry).replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        target = os.path.join(install_path, *parts) if parts else install_path
        if not _is_within(target, install_path):
            return False, _t("market.entry.escape", entry=entry)
        if os.path.isdir(target):
            if os.path.isfile(os.path.join(target, "__init__.py")):
                return True, _t("market.entry.package_ok", entry=entry)
            return False, _t("market.entry.package_no_init", entry=entry)
        if not os.path.isfile(target):
            return False, _t("market.entry.file_missing", entry=entry)
        try:
            spec = importlib.util.spec_from_file_location(
                f"_echodesk_entry_check_{_safe_filename(os.path.basename(target))}", target
            )
        except Exception as exc:  # noqa: BLE001
            return False, _t("market.entry.loader_failed", entry=entry, kind=type(exc).__name__)
        if spec is None or spec.loader is None:
            return False, _t("market.entry.not_importable", entry=entry)
        return True, _t("market.entry.loadable", entry=entry)

    # ------------------------------------------------------------------
    # [EN] Public interface 4: uninstall / [ZH] 公开接口 4：卸载
    # ------------------------------------------------------------------

    def uninstall(self, skill_id: str) -> dict:
        """
        [EN]
        Uninstall a skill: remove its registry entry, delete its installation
        directory, and re-read the registry to confirm no residue remains.

        [ZH]
        卸载技能：移除注册表条目、删除安装目录，并回读注册表确认无残留。
        """
        raw_id = str(skill_id or "").strip()
        if not raw_id:
            return {"ok": False, "message": _t("market.uninstall.missing_id")}

        key = _safe_filename(raw_id, "")
        with self._io_lock:
            registry = self._load_registry()
            entries = list(registry.get("skills") or [])
            matched = [
                e for e in entries
                if str(e.get("id")) == raw_id
                or str(e.get("id")) == key
                or str(e.get("repo") or "").lower() == raw_id.lower()
            ]
            if not matched:
                return {
                    "ok": False,
                    "message": _t("market.uninstall.not_found", id=raw_id),
                }
            matched_ids = {str(e.get("id")) for e in matched}
            registry["skills"] = [e for e in entries if str(e.get("id")) not in matched_ids]
            saved = self._save_registry(registry)

        # [EN] Delete installation directories. Only paths inside install_dir
        # [EN] are eligible, preventing accidental deletion elsewhere.
        # [ZH] 删除安装目录。仅允许 install_dir 内的路径，防止误删其他位置。
        removed_dirs: List[str] = []
        blocked_dirs: List[str] = []
        for entry in matched:
            path = str(entry.get("path") or "")
            if not path:
                path = os.path.join(self.install_dir, str(entry.get("id") or ""))
            if not _is_within(path, self.install_dir):
                blocked_dirs.append(path)
                continue
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                removed_dirs.append(path)

        # [EN] Re-read the registry to confirm removal.
        # [ZH] 回读注册表以确认移除。
        with self._io_lock:
            reread = self._load_registry()
            residual = [e for e in reread.get("skills") or [] if str(e.get("id")) in matched_ids]

        if residual:
            return {
                "ok": False,
                "message": _t("market.uninstall.residual", path=self.registry_file,
                              count=len(residual)),
            }
        if not saved:
            return {
                "ok": False,
                "message": _t("market.uninstall.registry_write_failed", path=self.registry_file),
            }
        leftover = [p for p in removed_dirs if os.path.isdir(p)]
        if leftover:
            return {
                "ok": False,
                "message": _t("market.uninstall.dir_remove_failed",
                              dirs=_t("market.list_sep").join(leftover)),
            }

        names = _t("market.list_sep").join(sorted(matched_ids))
        message = _t("market.uninstall.done", names=names)
        if removed_dirs:
            message += _t("market.uninstall.dir_removed",
                          dirs=_t("market.list_sep").join(removed_dirs))
        if blocked_dirs:
            message += _t("market.uninstall.dir_blocked",
                          dirs=_t("market.list_sep").join(blocked_dirs))
        message += _t("market.uninstall.cache_kept")
        self.log.info("已卸载技能：%s", names)
        return {"ok": True, "message": message}

    # ------------------------------------------------------------------
    # [EN] Public interface 5: installed / [ZH] 公开接口 5：已安装列表
    # ------------------------------------------------------------------

    def installed(self) -> list:
        """
        [EN]
        Return installed skills as
        ``[{id, version, path, loader, installed_at}, ...]``.

        [ZH]
        返回已安装技能列表：
        ``[{id, version, path, loader, installed_at}, ...]``。
        """
        try:
            with self._io_lock:
                registry = self._load_registry()
            out: List[Dict[str, Any]] = []
            for entry in registry.get("skills") or []:
                path = str(entry.get("path") or "")
                if path and not os.path.isdir(path):
                    self.log.warning("注册表中的技能 %s 目录已不存在：%s", entry.get("id"), path)
                    continue
                out.append({
                    "id": str(entry.get("id") or ""),
                    "version": str(entry.get("version") or ""),
                    "path": path,
                    "loader": str(entry.get("loader") or "python"),
                    "installed_at": str(entry.get("installed_at") or ""),
                })
            out.sort(key=lambda e: e.get("installed_at") or "", reverse=True)
            return out
        except Exception as exc:  # noqa: BLE001
            self.log.warning("读取已安装技能列表失败：%s", exc)
            return []

    # ------------------------------------------------------------------
    # [EN] Public interface 6: status / [ZH] 公开接口 6：状态
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """
        [EN]
        Return marketplace status:
        ``{"enabled", "state", "cached", "synced_at"}``.

        ``state`` values come from the language pack under ``market.state.*``
        and are directly displayable (disabled, not synchronized, synchronized
        with no matches, ready, cache expired, or unknown).

        The method reads local files only and performs no network request, so
        the settings interface receives a result immediately.

        [ZH]
        返回市场状态：
        ``{"enabled", "state", "cached", "synced_at"}``。

        ``state`` 取值来自语言包的 ``market.state.*``，可直接展示（已禁用、未同步、
        已同步但无匹配、已就绪、缓存已过期或未知）。

        该方法仅读取本地文件，不发起网络请求，使设置界面打开时立即获得结果。
        """
        try:
            with self._io_lock:
                cache = self._load_cache()
            cached = len(cache.get("items") or [])
            synced_at = str(cache.get("synced_at") or "")
            if not self.enabled:
                state = _t("market.state.disabled")
            elif not synced_at:
                state = _t("market.state.unsynced")
            elif cached <= 0:
                # [EN] Synchronization succeeded but the topic has no
                # [EN] repositories; this is distinct from "never synchronized".
                # [ZH] 同步成功但该 topic 下没有任何仓库；这与“从未同步”是两种
                # [ZH] 不同状态。
                state = _t("market.state.synced_empty")
            elif self._cache_is_fresh(cache):
                state = _t("market.state.ready")
            else:
                state = _t("market.state.stale")
            return {
                "enabled": bool(self.enabled),
                "state": state,
                "cached": int(cached),
                "synced_at": synced_at,
            }
        except Exception as exc:  # noqa: BLE001
            self.log.warning("读取市场状态失败：%s", exc)
            return {"enabled": bool(self.enabled), "state": _t("market.state.unknown"),
                    "cached": 0, "synced_at": ""}
