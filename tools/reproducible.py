#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reproducible-build primitives shared by every packaging script.

[EN]
This module is the single implementation of the deterministic-build rules used by the
release chain. It deliberately owns four concerns that used to be re-implemented, with
slightly different semantics, in several scripts:

* the canonical build timestamp (``SOURCE_DATE_EPOCH``), derived from the commit time of
  the checked-out ``HEAD`` unless the caller already exported the variable;
* the clean-worktree guard, so a release can never be produced from uncommitted sources;
* UTC-only timestamp formatting, so the build machine's local time zone can never change
  a shipped byte;
* deterministic ``zip`` and ``tar.gz`` writers with fixed entry timestamps, fixed Unix
  ownership (uid/gid 0), fixed permission bits, and a stable lexicographic entry order.

[ZH]
本模块是发布链中确定性构建规则的唯一实现。以下四类职责此前在多个脚本中各自实现、
语义略有差异，现集中到此处：

* 规范化的构建时间源（``SOURCE_DATE_EPOCH``）：调用方未显式导出该变量时，从当前
  ``HEAD`` 的提交时间派生；
* 干净工作区校验：发布物绝不允许由未提交的源码产生；
* 仅使用 UTC 的时间格式化：构建机的本地时区不得改变任何一个交付字节；
* 确定性的 ``zip`` 与 ``tar.gz`` 写入器：固定条目时间、固定 Unix 属主（uid/gid 为 0）、
  固定权限位，并采用稳定的字典序条目顺序。
"""
from __future__ import annotations

import fnmatch
import gzip
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: [EN] Explicit development-only escape hatch. Release verification never sets it.
#: [ZH] 仅限开发期使用的显式豁免开关；发布验证流程绝不设置该变量。
DIRTY_WORKTREE_ENV = "ECHODESK_ALLOW_DIRTY_WORKTREE"

_EPOCH_CACHE: int | None = None


def log(message: str) -> None:
    print(f"[repro] {message}", flush=True)


def _git(args: list[str]) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if done.returncode != 0:
        return ""
    return done.stdout.strip()


def worktree_dirty() -> bool:
    """[EN] Return whether the tracked worktree contains uncommitted changes. / [ZH] 返回工作区是否存在未提交改动。"""
    return bool(_git(["status", "--porcelain"]))


def require_clean_worktree(allow_dirty: bool = False) -> None:
    """[EN] Refuse to build a release from uncommitted sources. / [ZH] 拒绝由未提交的源码构建发布物。"""
    if allow_dirty or os.environ.get(DIRTY_WORKTREE_ENV) == "1":
        log("[EN] WARNING: clean-worktree guard bypassed for a development build. "
            "/ [ZH] 警告：开发构建已显式绕过干净工作区校验。")
        return
    if worktree_dirty():
        raise SystemExit(
            "[EN] The Git worktree is not clean; commit or stash every change before packaging.\n"
            "[ZH] Git 工作区不干净；请在打包前提交或暂存全部改动。"
        )


def source_date_epoch() -> int:
    """[EN] Return the canonical build timestamp (Unix seconds, UTC). / [ZH] 返回规范构建时间戳（Unix 秒，UTC）。"""
    global _EPOCH_CACHE
    if _EPOCH_CACHE is not None:
        return _EPOCH_CACHE
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if raw:
        try:
            _EPOCH_CACHE = int(raw)
        except ValueError as exc:
            raise SystemExit(
                f"[EN] SOURCE_DATE_EPOCH must be an integer number of seconds: {raw!r}\n"
                f"[ZH] SOURCE_DATE_EPOCH 必须为整数秒：{raw!r}"
            ) from exc
    else:
        commit_time = _git(["log", "-1", "--format=%ct"])
        if not commit_time:
            raise SystemExit(
                "[EN] Cannot derive SOURCE_DATE_EPOCH: this is not a Git checkout with a commit.\n"
                "[ZH] 无法派生 SOURCE_DATE_EPOCH：当前目录不是带提交记录的 Git 检出。"
            )
        _EPOCH_CACHE = int(commit_time)
    if _EPOCH_CACHE < 0:
        raise SystemExit(
            "[EN] SOURCE_DATE_EPOCH must not be negative.\n"
            "[ZH] SOURCE_DATE_EPOCH 不得为负数。"
        )
    return _EPOCH_CACHE


def utc_iso(epoch: int | None = None) -> str:
    """[EN] Format a timestamp as an ISO-8601 UTC value. / [ZH] 将时间戳格式化为 ISO-8601 UTC 字符串。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(source_date_epoch() if epoch is None else epoch))


