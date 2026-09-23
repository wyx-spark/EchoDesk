# -*- coding: utf-8 -*-
"""自动更新检查（任务书 任务五 · 方案 A：只检查与提示）。

**本模块刻意不做的事**：不下载发布包、不替换任何文件、不静默安装。
用户拿到"有新版本"的提示后，自己点开浏览器到 Releases 页面下载。
理由见 docs/v0.2.1-研发记录.md：开源首版优先保证稳定与可控，
方案 B（内置下载替换）需要处理文件占用、替换失败回滚、企业代理与证书，
本版不做；方案 C（增量清单 + 签名校验）留待有发布基础设施后再评估。

体积说明（任务书 5.4）：只用标准库 urllib + json，
不引入任何新的 HTTP 或 JSON 依赖。
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

#: 检查超时（秒）——启动时的检查不能拖慢界面
_TIMEOUT = 8
#: GitHub API 要求的 User-Agent
_UA = f"EchoDesk/{APP_VERSION.lstrip('v')} (+update-check)"


def parse_version(text: str) -> Tuple[int, ...]:
    """把语义化版本解析成元组，便于比较（``v0.2.10`` > ``v0.2.9``）。"""
    raw = str(text or "").strip().lstrip("vV")
    # 取主版本部分，丢掉 -beta.1 / +build 之类的后缀
    core = re.split(r"[-+]", raw, maxsplit=1)[0]
    parts: List[int] = []
    for chunk in core.split("."):
        digits = re.match(r"^\d+", chunk)
        parts.append(int(digits.group()) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def compare_versions(current: str, latest: str) -> int:
    """比较版本：返回 1 表示 latest 更新，-1 表示更旧（降级），0 表示相同。"""
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
    """判断是否到了该检查的时间；返回 (是否该检查, 配置的间隔小时数)。"""
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
    """请求 GitHub Releases API；尊重系统代理设置（urllib 默认走环境代理）。"""
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
    """检查是否有新版本。

    返回 ``{ok, status, current, latest, url, notes, message, checked_at}``，
    ``status`` 为 ``up_to_date`` / ``available`` / ``disabled`` / ``skipped`` / ``error``。
    任何网络异常都以 ``ok=False`` 返回，不抛给调用方（更新检查不该影响使用）。
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
    except Exception as e:  # noqa: BLE001 —— 网络问题不该弹出堆栈
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
