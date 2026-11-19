#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[EN]
Regenerate ``backend/requirements.lock`` from a clean virtual environment.

The Reader edition no longer installs an inference wheel: ``llama-cpp-python`` (and its
``numpy`` dependency) were removed with the native llama.cpp rewrite, so the lock is a
plain dependency closure of ``backend/requirements.txt``. The script reads every
installed ``*.dist-info`` directory, follows ``Requires-Dist`` from the direct
requirements and writes exact pins, using environment markers only where a package is
genuinely platform-specific (``pywin32`` and its ``pywin32-ctypes`` companion).

Usage:
    python tools/gen_requirements_lock.py --site-packages <clean-venv>/Lib/site-packages
    python tools/gen_requirements_lock.py --check

[ZH]
从干净虚拟环境重新生成 ``backend/requirements.lock``。

Reader 版不再安装推理轮子：``llama-cpp-python``（及其 ``numpy`` 依赖）已随原生
llama.cpp 换血一并移除，因此锁文件就是 ``backend/requirements.txt`` 的普通依赖闭包。
脚本读取每个已安装的 ``*.dist-info`` 目录，从直接依赖出发沿 ``Requires-Dist`` 求解，
输出精确版本；只有真正平台专属的包（``pywin32`` 及其伴随包 ``pywin32-ctypes``）才使用
环境标记。

用法：
    python tools/gen_requirements_lock.py --site-packages <干净venv>/Lib/site-packages
    python tools/gen_requirements_lock.py --check
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "backend", "requirements.lock")
REQUIREMENTS = os.path.join(ROOT, "backend", "requirements.txt")

# [EN] Packages that only exist on one platform; everything else is expected to resolve
# [EN] to the same version on every RID.
# [ZH] 只存在于单一平台的包；其余包在所有 RID 上都应解析到同一版本。
#: [EN] Build-only distributions declared in requirements.txt but never needed at run
#: [EN] time. They are pinned separately by backend/requirements-build.lock; keeping them
#: [EN] out of the runtime lock is what lets the two locks be installed together.
#: [ZH] requirements.txt 中声明、但运行期永远不需要的仅构建期发行包。它们由
#: [ZH] backend/requirements-build.lock 单独钉死；排除在运行期锁之外，两份锁才能一起安装
#: [ZH] 而不冲突。
BUILD_ONLY = {"pyinstaller"}


PLATFORM_ONLY = {
    "pywin32": 'sys_platform == "win32"',
    "pywin32-ctypes": 'sys_platform == "win32"',
}

