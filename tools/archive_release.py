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

**v0.2.2 全面双语与三平台友好 追加的两个能力**（为补齐 archive 缺口而定，见
`records/v0.2.2-发布记录.md`）：

* `--extra <文件>`（可重复）：冻结**不是"安装程序 + 便携目录"形态**的交付物。
  典型场景是 `dist/` 下的三平台归档（`*-win-x64.zip` / `*-linux-x64.tar.gz` / `*-osx-arm64.tar.gz`）
  以及它们的 `manifest-<rid>.json` —— 这三种在 `archive/` 里原本没有任何留存，
  而它们是跨平台版本的主交付物。
* `--exclude-glob <模式>`（可重复）：打便携 zip 时排除某些文件。
  实测用途：`publish/EchoDesk-v0.2.0/backend/models/*.gguf` 是**运行期下载**的模型
  （该版本的 `使用说明.txt` 明写"若 models 目录里没有模型，程序会询问是否下载（约 508MB）"），
  不属于交付物；不排除的话归档会凭空多出 500 MB 可重新下载的数据。
* `--preserve-provenance`：清单已存在时**保留其 tag / commit**。
  用于"只往已有归档里补挂额外产物"—— 那时当前 HEAD 早已不是当初构建产物的提交，
  直接覆盖会把"这份产物对应哪段代码"这条唯一凭据改错。
