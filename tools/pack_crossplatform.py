#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台打包（v0.2.1 跨平台 任务四）。

为什么需要这个脚本，而不是把 tools\\build_release.ps1 改一改：

* **PyInstaller 不能交叉编译**。后端要打成当前平台的可执行文件，就必须在**目标平台**
  上运行打包（Windows 上打不出 Linux 可执行文件，反之亦然）。因此脚本本身必须是
  跨平台的（Python，不用 PowerShell 专属能力），在哪个平台跑就产哪个平台的包。
* Windows 的正式交付物仍是 Inno Setup 安装包（`tools\\build_release.ps1`），
  本脚本在 Windows 上产出的是**便携 zip**，用于"不想安装、只想解压运行"的场景与
  与 Linux/macOS 产物做体积对比；两者不冲突。
* macOS 还要多两步外面的世界才算完整：代码签名与公证（notarytool）。这两步需要
  维护者的 Apple 证书与账号，脚本**只打印命令、不擅自执行**，并且在产物里留一份
  `SIGNING.txt` 说明缺哪一步。

用法：
    python tools/pack_crossplatform.py --plan              # 只打印将要执行的步骤
    python tools/pack_crossplatform.py                     # 本平台打包（含 PyInstaller，较慢）
    python tools/pack_crossplatform.py --skip-backend      # 只打前端（后端复用已有产物）
    python tools/pack_crossplatform.py --reuse-backend <dir> --reuse-frontend <dir>
    python tools/pack_crossplatform.py --rid linux-x64 --keep-pdb

产物：dist/EchoDesk-<version>-<rid>/（目录树）
      dist/EchoDesk-<version>-<rid>.tar.gz（Linux/macOS）或 .zip（Windows）
      dist/SHA256SUMS.txt、dist/manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import platform_support as ps  # noqa: E402

#: 打进发布物的顶层文本文件（与 Windows 安装包保持一致的信息面）
TOP_LEVEL_FILES = (
    "LICENSE",
    "LICENSE.zh-Hans.md",
    "NOTICE.zh-Hans.md",
    "README.md",
    "README.zh-Hans.md",
    "CHANGELOG.md",
    "CHANGELOG.zh-Hans.md",
)

#: 打进发布物的目录（文档与语言包）
TOP_LEVEL_DIRS = ("docs",)


def log(message: str) -> None:
    print(f"[pack] {message}", flush=True)


def run(cmd: list[str], cwd: str = ROOT, dry: bool = False) -> None:
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    if dry:
        log(f"（演练）{printable}")
        return
    log(printable)
    completed = subprocess.run(cmd, cwd=cwd)
    if completed.returncode != 0:
        raise SystemExit(f"命令失败（退出码 {completed.returncode}）：{printable}")


def app_version() -> str:
    """从 backend/version.py 读版本号（单一来源，避免脚本里再写一份）。"""
    namespace: dict[str, object] = {}
    with io.open(os.path.join(BACKEND, "version.py"), encoding="utf-8") as handle:
        exec(compile(handle.read(), "version.py", "exec"), namespace)  # noqa: S102
    return str(namespace.get("APP_VERSION", "0.0.0"))


def dir_size(path: str) -> tuple[int, int]:
    total = 0
    count = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
                count += 1
            except OSError:
                pass
    return total, count


def strip_binaries(app_dir: str, keep_pdb: bool) -> list[str]:
    """清掉打包机带出来的调试符号文件。

    Windows 上 `dotnet publish` 会把 SkiaSharp/HarfBuzzSharp 的**原生** .pdb 一起复制
    （实测 80.1MB + 19.9MB，占了发布物一半体积）；`-p:DebugType=none` 只管托管程序集，
    管不到这些原生包。Linux/macOS 没有这个问题，但同样的清理逻辑对三者都适用。
    """
    removed = []
    if keep_pdb:
        return removed
    for base, _dirs, files in os.walk(app_dir):
        for name in files:
            if name.lower().endswith((".pdb", ".vshost.exe")):
                path = os.path.join(base, name)
                try:
                    os.remove(path)
                    removed.append(os.path.relpath(path, app_dir))
                except OSError:
                    pass
    return removed