#: [EN] Packages whose available version differs per platform. The clean environment is
#: [EN] built on the maintainer's Windows host, so a single resolved version would pin a
#: [EN] wheel that does not exist for Linux. Keys are (normalised name, sys_platform).
#: [ZH] 各平台可用版本不同的包。干净环境在维护者的 Windows 机器上构建，单一解析版本会
#: [ZH] 钉住 Linux 上并不存在的轮子。键为（归一化包名, sys_platform）。
PLATFORM_VERSION_OVERRIDES = {
    ("pillow", "linux"): "12.2.0",
}

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize(name: str) -> str:
    """[EN] PEP 503 name normalisation. [ZH] PEP 503 名称归一化。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_declared() -> list[str]:
    """[EN] Direct requirement names from requirements.txt. [ZH] 从 requirements.txt 读直接依赖名。"""
    names: list[str] = []
    with io.open(REQUIREMENTS, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            match = _REQ_RE.match(line)
            if match:
                names.append(normalize(match.group(1)))
    return names


def read_distributions(site_packages: str) -> dict[str, dict[str, object]]:
    """[EN] Read name/version/requires from every dist-info. [ZH] 读取每个 dist-info 的名称/版本/依赖。"""
    result: dict[str, dict[str, object]] = {}
    if not os.path.isdir(site_packages):
        raise SystemExit(f"site-packages not found: {site_packages}")
    for entry in sorted(os.listdir(site_packages)):
        if not entry.endswith(".dist-info"):
            continue
        metadata = os.path.join(site_packages, entry, "METADATA")
        if not os.path.isfile(metadata):
            continue
        info: dict[str, object] = {"name": "", "version": "", "requires": []}
        with io.open(metadata, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("Name: ") and not info["name"]:
                    info["name"] = line[6:].strip()
                elif line.startswith("Version: ") and not info["version"]:
                    info["version"] = line[8:].strip()
                elif line.startswith("Requires-Dist: "):
                    info["requires"].append(line[14:].strip())
                elif not line.strip() and info["name"] and info["version"]:
                    # [EN] Metadata header ends at the first blank line.
                    # [ZH] 元数据头在第一个空行结束。
                    break
        if info["name"] and info["version"]:
            result[normalize(str(info["name"]))] = info
    return result


def requirement_name(requirement: str) -> str:
    """[EN] Extract the distribution name from a Requires-Dist line.
    [ZH] 从 Requires-Dist 行中取出分发名。"""
    match = _REQ_RE.match(requirement)
    return normalize(match.group(1)) if match else ""


def marker_allows(requirement: str) -> bool:
    """[EN] Evaluate the subset of markers this project uses, for the current platform.
    [ZH] 按当前平台求值本项目用到的标记子集。"""
    if ";" not in requirement:
        return True
    marker = requirement.split(";", 1)[1].strip()
    # [EN] Optional features of a dependency are not part of this closure.
    # [ZH] 依赖的可选特性不属于本闭包。
    if "extra ==" in marker:
        return False
    if "sys_platform" in marker:
        wanted = re.findall(r"sys_platform\s*==\s*[\"']([^\"']+)[\"']", marker)
        if wanted:
            return sys.platform == wanted[0]
        not_wanted = re.findall(r"sys_platform\s*!=\s*[\"']([^\"']+)[\"']", marker)
        if not_wanted:
            return sys.platform != not_wanted[0]
    # [EN] Version-gated backports (for example ``importlib-metadata`` on older
    # [EN] interpreters) must not enter a 3.13 closure.
    # [ZH] 版本门控的向后移植包（例如旧解释器上的 ``importlib-metadata``）不得进入
    # [ZH] 3.13 的闭包。
    for match in re.finditer(
            r"(python_full_version|python_version)\s*(<=|>=|==|!=|<|>)\s*[\"']([\d.]+)[\"']",
            marker):
        kind, operator, wanted = match.group(1), match.group(2), match.group(3)
        current = ".".join(str(part) for part in sys.version_info[:3])
        left = current if kind == "python_full_version" else f"{sys.version_info[0]}.{sys.version_info[1]}"
        right = wanted if kind == "python_full_version" else ".".join(wanted.split(".")[:2])
        try:
            left_key = tuple(int(part) for part in left.split("."))
            right_key = tuple(int(part) for part in right.split("."))
        except ValueError:
            continue
        checks = {
            "<": left_key < right_key,
            "<=": left_key <= right_key,
            ">": left_key > right_key,
            ">=": left_key >= right_key,
            "==": left_key == right_key,
            "!=": left_key != right_key,
        }
        if not checks[operator]:
            return False
    return True


def closure(site_packages: str) -> list[tuple[str, str]]:
    """[EN] Dependency closure of the direct requirements. [ZH] 直接依赖的依赖闭包。"""
    dists = read_distributions(site_packages)
    seeds = [name for name in read_declared() if name not in BUILD_ONLY]
    pending = list(seeds)
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    while pending:
        name = pending.pop(0)
        if name in seen:
            continue
        seen.add(name)
        info = dists.get(name)
        if info is None:
            raise SystemExit(f"declared dependency is not installed: {name}")
        result.append((str(info["name"]), str(info["version"])))
        for requirement in info["requires"]:  # type: ignore[union-attr]
            if not marker_allows(str(requirement)):
                continue
            dep = requirement_name(str(requirement))
            if dep and dep not in seen:
                pending.append(dep)
    return sorted(result, key=lambda item: item[0].lower())


def render(packages: list[tuple[str, str]]) -> str:
    """[EN] Render the lock file text. [ZH] 渲染锁文件文本。"""
    lines = [
        "# EchoDesk runtime dependency lock (Python 3.13).",
        "# Generated by tools/gen_requirements_lock.py; do not edit by hand.",
        "#",
        "# [EN] Exact transitive pins for the Reader backend. There is no inference wheel:",
        "# [EN] llama.cpp is loaded natively from backend/native/ through the vendored C ABI.",
        "# [EN] No package in this file (and no code path in the backend) needs an outbound",
        "# [EN] network connection at runtime.",
        "# [ZH] Reader 后端全部传递依赖的精确锁定。本文件不含推理轮子：llama.cpp 由",
        "# [ZH] backend/native/ 通过就地内置的 C ABI 原生加载。本文件中的任何包（以及后端的",
        "# [ZH] 任何代码路径）在运行期都不需要对外网络连接。",
        "",
    ]
    for name, version in packages:
        normalized = normalize(name)
        overrides = {platform: pinned
                     for (package, platform), pinned in PLATFORM_VERSION_OVERRIDES.items()
                     if package == normalized}
        if overrides:
            versions = {platform: overrides.get(platform, version)
                        for platform in ("win32", "darwin", "linux")}
            if len(set(versions.values())) == 1:
                lines.append(f"{name}=={version}")
            else:
                for platform in ("win32", "darwin", "linux"):
                    lines.append(
                        f'{name}=={versions[platform]} ; sys_platform == "{platform}"')
            continue
        marker = PLATFORM_ONLY.get(normalized)
        line = f"{name}=={version}"
        if marker:
            line += f" ; {marker}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", help="clean virtual environment site-packages")
    parser.add_argument("--check", action="store_true",
                        help="fail when the lock differs from the generated text")
    args = parser.parse_args()
    if not args.site_packages:
        # [EN] The release gate calls this with --check only. Default to the build
        # [EN] environment when present; its closure is the runtime lock (build-only
        # [EN] packages are excluded by BUILD_ONLY).
        # [ZH] 发布门禁只传 --check。存在构建环境时以其为默认来源：其闭包就是运行期锁
        # [ZH]（仅构建期包已由 BUILD_ONLY 排除）。
        candidates = [
            os.path.join(ROOT, "artifacts", "clean-venv", "Lib", "site-packages"),
            os.path.join(ROOT, "artifacts", "clean-venv", "lib", "python3.13", "site-packages"),
        ]
        args.site_packages = next((path for path in candidates if os.path.isdir(path)), "")
        if not args.site_packages:
            parser.error("--site-packages is required (no artifacts/clean-venv found)")
    text = render(closure(args.site_packages))
    if args.check:
        current = io.open(LOCK, encoding="utf-8").read() if os.path.isfile(LOCK) else ""
        if current != text:
            print("[lock] stale: backend/requirements.lock differs from the clean environment")
            return 1
        print("[lock] up to date")
        return 0
    with io.open(LOCK, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(f"[lock] wrote {LOCK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
