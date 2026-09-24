"""EchoDesk 技能/插件市场后端模块（任务五 5.1~5.6）。

架构
====
本模块把市场功能分成两层，边界清晰、便于单独测试：

* **数据源层**：GitHub Search API（统一 topic ``echodesk-plugin``）负责发现仓库；
  ``raw.githubusercontent.com``（首选）与 GitHub contents API（兜底）负责读取
  ``skill.json`` 与 ``README.md``；``codeload.github.com`` 负责下载源码 tarball。
  数据源只认 GitHub，不接受用户传入任意 URL／其它主机。
* **服务层**：:class:`Marketplace` 对外提供 ``search`` / ``detail`` / ``install`` /
  ``uninstall`` / ``installed`` / ``status`` 六个接口，供 ``main.py`` 的 JSON 行协议
  与前端"技能市场"界面调用。所有面向用户的文案都是简体中文。

缓存策略
========
``cache_file``（默认 ``market_cache.json``）保存一份完整快照：

* ``items``：仓库条目列表（含名称、描述、Star、最近推送时间、许可证、topic、头像等）。
* ``verified``：``skill.json`` 验证结果，**键为完整仓库名** ``owner/name``，
  这样每次刷新不会重复请求同一个仓库的 ``skill.json``。
* ``synced_at`` / ``last_pushed_at``：上次同步时间与上次见过的最大 ``pushed_at``。

* 首次加载（缓存为空）全量拉取前 100 个匹配仓库（``per_page=100``，只翻 1 页）。
* 之后的刷新走**增量更新**：把 ``pushed:>=<上次最大 pushed_at 的日期>`` 追加进查询，
  把结果与缓存按 ``full_name`` 合并（同一仓库取 ``pushed_at`` 较新的那份）。
  说明：GitHub 仓库搜索没有 ``pushed_at:`` 这个限定符，只有 ``pushed:``（接受日期或
  时间戳），因此这里按真实可用的 ``pushed:>=YYYY-MM-DD`` 实现，而不是照抄
  ``pushed_at:>=``——照抄会得到 422 校验错误，功能不可用。
  若增量结果已经打满单页上限（100 条），说明可能还有遗漏，此时自动退化为全量刷新。
* ``cache_ttl_hours`` 内直接返回缓存（``offline=False``、``message="缓存"``，不发任何请求）；
  过期或缓存为空时才尝试刷新。
* **离线优先**：网络不可用、超时、限流（HTTP 403/429）等一律不抛给用户，而是降级为
  缓存快照并置 ``offline=True``，在 ``message`` 里说明原因。失败时**不写入** ``synced_at``，
  避免把一次失败固化成"已同步"。

安全设计（任务书 5.5）
=====================
* 只从统一 topic 发现技能；``detail`` / ``install`` 接受 ``owner/name`` 形式（高级模式），
  但会做严格正则校验并明确拒绝 URL 与其它主机。
* 安装前先读取 ``skill.json`` 原文（``detail`` 也返回），供前端展示依赖与权限。
* tarball 解压前逐条检查成员路径：拒绝绝对路径、``..``、越界符号链接与设备文件，
  防止目录穿越；再用 ``filter="data"`` 二次加固。
* **不自动执行 prepare 脚本**、**不自动 pip 安装依赖**：只在 message 中列出并提示用户确认。
  :meth:`Marketplace._sanitized_env` 提供清洗后的环境变量副本（移除名称含
  KEY/SECRET/TOKEN/PASSWORD 的变量），供将来真要执行第三方 prepare 脚本时使用。
* 把 tarball 的 sha256 与 ``skill.json`` 的 license 记入注册表，便于签名校验与许可证展示。
* 版本兼容：``skill.json`` 声明 ``echodesk_min_version`` / ``min_version`` 时与
  ``version.APP_VERSION`` 比较；不兼容只在 message 中提示，**仍然允许安装**
  （市场不应该在用户明确点击后单方面阻止安装，提示风险比直接拒绝更合理）。
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

# 后端各模块是"平铺"导入（main.py 里 ``from config import ...``），这里把本文件所在目录
# 补进 sys.path，保证在任意工作目录下被导入时仍能解析 version 模块。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:  # pragma: no cover - 环境相关
    from version import APP_VERSION as _APP_VERSION
except Exception:  # noqa: BLE001
    _APP_VERSION = "v0.2.0"

try:  # pragma: no cover - urllib3 随 requests 一起安装，缺失时退化为不重试
    from urllib3.util.retry import Retry as _Retry
except Exception:  # noqa: BLE001
    _Retry = None  # type: ignore[assignment]

# 多语言（任务三）：照 main.py 的约定，i18n 只在本文件自己的导入区引用，
# 且 i18n 反向依赖 config 是惰性的（取文案时才读 program_root），不会成环。
from i18n import t as _t


__all__ = ["MarketplaceError", "Marketplace", "MAX_TARBALL_BYTES"]

log = logging.getLogger("EchoDesk.Marketplace")


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 请求超时（秒）。所有网络请求都必须带上超时，避免界面卡死。
REQUEST_TIMEOUT = 15
#: 验证 skill.json 时用的短超时，避免一次刷新被慢网络拖太久。
VERIFY_TIMEOUT = 10
#: 下载 tarball 的最大字节数（50MB）。
MAX_TARBALL_BYTES = 50 * 1024 * 1024
#: 验证线程池并发数。
MAX_VERIFY_WORKERS = 8
#: 单次刷新中等待并发验证的总预算（秒）。
VERIFY_DEADLINE_REFRESH = 45.0
#: 命中有效缓存时只做一次"顺手"验证，预算更短，保证界面响应。
VERIFY_DEADLINE_CACHE = 8.0
#: 首次全量拉取的条数与页数上限。
INITIAL_FETCH_PER_PAGE = 100
INITIAL_FETCH_PAGES = 1
#: 缓存条目上限（初始 100 条 + 增量合并，超出时按最近推送时间保留）。
MAX_CACHE_ITEMS = 200
#: 验证结果缓存条数上限，防止无限增长。
MAX_VERIFIED_CACHE = 800
#: README 摘要长度（字符）。
README_EXCERPT_CHARS = 800
#: 默认 User-Agent。
USER_AGENT = "EchoDesk/0.2.0"

#: 仓库全名格式（任务书 5.5：高级模式也只接受 owner/name）。
REPO_FULL_NAME_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
#: 技能 id 安全化：只保留字母数字与 -_. 
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9\-_.]")
#: Windows 保留设备名，避免把技能目录建成 CON/PRN 之类。
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
#: 环境变量名里出现这些子串（大小写不敏感）就剔除，交给第三方脚本会泄露密钥。
_SENSITIVE_ENV_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")

#: 缓存条目字段（不带 verified/installed，那两个在返回时计算）。
ITEM_FIELDS = (
    "full_name", "name", "description", "stars", "pushed_at",
    "license", "topics", "owner_avatar", "version", "html_url",
)


# ---------------------------------------------------------------------------
# 模块级小工具（纯函数，便于单独测试）
# ---------------------------------------------------------------------------

def _cfg_get(cfg: Any, name: str, default: Any) -> Any:
    """按属性或字典键读取配置，缺失/为 None 时回落到默认值。"""
    if isinstance(cfg, dict):
        value = cfg.get(name, default)
    else:
        value = getattr(cfg, name, default)
    return default if value is None else value


def _iso_now() -> str:
    """当前本地时间的 ISO 字符串（带时区偏移）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(text: Any) -> Optional[datetime]:
    """解析 ISO 时间字符串，失败返回 None（缓存被手工改坏时也不能崩）。"""
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
    """容错读取 JSON：文件不存在或已损坏都返回 default（视为空并等待重建）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except Exception as exc:  # noqa: BLE001 - 文件损坏不能影响程序启动
        log.warning("读取 %s 失败（视为空，将在下次写入时重建）：%s", path, exc)
        return default


def _write_json(path: str, data: Any) -> bool:
    """原子写 JSON（先写临时文件再替换），失败只记日志不影响调用方。"""
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
    """返回环境变量副本，剔除名称含 KEY/SECRET/TOKEN/PASSWORD 的项。"""
    cleaned: Dict[str, str] = {}
    for name, value in env.items():
        upper = str(name).upper()
        if any(marker in upper for marker in _SENSITIVE_ENV_MARKERS):
            continue
        cleaned[name] = value
    return cleaned


def _safe_filename(raw: Any, fallback: str = "skill") -> str:
    """把技能 id 安全化成目录名：只保留字母数字与 ``-_.``。"""
    text = SAFE_ID_RE.sub("", str(raw or "").strip())
    text = text.strip(".")  # 去掉首尾的点，避免 "." / ".."
    if not text:
        text = SAFE_ID_RE.sub("", str(fallback or "").strip()).strip(".") or "skill"
    if text.upper() in _WINDOWS_RESERVED:
        text = f"skill_{text}"
    return text[:80]


def _is_within(path: str, parent: str) -> bool:
    """判断 path 是否位于 parent 目录内（防目录穿越）。"""
    try:
        real_path = os.path.realpath(os.path.abspath(path))
        real_parent = os.path.realpath(os.path.abspath(parent))
    except Exception:  # noqa: BLE001
        return False
    if real_path == real_parent:
        return True
    try:
        return os.path.commonpath([real_path, real_parent]) == real_parent
    except ValueError:  # 不同盘符
        return False


def _version_tuple(text: Any) -> Tuple[int, ...]:
    """把 "v0.2.0" / "0.2" 之类的版本号转成可比较的数字元组。"""
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:4]) if nums else ()


def _as_str_list(value: Any) -> List[str]:
    """把 list / dict / 字符串统一拍平成字符串列表（兼容 skill.json 的多种写法）。"""
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
                    # {"requests": null} / {"requests": true} 形式：只取依赖名
                    text = str(key).strip()
                    if text:
                        out.append(text)
                elif item is False:
                    continue
                else:
                    # {"requests": ">=2.0"} 视作版本约束拼成 requests>=2.0
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
    # 去重但保持顺序
    seen = set()
    result = []
    for item in out:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _extract_license(skill: Dict[str, Any]) -> str:
    """读取 skill.json 里的 license 字段（兼容字符串与对象两种写法）。"""
    value = skill.get("license")
    if isinstance(value, dict):
        value = value.get("spdx_id") or value.get("type") or value.get("name") or ""
    return str(value or "").strip()


def _bundle_declared(skill: Dict[str, Any]) -> str:
    """严格读取 ``echodesk.bundle`` 字段（任务书 5.2 的验证依据）。

    同时兼容两种写法：``{"echodesk": {"bundle": "main.py"}}`` 与
    ``{"echodesk.bundle": "main.py"}``；``bundle`` 是对象时再取其 path/main/entry。
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
    """读取入口文件路径：优先 echodesk.bundle，其次 entry/main（兼容写法）。"""
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
    """读取依赖清单，兼容 list 与 dict 两种写法，缺失时回落到 requirements。"""
    deps = _as_str_list(skill.get("dependencies"))
    if not deps:
        deps = _as_str_list(skill.get("requirements"))
    return deps


