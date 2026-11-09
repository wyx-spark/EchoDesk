#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台发布物检查（v0.2.1 跨平台-2 任务四/任务五）。

`tools/pack_crossplatform.py` 打出来的东西**能不能被用户正确解开并启动**，靠肉眼看日志是
看不出来的 —— 下面这几项都是"日志一切正常、用户却用不了"的类型，所以固化成检查：

1. **归档格式必须匹配目标平台**：Linux/macOS 要 `tar.gz`、Windows 要 `zip`。
   出 zip 给 Linux 不只是命名问题 —— zip 不保留 Unix 权限位。
2. **`tar.gz` 里必须真的有可执行位**：打包机是 Windows（NTFS 没有 Unix 权限位），
   `tarfile.add()` 会把 0o666 写进去，解压后 `start.sh` / `app/EchoDesk` 都不可执行，
   用户必须先 `chmod +x`。这一条实测踩到过，因此按**显式策略表**写权限位并在这里复核。
3. **归档里每个文件的父目录条目必须排在它前面**：tar 是流式的，缺父目录条目时
   部分解压工具（rsync、非 GNU tar）会报"父目录不存在"。
4. **顶层启动脚本在位**：发布目录是三层的（根 / `app/` / 程序文件），
   没有它用户得自己猜可执行文件藏在哪。
5. **`SHA256SUMS.txt` 与归档的实际摘要一致**：下载校验的可信度取决于它。
6. **清单里的 `backend_mode` 与断言一致**，且归档大小/摘要与清单记录相符。

用法：

    python tools/check_crossplatform_release.py
    python tools/check_crossplatform_release.py --dir dist --rid linux-x64
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def current_version() -> str:
    """[EN] Read the single version source without importing backend modules.

    [ZH] 从唯一版本来源读取版本号，不导入 backend 模块。
    """
    path = os.path.join(ROOT, "backend", "version.py")
    with io.open(path, encoding="utf-8") as handle:
        match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', handle.read())
    return match.group(1).lstrip("vV") if match else ""

#: 目标 RID → (归档扩展名, 需要的可执行路径相对发布目录根)。
TARGETS: dict[str, dict[str, object]] = {
    "win-x64": {"ext": ".zip", "executables": ("启动 EchoDesk.bat",), "launcher": "启动 EchoDesk.bat"},
    "linux-x64": {"ext": ".tar.gz",
                  "executables": ("start.sh", "app/EchoDesk", "app/backend/run-backend.sh"),
                  "launcher": "start.sh"},
    "osx-arm64": {"ext": ".tar.gz",
                  "executables": ("start.sh", "app/EchoDesk", "app/backend/run-backend.sh"),
                  "launcher": "start.sh"},
}

_passed = 0
_failed = 0


def ok(message: str) -> None:
    global _passed
    _passed += 1
    print(f"  [OK]   {message}")


def fail(message: str) -> None:
    global _failed
    _failed += 1
    print(f"  [FAIL] {message}")


def note(message: str) -> None:
    print(f"  [--]   {message}")


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_archive(out_dir: str, rid: str, version: str) -> str:
    # [EN] Scope the lookup to the version being released. The old suffix-only match
    # [EN] picked the first name in sorted order, so a stale older archive left in
    # [EN] `dist/` (v0.2.4 < v0.2.5) was checked instead of the current one.
    # [ZH] 必须按待发布版本限定查找。旧实现只匹配后缀并取排序后的第一个名字，
    # [ZH] 于是 `dist/` 里残留的旧版本归档（v0.2.4 < v0.2.5）会被当成当前产物检查。
    prefix = f"EchoDesk-v{version}-{rid}"
    for name in sorted(os.listdir(out_dir)):
        if not name.startswith(prefix + "."):
            continue
        if name.endswith(".tar.gz") or name.endswith(".zip"):
            return os.path.join(out_dir, name)
    return ""


