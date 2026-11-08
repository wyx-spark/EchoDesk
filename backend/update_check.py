# -*- coding: utf-8 -*-
"""
[EN]
Update checking (design option A: check and notify only).

This module deliberately does not download release packages, replace files, or
install silently. When a newer version is detected, the user opens the releases
page in a browser and downloads it manually. Option B (in-app download and
replacement) would require handling file locks, rollback on replacement failure,
enterprise proxies and certificates; option C (incremental manifest with
signature verification) remains out of scope until release infrastructure exists.

Only the Python standard library (urllib, json) is used; no additional HTTP or
JSON dependency is introduced.

[ZH]
自动更新检查（设计选项 A：仅检查并提示）。

本模块刻意不下载发布包、不替换任何文件、不静默安装。检测到新版本后，
由用户在浏览器中打开 Releases 页面手动下载。选项 B（内置下载替代）需要
处理文件占用、替换失败回滚、企业代理与证书；选项 C（增量清单与签名校验）
需等待发布基础设施成熟后再评估。

仅使用 Python 标准库（urllib、json），不引入额外的 HTTP 或 JSON 依赖。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from i18n import t as _t
from version import APP_VERSION

#: [EN] Timeout for update checks, in seconds; a startup check must not delay the interface.
#: [ZH] 更新检查超时（秒）；启动阶段的检查不得拖慢界面。
_TIMEOUT = 8
#: [EN] User-Agent required by the GitHub API.
#: [ZH] GitHub API 要求的 User-Agent。
_UA = f"EchoDesk/{APP_VERSION.lstrip('v')} (+update-check)"


def parse_version(text: str) -> Tuple[int, ...]:
    """
    [EN]
    Parse a semantic version into a tuple so that versions compare correctly
    (``v0.2.10`` > ``v0.2.9``).

    [ZH]
    将语义化版本解析为元组，以保证版本比较正确（``v0.2.10`` > ``v0.2.9``）。
    """
    raw = str(text or "").strip().lstrip("vV")
    # [EN] Keep the numeric core and discard suffixes such as -beta.1 or +build.
    # [ZH] 保留数字主体，丢弃 -beta.1、+build 等后缀。
    core = re.split(r"[-+]", raw, maxsplit=1)[0]
    parts: List[int] = []
    for chunk in core.split("."):
        digits = re.match(r"^\d+", chunk)
        parts.append(int(digits.group()) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def compare_versions(current: str, latest: str) -> int:
    """
    [EN]
    Compare versions: 1 if ``latest`` is newer, -1 if it is older (downgrade),
    and 0 if the versions are equal.

    [ZH]
    比较版本：``latest`` 更新时返回 1，更旧（降级）时返回 -1，相同时返回 0。
    """
    a, b = parse_version(current), parse_version(latest)
    if b > a:
        return 1
    if b < a:
        return -1
    return 0


def _releases_url(cfg: Any) -> str:
    repo = str(getattr(cfg, "repo", "") or "").strip()
    return f"https://github.com/{repo}/releases" if repo else ""


def _api_url(cfg: Any) -> str:
    base = str(getattr(cfg, "api_base", "") or "https://api.github.com").rstrip("/")
    repo = str(getattr(cfg, "repo", "") or "").strip()
    return f"{base}/repos/{repo}/releases"


def _due(cfg: Any, force: bool) -> Tuple[bool, int]:
    """
    [EN]
    Determine whether a check is due. Returns (is_due, configured_interval_hours).

    [ZH]
    判断是否到达检查时间。返回 (是否应检查, 配置的间隔小时数)。
    """
    hours = max(1, int(getattr(cfg, "check_interval_hours", 24) or 24))
    if force:
        return True, hours
    last = str(getattr(cfg, "last_check", "") or "")
    if not last:
        return True, hours
    try:
        stamp = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return True, hours
    return (time.time() - stamp) >= hours * 3600, hours


def _fetch_releases(cfg: Any) -> List[Dict[str, Any]]:
    """
    [EN]
    Query the GitHub Releases API. System proxy settings are honoured because
    urllib uses the environment proxy configuration by default.

    [ZH]
    请求 GitHub Releases API。由于 urllib 默认读取环境代理配置，系统代理设置会被遵循。
    """
    request = urllib.request.Request(_api_url(cfg), headers={
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, list):
        raise RuntimeError("Releases API 返回了非预期的结构")
    return [item for item in payload if isinstance(item, dict)]


def check_for_update(cfg: Any, force: bool = False) -> Dict[str, Any]:
    """
    [EN]
    Check whether a newer release is available.

    Returns ``{ok, status, current, latest, url, notes, message, checked_at}``;
    ``status`` is one of ``up_to_date``, ``available``, ``disabled``, ``skipped``
    or ``error``. Network failures are returned with ``ok=False`` and are never
    propagated to the caller, because update checking must not affect normal use.

    [ZH]
    检查是否存在更新的发布版本。

    返回 ``{ok, status, current, latest, url, notes, message, checked_at}``；
    ``status`` 为 ``up_to_date``、``available``、``disabled``、``skipped``
    或 ``error``。网络故障以 ``ok=False`` 返回，不向调用方抛出，因为更新检查
    不得影响正常使用。
    """
    current = APP_VERSION
    result: Dict[str, Any] = {
        "ok": False, "status": "error", "current": current, "latest": "",
        "url": _releases_url(cfg), "notes": "", "message": "", "checked_at": "",
    }

    if not bool(getattr(cfg, "enabled", True)):
        result["status"] = "disabled"
        result["message"] = _t("update.disabled")
        result["ok"] = True
        return result

    repo = str(getattr(cfg, "repo", "") or "").strip()
    if not repo:
        result["status"] = "disabled"
        result["message"] = _t("update.repo_unset")
        result["ok"] = True
        return result

    due, interval_hours = _due(cfg, force)
    if not due:
        result["status"] = "skipped"
        result["message"] = _t("update.not_due", hours=interval_hours)
        result["ok"] = True
        result["last_check"] = str(getattr(cfg, "last_check", "") or "")
        return result

    try:
        releases = _fetch_releases(cfg)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            result["message"] = _t("update.rate_limited")
        elif e.code == 404:
            result["message"] = _t("update.repo_missing", repo=repo)
        else:
            result["message"] = _t("update.check_failed", error=f"HTTP {e.code}")
        return result
    # [EN] Network failures must not surface a traceback to the caller.
    # [ZH] 网络故障不得向调用方暴露堆栈。
    except Exception as e:  # noqa: BLE001
        result["message"] = _t("update.check_failed", error=e)
        return result

    result["ok"] = True
    result["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    include_pre = bool(getattr(cfg, "include_prerelease", False))

    candidates: List[Dict[str, Any]] = []
    for release in releases:
        if release.get("draft"):
            continue
        if release.get("prerelease") and not include_pre:
            continue
        tag = str(release.get("tag_name") or release.get("name") or "").strip()
        if tag:
            candidates.append({**release, "_tag": tag})
    if not candidates:
        result["status"] = "up_to_date"
        result["message"] = _t("update.up_to_date", version=current)
        return result

    best = max(candidates, key=lambda r: parse_version(r["_tag"]))
    latest = best["_tag"]
    result["latest"] = latest
    result["url"] = str(best.get("html_url") or _releases_url(cfg))
    result["notes"] = str(best.get("body") or "")[:4000]
    result["name"] = str(best.get("name") or "")
    # [EN] Expose the release assets so the frontend can ask the backend to download a
    # [EN] specific asset by name. The backend re-resolves the URL from the GitHub API at
    # [EN] download time, therefore the frontend never supplies an arbitrary URL.
    # [ZH] 暴露发布附件，使前端可以按名称请求后端下载指定附件。下载时后端会重新从
    # [ZH] GitHub API 解析 URL，因此前端永远不会提供任意 URL。
    assets = _release_assets(best)
    result["assets"] = assets
    preferred = pick_update_asset(assets)
    if preferred is not None:
        result["asset"] = preferred

    delta = compare_versions(current, latest)
    if delta > 0:
        result["status"] = "available"
        result["message"] = (_t("update.available", latest=latest, current=current)
                             + "\n" + _t("update.available_hint", url=result["url"]))
    elif delta < 0:
        result["status"] = "up_to_date"
        result["message"] = _t("update.ahead", current=current, latest=latest)
    else:
        result["status"] = "up_to_date"
        result["message"] = _t("update.up_to_date", version=current)
    return result

# ---------------------------------------------------------------------------
# [EN] Release assets, guarded download and user-data backup.
# [ZH] 发布附件、受限下载与用户数据备份。
# ---------------------------------------------------------------------------

#: [EN] Hosts a release asset may legitimately redirect to. GitHub's
#:   ``browser_download_url`` answers with a redirect to one of these CDN hosts;
#:   urllib follows redirects automatically, so only the initial host is checked
#:   here and the redirect target is constrained by GitHub itself.
#: [ZH] 发布附件可以合法重定向到的主机。GitHub 的 ``browser_download_url``
#:   会重定向到这些 CDN 主机；urllib 自动跟随重定向，因此这里只检查初始主机，
#:   重定向目标由 GitHub 自身约束。
_ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
}

#: [EN] Never download an asset larger than this (a release installer is ~120 MB).
#: [ZH] 拒绝下载大于此值的附件（发布安装包约 120 MB）。
_MAX_ASSET_BYTES = 1024 * 1024 * 1024

#: [EN] Small runtime files that make up user data. Models are deliberately not
#:   copied: they are large, and a model file is never overwritten by the
#:   installer because the installer excludes ``models\``.
#: [ZH] 构成用户数据的小型运行期文件。刻意不复制模型：模型很大，而且安装程序
#:   排除了 ``models\``，因此模型文件不会被覆盖。
_USER_DATA_FILES = (
    "config.json",
    "config.user.json",
    "session.json",
    "browser_state.json",
    "models_registry.json",
    "skills_registry.json",
    "skills_local.json",
    "market_cache.json",
    "summary_cache.json",
    "automation_log.jsonl",
    "automation_processed.json",
)


def _release_assets(release: Dict[str, Any]) -> List[Dict[str, Any]]:
    """[EN] Normalise the assets of one release. / [ZH] 归一化单个发布的附件列表。"""
    out: List[Dict[str, Any]] = []
    for item in release.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("browser_download_url") or "").strip()
        if not name or not url:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({
            "name": name,
            "size": size,
            "url": url,
            "digest": str(item.get("digest") or ""),
            "content_type": str(item.get("content_type") or ""),
        })
    return out


def pick_update_asset(assets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    [EN]
    Pick the asset a normal user needs: the Windows setup first, then a portable
    archive for the other platforms. Returns ``None`` when the release carries no
    downloadable artifact.

    [ZH]
    选择普通用户需要的附件：优先 Windows 安装包，其次其他平台的便携归档。
    发布没有任何可下载产物时返回 ``None``。
    """
    lowered = [(str(a.get("name") or "").lower(), a) for a in assets]

    def first(predicate) -> Optional[Dict[str, Any]]:
        for name, asset in lowered:
            if predicate(name):
                return asset
        return None

    return (
        first(lambda n: n.endswith(".exe") and "setup" in n)
        or first(lambda n: n.endswith(".exe"))
        or first(lambda n: n.endswith(".msi"))
        or first(lambda n: n.endswith((".zip", ".tar.gz", ".tgz", ".dmg", ".appimage")))
    )