def normalize_tree(tree: str, epoch: int | None = None) -> int:
    """[EN] Set every entry's access and modification time to the canonical epoch.

    [ZH] 将目录树中每个条目的访问时间与修改时间统一设为规范时间戳。
    """
    stamp = source_date_epoch() if epoch is None else epoch
    changed = 0
    for base, dirs, files in os.walk(tree):
        dirs.sort()
        for name in sorted(files) + [None] + list(dirs):
            target = os.path.join(base, name) if name is not None else base
            try:
                os.utime(target, (stamp, stamp), follow_symlinks=False)
                changed += 1
            except (OSError, NotImplementedError):
                pass
    return changed


def _entry_sort_key(rel: str) -> list[str]:
    return rel.split("/") if rel else []


def _walk_entries(tree: str, exclude_globs: tuple[str, ...] = ()) -> list[tuple[str, str, bool, bool]]:
    """[EN] Return ``(relative, absolute, is_dir, is_symlink)`` in a stable order.

    [ZH] 以稳定顺序返回 ``(相对路径, 绝对路径, 是否为目录, 是否为符号链接)``。
    """
    entries: list[tuple[str, str, bool, bool]] = [("", tree, True, False)]
    for base, dirs, files in os.walk(tree):
        dirs.sort()
        for name in dirs:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, tree).replace(os.sep, "/")
            entries.append((rel, full, True, os.path.islink(full)))
        for name in sorted(files):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, tree).replace(os.sep, "/")
            if any(fnmatch.fnmatch(rel, pattern) for pattern in exclude_globs):
                continue
            entries.append((rel, full, False, os.path.islink(full)))
    entries.sort(key=lambda item: _entry_sort_key(item[0]))
    return entries


def collect_excluded(tree: str, exclude_globs: tuple[str, ...]) -> list[str]:
    """[EN] List files that the supplied glob patterns exclude. / [ZH] 列出被给定通配模式排除的文件。"""
    excluded: list[str] = []
    if not exclude_globs:
        return excluded
    for base, dirs, files in os.walk(tree):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, tree).replace(os.sep, "/")
            if any(fnmatch.fnmatch(rel, pattern) for pattern in exclude_globs):
                excluded.append(rel)
    return excluded


def _zip_info(rel: str, is_dir: bool, mode: int, epoch: int) -> zipfile.ZipInfo:
    stamp = time.gmtime(epoch)[:6]
    info = zipfile.ZipInfo((rel + "/") if is_dir else rel, date_time=stamp)
    info.create_system = 3  # [EN] Unix / [ZH] Unix
    info.create_version = 20
    info.extract_version = 20
    if is_dir:
        info.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
        info.compress_type = zipfile.ZIP_STORED
    else:
        info.external_attr = (mode & 0xFFFF) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
    info.flag_bits = 0
    return info


def write_zip(
    tree: str,
    out_path: str,
    *,
    arcname: str | None = None,
    exclude_globs: tuple[str, ...] = (),
    mode_for=None,
) -> list[str]:
    """[EN] Write a deterministic zip archive of ``tree``.

    [ZH] 将 ``tree`` 写为确定性 zip 归档。

    ``arcname`` is the top-level directory name inside the archive. It defaults to the
    directory's own name so extraction creates one release folder instead of scattering
    files into the caller's current directory.
    """
    if os.path.isfile(out_path):
        os.remove(out_path)
    epoch = source_date_epoch()
    prefix = arcname or os.path.basename(tree)
    excluded: list[str] = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel, full, is_dir, is_link in _walk_entries(tree, exclude_globs):
            if rel == "":
                continue
            arc_rel = f"{prefix}/{rel}"
            if is_dir:
                archive.writestr(_zip_info(arc_rel, True, 0o755, epoch), b"")
                continue
            mode = int(mode_for(rel, False)) if mode_for is not None else 0o644
            if is_link:
                info = _zip_info(arc_rel, False, stat.S_IFLNK | 0o777, epoch)
                info.external_attr = ((stat.S_IFLNK | 0o777) << 16)
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, os.readlink(full).encode("utf-8"))
                continue
            info = _zip_info(arc_rel, False, stat.S_IFREG | mode, epoch)
            with open(full, "rb") as source, archive.open(info, "w") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
    return excluded