def check_tar(path: str, spec: dict) -> None:
    prefix = os.path.basename(path)[: -len(".tar.gz")]
    with tarfile.open(path, "r:gz") as tar:
        members = tar.getmembers()
        modern = [m for m in members if m.isfile()]
        if not modern:
            fail(f"{os.path.basename(path)} 里没有任何文件")
            return
        ok(f"{prefix} 归档可读：{len(members)} 个成员（{len(modern)} 个文件）")

        # ① 可执行位
        by_name = {m.name: m for m in members}
        missing = []
        for rel in spec["executables"]:
            full = f"{prefix}/{rel}"
            member = by_name.get(full)
            if member is None:
                missing.append(f"{rel}（归档里没有这个文件）")
            elif not member.mode & 0o111:
                missing.append(f"{rel}（mode={oct(member.mode)}，没有可执行位）")
        if missing:
            fail(f"{prefix} 的可执行位不对：{missing[0]}"
                 "（用户解压后必须先 chmod +x 才能启动）")
        else:
            ok(f"{prefix} 的关键可执行文件都带可执行位"
               f"（{len(spec['executables'])} 个：{'、'.join(spec['executables'])}）")

        # ② 父目录条目顺序
        seen_dirs: set[str] = set()
        orphans = 0
        for member in members:
            if member.isdir():
                seen_dirs.add(member.name)
            elif member.isfile() or member.issym():
                parent = member.name.rsplit("/", 1)[0]
                if parent and parent not in seen_dirs:
                    orphans += 1
        if orphans:
            fail(f"{prefix} 有 {orphans} 个文件的父目录条目排在它后面"
                 "（流式解压时会报「父目录不存在」）")
        else:
            ok(f"{prefix} 的目录条目都排在内容之前（父目录缺失数 0）")

        # ③ 顶层启动脚本
        launcher = f"{prefix}/{spec['launcher']}"
        if launcher in by_name:
            ok(f"{prefix} 顶层启动脚本在位：{spec['launcher']}")
        else:
            fail(f"{prefix} 缺少顶层启动脚本 {spec['launcher']}")

        # ④ 签名说明：文档让 macOS 用户去看它，因此**必须在归档里**
        if f"{prefix}/SIGNING.txt" in by_name:
            ok(f"{prefix} 含 SIGNING.txt（签名/公证还差哪一步写在这里）")
        else:
            fail(f"{prefix} 不含 SIGNING.txt（文档让用户去看它，归档里却没有）")


def check_zip(path: str, spec: dict) -> None:
    prefix = os.path.basename(path)[: -len(".zip")]
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        bad = zf.testzip()
    if bad:
        fail(f"{os.path.basename(path)} 内 {bad} 的校验和不符（归档损坏）")
        return
    ok(f"{prefix} 归档可读：{len(names)} 个条目")
    launcher = f"{prefix}/{spec['launcher']}"
    if launcher in names:
        ok(f"{prefix} 顶层启动脚本在位：{spec['launcher']}")
    else:
        fail(f"{prefix} 缺少顶层启动脚本 {spec['launcher']}")
    # 与 tar 那条路径**查同一组文件**：两个平台的发布物"信息面"应当一致，
    # 不一致本身就是缺陷（曾出现 SIGNING.txt 只写进产物目录、没进任何归档的情况）。
    if f"{prefix}/SIGNING.txt" in names:
        ok(f"{prefix} 含 SIGNING.txt")
    else:
        fail(f"{prefix} 不含 SIGNING.txt（文档让用户去看它，归档里却没有）")


