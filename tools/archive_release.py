#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一个版本的交付物**冻结归档**并写清单（v0.2.2 起）。

为什么需要这个工具，而不是让产物就躺在 `publish/` 里：

* **`publish/` 被 `.gitignore` 覆盖** —— 产物只存在于构建它的那台机器上。一旦有人重跑
  构建脚本，**同名文件就被覆盖**，而"当初发布的那一份到底是哪个哈希"从此无从查证。
  v0.2.1 就是这样出事的：跨平台那一轮把版本号也用成了 v0.2.1，把上一份交付物覆盖掉了。
* 因此冻结的位置必须是**构建脚本不会写的地方**（`publish/archive/<版本>/`），
  并且要带一份 `MANIFEST.json`：版本、标签、构建提交、构建命令、逐项体积与 sha256。
  清单里的 `tag` 与 `commit` 是"这份产物对应哪一段代码"的唯一凭据。

用法：

    python tools/archive_release.py --version 0.2.2
    python tools/archive_release.py --version 0.2.1 --no-portable     # 只要安装程序
    python tools/archive_release.py --version 0.2.2 --supersede EchoDesk-v0.2.1
    python tools/archive_release.py --list                           # 看看已冻结了什么
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISH = os.path.join(ROOT, "publish")
ARCHIVE_ROOT = os.path.join(PUBLISH, "archive")


def log(message: str) -> None:
    print(f"[archive] {message}", flush=True)


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dir_size(path: str) -> tuple[int, int]:
    total = files = 0
    for base, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(base, name))
                files += 1
            except OSError:
                pass
    return total, files


