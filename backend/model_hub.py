"""
[EN]
Hugging Face model search and download.

Design notes:

* Two data sources: ``https://hf-mirror.com`` (recommended for some regions)
  and ``https://huggingface.co`` (official). Reachability is probed and the
  implementation falls back when a source is unavailable. Every response states
  which source was used.
* Search and detail use the shared public REST endpoints ``/api/models`` and
  ``/api/models/{repo}?blobs=true``. The mirror is a reverse proxy of the
  official site, so the paths are identical.
* Download is implemented with urllib rather than ``hf_hub_download``.
  ``HF_ENDPOINT`` is a module-level constant and cannot be switched per call at
  runtime. The local implementation also supports resumable transfers, retries,
  live speed and remaining-time reporting, cancellation, and integrity checks.
* The model registry is stored in ``models_registry.json`` and reconciled with
  the actual files under ``models/``. Models placed in that directory manually
  are registered with ``source=local``.

[ZH]
Hugging Face 模型搜索与下载。

设计说明：

* 双数据源：``https://hf-mirror.com``（部分地区推荐）与
  ``https://huggingface.co``（官方）。实现会探测可达性，在数据源不可用时回退，
  并在每次响应中说明所使用的数据源。
* 搜索与详情使用共享的公开 REST 接口 ``/api/models`` 与
  ``/api/models/{repo}?blobs=true``。镜像站是官方站的反向代理，路径一致。
* 下载使用 urllib 实现，而不是 ``hf_hub_download``。``HF_ENDPOINT`` 是模块级
  常量，无法在运行时为单次调用切换数据源。本地实现还支持断点续传、失败重试、
  实时速度与剩余时间、取消与完整性校验。
* 模型库记录在 ``models_registry.json`` 中，并与 ``models/`` 目录中的实际文件
  核对。手工放入该目录的模型会以 ``source=local`` 登记。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# [EN] i18n's reverse dependency on config is lazy (program_root is read only
# [EN] when resolving text), and this module does not import config, so a direct
# [EN] import creates no cycle.
# [ZH] i18n 对 config 的反向依赖是惰性的（仅在取文案时读取 program_root），且
# [ZH] 本模块不导入 config，因此直接导入不会形成循环。
from i18n import t as _t

__all__ = ["ModelHub", "ModelHubError", "MIRROR_ENDPOINT", "OFFICIAL_ENDPOINT",
           "guess_quantization", "guess_quant_code", "EXPAND_FIELDS",
           "MAX_PAGE_SIZE", "HARD_MAX_ITEMS"]

log = logging.getLogger("EchoDesk.ModelHub")

MIRROR_ENDPOINT = "https://hf-mirror.com"
OFFICIAL_ENDPOINT = "https://huggingface.co"

# [EN] Quantization-format detection: Q4_K_M, Q5_0, IQ2_XS, F16, BF16, and so on.
# [ZH] 量化格式识别：Q4_K_M、Q5_0、IQ2_XS、F16、BF16 等。
_QUANT_RE = re.compile(
    r"(?i)\b(i?q\d+(?:_[a-z0-9]+)*|f16|fp16|bf16|f32|fp32|q8_0|q4_0)\b"
)

#: [EN] Server-side sort field mapped to (sort parameter, direction parameter).
#: [EN]
#: [EN] The server's ``sort=downloads`` orders by downloads in the last 30 days.
#: [EN] ``sort=downloadsAllTime`` is not accepted (HTTP 400), and
#: [EN] ``direction=1`` is also rejected. Remote ordering therefore only decides
#: [EN] which entries enter the fetch window; the displayed order is always
#: [EN] recomputed locally by lifetime downloads (see
#: [EN] :func:`model_query.sort_items`). Otherwise "sorted by downloads" and
#: [EN] "displaying lifetime downloads" would contradict each other. The server
#: [EN] has no parameter-count sort, so ``downloads`` is used as a fallback.
#: [ZH] 服务端排序字段到 (sort 参数, direction 参数) 的映射。
#: [ZH]
#: [ZH] 服务端的 ``sort=downloads`` 按最近 30 天下载量排序。它不接受
#: [ZH] ``sort=downloadsAllTime``（HTTP 400），``direction=1`` 也会返回 400。
#: [ZH] 因此远端排序仅决定哪些条目进入抓取窗口；展示顺序始终由本地按历史总下载量
#: [ZH] 重新计算（参见 :func:`model_query.sort_items`）。否则“按下载量排序”与
#: [ZH] “显示历史总下载量”会互相矛盾。服务端没有参数量排序，使用 downloads
#: [ZH] 作为回退。
_SORT_MAP = {
    "downloads": ("downloads", -1),
    "updated": ("lastModified", -1),
    "likes": ("likes", -1),
    "params": ("downloads", -1),
    "name": ("modelId", 1),
}

#: [EN] Server-side page-size limit. A ``limit`` above 1000 is silently truncated
#: [EN] to 1000, so requests use 1000 rather than asking for a larger value and
#: [EN] relying on truncation.
#: [ZH] 服务端单页上限。``limit`` 超过 1000 会被静默截断为 1000，因此请求统一
#: [ZH] 使用 1000，而不是请求更大的值依赖服务端截断。
MAX_PAGE_SIZE = 1000

#: [EN] Fields expanded in each request.
#: [EN]
#: [EN] When ``expand`` is supplied, the response contains only the expanded
#: [EN] fields; default fields such as tags, likes, and downloads disappear.
#: [EN] The list must therefore be complete in one request. Duplicate field names
#: [EN] cause HTTP 400, so parameters are deduplicated with ``dict.fromkeys``.
#: [ZH] 每次请求需要 expand 的字段。
#: [ZH]
#: [ZH] 一旦传入 ``expand``，响应仅包含被 expand 的字段；tags、likes、downloads
#: [ZH] 等默认字段会消失。因此必须在一次请求中列出全部字段。重复字段名会触发
#: [ZH] HTTP 400，因此使用 ``dict.fromkeys`` 去重后再拼接参数。
EXPAND_FIELDS = (
    "downloadsAllTime", "downloads", "likes", "lastModified", "createdAt",
    "tags", "author", "gated", "private", "pipeline_tag", "library_name",
)

#: [EN] Safety limits for automatic loading. The first of three gates to trigger
#: [EN] stops loading and reports "N loaded, more available".
#: [ZH] 自动加载的安全上限。三个限制中任一触发即停止，并报告“已加载 N 条，
#: [ZH] 还有更多”。
DEFAULT_MAX_ITEMS = 5000
DEFAULT_MAX_REQUESTS = 8
DEFAULT_MAX_SECONDS = 90.0
#: [EN] Hard cap: repeated "load more" actions cannot grow the data set without
#: [EN] bound or exhaust memory.
#: [ZH] 硬上限：反复点击“继续加载”也不能无限增长或耗尽内存。
HARD_MAX_ITEMS = 20000

#: [EN] Disk-cache version. Increment it when the cache structure changes so old
#: [EN] caches expire without explicit cleanup.
#: [ZH] 磁盘缓存版本号。缓存结构变化时递增，使旧缓存自然失效，无需额外清理逻辑。
CACHE_VERSION = 1


class ModelHubError(RuntimeError):
    """
    [EN]
    Expected model-hub error: unreachable network, repository without GGUF
    files, verification failure, and similar conditions.

    [ZH]
    模型中心的可预期错误：网络不可达、仓库不含 GGUF 文件、校验失败等。
    """


def guess_quant_code(filename: str) -> str:
    """
    [EN]
    Infer the quantization code from a filename or repository name
    (``Qwen3-0.6B-Q6_K.gguf`` yields ``Q6_K``).

    Returns an empty string when no code is found, not a localized "unknown".
    The value is used as a filter key (see :mod:`model_query`); using localized
    display text as data would invalidate filters after a language switch. The
    display form is provided by :func:`guess_quantization`.

    [ZH]
    从文件名或仓库名推断量化代码（``Qwen3-0.6B-Q6_K.gguf`` 得到 ``Q6_K``）。

    无法识别时返回空串，而不是本地化的“未知”。该值用作筛选键（参见
    :mod:`model_query`）；将本地化显示文本作为数据会导致切换语言后筛选失效。
    显示形式由 :func:`guess_quantization` 提供。
    """
    base = os.path.splitext(os.path.basename(filename or ""))[0]
    matches = _QUANT_RE.findall(base)
    if matches:
        return matches[-1].upper()
    return ""


def guess_quantization(filename: str) -> str:
    """
    [EN]
    Return the display form of the quantization format, using the localized
    "unknown" label when unavailable.

    [ZH]
    返回量化格式的显示文本；无法识别时使用本地化的“未知”标签。
    """
    return guess_quant_code(filename) or _t("hub.unknown")


def _human(size: Optional[int]) -> str:
    """
    [EN]
    Convert a byte count to a human-readable string with one decimal place.

    [ZH]
    将字节数转换为保留一位小数的人类可读字符串。
    """
    if not size:
        return _t("hub.unknown")
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _resolve_state_path(raw: Any) -> str:
    """
    [EN]
    Resolve a configured path against the writable state directory.

    Absolute paths (and ``~`` paths) are passed through unchanged. Relative paths are
    joined to :func:`config.state_dir`: on a writable working directory (portable tar.gz)
    that is exactly the legacy ``os.path.abspath`` behaviour, while on a read-only mount
    point they are redirected to the user data root (v0.2.5 P0).

    [ZH]
    把配置里的路径按可写状态目录解析。

    绝对路径（以及 ``~`` 路径）原样返回；相对路径拼到 :func:`config.state_dir` 上：
    工作目录可写时（tar.gz 便携版）与旧的 ``os.path.abspath`` 行为完全一致，
    只读挂载点上则改道到用户数据根（v0.2.5 P0）。
    """
    text = os.path.expanduser(str(raw or "").strip() or ".")
    if os.path.isabs(text):
        return os.path.abspath(text)
    from config import state_dir  # [EN] Deferred to avoid an import cycle at load time.
                                  # [ZH] 延迟导入以避开加载期的循环依赖。
    return os.path.abspath(os.path.join(state_dir(), text))


class ModelHub:
    """
    [EN]
    Model hub: search, detail, download, and model-registry management.

    [ZH]
    模型中心：搜索、详情、下载与模型库管理。
    """

    def __init__(self, cfg: Any, logger: Optional[logging.Logger] = None,
                 app_config: Any = None) -> None:
        """
        [EN]
        ``cfg`` is ``config.model_hub`` (data source, model directory, token).
        ``app_config`` is the top-level ``Config``, read only to resolve the
        default model (``model_path`` / ``model_repo``).

        The two objects are separate because the identity of the default model is
        application-level configuration, not model-hub configuration. Without it
        the default model cannot be pinned into the list, and the question 'why
        is the default model absent from the list?' cannot be answered.

        [ZH]
        ``cfg`` 为 ``config.model_hub``（数据源、模型目录、Token）。
        ``app_config`` 为顶层 ``Config``，仅用于读取默认模型
        （``model_path`` / ``model_repo``）。

        两者分开是因为默认模型的身份属于应用级配置，而不是模型中心配置。缺少该
        配置时无法将默认模型钉入列表，也无法回答“默认模型为何不在列表中”。
        """
        self.cfg = cfg
        self.app_config = app_config
        self.logger = logger or log
        self._lock = __import__("threading").Lock()
        self._source_cache: Dict[str, Any] = {}
        self._last_basis: Dict[str, Any] = {}
        # [EN] Cache of locally downloaded repository identifiers.
        # [ZH] 本地已下载仓库标识的缓存。
        self._installed_cache: Optional[set] = None
        # [EN] Currently loaded dataset (in-memory cache used by model_hub_query
        # [EN] for local filtering and pagination). It is process-local and is
        # [EN] lost on backend restart; disk caching is handled separately.
        # [ZH] 当前已加载数据集（内存缓存，供 model_hub_query 执行本地筛选与分页）。
        # [ZH] 该缓存仅存在于进程内，后端重启后失效；磁盘缓存另行处理。
        self._session: Optional[Dict[str, Any]] = None

    # [EN] ---------- Configuration ----------
    # [ZH] ---------- 配置读取 ----------

    @property
    def source_setting(self) -> str:
        """
        [EN]
        Return the configured data source: auto, mirror, or official.

        [ZH]
        返回用户配置的数据源：auto、mirror 或 official。
        """
        raw = str(getattr(self.cfg, "source", "auto") or "auto").strip().lower()
        return raw if raw in ("auto", "mirror", "official") else "auto"

    @property
    def token(self) -> str:
        """
        [EN]
        Return the Hugging Face access token required by gated models, preferring
        configuration over environment variables.

        [ZH]
        返回受限模型所需的 Hugging Face 访问令牌，优先使用配置，其次使用环境变量。
        """
        raw = str(getattr(self.cfg, "hf_token", "") or "").strip()
        if raw:
            return raw
        return (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or "").strip()

    @property
    def models_dir(self) -> str:
        """
        [EN]
        Model repository directory. Relative values are resolved against
        :func:`config.state_dir` (v0.2.5 P0), which equals the working directory in the
        portable tar.gz layout, so existing installations keep exactly the same path;
        only read-only mount points (.app / .AppImage) fall back to the user data root.

        [ZH]
        模型库目录。相对路径以 :func:`config.state_dir` 为基准解析（v0.2.5 P0）：
        在 tar.gz 便携版里它就等于工作目录，因此现有安装的路径完全不变；只有在只读
        挂载点（.app / .AppImage）上才下沉到用户数据根。
        """
        return _resolve_state_path(getattr(self.cfg, "models_dir", "models") or "models")

    @property
    def registry_path(self) -> str:
        """
        [EN]
        Model registry file. Relative values follow the same
        :func:`config.state_dir` rule as :attr:`models_dir`.

        [ZH]
        模型注册表文件。相对路径与 :attr:`models_dir` 使用同一条
        :func:`config.state_dir` 规则。
        """
        return _resolve_state_path(getattr(self.cfg, "registry_file", "models_registry.json")
                                   or "models_registry.json")

    # [EN] Model downloads default to models/ under the program directory,
    # [EN] consistent with existing backend conventions.
    # [ZH] 模型下载默认使用程序目录下的 models/，与后端既有约定一致。
    def _ensure_dirs(self) -> None:
        os.makedirs(self.models_dir, exist_ok=True)

    # [EN] ---------- HTTP basics ----------
    # [ZH] ---------- HTTP 基础 ----------

    def _headers(self) -> Dict[str, str]:
        headers = {"User-Agent": "EchoDesk/0.2.0 (+local desktop assistant)"}
        token = self.token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _get_json(self, url: str, timeout: int = 20) -> Any:
        request = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", "replace")
        return json.loads(payload)

    def _probe(self, base: str, timeout: int = 6) -> bool:
        """
        [EN]
        Probe whether a data source is reachable, using a single-result request
        to keep the cost low.

        [ZH]
        探测数据源是否可达，仅请求一条结果以降低开销。
        """
        try:
            self._get_json(f"{base}/api/models?limit=1", timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("数据源不可达 %s: %s", base, exc)
            return False

    def resolve_source(self, requested: Optional[str] = None) -> Dict[str, str]:
        """
        [EN]
        Decide which data source to use for this request and return
        ``{key, base, note}``.

        ``auto`` prefers the mirror, which is often faster in some regions, and
        falls back to the official endpoint when the mirror is unreachable. When
        both are unreachable, the mirror is still returned and the note records
        the failure for the caller to report.

        [ZH]
        决定本次请求使用的数据源，返回 ``{key, base, note}``。

        ``auto`` 优先使用镜像站（部分地区通常更快），镜像站不可达时回退到官方
        站点。两者均不可达时仍返回镜像站，并在 note 中记录状态，由调用方明确提示。
        """
        choice = (requested or self.source_setting).strip().lower()
        if choice not in ("auto", "mirror", "official"):
            choice = self.source_setting

        if choice == "mirror":
            return {"key": "mirror", "base": MIRROR_ENDPOINT, "note": _t("hub.source.note_mirror_manual")}
        if choice == "official":
            return {"key": "official", "base": OFFICIAL_ENDPOINT,
                    "note": _t("hub.source.note_official_manual")}

        mirror_ok = self._probe(MIRROR_ENDPOINT)
        if mirror_ok:
            return {"key": "mirror", "base": MIRROR_ENDPOINT,
                    "note": _t("hub.source.note_mirror_auto")}
        if self._probe(OFFICIAL_ENDPOINT):
            return {"key": "official", "base": OFFICIAL_ENDPOINT,
                    "note": _t("hub.source.note_official_fallback")}
        return {"key": "mirror", "base": MIRROR_ENDPOINT,
                "note": _t("hub.source.note_all_unreachable")}

    # [EN] ---------- Search ----------
    # [ZH] ---------- 搜索 ----------

    def load_limits(self) -> Dict[str, Any]:
        """
        [EN]
        Return the safety limits for automatic loading, preventing an overly
        broad keyword from causing unbounded requests.

        Loading stops when any of three gates triggers: total item count,
        request count, or elapsed time. Values are configurable through
        ``config.model_hub`` and are bounded by :data:`HARD_MAX_ITEMS`.
        Repeated "load more" actions restart the per-round counters and cannot
        grow the data set without bound.

        [ZH]
        返回自动加载的安全上限，防止过宽的关键词导致无界请求。

        三个限制中任一触发即停止：总条数、请求次数或耗时。数值可通过
        ``config.model_hub`` 配置，并受 :data:`HARD_MAX_ITEMS` 绝对封顶。
        反复点击“继续加载”会重置每轮计数，但数据规模不会无限增长。
        """
        def number(name: str, default: Any, cast: Any) -> Any:
            try:
                return cast(getattr(self.cfg, name, default))
            except (TypeError, ValueError):
                return default

        max_items = min(number("max_items", DEFAULT_MAX_ITEMS, int), HARD_MAX_ITEMS)
        return {
            "max_items": max(1, max_items),
            "max_requests": max(1, number("max_requests", DEFAULT_MAX_REQUESTS, int)),
            "max_seconds": max(1.0, number("max_seconds", DEFAULT_MAX_SECONDS, float)),
            "page_size": MAX_PAGE_SIZE,
            "hard_max_items": HARD_MAX_ITEMS,
        }

    @property
    def page_size(self) -> int:
        """
        [EN]
        Return the default page size for local pagination (20 by default,
        configurable to 20, 50, or 100).

        [ZH]
        返回本地分页的默认每页条数（默认 20，可选 20、50 或 100）。
        """
        try:
            size = int(getattr(self.cfg, "page_size", 20) or 20)
        except (TypeError, ValueError):
            size = 20
        return size if size > 0 else 20

    @property
    def cache_ttl_hours(self) -> float:
        try:
            return max(0.0, float(getattr(self.cfg, "search_cache_ttl_hours", 6) or 0))
        except (TypeError, ValueError):
            return 6.0

    @property
    def cache_dir(self) -> str:
        """
        [EN]
        Return the disk-cache directory for search results.

        The default is ``cache/model_hub`` under the user data directory. That
        location is writable, travels with user data backups, and does not pollute
        the program directory. If ``search_cache_dir`` is configured, it takes
        precedence; relative paths resolve against the current working directory.

        [ZH]
        返回搜索结果磁盘缓存目录。

        默认位于用户数据目录下的 ``cache/model_hub``。该位置可写、会随用户数据
        一同备份，并且不会污染程序目录。若配置了 ``search_cache_dir``，则优先使用
        该值；相对路径按当前工作目录解析。
        """
        raw = str(getattr(self.cfg, "search_cache_dir", "") or "").strip()
        if raw:
            return os.path.abspath(os.path.expanduser(raw))
        try:
            from config import user_data_root

            return os.path.join(user_data_root(), "cache", "model_hub")
        except Exception:  # noqa: BLE001
            # [EN] If the user directory cannot be resolved, fall back to the
            # [EN] directory next to the configuration / model library.
            # [ZH] 取不到用户目录就退到配置/模型库同级。
            return os.path.abspath(os.path.join(os.path.dirname(self.registry_path), "hub_cache"))

    # [EN] ---------- HTTP pagination ----------
    # [ZH] ---------- HTTP 分页 ----------

    def _search_params(self, keyword: str, sort: str, cursor: str = "",
                       page_limit: int = MAX_PAGE_SIZE) -> List[Tuple[str, str]]:
        """
        [EN]
        Build parameters for one search request as a list because ``expand``
        must occur multiple times.

        [ZH]
        构造单次搜索请求的参数；使用列表形式，因为 ``expand`` 需要重复出现。
        """
        sort_key, direction = _SORT_MAP.get((sort or "downloads").lower(), _SORT_MAP["downloads"])
        # [EN] Deduplicate with dict.fromkeys: a duplicate expand field causes HTTP 400.
        # [ZH] 使用 dict.fromkeys 去重：重复的 expand 字段会触发 HTTP 400。
        params: List[Tuple[str, str]] = [
            ("filter", "gguf"),
            ("sort", sort_key),
            ("direction", str(direction)),
            ("limit", str(max(1, min(int(page_limit or MAX_PAGE_SIZE), MAX_PAGE_SIZE)))),
        ]
        # [EN] Do not use full=true with search; the default fields suffice and
        # [EN] full would include siblings.
        # [ZH] 使用 search 时不要设置 full=true；默认字段已足够，full 会包含 siblings。
        params.append(("full", "false"))
        text = (keyword or "").strip()
        if text:
            params.append(("search", text))
        for field in dict.fromkeys(EXPAND_FIELDS):
            params.append(("expand", field))
        if cursor:
            params.append(("cursor", cursor))
        return params

    def _get_json_with_headers(self, url: str, timeout: int = 40) -> Tuple[Any, Dict[str, str]]:
        """
        [EN]
        Like :meth:`_get_json`, but also returns response headers. The pagination
        cursor is carried in the ``Link`` header.

        [ZH]
        与 :meth:`_get_json` 相同，但同时返回响应头。分页游标位于 ``Link`` 头中。
        """
        request = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", "replace")
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        return json.loads(payload), headers

    @staticmethod
    def cursor_from_link(link: str) -> str:
        """
        [EN]
        Extract the next-page cursor from ``Link: <...>; rel="next"``, or return
        an empty string when there is no next page.

        Only the ``cursor`` query parameter is extracted and appended to the
        current data source. The full URL must not be reused: the mirror may
        return a link pointing to ``https://huggingface.co/...``, and following
        it would send all subsequent pages to the official site. On networks
        where only the mirror is reachable, that would break pagination.

        [ZH]
        从 ``Link: <...>; rel="next"`` 中提取下一页游标；没有下一页时返回空串。

        仅提取 ``cursor`` 查询参数，并拼回当前数据源。不得直接复用完整 URL：镜像
        站返回的链接可能指向 ``https://huggingface.co/...``，直接使用会将后续分页
        全部发往官方站点。在仅镜像站可达的网络中，这会导致分页中断。
        """
        if not link or 'rel="next"' not in link:
            return ""
        target = link.split(">")[0].lstrip("<").strip()
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
        except ValueError:
            return ""
        values = query.get("cursor") or []
        return values[0] if values else ""

    @staticmethod
    def _downloads_of(entry: Dict[str, Any]) -> Tuple[int, int, str]:
        """
        [EN]
        Return ``(display_value, 30_day_value, metric_source)`` for downloads.

        The default display metric is lifetime downloads, which requires
        ``expand=downloadsAllTime``. If an entry lacks the field because of an
        API change or mirror difference, the implementation falls back to the
        30-day count and marks the source. It never silently displays zero, which
        would imply that the model has never been downloaded.

        [ZH]
        返回下载量的 ``(展示值, 30 天值, 口径来源)``。

        默认展示口径为历史总下载量，需要显式 ``expand=downloadsAllTime``。若条目因
        接口变更或镜像差异而缺少该字段，则回退到 30 天下载量并标注来源。不得静默
        显示为 0，否则会让人误以为该模型从未被下载。
        """
        all_time = entry.get("downloadsAllTime")
        try:
            recent = int(entry.get("downloads") or 0)
        except (TypeError, ValueError):
            recent = 0
        if all_time is None:
            return recent, recent, "default_fallback"
        try:
            return int(all_time), recent, "all_time"
        except (TypeError, ValueError):
            return recent, recent, "default_fallback"

    def _normalize_entry(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        [EN]
        Normalize one remote result into a list entry, including the download
        metric and quantization code.

        [ZH]
        将一条远端结果规范化为列表条目，包含下载量口径与量化代码。
        """
        if not isinstance(entry, dict):
            return None
        model_id = str(entry.get("modelId") or entry.get("id") or "").strip()
        if not model_id:
            return None
        downloads, recent, metric = self._downloads_of(entry)
        gated = entry.get("gated")
        tags = [str(t) for t in (entry.get("tags") or [])][:12]
        pipeline = str(entry.get("pipeline_tag") or "")
        library = str(entry.get("library_name") or "")
        # [EN] The list endpoint does not return the model-card body, so the
        # [EN] description is composed from available metadata.
        # [ZH] 列表接口不返回模型卡正文，因此描述由可用元数据合成。
        description = " · ".join(x for x in (pipeline, library, ", ".join(tags[:6])) if x)
        return {
            "repo": model_id,
            "name": model_id.split("/")[-1],
            "author": str(entry.get("author") or model_id.split("/")[0]),
            "downloads": downloads,
            "downloads_all_time": downloads if metric == "all_time" else None,
            "downloads_recent": recent,
            "downloads_source": metric,
            "likes": int(entry.get("likes") or 0),
            "updated_at": str(entry.get("lastModified") or "")[:10],
            "created_at": str(entry.get("createdAt") or "")[:10],
            "quantization": guess_quantization(model_id),
            "quant_code": guess_quant_code(model_id),
            "gated": bool(gated) and gated is not False,
            "tags": tags,
            "pipeline_tag": pipeline,
            "library_name": library,
            "description": description,
            "installed": self._is_installed_repo(model_id),
            "origin": "remote",
        }

    def _cap_after(self, base: str, exc: Exception) -> Dict[str, str]:
        """
        [EN]
        When the first page fails, retry once against the other data source,
        consistent with the dual-source contract.

        [ZH]
        第一页失败时切换到另一个数据源重试一次，与双数据源约定一致。
        """
        other = OFFICIAL_ENDPOINT if base == MIRROR_ENDPOINT else MIRROR_ENDPOINT
        name = _t("hub.source.official") if other == OFFICIAL_ENDPOINT else _t("hub.source.mirror")
        self.logger.warning("数据源 %s 第 1 页失败（%s），改用 %s", base, exc, other)
        return {"key": "official" if other == OFFICIAL_ENDPOINT else "mirror", "base": other,
                "note": _t("hub.source.note_fallback_other", name=name)}

    def load_all(
        self,
        keyword: str = "",
        sort: str = "downloads",
        source: Optional[str] = None,
        *,
        resume: bool = False,
        refresh: bool = False,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        [EN]
        Page through all results for a keyword and return the loaded data set.

        Flow and trade-offs:

        1. Cache first: in-memory session, unexpired disk cache, then the
           network. ``refresh=True`` bypasses the first two levels.
        2. Pagination loop: request up to 1000 entries per page and follow the
           ``Link`` cursor until no next page exists (at which point
           ``total_available`` is exact) or a safety limit triggers.
        3. Retry: a failed page is retried ``download_retries`` times with
           exponential backoff. Repeated failure aborts the load and returns the
           partial results already obtained.
        4. Progress: each page is reported through ``on_progress`` so the
           interface can display the current count instead of waiting silently.
        5. ``resume=True`` continues after existing results for the "load more"
           action, bounded by :data:`HARD_MAX_ITEMS`.

        The result structure is documented in :meth:`_dataset` and contains
        ``items``, ``pinned``, ``loaded``, ``total_available``, ``has_more``,
        ``stop_reason``, ``cache``, and ``query``.

        [ZH]
        自动分页获取某个关键词的全部结果，并返回已加载数据集。

        流程与取舍：

        1. 缓存优先：内存会话、未过期磁盘缓存，最后才访问网络。``refresh=True``
           跳过前两级缓存。
        2. 分页循环：每页最多请求 1000 条，沿 ``Link`` 游标继续，直到不存在下一页
           （此时 ``total_available`` 为精确总数）或触发安全上限。
        3. 重试：单页失败后按指数退避重试 ``download_retries`` 次。连续失败则中止，
           并返回已取得的部分结果。
        4. 进度：每页通过 ``on_progress`` 上报，界面可显示当前加载数量，而不是无反馈
           等待。
        5. ``resume=True`` 在已有结果之后继续加载，对应“继续加载”操作，并受
           :data:`HARD_MAX_ITEMS` 绝对封顶。

        结果结构见 :meth:`_dataset`，包含 ``items``、``pinned``、``loaded``、
        ``total_available``、``has_more``、``stop_reason``、``cache`` 与 ``query``。
        """
        caps = self.load_limits()
        keyword = (keyword or "").strip()
        sort = sort if sort in _SORT_MAP else "downloads"
        resolved = self.resolve_source(source)
        cache_key = self._cache_key(keyword, sort, resolved["key"])
        started = time.time()

        base_record = self._current_record(cache_key) if resume else None
        if resume and base_record is None:
            # [EN] No resumable base exists (backend restarted or cache was
            # [EN] removed): degrade to a fresh load rather than returning an
            # [EN] error the user cannot act on.
            # [ZH] 不存在可续加载的底子（后端已重启或缓存被删除）：退化为全新加载，
            # [ZH] 而不是返回用户无法处理的错误。
            resume = False

        if not resume and not refresh:
            cached = self._read_cache(cache_key, allow_expired=False)
            if cached is not None:
                # [EN] A cache hit must also be registered as the current data
                # [EN] set. ``query_session`` (pagination, sorting, filtering,
                # [EN] page clamping) reads ``self._session``; without this
                # [EN] assignment a cache-backed list renders correctly but the
                # [EN] first pagination or sort action reports "no search
                # [EN] results loaded". Cache hits are common: the default TTL is
                # [EN] six hours, so the second visit usually hits.
                # [EN]
                # [EN] Cache records and network records share the same structure,
                # [EN] so the cached record can be reused directly. This also
                # [EN] enables "load more" after a cache hit.
                # [ZH] 缓存命中的记录也必须登记为当前数据集。``query_session``
                # [ZH]（翻页、排序、筛选与页码钳制）读取 ``self._session``；若缺少
                # [ZH] 该赋值，缓存驱动的列表可以正常显示，但首次翻页或排序会报告
                # [ZH] “尚未加载搜索结果”。缓存命中有两种情况很常见：默认 TTL 为
                # [ZH] 六小时，因此第二次访问通常会命中。
                # [ZH]
                # [ZH] 缓存记录与联网记录结构一致，可直接复用。这也使缓存命中后的
                # [ZH]“继续加载”得以工作。
                self._session = cached
                return self._dataset(cached, resolved, caps, cache_hit=True,
                                     cache_age=time.time() - float(cached.get("created_at") or 0))

        cursor = str(base_record.get("next_cursor") or "") if base_record else ""
        raw_items: List[Dict[str, Any]] = list(base_record.get("items") or []) if base_record else []
        seen = {str(it.get("repo") or "") for it in raw_items}
        requests = 0
        pages = 0
        fallback_count = sum(1 for it in raw_items if it.get("downloads_source") == "default_fallback")
        stop_reason = "none"
        error = ""
        retries = max(1, int(getattr(self.cfg, "download_retries", 3) or 3))
        base = resolved["base"]

        def report(phase: str) -> None:
            if on_progress:
                on_progress({
                    "phase": phase,
                    "loaded": len(raw_items),
                    "requests": requests,
                    "pages": pages,
                    "elapsed": round(time.time() - started, 2),
                    "has_more": stop_reason in ("items", "requests", "seconds") or bool(cursor),
                    "source": resolved["key"],
                    "limits": {k: caps[k] for k in ("max_items", "max_requests", "max_seconds")},
                })

        report("resolving")
        first_page = not raw_items
        # [EN] Start offset for this "load more" round. Safety limits are
        # [EN] evaluated per round, not cumulatively; otherwise a second load-more
        # [EN] action would fall back to fetching a single entry. The cumulative
        # [EN] size remains bounded by HARD_MAX_ITEMS.
        # [ZH] 本轮“继续加载”的起始偏移。安全上限按每轮而非累计计算；否则第二次
        # [ZH] “继续加载”会退化为只获取一条。累计规模仍受 HARD_MAX_ITEMS 绝对封顶。
        round_start = len(raw_items)
        while True:
            if should_stop and should_stop():
                stop_reason = "aborted"
                break
            if requests >= caps["max_requests"]:
                stop_reason = "requests"
                break
            if time.time() - started >= caps["max_seconds"]:
                stop_reason = "seconds"
                break
            if not first_page and not cursor:
                # [EN] No next page means loading is complete.
                # [ZH] 没有下一页表示确实已加载完毕。
                stop_reason = "none"
                break
            # [EN] Request only the number of entries still allowed by the
            # [EN] limit. The server caps a page at 1000; without this
            # [EN] calculation the limit is soft and a page can overshoot it,
            # [EN] which also corrupts resume accounting.
            # [ZH] 仅请求距离上限尚可容纳的条数。服务端单页上限为 1000；若不做此
            # [ZH] 计算，上限会变成软约束，一页可能超出限制，并扰乱续加载计数。
            remaining_round = caps["max_items"] - (len(raw_items) - round_start)
            remaining_hard = HARD_MAX_ITEMS - len(raw_items)
            if remaining_round <= 0:
                stop_reason = "items"
                break
            if remaining_hard <= 0:
                stop_reason = "items"
                break
            page_limit = max(1, min(MAX_PAGE_SIZE, remaining_round, remaining_hard))

            params = self._search_params(keyword, sort, cursor if not first_page else "",
                                         page_limit)
            url = f"{base}/api/models?" + urllib.parse.urlencode(params)
            payload: Optional[List[Any]] = None
            last_error = ""
            for attempt in range(1, retries + 1):
                try:
                    payload, headers = self._get_json_with_headers(url)
                    requests += 1
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    requests += 1
                    self.logger.warning("加载第 %s 页失败（第 %s/%s 次）：%s",
                                        pages + 1, attempt, retries, last_error)
                    if first_page and attempt == 1:
                        # [EN] On first-page failure, switch data sources before
                        # [EN] retrying the same endpoint.
                        # [ZH] 第一页失败时，先切换数据源，而不是在同一端点重试。
                        other = self._cap_after(base, exc)
                        base = other["base"]
                        resolved = other
                        url = f"{base}/api/models?" + urllib.parse.urlencode(params)
                        continue
                    if attempt < retries:
                        time.sleep(min(1.5 * attempt, 4.0))
            if payload is None:
                error = last_error
                stop_reason = "error"
                break

            if not isinstance(payload, list):
                error = _t("hub.error.detail_bad_format")
                stop_reason = "error"
                break

            pages += 1
            added = 0
            for entry in payload:
                item = self._normalize_entry(entry)
                if item is None:
                    continue
                repo = item["repo"]
                if repo in seen:
                    continue
                seen.add(repo)
                if item["downloads_source"] == "default_fallback":
                    fallback_count += 1
                raw_items.append(item)
                added += 1

            cursor = self.cursor_from_link(headers.get("link", ""))
            report("loading")
            first_page = False
            if not cursor:
                # [EN] No next page means loading is complete.
                # [ZH] 没有下一页表示确实已加载完毕。
                stop_reason = "none"
                break
            if len(raw_items) - round_start >= caps["max_items"]:
                stop_reason = "items"
                break
            if len(raw_items) >= HARD_MAX_ITEMS:
                stop_reason = "items"
                break

        record = {
            "version": CACHE_VERSION,
            "key": cache_key,
            "keyword": keyword,
            "sort": sort,
            "source": resolved["key"],
            "source_note": resolved["note"],
            "created_at": time.time(),
            "elapsed": round(time.time() - started, 2),
            "requests": requests,
            "pages": pages,
            "items": raw_items,
            "next_cursor": cursor,
            "stop_reason": stop_reason,
            "has_more": stop_reason in ("items", "requests", "seconds", "aborted")
                        or bool(cursor),
            "fallback_count": fallback_count,
            "error": error,
            "offline": False,
            "resumed": bool(resume),
        }
        # [EN] Write the cache only when the network returned results or the
        # [EN] record is already valid. If no page was retrieved because every
        # [EN] endpoint failed, do not persist an empty result as a cache entry.
        # [ZH] 仅在网络返回结果或记录本身有效时写入缓存。若所有端点均失败且未取得
        # [ZH] 任何页面，则不得将空结果固化为缓存。
        if raw_items or stop_reason in ("none", "items", "requests", "seconds"):
            self._write_cache(record)
        self._session = record
        return self._dataset(record, resolved, caps, cache_hit=False,
                             cache_age=time.time() - float(record["created_at"]))

    def search(
        self,
        keyword: str = "",
        sort: str = "downloads",
        page: int = 1,
        limit: int = 0,
        source: Optional[str] = None,
        min_downloads: int = 0,
        quant: str = "",
        page_size: int = 0,
        filters: Optional[Dict[str, Any]] = None,
        refresh: bool = False,
        resume: bool = False,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        [EN]
        Search GGUF models by loading a dataset and then applying filtering,
        sorting, and pagination locally.

        Returns ``{items, pinned, total, page, source, source_note, offline,
        message, query, ...}``, where ``items`` is the requested page (page 1 by
        default, 20 entries per page).

        Differences from the earlier implementation:

        * ``items`` is the current page of the locally filtered and sorted data
          set, not one 20-entry server page.
        * ``limit`` is retained as a compatibility alias for the local page
          size. The remote page size is fixed at 1000 in :meth:`load_all`.
        * ``min_downloads`` and ``quant`` are local filters, so the loaded count
          and the filterable set stay consistent.
        * The default model is returned in ``pinned`` rather than mixed into
          ``items``. It remains pinned at the top, labels its source, and is
          excluded from pagination counts.

        [ZH]
        搜索 GGUF 模型：先加载数据集，再在本地执行筛选、排序与分页。

        返回 ``{items, pinned, total, page, source, source_note, offline,
        message, query, ...}``，其中 ``items`` 是请求页（默认第 1 页，每页 20 条）。

        与早期实现的差异：

        * ``items`` 是本地筛选与排序后的当前页，而不是服务端一页 20 条。
        * ``limit`` 保留为本地每页条数的兼容别名。远端页大小在
          :meth:`load_all` 中固定为 1000。
        * ``min_downloads`` 与 ``quant`` 是本地筛选条件，使已加载数量与可筛选集合
          保持一致。
        * 默认模型放在 ``pinned`` 中，而不是混入 ``items``。它固定在顶部、标注来源，
          且不计入分页计数。
        """
        dataset = self.load_all(keyword, sort, source, resume=resume, refresh=refresh,
                                on_progress=on_progress, should_stop=should_stop)
        extra = dict(filters or {})
        if min_downloads:
            extra.setdefault("min_downloads", min_downloads)
        if quant:
            quants = list(extra.get("quant") or [])
            if quant.upper() not in [q.upper() for q in quants]:
                quants.append(quant.upper())
            extra["quant"] = quants
        size = int(page_size or limit or self.page_size)
        dataset["query"] = self.query_dataset(dataset, page=page, page_size=size, filters=extra)
        return dataset

    # [EN] ---------- Loaded dataset to page (local filter, sort, paginate) ----------
    # [ZH] ---------- 已加载数据集到页面（本地筛选、排序、分页） ----------

    def _cache_key(self, keyword: str, sort: str, source_key: str) -> str:
        raw = f"{CACHE_VERSION}|{source_key}|{keyword.strip().lower()}|{sort}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, cache_key: str) -> str:
        return os.path.join(self.cache_dir, f"{cache_key}.json")

    def _read_cache(self, cache_key: str, allow_expired: bool = False) -> Optional[Dict[str, Any]]:
        """
        [EN]
        Read the disk cache. ``allow_expired=True`` is used for offline
        degradation, where stale data is preferable to an empty result.

        [ZH]
        读取磁盘缓存。``allow_expired=True`` 用于离线降级；过期数据优于空白结果。
        """
        path = self._cache_path(cache_key)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("搜索结果缓存损坏，已忽略：%s", exc)
            return None
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return None
        if not isinstance(data.get("items"), list):
            return None
        age_hours = (time.time() - float(data.get("created_at") or 0)) / 3600.0
        if not allow_expired and age_hours > self.cache_ttl_hours:
            return None
        data["_age_hours"] = age_hours
        return data

    def _write_cache(self, record: Dict[str, Any]) -> str:
        """
        [EN]
        Write the disk cache. A write failure is logged only; a cache write
        failure must not fail the entire search.

        [ZH]
        写入磁盘缓存。写入失败仅记录日志；缓存写入失败不得导致整个搜索失败。
        """
        path = self._cache_path(str(record.get("key") or ""))
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
            return path
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("搜索结果缓存写入失败：%s", exc)
            return ""

    def _current_record(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """
        [EN]
        Return the base record for resumable loading: in-memory session first,
        then disk cache including expired entries.

        [ZH]
        获取可续加载的基础记录：优先内存会话，其次磁盘缓存（包含过期条目）。
        """
        session = getattr(self, "_session", None)
        if session and session.get("key") == cache_key and session.get("items"):
            return session
        return self._read_cache(cache_key, allow_expired=True)

    def _dataset(
        self,
        record: Dict[str, Any],
        resolved: Dict[str, str],
        caps: Dict[str, Any],
        *,
        cache_hit: bool,
        cache_age: float,
    ) -> Dict[str, Any]:
        """
        [EN]
        Assemble a cache record into the dataset used by the interface: merge
        local sources, add recommendation markers, and compute the three-level
        counts.

        [ZH]
        将缓存记录组装为界面数据集：合并本地来源、添加推荐标记，并计算三层计数。
        """
        remote_items = [dict(it) for it in (record.get("items") or [])]
        pinned, local_items = self._split_local_and_default()
        pinned_repos = {str(it.get("repo") or "") for it in pinned}
        seen = {str(it.get("repo") or "") for it in remote_items}
        for extra in local_items:
            repo = str(extra.get("repo") or "")
            if repo in seen or repo in pinned_repos:
                continue
            seen.add(repo)
            remote_items.append(extra)

        # [EN] Recommendation markers must also cover pinned entries. Pinned
        # [EN] entries are not in items, and omitting them would leave no basis
        # [EN] for explaining why they are recommended.
        # [ZH] 推荐标记必须覆盖 pinned 条目。pinned 条目不在 items 中，遗漏后将缺少
        # [ZH] 说明其推荐原因的依据。
        basis = self._mark_recommended(remote_items + pinned)

        total_available = record.get("total_available")
        if record.get("stop_reason") == "none" and not record.get("next_cursor"):
            # [EN] Loading is complete, so the merged data-set size is the exact
            # [EN] total. The merged count includes locally downloaded sources
            # [EN] because the displayed total and page count must be consistent.
            # [ZH] 加载已完成，因此合并后的数据集大小即精确总数。合并计数包含本地
            # [ZH] 已下载来源，以保证显示的总数与分页一致。
            total_available = len(remote_items)

        fallback = int(record.get("fallback_count") or 0)
        loaded = len(remote_items)
        has_more = bool(record.get("has_more"))
        stop_reason = str(record.get("stop_reason") or "none")
        message = self._load_message(loaded, total_available, has_more, stop_reason,
                                     record.get("error") or "", cache_hit)
        return {
            "items": remote_items,
            "pinned": pinned,
            "loaded": loaded,
            "pinned_count": len(pinned),
            # [EN] Legacy-compatible field: total item count (including local
            # [EN] sources).
            # [ZH] 兼容旧字段：总条数（含本地来源）。
            "total": loaded,
            "total_available": total_available,
            "has_more": has_more,
            "stop_reason": stop_reason,
            "limits": {k: caps[k] for k in ("max_items", "max_requests", "max_seconds")},
            "sort": record.get("sort") or "downloads",
            "keyword": record.get("keyword") or "",
            "source": record.get("source") or resolved["key"],
            "source_note": record.get("source_note") or resolved["note"],
            "offline": bool(record.get("offline")),
            "requests": int(record.get("requests") or 0),
            "pages": int(record.get("pages") or 0),
            "elapsed": float(record.get("elapsed") or 0.0),
            "error": record.get("error") or "",
            "resumed": bool(record.get("resumed")),
            # [EN] Download metric: per-entry source markers plus an aggregate
            # [EN] so the interface can state which metric is displayed.
            # [ZH] 下载量口径：条目级来源标记与汇总，供界面说明所显示的口径。
            "downloads_metric": ("all_time" if fallback == 0 else "mixed"),
            "downloads_fallback_count": fallback,
            "source_items": {
                "remote": len(record.get("items") or []),
                "local": len(remote_items) - len(record.get("items") or []),
                "pinned": len(pinned),
                "merged": loaded,
            },
            "recommend_basis": basis,
            "cache": {
                "hit": cache_hit,
                "age_seconds": int(max(0, cache_age)),
                "ttl_hours": self.cache_ttl_hours,
                "path": self._cache_path(str(record.get("key") or "")),
            },
            "message": message,
        }

    def _load_message(self, loaded: int, total_available: Optional[int], has_more: bool,
                      stop_reason: str, error: str, cache_hit: bool) -> str:
        """
        [EN]
        Build a one-line result message for the interface, distinguishing a
        safety-limit stop, complete loading, and interruption.

        [ZH]
        为界面生成一行结果说明，区分安全上限停止、加载完成与中断。
        """
        if stop_reason == "error":
            return _t("hub.load.error", loaded=loaded, error=error)
        if stop_reason == "aborted":
            return _t("hub.load.aborted", loaded=loaded)
        if has_more:
            reason = {"items": _t("hub.load.cap_items"), "requests": _t("hub.load.cap_requests"),
                      "seconds": _t("hub.load.cap_seconds")}.get(stop_reason, "")
            return _t("hub.load.more", loaded=loaded, reason=reason)
        if total_available is not None:
            return _t("hub.load.done", count=total_available)
        if cache_hit:
            return _t("hub.load.cached", loaded=loaded)
        return _t("hub.search.count", count=loaded, note="")

    def query_dataset(
        self,
        dataset: Dict[str, Any],
        *,
        keyword: str = "",
        filters: Optional[Dict[str, Any]] = None,
        sort: str = "",
        page: Any = 1,
        page_size: Any = 0,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        [EN]
        Apply keyword filtering, structured filtering, sorting, and pagination
        to the loaded dataset, entirely locally.

        No remote request is made, and the three-level counts (after local
        filtering / loaded / total) are computed together.

        [ZH]
        在已加载数据集上执行关键词筛选、结构化筛选、排序与分页，全部在本地完成。

        不重新请求远端，并一次性计算三层计数（本地筛选后 / 已加载 / 总数）。
        """
        from model_query import query as _query

        items = list(dataset.get("items") or [])
        labels = {}
        for item in items:
            code = str(item.get("quant_code") or "").upper()
            if code:
                labels.setdefault(code, code)
        labels.setdefault("UNKNOWN", _t("hub.unknown"))
        return _query(
            items,
            keyword=keyword if keyword is not None else dataset.get("keyword") or "",
            filters=filters,
            sort=sort or dataset.get("sort") or "downloads",
            page=page,
            page_size=int(page_size or self.page_size),
            now=time.time() if now is None else now,
            loaded=dataset.get("loaded"),
            total_available=dataset.get("total_available"),
            has_more=bool(dataset.get("has_more")),
            quant_labels=labels,
        )

    def query_session(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Implement the ``model_hub_query`` action by querying one page from the
        in-memory dataset.

        When no dataset exists, return ``needs_load=True`` so the frontend
        triggers a load. This prevents a stale, unexplained list from appearing
        after a backend restart.

        [ZH]
        实现 ``model_hub_query`` 动作：从内存数据集中查询一页。

        若不存在数据集，返回 ``needs_load=True``，由前端触发加载。这样后端重启后
        不会显示来源不明的旧列表。
        """
        session = getattr(self, "_session", None)
        if not session or not session.get("items"):
            return {"needs_load": True, "loaded": 0, "filtered": 0, "items": [],
                    "page": 1, "pages": 1, "page_size": self.page_size,
                    "message": _t("hub.query.needs_load")}
        dataset = self._dataset(session, self.resolve_source(session.get("source")),
                                self.load_limits(), cache_hit=False, cache_age=0.0)
        result = self.query_dataset(
            dataset,
            keyword=str(request.get("keyword") or ""),
            filters=request.get("filters"),
            sort=str(request.get("sort") or ""),
            page=request.get("page") or 1,
            page_size=request.get("page_size") or self.page_size,
        )
        result.update({
            "needs_load": False,
            "offline": dataset.get("offline"),
            "source_note": dataset.get("source_note"),
            "downloads_metric": dataset.get("downloads_metric"),
            "downloads_fallback_count": dataset.get("downloads_fallback_count"),
            "pinned": dataset.get("pinned") or [],
            "message": dataset.get("message") or "",
            "recommend_basis": dataset.get("recommend_basis") or {},
        })
        return result

    def _split_local_and_default(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        [EN]
        Separate the default model from locally downloaded models and return
        ``(pinned, local_items)``.

        The default model is pinned at the top, labels its source, and is
        excluded from pagination counts. It therefore lives in ``pinned`` and
        not in ``items``, so the total and page count exclude it while it remains
        visible above the list.

        [ZH]
        将默认模型与本地已下载模型分开，返回 ``(pinned, local_items)``。

        默认模型固定在顶部、标注来源，且不计入分页计数。因此它放在 ``pinned``
        而不是 ``items`` 中：总数与页数不包含它，但它始终显示在列表上方。
        """
        entries = self._local_and_default_items()
        pinned = [it for it in entries if it.get("is_default")]
        rest = [it for it in entries if not it.get("is_default")]
        return pinned, rest

    def offline_dataset(self, error: Any = "", keyword: str = "", sort: str = "downloads",
                        source: Optional[str] = None) -> Dict[str, Any]:
        """
        [EN]
        Build an offline degraded dataset containing cached results, locally
        downloaded models, and the default model.

        Cache-first ordering is intentional: a cached result for the same keyword
        is more useful than an empty list plus a network error. Expired caches
        are still used and the response is marked as offline.

        [ZH]
        构造离线降级数据集，包含缓存结果、本地已下载模型与默认模型。

        有意采用缓存优先顺序：同一关键词的缓存结果比空列表加网络错误更有用。
        过期缓存仍会使用，并将响应标记为离线模式。
        """
        resolved = self.resolve_source(source)
        cached = self._read_cache(self._cache_key((keyword or "").strip(), sort, resolved["key"]),
                                  allow_expired=True)
        if cached is None:
            cached = {"items": [], "keyword": keyword, "sort": sort, "source": resolved["key"],
                      "stop_reason": "none", "has_more": False, "fallback_count": 0,
                      "created_at": time.time(), "requests": 0, "pages": 0,
                      "key": self._cache_key((keyword or "").strip(), sort, resolved["key"]),
                      "offline": True, "error": ""}
        cached["offline"] = True
        cached["source_note"] = resolved["note"]
        dataset = self._dataset(cached, resolved, self.load_limits(), cache_hit=bool(cached["items"]),
                               cache_age=time.time() - float(cached.get("created_at") or 0))
        dataset["offline"] = True
        dataset["message"] = _t("hub.search.offline", error=error)
        dataset["query"] = self.query_dataset(dataset, page=1)
        return dataset


    # [EN] ---------- Default model, local models, hardware recommendation ----------
    # [ZH] ---------- 默认模型、本地模型与硬件推荐 ----------

    def default_repo(self) -> str:
        """
        [EN]
        Return the repository of the default model (``config.model_repo``).

        The market lists repositories, while the default model is configured as
        a file name in ``config.model_path``. A remotely sorted page is not
        guaranteed to contain the repository of the default model: it may not
        match the keyword, may fall outside the first page by download count, or
        may be filtered out by a hardware recommendation.

        The fix is structural: pin the default model's repository into the list
        (see :meth:`_local_and_default_items`) independently of search criteria.
        It always appears first, so "recommended but absent from the list" cannot
        occur.

        [ZH]
        返回默认模型所属仓库（``config.model_repo``）。

        市场列出的是仓库，而默认模型配置为 ``config.model_path`` 中的文件名。按远端
        排序的一页结果不能保证包含默认模型所在仓库：它可能不匹配关键词、按下载量
        排在第一页之外，或被硬件推荐筛选条件排除。

        修复方式是结构性的：将默认模型仓库固定加入列表（见
        :meth:`_local_and_default_items`），不受搜索条件影响，始终显示在第一位。
        因此“推荐了但列表中不存在”的矛盾在结构上不会发生。
        """
        cfg = self.app_config
        repo = str(getattr(cfg, "model_repo", "") or "").strip() if cfg is not None else ""
        return repo

    def _local_and_default_items(self) -> List[Dict[str, Any]]:
        """
        [EN]
        Build entries for the two supplementary sources: the default model and
        locally downloaded models. Neither has remote metadata.

        These entries have no download, like, or update metadata, so the
        interface must display them by source label rather than as zero
        downloads; see ``note_key``.

        [ZH]
        构造两个补充来源的条目：默认模型与本地已下载模型。两者都没有远端元数据。

        这些条目没有下载量、点赞或更新时间，因此界面必须按来源标注展示，而不是
        显示为零次下载；参见 ``note_key``。
        """
        items: List[Dict[str, Any]] = []
        default_repo = self.default_repo()
        default_name = ""
        cfg = self.app_config
        if cfg is not None:
            default_name = os.path.basename(str(getattr(cfg, "model_path", "") or ""))
        if default_repo or default_name:
            repo = default_repo or default_name
            local_path = os.path.join(self.models_dir, default_name) if default_name else ""
            inst = bool(local_path) and os.path.isfile(local_path)
            items.append({
                "repo": repo,
                "name": repo.split("/")[-1] or default_name,
                "author": repo.split("/")[0] if "/" in repo else "",
                "downloads": 0, "downloads_all_time": None, "downloads_recent": 0,
                "downloads_source": "none",
                "likes": 0, "updated_at": "", "created_at": "",
                "quantization": guess_quantization(default_name or repo),
                "quant_code": guess_quant_code(default_name or repo),
                "gated": False, "tags": [],
                "description": "", "pipeline_tag": "", "library_name": "",
                "installed": inst,
                "origin": "local" if inst else "default",
                "is_default": True,
                "note_key": "hub.note.default_model",
            })
        # [EN] Locally downloaded models, including files placed manually in
        # [EN] models/; the registry records them.
        # [ZH] 本地已下载模型，包括手工放入 models/ 的文件；注册表会记录它们。
        try:
            entries = self.registry().get("models", [])
        except Exception:  # noqa: BLE001
            entries = []
        for entry in entries:
            repo = str(entry.get("repo") or "").strip()
            if not repo or repo == default_repo:
                continue
            items.append({
                "repo": repo,
                "name": repo.split("/")[-1],
                "author": repo.split("/")[0] if "/" in repo else "",
                "downloads": 0, "downloads_all_time": None, "downloads_recent": 0,
                "downloads_source": "none",
                "likes": 0, "updated_at": "", "created_at": "",
                "quantization": str(entry.get("quantization") or guess_quantization(repo)),
                "quant_code": guess_quant_code(repo) or guess_quant_code(str(entry.get("name") or "")),
                "gated": False, "tags": [],
                "description": "", "pipeline_tag": "", "library_name": "",
                "installed": True,
                "origin": "local",
                "note_key": "hub.note.local_model",
            })
        return items

    @staticmethod
    def _param_billions(text: str) -> float:
        """
        [EN]
        Extract the parameter scale in billions from a name such as
        ``Qwen3.5-0.8B-GGUF``; return 0 when no scale is present.

        The pattern requires a number followed by ``b`` or ``B`` and not
        followed by another letter, so quantization labels such as ``Q4_K_M``,
        ``F16``, and ``IQ2_XS`` are not mistaken for parameter counts.

        [ZH]
        从 ``Qwen3.5-0.8B-GGUF`` 等名称中提取参数量（十亿）；无法识别时返回 0。

        正则要求数字后紧跟 ``b`` 或 ``B``，且其后不是字母，因此 ``Q4_K_M``、
        ``F16``、``IQ2_XS`` 等量化标签不会被误判为参数量。
        """
        match = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![A-Za-z])", os.path.basename(text or ""))
        if not match:
            return 0.0
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0

    def recommend_basis(self) -> Dict[str, Any]:
        """
        [EN]
        Return the local hardware recommendation basis: memory and CPU cores
        mapped to a suggested parameter scale.

        The existing hardware detection and recommendation implementation in
        ``model_profile`` is reused, so the settings model page and the market
        recommendation markers share one source of truth.

        [ZH]
        返回本机硬件推荐依据：内存与 CPU 核数映射到建议参数量。

        复用 ``model_profile`` 中已有的硬件探测与推荐实现，使设置中的模型页与市场
        推荐标记共享同一事实来源。
        """
        try:
            from model_profile import _read_machine_memory, recommend_config

            ram, cores = _read_machine_memory()
            advice = recommend_config(ram, cores)
            return {
                "ram_gb": advice.get("ram_gb"),
                "cpu_cores": advice.get("cpu_cores"),
                "params": advice.get("params"),
                "quantization": advice.get("quantization"),
                "max_params_b": self._param_billions(str(advice.get("params") or "")),
                "reasons": advice.get("reasons") or [],
            }
        except Exception:  # noqa: BLE001
            self.logger.debug("硬件推荐探测失败（只按默认模型标记）", exc_info=True)
            return {"max_params_b": 0.0, "reasons": []}

    def _mark_recommended(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        [EN]
        Add recommendation markers based on hardware fit and default-model
        status. Markers are added only; entries are never removed.

        Returns the recommendation basis, including ``recommended_count``, so
        the interface can explain why an entry is marked as recommended.

        [ZH]
        根据硬件适配情况与默认模型状态添加推荐标记。只添加标记，绝不删除条目。

        返回推荐依据，包括 ``recommended_count``，使界面能够解释条目的推荐原因。
        """
        basis = self.recommend_basis()
        max_b = float(basis.get("max_params_b") or 0.0)
        default_repo = self.default_repo()
        for item in items:
            repo = str(item.get("repo") or "")
            size_b = self._param_billions(repo)
            reasons: List[str] = []
            if item.get("is_default"):
                reasons.append("default")
            if repo and default_repo and repo == default_repo and "default" not in reasons:
                reasons.append("default")
                item["is_default"] = True
            if max_b and size_b and size_b <= max_b + 1e-9:
                reasons.append("fit")
            item["param_b"] = size_b
            item["recommended"] = bool(reasons)
            item["recommend_reasons"] = reasons
        basis["recommended_count"] = sum(1 for it in items if it.get("recommended"))
        self._last_basis = basis
        return basis

    def scan(self) -> Dict[str, Any]:
        """
        [EN]
        Rescan the model directory and return the same inventory as the model
        registry.

        ``registry()`` already registers manually placed ``.gguf`` files, so a
        rescan only re-reads the directory and removes records whose files no
        longer exist.

        [ZH]
        重新扫描模型目录，并返回与模型库一致的清单。

        ``registry()`` 会登记手工放入的 ``.gguf`` 文件，因此重新扫描只需重新读取
        目录，并移除文件已不存在的记录。
        """
        self._ensure_dirs()
        self.invalidate_installed()
        info = self.registry()
        return {"models_dir": self.models_dir, "count": info.get("count", 0),
                "models": info.get("models", []),
                "total_size_human": info.get("total_size_human", "0 B")}

    # [EN] ---------- Detail ----------
    # [ZH] ---------- 详情 ----------

    def detail(self, repo: str, source: Optional[str] = None) -> Dict[str, Any]:
        """
        [EN]
        Return model details: available GGUF files (different quantization
        versions) and their sizes.

        [ZH]
        返回模型详情：可用的 GGUF 文件（不同量化版本）及其大小。
        """
        repo = (repo or "").strip().strip("/")
        if not repo:
            raise ModelHubError(_t("hub.error.missing_repo"))
        resolved = self.resolve_source(source)
        base = resolved["base"]
        url = f"{base}/api/models/{repo}?blobs=true"
        try:
            info = self._get_json(url, timeout=25)
        except Exception as exc:  # noqa: BLE001
            raise ModelHubError(_t("hub.error.detail_failed", error=exc)) from exc
        if not isinstance(info, dict):
            raise ModelHubError(_t("hub.error.detail_bad_format"))

        files: List[Dict[str, Any]] = []
        for sibling in info.get("siblings") or []:
            if not isinstance(sibling, dict):
                continue
            name = str(sibling.get("rfilename") or "").strip()
            if not name.lower().endswith(".gguf"):
                continue
            lfs = sibling.get("lfs") or {}
            size = lfs.get("size") or sibling.get("size") or 0
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = 0
            files.append({
                "filename": name,
                "size": size,
                "size_human": _human(size),
                "quantization": guess_quantization(name),
                "sha256": str(lfs.get("sha256") or ""),
            })
        files.sort(key=lambda f: (f["size"] or 0))

        gated = info.get("gated")
        local_dir = self.models_dir
        for item in files:
            item["installed"] = os.path.isfile(os.path.join(local_dir, item["filename"]))

        return {
            "repo": repo,
            "author": str(info.get("author") or repo.split("/")[0]),
            "downloads": int(info.get("downloads") or 0),
            "likes": int(info.get("likes") or 0),
            "updated_at": str(info.get("lastModified") or "")[:10],
            "tags": [str(t) for t in (info.get("tags") or [])][:20],
            "gated": bool(gated) and gated is not False,
            "license": self._license_of(info),
            "files": files,
            "source": resolved["key"],
            "source_note": resolved["note"],
            "models_dir": local_dir,
        }

    @staticmethod
    def _license_of(info: Dict[str, Any]) -> str:
        for tag in info.get("tags") or []:
            text = str(tag)
            if text.startswith("license:"):
                return text.split(":", 1)[1]
        return ""

    # [EN] ---------- Download ----------
    # [ZH] ---------- 下载 ----------

    def download(
        self,
        repo: str,
        filename: str,
        expected_size: int = 0,
        source: Optional[str] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        retries: int = 3,
        verify_sha256: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        [EN]
        Download a GGUF file into the model library and return
        ``{ok, path, message, ...}``.

        Supports resumable transfer through a ``.part`` file and the Range
        header, retries, live progress callbacks, and cancellation.

        [ZH]
        将指定 GGUF 文件下载到模型库，返回 ``{ok, path, message, ...}``。

        支持通过 ``.part`` 文件与 Range 头进行断点续传、失败重试、实时进度回调与
        取消。
        """
        repo = (repo or "").strip().strip("/")
        filename = os.path.basename((filename or "").strip())
        if not repo or not filename:
            raise ModelHubError(_t("hub.error.missing_repo_or_file"))

        self._ensure_dirs()
        dest = os.path.join(self.models_dir, filename)
        partial = dest + ".part"
        resolved = self.resolve_source(source)
        url = f"{resolved['base']}/{repo}/resolve/main/{urllib.parse.quote(filename)}?download=true"
        verify = bool(getattr(self.cfg, "verify_sha256", True)) if verify_sha256 is None else bool(verify_sha256)

        if os.path.isfile(dest) and expected_size and os.path.getsize(dest) == expected_size:
            return {"ok": True, "path": dest, "message": _t("hub.download.exists", filename=filename),
                    "skipped": True}

        last_error = ""
        for attempt in range(1, max(1, retries) + 1):
            if should_stop and should_stop():
                return {"ok": False, "path": "", "message": _t("hub.download.cancelled"), "cancelled": True,
                        "partial": partial}
            try:
                result = self._download_once(
                    url, dest, partial, expected_size, resolved, on_progress, should_stop, attempt, retries
                )
                if result.get("ok") and verify:
                    self._verify(dest, expected_size, result.pop("sha256", ""))
                result["sha256"] = ""
                self._register(dest, repo, resolved["key"], source_note=resolved["note"])
                return result
            except _Cancelled:
                return {"ok": False, "path": "", "message": _t("hub.download.cancelled_resumable"),
                        "cancelled": True, "partial": partial}
            except _NeedToken as exc:
                return {"ok": False, "path": "", "message": str(exc), "need_token": True}
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self.logger.warning("下载尝试 %s/%s 失败：%s", attempt, retries, last_error)
                if attempt < retries:
                    if on_progress:
                        on_progress({"retry": attempt, "retries": retries, "error": last_error})
                    time.sleep(min(2 * attempt, 6))
        return {"ok": False, "path": "", "message": _t("hub.download.failed", retries=retries,
                                                       error=last_error)}

    def _download_once(
        self,
        url: str,
        dest: str,
        partial: str,
        expected_size: int,
        resolved: Dict[str, str],
        on_progress: Optional[Callable[[Dict[str, Any]], None]],
        should_stop: Optional[Callable[[], bool]],
        attempt: int,
        retries: int,
    ) -> Dict[str, Any]:
        """
        [EN]
        Perform one download attempt, including resumable transfer. Raises on
        failure and raises ``_Cancelled`` when cancelled.

        [ZH]
        执行单次下载，包含断点续传。失败时抛出异常，取消时抛出 ``_Cancelled``。
        """
        resume_at = os.path.getsize(partial) if os.path.isfile(partial) else 0
        headers = self._headers()
        if resume_at:
            headers["Range"] = f"bytes={resume_at}-"

        request = urllib.request.Request(url, headers=headers)
        started = time.time()
        start_bytes = resume_at
        try:
            response = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise _NeedToken(_t("hub.download.need_token")) from exc
            if exc.code == 416 and resume_at:
                # [EN] HTTP 416 with a partial file means the file is already complete.
                # [ZH] 带部分文件时返回 HTTP 416，表示文件已完整。
                os.replace(partial, dest)
                return {"ok": True, "path": dest, "message": _t("hub.download.reused_local")}
            raise

        with response:
            if resume_at and response.status != 206:
                # [EN] The server does not support resumable transfer; restart
                # [EN] from the beginning.
                # [ZH] 服务端不支持断点续传；从头开始下载。
                resume_at = 0
                start_bytes = 0
            total = expected_size
            if not total:
                length = response.headers.get("Content-Length")
                if length and length.isdigit():
                    total = int(length) + resume_at
            mode = "ab" if resume_at else "wb"
            done = resume_at
            last_report = 0.0
            with open(partial, mode) as fh:
                while True:
                    if should_stop and should_stop():
                        raise _Cancelled()
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if on_progress and now - last_report >= 0.4:
                        elapsed = max(1e-6, now - started)
                        speed = (done - start_bytes) / elapsed
                        eta = int((total - done) / speed) if total and speed > 0 else 0
                        last_report = now
                        on_progress({
                            "done": done,
                            "total": total,
                            "percent": (done * 100.0 / total) if total else 0.0,
                            "speed": speed,
                            "eta": eta,
                            "attempt": attempt,
                            "retries": retries,
                            "source": resolved["key"],
                        })

        size = os.path.getsize(partial)
        if expected_size and size != expected_size:
            raise ModelHubError(
                _t("hub.download.incomplete", done=_human(size), total=_human(expected_size))
            )
        os.replace(partial, dest)
        if on_progress:
            on_progress({"done": size, "total": size or expected_size, "percent": 100.0, "speed": 0.0, "eta": 0})
        return {
            "ok": True,
            "path": dest,
            "size": size,
            "message": _t("hub.download.done", name=os.path.basename(dest), size=_human(size)),
        }

    def _verify(self, path: str, expected_size: int, expected_sha: str) -> None:
        """
        [EN]
        Verify integrity by comparing the file size. When a SHA-256 is available
        and the file is at most 2 GB, also compare the digest.

        [ZH]
        通过比较文件大小校验完整性。存在 SHA-256 且文件不超过 2 GB 时，再比较摘要。
        """
        size = os.path.getsize(path)
        if expected_size and size != expected_size:
            raise ModelHubError(_t("hub.verify.size_mismatch", actual=_human(size),
                                   expected=_human(expected_size)))
        if expected_sha and size <= 2 * 1024 ** 3:
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest().lower() != expected_sha.lower():
                raise ModelHubError(_t("hub.verify.sha_mismatch"))

    # [EN] ---------- Model registry ----------
    # [ZH] ---------- 模型库 ----------

    def _load_registry(self) -> Dict[str, Any]:
        path = self.registry_path
        if not os.path.isfile(path):
            return {"version": 1, "models": []}
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("registry 不是对象")
            data.setdefault("version", 1)
            data.setdefault("models", [])
            if not isinstance(data["models"], list):
                data["models"] = []
            return data
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("模型库记录损坏，已忽略：%s", exc)
            return {"version": 1, "models": []}

    def _save_registry(self, data: Dict[str, Any]) -> None:
        try:
            with open(self.registry_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("模型库记录写入失败：%s", exc)

    def _register(self, path: str, repo: str, source_key: str, source_note: str = "",
                  downloaded_at: str = "") -> Dict[str, Any]:
        """
        [EN]
        Register a model in the model registry, updating an existing entry with
        the same path.

        [ZH]
        将模型登记到模型库；同一路径的条目会被更新。
        """
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        stamp = downloaded_at or time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            data = self._load_registry()
            entry = next((m for m in data["models"] if os.path.basename(str(m.get("name", ""))) == name), None)
            if entry is None:
                entry = {"name": name}
                data["models"].append(entry)
            entry.update({
                "name": name,
                "path": path,
                "repo": repo or str(entry.get("repo", "")),
                "quantization": guess_quantization(name),
                "size": size,
                "size_human": _human(size),
                "downloaded_at": str(entry.get("downloaded_at") or stamp),
                "source": source_key,
                "source_note": source_note,
            })
            entry.setdefault("last_used", "")
            self._save_registry(data)
        # [EN] A completed download means the repository is now installed, so
        # [EN] the cache must be invalidated; otherwise the installed marker
        # [EN] would remain stale until restart.
        # [ZH] 下载完成后该仓库已安装，因此必须使缓存失效；否则已安装标记会一直
        # [ZH] 保持旧状态直到重启。
        self.invalidate_installed()
        return entry

    def touch(self, name: str) -> None:
        """
        [EN]
        Record the model's last-used timestamp. The backend calls this after
        loading a model.

        [ZH]
        记录模型的最后使用时间。后端在加载模型后调用。
        """
        base = os.path.basename(str(name or ""))
        if not base:
            return
        with self._lock:
            data = self._load_registry()
            changed = False
            for entry in data["models"]:
                if os.path.basename(str(entry.get("name", ""))) == base:
                    entry["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    changed = True
                    break
            if changed:
                self._save_registry(data)

    def registry(self, current_model: str = "") -> Dict[str, Any]:
        """
        [EN]
        Return the model registry reconciled with disk contents, including
        entries, total size, and the model currently in use.

        [ZH]
        返回与磁盘内容核对后的模型库，包括条目、总占用与当前使用中的模型。
        """
        self._ensure_dirs()
        data = self._load_registry()
        entries: List[Dict[str, Any]] = []
        known = set()
        for entry in data["models"]:
            name = os.path.basename(str(entry.get("name", "")))
            if not name:
                continue
            path = os.path.join(self.models_dir, name)
            record = dict(entry)
            record["name"] = name
            record["path"] = path
            record["exists"] = os.path.isfile(path)
            record["size"] = os.path.getsize(path) if record["exists"] else int(entry.get("size") or 0)
            record["size_human"] = _human(record["size"])
            record["quantization"] = str(entry.get("quantization") or guess_quantization(name))
            record["source"] = str(entry.get("source") or "local")
            record["last_used"] = str(entry.get("last_used") or "")
            record["downloaded_at"] = str(entry.get("downloaded_at") or "")
            record["current"] = bool(current_model) and os.path.basename(current_model) == name
            record["in_use"] = bool(current_model) and os.path.basename(current_model) == name
            entries.append(record)
            known.add(name)

        # [EN] Files placed manually in models/ must also appear in the registry,
        # [EN] so a visible file is never invisible in the model list.
        # [ZH] 手工放入 models/ 的文件也必须出现在模型库中，避免文件存在但列表不可见。
        for name in sorted(os.listdir(self.models_dir)):
            if name in known or not name.lower().endswith(".gguf"):
                continue
            path = os.path.join(self.models_dir, name)
            size = os.path.getsize(path)
            entries.append({
                "name": name, "path": path, "repo": "", "quantization": guess_quantization(name),
                "size": size, "size_human": _human(size), "downloaded_at": "",
                "last_used": "", "source": "local", "exists": True,
                "current": bool(current_model) and os.path.basename(current_model) == name,
                "in_use": bool(current_model) and os.path.basename(current_model) == name,
            })

        entries.sort(key=lambda e: (not e["current"], e["name"].lower()))
        total = sum(int(e["size"] or 0) for e in entries if e["exists"])
        orphans = [e["name"] for e in entries if not e["exists"]]
        if orphans:
            data["models"] = [m for m in data["models"]
                              if os.path.basename(str(m.get("name", ""))) not in set(orphans)]
            self._save_registry(data)
        return {
            "models": entries,
            "count": len(entries),
            "total_size": total,
            "total_size_human": _human(total),
            "models_dir": self.models_dir,
            "registry_file": self.registry_path,
        }

    def uninstall(self, name: str, current_model: str = "") -> Dict[str, Any]:
        """
        [EN]
        Delete a model file and remove it from the registry. The frontend
        requests confirmation first.

        [ZH]
        删除模型文件并将其从模型库移除。前端会先请求确认。
        """
        base = os.path.basename(str(name or "").strip())
        if not base:
            raise ModelHubError(_t("hub.uninstall.missing_name"))
        if current_model and os.path.basename(current_model) == base:
            raise ModelHubError(_t("hub.uninstall.in_use"))
        path = os.path.join(self.models_dir, base)
        removed = False
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed = True
            except OSError as exc:
                raise ModelHubError(_t("hub.uninstall.remove_failed", error=exc)) from exc
        with self._lock:
            data = self._load_registry()
            before = len(data["models"])
            data["models"] = [m for m in data["models"]
                              if os.path.basename(str(m.get("name", ""))) != base]
            self._save_registry(data)
        if not removed and before == len(data["models"]):
            raise ModelHubError(_t("hub.uninstall.not_in_registry", name=base))
        self.invalidate_installed()
        return {"ok": True, "name": base, "removed_file": removed,
                "message": (_t("hub.uninstall.done_removed", name=base) if removed
                            else _t("hub.uninstall.done_cleaned", name=base))}

    def _installed_repos(self) -> set:
        """
        [EN]
        Return the set of repositories with locally stored models.

        ``_is_installed_repo`` is called for every search result, and
        ``registry()`` reads and reconciles ``models_registry.json`` each time.
        Without a cache, a data set of 5000 entries would cause 5000 file
        reads. The cache is invalidated by :meth:`invalidate_installed` after
        downloads, deletions, and rescans.

        [ZH]
        返回本地已有模型落盘的仓库集合。

        ``_is_installed_repo`` 会为每条搜索结果调用一次，而 ``registry()`` 每次都
        读取并核对 ``models_registry.json``。若没有缓存，5000 条结果会造成 5000 次
        文件读取。下载、删除与重新扫描后通过 :meth:`invalidate_installed` 使缓存
        失效。
        """
        cached = getattr(self, "_installed_cache", None)
        if cached is not None:
            return cached
        try:
            entries = self.registry()["models"]
        except Exception:  # noqa: BLE001
            entries = []
        repos = {str(e.get("repo") or "") for e in entries if e.get("repo")}
        self._installed_cache = repos
        return repos

    def invalidate_installed(self) -> None:
        """
        [EN]
        Clear the installed-repository cache after a registry change such as a
        completed download, deletion, or rescan.

        [ZH]
        在模型库发生变化（下载完成、删除或重新扫描）后清除已下载仓库缓存。
        """
        self._installed_cache = None

    def _is_installed_repo(self, repo: str) -> bool:
        """
        [EN]
        Return whether a model from this repository is already on disk, used for
        the installed marker in the list.

        [ZH]
        返回该仓库是否已有模型落盘，用于列表中的已安装标记。
        """
        if not repo:
            return False
        return repo in self._installed_repos()

    def status(self) -> Dict[str, Any]:
        """
        [EN]
        Return model-hub status: current data source, registry size, and whether
        a token is configured.

        [ZH]
        返回模型中心状态：当前数据源、模型库规模与是否配置了 Token。
        """
        resolved = self.resolve_source()
        info = self.registry() if os.path.isdir(self.models_dir) else {"count": 0, "total_size_human": "0 B"}
        return {
            "enabled": bool(getattr(self.cfg, "enabled", True)),
            "source_setting": self.source_setting,
            "source": resolved["key"],
            "source_note": resolved["note"],
            "mirror": MIRROR_ENDPOINT,
            "official": OFFICIAL_ENDPOINT,
            "token_configured": bool(self.token),
            "models_dir": self.models_dir,
            "registry_file": self.registry_path,
            "count": int(info.get("count") or 0),
            "total_size_human": info.get("total_size_human", "0 B"),
        }


class _Cancelled(Exception):
    """
    [EN]
    Internal exception: the user cancelled the download.

    [ZH]
    内部异常：用户取消了下载。
    """


class _NeedToken(Exception):
    """
    [EN]
    Internal exception: a Hugging Face token is required.

    [ZH]
    内部异常：需要 Hugging Face Token。
    """
