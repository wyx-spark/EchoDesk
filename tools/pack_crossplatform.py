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

后端形态（v0.2.1 跨平台-2 任务二/任务四）由 `--backend-mode` 选择：
    auto（默认）  有 `publish/backend-<rid>/` 就用它（standalone 形态），否则退回 PyInstaller
    standalone    用 tools/pack_backend.py 的产物（包内解释器，三平台通用）
    pyinstaller   用 backend/EchoBackend.spec 现场打一个（本平台，Windows 正式交付形态）

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

import reproducible as repro
import prune_release as pruner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import platform_support as ps  # noqa: E402

#: 打进发布物的顶层文本文件（与 Windows 安装包保持一致的信息面）
#: 打进发布物**顶层**的文件（与 Windows 发布目录的信息面保持一致）。
#:
#: **实测缺陷修正（v0.2.2 全面双语与三平台友好）**：原清单只有 7 项，漏了 `NOTICE`
#: （英文正本！只带了中文译本 `NOTICE.zh-Hans.md`）、`SECURITY.md` 成对、
#: `CONTRIBUTING.md` 成对、`CODE_OF_CONDUCT.md` 成对 —— 而 `tools/build_release.ps1`
#: 的 Windows 发布目录这几样全都有。对外分发时"只给译本不给正本"是**合规问题**，
#: 不是排版问题（Apache-2.0 要求随分发保留 NOTICE 原文）。
TOP_LEVEL_FILES = (
    "LICENSE",
    "LICENSE.zh-Hans.md",
    "NOTICE",
    "NOTICE.zh-Hans.md",
    "SECURITY.md",
    "SECURITY.zh-Hans.md",
    "README.md",
    "README.zh-Hans.md",
    "CHANGELOG.md",
    "CHANGELOG.zh-Hans.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING.zh-Hans.md",
    "CODE_OF_CONDUCT.md",
    "CODE_OF_CONDUCT.zh-Hans.md",
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


#: 发布目录里**在目标平台上必须可执行**的相对路径（posix 分隔符，相对发布目录根）。
#:
#: 为什么需要一份显式清单（v0.2.1 跨平台-2 实测踩到）：打包机是 Windows，NTFS 上没有
#: Unix 权限位，`tarfile.add()` 只会把 `st_mode` 记成 0o666/0o777 —— 于是**从 Windows 打出的
#: Linux/macOS 归档解压后所有文件都没有可执行位**，用户必须先 `chmod +x` 才能启动。
#: 也就是说"用 tar.gz 就能保留权限"这句话只在**在 Unix 上打包**时成立。
#: 既然本项目的跨平台包是刻意在 Windows 上交叉准备的，就必须自己写清楚谁该可执行。
EXECUTABLE_PATHS = (
    "start.sh",
    "app/EchoDesk",
    "app/backend/run-backend.sh",
    "app/backend/run-backend.bat",
)
#: 目录前缀：这些目录下**所有**文件都可执行（standalone Python 的 bin/ 全是可执行文件）。
EXECUTABLE_PREFIXES = ("app/backend/python/bin/",)


def tar_mode(rel_posix: str, is_dir: bool) -> int:
    """给出该条目在目标平台上的权限位（Windows 打包机上拿不到真实权限，只能按策略给）。"""
    if is_dir:
        return 0o755
    if rel_posix in EXECUTABLE_PATHS:
        return 0o755
    if any(rel_posix.startswith(prefix) for prefix in EXECUTABLE_PREFIXES):
        return 0o755
    return 0o644


def make_archive(tree: str, out_dir: str, fmt: str) -> str:
    """[EN] Delegate archive creation to the shared deterministic writers.

    [ZH] 将归档创建委托给共享的确定性写入器。
    """
    name = os.path.basename(tree)
    if fmt == "tar.gz":
        path = os.path.join(out_dir, name + ".tar.gz")
        # [EN] Unix ownership, mode bits, entry order and the gzip header are all fixed by
        # [EN] the shared writer; the caller only supplies the target-platform mode policy.
        # [ZH] Unix 属主、权限位、条目顺序与 gzip 头均由共享写入器固定；调用方只提供
        # [ZH] 目标平台的权限策略。
        repro.write_tar_gz(tree, path, arcname=name, mode_for=tar_mode)
        return path
    path = os.path.join(out_dir, name + ".zip")
    repro.write_zip(tree, path)
    return path


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_backend_manifest(standalone_dir: str, rid: str) -> None:
    """自证：目标 RID 必须与后端产物清单里记录的平台一致。

    为什么值得单独查（v0.2.1 跨平台-2 实测）：把"打包机平台"当成"目标平台"用，
    正是这一轮反复出问题的地方（pip 用打包机的环境标记去要 Linux 版 pywin32、
    解释器路径按打包机选）。文件存在性检查能挡住大部分情况，但两个平台的产物
    若恰好同名同结构（比如都是 `python/bin/python3`），它挡不住 ——
    清单是唯一记录"这份产物到底是给谁打的"的地方。
    清单不存在时**跳过**而不是失败：`--reuse-backend` 允许复用任意来源的目录。
    """
    manifest_path = os.path.join(os.path.dirname(standalone_dir), f"backend-manifest-{rid}.json")
    if not os.path.isfile(manifest_path):
        return
    try:
        with io.open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"警告：后端清单读不出来，跳过平台自证（{os.path.relpath(manifest_path, ROOT)}：{exc}）")
        return
    recorded = manifest.get("rid")
    if recorded and recorded != rid:
        raise SystemExit(
            f"后端产物的清单说它是给 {recorded} 打的，但本次要打的是 {rid}。\n"
            f"  清单：{os.path.relpath(manifest_path, ROOT)}\n"
            f"  先执行： python tools/pack_backend.py --platform {rid}"
        )
    log(f"后端清单平台自证通过：{os.path.relpath(manifest_path, ROOT)}（rid={recorded}，"
        f"{manifest.get('triple')}）")


