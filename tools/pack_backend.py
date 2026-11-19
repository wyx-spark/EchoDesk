#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三平台后端打包（v0.2.1 跨平台-2 任务二）。

## 这个脚本解决什么问题

上一轮（`prompts/EchoDesk-v0.2.1跨平台.md`）用 PyInstaller 打后端，而 PyInstaller
**不能交叉编译**：Windows 打包机上产不出 Linux/macOS 的后端可执行文件，必须依赖
目标平台虚拟机或 CI 矩阵。本轮改成 **python-build-standalone + 自建加载器**：

    python-build-standalone 提供**预编译、可重定位**的 CPython。
    把它 + 依赖（pip 交叉下载的 wheel）+ 后端代码放在一个目录里，
    目标机器解压即用，无需安装 Python、无需联网。

于是"在 Windows 打包机上产出三平台后端"成为可能。

## 为什么不用 uvbox

uvbox 首次运行要联网下载依赖，与 EchoDesk"数据不出本机、断网可用"的核心承诺冲突：
企业内网的机器很可能访问不了 PyPI，或者访问 PyPI 本身就不被允许。

## 目录结构（产物）

    publish/backend-<rid>/
        python/              standalone CPython（自带，目标机无需装 Python）
        site-packages/       pip 交叉下载的依赖（`--target` 装在这里，**不合并进 python/**）
        app/                 后端代码（backend/ 的内容：*.py、lang/、config.json）
        run-backend.bat      Windows 启动脚本（给人手动启动/调试用）
        run-backend.sh       Linux/macOS 启动脚本
        manifest.json        平台、Python 版本、每个依赖的实际版本、体积

**为什么 site-packages 与 python/ 并排，而不是并进 python/lib/site-packages**：
并排之后，"哪些是解释器自带的、哪些是我们装的"一眼可分（升级解释器时不会把旧依赖
留在原地），manifest 也能逐项记录依赖版本；代价只是启动脚本里多一行 `PYTHONPATH`。

## 依赖处理（本轮最容易踩坑的地方，实测记录）

* `pip install --target ... --platform ...` 能交叉下载**其他平台**的 wheel，
  但 pip 评估依赖的**环境标记**时用的是**打包机**的平台，不是目标平台。
  于是为 Linux 准备依赖时，`pywin32; sys_platform == "win32"` 会被算成"需要"，
  接着 pip 去找 pywin32 的 Linux wheel（不存在）而整体失败 —— 实测确认过。
  因此本脚本自己解析 requirements，按**目标平台**过滤标记（见 `_marker_holds`）。
* **不要**给 pip 传 `--abi cp313`：部分预编译包只发布 `py3-none-any` 轮子，
  `py3-none-<platform>` wheel（平台相关、Python 版本无关），限定 abi 会把它们滤掉，
  于是退回 PyPI 的源码包（只有 sdist）→ `--only-binary` 下直接失败。
* 运行期不再安装任何推理轮子：原生 llama.cpp 动态库来自 `backend/native/<rid>/`，
  该索引才有三平台的预编译 wheel（PyPI 上只有源码包，装了就要现场编译）。
* `pyinstaller` **不装**：它是构建工具，standalone 方案下目标机不需要它。

## 用法

    python tools/pack_backend.py --plan                    # 只打印将要执行的事
    python tools/pack_backend.py                           # 产出全部三平台
    python tools/pack_backend.py --platform linux-x64      # 只产一个（可重复传）
    python tools/pack_backend.py --reuse-python            # 复用已下载的 standalone Python
    python tools/pack_backend.py --reuse-deps              # 复用已下载的依赖 wheel
    python tools/pack_backend.py --keep-python-extras      # 不清理解释器里的测试套件等

产物：publish/backend-<rid>/ 与 publish/EchoDesk-backend-<rid>-<版本>.zip|.tar.gz
      publish/backend-SHA256SUMS.txt、publish/backend-manifest-<rid>.json

## 依赖下载与镜像（v0.2.6 发布期补充）

* 轮子优先来自 `artifacts/wheels`（`--find-links`），其次是 `artifacts/pip-cache`
  （`--cache-dir`）：两者都是**由 `backend/requirements.lock` 钉死的输入**，不会改变
  产物字节，只避免重复下载。
* 网络较慢时可导出 `ECHODESK_PIP_INDEX_URL` 指向镜像（例如
  `https://pypi.tuna.tsinghua.edu.cn/simple`）；未设置时沿用 pip 自身配置。
  镜像只改变下载位置，版本仍由锁文件决定。
* Playwright 只安装 Python 轮子，**绝不执行 `playwright install`**；若本机已有浏览器
  目录（`%LOCALAPPDATA%\\ms-playwright` 等），会自动把 `PLAYWRIGHT_BROWSERS_PATH`
  指向它，复用而不是下载。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request

import reproducible as repro
import prune_release as pruner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

#: [EN] Exact, per-platform dependency lock. It is the only input of the runtime install
#: step; ``requirements.txt`` records intent and pip options.
#: [ZH] 分平台的精确依赖锁。它是运行期安装步骤的唯一输入；``requirements.txt`` 只记录
#: [ZH] 依赖意图与 pip 选项。
REQUIREMENTS_LOCK = os.path.join(BACKEND, "requirements.lock")

#: 三平台目标。RID 命名与 tools/pack_crossplatform.py、Avalonia 的 dotnet publish -r 保持一致。
PLATFORMS: dict[str, dict[str, str]] = {
    "win-x64": {
        "triple": "x86_64-pc-windows-msvc",
        "pip_platform": "win_amd64",
        "sys_platform": "win32",
        "platform_system": "Windows",
        "python_rel": "python/python.exe",
        "launcher": "run-backend.bat",
        "archive": "zip",
    },
    "linux-x64": {
        "triple": "x86_64-unknown-linux-gnu",
        "pip_platform": "manylinux2014_x86_64",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "python_rel": "python/bin/python3",
        "launcher": "run-backend.sh",
        "archive": "tar.gz",
    },
    "osx-arm64": {
        "triple": "aarch64-apple-darwin",
        "pip_platform": "macosx_11_0_arm64",
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "python_rel": "python/bin/python3",
        "launcher": "run-backend.sh",
        "archive": "tar.gz",
    },
}

#: [EN] Pinned python-build-standalone release. Querying ``latest`` would let the
#: [EN] interpreter change between two builds of the same commit; the tag is therefore fixed
#: [EN] and treated as a supply-chain input.
#: [ZH] 固定的 python-build-standalone 版本。查询 ``latest`` 会让同一提交的两次构建使用
#: [ZH] 不同解释器；因此固定标签，并将其视为供应链输入。
PBS_RELEASE_TAG = "20261026"
PBS_RELEASE_API = (
    "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/"
    + PBS_RELEASE_TAG
)

#: 缓存目录（artifacts/ 已被 .gitignore 覆盖，不入库、不进发布物）。
CACHE_ROOT = os.path.join(ROOT, "artifacts", "backend-cache")


def _cached_release_from_archives() -> dict:
    """
    [EN]
    Build a release description from the already verified standalone-Python
    archives in the build cache. ``download()`` treats an existing archive as
    complete, so the only thing missing on an offline machine is the asset
    list. Reconstructing it keeps a second reproducible build working when the
    GitHub API is unreachable; the archives themselves still have to exist and
    match the pinned release tag.

    [ZH]
    用构建缓存中已校验的 standalone Python 归档构造 Release 描述。``download()``
    把已存在的归档视为完整文件，因此离线机器上唯一缺少的是资产清单。重建该清单
    可以让第二次可复现构建在 GitHub API 不可达时继续；归档本身仍必须存在并与固定
    的 Release 标签匹配。
    """
    if not os.path.isdir(CACHE_ROOT):
        return {}
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    marker = f"+{PBS_RELEASE_TAG}-"
    assets = []
    for name in sorted(os.listdir(CACHE_ROOT)):
        if not name.startswith(f"cpython-{version}.") or marker not in name:
            continue
        if not name.endswith(".tar.gz"):
            continue
        assets.append({
            "name": name,
            "browser_download_url": (
                "https://github.com/astral-sh/python-build-standalone/releases/download/"
                f"{PBS_RELEASE_TAG}/{name}"
            ),
        })
    if not assets:
        return {}
    return {"tag_name": PBS_RELEASE_TAG, "assets": assets}


#: 已下载/已校验的 wheel 缓存（tools/fetch_wheel.py 的落点）。
#: 用途：钉 URL 的依赖可以复用这里**同一份、已逐成员校验过**的文件，避免重复下载。
WHEEL_CACHE = os.path.join(ROOT, "artifacts", "wheels")

#: pip 的下载缓存（v0.2.5）。默认位置在用户目录，换用户或清 profile 就失效；
#: 固定在 artifacts\ 下可让同一台机器的两次可复现构建共享**已下载的 wheel**。
#: 它只缓存下载结果，不缓存安装结果，因此不会把上一次的目录内容带进产物。
PIP_CACHE = os.path.join(ROOT, "artifacts", "pip-cache")

#: 构建工具，不进运行时依赖。改这里必须同时改注释与记录。
BUILD_ONLY_PACKAGES = ("pyinstaller",)

#: 后端代码里**不该**随包分发的东西（开发期产物与本地环境）。
#: [EN] Native llama.cpp runtime layout: ``backend/native/<rid>/*``. The loader resolves
#: [EN] this directory at run time; the packager copies exactly one RID into ``app/native``
#: [EN] (flat) so the frozen tree keeps the same relative path.
#: [ZH] 原生 llama.cpp 运行期布局：``backend/native/<rid>/*``。加载器在运行期解析该目录；
#: [ZH] 打包器只把目标 RID 复制到 ``app/native``（摊平），使冻结后的目录保持同一相对路径。
NATIVE_DIR = os.path.join(BACKEND, "native")

#: [EN] Libraries without which the native runtime cannot load on that target. Versioned
#: [EN] aliases and optional backends (mtmd / metal / blas) are copied when present.
#: [ZH] 缺少即无法在该目标上加载原生运行期的库。带版本别名与可选后端（mtmd / metal /
#: [ZH] blas）在存在时一并复制。
REQUIRED_NATIVE_LIBS = {
    "win-x64": ("llama.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu.dll"),
    "linux-x64": ("libllama.so", "libggml.so", "libggml-base.so", "libggml-cpu.so"),
    "osx-arm64": ("libllama.dylib", "libggml.dylib", "libggml-base.dylib",
                  "libggml-cpu.dylib"),
}

#: [EN] Dynamic-library extensions per RID; anything else in the native directory is not a
#: [EN] runtime dependency and is not copied.
#: [ZH] 各 RID 的动态库扩展名；原生目录里其余文件不是运行期依赖，不复制。
NATIVE_SUFFIXES = {
    "win-x64": (".dll",),
    "linux-x64": (".so",),
    "osx-arm64": (".dylib",),
}

BACKEND_EXCLUDE_DIRS = {".idea", ".venv", "venv", "__pycache__", "models", ".git", "_internal"}
BACKEND_EXCLUDE_FILES = {"EchoBackend.spec", "requirements.txt"}
BACKEND_EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".spec")
#: 运行期状态文件：它们属于"用户机器上生成的"，不能打进发布物（否则门禁与卸载清单都要打架）。
BACKEND_EXCLUDE_STATE = {"session.json", "backend_crash.log", "skills_dev.log"}

#: standalone Python 里对本项目无用的部分（清理后显著减小体积）。
#: 只删"确认与 EchoDesk 无关"的东西：测试套件、IDLE、tkinter、演示代码、静态库与头文件。
#:
#: **Tcl/Tk 的运行时也要删**（v0.2.1 跨平台-2 实测补充）：`tkinter` 的 Python 包已经删了，
#: 那么 `libtcl9*.so` / `libtk9*.so` / `itcl*` / `lib-dynload/_tkinter*.so` / `tcl9/` / `tk9/`
#: 就**没有任何代码路径能加载**——它们是纯粹的遗留体积（实测约 7 MB/平台）。
#: 判据是"可达性"而不是"看起来没用"：Python 侧删了 tkinter，就没有任何 import 能触到这些 .so。
PYTHON_STRIP_DIRS = (
    "lib/python{ver}/test",
    "lib/python{ver}/idlelib",
    "lib/python{ver}/tkinter",
    "lib/python{ver}/turtledemo",
    "lib/python{ver}/lib2to3",
    "lib/python{ver}/ensurepip",
    "lib/python{ver}/distutils",
    "lib/python{ver}/site-packages",
    "Lib/test",
    "Lib/idlelib",
    "Lib/tkinter",
    "Lib/turtledemo",
    "Lib/lib2to3",
    "Lib/ensurepip",
    "Lib/site-packages",
    "include",
    "share",
    "lib/pkgconfig",
    "lib/python{ver}/config-{ver}-{arch}-linux-gnu",
    # Tcl/Tk 运行时（Python 侧的 tkinter 已删 → 不可达）
    "lib/tcl9",
    "lib/tk9",
    "lib/itcl4.3.8",
    "tcl",
    "libs",
)
#: 按**文件名模式**删的项：Tcl/Tk 的共享库在各平台的命名不一样
#: （Linux `libtcl9.0.so` / macOS `libtcl9.0.dylib` / Windows `tcl86t.dll`、`tk86t.dll`），
#: 用 glob 一次覆盖三平台，比在目录清单里逐个平台列一遍更不容易漏。
PYTHON_STRIP_GLOBS = (
    "**/_test*",
    "**/test_*.py",
    "**/__pycache__",
    "**/_tkinter*",
    "**/libtcl*.so*",
    "**/libtk*.so*",
    "**/libtcl*.dylib",
    "**/libtk*.dylib",
    "**/libitcl*",
    "**/libitk*",
    "**/tcl*.dll",
    "**/tk*.dll",
)