def copy_tree(src: str, dst: str) -> None:
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def make_archive(tree: str, out_dir: str, fmt: str) -> str:
    name = os.path.basename(tree)
    if fmt == "tar.gz":
        path = os.path.join(out_dir, name + ".tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            tar.add(tree, arcname=name)
        return path
    path = os.path.join(out_dir, name + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(tree):
            for file_name in files:
                full = os.path.join(base, file_name)
                zf.write(full, os.path.relpath(full, os.path.dirname(tree)))
    return path


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def signing_notes(rid: str, archive: str) -> str:
    """在产物里留一份"签名/公证还差什么"的说明（任务四要求把没做完的写清楚）。"""
    lines = [
        f"# 分发签名与公证（{rid}）",
        "",
        f"产物：{archive}",
        "",
    ]
    if ps.kind() == "macos":
        lines += [
            "## 已自动完成",
            "- 无（脚本不做任何需要证书的操作）",
            "",
            "## 仍需维护者在本机执行（需要 Developer ID 证书与 Apple ID）",
            "",
            "```sh",
            '# 1. 用 Developer ID Application 证书签名（替换证书名）',
            'codesign --force --deep --options runtime --timestamp \\',
            '  --sign "Developer ID Application: <你的名字> (TEAMID)" \\',
            '  "EchoDesk-vX.Y.Z-osx-arm64/app/EchoDesk" \\',
            '  "EchoDesk-vX.Y.Z-osx-arm64/app/backend/EchoDeskBackend"',
            "",
            '# 2. 打包成 dmg（可选，便于分发）',
            'hdiutil create -volname EchoDesk -srcfolder "EchoDesk-vX.Y.Z-osx-arm64" \\',
            '  -ov -format UDZO "EchoDesk-vX.Y.Z-osx-arm64.dmg"',
            "",
            '# 3. 公证并装订票据',
            'xcrun notarytool submit "EchoDesk-vX.Y.Z-osx-arm64.dmg" \\',
            '  --apple-id "<AppleID>" --team-id "<TEAMID>" --password "<App 专用密码>" --wait',
            'xcrun stapler staple "EchoDesk-vX.Y.Z-osx-arm64.dmg"',
            "```",
            "",
            "**未完成的原因**：签名需要 Apple 开发者账号（付费）与私钥，",
            "不能写进仓库、也不该由构建机自动持有。在拿到证书前，",
            "macOS 用户需要右键打开（绕过 Gatekeeper 首次拦截）或用 `xattr -dr com.apple.quarantine` 解除隔离。",
        ]
    elif ps.kind() == "linux":
        lines += [
            "## Linux",
            "目前交付 .tar.gz（解压即用）。若要做 AppImage，需要额外安装 appimagetool：",
            "",
            "```sh",
            "wget -O appimagetool https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage",
            "chmod +x appimagetool",
            "./appimagetool --appimage-extract-and-run <AppDir> EchoDesk-vX.Y.Z-linux-x64.AppImage",
            "```",
            "",
            "**未自动完成的原因**：AppImage 需要维护 AppDir 结构与 desktop 文件，",
            "且 appimagetool 不是本仓库依赖；现阶段以 .tar.gz 为准（见 records 的未完成项）。",
        ]
    else:
        lines += [
            "## Windows",
            "Windows 的正式交付物是 Inno Setup 安装包（`tools\\build_release.ps1` 产出）。",
            "本目录是**便携 zip**，用于免安装运行与跨平台体积对比。",
            "",
            "签名（如需）：`signtool sign /fd SHA256 /a EchoDesk.exe`，需要代码签名证书。",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="EchoDesk 跨平台打包")
    parser.add_argument("--rid", default="", help="目标 RID（默认按当前平台判断）")
    parser.add_argument("--out", default=os.path.join(ROOT, "dist"), help="输出目录")
    parser.add_argument("--plan", action="store_true", help="只打印步骤，不执行")
    parser.add_argument("--skip-backend", action="store_true", help="跳过 PyInstaller（后端必须已有产物或用 --reuse-backend）")
    parser.add_argument("--reuse-backend", default="", help="复用既有后端目录（跳过 PyInstaller）")
    parser.add_argument("--reuse-frontend", default="", help="复用既有前端发布目录（跳过 dotnet publish）")
    parser.add_argument("--keep-pdb", action="store_true", help="保留 .pdb（默认清理，Windows 上能省下约 100MB）")
    parser.add_argument("--archive", default="auto", choices=("auto", "tar.gz", "zip", "none"))
    args = parser.parse_args()

    rid = args.rid or ps.rid()
    version = app_version()
    tree = os.path.join(args.out, f"EchoDesk-{version}-{rid}")
    app_dir = os.path.join(tree, "app")

    log(f"平台：{ps.summary_line()}（kind={ps.kind()}，forced={ps.is_forced()}）")
    log(f"目标 RID：{rid}　版本：{version}")
    log(f"产物目录：{os.path.relpath(tree, ROOT)}")
    if ps.is_forced():
        log("注意：平台判断来自 ECHODESK_FORCE_PLATFORM 注入 —— 打出来的包是按当前真实平台构建的，"
            "RID 只是命名，别拿它当「在别的平台打过包」的证据。")

    archive_fmt = args.archive
    if archive_fmt == "auto":
        archive_fmt = "zip" if ps.kind() == "windows" else "tar.gz"

    if not args.plan and os.path.isdir(tree):
        shutil.rmtree(tree)
    if not args.plan:
        os.makedirs(tree, exist_ok=True)
        # app/ 必须提前建好：复用前端/后端的分支是"往 app/ 里拷文件"，
        # 而 dotnet publish 会自动建目录 —— 两条路的目录创建时机不同，统一在这里建。
        os.makedirs(app_dir, exist_ok=True)

    # ---------- 1. 前端（Avalonia，多文件自包含发布） ----------
    if args.reuse_frontend:
        log(f"复用前端发布目录：{os.path.relpath(args.reuse_frontend, ROOT)}")
        if not args.plan:
            for entry in os.listdir(args.reuse_frontend):
                src = os.path.join(args.reuse_frontend, entry)
                dst = os.path.join(app_dir, entry)
                if os.path.isdir(src):
                    copy_tree(src, dst)
                else:
                    shutil.copy2(src, dst)
    else:
        run(["dotnet", "publish", os.path.join("frontend-avalonia", "EchoDesk.Avalonia", "EchoDesk.Avalonia.csproj"),
             "-c", "Release", "-r", rid, "--self-contained", "true",
             "-p:PublishSingleFile=false", "-p:DebugType=none", "-p:DebugSymbols=false",
             "-p:NuGetAudit=false", "-o", app_dir], dry=args.plan)

    # ---------- 2. 后端（PyInstaller onedir；spec 已按平台分支） ----------
    backend_out = os.path.join(app_dir, "backend")
    if args.reuse_backend:
        log(f"复用后端目录：{os.path.relpath(args.reuse_backend, ROOT)}")
        if not args.plan:
            copy_tree(args.reuse_backend, backend_out)
    elif args.skip_backend:
        log("跳过后端构建（--skip-backend）：发布物里将**没有** backend/，"
            "只适合验证前端打包布局")
    else:
        work = os.path.join(args.out, "_pyinstaller-work")
        stage = os.path.join(args.out, "_pyinstaller-dist")
        if not args.plan:
            for path in (work, stage):
                if os.path.isdir(path):
                    shutil.rmtree(path)
        run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
             "--distpath", stage, "--workpath", work,
             os.path.join("backend", "EchoBackend.spec")], dry=args.plan)
        built = os.path.join(stage, "EchoDeskBackend")
        if not args.plan:
            if not os.path.isdir(built):
                raise SystemExit(f"未找到后端产物：{built}")
            copy_tree(built, backend_out)
            for path in (work, stage):
                if os.path.isdir(path):
                    shutil.rmtree(path)

    # ---------- 3. 文档与语言包（与安装包的信息面一致） ----------
    if not args.plan:
        for name in TOP_LEVEL_FILES:
            src = os.path.join(ROOT, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(tree, name))
        for name in TOP_LEVEL_DIRS:
            src = os.path.join(ROOT, name)
            if os.path.isdir(src):
                copy_tree(src, os.path.join(tree, name))
        # 界面语言包放在 lang/ui，后端语言包在 app/backend/_internal/lang（互不覆盖）
        ui_lang = os.path.join(tree, "lang", "ui")
        os.makedirs(ui_lang, exist_ok=True)
        lang_src = os.path.join(ROOT, "frontend", "Resources", "Lang")
        if os.path.isdir(lang_src):
            for name in os.listdir(lang_src):
                if name.endswith(".json"):
                    shutil.copy2(os.path.join(lang_src, name), os.path.join(ui_lang, name))

    # ---------- 4. 清理调试符号 ----------
    if not args.plan:
        removed = strip_binaries(app_dir, args.keep_pdb)
        if removed:
            log(f"清理调试符号 {len(removed)} 个（例如 {removed[0]}）")
        elif not args.keep_pdb:
            log("没有需要清理的调试符号")

    # ---------- 5. 归档 + 校验和 + 清单 ----------
    archive_path = ""
    if not args.plan and archive_fmt != "none":
        os.makedirs(args.out, exist_ok=True)
        archive_path = make_archive(tree, args.out, archive_fmt)
        log(f"归档：{os.path.relpath(archive_path, ROOT)}")

    if not args.plan:
        with io.open(os.path.join(tree, "SIGNING.txt"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(signing_notes(rid, os.path.basename(archive_path) or os.path.basename(tree)))

        if archive_path:
            digest = sha256_of(archive_path)
            with io.open(os.path.join(args.out, "SHA256SUMS.txt"), "a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{digest}  {os.path.basename(archive_path)}\n")
            log(f"SHA256：{digest}")

        total, count = dir_size(tree)
        manifest = {
            "version": version,
            "rid": rid,
            "platform": ps.report(),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tree": os.path.relpath(tree, ROOT),
            "app_bytes": total,
            "app_files": count,
            "archive": os.path.relpath(archive_path, ROOT) if archive_path else "",
            "archive_sha256": sha256_of(archive_path) if archive_path else "",
            "pdb_stripped": not args.keep_pdb,
            "backend_included": os.path.isdir(backend_out),
        }
        manifest_path = os.path.join(args.out, f"manifest-{rid}.json")
        with io.open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
        log(f"清单：{os.path.relpath(manifest_path, ROOT)}")
        log(f"发布物共 {count} 个文件，{total / 1048576:.1f} MB（解压后）")

    log("完成。未自动完成的部分（签名/公证/AppImage）见产物里的 SIGNING.txt。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