def write_top_launcher(tree: str, rid: str) -> str:
    """在发布物**根目录**放一个启动脚本，省得用户自己猜要进 `app/` 找可执行文件。

    为什么需要（v0.2.1 跨平台-2 任务四暴露的可用性问题）：布局刻意是三层
    （根目录 / `app/` / 程序文件），这样 `UserPaths.DataRoot` 才能认出"这是便携布局、
    用户数据放外层"。代价是用户解压后看到一堆文档和一个 `app/` 目录 ——
    没有这个脚本，他得先知道可执行文件藏在 `app/` 里。

    脚本本身只做一件事：把真正的可执行文件**接管本进程**（exec），参数与退出码原样透传。
    """
    if rid.startswith("win"):
        # 文件名**刻意保持中文**（v0.2.2 硬编码清查裁定 §3）：
        #   * 文件名不是界面文本 —— 它出现在资源管理器里，用户双击的就是它；
        #   * 本地化文件名会连带改掉快捷方式目标、文档里的引用（README / 用户手册 / 解压说明）
        #     与 `check_crossplatform_release.py` 的断言，收益是"英文用户看着顺眼"、
        #     代价是**所有既有链接与说明同时失效**；
        #   * 打包脚本本身用 UTF-8 写入，Windows 10+ 的资源管理器与 cmd 都能正常显示。
        # 所以这里保留中文名，并按裁定要求登记理由（不是漏掉）。
        path = os.path.join(tree, "启动 EchoDesk.bat")
        text = (
            "@echo off\r\n"
            "rem EchoDesk 启动脚本。真正的程序在 app\\ 目录里；用户数据（对话记录、设置、\r\n"
            "rem 语言包）按布局约定放在本目录，不跟着程序文件走。\r\n"
            "setlocal\r\n"
            'set "HERE=%~dp0"\r\n'
            'start "" "%HERE%app\\EchoDesk.exe" %*\r\n'
        )
        with io.open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    path = os.path.join(tree, "start.sh")
    text = (
        "#!/bin/sh\n"
        "# EchoDesk 启动脚本。真正的程序在 app/ 目录里；用户数据（对话记录、设置、语言包）\n"
        "# 按布局约定放在本目录，不跟着程序文件走。\n"
        "set -e\n"
        'HERE=$(cd "$(dirname "$0")" && pwd)\n'
        "# exec：让界面进程直接接管本进程，信号（Ctrl-C / 关闭窗口）与退出码都原样透传。\n"
        'exec "$HERE/app/EchoDesk" "$@"\n'
    )
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    try:
        os.chmod(path, 0o755)
    except OSError:
        # 打包机（Windows）上 chmod 无意义；归档格式会尽力保留权限位，
        # 真丢了也只是"解压后要 chmod +x 一次"，不影响内容正确性。
        pass
    return path


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
    parser.add_argument("--skip-backend", action="store_true", help="跳过后端构建（后端必须已有产物或用 --reuse-backend）")
    parser.add_argument("--reuse-backend", default="", help="复用既有后端目录（跳过构建）")
    # v0.2.1 跨平台-2 任务二/任务四：后端可以是两种形态之一，由这里选择。
    #   pyinstaller —— onedir 可执行文件（Windows 正式交付形态，本机可构建）
    #   standalone  —— python-build-standalone + 包内解释器（三平台通用，
    #                  由 tools/pack_backend.py 产出在 publish/backend-<rid>/）
    #   auto        —— 有 publish/backend-<rid>/ 就用 standalone，否则退回 PyInstaller
    parser.add_argument("--backend-mode", default="auto", choices=("auto", "pyinstaller", "standalone"),
                        help="后端形态（默认 auto：优先复用 tools/pack_backend.py 的产物）")
    parser.add_argument("--reuse-frontend", default="", help="复用既有前端发布目录（跳过 dotnet publish）")
    parser.add_argument("--keep-pdb", action="store_true", help="保留 .pdb（默认清理，Windows 上能省下约 100MB）")
    parser.add_argument("--archive", default="auto", choices=("auto", "tar.gz", "zip", "none"))
    parser.add_argument("--allow-dirty", action="store_true",
                        help="[EN] development only: bypass the clean-worktree guard "
                             "/ [ZH] 仅限开发：绕过干净工作区校验")
    args = parser.parse_args()

    repro.require_clean_worktree(args.allow_dirty)
    repro.source_date_epoch()

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

    # 归档格式按**目标 RID** 决定，不按打包机平台决定。
    # 这不只是命名习惯：`tar.gz` 保留 Unix 权限位，而 `zip` 不会 ——
    # 在 Windows 上给 Linux 打出的 zip，解压后 `start.sh` 与 `app/EchoDesk` 都没有可执行位，
    # 用户必须先 `chmod +x` 才能启动。原实现用的是 `ps.kind()`（打包机），
    # 于是三平台全出 zip；这与上面解释器路径那处是**同一个主机/目标混淆**。
    archive_fmt = args.archive
    if archive_fmt == "auto":
        archive_fmt = "zip" if rid.startswith("win") else "tar.gz"

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
             "-p:NuGetAudit=false", "-p:Deterministic=true", "-p:ContinuousIntegrationBuild=true",
             "-p:DeterministicSourcePaths=true", "-o", app_dir], dry=args.plan)

    # ---------- 2. 后端（两种形态，由 --backend-mode 决定） ----------
    #
    # 为什么 Windows 正式安装包仍走 PyInstaller，而跨平台发布物走 standalone：
    # WPF 前端的启动路径只接受一个 exe（没有"解释器 + main.py"模式），而在 Windows 上
    # standalone 相对 PyInstaller 没有收益（体积相当、还要多一层 PYTHONPATH 注入）。
    # 详见 records/v0.2.2-跨平台-2记录.md 第四节。
    backend_out = os.path.join(app_dir, "backend")
    # standalone 后端产物可能落在两处：本脚本的输出目录（`--out`，默认 dist），
    # 或 tools/pack_backend.py 的默认输出目录（publish）。**两处都找**：
    # 两个脚本的默认输出目录本来就不同，只认一处会让"按各自默认值连着跑"直接失败
    # （实测就是这么撞上的），而报错信息里给出的命令（pack_backend）又恰好写到另一处。
    standalone_candidates = [
        os.path.join(args.out, f"backend-{rid}"),
        os.path.join(ROOT, "publish", f"backend-{rid}"),
    ]
    standalone_dir = next((p for p in standalone_candidates if os.path.isdir(p)), standalone_candidates[0])
    mode = args.backend_mode
    if mode == "auto":
        mode = "standalone" if os.path.isdir(standalone_dir) else "pyinstaller"
        log(f"后端形态：auto → {mode}"
            + (f"（发现 {os.path.relpath(standalone_dir, ROOT)}）" if mode == "standalone"
               else "（没有 standalone 产物，退回 PyInstaller）"))

    if args.reuse_backend:
        log(f"复用后端目录：{os.path.relpath(args.reuse_backend, ROOT)}")
        if not args.plan:
            copy_tree(args.reuse_backend, backend_out)
    elif args.skip_backend:
        log("跳过后端构建（--skip-backend）：发布物里将**没有** backend/，"
            "只适合验证前端打包布局")
    elif mode == "standalone":
        # 包内解释器形态：python/ + site-packages/ + app/ + run-backend.{bat,sh}
        # 前端（Avalonia）会直接调用 backend/python/python[.exe] 跑 backend/app/main.py，
        # 启动脚本只是给人手动启动用的（见 BackendService.TryBundledStandalone 注释）。
        if not os.path.isdir(standalone_dir):
            raise SystemExit(
                f"找不到 standalone 后端产物：{os.path.relpath(standalone_dir, ROOT)}\n"
                "  已查找：" + "、".join(os.path.relpath(p, ROOT) for p in standalone_candidates) + "\n"
                "  先执行： python tools/pack_backend.py --platform " + rid
            )
        log(f"使用 standalone 后端：{os.path.relpath(standalone_dir, ROOT)}")
        if not args.plan:
            copy_tree(standalone_dir, backend_out)
            # **解释器路径按目标 RID 决定，不按打包机平台决定**（v0.2.1 跨平台-2 实测踩到）：
            # 原实现写成 `"python/python.exe" if ps.kind() == "windows" else ...`，
            # 于是"在 Windows 上为 Linux 打包"会去找 Linux 产物里根本不存在的 python.exe，
            # 整轮以"产物结构不符"失败。这与 pip 用打包机环境标记是**同一类错误**
            # （主机 / 目标混淆），所以这里写成一条由 rid 推出的规则，并留一条自证：
            # 目标 RID 的平台必须与后端 manifest 里记录的平台一致。
            python_rel = "python/python.exe" if rid.startswith("win") else "python/bin/python3"
            if not os.path.isfile(os.path.join(backend_out, python_rel)):
                raise SystemExit(
                    f"standalone 后端里没有解释器：{python_rel}（产物结构不符）\n"
                    f"  目标 RID：{rid}　后端目录：{os.path.relpath(backend_out, ROOT)}\n"
                    "  常见原因：这个后端产物是给**另一个平台**打的（用 --platform 指定 RID 重跑 pack_backend.py）。"
                )
            if not os.path.isfile(os.path.join(backend_out, "app", "main.py")):
                raise SystemExit("standalone 后端里没有 app/main.py（产物结构不符）")
            check_backend_manifest(standalone_dir, rid)
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
        missing: list[str] = []
        for name in TOP_LEVEL_FILES:
            src = os.path.join(ROOT, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(tree, name))
            else:
                missing.append(name)
        for name in TOP_LEVEL_DIRS:
            src = os.path.join(ROOT, name)
            if os.path.isdir(src):
                copy_tree(src, os.path.join(tree, name))
        # 缺失要**点名报出来**：这几份是合规与说明文件，"少了一份"必须当场可见，
        # 否则打包日志一切正常、用户手里却没有 NOTICE 正本（原清单漏 NOTICE 就是这么漏掉的）。
        if missing:
            log(f"警告：以下顶层文件在仓库里不存在，未进发布物：{'、'.join(missing)}")
        else:
            log(f"顶层文件齐备：{len(TOP_LEVEL_FILES)} 个")

        # 配置示例：**生成**而不是复制（内含双语说明与三平台路径对照，见 tools/gen_config_example.py）。
        # 生成器是 Python，所以三平台构建都能用同一条路径得到同一份结果。
        run([sys.executable, os.path.join("tools", "gen_config_example.py"),
             "--out", os.path.join(tree, "config.example.json")], dry=args.plan)

        # 界面语言包放在 lang/ui，后端语言包在 app/backend/_internal/lang（互不覆盖）
        ui_lang = os.path.join(tree, "lang", "ui")
        os.makedirs(ui_lang, exist_ok=True)
        lang_src = os.path.join(ROOT, "frontend", "Resources", "Lang")
        if os.path.isdir(lang_src):
            for name in os.listdir(lang_src):
                if name.endswith(".json"):
                    shutil.copy2(os.path.join(lang_src, name), os.path.join(ui_lang, name))

    # ---------- 4. 清理调试符号与冗余文件 ----------
    if not args.plan:
        removed = strip_binaries(app_dir, args.keep_pdb)
        if removed:
            log(f"清理调试符号 {len(removed)} 个（例如 {removed[0]}）")
        elif not args.keep_pdb:
            log("没有需要清理的调试符号")
        # [EN] Apply the same platform-aware pruning to reused backend trees, then prune the
        # [EN] whole application directory (tests / caches / foreign binaries / dev tooling).
        # [ZH] 对复用后端树应用同一套平台感知清理，然后清理整个应用目录
        # [ZH]（测试 / 缓存 / 异平台二进制 / 开发工具）。
        summary = pruner.prune_tree(app_dir, rid)
        log(f"[EN] pruned {summary['removed_files']} redundant files "
            f"({summary['removed_bytes'] / 1048576:.1f} MB) / [ZH] 已清理 "
            f"{summary['removed_files']} 个冗余文件（{summary['removed_bytes'] / 1048576:.1f} MB）")

    # ---------- 5. 顶层启动脚本 + 签名说明（**都必须在归档之前写**） ----------
    #
    # 顺序是有讲究的（v0.2.1 跨平台-2 由新门禁 `check_crossplatform_release.py` 抓出来）：
    # 最初 `SIGNING.txt` 是在 `make_archive()` **之后**才写进树的，于是三个平台的归档里
    # 都没有它 —— 而文档明确让 macOS 用户去看 `SIGNING.txt`（Gatekeeper 拦截时怎么办、
    # 签名与公证还差哪一步）。**"文件写进了产物目录"与"文件进了归档"是两件事**。
    if not args.plan:
        launcher = write_top_launcher(tree, rid)
        log(f"顶层启动脚本：{os.path.relpath(launcher, ROOT)}")
        with io.open(os.path.join(tree, "SIGNING.txt"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(signing_notes(rid, f"EchoDesk-{version}-{rid}"
                                       + (".zip" if archive_fmt == "zip" else ".tar.gz")))

    # ---------- 6. 归档 + 校验和 + 清单 ----------
    archive_path = ""
    archive_digest = ""
    if not args.plan and archive_fmt != "none":
        os.makedirs(args.out, exist_ok=True)
        # [EN] Normalise the tree before compression so every generated file carries the
        # [EN] same timestamp regardless of when its individual step completed.
        # [ZH] 压缩前统一目录树时间戳，使每个生成文件无论其步骤何时完成都携带同一时间。
        repro.normalize_tree(tree)
        archive_path = make_archive(tree, args.out, archive_fmt)
        log(f"[EN] archive: {os.path.relpath(archive_path, ROOT)}"
            f" / [ZH] 归档：{os.path.relpath(archive_path, ROOT)}")

    if not args.plan:
        if archive_path:
            # [EN] Hash the archive once: it is needed for SHA256SUMS.txt and for the
            # [EN] manifest, and the archive is hundreds of megabytes.
            # [ZH] 归档只哈希一次：SHA256SUMS.txt 与清单都要用，而归档有数百 MB。
            archive_digest = sha256_of(archive_path)
            # **按文件名替换**而不是纯追加：同一个 rid 重跑打包（例如先打成 pyinstaller 形态、
            # 再改成 standalone 重打）会留下**两条同名、哈希不同**的记录，而门禁按文件名查表，
            # 命中旧的那条就报"摘要不一致" —— 实测踩到（v0.2.2 交付轮：win-x64 重打后
            # SHA256SUMS.txt 里两条 win-x64 记录，校验按第一条判失败）。
            # 保留其它行，只覆盖同名行；文件不存在时等同写入。
            sums_path = os.path.join(args.out, "SHA256SUMS.txt")
            name = os.path.basename(archive_path)
            kept = []
            if os.path.isfile(sums_path):
                with io.open(sums_path, encoding="utf-8") as existing:
                    kept = [line for line in existing.read().splitlines()
                            if line.strip() and not line.rstrip().endswith("  " + name)]
            kept.append(f"{archive_digest}  {name}")
            with io.open(sums_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(kept) + "\n")
            log(f"SHA256：{archive_digest}")

        total, count = dir_size(tree)
        manifest = {
            "version": version,
            "rid": rid,
            "platform": ps.report(),
            "generated_at": repro.utc_iso(),
            "tree": os.path.relpath(tree, ROOT),
            "app_bytes": total,
            "app_files": count,
            "archive": os.path.relpath(archive_path, ROOT) if archive_path else "",
            "archive_sha256": archive_digest,
            "pdb_stripped": not args.keep_pdb,
            "backend_included": os.path.isdir(backend_out),
            # 后端是哪种形态（pyinstaller 的可执行文件 or standalone 的包内解释器）。
            # 记录它是因为两者的**验证方式不同**：前者直接跑 exe，后者要经解释器 + PYTHONPATH。
            "backend_mode": mode,
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