def _validate_download_url(url: str, cfg: Any) -> None:
    """[EN] Reject anything that is not a GitHub release download. / [ZH] 拒绝非 GitHub 发布下载地址。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("更新附件必须使用 https")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError(f"更新附件主机不受信任：{host}")
    repo = str(getattr(cfg, "repo", "") or "").strip()
    if host == "github.com" and repo and not parsed.path.startswith(f"/{repo}/releases/download/"):
        raise ValueError("更新附件不属于当前仓库的 Releases")


def download_update_asset(cfg: Any, tag: str, asset_name: str, dest_dir: str) -> Dict[str, Any]:
    """
    [EN]
    Download one asset of a specific release. The URL is re-resolved from the
    GitHub API; only the asset *name* comes from the caller. The file is written
    to a temporary ``.part`` file, its size and (when GitHub supplies one)
    SHA-256 digest are verified, and only then is it atomically renamed. A
    failed or truncated download therefore never leaves a usable-looking file.

    [ZH]
    下载指定发布的单个附件。URL 从 GitHub API 重新解析，调用方只提供附件 *名称*。
    文件先写入临时 ``.part`` 文件，校验大小与（GitHub 提供时）SHA-256 摘要后，
    才原子重命名。因此失败或截断的下载不会留下一个看起来可用的文件。
    """
    tag = str(tag or "").strip()
    asset_name = os.path.basename(str(asset_name or "").strip())
    if not tag or not asset_name:
        raise ValueError("缺少版本标签或附件名称")
    releases = _fetch_releases(cfg)
    release = next((r for r in releases if str(r.get("tag_name") or "") == tag), None)
    if release is None:
        raise ValueError(f"找不到发布 {tag}")
    assets = _release_assets(release)
    asset = next((a for a in assets if a["name"] == asset_name), None)
    if asset is None:
        raise ValueError(f"发布 {tag} 中没有附件 {asset_name}")
    size = int(asset.get("size") or 0)
    if size <= 0 or size > _MAX_ASSET_BYTES:
        raise ValueError(f"附件大小异常：{size}")
    url = str(asset.get("url") or "")
    _validate_download_url(url, cfg)

    os.makedirs(dest_dir, exist_ok=True)
    final_path = os.path.join(dest_dir, asset_name)
    part_path = final_path + ".part"
    digest = hashlib.sha256()
    written = 0
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp, open(part_path, "wb") as handle:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_ASSET_BYTES:
                    raise ValueError("附件超过允许的最大体积")
                digest.update(chunk)
                handle.write(chunk)
        if size and written != size:
            raise ValueError(f"下载不完整：期望 {size} 字节，实际 {written} 字节")
        got = digest.hexdigest()
        expected = str(asset.get("digest") or "")
        if expected.startswith("sha256:"):
            want = expected.split(":", 1)[1].strip().lower()
            if want and want != got:
                raise ValueError("SHA-256 校验失败，下载已丢弃")
        os.replace(part_path, final_path)
    except Exception:
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "path": final_path,
        "name": asset_name,
        "tag": tag,
        "size": written,
        "sha256": got,
        "url": url,
    }


def backup_user_data(program_dir: str, data_root: str, tag: str) -> Dict[str, Any]:
    """
    [EN]
    Copy the small user-data files into an isolated backup directory under
    ``<data_root>/updates/`` before an update is applied. Model files are not
    copied; a manifest records their names and sizes so the user can see what
    the backup intentionally left in place. Both the installer and the manual
    updater rely on this backup as the last line of defence.

    [ZH]
    在应用更新之前，把小体积用户数据复制到 ``<data_root>/updates/`` 下的隔离备份目录。
    模型文件不复制；清单记录其名称与大小，让用户看到备份刻意保留了哪些文件。
    安装程序与手动更新都把这个备份当作最后一道防线。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_tag = re.sub(r"[^0-9A-Za-z._-]+", "-", str(tag or "update")).strip("-") or "update"
    backup_dir = os.path.join(data_root, "updates", f"backup-{safe_tag}-{stamp}")
    os.makedirs(backup_dir, exist_ok=True)
    copied: List[str] = []
    for rel in _USER_DATA_FILES:
        src = os.path.join(program_dir, rel)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(backup_dir, rel)
        try:
            shutil.copy2(src, dst)
            copied.append(rel)
        except OSError:
            continue
    # [EN] Outer user-data root files (UI state, snippets) live one level above app/.
    # [ZH] 外层用户数据根目录（界面状态、常用语）位于 app/ 的上一级。
    for rel in ("ui_state.json", "snippets.json"):
        src = os.path.join(data_root, rel)
        if not os.path.isfile(src):
            continue
        try:
            shutil.copy2(src, os.path.join(backup_dir, "root-" + rel))
            copied.append(rel)
        except OSError:
            continue
    models_dir = os.path.join(program_dir, "models")
    models: List[Dict[str, Any]] = []
    if os.path.isdir(models_dir):
        for name in sorted(os.listdir(models_dir)):
            path = os.path.join(models_dir, name)
            if os.path.isfile(path):
                try:
                    models.append({"name": name, "size": os.path.getsize(path)})
                except OSError:
                    models.append({"name": name, "size": 0})
    manifest = {
        "tag": str(tag or ""),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "program_dir": program_dir,
        "data_root": data_root,
        "copied": copied,
        "models_left_in_place": models,
        "note": "models are excluded from the installer and are never overwritten by an update",
    }
    with open(os.path.join(backup_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return {"ok": True, "dir": backup_dir, "copied": copied, "models": len(models)}
