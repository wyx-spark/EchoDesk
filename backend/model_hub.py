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
    return "未知"


def _human(size: Optional[int]) -> str:
    """字节数转人类可读（1 位小数）。"""
    if not size:
        return "未知"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


class ModelHub:
    """模型中心：搜索 / 详情 / 下载 / 模型库管理。"""

    def __init__(self, cfg: Any, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.logger = logger or log
        self._lock = __import__("threading").Lock()
        self._source_cache: Dict[str, Any] = {}

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
            return {"key": "mirror", "base": MIRROR_ENDPOINT, "note": "使用镜像源 hf-mirror.com（手动指定）"}
        if choice == "official":
            return {"key": "official", "base": OFFICIAL_ENDPOINT, "note": "使用官方源 huggingface.co（手动指定）"}

        mirror_ok = self._probe(MIRROR_ENDPOINT)
        if mirror_ok:
            return {"key": "mirror", "base": MIRROR_ENDPOINT,
                    "note": "自动选择：镜像源 hf-mirror.com（可达）"}
        if self._probe(OFFICIAL_ENDPOINT):
            return {"key": "official", "base": OFFICIAL_ENDPOINT,
                    "note": "自动选择：镜像源不可达，已回退官方源 huggingface.co"}
        return {"key": "mirror", "base": MIRROR_ENDPOINT,
                "note": "⚠️ 镜像源与官方源均不可达，请检查网络或代理设置"}

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

        返回 ``{items, total, source, source_note, offline, message, page}``。
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
            other_name = "官方源 huggingface.co" if other == OFFICIAL_ENDPOINT else "镜像源 hf-mirror.com"
            fallback: Any = None
            try:
                fallback = self._get_json(url.replace(base, other, 1))
            except Exception:  # noqa: BLE001
                fallback = None
            if fallback is None:
                return {
                    "items": [], "total": 0, "page": page,
                    "source": resolved["key"], "source_note": resolved["note"],
                    "offline": True, "message": f"❌ 两个数据源都无法访问：{exc}",
                }
            raw = fallback
            resolved = {"key": "official" if other == OFFICIAL_ENDPOINT else "mirror", "base": other,
                        "note": f"⚠️ 原数据源请求失败，已回退 {other_name}"}

        if not isinstance(raw, list):
            raw = []

        items: List[Dict[str, Any]] = []
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
            })

        return {
            "items": items,
            "total": len(items),
            "page": page,
            "source": resolved["key"],
            "source_note": resolved["note"],
            "offline": False,
            "message": f"共 {len(items)} 个结果（数据源：{resolved['note']}）" if items else "没有匹配的 GGUF 模型。",
        }

    # ---------- 详情 ----------

    def detail(self, repo: str, source: Optional[str] = None) -> Dict[str, Any]:
        """模型详情：可用 GGUF 文件清单（不同量化版本）与各自大小。"""
        repo = (repo or "").strip().strip("/")
        if not repo:
            raise ModelHubError("缺少模型仓库名。")
        resolved = self.resolve_source(source)
        base = resolved["base"]
        url = f"{base}/api/models/{repo}?blobs=true"
        try:
            info = self._get_json(url, timeout=25)
        except Exception as exc:  # noqa: BLE001
            raise ModelHubError(f"获取模型详情失败：{exc}") from exc
        if not isinstance(info, dict):
            raise ModelHubError("模型详情返回格式异常。")

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
            raise ModelHubError("缺少模型仓库或文件名。")

        self._ensure_dirs()
        dest = os.path.join(self.models_dir, filename)
        partial = dest + ".part"
        resolved = self.resolve_source(source)
        url = f"{resolved['base']}/{repo}/resolve/main/{urllib.parse.quote(filename)}?download=true"
        verify = bool(getattr(self.cfg, "verify_sha256", True)) if verify_sha256 is None else bool(verify_sha256)

        if os.path.isfile(dest) and expected_size and os.path.getsize(dest) == expected_size:
            return {"ok": True, "path": dest, "message": f"模型已存在：{filename}", "skipped": True}

        last_error = ""
        for attempt in range(1, max(1, retries) + 1):
            if should_stop and should_stop():
                return {"ok": False, "path": "", "message": "已取消下载。", "cancelled": True,
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
                return {"ok": False, "path": "", "message": "已取消下载（已保留断点，可继续）。",
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
        return {"ok": False, "path": "", "message": f"下载失败（已重试 {retries} 次）：{last_error}"}

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
                raise _NeedToken(
                    "该模型需要登录授权（Gated Model）。请在「设置 → 模型」中填写 Hugging Face Token 后重试。"
                ) from exc
            if exc.code == 416 and resume_at:      # 断点已完整
                os.replace(partial, dest)
                return {"ok": True, "path": dest, "message": "已使用本地已下载完成的文件。"}
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
                f"下载不完整（{_human(size)} / {_human(expected_size)}），已保留断点，重试可继续。"
            )
        os.replace(partial, dest)
        if on_progress:
            on_progress({"done": size, "total": size or expected_size, "percent": 100.0, "speed": 0.0, "eta": 0})
        return {
            "ok": True,
            "path": dest,
            "size": size,
            "message": f"已下载并加入模型库：{os.path.basename(dest)}（{_human(size)}）",
        }

    def _verify(self, path: str, expected_size: int, expected_sha: str) -> None:
        """完整性校验：比对文件大小；有 sha256 且文件不超过 2GB 时再比对哈希。"""
        size = os.path.getsize(path)
        if expected_size and size != expected_size:
            raise ModelHubError(f"校验失败：文件大小为 {_human(size)}，应为 {_human(expected_size)}")
        if expected_sha and size <= 2 * 1024 ** 3:
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest().lower() != expected_sha.lower():
                raise ModelHubError("校验失败：文件 sha256 与仓库记录不一致，已保留文件但未登记入库")

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
            raise ModelHubError("缺少模型文件名。")
        if current_model and os.path.basename(current_model) == base:
            raise ModelHubError("该模型正在使用中，请先用 /model 切换到其它模型再删除。")
        path = os.path.join(self.models_dir, base)
        removed = False
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed = True
            except OSError as exc:
                raise ModelHubError(f"删除文件失败：{exc}") from exc
        with self._lock:
            data = self._load_registry()
            before = len(data["models"])
            data["models"] = [m for m in data["models"]
                              if os.path.basename(str(m.get("name", ""))) != base]
            self._save_registry(data)
        if not removed and before == len(data["models"]):
            raise ModelHubError(f"模型库里没有 {base}。")
        return {"ok": True, "name": base, "removed_file": removed,
                "message": f"已删除 {base}" + ("并更新模型库。" if removed else "（文件不存在，已清理记录）。")}

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