def check_one(out_dir: str, rid: str, version: str) -> None:
    print(f"\n== {rid} ==")
    spec = TARGETS[rid]
    archive = find_archive(out_dir, rid, version)
    if not archive:
        fail(f"{rid} 没有归档（期望 {rid}{spec['ext']}）")
        return
    name = os.path.basename(archive)
    if not name.endswith(str(spec["ext"])):
        fail(f"{rid} 的归档格式不对：{name}（期望以 {spec['ext']} 结尾）")
    else:
        ok(f"{rid} 的归档格式正确：{name}")

    if archive.endswith(".tar.gz"):
        check_tar(archive, spec)
    else:
        check_zip(archive, spec)

    # 校验清单里必须有这个归档，且摘要一致。
    #
    # **为什么是"任一份 `*SHA256SUMS*.txt"而不是固定的 `SHA256SUMS.txt`**（v0.2.2 修正）：
    # 检查的本意是"下载校验的可信度取决于它"—— 也就是**这些归档的摘要有没有被发布出来**，
    # 而不是它叫什么文件名。原来写死 `SHA256SUMS.txt` 在构建目录（`dist/`）里成立，
    # 但把归档冻结进 `publish/archive/<版本>/` 之后，那个位置已经有同名文件
    # （Windows 发布物 + 合规文档的清单），防覆盖规则会把三平台的清单另存为
    # `EchoDesk-<版本>-platform-SHA256SUMS.txt` —— 于是本检查会因为**文件名**而不通过，
    # 而它真正关心的内容其实完全没问题。
    sums_files = sorted(n for n in os.listdir(out_dir)
                        if n.endswith(".txt") and "SHA256SUMS" in n.upper())
    if not sums_files:
        fail(f"{out_dir} 里没有任何 SHA256SUMS 清单（下载校验无从谈起）")
    else:
        digest = sha256_of(archive)
        recorded = ""
        found_in = ""
        for sums_name in sums_files:
            with io.open(os.path.join(out_dir, sums_name), encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == name:
                        recorded, found_in = parts[0], sums_name
                        break
            if recorded:
                break
        if not recorded:
            fail(f"{'/'.join(sums_files)} 里都没有 {name}")
        elif recorded != digest:
            fail(f"{name} 的摘要与 {found_in} 不一致：记录 {recorded} / 实际 {digest}")
        else:
            ok(f"{name} 的 sha256 与 {found_in} 一致（{digest[:16]}…）")

    # 清单：backend_mode 与归档大小/摘要
    manifest_path = os.path.join(out_dir, f"manifest-{rid}.json")
    if not os.path.isfile(manifest_path):
        fail(f"缺少 manifest-{rid}.json")
        return
    with io.open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    mode = manifest.get("backend_mode")
    if mode != "standalone":
        fail(f"{rid} 的清单说 backend_mode={mode}，期望 standalone")
    else:
        ok(f"{rid} 的清单记录 backend_mode=standalone")
    if manifest.get("archive_sha256") and manifest["archive_sha256"] != sha256_of(archive):
        fail(f"{rid} 的清单里归档摘要与实际不符（清单过时了）")
    elif manifest.get("archive_sha256"):
        ok(f"{rid} 的清单归档摘要与实际一致")
    if manifest.get("backend_included") is not True:
        fail(f"{rid} 的清单说发布物里没有后端（backend_included={manifest.get('backend_included')}）")
    else:
        ok(f"{rid} 的清单确认包含后端")


def main() -> int:
    parser = argparse.ArgumentParser(description="跨平台发布物检查")
    parser.add_argument("--dir", default=os.path.join(ROOT, "dist"), help="发布物目录（默认 dist）")
    parser.add_argument("--rid", action="append", default=[], help="只检查指定 RID（可重复）")
    parser.add_argument("--version", default="",
                        help="只检查该版本的产物（默认取 backend/version.py）")
    parser.add_argument("--allow-missing", action="store_true",
                        help="某个 RID 的产物不存在时只提示不失败")
    args = parser.parse_args()

    out_dir = args.dir if os.path.isabs(args.dir) else os.path.join(ROOT, args.dir)
    version = args.version.lstrip("vV") if args.version else current_version()
    if not version:
        raise SystemExit("读不出 APP_VERSION：backend/version.py（可用 --version 显式指定）")
    rids = args.rid or list(TARGETS)
    unknown = [rid for rid in rids if rid not in TARGETS]
    if unknown:
        raise SystemExit(f"未知 RID：{'、'.join(unknown)}（可用：{'、'.join(TARGETS)}）")

    print("== 跨平台发布物检查 ==")
    print(f"产物目录：{os.path.relpath(out_dir, ROOT)}")
    print(f"版本：v{version}")

    for rid in rids:
        if not find_archive(out_dir, rid, version):
            if args.allow_missing:
                print(f"\n== {rid} ==")
                note(f"{rid} 的 v{version} 产物不存在（--allow-missing）")
                continue
            print(f"\n== {rid} ==")
            fail(f"{rid} 的 v{version} 产物不存在：先跑 python tools/pack_crossplatform.py --rid {rid}")
            continue
        check_one(out_dir, rid, version)

    print("\n" + "-" * 42)
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    if _failed:
        print("跨平台发布物检查未通过。")
        return 1
    print("跨平台发布物检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
