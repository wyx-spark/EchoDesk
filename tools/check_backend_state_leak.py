"""
[EN]
Release-tree guard: no runtime state file may ship.

Why this exists
---------------
``tools/pack_backend.py`` copies the development ``backend/`` tree into every release tree, with an
explicit exclusion list for files that are generated on the user's machine. In v0.2.7 a probe run
left ``backend/market_cache.json`` behind (a 232-byte search cache, git-ignored), the list did not
name it, and the three standalone trees shipped it. The double-blind build did not go red only
because no probe happened to run between its two passes -- the cache content is a timestamp, so two
runs at different times would have differed. "Remember to add the next one to the list" is not a
control; this check is.

Policy, defined in exactly one place (this file)
------------------------------------------------
1. Forbidden basenames: mutable per-user state produced by the application.
2. Forbidden suffixes/globs: logs, databases and ``*_cache.json``.
3. ``config.json`` is a shipped template: it must exist and be byte-identical to the tracked
   ``backend/config.json``; a user-modified copy would otherwise ship that user's settings.

[ZH]
发布树守卫：运行期状态文件不得随包分发。

为什么需要
----------
``tools/pack_backend.py`` 会把开发树的 ``backend/`` 复制进每棵发布树，并用一份排除清单挡住"用户
机器上生成"的文件。v0.2.7 时一次探针运行在 ``backend/`` 留下 ``market_cache.json``（232 字节的搜索
缓存，被 .gitignore 忽略），清单没有点名它，三棵 standalone 树就把它带进了发布物。双盲构建没有变红
纯属侥幸——两轮之间没有探针运行；该缓存的内容是时间戳，换一个时间跑两轮就会不一致。"下次记得加进
清单"不是控制手段，本检查才是。

策略（只在本文件定义一处）
--------------------------
1. 禁止出现的文件名：应用生成的、属于用户运行期的可变状态。
2. 禁止的后缀/通配：日志、数据库与 ``*_cache.json``。
3. ``config.json`` 是随包模板：必须存在且与仓库跟踪的 ``backend/config.json`` 逐字节一致；否则
   一个被本地改过的副本就会把该用户的设置带进发布物。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: [EN] Mutable per-user state files that must never be packaged.
#: [ZH] 绝不能随包分发的用户运行期可变状态文件。
FORBIDDEN_BASENAMES = {
    "session.json",
    "market_cache.json",
    "backend_crash.log",
    "skills_dev.log",
    "config.user.json",
    "ui_state.json",
    "snippets.json",
    "automation_log.jsonl",
}

#: [EN] Suffixes that mark generated state (checked case-insensitively).
#: [ZH] 表示生成状态的后缀（不区分大小写检查）。
FORBIDDEN_SUFFIXES = (".log", ".db", ".sqlite", ".sqlite3", ".cache")

#: [EN] Filename globs that mark generated state.
#: [ZH] 表示生成状态的文件名通配。
FORBIDDEN_GLOBS = ("*_cache.json",)

#: [EN] The shipped config template, and its authoritative source in the repository.
#: [ZH] 随包配置模板，以及仓库里的权威来源。
CONFIG_BASENAME = "config.json"
CONFIG_SOURCE = os.path.join(ROOT, "backend", CONFIG_BASENAME)


def app_version() -> str:
    path = os.path.join(ROOT, "backend", "version.py")
    with open(path, encoding="utf-8") as handle:
        match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', handle.read())
    if not match:
        raise SystemExit(f"读不出 APP_VERSION：{path}")
    return match.group(1).strip().lstrip("v")


def scan_roots(version: str) -> list[str]:
    roots = [os.path.join(ROOT, "publish", f"EchoDesk-v{version}", "app", "backend")]
    for rid in ("win-x64", "linux-x64", "osx-arm64"):
        roots.append(os.path.join(ROOT, "dist", f"EchoDesk-v{version}-{rid}", "app", "backend"))
    return roots


def is_forbidden(name: str) -> str:
    if name in FORBIDDEN_BASENAMES:
        return "禁止的文件名"
    lowered = name.lower()
    for suffix in FORBIDDEN_SUFFIXES:
        if lowered.endswith(suffix):
            return f"禁止的后缀 {suffix}"
    for pattern in FORBIDDEN_GLOBS:
        if re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), name, re.IGNORECASE):
            return f"禁止的通配 {pattern}"
    return ""


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    version = app_version()
    if not os.path.isfile(CONFIG_SOURCE):
        print(f"找不到配置模板来源：{CONFIG_SOURCE}")
        return 1
    want_config = sha256(CONFIG_SOURCE)

    problems: list[str] = []
    scanned = 0
    roots = scan_roots(version)
    existing = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        existing += 1
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                scanned += 1
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, ROOT)
                reason = is_forbidden(name)
                if reason:
                    problems.append(f"{rel}：{reason}")
                elif name == CONFIG_BASENAME:
                    got = sha256(full)
                    if got != want_config:
                        problems.append(
                            f"{rel}：config.json 与仓库模板不一致（可能带进了本机设置）")
    print(f"扫描发布树 {existing}/{len(roots)} 棵，文件 {scanned} 个")
    if existing == 0:
        print("没有可扫描的发布树：先跑 tools\\pack_backend.py 与 tools\\pack_crossplatform.py")
        return 1
    if problems:
        print(f"发现 {len(problems)} 个运行期状态文件泄漏进发布树：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("三棵 standalone 树与 PyInstaller 树均无运行期状态文件；config.json 与仓库模板一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
