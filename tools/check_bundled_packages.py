"""比对「随包分发的后端实际打进了哪些顶层模块」与「SBOM/许可证清单声明的包」。

为什么需要这个检查（v0.2.1 发布复核时意识到的盲区）：
  * `docs/THIRD_PARTY_LICENSES.md` / `docs/SBOM.json` 是在**干净 venv**（只装
    `backend/requirements.txt`）里生成的，代表"应该随包分发什么"；
  * `app/backend/_internal` 里的东西是 **PyInstaller 在开发机环境里分析出来的**，
    代表"实际随包分发了什么"。
  两者口径不同：PyInstaller 会把依赖里 `try: import X / except ImportError` 形式的
  可选依赖也打进去（只要开发机装了 X）。于是发布物可能包含 SBOM 里没有的组件 ——
  对外分发时这就是"未声明的第三方组件"，属发布任务书「不该有的一样不能有」。
  v0.2.1 实测就是这样混进了 `cryptography`（后改为显式声明）与
  `chardet`/`bidi`（LGPL，后加入 spec 的 EXCLUDES）。

本脚本只做**只读比对**并如实报告：
  1. 用干净 venv 的 `importlib.metadata.packages_distributions()` 建立
     「顶层模块名 → 发行包名」映射；
  2. 扫 `_internal` 下的顶层项（目录/扩展模块），过滤掉标准库与 PyInstaller 运行时；
  3. 输出三张清单：能对应到清单包的、对应不到的（可疑/未声明）、以及清单里有但没打进去的。

注意：**纯 Python 模块在 PyInstaller 里进的是 PYZ 归档，不会以顶层文件形式出现在
`_internal` 中**。所以"声明了但没有对应顶层模块"那一节天然会有很多条目，
它只用于人工扫一眼有没有意外，不是失败条件。

退出码：0 = 无"对应不到的"项；1 = 存在未声明组件（需要人工判断是否可接受）；2 = 前提缺失。

用法：
    python tools/check_bundled_packages.py
    python tools/check_bundled_packages.py --venv artifacts/clean-venv/Scripts/python.exe
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VENV = os.path.join(ROOT, "artifacts", "clean-venv", "Scripts", "python.exe")
INTERNAL = os.path.join(ROOT, "publish", "EchoDesk-v0.2.1", "app", "backend", "_internal")

# PyInstaller 自身的运行时与 Python 标准库（不属第三方发行包）
PYINSTALLER_RUNTIME = {
    "pyimod01_archive", "pyimod02_importers", "pyimod03_ctypes", "pyimod04_pywin32",
    "pyi_rth_utils", "pyi_rth_inspect", "pyi_rth_pkgutil", "pyi_rth_pywin32",
    "struct", "pyi_splash",
}

# 不是模块的东西：发行包元数据目录与纯数据目录。
# 不过滤掉它们会让输出里一半是噪音（实测第一版 19 项"可疑"里 14 项是这个原因），
# 噪音一多就没人看结论了，门禁等于没做。
NOT_A_MODULE_SUFFIX = (".dist-info", ".data", ".libs", ".egg-info")
DATA_DIRS = {"pywin32_system32", "numpy.libs", "certifi", "tzdata", "setuptools"}
# 本项目自己的资源目录（随 datas 打进去的），不属第三方组件
OWN_RESOURCE_DIRS = {"lang", "docs", "examples", "skills", "skills_dev", "models"}

# 发行包名与顶层模块名不一致的少数情况（importlib.metadata 的
# packages_distributions() 偶尔映射不到，例如 pywin32 的 win32/*）
MODULE_ALIASES = {
    "win32": "pywin32", "pythonwin": "pywin32", "win32comext": "pywin32",
    "win32com": "pywin32", "pywintypes": "pywin32", "pythoncom": "pywin32",
    "yaml": "pyyaml", "PIL": "pillow", "dateutil": "python-dateutil",
}
# 打进 _internal 的 Python 标准库 DLL/扩展（名字与发行包无关）
STDLIB_LIKE_SUFFIX = (
    "_cp313-win_amd64.pyd", ".pyd",
)


def venv_package_map(venv_python: str) -> dict[str, str]:
    """{顶层模块名: 发行包名}，在干净 venv 里算。"""
    code = (
        "import json; from importlib.metadata import packages_distributions as p;"
        "print(json.dumps({k: (v[0] if v else '') for k, v in p().items()}))"
    )
    out = subprocess.run([venv_python, "-c", code], capture_output=True, check=True)
    return json.loads(out.stdout.decode("utf-8"))


def clean_venv_manifest(venv_python: str) -> dict[str, str]:
    """{归一化发行包名: 版本}。"""
    code = (
        "import json;from importlib.metadata import distributions as d;"
        "print(json.dumps({(x.metadata.get('Name') or '').lower().replace('_','-'): x.version for x in d()}))"
    )
    out = subprocess.run([venv_python, "-c", code], capture_output=True, check=True)
    return json.loads(out.stdout.decode("utf-8"))


def top_level_entries(internal: str) -> list[str]:
    """_internal 下真正算"模块"的顶层名（滤掉元数据目录、数据目录与自有资源）。"""
    names = []
    for entry in os.listdir(internal):
        full = os.path.join(internal, entry)
        if entry.endswith(NOT_A_MODULE_SUFFIX):
            continue
        if entry in DATA_DIRS or entry in OWN_RESOURCE_DIRS:
            continue
        if os.path.isdir(full):
            names.append(entry)
            continue
        low = entry.lower()
        if low.endswith((".dll", ".json", ".toc", ".zip")):
            continue
        # 扩展模块名形如 `name.cp313-win_amd64.pyd`（带 ABI 标签），
        # 所以要去掉**所有**后缀，只留第一段 —— 只 rsplit 一次会留下 `.cp313-win_amd64`。
        stem = entry.split(".")[0]
        # mypyc 编译产物带随机前缀（如 ada92cb5d92a588d1b93__mypyc.pyd），
        # 它是 charset_normalizer 之类已声明包的编译辅助模块，不是独立组件。
        # 必须在**去掉扩展名之后**判断，否则 `...__mypyc.pyd` 会漏过去（实测踩到）。
        if stem.endswith("__mypyc"):
            continue
        if low.endswith(".pyd") or low.endswith(".py"):
            names.append(stem)
    return sorted(set(names))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venv", default=DEFAULT_VENV, help="干净 venv 的 python.exe")
    ap.add_argument("--internal", default=INTERNAL, help="_internal 目录")
    args = ap.parse_args()

    if not os.path.isfile(args.venv):
        print(f"未找到干净 venv 解释器：{args.venv}")
        print("请先建一个只装 backend/requirements.txt 的 venv（见 docs/v0.2.1-发布记录.md）。")
        return 2
    if not os.path.isdir(args.internal):
        print(f"未找到 _internal 目录：{args.internal}")
        print("请先跑 tools\\build_release.ps1 构建后端。")
        return 2

    mod2dist = venv_package_map(args.venv)
    manifest = clean_venv_manifest(args.venv)
    entries = top_level_entries(args.internal)

    stdlib = set(sys.stdlib_module_names)
    mapped: list[tuple[str, str]] = []
    unmapped: list[str] = []
    for name in entries:
        if name in PYINSTALLER_RUNTIME or name in stdlib or name.startswith("pyi_"):
            continue
        dist = mod2dist.get(name) or MODULE_ALIASES.get(name)
        if dist:
            mapped.append((name, dist))
        else:
            unmapped.append(name)

    bundled_dists = sorted({d.lower().replace("_", "-") for _m, d in mapped})
    not_bundled = sorted(set(manifest) - set(bundled_dists))

    print(f"干净 venv 声明的发行包   : {len(manifest)} 个")
    print(f"_internal 顶层模块       : {len(entries)} 个（已排除标准库与 PyInstaller 运行时）")
    print(f"  能对应到声明包         : {len(mapped)} 个")
    print(f"  对应不到（可疑）       : {len(unmapped)} 个")
    print(f"声明了但未见对应顶层模块 : {len(not_bundled)} 个（多为纯数据/元包，属正常）")
    print()

    if unmapped:
        print("!! 以下顶层模块在干净 venv 里找不到归属 —— 可能是开发机环境带进包的未声明组件：")
        for n in unmapped:
            print(f"   - {n}")
        print()
        print("   处理办法（二选一）：")
        print("     1) 加进 backend/EchoBackend.spec 的 EXCLUDES（不该随包分发时）；")
        print("     2) 补进 backend/requirements.txt 并在干净 venv 里重跑")
        print("        gen_licenses.py / gen_sbom.py，让它出现在合规清单里。")
    else:
        print("发布物内的顶层模块全部能对应到已声明的发行包。")

    # 清单里有、但发布物里没有对应顶层模块的包：多数是纯数据包（如 certifi 的 cacert.pem）
    # 或元包，属正常；这里只列名字，方便人工扫一眼有没有"声明了却没打进去"的意外。
    print()
    print("声明了但没有对应顶层模块的包（仅提示，通常为纯数据/元包）：")
    for n in not_bundled:
        print(f"   · {n}")

    return 1 if unmapped else 0


if __name__ == "__main__":
    raise SystemExit(main())
