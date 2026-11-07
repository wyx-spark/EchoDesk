#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify that the PyInstaller build virtual environment matches the committed locks.

[EN]
The Windows release backend is produced by PyInstaller running inside
``artifacts/clean-venv``. That environment is not part of the repository, so its contents
must be checked against ``backend/requirements.lock`` (runtime distributions, Windows
subset) and ``backend/requirements-build.lock`` (build-only distributions) before a release
build starts.

[ZH]
Windows 发布后端由运行于 ``artifacts/clean-venv`` 的 PyInstaller 产出。该环境不属于仓库
内容，因此发布构建开始前必须对照 ``backend/requirements.lock``（运行期发行包的 Windows
子集）与 ``backend/requirements-build.lock``（仅构建期发行包）校验其内容。
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_LOCK = os.path.join(ROOT, "backend", "requirements.lock")
BUILD_LOCK = os.path.join(ROOT, "backend", "requirements-build.lock")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: str, marker: str) -> dict[str, str]:
    result: dict[str, str] = {}
    with io.open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            requirement, _, condition = line.partition(";")
            if condition.strip() and condition.strip() != marker:
                continue
            match = re.fullmatch(r"\s*([A-Za-z0-9_.\-]+)\s*==\s*([^\s;]+)\s*", requirement)
            if match:
                result[canonical(match.group(1))] = match.group(2)
                continue
            match = re.fullmatch(r"\s*([A-Za-z0-9_.\-]+)\s*@\s*\S+\s*", requirement)
            if match:
                # [EN] A direct URL may target a different platform; only declared here.
                # [ZH] 直接 URL 可能指向其它平台，此处仅记录其存在。
                result[canonical(match.group(1))] = "direct-url"
    return result


def installed(venv: str) -> dict[str, str]:
    packages = os.path.join(venv, "Lib", "site-packages")
    result: dict[str, str] = {}
    if not os.path.isdir(packages):
        return result
    for entry in sorted(os.listdir(packages)):
        if not entry.endswith(".dist-info"):
            continue
        metadata = os.path.join(packages, entry, "METADATA")
        name = version = ""
        if os.path.isfile(metadata):
            with io.open(metadata, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Name: ") and not name:
                        name = line[6:].strip()
                    elif line.startswith("Version: ") and not version:
                        version = line[8:].strip()
                    if name and version:
                        break
        if name:
            result[canonical(name)] = version
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check the PyInstaller build virtual environment")
    parser.add_argument("--venv", default=os.path.join(ROOT, "artifacts", "clean-venv"))
    args = parser.parse_args(argv)
    expected: dict[str, str] = {}
    expected.update(parse_lock(RUNTIME_LOCK, 'sys_platform == "win32"'))
    expected.update(parse_lock(BUILD_LOCK, ""))
    actual = installed(args.venv)
    if not actual:
        print(f"[EN] build environment not found: {args.venv}\n[ZH] 未找到构建环境：{args.venv}")
        return 1
    problems: list[str] = []
    for name, version in sorted(expected.items()):
        if name not in actual:
            problems.append(f"missing {name}=={version}")
        elif version != "direct-url" and actual[name] != version:
            problems.append(f"{name}: expected {version}, found {actual[name]}")
    for name in sorted(set(actual) - set(expected)):
        problems.append(f"unexpected {name}=={actual[name]}")
    if problems:
        print("[EN] build environment does not match the locks / [ZH] 构建环境与锁定文件不一致：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print(f"[EN] build environment matches both locks ({len(actual)} distributions) "
          f"/ [ZH] 构建环境与两份锁定文件一致（{len(actual)} 个发行包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