"""
from __future__ import annotations

import argparse
import fnmatch
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


def zip_tree(tree: str, out_path: str, exclude_globs: tuple[str, ...] = ()) -> list[str]:
    """把发布目录打成 zip（便携版）。**不用 `shutil.make_archive`**：它不接受"把内容放在
    某个顶层目录下"的写法，而发布目录的顶层目录名必须保留 —— `UserPaths.DataRoot`
    靠"程序目录名是否为 `app`"来判断便携布局，压缩包的层级不能变。

    ``exclude_globs``：按 **相对发布树的路径**（正斜杠）做 fnmatch 排除。
    实测用途见模块 docstring（v0.2.0 的运行期下载模型）。返回被排除的文件列表，
    以便写进清单 —— "归档里少了什么"必须可查，不能只体现在体积上。
    """
    if os.path.isfile(out_path):
        os.remove(out_path)
    root = os.path.dirname(tree)
    excluded: list[str] = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, names in os.walk(tree):
            dirs.sort()
            for name in sorted(names):
                full = os.path.join(base, name)
                rel = os.path.relpath(full, tree).replace("\\", "/")
                if any(fnmatch.fnmatch(rel, pattern) for pattern in exclude_globs):
                    excluded.append(rel)
                    continue
                zf.write(full, os.path.relpath(full, root))
    return excluded


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
    parser.add_argument("--extra", action="append", default=[],
                        help="额外产物（可重复）：如 dist/ 下的三平台归档与 manifest-<rid>.json")
    parser.add_argument("--installer", default="",
                        help="显式指定安装程序路径（老版本的文件名可能不符合当前命名规范）")
    parser.add_argument("--exclude-glob", action="append", default=[],
                        help="打便携 zip 时排除的文件（可重复，相对发布树路径的 fnmatch 模式）")
    parser.add_argument("--preserve-provenance", action="store_true",
                        help="清单已存在时保留其 tag/commit（只补挂额外产物时用）")
    parser.add_argument("--commit", default="",
                        help="显式指定产物对应的提交（冻结历史产物时用；当前 HEAD 不是它的来源）")
    parser.add_argument("--tag", default="",
                        help="显式指定产物对应的标签（默认按 --commit 反查）")
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
    # `--installer` 可显式指定：老版本的安装程序**文件名不一定符合当前命名规范**
    # （v0.1.0 实际是 `EchoDesk-v0.1.0-setup.exe`，小写 s）。派生名会在归档里把它改名，
    # 而"发布时叫什么名字"是产物身份的一部分（CHANGELOG 与校验清单都按原名引用）。
    installer = (args.installer if args.installer else os.path.join(publish, f"EchoDesk-{version}-Setup.exe"))
    if not os.path.isabs(installer):
        installer = os.path.join(ROOT, installer)
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
        excluded = zip_tree(tree, target, tuple(args.exclude_glob))
        size, files = dir_size(tree)
        entry = {
            "file": os.path.basename(target),
            "kind": "便携版（解压即用；运行 install-complete.bat 生成快捷方式）",
            "bytes": os.path.getsize(target),
            "sha256": sha256_of(target),
            "tree_files": files,
            "tree_bytes": size,
        }
        if excluded:
            # 排除项必须写进清单：归档里"少了什么"要可查，否则以后只能靠体积猜。
            entry["excluded"] = excluded
            entry["excluded_bytes"] = sum(
                os.path.getsize(os.path.join(tree, rel.replace("/", os.sep)))
                for rel in excluded if os.path.isfile(os.path.join(tree, rel.replace("/", os.sep))))
            entry["kind"] += "（已按下述 glob 排除运行期数据）"
            log(f"  排除 {len(excluded)} 个文件（{entry['excluded_bytes'] / 1048576:.2f} MB）："
                f"{', '.join(excluded[:3])}{' …' if len(excluded) > 3 else ''}")
        artifacts.append(entry)
    elif not args.no_portable:
        log(f"没有便携目录可归档：{os.path.relpath(tree, ROOT)}")

    # ---------- 额外产物（--extra）：不是"安装程序 + 便携目录"形态的交付物 ----------
    for src in args.extra:
        full = src if os.path.isabs(src) else os.path.join(ROOT, src)
        if not os.path.isfile(full):
            log(f"额外产物不存在，跳过：{src}")
            continue
        target = os.path.join(archive_dir, os.path.basename(full))
        # **同名冲突一律改名，绝不覆盖**（自己踩到的坑）：`dist/SHA256SUMS.txt` 与
        # 归档里既有的 `SHA256SUMS.txt`（安装程序与合规文档的清单）同名，
        # 直接 copy 会把后者**静默覆盖** —— 归档里少了一份清单、而且没有任何提示。
        # 归档目录的存在意义就是"不再被覆盖"，所以这里宁可多一个带前缀的名字。
        if os.path.isfile(target) and sha256_of(target) != sha256_of(full):
            renamed = f"EchoDesk-{version}-{os.path.basename(full)}"
            target = os.path.join(archive_dir, renamed)
            log(f"同名但内容不同，改名为 {renamed}（不覆盖归档里的同名文件）")
        shutil.copy2(full, target)
        artifacts.append({
            "file": os.path.basename(target),
            "kind": "额外产物（三平台归档 / 其清单等，用 --extra 指定）",
            "source": os.path.relpath(full, ROOT).replace("\\", "/"),
            "bytes": os.path.getsize(target),
            "sha256": sha256_of(target),
        })

    if not artifacts:
        raise SystemExit("没有任何产物被归档 —— 先构建，或检查 --version / --extra 是否正确。")

    # 校验清单只在**确实描述本次归档的产物**时才复制。
    # 原实现无条件复制 `publish/SHA256SUMS.txt` —— 那对 v0.2.1/v0.2.2 是对的（它们就在 publish/ 下构建），
    # 但对从别处收集来的老版本是错的：会把 v0.2.2 的清单塞进 v0.1.0 的归档，
    # 而清单里一个文件名都对不上，比没有清单更误导。
    sums_src = os.path.join(publish, "SHA256SUMS.txt")
    if os.path.isfile(sums_src) and not args.preserve_provenance:
        with io.open(sums_src, encoding="utf-8", errors="replace") as handle:
            sums_text = handle.read()
        archived_names = [item["file"] for item in artifacts]
        if any(name in sums_text for name in archived_names):
            shutil.copy2(sums_src, os.path.join(archive_dir, "SHA256SUMS.txt"))
        else:
            log(f"校验清单与本次产物无关，未复制：{os.path.relpath(sums_src, ROOT)}"
                "（它列的是别的版本的产物）")

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
    old_manifest: dict = {}
    manifest_path = os.path.join(archive_dir, "MANIFEST.json")
    if os.path.isfile(manifest_path):
        try:
            with io.open(manifest_path, encoding="utf-8") as handle:
                old_manifest = json.load(handle)
        except (OSError, ValueError):
            old_manifest = {}

    # **补挂时必须与已有清单合并，而不是覆盖**（自己踩到的坑）：
    # 只传 `--extra` 时本次收集到的 artifacts 里只有那几个额外产物，
    # 直接覆盖写下去会让原先记录的安装程序与便携版**从清单里消失** ——
    # 文件还在归档目录里、清单却不认它们，"清单是产物身份的唯一凭据"就废了。
    if old_manifest and args.preserve_provenance:
        merged = {item["file"]: item for item in old_manifest.get("artifacts", [])}
        added = []
        for item in artifacts:
            if item["file"] not in merged:
                added.append(item["file"])
            merged[item["file"]] = item
        artifacts = [merged[name] for name in sorted(merged)]
        if added:
            log(f"清单合并：原有 {len(merged) - len(added)} 项保留，新增 {len(added)} 项"
                f"（{', '.join(sorted(added))}）")

    if args.preserve_provenance and old_manifest.get("commit"):
        # 只往已有归档里补挂额外产物时用：当前 HEAD 早已不是当初构建产物的提交，
        # 覆盖 tag/commit 会把"这份产物对应哪段代码"这条唯一凭据改错。
        provenance = {
            "tag": old_manifest.get("tag", ""),
            "commit": old_manifest["commit"],
            "commit_subject": old_manifest.get("commit_subject", ""),
            "provenance_preserved_from": old_manifest.get("frozen_at", ""),
            "provenance_preserved_why": (
                "本次只是往该版本归档里**补挂额外产物**（见 artifacts 里 kind 含「额外产物」的项），"
                f"没有重新构建；当前 HEAD（{commit[:8]}）不是产出这些产物的提交，"
                "因此清单的 tag/commit 保留原值。"),
        }
    else:
        provenance = {
            "tag": args.tag if args.tag else find_tag_for(commit),
            "commit": args.commit or commit,
            "commit_subject": (git("log", "-1", "--format=%s", args.commit) if args.commit
                               else git("log", "-1", "--format=%s")),
        }
        if args.commit and args.commit != commit:
            # 冻结历史产物时用得着：产物不是现在构建的，当前 HEAD 不是它的来源。
            provenance["commit_source"] = (
                "由 --commit 显式指定（该产物不是本次构建的，当前 HEAD 不是它的来源）")

    manifest = {
        "version": version,
        "tag": provenance["tag"],
        "commit": provenance["commit"],
        "commit_subject": provenance["commit_subject"],
        "working_tree_dirty": bool(git("status", "--porcelain")),
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "exists_because": ("publish/ 被 .gitignore 覆盖：产物只存在于构建它的机器上，"
                           "重跑构建脚本会覆盖同名文件。本目录是**构建脚本不会写**的位置，"
                           "清单里的 tag/commit 是「这份产物对应哪一段代码」的唯一凭据。"),
        "artifacts": artifacts,
    }
    for key in ("provenance_preserved_from", "provenance_preserved_why", "commit_source"):
        if key in provenance:
            manifest[key] = provenance[key]
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
    log(f"清单：tag={manifest['tag'] or '（无）'} commit={manifest['commit'][:8]}"
        f"{'　（沿用原有来源，本次只补挂额外产物）' if args.preserve_provenance and 'provenance_preserved_from' in manifest else ''}"
        f"{'　（工作区有未提交改动）' if manifest['working_tree_dirty'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