def git(*args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        return done.stdout.strip()
    except OSError:
        return ""


def app_version() -> str:
    path = os.path.join(ROOT, "backend", "version.py")
    with io.open(path, encoding="utf-8") as handle:
        match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', handle.read())
    return match.group(1) if match else ""


def zip_tree(tree: str, out_path: str) -> None:
    """把发布目录打成 zip（便携版）。**不用 `shutil.make_archive`**：它不接受"把内容放在
    某个顶层目录下"的写法，而发布目录的顶层目录名必须保留 —— `UserPaths.DataRoot`
    靠"程序目录名是否为 `app`"来判断便携布局，压缩包的层级不能变。"""
    if os.path.isfile(out_path):
        os.remove(out_path)
    root = os.path.dirname(tree)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, names in os.walk(tree):
            dirs.sort()
            for name in sorted(names):
                full = os.path.join(base, name)
                zf.write(full, os.path.relpath(full, root))


def find_tag_for(commit: str) -> str:
    if not commit:
        return ""
    out = git("tag", "--points-at", commit)
    return out.splitlines()[0] if out else ""


def cmd_list() -> int:
    if not os.path.isdir(ARCHIVE_ROOT):
        log("还没有冻结任何版本。")
        return 0
    for name in sorted(os.listdir(ARCHIVE_ROOT)):
        path = os.path.join(ARCHIVE_ROOT, name)
        if not os.path.isdir(path):
            continue
        if name == "superseded":
            # 留证目录（被覆盖/易名的产物），本身不是一个"已冻结的版本"。
            count = len([n for n in os.listdir(path)])
            log(f"superseded/：{count} 项留证（不是版本归档，见各版本 MANIFEST.json 的 superseded 字段）")
            continue
        manifest = os.path.join(path, "MANIFEST.json")
        if os.path.isfile(manifest):
            with io.open(manifest, encoding="utf-8") as handle:
                data = json.load(handle)
            log(f"{name}：基线提交 {data.get('baseline_commit') or data.get('commit') or '?'}"
                f"　标签 {data.get('tag') or '（无）'}　产物 {len(data.get('artifacts', []))} 项")
        else:
            log(f"{name}：没有 MANIFEST.json")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结归档一个版本的交付物")
    parser.add_argument("--version", default="", help="版本号（可带或不带 v 前缀），默认取 backend/version.py")
    parser.add_argument("--publish", default=PUBLISH, help="产物根目录（默认 publish）")
    parser.add_argument("--no-installer", action="store_true", help="不归档安装程序")
    parser.add_argument("--no-portable", action="store_true", help="不归档便携版 zip")
    parser.add_argument("--supersede", default="", help="把该版本的同名产物移进 superseded/ 留证（如 EchoDesk-v0.2.1）")
    parser.add_argument("--note", default="", help="写进清单的补充说明")
    parser.add_argument("--list", action="store_true", help="只列出已冻结的版本")
    args = parser.parse_args()

    if args.list:
        return cmd_list()

    version = args.version or app_version()
    version = version if version.startswith("v") else f"v{version}"
    if not version or version == "v":
        raise SystemExit("没有版本号：用 --version 指定，或让 backend/version.py 可读。")

    publish = args.publish if os.path.isabs(args.publish) else os.path.join(ROOT, args.publish)
    bare = version.lstrip("v")
    tree = os.path.join(publish, f"EchoDesk-{version}")
    installer = os.path.join(publish, f"EchoDesk-{version}-Setup.exe")
    archive_dir = os.path.join(publish, "archive", version)
    os.makedirs(archive_dir, exist_ok=True)

    artifacts: list[dict] = []

    if not args.no_installer and os.path.isfile(installer):
        target = os.path.join(archive_dir, os.path.basename(installer))
        if not os.path.isfile(target):
            shutil.copy2(installer, target)
        artifacts.append({
            "file": os.path.basename(target),
            "kind": "Inno Setup 安装程序（Windows，不含模型）",
            "bytes": os.path.getsize(target),
            "sha256": sha256_of(target),
        })
    elif not args.no_installer:
        log(f"没有安装程序可归档：{os.path.relpath(installer, ROOT)}")

    if not args.no_portable and os.path.isdir(tree):
        target = os.path.join(archive_dir, f"EchoDesk-{version}-portable.zip")
        log("打包便携版（这一步最慢）…")
        zip_tree(tree, target)
        size, files = dir_size(tree)
        artifacts.append({
            "file": os.path.basename(target),
            "kind": "便携版（解压即用；运行 install-complete.bat 生成快捷方式）",
            "bytes": os.path.getsize(target),
            "sha256": sha256_of(target),
            "tree_files": files,
            "tree_bytes": size,
        })
    elif not args.no_portable:
        log(f"没有便携目录可归档：{os.path.relpath(tree, ROOT)}")

    if not artifacts:
        raise SystemExit("没有任何产物被归档 —— 先构建，或检查 --version 是否正确。")

    sums_src = os.path.join(publish, "SHA256SUMS.txt")
    if os.path.isfile(sums_src):
        shutil.copy2(sums_src, os.path.join(archive_dir, "SHA256SUMS.txt"))

    superseded: dict = {}
    if args.supersede:
        old_installer = os.path.join(publish, f"{args.supersede}-Setup.exe")
        old_tree = os.path.join(publish, f"{args.supersede}")
        os.makedirs(os.path.join(publish, "archive", "superseded"), exist_ok=True)
        for old, suffix in ((old_installer, ".exe"), (old_tree, "-tree")):
            if not os.path.exists(old):
                continue
            name = os.path.basename(old) + ("" if suffix == ".exe" else ".dir")
            dest = os.path.join(publish, "archive", "superseded",
                                f"{name}{'.from-other-build' if suffix == '.exe' else ''}")
            if os.path.isdir(old):
                dest = os.path.join(publish, "archive", "superseded", f"{args.supersede}-tree.from-other-build")
            if os.path.exists(dest):
                log(f"留证项已存在，跳过：{os.path.relpath(dest, ROOT)}")
            else:
                shutil.move(old, dest)
                log(f"留证：{os.path.relpath(dest, ROOT)}")
            if os.path.isfile(dest):
                superseded[os.path.basename(dest)] = {
                    "bytes": os.path.getsize(dest),
                    "sha256": sha256_of(dest),
                }
            else:
                size, files = dir_size(dest)
                superseded[os.path.basename(dest)] = {"bytes": size, "files": files}

    commit = git("rev-parse", "HEAD")
    manifest = {
        "version": version,
        "tag": find_tag_for(commit),
        "commit": commit,
        "commit_subject": git("log", "-1", "--format=%s"),
        "working_tree_dirty": bool(git("status", "--porcelain")),
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "exists_because": ("publish/ 被 .gitignore 覆盖：产物只存在于构建它的机器上，"
                           "重跑构建脚本会覆盖同名文件。本目录是**构建脚本不会写**的位置，"
                           "清单里的 tag/commit 是「这份产物对应哪一段代码」的唯一凭据。"),
        "artifacts": artifacts,
    }
    if superseded:
        manifest["superseded"] = superseded
    if args.note:
        manifest["note"] = args.note

    with io.open(os.path.join(archive_dir, "MANIFEST.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    log("")
    log(f"冻结目录：{os.path.relpath(archive_dir, ROOT)}")
    for item in artifacts:
        log(f"  {item['file']}  {item['bytes'] / 1048576:.2f} MB  {item['sha256']}")
    for name, info in superseded.items():
        log(f"  留证（非本版本）：{name}  {info['bytes'] / 1048576:.2f} MB")
    log(f"清单：tag={manifest['tag'] or '（无）'} commit={commit[:8]}"
        f"{'　（工作区有未提交改动）' if manifest['working_tree_dirty'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
