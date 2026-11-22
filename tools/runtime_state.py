"""
[EN]
One definition of "runtime state that must never ship".

Both the packager (which must sweep such files out of a tree right before archiving) and the release
gate (which must fail if any tree contains one) import this module, so the list cannot drift between
the two places -- the v0.2.7 incident happened because a list existed in only one place and was
incomplete.

[ZH]
"绝不随包分发的运行期状态"的唯一定义。

打包器（归档前必须把这类文件从树里清扫掉）与发布门禁（树里出现即失败）都导入本模块，使清单不会在
两处漂移 —— v0.2.7 那次事故正是因为清单只存在一处且不完整。
"""
from __future__ import annotations

import re

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


def is_forbidden(name: str) -> str:
    """[EN] Return a reason when ``name`` is runtime state, otherwise an empty string.
    [ZH] ``name`` 属运行期状态时返回原因，否则返回空串。"""
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


def sweep(root: str) -> list[str]:
    """[EN] Delete every forbidden file under ``root``; return the removed relative paths.
    [ZH] 删除 ``root`` 下所有禁止文件；返回被删除的相对路径。"""
    import os

    removed: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not is_forbidden(name):
                continue
            full = os.path.join(dirpath, name)
            try:
                os.remove(full)
            except OSError:
                continue
            removed.append(os.path.relpath(full, root))
    return removed
