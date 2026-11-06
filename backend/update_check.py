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

import json
import re
import time
import urllib.error
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
