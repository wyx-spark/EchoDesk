"""Hugging Face 模型选择与下载（任务书 新增四：模型选择栏）。

设计要点：

* **双数据源**：``https://hf-mirror.com``（国内推荐）与 ``https://huggingface.co``（官方），
  支持自动探测可达性、不可用时回退，并始终在返回值里说明当前用的是哪个源。
* **搜索/详情**直接走两家共有的公开 REST 接口：
  ``/api/models`` 与 ``/api/models/{repo}?blobs=true``（镜像站是官方站的反向代理，路径一致）。
* **下载**自己用 urllib 实现，而不是 ``hf_hub_download``：因为 ``HF_ENDPOINT`` 是模块级常量、
  运行时无法为单次调用切换数据源，而自实现还能同时满足任务书要求的
  **断点续传、失败重试、实时速度/剩余时间、取消、完整性校验**。
* **模型库**记录在 ``models_registry.json``，并与 ``models/`` 目录实际文件核对，
  手动放进目录的模型也会被登记（``source=local``）。
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

# 多语言（任务三）：i18n 反向依赖 config 是惰性的（只在取文案时读 program_root），
# 本模块又不导入 config，因此这里直接导入不会成环。
from i18n import t as _t

__all__ = ["ModelHub", "ModelHubError", "MIRROR_ENDPOINT", "OFFICIAL_ENDPOINT",
           "guess_quantization", "guess_quant_code", "EXPAND_FIELDS",
           "MAX_PAGE_SIZE", "HARD_MAX_ITEMS"]

log = logging.getLogger("EchoDesk.ModelHub")

MIRROR_ENDPOINT = "https://hf-mirror.com"
OFFICIAL_ENDPOINT = "https://huggingface.co"

# 量化格式识别：Q4_K_M / Q5_0 / IQ2_XS / F16 / BF16 …
_QUANT_RE = re.compile(
    r"(?i)\b(i?q\d+(?:_[a-z0-9]+)*|f16|fp16|bf16|f32|fp32|q8_0|q4_0)\b"
)

#: 服务端排序字段 → (sort 参数, direction 参数)。
#:
#: **口径警告（v0.2.1 模型市场优化 实测）**：服务端的 ``sort=downloads`` 排的是
#: **最近 30 天下载量**（该列单调递减，而历史总下载量列并不单调），而且不接受
#: ``sort=downloadsAllTime``（实测 HTTP 400），``direction=1`` 也会 400。
#: 因此这里的远端排序只决定"抓取窗口里装哪些条目"，**展示顺序一律由本地按
#: 历史总下载量重排**（见 :func:`model_query.sort_items`），否则"按下载量排序"
#: 与"显示历史总下载量"会自相矛盾。``params`` 服务端没有对应排序，用 downloads 兜底。
_SORT_MAP = {
    "downloads": ("downloads", -1),
    "updated": ("lastModified", -1),
    "likes": ("likes", -1),
    "params": ("downloads", -1),
    "name": ("modelId", 1),
}

#: 服务端单页上限。``limit`` 超过 1000 会被**静默截断到 1000**
#: （实测 limit=2000 / 5000 都只回 1000 条），所以分页一律按 1000 发，
#: 而不是"请求 5000 让服务端自己截"。
MAX_PAGE_SIZE = 1000

#: 发请求时要 expand 的字段。
#:
#: **坑一**：一旦传了 ``expand``，响应体就**只包含被 expand 的字段**
#: （实测默认字段 tags/likes/downloads 全部消失，只剩 _id/id/trendingScore），
#: 所以必须一次列全，不能"只加一个 downloadsAllTime"。
#: **坑二**：同一个字段名重复出现会直接 HTTP 400（实测），因此下面用
#: ``dict.fromkeys`` 去重后再拼参数 —— 写死一份列表 + 动态追加是很容易踩到的。
EXPAND_FIELDS = (
    "downloadsAllTime", "downloads", "likes", "lastModified", "createdAt",
    "tags", "author", "gated", "private", "pipeline_tag", "library_name",
)

#: 自动加载的安全上限（任务二）。三个闸门任一触发即停，并告知"已加载 N 条，还有更多"。
DEFAULT_MAX_ITEMS = 5000
DEFAULT_MAX_REQUESTS = 8
DEFAULT_MAX_SECONDS = 90.0
#: 硬上限：用户反复点"继续加载"也不能无限增长（防止把内存吃光）
HARD_MAX_ITEMS = 20000

#: 磁盘缓存版本号：缓存结构变了就把它 +1，旧缓存自然失效，不需要清理逻辑
CACHE_VERSION = 1


class ModelHubError(RuntimeError):
    """模型中心的可预期错误（网络不可达、仓库无 GGUF、校验失败等）。"""


def guess_quant_code(filename: str) -> str:
    """从文件名/仓库名推断量化代码（``Qwen3.5-0.8B-Q4_K_M.gguf`` → ``Q4_K_M``）。

    取不到时返回**空串**而不是本地化的"未知"：这个值会被当作**筛选键**用
    （见 :mod:`model_query`），把界面文案当数据会让"界面语言一换、筛选条件就失效"。
    界面要显示的那份走 :func:`guess_quantization`。
    """
    base = os.path.splitext(os.path.basename(filename or ""))[0]
    matches = _QUANT_RE.findall(base)
    if matches:
        return matches[-1].upper()
    return ""


def guess_quantization(filename: str) -> str:
    """量化格式的**显示文本**（取不到时是本地化的"未知"）。"""
    return guess_quant_code(filename) or _t("hub.unknown")


def _human(size: Optional[int]) -> str:
    """字节数转人类可读（1 位小数）。"""
    if not size:
        return _t("hub.unknown")
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


class ModelHub:
    """模型中心：搜索 / 详情 / 下载 / 模型库管理。"""

    def __init__(self, cfg: Any, logger: Optional[logging.Logger] = None,
                 app_config: Any = None) -> None:
        """
        ``cfg``        ：``config.model_hub``（数据源、模型目录、Token 等）；
        ``app_config`` ：顶层 ``Config``，**只读**用于取默认模型（``model_path`` /
                         ``model_repo``）。分成两个参数是因为"默认模型是谁"属于应用级
                         配置，不属于模型中心自己的配置；没有它就无法把默认模型钉进列表，
                         也就答不上"默认模型为什么在列表里看不到"这个问题（任务三）。
        """
        self.cfg = cfg
        self.app_config = app_config
        self.logger = logger or log
        self._lock = __import__("threading").Lock()
        self._source_cache: Dict[str, Any] = {}
        self._last_basis: Dict[str, Any] = {}
        #: "本地已下载仓库"集合缓存（见 _installed_repos）
        self._installed_cache: Optional[set] = None
        #: 当前已加载数据集（内存缓存，供 model_hub_query 做本地筛选/翻页）。
        #: 只在内存里，后端重启即失效；磁盘缓存另有 _read_cache/_write_cache。
        self._session: Optional[Dict[str, Any]] = None

    # ---------- 配置读取 ----------

    @property
    def source_setting(self) -> str:
        """用户设定的数据源：auto / mirror / official。"""
        raw = str(getattr(self.cfg, "source", "auto") or "auto").strip().lower()
        return raw if raw in ("auto", "mirror", "official") else "auto"

    @property
    def token(self) -> str:
        """Hugging Face 访问令牌（Gated 模型需要），优先取配置，其次取环境变量。"""
        raw = str(getattr(self.cfg, "hf_token", "") or "").strip()
        if raw:
            return raw
        return (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or "").strip()

    @property
    def models_dir(self) -> str:
        raw = str(getattr(self.cfg, "models_dir", "models") or "models").strip()
        return os.path.abspath(os.path.expanduser(raw))

    @property
    def registry_path(self) -> str:
        raw = str(getattr(self.cfg, "registry_file", "models_registry.json") or "models_registry.json").strip()
        return os.path.abspath(os.path.expanduser(raw))

    # 模型下载默认使用程序所在目录下的 models/，与后端既有约定一致
    def _ensure_dirs(self) -> None:
        os.makedirs(self.models_dir, exist_ok=True)

    # ---------- HTTP 基础 ----------

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
        """探测数据源是否可达（只取一条搜索结果，开销很小）。"""
        try:
            self._get_json(f"{base}/api/models?limit=1", timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("数据源不可达 %s: %s", base, exc)
            return False

    def resolve_source(self, requested: Optional[str] = None) -> Dict[str, str]:
        """决定本次请求使用哪个数据源，返回 ``{key, base, note}``。

        ``auto`` 会优先镜像站（国内通常更快），不可达时回退官方站；两者都不可达时
        仍返回镜像站并把情况写进 note，由调用方给出明确提示。
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

    # ---------- 搜索 ----------

    def load_limits(self) -> Dict[str, Any]:
        """自动加载的安全上限（任务二：防止关键词过宽导致请求失控）。

        **三个闸门任一触发即停**：总条数、请求次数、耗时。数值全部可配
        （``config.model_hub``），并受 :data:`HARD_MAX_ITEMS` 绝对封顶 ——
        用户反复点"继续加载"也只是每一轮重新计数，不可能无限增长。
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
        """本地分页的每页条数默认值（任务三：默认 20，可选 20/50/100）。"""
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
        """搜索结果磁盘缓存目录（任务二"缓存在本地（内存 + 磁盘）"）。

        默认落在**用户数据目录**下的 ``cache/model_hub``：那里一定可写、会随用户数据
        一起备份，而且不会污染程序目录（便携版里 ``app\\`` 是程序本体）。
        配置里写了 ``search_cache_dir`` 就用它（相对路径按当前工作目录解析）。
        """
        raw = str(getattr(self.cfg, "search_cache_dir", "") or "").strip()
        if raw:
            return os.path.abspath(os.path.expanduser(raw))
        try:
            from config import user_data_root

            return os.path.join(user_data_root(), "cache", "model_hub")
        except Exception:  # noqa: BLE001 - 取不到用户目录就退到配置/模型库同级
            return os.path.abspath(os.path.join(os.path.dirname(self.registry_path), "hub_cache"))

    # ---------- HTTP 分页 ----------

    def _search_params(self, keyword: str, sort: str, cursor: str = "",
                       page_limit: int = MAX_PAGE_SIZE) -> List[Tuple[str, str]]:
        """拼一次搜索结果请求的参数（列表形式，因为 ``expand`` 要重复出现）。"""
        sort_key, direction = _SORT_MAP.get((sort or "downloads").lower(), _SORT_MAP["downloads"])
        # dict.fromkeys 去重：同一个 expand 字段出现两次会让服务端直接 400（实测）
        params: List[Tuple[str, str]] = [
            ("filter", "gguf"),
            ("sort", sort_key),
            ("direction", str(direction)),
            ("limit", str(max(1, min(int(page_limit or MAX_PAGE_SIZE), MAX_PAGE_SIZE)))),
        ]
        # 带 search 时不要 full=true（默认字段足够，full 会把 siblings 一起塞进来）
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
        """同 :meth:`_get_json`，但把响应头一并返回（分页游标在 ``Link`` 头里）。"""
        request = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", "replace")
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        return json.loads(payload), headers

    @staticmethod
    def cursor_from_link(link: str) -> str:
        """从 ``Link: <...>; rel="next"`` 里取出下一页游标；没有下一页返回空串。

        **只取 ``cursor`` 查询参数再拼回当前数据源**，不能照抄整个 URL：
        实测镜像站返回的 Link 指向的是 ``https://huggingface.co/...``，
        照抄就等于把后续分页全部打到官方站 —— 在只通镜像的网络里直接断流
        （本机实测官方站超时，就是靠这条才没有在第 2 页卡死）。
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
        """取下载量：返回 ``(展示用值, 30 天值, 口径来源)``。

        任务一的口径要求：默认显示**历史总下载量**。服务端要显式 ``expand=downloadsAllTime``
        才会返回它；万一条目缺失该字段（接口变更/镜像差异），**回退到 30 天下载量并标注
        来源**，绝不静默显示成 0 —— 那会让用户以为这个模型根本没人下载过。
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
        """把一条远端结果整理成列表项（含下载量口径与量化代码）。"""
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
        # 列表接口不返回模型卡正文，"描述"只能由接口给的元数据合成（记录里有偏差说明）
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
        """第 1 页失败时换另一家数据源再试一次（与既有"双数据源"约定一致）。"""
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
        """自动分页取完某个关键词的全部结果（任务二），返回**已加载数据集**。

        流程与取舍：

        1. **缓存优先**（任务二"同一关键词再次搜索时优先使用缓存"）：内存会话 →
           未过期的磁盘缓存 → 真正联网。``refresh=True`` 跳过前两级（手动刷新）。
        2. **分页循环**：每页 1000（服务端上限），跟着 ``Link`` 里的游标一直往下取，
           直到服务端没有下一页（此时 ``total_available`` 就是**精确总数**）
           或撞上安全上限。
        3. **失败重试**：单页失败重试 ``download_retries`` 次（指数退避）；
           连续失败则中止，并把"已加载到哪"一并返回（不丢已经拿到的结果）。
        4. **进度反馈**：每页通过 ``on_progress`` 回调报数，
           界面据此显示"正在加载 500 / ... "而不是干等。
        5. **``resume=True``**：在已有结果后面接着取（"继续加载"按钮），
           受 :data:`HARD_MAX_ITEMS` 绝对封顶。

        返回结构见 :meth:`_dataset`（含 ``items`` / ``pinned`` / ``loaded`` /
        ``total_available`` / ``has_more`` / ``stop_reason`` / ``cache`` / ``query``）。
        """
        caps = self.load_limits()
        keyword = (keyword or "").strip()
        sort = sort if sort in _SORT_MAP else "downloads"
        resolved = self.resolve_source(source)
        cache_key = self._cache_key(keyword, sort, resolved["key"])
        started = time.time()

        base_record = self._current_record(cache_key) if resume else None
        if resume and base_record is None:
            # 没有可续的底子（后端正则重启过、缓存被删）：退化为全新加载，
            # 而不是报错让用户一脸茫然
            resume = False

        if not resume and not refresh:
            cached = self._read_cache(cache_key, allow_expired=False)
            if cached is not None:
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
        #: 本轮"继续加载"的起点。安全上限按**每轮**计算而不是按累计：
        #: 否则第二次点"继续加载"时"距离上限还差 0 条"会退化成一次只取 1 条
        #: （实测踩到过），而任务书要求"继续加载"就是要能突破单轮上限；
        #: 累计规模另有 HARD_MAX_ITEMS 绝对封顶。
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
                stop_reason = "none"          # 服务端没有下一页 == 真的取完了
                break
            # 本页只请求"还差多少就到上限"那么多条：服务端一页最多 1000，
            # 不这样算的话上限是"软"的 —— 上限 100 时第一页会一次塞进 250 条
            # （实测踩到过：max_items=100 却返回 250 条，且续载逻辑随之全乱）。
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
                        # 第 1 页失败先换数据源（比在同一条死路上重试更有意义）
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
                stop_reason = "none"          # 服务端没有下一页 == 真的取完了
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
        # 只有在真的联网取过、或本来就是有效结果时才写缓存；
        # 一页都没取到（远端全挂）时不写，免得把"空结果"当成缓存固化下来
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
        """搜索 GGUF 模型：**先加载数据集，再在本地做筛选/排序/分页**。

        返回 ``{items, pinned, total, page, source, source_note, offline, message,
        query, ...}``，其中 ``items`` 是**第 page 页**的内容（默认第 1 页、每页 20 条）。

        与旧版的区别（v0.2.1 模型市场优化）：

        * ``items`` 不再是"服务端一页 20 条"，而是"已加载的全部结果经过本地
          筛选+排序后的当前页"——这也是"用户最多只能看到 21 条"那个问题的修法；
        * ``limit`` 现在是**本地每页条数**的兼容别名（旧调用方传 ``limit=5``），
          真正的远端页大小固定 1000（服务端上限），由 :meth:`load_all` 决定；
        * ``min_downloads`` / ``quant`` 从"远端过滤参数"改为**本地筛选条件**，
          因为远端过滤会让"已加载 N 条"与实际能筛的东西对不上；
        * 默认模型不再混在 ``items`` 里，而是单独放在 ``pinned``（任务七：
          "钉在最前、明确标注来源、不干扰分页计数"）。
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

    # ---------- 已加载数据集 → 页面（本地筛选 / 排序 / 分页） ----------

    def _cache_key(self, keyword: str, sort: str, source_key: str) -> str:
        raw = f"{CACHE_VERSION}|{source_key}|{keyword.strip().lower()}|{sort}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, cache_key: str) -> str:
        return os.path.join(self.cache_dir, f"{cache_key}.json")

    def _read_cache(self, cache_key: str, allow_expired: bool = False) -> Optional[Dict[str, Any]]:
        """读磁盘缓存。``allow_expired=True`` 用于离线降级（过期数据也比空白强）。"""
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
        """写磁盘缓存（失败只记日志：缓存写不进去不该让搜索整体失败）。"""
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
        """取"可续加载"的底子：内存会话优先，其次磁盘缓存（含过期）。"""
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
        """把缓存记录组装成界面用的数据集（合并本地来源、加推荐标记、算三层数字）。"""
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

        # 推荐标记要覆盖 pinned（它不在 items 里，漏了就没有"为什么推荐"的依据）
        basis = self._mark_recommended(remote_items + pinned)

        total_available = record.get("total_available")
        if record.get("stop_reason") == "none" and not record.get("next_cursor"):
            # 真的取完了 ⇒ 分页数据集的大小就是精确总数。
            # 这里用**合并后**的条数（含本地已下载来源），因为界面上的"共 N 条"与
            # "第 x / y 页"必须自洽：分页是对整个数据集算的。
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
            "total": loaded,                 # 兼容旧字段：总条数（含本地来源）
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
            # 下载量口径（任务一）：条目级来源标记 + 汇总，界面据此说明"历史总下载量"
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
        """给界面的一句话结果说明（撞上限 / 取完 / 中断各有各的说法）。"""
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
        """在已加载数据集上执行 关键词筛选 → 结构化筛选 → 排序 → 分页（纯本地）。

        这是任务三/四/五/六的落点：**不重新请求远端**，
        三层数字（本地筛选后 / 已加载 / 共 N 条）一次算齐。
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
        """``model_hub_query`` 动作的实现：在**后端内存里的当前数据集**上查一页。

        没有当前数据集时返回 ``needs_load=True``，由前端触发一次加载 ——
        这样后端重启后界面也不会显示一份来路不明的旧列表。
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
        """把"默认模型"与"本地已下载"拆开：返回 ``(pinned, local_items)``。

        任务七要求默认模型"钉在最前、明确标注来源、不干扰分页计数"，所以它单独成一份
        （``pinned``），**不进 ``items``**：这样"共 N 条 / 第 x/y 页"里就没有它，
        而它永远显示在列表上方，位置固定、来源写清（见 :meth:`default_repo`）。
        """
        entries = self._local_and_default_items()
        pinned = [it for it in entries if it.get("is_default")]
        rest = [it for it in entries if not it.get("is_default")]
        return pinned, rest

    def offline_dataset(self, error: Any = "", keyword: str = "", sort: str = "downloads",
                        source: Optional[str] = None) -> Dict[str, Any]:
        """离线降级数据集（任务七）：远端不可达时给出**缓存 + 本地已下载 + 默认模型**。

        顺序刻意是"缓存优先"：磁盘上有同一关键词的结果说明用户之前搜到过，
        比"一片空白 + 一句网络错误"有用得多。缓存过期也照用，并标注是离线模式。
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


    # ---------- 任务三：默认模型 / 本地模型 / 硬件推荐 ----------

    def default_repo(self) -> str:
        """默认模型所属仓库（``config.model_repo``）。

        **根因记录（任务书要求排查"默认模型在列表里显示不出来"）**：
        模型的"市场列表"列的是**仓库**（例如 ``unsloth/Qwen3.5-0.8B-GGUF``），
        而默认值是 ``config.model_path`` 里的**文件名**（``Qwen3.5-0.8B-Q4_K_M.gguf``）。
        列表此前完全由"按下载量排序的一页远端搜索结果"构成，**没有任何逻辑保证
        默认模型所在的那个仓库会出现**：仓库名不含关键词时搜不到，下载量排在几十名
        开外时第一页也看不到，而"按硬件过滤推荐"的开关一开更是直接被筛掉。
        修法不是去调过滤条件，而是**把默认模型的仓库钉进列表**（见 :meth:`_local_and_default_items`）：
        它与搜索条件无关，永远出现在第一位。这样"推荐了这个但列表里没有"的矛盾从
        结构上就不可能发生。
        """
        cfg = self.app_config
        repo = str(getattr(cfg, "model_repo", "") or "").strip() if cfg is not None else ""
        return repo

    def _local_and_default_items(self) -> List[Dict[str, Any]]:
        """构造"默认模型"与"本地已下载"两个补充来源的条目（不含远端元数据）。

        这两类条目**没有远端元数据**（下载量/点赞/更新时间都是空），
        界面必须按"来源标注"而不是按"0 次下载"来展示 —— 见 `note_key`。
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
        # 本地已下载（含手工放进 models/ 的文件，registry 会把它们登记进来）
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
        """从 ``Qwen3.5-0.8B-GGUF`` 这类名字里取出参数规模（十亿）；取不到返回 0。

        正则要求数字后紧跟 ``b``/``B`` 且再后面不是字母，因此量化标签
        （``Q4_K_M`` / ``F16`` / ``IQ2_XS``）不会被误当成参数量。
        """
        match = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![A-Za-z])", os.path.basename(text or ""))
        if not match:
            return 0.0
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0

    def recommend_basis(self) -> Dict[str, Any]:
        """本机硬件推荐依据（内存 / 核数 → 建议参数规模）。

        复用既有的硬件探测与推荐实现（``model_profile``），不另起一套规则，
        因此"设置→模型"里显示的推荐配置与市场里的推荐标记是同一个来源。
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
        """按硬件推荐与"是否默认模型"给条目加标记（**只加标记，绝不删条目**）。

        返回本次使用的推荐依据（含 `recommended_count`），供调用方一并回传给界面：
        "为什么这条是推荐的"必须能回答，否则标记就是个没来由的装饰。
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
        """重新扫描模型文件夹（任务二/五）：返回与模型库一致的清单。

        ``registry()`` 会把手工放进目录的 ``.gguf`` 一并登记，因此"扫描"真正的动作
        就是重新读一次目录（外加清掉记录里已不存在的条目）。
        """
        self._ensure_dirs()
        self.invalidate_installed()
        info = self.registry()
        return {"models_dir": self.models_dir, "count": info.get("count", 0),
                "models": info.get("models", []),
                "total_size_human": info.get("total_size_human", "0 B")}

    # ---------- 详情 ----------

    def detail(self, repo: str, source: Optional[str] = None) -> Dict[str, Any]:
        """模型详情：可用 GGUF 文件清单（不同量化版本）与各自大小。"""
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

    # ---------- 下载 ----------

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
        """下载指定 GGUF 文件到模型库，返回 ``{ok, path, message, ...}``。

        支持断点续传（``.part`` 文件 + Range）、失败重试、实时进度回调与取消。
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
        """单次下载（含断点续传）；失败抛异常，取消抛 _Cancelled。"""
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
            if exc.code == 416 and resume_at:      # 断点已完整
                os.replace(partial, dest)
                return {"ok": True, "path": dest, "message": _t("hub.download.reused_local")}
            raise

        with response:
            if resume_at and response.status != 206:
                # 服务端不支持断点续传：从头开始
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
        """完整性校验：比对文件大小；有 sha256 且文件不超过 2GB 时再比对哈希。"""
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

    # ---------- 模型库 ----------

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
        """把模型登记到模型库（同路径覆盖更新）。"""
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
        # 下载完成即"这个仓库已下载"——缓存必须失效，否则列表上的「已下载」标记要等重启
        self.invalidate_installed()
        return entry

    def touch(self, name: str) -> None:
        """记录模型"最后使用时间"（后端在加载模型后调用）。"""
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
        """模型库列表：与磁盘核对，返回条目、总占用与当前使用中的模型。"""
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

        # 手工放进 models/ 的文件也要出现在模型库里，避免"看得见文件却看不到模型"
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
        """删除模型文件并从模型库移除（前端会先确认）。"""
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
        """本地已落盘的仓库集合。

        **为什么要缓存**：``_is_installed_repo`` 会对**每一条**搜索结果查一次模型库，
        而 ``registry()`` 每次都要读一遍 ``models_registry.json`` 并核对磁盘。
        一页 20 条时看不出来，自动加载到 5000 条就是 5000 次文件读写 ——
        这是"下载量口径改了、加载也改成全量了"之后必须一起修掉的性能坑。
        缓存由 :meth:`invalidate_installed` 在下载/删除/扫描后失效。
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
        """模型库发生变化（下载完成 / 删除 / 重新扫描）后清掉"已下载仓库"缓存。"""
        self._installed_cache = None

    def _is_installed_repo(self, repo: str) -> bool:
        """该仓库是否已有模型落盘（用于列表上的「已下载」标记）。"""
        if not repo:
            return False
        return repo in self._installed_repos()

    def status(self) -> Dict[str, Any]:
        """模型中心状态：当前数据源、模型库规模、是否需要 Token。"""
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
    """内部：用户取消下载。"""


class _NeedToken(Exception):
    """内部：需要 Hugging Face Token。"""