def log(message: str) -> None:
    print(f"[backend] {message}", flush=True)


def run(cmd: list[str], dry: bool = False) -> None:
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    if dry:
        log(f"（演练）{printable}")
        return
    log(printable)
    completed = subprocess.run(cmd, cwd=ROOT)
    if completed.returncode != 0:
        raise SystemExit(f"命令失败（退出码 {completed.returncode}）：{printable}")


def app_version() -> str:
    namespace: dict[str, object] = {}
    with io.open(os.path.join(BACKEND, "version.py"), encoding="utf-8") as handle:
        exec(compile(handle.read(), "version.py", "exec"), namespace)  # noqa: S102
    return str(namespace.get("APP_VERSION", "0.0.0"))


def py_minor() -> str:
    """目标 Python 的 major.minor（与打包机一致，任务书要求"版本需与开发环境一致"）。"""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def dir_size(path: str) -> tuple[int, int]:
    """统计目录的体积与文件数。

    **按 (st_dev, st_ino) 去重**：解压 Linux/macOS 产物时会用**硬链接**来还原 tar 里的
    符号链接（Windows 建不了符号链接，见 `extract_python`），而硬链接的两个名字指向
    同一份数据 —— 不去重就会把同一份数据统计两次（实测 `libpython3.13.so` 与
    `.so.1.0` 被各算了一个 136.6 MB，得出 526 MB 这个**虚高**的数字）。
    这直接决定"standalone 到底比 PyInstaller 大多少"这个结论是否可信。

    符号链接用 `lstat`（不跟随）且只计链接本身 —— 与"磁盘实际占用"的口径一致。
    """
    total = 0
    count = 0
    seen: set[tuple[int, int]] = set()
    for base, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(base, name)
            try:
                stat = os.lstat(full)
            except OSError:
                continue
            count += 1
            if stat.st_nlink > 1:
                key = (stat.st_dev, stat.st_ino)
                if key in seen:
                    continue  # 同一份数据的第二个名字：不再计入
                seen.add(key)
            total += stat.st_size
    return total, count


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 依赖解析：按**目标平台**处理环境标记
# ---------------------------------------------------------------------------
_MARKER_RE = re.compile(r'^\s*(sys_platform|platform_system)\s*(==|!=)\s*["\']([\w\-]+)["\']\s*$')


