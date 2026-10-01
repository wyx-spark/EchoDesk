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

__all__ = ["ModelHub", "ModelHubError", "MIRROR_ENDPOINT", "OFFICIAL_ENDPOINT", "guess_quantization"]

log = logging.getLogger("EchoDesk.ModelHub")

MIRROR_ENDPOINT = "https://hf-mirror.com"
OFFICIAL_ENDPOINT = "https://huggingface.co"

# 量化格式识别：Q4_K_M / Q5_0 / IQ2_XS / F16 / BF16 …
_QUANT_RE = re.compile(
    r"(?i)\b(i?q\d+(?:_[a-z0-9]+)*|f16|fp16|bf16|f32|fp32|q8_0|q4_0)\b"
)

_SORT_MAP = {
    "downloads": ("downloads", -1),
    "updated": ("lastModified", -1),
    "likes": ("likes", -1),
    "name": ("modelId", 1),
}


class ModelHubError(RuntimeError):
    """模型中心的可预期错误（网络不可达、仓库无 GGUF、校验失败等）。"""


def guess_quantization(filename: str) -> str:
    """从文件名推断量化格式（如 ``Qwen3.5-0.8B-Q4_K_M.gguf`` → ``Q4_K_M``）。"""
    base = os.path.splitext(os.path.basename(filename or ""))[0]
    matches = _QUANT_RE.findall(base)
    if matches:
        return matches[-1].upper()
    return _t("hub.unknown")


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

    def search(
        self,
        keyword: str = "",
        sort: str = "downloads",
        page: int = 1,
        limit: int = 20,
        source: Optional[str] = None,
        min_downloads: int = 0,
        quant: str = "",
    ) -> Dict[str, Any]:
        """按名称/作者/标签搜索 GGUF 模型。

        返回 ``{items, total, source, source_note, offline, message, page, ...}``。

        **任务三（v0.2.1 综合优化）：不再按硬件过滤隐藏任何模型。**
        列表由三个来源合并去重，用户看到的是完整可选范围：

        1. **远端仓库搜索**（主来源）：Hugging Face / 镜像站的 ``filter=gguf`` 结果；
        2. **默认模型**：``config.json`` 的 ``model_path`` + ``model_repo`` 对应的仓库，
           无论它有没有出现在远端结果里都**钉在最前面** —— 这正是任务书要排查的那个
           "默认模型在列表里显示不出来"的坑（根因见 :meth:`default_repo`）；
        3. **本地已下载**：模型库里已落盘模型所属的仓库（远端搜不到时也会出现在列表里，
           标记为 ``source=local``，避免"我明明下载了却搜不到"）。

        去重按 ``repo``：远端结果优先（它有下载量等元数据），本地/默认来源只补缺。
        硬件推荐只**加标记**（``recommended`` / ``param_b`` / ``recommend_reasons``），
        不删条目；排序上把默认模型与推荐项排到前面，其余保持请求的排序。
        """
        resolved = self.resolve_source(source)
        base = resolved["base"]
        sort_key, direction = _SORT_MAP.get((sort or "downloads").lower(), _SORT_MAP["downloads"])
        limit = max(1, min(int(limit or 20), 100))
        page = max(1, int(page or 1))

        params = {
            "filter": "gguf",
            "sort": sort_key,
            "direction": str(direction),
            "limit": str(limit),
            "full": "false",
        }
        keyword = (keyword or "").strip()
        if keyword:
            params["search"] = keyword
        url = f"{base}/api/models?" + urllib.parse.urlencode(params)

        try:
            raw = self._get_json(url)
        except Exception as exc:  # noqa: BLE001
            other = OFFICIAL_ENDPOINT if base == MIRROR_ENDPOINT else MIRROR_ENDPOINT
            other_name = _t("hub.source.official") if other == OFFICIAL_ENDPOINT else _t("hub.source.mirror")
            fallback: Any = None
            try:
                fallback = self._get_json(url.replace(base, other, 1))
            except Exception:  # noqa: BLE001
                fallback = None
            if fallback is None:
                # 远端不可达也要给出可用内容：默认模型 + 本地已下载仍然列出来，
                # 用户至少能看到"我本地有什么、默认该下哪个"，而不是一片空白（任务三的列表源）。
                offline_items = self._local_and_default_items()
                offline_basis = self._mark_recommended(offline_items)
                offline_items.sort(key=lambda it: (0 if it.get("is_default") else
                                                   (1 if it.get("recommended") else 2)))
                return {
                    "items": offline_items, "total": len(offline_items), "page": page,
                    "source": resolved["key"], "source_note": resolved["note"],
                    "offline": True, "source_items": {"remote": 0, "merged": len(offline_items)},
                    # 离线也要给推荐依据：界面上的"(推荐)"标记与"为什么推荐"是同一份数据，
                    # 少了它标记就成了没来由的（实测：离线分支漏了它，界面无法解释推荐原因）。
                    "recommend_basis": offline_basis,
                    "message": _t("hub.search.offline", error=exc),
                }
            raw = fallback
            resolved = {"key": "official" if other == OFFICIAL_ENDPOINT else "mirror", "base": other,
                        "note": _t("hub.source.note_fallback_other", name=other_name)}

        if not isinstance(raw, list):
            raw = []

        items: List[Dict[str, Any]] = []
        seen: set = set()
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("modelId") or entry.get("id") or "").strip()
            if not model_id:
                continue
            downloads = int(entry.get("downloads") or 0)
            if min_downloads and downloads < min_downloads:
                continue
            quant_guess = guess_quantization(model_id)
            if quant and quant.lower() not in quant_guess.lower() and quant.lower() not in model_id.lower():
                continue
            gated = entry.get("gated")
            seen.add(model_id)
            items.append({
                "repo": model_id,
                "name": model_id.split("/")[-1],
                "author": str(entry.get("author") or model_id.split("/")[0]),
                "downloads": downloads,
                "likes": int(entry.get("likes") or 0),
                "updated_at": str(entry.get("lastModified") or "")[:10],
                "quantization": quant_guess,
                "gated": bool(gated) and gated is not False,
                "tags": [str(t) for t in (entry.get("tags") or [])][:12],
                "installed": self._is_installed_repo(model_id),
                "origin": "remote",
            })

        remote_count = len(items)
        # 来源 2/3：默认模型 + 本地已下载（只补缺，不重复）
        for extra in self._local_and_default_items():
            if str(extra.get("repo") or "") in seen:
                continue
            seen.add(str(extra.get("repo") or ""))
            items.append(extra)

        self._mark_recommended(items)
        # 排序：默认模型最前，其次推荐项，最后保持原有顺序（任务书允许"推荐排前或加标记"，
        # 这里两者都做——标记负责"能不能看出来"，排序负责"第一眼看到能跑的"）。
        items.sort(key=lambda it: (0 if it.get("is_default") else (1 if it.get("recommended") else 2)))

        return {
            "items": items,
            "total": len(items),
            "page": page,
            "source": resolved["key"],
            "source_note": resolved["note"],
            "offline": False,
            "source_items": {"remote": remote_count, "merged": len(items) - remote_count},
            "recommend_basis": self.recommend_basis(),
            "message": (_t("hub.search.count", count=len(items), note=resolved["note"]) if items
                        else _t("hub.search.empty")),
        }

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
        """构造"默认模型"与"本地已下载"两个补充来源的条目（不含远端元数据）。"""
        items: List[Dict[str, Any]] = []
        default_repo = self.default_repo()
        default_name = ""
        cfg = self.app_config
        if cfg is not None:
            default_name = os.path.basename(str(getattr(cfg, "model_path", "") or ""))
        if default_repo or default_name:
            repo = default_repo or default_name
            local_path = os.path.join(self.models_dir, default_name) if default_name else ""
            items.append({
                "repo": repo,
                "name": repo.split("/")[-1] or default_name,
                "author": repo.split("/")[0] if "/" in repo else "",
                "downloads": 0, "likes": 0, "updated_at": "",
                "quantization": guess_quantization(default_name or repo),
                "gated": False, "tags": [],
                "installed": bool(local_path) and os.path.isfile(local_path),
                "origin": "local" if (local_path and os.path.isfile(local_path)) else "default",
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
                "downloads": 0, "likes": 0, "updated_at": "",
                "quantization": str(entry.get("quantization") or ""),
                "gated": False, "tags": [],
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
        return {"ok": True, "name": base, "removed_file": removed,
                "message": (_t("hub.uninstall.done_removed", name=base) if removed
                            else _t("hub.uninstall.done_cleaned", name=base))}

    def _is_installed_repo(self, repo: str) -> bool:
        """该仓库是否已有模型落盘（用于列表上的「已下载」标记）。"""
        try:
            entries = self.registry()["models"]
        except Exception:  # noqa: BLE001
            return False
        return any(str(e.get("repo", "")) == repo for e in entries)

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