def _min_version_required(skill: Dict[str, Any]) -> str:
    """读取技能声明的最低 EchoDesk 版本（兼容 echodesk_min_version / min_version）。"""
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
    """解析 ``publish.npm`` / ``publish.pypi``（以及顶层 npm/pypi）声明。"""
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
    """把（可能被手工改坏的）缓存条目规整成固定字段的字典。"""
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
    """本地排序：GitHub 的"最佳匹配"无法离线复现，用「星数 + 最近推送」近似。"""
    if sort_key == "updated":
        return sorted(
            items,
            key=lambda i: (str(i.get("pushed_at") or ""), int(i.get("stars") or 0)),
            reverse=True,
        )
    # "stars" 与 "best" 都按星数降序，同分按最近推送倒序
    return sorted(
        items,
        key=lambda i: (int(i.get("stars") or 0), str(i.get("pushed_at") or "")),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class MarketplaceError(Exception):
    """市场模块的业务异常：message 一定是可直接展示给用户的中文说明。"""


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------

class Marketplace:
    """技能市场服务层（搜索 / 详情 / 安装 / 卸载 / 已安装列表 / 状态）。

    所有公开方法都保证「不因一次网络抖动抛异常」：网络类问题一律降级为
    中文 message + 缓存快照。只有明显属于调用方错误的情况（如仓库名格式非法）
    才会抛 :class:`MarketplaceError`。
    """

    def __init__(self, cfg: Any, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.log = logger or log

        # ---- 配置读取（全部 getattr/字典容错，缺失就用文档里的默认值） ----
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
        # 源码读取与 tarball 下载的地址也允许被覆盖（便于离线/镜像测试），默认官方地址
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

        # ---- 路径解析：相对路径按"后端工作目录"（即当前工作目录）解析 ----
        self.cache_file: str = self._resolve_path(_cfg_get(cfg, "cache_file", "market_cache.json"))
        self.install_dir: str = self._resolve_path(_cfg_get(cfg, "install_dir", "skills"))
        self.registry_file: str = self._resolve_path(_cfg_get(cfg, "registry_file", "skills_registry.json"))

        # ---- 内部状态 ----
        self._io_lock = threading.RLock()        # 保护缓存/注册表的读-改-写
        self._refresh_lock = threading.Lock()    # 同一时刻只允许一个线程刷新缓存
        self._tls = threading.local()            # 每线程一个 requests.Session

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_path(value: Any) -> str:
        """把配置里的（可能相对的）路径解析成绝对路径。"""
        raw = os.path.expanduser(str(value or "").strip())
        if not raw:
            raw = "."
        if not os.path.isabs(raw):
            raw = os.path.join(os.getcwd(), raw)
        return os.path.abspath(raw)

    def _sanitized_env(self) -> Dict[str, str]:
        """返回当前环境变量的**副本**，并移除所有名称包含 KEY、SECRET、TOKEN、
        PASSWORD（大小写不敏感）的变量。

        用途（任务书 5.5）：第三方技能的 ``prepare`` 脚本以本地账户权限运行，
        若把环境变量原样传进去，等于把 API Key / 云凭证 / 访问令牌一并交给它。
        当前实现**不会自动执行** ``prepare`` 脚本，本函数先按安全约定实现并对外可用，
        将来真要在用户确认后执行时，必须用本函数的返回值作为子进程环境。
        """
        return _clean_env(dict(os.environ))

    def _session(self) -> requests.Session:
        """取当前线程的 requests.Session（Session 不是严格线程安全的，故用线程局部）。"""
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
        """轻度重试：只重试连接错误与 5xx，绝不重试 403/429（限流重试只会更糟）。"""
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
        """构造请求头；有 token 时加 Authorization 以提高 API 限额。"""
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
        """发起 GET 请求。

        只有网络层故障与"整段请求不可再用"的状态码（限流/未授权/5xx）才抛
        :class:`MarketplaceError`；404 等业务状态码原样返回给调用方判断。
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
        """判断是否命中 GitHub 限流。"""
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
        """截断 URL 用于日志/提示，避免把超长地址塞进界面。"""
        return url if len(url) <= limit else url[: limit - 1] + "…"

    def _emit(self, on_progress: Optional[Callable[[str], None]], text: str) -> None:
        """上报安装阶段；回调异常绝不能影响安装流程。"""
        self.log.info("%s", text)
        if on_progress is None:
            return
        try:
            on_progress(str(text))
        except Exception as exc:  # noqa: BLE001
            self.log.debug("进度回调异常（已忽略）：%s", exc)

    # ------------------------------------------------------------------
    # 缓存：读写
    # ------------------------------------------------------------------

    def _empty_cache(self) -> Dict[str, Any]:
        return {"schema": 1, "synced_at": "", "last_pushed_at": "", "items": [], "verified": {}}

    def _load_cache(self) -> Dict[str, Any]:
        """读取缓存文件；损坏或缺失时返回空缓存（不抛异常）。"""
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
        """落盘缓存：条目与验证记录都做数量上限，避免文件无限增长。"""
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
        """缓存是否在有效期（TTL）内。TTL<=0 视为永不过期无效，便于强制刷新测试。"""
        if self.cache_ttl_hours <= 0:
            return False
        synced = _parse_iso(cache.get("synced_at"))
        if synced is None:
            return False
        age = (datetime.now(timezone.utc) - synced).total_seconds()
        return age < self.cache_ttl_hours * 3600

    def _verified_entry_valid(self, entry: Optional[Dict[str, Any]]) -> bool:
        """验证结果是否仍在有效期内（复用 cache_ttl_hours）。"""
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
    # 缓存：刷新与合并
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_items(
        old_items: Sequence[Dict[str, Any]], new_items: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """按 full_name 合并条目：同一仓库保留 ``pushed_at`` 较新的那份。"""
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
            # 合并策略：新数据覆盖旧数据，但字段缺失时沿用旧值
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
        """调用 GitHub Search API，返回 (规整后的条目列表, total_count)。

        查询统一是 ``topic:<skill_topic> <附加条件>``；``extra_query`` 既可以是用户
        关键词，也可以是 ``pushed:>=YYYY-MM-DD`` 这类限定符（增量刷新用）。
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
        # sort 映射："stars"→stars、"updated"→updated、"best"→不传（GitHub 最佳匹配）
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
        """把 GitHub 仓库 JSON 映射成前端绑定的固定字段。"""
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
        """刷新缓存。

        返回 ``(是否成功, message, offline)``；失败时 message 已是可直接展示的原因。
        刷新策略见模块顶部 docstring：首次全量 100 条，之后 ``pushed:>=`` 增量合并。
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
                        # 增量结果打满单页上限，可能还有遗漏 → 退化为全量刷新
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
            # 网络/限流：不改动 synced_at，直接降级
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

        # 并发验证 skill.json（结果会写回缓存）
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
        """确保缓存可用；返回 ``(成功拿到可用缓存, message, offline)``。"""
        cache = self._load_cache()
        if self._cache_is_fresh(cache):
            return True, _t("market.cache.hit"), False
        if not self.auto_update_check:
            # 配置关闭了自动更新检查：只读缓存，不联网
            if cache.get("items"):
                return True, _t("market.cache.offline_disabled"), True
            return False, _t("market.cache.disabled_empty"), True
        with self._refresh_lock:
            cache = self._load_cache()
            if self._cache_is_fresh(cache):
                # 其它线程刚刷新过
                return True, _t("market.cache.hit"), False
            return self._refresh()

    # ------------------------------------------------------------------
    # skill.json / README 读取与验证
    # ------------------------------------------------------------------

    def _fetch_skill_json(self, repo: str) -> Tuple[Optional[Dict[str, Any]], str, bool]:
        """读取仓库根目录的 skill.json。

        返回 ``(内容, 错误说明, 是否属于临时故障)``；``临时故障=True`` 表示这是网络/
        限流问题，结果不应被写进缓存（否则一次抖动会被记成"没有 skill.json"）。
        优先 ``raw.githubusercontent.com/<repo>/HEAD/skill.json``，失败再退回 contents API。
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
        """解析 skill.json 字节内容：成功返回 dict，失败返回可直接展示的错误说明字符串。"""
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
        """验证单个仓库；临时性故障返回 None（表示"本次不写入缓存"）。"""
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
        """用线程池并发验证一批仓库，返回 ``full_name -> 验证信息`` 的映射。

        * 已有有效缓存结果的仓库不会重复请求。
        * 整体等待时间受 ``deadline`` 限制（默认 45 秒），超时未完成的仓库本轮跳过，
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
            # 不等待仍在飞行中的请求，保证调用方及时返回；它们受各自 10 秒超时约束
            executor.shutdown(wait=False, cancel_futures=True)

        if results:
            with self._io_lock:
                cache = self._load_cache()
                vmap = dict(cache.get("verified") or {})
                vmap.update(results)
                cache["verified"] = vmap
                # 顺手把 skill.json 里的版本号回填到条目，便于卡片直接展示版本
                for item in cache.get("items") or []:
                    info = results.get(item.get("full_name"))
                    if info and info.get("version"):
                        item["version"] = str(info["version"])
                self._save_cache(cache)
        return vmap

    def _fetch_readme(self, repo: str, limit: int = README_EXCERPT_CHARS) -> str:
        """读取 README 摘要（README.md 前 800 字），优先 HEAD 的 raw 地址。"""
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
        # 兜底：contents API（可能因文件过大返回需要 base64 解码的内容，这里只取纯文本）
        api_url = f"{self.api_base}/repos/{repo}/readme"
        try:
            resp = self._request(api_url, accept="application/vnd.github.raw")
            if resp.status_code == 200:
                return resp.content.decode("utf-8", errors="replace")[:limit]
        except MarketplaceError as exc:
            self.log.debug("contents API 读取 README 失败：%s", exc)
        return ""

    def _repo_meta(self, repo: str) -> Dict[str, Any]:
        """读取仓库元信息（许可证、HTML 地址、默认分支）；失败返回空字典。"""
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
    # 仓库名校验
    # ------------------------------------------------------------------

    def _normalize_repo(self, repo: Any) -> str:
        """校验并规整 ``owner/name``；拒绝 URL、其它主机与目录穿越写法。"""
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
    # 注册表
    # ------------------------------------------------------------------

    def _empty_registry(self) -> Dict[str, Any]:
        return {"schema": 1, "skills": []}

    def _load_registry(self) -> Dict[str, Any]:
        """读取注册表；损坏时视为空并在下次保存时重建。"""
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
        """返回"已安装"匹配用的键集合（仓库名与技能 id）。"""
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
    # 公开接口 1：search
    # ------------------------------------------------------------------

    def search(self, keyword: str = "", sort: str = "best", page: int = 1) -> dict:
        """按 topic 搜索技能仓库。

        sort: ``"best"`` | ``"stars"`` | ``"updated"``。
        返回 ``{"items", "page", "per_page", "total_count", "total_pages", "offline", "message"}``。
        每个 item 固定包含：full_name / name / description / stars / pushed_at / license /
        topics / owner_avatar / verified / version / installed / html_url。
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
        """关键词搜索：优先走 GitHub 在线搜索（不在本地做关键词过滤）。"""
        try:
            items, total = self._github_search(keyword, sort_key, page, per_page)
        except MarketplaceError as exc:
            return self._degraded_result(sort_key, page, per_page, str(exc), keyword)

        # 合并缓存里的 verified / version 状态，并把新发现的仓库并入缓存
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
        """无关键词搜索：走缓存（TTL 内直接返回，过期则刷新）。"""
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

        # 顺手验证当前页（已有有效缓存则完全不发请求）
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
        """取一次验证结果快照（用于渲染 verified 字段）。"""
        with self._io_lock:
            return dict(self._load_cache().get("verified") or {})

    def _degraded_result(self, sort_key: str, page: int, per_page: int, reason: str, keyword: str) -> dict:
        """降级：用本地缓存切片兜底，并在 message 里讲清楚原因。"""
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
        """把内部条目转换成前端绑定的固定字段（键名不能改）。"""
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
    # 公开接口 2：detail
    # ------------------------------------------------------------------

    def detail(self, repo: str) -> dict:
        """返回仓库详情（含 skill.json 全文、README 摘要、依赖、入口、prepare 标志）。

        ``repo`` 必须是 ``owner/name``；格式非法时抛 :class:`MarketplaceError`。
        网络不可用时返回降级信息（``offline=True`` + message 说明），不抛异常。
        除任务书要求的字段外，额外提供 ``offline`` 与 ``message`` 两个键供前端提示用。
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

        # 缓存里的历史信息作为兜底（离线时也能显示版本/许可证/验证状态）
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
                # 网络/限流导致读不到：属于降级，明确告知
                offline = True
                message = _t("market.detail.skill_read_failed", error=error)
            else:
                # 网络正常，只是这个仓库确实没有 skill.json：不算离线
                message = _t("market.detail.no_valid_skill", error=error)
        elif not bundle:
            message = _t("market.detail.no_bundle")
        else:
            message = _t("market.detail.ok")

        if not license_text:
            # 仓库元数据里可能带许可证；只在 skill.json 没写时才查，省一次 API 调用
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
            # 额外字段（前端可按需展示；不影响任务书要求的键）
            "offline": offline,
            "message": message,
            "min_version": _min_version_required(skill_json) if skill_json else "",
            "publish": _publish_sources(skill_json) if skill_json else {},
        }

    # ------------------------------------------------------------------
    # 公开接口 3：install
    # ------------------------------------------------------------------

    def install(self, repo: str, on_progress: Optional[Callable[[str], None]] = None) -> dict:
        """从 GitHub 源码安装技能。

        返回 ``{"ok", "id", "version", "path", "message"}``；任何失败都以 ``ok=False``
        加可直接展示的 message 返回，不抛异常。
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

            # ---- 复制到安装目录 ----
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

            # ---- 注册到注册表 ----
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

            # ---- 安装后校验 ----
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

            # ---- 依赖、prepare、许可证、版本兼容提示（均不自动执行） ----
            if dependencies:
                preview = _t("market.list_sep").join(dependencies[:10])
                more = (_t("market.install.deps_more", count=len(dependencies))
                        if len(dependencies) > 10 else "")
                notes.append(_t("market.install.deps_pending", preview=preview, more=more))
            if has_prepare:
                # prepare 脚本一律不执行；真要在用户确认后执行时，必须使用清洗过的环境变量
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
                    # 按模块 docstring 的说明：只提示不阻止，用户明确点击后仍允许安装
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
            # 任务书 5.3：临时文件在 finally 中清理，无论成功失败都不留残留
            if tmp_dir and os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _download_tarball(
        self,
        repo: str,
        tmp_dir: str,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, str, int]:
        """下载源码 tarball 到临时目录，返回 ``(路径, sha256, 字节数)``。

        大小上限 50MB：先看 Content-Length，边下边计数（防止服务端不报长度时超限）。
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
        """安全解压 tarball：拒绝绝对路径、``..``、越界链接与设备文件。

        先用 ``tarfile`` 的 ``filter="data"`` 做二次加固（Python 3.12+ 支持），
        但第一道防线是本函数自己的逐条检查——它能给出明确的中文原因，
        并且在老版本 Python 上依然生效。
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
        """定位解压后的技能根目录（skill.json 可能在根，也可能在一层子目录里）。"""
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
        """安装后校验入口文件：只做"可加载性"检查，**不执行**任何技能代码。

        * 入口是文件 → 用 ``importlib.util.spec_from_file_location`` 确认能构建出加载器。
        * 入口是包目录 → 确认目录里存在 ``__init__.py``。
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
    # 公开接口 4：uninstall
    # ------------------------------------------------------------------

    def uninstall(self, skill_id: str) -> dict:
        """卸载技能：移除注册表条目 → 删除安装目录 → 回读确认无残留。"""
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

        # 删除安装目录（只允许删 install_dir 内的目录，防误删）
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

        # 回读确认
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
    # 公开接口 5：installed
    # ------------------------------------------------------------------

    def installed(self) -> list:
        """返回已安装技能列表：``[{id, version, path, loader, installed_at}, ...]``。"""
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
    # 公开接口 6：status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """返回市场状态：``{"enabled", "state", "cached", "synced_at"}``。

        ``state`` 取值取自语言包 ``market.state.*``（已禁用 / 未同步 /
        已同步（当前无匹配技能）/ 已就绪 / 缓存已过期 / 未知），可直接展示。
        本方法只读本地文件，不发网络请求，保证设置界面打开时立刻有结果。
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
                # 同步成功但 topic 下没有任何仓库：与"从未同步"是两种不同状态
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