def write_tar_gz(
    tree: str,
    out_path: str,
    *,
    arcname: str | None = None,
    exclude_globs: tuple[str, ...] = (),
    mode_for=None,
) -> None:
    """[EN] Write a deterministic ``tar.gz`` archive with a fixed gzip header.

    [ZH] 写出具有固定 gzip 头的确定性 ``tar.gz`` 归档。
    """
    if os.path.isfile(out_path):
        os.remove(out_path)
    epoch = source_date_epoch()
    name = arcname or os.path.basename(tree)
    # [EN] ``filename=""`` removes the source filename from the gzip header; ``mtime`` fixes
    # [EN] the only other variable field. Both are mandatory for byte-identical archives.
    # [ZH] ``filename=""`` 从 gzip 头中移除源文件名；``mtime`` 固定唯一剩余的可变字段。
    # [ZH] 两者都是归档逐字节一致的必需项。
    with open(out_path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for rel, full, is_dir, is_link in _walk_entries(tree, exclude_globs):
                    info = tarfile.TarInfo(name if rel == "" else f"{name}/{rel}")
                    info.mtime = epoch
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    if is_dir:
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        info.size = 0
                        archive.addfile(info)
                        continue
                    if is_link:
                        info.type = tarfile.SYMTYPE
                        info.linkname = os.readlink(full)
                        info.mode = 0o777
                        info.size = 0
                        archive.addfile(info)
                        continue
                    info.type = tarfile.REGTYPE
                    info.mode = int(mode_for(rel, False)) if mode_for is not None else 0o644
                    info.size = os.path.getsize(full)
                    with open(full, "rb") as source:
                        archive.addfile(info, source)


def write_zip_files(out_path: str, files: list[tuple[str, str]]) -> None:
    """[EN] Write a deterministic zip from explicit ``(source, archive_name)`` pairs.

    [ZH] 依据显式的 ``(源文件, 归档名)`` 列表写出确定性 zip。
    """
    if os.path.isfile(out_path):
        os.remove(out_path)
    epoch = source_date_epoch()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, arcname in sorted(files, key=lambda item: item[1]):
            info = _zip_info(arcname, False, stat.S_IFREG | 0o644, epoch)
            with open(source, "rb") as handle, archive.open(info, "w") as target:
                shutil.copyfileobj(handle, target, length=1024 * 1024)


def rewrite_zip_timestamps(path: str, epoch: int | None = None) -> int:
    """[EN] Rewrite an existing zip in place with fixed entry timestamps and attributes.

    [ZH] 原地重写现有 zip，固定每个条目的时间戳与属性。

    PyInstaller builds ``base_library.zip`` with ``zipfile.ZipInfo`` objects that default to
    the current wall-clock time. The module payload is deterministic, but the central
    directory is not. Rewriting only the metadata preserves the payload and entry order.
    """
    stamp = source_date_epoch() if epoch is None else epoch
    temp = path + ".reproducible.tmp"
    if os.path.isfile(temp):
        os.remove(temp)
    count = 0
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temp, "w") as target:
        # [EN] PyInstaller derives the base-module order from a set, so two builds can emit
        # [EN] the same entries in different orders. Sorting by name makes the archive
        # [EN] independent of that set iteration order; zip readers do not depend on it.
        # [ZH] PyInstaller 从集合派生基础模块顺序，两次构建可能以不同顺序写入相同条目。
        # [ZH] 按名称排序可消除集合迭代顺序的影响；zip 读取器不依赖条目顺序。
        for info in sorted(source.infolist(), key=lambda item: item.filename):
            data = source.read(info.filename)
            fresh = _zip_info(info.filename, info.filename.endswith("/"), 0o644, stamp)
            fresh.compress_type = info.compress_type
            target.writestr(fresh, data)
            count += 1
    os.replace(temp, path)
    return count


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Reproducible-build helpers")
    parser.add_argument("--print-epoch", action="store_true")
    parser.add_argument("--print-utc", action="store_true")
    parser.add_argument("--normalize", default="")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    if args.require_clean:
        require_clean_worktree(args.allow_dirty)
    if args.normalize:
        count = normalize_tree(args.normalize)
        log(f"[EN] normalized {count} entries / [ZH] 已统一 {count} 个条目的时间戳")
    if args.print_epoch:
        print(source_date_epoch())
    if args.print_utc:
        print(utc_iso())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