def _marker_holds(marker: str, spec_target: dict[str, str]) -> bool:
    """判断环境标记在**目标平台**上是否成立。

    只支持仓库里实际用到的形式（`sys_platform == "win32"` / `sys_platform != "darwin"`
    这类单个等值/不等值判断）。遇到不认识的形式**直接报错**，而不是猜 ——
    猜错的后果是"某个平台的依赖静默缺一个"，那种缺陷只会在目标机器上以 ImportError
    的形式暴露，极难排查。
    """
    match = _MARKER_RE.match(marker)
    if not match:
        raise SystemExit(
            f"不认识的依赖环境标记：{marker!r}\n"
            "  tools/pack_backend.py 只实现了 sys_platform / platform_system 的 == 与 != 判断。\n"
            "  请扩展 _marker_holds()，不要让它静默猜（缺依赖只会在目标机器上才报错）。"
        )
    variable, operator, value = match.group(1), match.group(2), match.group(3)
    actual = spec_target["sys_platform"] if variable == "sys_platform" else spec_target["platform_system"]
    return (actual == value) if operator == "==" else (actual != value)


def resolve_requirements(spec_target: dict[str, str]) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """[EN] Resolve the runtime dependency set for the target platform from the lock file.

    [ZH] 从锁文件解析目标平台的运行期依赖集合。

    The lock file is authoritative for versions. ``requirements.txt`` is still read for the
    pip options it carries (index mirrors and timeouts). Every lock
    line is expected to be an exact pin or a direct wheel URL; ranges are rejected because
    they would reintroduce dependency drift.
    """
    pip_args: list[str] = []
    decisions: list[tuple[str, str]] = []
    source = REQUIREMENTS_LOCK if os.path.isfile(REQUIREMENTS_LOCK) else os.path.join(BACKEND, "requirements.txt")
    if not os.path.isfile(source):
        raise SystemExit(
            "[EN] Missing dependency lock file: backend/requirements.lock\n"
            "[ZH] 缺少依赖锁文件：backend/requirements.lock"
        )
    # [EN] pip options always come from requirements.txt, never from the lock file.
    # [ZH] pip 选项始终来自 requirements.txt，绝不来自锁文件。
    options_path = os.path.join(BACKEND, "requirements.txt")
    if os.path.isfile(options_path):
        with io.open(options_path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.split("#", 1)[0].strip()
                if line.startswith("-"):
                    pip_args.extend(shlex.split(line))
    if source == os.path.join(BACKEND, "requirements.txt"):
        decisions.append(("backend/requirements.lock",
                          "[EN] lock file missing; fell back to requirements.txt "
                          "/ [ZH] 锁文件缺失，已回退到 requirements.txt"))

    requirements: list[str] = []
    with io.open(source, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            requirement = line
            marker = ""
            if ";" in line:
                requirement, marker = (part.strip() for part in line.split(";", 1))
            if marker and not _marker_holds(marker, spec_target):
                decisions.append((requirement, f"{marker} → target platform does not match; skipped"))
                continue
            if marker:
                decisions.append((requirement, f"{marker} → target platform matches; kept"))
            name = requirement_name(requirement)
            if name in BUILD_ONLY_PACKAGES:
                decisions.append((requirement, "build-only tool; excluded from the runtime tree"))
                continue
            if "@" not in requirement and "==" not in requirement:
                raise SystemExit(
                    "[EN] The dependency lock contains an unpinned requirement: "
                    f"{requirement!r}\n[ZH] 依赖锁包含未钉死版本的依赖：{requirement!r}"
                )
            new_line, why = local_wheel_override(requirement)
            if not why:
                new_line, why = pinned_local_override(requirement, spec_target["pip_platform"])
            if why:
                decisions.append((requirement, why))
            requirements.append(new_line)
    if source == REQUIREMENTS_LOCK:
        decisions.insert(0, ("backend/requirements.lock",
                             "[EN] exact pins for the target platform / [ZH] 目标平台的精确版本锁定"))
    return pip_args, requirements, decisions


def pinned_local_override(line: str, pip_platform: str) -> tuple[str, str]:
    """`name==version` 且缓存里有**同平台**的 wheel 时，改写成 file:// 直链。

    只认精确版本（`==`）——范围约束（`>=`）不该被本地缓存"钉死"，那会让依赖解析变得不可预期。
    平台标签必须相符，否则 pip 会因为"不兼容当前 --platform"直接失败。
    """
    match = re.fullmatch(r"\s*([A-Za-z0-9_.\-]+)\s*==\s*([0-9][^\s;]*)\s*", line)
    if not match:
        return line, ""
    name, version = match.group(1), match.group(2)
    normalized = name.replace("-", "_").replace(".", "_").lower()
    prefix = f"{normalized}-{version}-"
    platform_hint = {
        "win_amd64": "win_amd64",
        "manylinux2014_x86_64": "manylinux",
        "macosx_11_0_arm64": "macosx_11_0_arm64",
    }.get(pip_platform, pip_platform)
    matches = [
        entry for entry in sorted(os.listdir(WHEEL_CACHE))
        if entry.lower().startswith(prefix.lower()) and entry.endswith(".whl")
        and platform_hint.lower() in entry.lower()
    ] if os.path.isdir(WHEEL_CACHE) else []
    if len(matches) != 1:
        return line, ""
    cached = os.path.join(WHEEL_CACHE, matches[0])
    return (f"{name} @ {pathlib.Path(cached).resolve().as_uri()}",
            f"复用本地已校验的 {matches[0]}（{os.path.getsize(cached) / 1048576:.1f} MB）")


def requirement_name(line: str) -> str:
    """取出依赖行的包名（用于与 BUILD_ONLY_PACKAGES 比较），小写。

    要同时处理 `包名>=x` 与 `包名 @ https://…whl` 两种形式：后者是直接 URL 钉死的
    写法，不先按 `@` 切开就会把整条 URL 当包名，构建工具过滤因此静默失效。
    """
    name = re.split(r"[@<>=!\[;]", line, maxsplit=1)[0].strip()
    return name.lower().replace("_", "-")


def local_wheel_override(line: str) -> tuple[str, str]:
    """把 `包名 @ https://…/foo.whl` 换成**本地已缓存的同名 wheel**，返回 (新行, 说明)。

    动机：依赖锁里的直接 URL（以及部分镜像上的大轮子）在本机链路上容易反复
    `ConnectionResetError` / `ConnectTimeout`。同一个文件只要已经用
    `tools/fetch_wheel.py` 下好并**逐成员解压校验过**放在 `artifacts/wheels/` 里，
    再让 pip 从网上重下一遍没有意义，只是多一次"下到一半断掉"的风险。

    只在**同名文件确实存在**时才替换（wheel 文件名唯一标识版本与平台，不做模糊匹配）；
    没有缓存就原样保留 URL，让 pip 自己去下。
    """
    if " @ " not in line:
        return line, ""
    package, reference = line.split(" @ ", 1)
    reference = reference.strip()
    if not reference.lower().startswith(("http://", "https://")):
        return line, ""
    # [EN] The JamePeng Windows wheel URL escapes the local version separator
    # [EN] (``0.4.7%2Bcu132``); percent-decoding is required before comparing the
    # [EN] name with the cached file, whose name contains a literal ``+``.
    # [ZH] JamePeng 的 Windows 轮子 URL 转义了本地版本分隔符（``0.4.7%2Bcu132``）；
    # [ZH] 在与缓存文件名比较之前必须做百分号解码，缓存文件名里是字面量 ``+``。
    name = urllib.parse.unquote(os.path.basename(reference.split("?", 1)[0].rstrip("/")))
    if not name.endswith(".whl"):
        return line, ""
    cached = os.path.join(WHEEL_CACHE, name)
    if not os.path.isfile(cached):
        return line, ""
    return f"{package.strip()} @ {pathlib.Path(cached).resolve().as_uri()}", \
        f"复用本地已校验的 {name}（{os.path.getsize(cached) / 1048576:.1f} MB）"


def write_resolved_requirements(requirements: list[str], pip_args: list[str], dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with io.open(dest, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# 由 tools/pack_backend.py 生成：已按**目标平台**处理环境标记，\n")
        handle.write("# 已剔除构建工具（pyinstaller）。不要手工编辑。\n")
        # **不把 pip 选项写成本文件的行**：选项（如 --extra-index-url）已经由打包脚本
        # 直接放进 pip 的命令行参数里。而 pip_args 是**已拆分**的参数列表，
        # 逐行写出来会把"选项"和"它的值"拆成两行，pip 于是把选项名当成依赖名 ——
        # 实测报 `ERROR: Invalid requirement: --extra-index-url` 并以退出码 1 结束。
        # 这里只把选项**记成注释**，便于排查"这一轮到底用了哪个索引"。
        if pip_args:
            handle.write("# pip 选项（由命令行传递，故意不写成本文件的行）：" + " ".join(pip_args) + "\n")
        for line in requirements:
            handle.write(line + "\n")


# ---------------------------------------------------------------------------
# python-build-standalone
# ---------------------------------------------------------------------------
PBS_RELEASE_CACHE = os.path.join(CACHE_ROOT, f"pbs-release-{PBS_RELEASE_TAG}.json")


def fetch_release_info() -> dict:
    """
    [EN]
    Fetch the pinned Release description, with an offline fallback. The API can
    be unreachable on an air-gapped or flaky build machine; the archives are
    already cached and verified, so a cached JSON description or a synthetic
    asset list is enough to complete the second reproducible build.

    [ZH]
    获取固定的 Release 描述，并提供离线回退。构建机可能断网或网络抖动；归档已经
    缓存并校验，因此缓存的 JSON 描述或由归档重建的资产清单足以完成第二次可复现构建。
    """
    request = urllib.request.Request(PBS_RELEASE_API, headers={"User-Agent": "EchoDesk-pack-backend"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            release = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - offline build machines are expected
        for candidate in (PBS_RELEASE_CACHE,):
            if os.path.isfile(candidate):
                try:
                    with open(candidate, "r", encoding="utf-8") as handle:
                        release = json.load(handle)
                    log(f"Release API 不可达（{exc}），已使用缓存清单 {os.path.relpath(candidate, ROOT)}")
                    return release
                except Exception:  # noqa: BLE001
                    pass
        release = _cached_release_from_archives()
        if release:
            log(f"Release API 不可达（{exc}），已由本地归档重建资产清单")
            return release
        raise
    if not os.path.isdir(CACHE_ROOT):
        os.makedirs(CACHE_ROOT, exist_ok=True)
    try:
        with open(PBS_RELEASE_CACHE, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(release, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError:
        pass
    return release



def pick_asset(release: dict, triple: str, version: str, keep_symbols: bool = False) -> dict:
    """在 Release 里挑出「某平台 + 某 Python 版本」的那一个归档。

    **默认要 `install_only_stripped`**（v0.2.1 跨平台-2 的实测结论）：python-build-standalone
    同时发布 `install_only` 与 `install_only_stripped` 两个变体，后者剥掉了原生调试符号。
    实测体积差得非常多（同一 Release 20261003）：

        Linux x86-64    71.8 MB → 33.5 MB
        Windows x86-64  45.2 MB → 21.0 MB
        macOS arm64     24.2 MB → 24.1 MB（macOS 的构建本来就几乎没有可剥的符号）

    为什么可以直接剥：调试符号只在**用 gdb/lldb 调试原生崩溃**时才需要；
    Python 自己的 traceback 来自 `.py` 源码与符号表里的 Python 函数名，与 DWARF 无关。
    也就是说这是**纯粹的减重**，不影响任何运行期行为。
    需要原生调试时用 `--keep-symbols` 换回未剥离的变体。
    """
    suffix = f"-{triple}-install_only{'' if keep_symbols else '_stripped'}.tar.gz"
    candidates = [
        asset for asset in release.get("assets", [])
        if asset["name"].startswith(f"cpython-{version}.")
        and asset["name"].endswith(suffix)
        # 排除 freethreaded / debug 变体：本项目用标准 GIL 构建。
        and "freethreaded" not in asset["name"]
    ]
    if not candidates and not keep_symbols:
        # 老 Release 可能没有 stripped 变体：退回未剥离，而不是直接失败。
        log("  该 Release 没有 install_only_stripped 变体，退回 install_only（未剥离）")
        return pick_asset(release, triple, version, keep_symbols=True)
    if not candidates:
        raise SystemExit(
            f"在 release {release.get('tag_name')} 里找不到 {triple} 的 CPython {version} install_only 资产。\n"
            "  可能原因：该 release 尚未提供这个平台，或 Python 小版本已升级（试试更新本机 Python）。"
        )
    # 同一平台同一小版本理论上只有一个；多于一个时取名字最短的（最"普通"的那个）。
    candidates.sort(key=lambda a: len(a["name"]))
    return candidates[0]


def download(url: str, dest: str, dry: bool = False) -> None:
    """下载一个文件（优先 curl，带断点续传）。

    **为什么不用 urllib**（实测记录）：本机网络下 urllib 拿不到 GitHub Release 的资源 ——
    先正常收到 302 指向 `release-assets.githubusercontent.com`，然后**一个字节都收不到**
    （实测挂十几分钟、`.part` 仍是 0 字节）。同一个 URL 用 curl 能正常下载，只是慢：
    约 40–70 KB/s，45 MB 的解释器要十几分钟。

    慢链路还必须能**续传**：一次跑不完是常态，不能用"从头再来"惩罚用户。
    `-C -` 让 curl 接着已有片段下，`--retry-all-errors` 让瞬时断流不致命。
    下载完成后才改名为正式文件名，所以"存在即完整"（不会把半个包当成品用）。
    """
    if dry:
        log(f"（演练）下载 {url}")
        return
    if os.path.isfile(dest):
        log(f"已缓存：{os.path.relpath(dest, ROOT)}（{os.path.getsize(dest) / 1048576:.1f} MB）")
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    curl = shutil.which("curl")
    if curl:
        log(f"下载（curl，可续传）：{url}")
        if os.path.isfile(part):
            log(f"  发现已有片段 {os.path.getsize(part) / 1048576:.1f} MB，将续传")
        completed = subprocess.run(
            [curl, "-L", "--fail", "--retry", "8", "--retry-delay", "3", "--retry-all-errors",
             "--progress-bar", "-C", "-", "-o", part, url],
            cwd=ROOT,
        )
        if completed.returncode != 0:
            raise SystemExit(
                f"下载失败（curl 退出码 {completed.returncode}）：{url}\n"
                f"  已下载的部分保留在 {os.path.relpath(part, ROOT)}，**重跑本脚本会自动续传**。\n"
                "  网络很慢（实测 40–70 KB/s）属正常现象，不要删掉那个片段。"
            )
    else:
        # 没有 curl 时的兜底（POSIX 上几乎总有，Windows 10+ 自带）。
        log(f"下载（urllib 兜底，不支持续传）：{url}")
        request = urllib.request.Request(url, headers={"User-Agent": "EchoDesk-pack-backend"})
        with urllib.request.urlopen(request, timeout=300) as response, open(part, "wb") as handle:
            shutil.copyfileobj(response, handle, length=256 * 1024)
    os.replace(part, dest)
    log(f"完成：{os.path.relpath(dest, ROOT)}（{os.path.getsize(dest) / 1048576:.1f} MB）")


def extract_python(archive: str, dest_dir: str) -> None:
    """解压 standalone Python 到 dest_dir（归档内部顶层就是 `python/`）。

    **为什么要自己处理符号链接，而不是一句 `tar.extractall()`**（实测踩到）：

    Windows 上 `extractall()` **无法创建符号链接**（需要特权或开发者模式），于是它把
    链接目标**复制成实体文件**。实测后果：`lib/libpython3.13.so` 与
    `lib/libpython3.13.so.1.0` **各 136.6 MB**（后者本应是前者的符号链接），
    Linux 产物因此从应有的约 270 MB 膨胀到 **537.9 MB**。

    产物功能上仍然可用（内容一模一样），但体积翻倍，而且会**掩盖真实体积** ——
    让"standalone 到底比 PyInstaller 大多少"这个判断失真，用户解压后也会看到虚胖目录。

    做法：符号链接优先建**符号链接**（Linux/macOS 上正常），Windows 上退回**硬链接**
    （NTFS 支持且无需特权，两份名字共享同一份数据，体积只算一次），两者都失败才复制。
    """
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)

    stats = {"symlink": 0, "hardlink": 0, "copied": 0}
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()

        # 第一遍：目录与普通文件（链接的目标必须先存在）
        for member in members:
            if member.issym() or member.islnk():
                continue
            try:
                tar.extract(member, dest_dir, filter="fully_trusted")  # Python 3.12+
            except TypeError:
                tar.extract(member, dest_dir)

        # 第二遍：链接成员
        for member in members:
            if not (member.issym() or member.islnk()):
                continue
            target = os.path.join(dest_dir, member.name.replace("/", os.sep))
            os.makedirs(os.path.dirname(target) or dest_dir, exist_ok=True)
            if os.path.lexists(target):
                os.remove(target)

            if member.issym():
                # 符号链接的 linkname 是相对本文件所在目录的路径（也可能是绝对路径）
                link_source = (member.linkname if os.path.isabs(member.linkname)
                               else os.path.normpath(os.path.join(os.path.dirname(target), member.linkname)))
            else:
                # 硬链接成员的 linkname 是归档内的路径
                link_source = os.path.join(dest_dir, member.linkname.replace("/", os.sep))

            try:
                os.symlink(member.linkname, target)
                stats["symlink"] += 1
                continue
            except (OSError, NotImplementedError, AttributeError):
                pass
            try:
                os.link(link_source, target)
                stats["hardlink"] += 1
                continue
            except OSError:
                pass
            try:
                shutil.copy2(link_source, target)
                stats["copied"] += 1
            except OSError as exc:
                # 单个链接处理不了**不该让整个构建失败**。实测：standalone Python 里有
                # `share/terminfo/N/NCR260VT300WPP` 这样指向 `n/ncr260vt300wpp` 的条目，
                # 在 Windows 的大小写规则下解析不到目标 —— 而 terminfo 整个目录对
                # EchoDesk 毫无用处，随后还会被 strip_python() 删掉。
                # 记数并继续：失败数在末尾汇总，不静默吞掉，但也不因此中断构建。
                stats["failed"] = stats.get("failed", 0) + 1
                if stats["failed"] <= 3:
                    log(f"  跳过无法处理的链接 {member.name}：{exc}")
                continue

    log(f"链接成员处理：符号链接 {stats['symlink']}、硬链接 {stats['hardlink']}、"
        f"复制 {stats['copied']}、跳过 {stats.get('failed', 0)}")
    if stats["copied"]:
        log("  注意：有链接被复制成实体文件（体积会偏大）；本机不支持符号链接也不支持硬链接时才会发生。")

    if not os.path.isdir(os.path.join(dest_dir, "python")):
        raise SystemExit(f"解压后没找到 python/ 目录：{dest_dir}（归档结构与预期不符）")


def strip_python(python_dir: str, version: str) -> list[str]:
    """清掉 standalone Python 里与 EchoDesk 无关的部分（测试套件、IDLE、tkinter…）。"""
    import glob as _glob

    removed: list[str] = []
    arch = "x86_64" if sys.maxsize > 2 ** 32 else "i686"
    for pattern in PYTHON_STRIP_DIRS:
        rel = pattern.format(ver=version, arch=arch)
        target = os.path.join(python_dir, rel.replace("/", os.sep))
        if os.path.isdir(target):
            size, count = dir_size(target)
            shutil.rmtree(target, ignore_errors=True)
            removed.append(f"{rel}（{count} 文件，{size / 1048576:.1f} MB）")
    for pattern in PYTHON_STRIP_GLOBS:
        for path in _glob.glob(os.path.join(python_dir, pattern.replace("/", os.sep)), recursive=True):
            if os.path.isdir(path):
                size, count = dir_size(path)
                shutil.rmtree(path, ignore_errors=True)
                removed.append(f"{os.path.relpath(path, python_dir)}（{count} 文件，{size / 1048576:.1f} MB）")
    return removed


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def native_source_dir(rid: str) -> str:
    """[EN] Source directory of the native libraries for one target.
    [ZH] 某个目标平台的原生库源目录。"""
    return os.path.join(NATIVE_DIR, rid)


def native_libraries(rid: str) -> list[tuple[str, str, int]]:
    """[EN] ``(name, path, bytes)`` of every dynamic library for the target.
    [ZH] 目标平台全部动态库的 ``(名称, 路径, 字节数)``。"""
    directory = native_source_dir(rid)
    suffixes = NATIVE_SUFFIXES.get(rid, ())
    found: list[tuple[str, str, int]] = []
    if not os.path.isdir(directory):
        return found
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or not name.lower().endswith(suffixes):
            continue
        found.append((name, path, os.path.getsize(path)))
    return found


def verify_native_libraries(rid: str) -> list[tuple[str, str, int]]:
    """[EN] Validate that the target's native runtime is complete before packaging.
    [ZH] 打包前校验目标平台的原生运行期是否完整。"""
    directory = native_source_dir(rid)
    libraries = native_libraries(rid)
    names = {name for name, _path, _size in libraries}
    required = REQUIRED_NATIVE_LIBS.get(rid)
    if required is None:
        raise SystemExit(
            f"[EN] No native runtime mapping for {rid}; update REQUIRED_NATIVE_LIBS.\n"
            f"[ZH] 没有 {rid} 的原生运行期映射；请更新 REQUIRED_NATIVE_LIBS。")
    if not os.path.isdir(directory):
        raise SystemExit(
            f"[EN] Native runtime directory missing for {rid}: {directory}\n"
            f"[EN] Build it from a llama.cpp version that matches backend/llama_capi.py.\n"
            f"[ZH] {rid} 的原生运行期目录缺失：{directory}\n"
            f"[ZH] 请用与 backend/llama_capi.py 匹配的 llama.cpp 版本构建该目录。")
    missing = [name for name in required if name not in names]
    if missing:
        raise SystemExit(
            f"[EN] Native runtime for {rid} is incomplete; missing: {', '.join(missing)}\n"
            f"[ZH] {rid} 的原生运行期不完整；缺少：{', '.join(missing)}")
    return libraries


def copy_native_libraries(rid: str, dest_dir: str) -> dict[str, object]:
    """[EN] Copy one target's dynamic libraries into ``app/native`` (flat).
    [ZH] 把目标平台的动态库复制到 ``app/native``（摊平）。"""
    libraries = verify_native_libraries(rid)
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    total = 0
    for name, path, size in libraries:
        shutil.copy2(path, os.path.join(dest_dir, name))
        total += size
    return {"rid": rid, "count": len(libraries), "bytes": total,
            "libraries": [name for name, _path, _size in libraries]}


def copy_backend_code(dest_dir: str, rid: str) -> int:
    """把 backend/ 的内容复制成 app/（排除开发期产物与运行期状态文件）。"""
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    copied = 0
    for name in sorted(os.listdir(BACKEND)):
        src = os.path.join(BACKEND, name)
        if os.path.isdir(src):
            if name in BACKEND_EXCLUDE_DIRS:
                continue
            if name == "native":
                # [EN] Only the target RID's dynamic libraries belong in this package.
                # [ZH] 本包只携带目标 RID 的动态库。
                info = copy_native_libraries(rid, os.path.join(dest_dir, name))
                log(f"[EN] native runtime: {info['count']} libraries, "
                    f"{info['bytes'] / 1048576:.1f} MB → app/native/"
                    f" / [ZH] 原生运行期：{info['count']} 个动态库，"
                    f"{info['bytes'] / 1048576:.1f} MB → app/native/")
                copied += 1
                continue
            if name == "lang":
                # 语言包要进（后端 i18n 依赖它），但排除缓存。
                shutil.copytree(src, os.path.join(dest_dir, name),
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                copied += 1
                continue
            continue
        if name in BACKEND_EXCLUDE_FILES or name in BACKEND_EXCLUDE_STATE:
            continue
        if name.endswith(BACKEND_EXCLUDE_SUFFIX):
            continue
        shutil.copy2(src, os.path.join(dest_dir, name))
        copied += 1
    return copied


def launcher_bat() -> str:
    return (
        "@echo off\r\n"
        "rem EchoDesk 后端启动脚本（standalone Python，目标机无需安装 Python）\r\n"
        "rem 用途：手动启动/调试后端；前端会直接调用 python\\python.exe，不经这个脚本。\r\n"
        "setlocal\r\n"
        'set "HERE=%~dp0"\r\n'
        "rem 切到脚本所在目录再启动：后端的相对路径（models/、skills_dev/、results/）\r\n"
        "rem 都是相对**工作目录**解析的，不 cd 就会跟着调用者的当前位置跑，\r\n"
        "rem 于是同一个包在不同地方启动会去不同的 models/ 找模型（v0.2.2 修正）。\r\n"
        'cd /d "%HERE%"\r\n'
        "rem UTF-8 是硬要求：POSIX/中文 Windows 下若用 GBK/ASCII，中文与 emoji 会让 Python 抛 UnicodeEncodeError。\r\n"
        "set \"PYTHONUTF8=1\"\r\n"
        "set \"PYTHONIOENCODING=utf-8\"\r\n"
        "rem 不写 .pyc：发布目录可能只读，且减少运行期残留（卸载与门禁都更干净）。\r\n"
        "set \"PYTHONDONTWRITEBYTECODE=1\"\r\n"
        'set "PYTHONPATH=%HERE%site-packages;%HERE%app"\r\n'
        '"%HERE%python\\python.exe" "%HERE%app\\main.py" %*\r\n'
    )


def launcher_sh() -> str:
    return (
        "#!/bin/sh\n"
        "# EchoDesk 后端启动脚本（standalone Python，目标机无需安装 Python）\n"
        "# 用途：手动启动/调试后端；前端会直接调用 python/bin/python3，不经这个脚本。\n"
        "set -e\n"
        'HERE=$(cd "$(dirname "$0")" && pwd)\n'
        "# 切到脚本所在目录再启动：后端的相对路径（models/、skills_dev/、results/）\n"
        "# 都是相对**工作目录**解析的，不 cd 就会跟着调用者的当前位置跑，\n"
        "# 于是同一个包在不同地方启动会去不同的 models/ 找模型（v0.2.2 修正）。\n"
        'cd "$HERE"\n'
        "# UTF-8 是硬要求：POSIX locale 下中文与 emoji 会让 Python 抛 UnicodeEncodeError。\n"
        "PYTHONUTF8=1\n"
        "PYTHONIOENCODING=utf-8\n"
        "# 不写 .pyc：发布目录可能只读，且减少运行期残留（卸载与门禁都更干净）。\n"
        "PYTHONDONTWRITEBYTECODE=1\n"
        'PYTHONPATH="$HERE/site-packages:$HERE/app"\n'
        "export PYTHONUTF8 PYTHONIOENCODING PYTHONDONTWRITEBYTECODE PYTHONPATH\n"
        "# exec：让后端直接接管本进程，信号（Ctrl-C / SIGTERM）与退出码都原样透传，\n"
        "# 不在中间多留一层 shell —— 前端是按子进程管理它的，多一层会带来终止不干净的风险。\n"
        'exec "$HERE/python/bin/python3" "$HERE/app/main.py" "$@"\n'
    )


def installed_packages(site_packages: str) -> list[dict[str, str]]:
    """从 *.dist-info 读实际装上的依赖版本（manifest 里记录真相，而不是记"我们要求了什么"）。"""
    result: list[dict[str, str]] = []
    if not os.path.isdir(site_packages):
        return result
    for name in sorted(os.listdir(site_packages)):
        if not name.endswith(".dist-info"):
            continue
        meta = os.path.join(site_packages, name, "METADATA")
        package = version = ""
        if os.path.isfile(meta):
            with io.open(meta, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Name: ") and not package:
                        package = line[6:].strip()
                    elif line.startswith("Version: ") and not version:
                        version = line[8:].strip()
                    if package and version:
                        break
        if not package:
            package = name[: -len(".dist-info")]
        result.append({"name": package, "version": version or "unknown"})
    return result


def sanitize_site_packages(site_packages: str) -> list[str]:
    """[EN] Remove installer metadata that embeds absolute build-machine paths.

    [ZH] 移除嵌入了构建机绝对路径的安装器元数据。

    ``pip install --target`` writes ``direct_url.json`` for wheel installs performed from a
    local ``file://`` URL. That file contains the absolute cache path, which is not part of
    the dependency and would make the tree depend on the build machine layout.
    """
    removed: list[str] = []
    if not os.path.isdir(site_packages):
        return removed
    for name in sorted(os.listdir(site_packages)):
        if not name.endswith(".dist-info"):
            continue
        direct = os.path.join(site_packages, name, "direct_url.json")
        if os.path.isfile(direct):
            try:
                os.remove(direct)
                removed.append(os.path.relpath(direct, site_packages))
            except OSError:
                pass
    return removed


def make_archive(tree: str, out_dir: str, fmt: str) -> str:
    """[EN] Delegate archive creation to the shared deterministic writers.

    [ZH] 将归档创建委托给共享的确定性写入器。
    """
    name = os.path.basename(tree)
    if fmt == "zip":
        path = os.path.join(out_dir, name + ".zip")
        repro.write_zip(tree, path)
        return path
    path = os.path.join(out_dir, name + ".tar.gz")
    repro.write_tar_gz(tree, path, arcname=name)
    return path


# ---------------------------------------------------------------------------
def build_one(rid: str, args: argparse.Namespace, release: dict | None, version: str) -> dict:
    spec_target = PLATFORMS[rid]
    out_root = os.path.join(args.out)
    tree = os.path.join(out_root, f"backend-{rid}")
    py_ver = py_minor()

    log("")
    log(f"===== {rid}（{spec_target['triple']}）=====")

    # [EN] Validate the native runtime before downloading Python or installing packages:
    # [EN] a platform without ABI-matching llama.cpp libraries must fail in seconds with a
    # [EN] clear message, not after hundreds of megabytes and then produce a broken tree.
    # [ZH] 在下载 Python、安装依赖之前先校验原生运行期：缺少 ABI 匹配的 llama.cpp 动态库
    # [ZH] 的平台必须在数秒内明确失败，而不是先下载数百 MB、最后产出一个不可用的目录树。
    try:
        native_preview = verify_native_libraries(rid)
    except SystemExit as exc:
        if not args.plan:
            raise
        log(str(exc))
        log("[EN] (plan mode: the failure above would stop a real build for this target)"
            " / [ZH]（演练模式：上面的失败会在真实构建中阻止该目标）")
    else:
        if args.plan:
            log(f"[EN] native runtime ready: {len(native_preview)} libraries"
                f" / [ZH] 原生运行期就绪：{len(native_preview)} 个动态库")

    # ---------- 1. standalone Python ----------
    python_dir = os.path.join(tree, "python")
    if args.reuse_python and os.path.isdir(python_dir):
        log(f"复用已解压的 standalone Python：{os.path.relpath(python_dir, ROOT)}")
    else:
        if release is None:
            release = fetch_release_info()
        asset = pick_asset(release, spec_target["triple"], py_ver, keep_symbols=args.keep_symbols)
        cache_archive = os.path.join(CACHE_ROOT, asset["name"])
        download(asset["browser_download_url"], cache_archive, dry=args.plan)
        if not args.plan:
            log(f"解压 {asset['name']} → {os.path.relpath(python_dir, ROOT)}")
            extract_python(cache_archive, tree)
            if not args.keep_python_extras:
                removed = strip_python(python_dir, py_ver)
                for item in removed:
                    log(f"  清理 {item}")

    # ---------- 2. 依赖 ----------
    site_packages = os.path.join(tree, "site-packages")
    pip_args, requirements, decisions = resolve_requirements(spec_target)
    if not args.plan:
        log(f"目标平台依赖 {len(requirements)} 项（环境标记与构建工具已按目标平台过滤）：")
        for requirement, why in decisions:
            log(f"  · {requirement}  —— {why}")

    resolved_req = os.path.join(CACHE_ROOT, f"requirements-{rid}.txt")
    if not args.plan:
        write_resolved_requirements(requirements, pip_args, resolved_req)

    if args.reuse_deps and os.path.isdir(site_packages) and os.listdir(site_packages):
        log(f"复用已装依赖：{os.path.relpath(site_packages, ROOT)}")
    else:
        if not args.plan and os.path.isdir(site_packages):
            shutil.rmtree(site_packages)
        pip_cmd = [
            sys.executable, "-m", "pip", "install",
            "--target", site_packages,
            "--platform", spec_target["pip_platform"],
            "--python-version", py_ver,
            "--implementation", "cp",
            # [EN] Pin the download cache inside the workspace so both reproducible-build
            # [EN] runs share the already-downloaded wheels; only downloads are cached, the
            # [EN] installed tree is always rebuilt from wheels.
            # [ZH] 把下载缓存固定在仓库内，使两次可复现构建共享已下载的 wheel；缓存只保存
            # [ZH] 下载结果，安装目录始终由 wheel 重新解出。
            "--cache-dir", PIP_CACHE,
            # [EN] Prefer wheels already promoted into ``artifacts/wheels`` before any index
            # [EN] or URL download. That directory is the same verified cache used by
            # [EN] ``local_wheel_override``; adding it as a find-links source lets the release
            # [EN] pipeline install large pinned wheels (Playwright, cryptography, Pillow)
            # [EN] offline instead of re-downloading them at the build machine's link speed.
            # [EN] The cache is an input pinned by ``backend/requirements.lock``, so using it
            # [EN] does not change any output byte: the same wheel is unpacked either way.
            # [ZH] 优先使用已经放进 ``artifacts/wheels`` 的轮子，而不是走索引或 URL 下载。
            # [ZH] 该目录正是 ``local_wheel_override`` 使用的同一份已校验缓存；把它作为
            # [ZH] find-links 来源，可让发布流水线离线安装体积大、版本被钉死的轮子
            # [ZH] （Playwright、cryptography、Pillow），不再受构建机链路速度拖累。
            # [ZH] 该缓存由 ``backend/requirements.lock`` 钉死，属于输入；无论从缓存还是从
            # [ZH] 网络取得，解出的都是同一个轮子，因此不会改变任何输出字节。
            *(["--find-links", WHEEL_CACHE] if os.path.isdir(WHEEL_CACHE) else []),
            # [EN] Optional mirror override for networks where the upstream index is slow.
            # [EN] CI and maintainers may export ECHODESK_PIP_INDEX_URL (for example
            # [EN] ``https://pypi.tuna.tsinghua.edu.cn/simple``); an empty value keeps pip's
            # [EN] own configuration. Versions are still pinned by ``requirements.lock``: a
            # [EN] mirror changes where a wheel is fetched from, never which version is used.
            # [ZH] 可选镜像源覆盖，用于上游索引很慢的网络。CI 与维护者可以导出
            # [ZH] ECHODESK_PIP_INDEX_URL（例如 ``https://pypi.tuna.tsinghua.edu.cn/simple``）；
            # [ZH] 空值时沿用 pip 自身的配置。版本仍由 ``requirements.lock`` 钉死：镜像只改变
            # [ZH] 轮子的获取位置，绝不改变使用的版本。
            *(["--index-url", os.environ["ECHODESK_PIP_INDEX_URL"].strip()]
              if os.environ.get("ECHODESK_PIP_INDEX_URL", "").strip() else []),
            # 只收预编译 wheel：交叉下载时 pip 无法编译，静默退回 sdist 会得到"装不上"或
            # "装上了却是给打包机的二进制"这两种更糟的结果。
            "--only-binary=:all:",
            "--no-compile",
            "--upgrade",
            "--no-warn-script-location",
            "--disable-pip-version-check",
            # 慢/抖动网络下的必需项：本机到上游索引与 GitHub Releases 的连接时通时断，
            # pip 默认 15 秒连接超时会让整轮直接失败。放宽超时与重试，
            # 避免"已经下了几十 MB 却因为一次抖动全部作废"。
            # 平衡值：pip 默认 15 秒在本机链路上"一抖就死"；而 120 秒会让**卡住变得看不出来**
            # ——单次超时 120 秒 × 8 次重试 = 最长 16 分钟才失败，实测就是这样静悄悄地等。
            # 30 秒 + 10 次重试：失败得快（约 5 分钟见分晓），又能熬过常见的抖动。
            "--timeout", "30",
            "--retries", "10",
        ] + pip_args + ["--no-deps", "-r", resolved_req]
        run(pip_cmd, dry=args.plan)
        if not args.plan:
            removed_metadata = sanitize_site_packages(site_packages)
            if removed_metadata:
                log(f"[EN] removed {len(removed_metadata)} absolute-path metadata file(s) from site-packages"
                    f" / [ZH] 已从 site-packages 移除 {len(removed_metadata)} 个含绝对路径的元数据文件")
            # [EN] Prune tests, bytecode caches and foreign-platform binaries after installation.
            # [ZH] 安装完成后清理测试、字节码缓存与异平台二进制。
            prune_summary = pruner.prune_tree(site_packages, rid)
            log(f"[EN] pruned {prune_summary['removed_files']} redundant files "
                f"({prune_summary['removed_bytes'] / 1048576:.1f} MB) from site-packages"
                f" / [ZH] 已从 site-packages 清理 {prune_summary['removed_files']} 个冗余文件"
                f"（{prune_summary['removed_bytes'] / 1048576:.1f} MB）")

    # ---------- 3. 后端代码 ----------
    if not args.plan:
        count = copy_backend_code(os.path.join(tree, "app"), rid)
        log(f"复制后端代码 {count} 个顶层条目 → app/")

    # ---------- 4. 启动脚本 ----------
    if not args.plan:
        is_windows = spec_target["sys_platform"] == "win32"
        launcher_name = spec_target["launcher"]
        with io.open(os.path.join(tree, launcher_name), "w", encoding="utf-8",
                     newline="" if is_windows else "\n") as handle:
            handle.write(launcher_bat() if is_windows else launcher_sh())
        # 另一个平台的脚本也给一份，便于在别的机器上查看（不影响本平台使用）。
        other_name = "run-backend.sh" if is_windows else "run-backend.bat"
        other_text = launcher_sh() if is_windows else launcher_bat()
        with io.open(os.path.join(tree, other_name), "w", encoding="utf-8",
                     newline="\n" if is_windows else "") as handle:
            handle.write(other_text)
        if not is_windows:
            os.chmod(os.path.join(tree, launcher_name), 0o755)
        log(f"启动脚本：{launcher_name}（另附 {other_name} 供跨平台查看）")

    # ---------- 5. manifest 与归档 ----------
    result: dict[str, object] = {"rid": rid, "triple": spec_target["triple"]}
    if args.plan:
        log("（演练结束，未产出任何文件）")
        return result

    packages = installed_packages(site_packages)
    py_size, py_files = dir_size(os.path.join(tree, "python"))
    deps_size, deps_files = dir_size(site_packages)
    app_size, app_files = dir_size(os.path.join(tree, "app"))
    total_size, total_files = dir_size(tree)
    # [EN] Record the native runtime explicitly: a package without it starts but cannot
    # [EN] run inference, and the manifest is the only per-platform evidence kept next to
    # [EN] the archive.
    # [ZH] 显式记录原生运行期：缺少它的包能启动却无法推理，而清单是归档旁边唯一保留的
    # [ZH] 分平台证据。
    native_payload = native_libraries(rid)
    native_bytes = sum(size for _name, _path, size in native_payload)

    # [EN] Fixed timestamps before archiving make the tree itself independent of the moment
    # [EN] at which each step ran. The archive writers also stamp every entry explicitly.
    # [ZH] 归档前统一时间戳，使目录树本身不依赖各步骤的执行时刻；归档写入器还会显式
    # [ZH] 固定每个条目的时间。
    repro.normalize_tree(tree)
    archive = make_archive(tree, out_root, spec_target["archive"]) if args.archive != "none" else ""
    archive_hash = ""
    if archive:
        archive_hash = sha256_of(archive)
        log(f"[EN] archive: {os.path.relpath(archive, ROOT)} ({os.path.getsize(archive) / 1048576:.1f} MB)"
            f" / [ZH] 归档：{os.path.relpath(archive, ROOT)}（{os.path.getsize(archive) / 1048576:.1f} MB）")
        sums_path = os.path.join(out_root, "backend-SHA256SUMS.txt")
        name = os.path.basename(archive)
        kept: list[str] = []
        if os.path.isfile(sums_path):
            with io.open(sums_path, encoding="utf-8") as existing:
                kept = [line for line in existing.read().splitlines()
                        if line.strip() and not line.rstrip().endswith("  " + name)]
        kept.append(f"{archive_hash}  {name}")
        with io.open(sums_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(kept) + "\n")

    manifest = {
        "version": version,
        "rid": rid,
        "triple": spec_target["triple"],
        "pip_platform": spec_target["pip_platform"],
        "python": {"version": py_ver, "standalone_release": (release or {}).get("tag_name", ""),
                   "bytes": py_size, "files": py_files},
        "dependencies": {"count": len(packages), "bytes": deps_size, "files": deps_files,
                         "packages": packages},
        "app": {"bytes": app_size, "files": app_files},
        "native": {"rid": rid, "count": len(native_payload), "bytes": native_bytes,
                   "libraries": [name for name, _path, _size in native_payload]},
        "tree": {"bytes": total_size, "files": total_files,
                 "path": os.path.relpath(tree, ROOT)},
        "archive": {"path": os.path.relpath(archive, ROOT) if archive else "",
                    "sha256": archive_hash,
                    "bytes": os.path.getsize(archive) if archive else 0},
        "launcher": spec_target["launcher"],
        "generated_at": repro.utc_iso(),
        "generated_on": f"{sys.platform} (python {sys.version.split()[0]})",
        "requirements_decisions": [{"requirement": r, "reason": w} for r, w in decisions],
        "build_only_excluded": list(BUILD_ONLY_PACKAGES),
        "notes": [
            "后端运行不需要目标机安装 Python：python/ 是随包的 standalone 解释器。",
            "前端不通过启动脚本拉起后端，而是直接调用 python/python.exe(或 python/bin/python3) + app/main.py；"
            "启动脚本是给人手动启动/调试用的。",
            "site-packages 与 python/ 并排，靠启动脚本/前端的 PYTHONPATH 注入，便于区分自带与新增依赖。",
        ],
    }
    manifest_path = os.path.join(out_root, f"backend-manifest-{rid}.json")
    with io.open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    log(f"清单：{os.path.relpath(manifest_path, ROOT)}")
    log(f"{rid} 解压后共 {total_files} 个文件，{total_size / 1048576:.1f} MB")
    log(f"  其中 python/ {py_size / 1048576:.1f} MB、site-packages/ {deps_size / 1048576:.1f} MB、"
        f"app/ {app_size / 1048576:.1f} MB")
    result.update(manifest)
    return result


def guard_playwright_browsers() -> None:
    """[EN] Point Playwright at an existing local browser bundle; never install one.
    [ZH] 让 Playwright 指向本地已存在的浏览器目录；绝不安装浏览器。

    [EN] Packaging installs the ``playwright`` Python wheel only. Browser binaries are not a
    [EN] distribution asset: EchoDesk drives the user's Microsoft Edge. Some hosts still run
    [EN] ``playwright install`` implicitly through post-install hooks, so when a bundle already
    [EN] exists (``ms-playwright`` on Windows, ``~/.cache/ms-playwright`` on Linux,
    [EN] ``~/Library/Caches/ms-playwright`` on macOS) this pins ``PLAYWRIGHT_BROWSERS_PATH`` to
    [EN] it. The function never creates a browser directory and never downloads anything.
    [ZH] 打包只安装 ``playwright`` 的 Python 轮子；浏览器二进制不是发布资产 —— EchoDesk 驱动
    [ZH] 用户系统上的 Microsoft Edge。某些主机的安装钩子会隐式触发 ``playwright install``，
    [ZH] 因此当本地已有浏览器目录时（Windows 的 ``ms-playwright``、Linux 的
    [ZH] ``~/.cache/ms-playwright``、macOS 的 ``~/Library/Caches/ms-playwright``），把
    [ZH] ``PLAYWRIGHT_BROWSERS_PATH`` 钉到该目录。本函数不创建目录，也不下载任何内容。
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        log(f"[EN] honouring PLAYWRIGHT_BROWSERS_PATH={os.environ['PLAYWRIGHT_BROWSERS_PATH']}"
            f" / [ZH] 沿用已有的 PLAYWRIGHT_BROWSERS_PATH")
        return
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
        os.path.join(home, ".cache", "ms-playwright"),
        os.path.join(home, "Library", "Caches", "ms-playwright"),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = candidate
            log(f"[EN] reusing local Playwright browsers: {candidate} (no download, no install)"
                f" / [ZH] 复用本地 Playwright 浏览器目录：{candidate}（不下载、不安装）")
            return
    log("[EN] no local Playwright browser bundle; the wheel is installed without browsers"
        " / [ZH] 未发现本地 Playwright 浏览器目录；只安装 Python 轮子，不安装浏览器")


def main() -> int:
    parser = argparse.ArgumentParser(description="EchoDesk 三平台后端打包（standalone Python）")
    parser.add_argument("--platform", action="append", default=[],
                        help=f"目标平台，可重复；默认全部。可选：{', '.join(PLATFORMS)}")
    parser.add_argument("--out", default=os.path.join(ROOT, "publish"), help="输出目录")
    parser.add_argument("--plan", action="store_true", help="只打印步骤，不下载也不写文件")
    parser.add_argument("--reuse-python", action="store_true", help="复用已解压的 standalone Python")
    parser.add_argument("--reuse-deps", action="store_true", help="复用已装依赖（不重新 pip install）")
    parser.add_argument("--keep-python-extras", action="store_true", help="不清理解释器里的测试套件等")
    parser.add_argument("--keep-symbols", action="store_true",
                        help="用未剥离调试符号的解释器归档（默认用 install_only_stripped，"
                             "体积小得多且不影响运行；只有要用 gdb/lldb 调原生崩溃时才需要它）")
    parser.add_argument("--archive", default="auto", choices=("auto", "none"))
    parser.add_argument("--allow-dirty", action="store_true",
                        help="[EN] development only: bypass the clean-worktree guard "
                             "/ [ZH] 仅限开发：绕过干净工作区校验")
    args = parser.parse_args()
    # [EN] Normalise the output root once: every later path is joined onto it, while
    # [EN] ``run()`` executes pip with cwd=ROOT. A relative --out would otherwise resolve
    # [EN] differently in those two places and install dependencies outside the tree.
    # [ZH] 只在这里归一化输出根目录：后续路径都拼接在它上面，而 ``run()`` 以 ROOT 为工作
    # [ZH] 目录执行 pip。相对 --out 会在两处解析出不同结果，把依赖装到目录树之外。
    args.out = os.path.abspath(args.out)

    repro.require_clean_worktree(args.allow_dirty)
    repro.source_date_epoch()
    guard_playwright_browsers()

    targets = args.platform or list(PLATFORMS)
    unknown = [t for t in targets if t not in PLATFORMS]
    if unknown:
        raise SystemExit(f"不认识的目标平台：{', '.join(unknown)}；可选：{', '.join(PLATFORMS)}")

    version = app_version()
    log(f"EchoDesk 后端跨平台打包　版本 {version}　Python {py_minor()}　目标：{', '.join(targets)}")
    log(f"产物目录：{os.path.relpath(args.out, ROOT)}")

    release: dict | None = None
    if not args.plan:
        log("查询 python-build-standalone 最新 Release …")
        release = fetch_release_info()
        log(f"  release {release.get('tag_name')}（{len(release.get('assets', []))} 个资产）")

    summaries = []
    for rid in targets:
        summaries.append(build_one(rid, args, release, version))

    if not args.plan:
        log("")
        log("===== 汇总 =====")
        for item in summaries:
            tree = item.get("tree", {})
            archive = item.get("archive", {})
            log(f"  {item['rid']:<11} 解压 {tree.get('bytes', 0) / 1048576:8.1f} MB / "
                f"{tree.get('files', 0):>6} 文件　归档 {archive.get('bytes', 0) / 1048576:7.1f} MB")
    log("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
