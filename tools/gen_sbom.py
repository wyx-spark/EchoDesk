"""生成 SPDX 风格的软件物料清单（SBOM）并与第三方许可证清单核对。

用途（企业合规发布）：
  * 产出 ``docs/SBOM.json``（SPDX 2.3 的最小可用子集），供审计方机器读取；
  * 同时在 stdout 打印人类可读的对照摘要，并把与 ``docs/THIRD_PARTY_LICENSES.md``
    的**差异**列出来（清单里有而环境里没有、环境里有而清单里没有、版本或许可证不一致），
    便于完成"SBOM 与许可证清单核对一致"这项验收。

输入来源（全部为仓库内已有文件 + 本机已安装发行包，不联网）：
  1. ``backend/requirements.txt``   —— 后端直接依赖声明（发布依赖闭包的根）；
  2. ``frontend/EchoDesk.csproj``   —— 前端 ``<PackageReference>``（当前仓库为 0 个）；
  3. 本机已安装的 Python 发行包      —— ``importlib.metadata`` 读取 name/version/license；
  4. ``docs/THIRD_PARTY_LICENSES.md`` —— 由 ``tools/gen_licenses.py`` 生成的对照清单。

**作用域（为什么默认不是"整台机器装的包"）**：
默认 ``--scope release`` 只把"``backend/requirements.txt`` 的传递依赖闭包 ∪ 许可证清单"
写进 SBOM。开发机上常残留已移除功能留下的包（例如 pyxtxt 依赖链里的
extract-msg / pcodedmp / pygame / pynput，或 easyocr / torch 这类大件），
它们**不随发布物分发**，写进 SBOM 会让审计方以为发布物里有 GPL/AGPL 组件。
``--scope env`` 可生成"整机环境清单"，用于排查。
闭包计算会评估 ``Requires-Dist`` 上的环境标记（例如 ``; python_version < "3.11"``
或 ``; sys_platform == "darwin"``），只保留**当前解释器/平台真的会安装**的依赖；
extras 一律不计（默认安装不含 extra）。
为什么不评估标记（v0.2.1 之前的做法）：带标记的依赖在 Windows + CPython 3.13 上
根本不会被安装，把它们算进闭包会让 [3] 节的 B 类永远非空，
``--check`` 在干净环境里也永远失败，门禁因此失去意义
（实测踩到 exceptiongroup / tomli / importlib-metadata / macholib / httpx2-jsfetch 五个）。
评估标记需要 ``packaging``；环境里没有它时退化为"不评估标记"的保守超集，
并在输出里明确标注，不会静默改变语义。

设计约束：**只用标准库**（不引入任何新的第三方依赖）。
``packaging`` 是**可选**导入——没有它脚本照常工作，只是闭包退化为保守超集。

用法：
    python tools/gen_sbom.py              # 生成 docs/SBOM.json，打印摘要与差异；退出码 0
    python tools/gen_sbom.py --check      # 只核对不写文件；发现不一致时退出码 1（供 CI 用）
    python tools/gen_sbom.py --scope env  # 生成整机环境清单（排查开发机残留时用）
    python tools/gen_sbom.py --output D:\\out\\SBOM.json

判定为"不一致"并导致 ``--check`` 非 0 退出的情况（对应输出 [3] 节的 A–D，
以及 [1] 节里"直接依赖未安装"）：
  * A 清单里有、本机环境里没有安装；
  * B 发布依赖闭包里有、清单里没有（清单需要补）；
  * C 同名包版本不一致；
  * D 许可证标注不一致；
  * ``requirements.txt`` 直接声明的包在环境里找不到（见 [1] 节）。
输出 [3] 节的 E 类（环境里有、但既不在闭包也不在清单中的包，通常是开发机残留）
**默认只作提示、不影响退出码**；``--allow-extra`` 可省略其明细，
``--strict-env`` 则把它也当作失败条件（干净环境校验用）。

注意：本脚本读的是**本机环境**。请在与发布物一致的环境（干净 venv，只装
``backend/requirements.txt``）里运行，结果才代表真实发布内容。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from importlib.metadata import distributions
from typing import Dict, List, Optional, Sequence, Set, Tuple

import reproducible as repro

# 可选导入：只在评估 Requires-Dist 的环境标记时用到。
# 没有它脚本依然可用（退化为"不评估标记"的保守超集），因此不写进 requirements。
try:  # pragma: no cover - 取决于环境
    from packaging.requirements import Requirement as _PkgRequirement
except Exception:  # noqa: BLE001
    _PkgRequirement = None

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REQUIREMENTS_PATH = os.path.join(REPO_ROOT, "backend", "requirements.txt")
CSPROJ_PATH = os.path.join(REPO_ROOT, "frontend", "EchoDesk.csproj")
LICENSE_MD_PATH = os.path.join(REPO_ROOT, "docs", "THIRD_PARTY_LICENSES.md")
VERSION_PY_PATH = os.path.join(REPO_ROOT, "backend", "version.py")
OUT_JSON_PATH = os.path.join(REPO_ROOT, "docs", "SBOM.json")

#: 清单里"全部 Python 发行包明细"小节的标题关键词（**兜底**：老文档没有锚点时用）
MD_DETAIL_SECTION = "Python 发行包明细"
#: 清单里"后端直接依赖"小节的标题关键词（兜底）
MD_DIRECT_SECTION = "后端直接依赖"
#: **语言无关锚点**（HTML 注释，不进渲染结果）。文档怎么翻译都能定位到小节。
#: 加它的原因见 _parse_md_table 的说明：原先只认中文标题，而 THIRD_PARTY_LICENSES.md
#: 按名字是英文版 —— 一旦真的译成英文，SBOM 会静默丢掉全部包明细。
MD_DETAIL_MARKER = "<!-- sbom:python-detail -->"
MD_DIRECT_MARKER = "<!-- sbom:direct-deps -->"

SPDX_VERSION = "SPDX-2.3"
DATA_LICENSE = "CC0-1.0"
DOCUMENT_ID = "SPDXRef-DOCUMENT"
NOASSERTION = "NOASSERTION"

#: 需要人工关注的高风险许可证前缀（与 gen_licenses.py 的口径保持一致）
RISKY_PREFIXES = ("AGPL", "GPL", "LGPL", "SSPL")

#: 打印差异时最多列出多少个包名（超过则省略，避免刷屏）
MAX_LISTED = 40

_REQ_NAME = re.compile(r"^([A-Za-z0-9._\-]+)")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _norm(name: str) -> str:
    """包名归一化：小写 + 下划线转连字符（``huggingface_hub`` == ``huggingface-hub``）。"""
    return (name or "").strip().lower().replace("_", "-")


def _clean(value: str) -> str:
    """压缩空白，便于比较与展示。"""
    return " ".join(str(value or "").split())


def _requirement_name(requirement: str) -> str:
    """从 ``name>=1.0; marker`` 这类依赖串里取出包名（归一化）。"""
    match = _REQ_NAME.match((requirement or "").strip())
    return _norm(match.group(1)) if match else ""


def _requirement_applies(requirement: str) -> bool:
    """该 ``Requires-Dist`` 是否适用于**当前解释器 / 平台**。

    返回 False 表示这条依赖在当前环境下不会被安装（标记不成立），
    例如 ``exceptiongroup; python_version < "3.11"``、
    ``macholib; sys_platform == "darwin"``。
    extras 一律当作不适用（``extra == "..."`` 在 ``extra=""`` 下即为假）。

    没有 ``packaging`` 或解析失败时**保守地返回 True**（宁可多列也不漏列）。
    """
    if _PkgRequirement is None:
        return True
    try:
        parsed = _PkgRequirement((requirement or "").strip())
    except Exception:  # noqa: BLE001 - 解析不了就保守处理
        return True
    if parsed.marker is None:
        return True
    try:
        return bool(parsed.marker.evaluate({"extra": ""}))
    except Exception:  # noqa: BLE001
        return True


def _spdx_id(kind: str, name: str, version: str, used: Set[str]) -> str:
    """生成合法且唯一的 SPDXID（只允许字母、数字、``.``、``-``）。"""
    safe = re.sub(r"[^A-Za-z0-9.\-]", "-", f"SPDXRef-Package-{kind}-{name}-{version}")
    candidate = safe
    index = 2
    while candidate in used:
        candidate = f"{safe}-{index}"
        index += 1
    used.add(candidate)
    return candidate


def _license_of(dist) -> str:
    """读取发行包的许可证标识（与 tools/gen_licenses.py 同口径，便于两者核对）。"""
    meta = dist.metadata
    value = _clean(meta.get("License-Expression") or meta.get("License") or "")
    if not value or len(value) > 60:
        for item in meta.get_all("Classifier") or []:
            if item.startswith("License ::"):
                value = item.split("::")[-1].strip()
                break
    return value or NOASSERTION


def _homepage_of(dist) -> str:
    """取项目主页/源码地址（没有就返回空串，不猜测）。"""
    meta = dist.metadata
    url = _clean(meta.get("Home-page") or "")
    if url:
        return url
    for item in meta.get_all("Project-URL") or []:
        text = _clean(item)
        _, _, tail = text.partition(",")
        tail = tail.strip()
        if tail.startswith("http"):
            return tail
    return ""


def app_version() -> str:
    """从 backend/version.py 读取 APP_VERSION，避免在本脚本里重复维护版本号。"""
    try:
        with open(VERSION_PY_PATH, "r", encoding="utf-8") as fh:
            match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', fh.read())
        if match:
            return match.group(1)
    except OSError:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# 输入一：requirements.txt
# ---------------------------------------------------------------------------


def parse_requirements(path: str) -> List[str]:
    """解析 requirements.txt，返回直接依赖的包名（保留书写形式，已去重）。"""
    names: List[str] = []
    seen: Set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print(f"[WARN] 无法读取 {path}：{exc}")
        return names

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):      # 空行 / --extra-index-url / -r / -e 等选项
            continue
        line = line.split(";", 1)[0].strip()      # 环境标记
        line = line.split("[", 1)[0].strip()      # extras
        name = _requirement_name(line)
        if not name or name in seen:
            continue
        seen.add(name)
        match = _REQ_NAME.match(line)
        names.append(match.group(1) if match else name)
    return names


# ---------------------------------------------------------------------------
# 输入二：csproj 的 <PackageReference>
# ---------------------------------------------------------------------------


def parse_csproj(path: str) -> Tuple[List[Tuple[str, str]], str]:
    """解析 csproj，返回 ([(包名, 版本)], 目标框架)。

    版本取自 ``Version`` 属性或子元素 ``<Version>``；都没有时填 NOASSERTION（不猜版本）。
    """
    packages: List[Tuple[str, str]] = []
    framework = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            root = ET.fromstring(fh.read())
    except OSError as exc:
        print(f"[WARN] 无法读取 {path}：{exc}")
        return packages, framework
    except ET.ParseError as exc:
        print(f"[WARN] {path} 不是合法 XML：{exc}")
        return packages, framework

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "TargetFramework" and element.text and not framework:
            framework = element.text.strip()
        if tag != "PackageReference":
            continue
        name = (element.get("Include") or element.get("Update") or "").strip()
        if not name:
            continue
        version = (element.get("Version") or "").strip()
        if not version:
            for child in element:
                if child.tag.rsplit("}", 1)[-1] == "Version" and child.text:
                    version = child.text.strip()
                    break
        packages.append((name, version or NOASSERTION))
    return packages, framework


# ---------------------------------------------------------------------------
# 输入三：本机已安装发行包
# ---------------------------------------------------------------------------


def collect_installed() -> Tuple[List[dict], Dict[str, dict], Dict[str, List[str]]]:
    """读取本机全部 Python 发行包。

    返回 ``(按名称排序的行, 归一化名 → 行, 归一化名 → 依赖名列表)``。
    """
    rows: List[dict] = []
    by_name: Dict[str, dict] = {}
    requires: Dict[str, List[str]] = {}
    for dist in distributions():
        name = _clean(dist.metadata.get("Name") or "")
        if not name:
            continue
        key = _norm(name)
        row = {
            "name": name,
            "version": _clean(dist.version or "") or NOASSERTION,
            "license": _license_of(dist),
            "homepage": _homepage_of(dist),
        }
        rows.append(row)
        by_name[key] = row
        deps: List[str] = []
        for requirement in dist.requires or []:
            if "extra ==" in requirement:         # 可选 extra，默认不安装
                continue
            if not _requirement_applies(requirement):
                # 环境标记不成立：当前平台不会安装它，因此不属于发布闭包
                # （否则例外如 `macholib; sys_platform == "darwin"` 会被算进来）
                continue
            dependency = _requirement_name(requirement)
            if dependency:
                deps.append(dependency)
        requires[key] = deps
    rows.sort(key=lambda row: row["name"].lower())
    return rows, by_name, requires


def release_closure(roots: Sequence[str], by_name: Dict[str, dict],
                    requires: Dict[str, List[str]]) -> Tuple[Set[str], List[str]]:
    """计算发布依赖闭包：从 roots 出发沿 ``requires`` 逐层展开。

    返回 ``(闭包内的归一化包名集合, 闭包内但本机未安装的包名)``。
    """
    seen: Set[str] = set()
    queue: List[str] = [_norm(name) for name in roots]
    while queue:
        current = queue.pop()
        if not current or current in seen:
            continue
        seen.add(current)
        for dependency in requires.get(current, []):
            if dependency not in seen:
                queue.append(dependency)
    missing = sorted(name for name in seen if name not in by_name)
    return seen, missing


# ---------------------------------------------------------------------------
# 输入四：THIRD_PARTY_LICENSES.md
# ---------------------------------------------------------------------------


def _parse_md_table(text: str, section_keyword: str, marker: str = "") -> Dict[str, Tuple[str, str, str]]:
    """从 Markdown 表格里取 ``| 包名 | 版本 | 许可证 |`` 三列。

    **定位小节的方式**（v0.2.2 硬编码清查第 6 批收尾改的）：
    优先找 ``marker``（形如 ``<!-- sbom:python-detail -->`` 的 HTML 注释）—— 它是
    **语言无关**的锚点；找不到再退回 ``section_keyword``（中文小节标题）以保持兼容。

    为什么加 marker：原先只能靠中文标题定位，而 ``docs/THIRD_PARTY_LICENSES.md``
    按名字是"英文版"（实际正文目前仍是中文，见 check_lang 里已记在案的偏差）——
    一旦那份文档真的译成英文，这里会**静默取不到表格**（SBOM 少掉全部包明细）。
    有 marker 之后，文档怎么翻译都不影响取值。

    先定位小节标题，再向下扫描表格行；遇到小节内第一段非表格内容即停止。
    """
    rows: Dict[str, Tuple[str, str, str]] = {}
    lines = text.splitlines()
    start: Optional[int] = None
    for index, line in enumerate(lines):
        if marker and marker in line:
            # 锚点**后面紧跟的就是小节标题**（`<!-- sbom:… -->` + `## 五、…`）：
            # 必须连标题一起跳过去，否则下面的扫描循环一看到 `#` 就以为"小节结束"、
            # 直接取到 0 行 —— 这个坑是加锚点当轮的自检抓到的（有锚点时反而比没锚点更糟）。
            j = index + 1
            while j < len(lines) and not lines[j].strip().startswith("#"):
                j += 1
            start = j + 1 if j < len(lines) else j
            break
        if not marker and line.startswith("#") and section_keyword in line:
            start = index + 1
            break
    if start is None and marker:
        # 老文档没有 marker：退回中文标题（兼容），但把这件事说出来，别静默少数据
        for index, line in enumerate(lines):
            if line.startswith("#") and section_keyword in line:
                start = index + 1
                print(f"[WARN] {section_keyword}：文档里没有锚点 {marker}，已按中文标题定位"
                      f"（建议给该小节补上锚点注释）")
                break
    if start is None:
        print(f"[WARN] 取不到小节 {section_keyword!r}（锚点 {marker!r} 与中文标题都没找到）")
        return rows

    seen_data = False
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if not stripped.startswith("|"):
            if seen_data:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 3:
            continue
        if cells[0] in ("包名", "组件") or set(cells[0]) <= set("-: "):
            continue                              # 表头或分隔行
        name, version, license_text = cells
        if not name:
            continue
        rows[_norm(name)] = (name, version, license_text)
        seen_data = True
    return rows


def parse_license_md(path: str) -> Dict[str, Tuple[str, str, str]]:
    """返回许可证清单里"全部 Python 发行包明细"表，键为归一化包名。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"[WARN] 无法读取 {path}：{exc}")
        return {}
    return _parse_md_table(text, MD_DETAIL_SECTION, MD_DETAIL_MARKER)


# ---------------------------------------------------------------------------
# SBOM 组装
# ---------------------------------------------------------------------------


def build_sbom(installed: Sequence[dict], nuget: Sequence[Tuple[str, str]], version: str,
               scope: str) -> dict:
    """组装 SPDX 2.3 文档（最小可用子集）。"""
    used_ids: Set[str] = set()
    packages: List[dict] = []

    for row in installed:
        purl = f"pkg:pypi/{_norm(row['name'])}@{row['version']}"
        entry = {
            "SPDXID": _spdx_id("pypi", row["name"], row["version"], used_ids),
            "name": row["name"],
            "versionInfo": row["version"],
            # 发行包元数据里没有记录下载地址，故如实声明为 NOASSERTION
            # （安装来源见 externalRefs 的 purl 与 homepage 字段）。
            "downloadLocation": NOASSERTION,
            "licenseConcluded": row["license"],
            "licenseDeclared": row["license"],
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": purl,
                }
            ],
            "comment": "Python distribution installed in the build environment (importlib.metadata).",
        }
        if row["homepage"]:
            entry["homepage"] = row["homepage"]
        packages.append(entry)

    for name, package_version in nuget:
        packages.append(
            {
                "SPDXID": _spdx_id("nuget", name, package_version, used_ids),
                "name": name,
                "versionInfo": package_version,
                "downloadLocation": NOASSERTION,
                "licenseConcluded": NOASSERTION,
                "licenseDeclared": NOASSERTION,
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:nuget/{name}@{package_version}",
                    }
                ],
                "comment": "Declared as <PackageReference> in frontend/EchoDesk.csproj.",
            }
        )

    scope_note = (
        "Scope: transitive dependency closure of backend/requirements.txt plus every package "
        "listed in docs/THIRD_PARTY_LICENSES.md. Packages left over in the build machine's "
        "site-packages but outside that scope are intentionally excluded."
        if scope == "release"
        else "Scope: every Python distribution installed in the environment that generated this file."
    )

    return {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": DATA_LICENSE,
        "SPDXID": DOCUMENT_ID,
        "name": f"EchoDesk-{version}",
        # 版本唯一且可复现（同一版本重复运行得到同一命名空间），便于比对两次生成的差异。
        "documentNamespace": f"https://github.com/Void-Entropia-Studio/EchoDesk/spdx/{version}",
        "creationInfo": {
            # [EN] The creation timestamp is derived from SOURCE_DATE_EPOCH, so regenerating
            # [EN] the same commit produces a byte-identical document.
            # [ZH] 创建时间戳由 SOURCE_DATE_EPOCH 派生，因此同一提交重复生成会得到逐字节
            # [ZH] 一致的文档。
            "created": repro.utc_iso(),
            "creators": [
                "Tool: gen_sbom.py",
                "Organization: Void Entropia Studio",
            ],
            "comment": (
                "Generated with the Python standard library only, from backend/requirements.txt, "
                "frontend/EchoDesk.csproj and the locally installed distributions "
                "(importlib.metadata). License strings and version numbers come from package "
                "metadata of the machine that ran the tool and have not been verified package by "
                "package; regenerate in the release virtual environment before publishing."
            ),
        },
        "comment": (
            "Minimal SPDX 2.3 subset: spdxVersion, name, creationInfo, packages "
            "(name/versionInfo/licenseConcluded/downloadLocation/externalRefs). "
            + scope_note
            + " Run 'python tools/gen_sbom.py --check' to compare this inventory with "
              "docs/THIRD_PARTY_LICENSES.md."
        ),
        "packages": packages,
    }


# ---------------------------------------------------------------------------
# 差异核对
# ---------------------------------------------------------------------------


def diff_against_manifest(installed: Sequence[dict],
                          manifest: Dict[str, Tuple[str, str, str]],
                          requirements: Sequence[str],
                          closure: Set[str]) -> dict:
    """对比本机环境与许可证清单，返回各类差异。

    * ``only_in_manifest``：清单里有、环境里没有（清单不可信）。
    * ``only_in_closure``：发布闭包里有、清单里没有（清单需要补）。
    * ``unrelated``：环境里有，但既不在闭包也不在清单（开发机残留，不随发布物分发）。
    """
    env = {_norm(row["name"]): row for row in installed}

    version_mismatch: List[Tuple[str, str, str]] = []
    license_mismatch: List[Tuple[str, str, str]] = []
    for key in sorted(set(manifest) & set(env)):
        _md_name, md_version, md_license = manifest[key]
        row = env[key]
        if _clean(md_version) != _clean(row["version"]):
            version_mismatch.append((row["name"], md_version, row["version"]))
        if _clean(md_license).lower() != _clean(row["license"]).lower():
            license_mismatch.append((row["name"], md_license, row["license"]))

    return {
        "only_in_manifest": sorted(set(manifest) - set(env)),
        "only_in_closure": sorted(set(closure) - set(manifest)),
        "unrelated": sorted(set(env) - set(closure) - set(manifest)),
        "version_mismatch": version_mismatch,
        "license_mismatch": license_mismatch,
        "missing_declared": [name for name in requirements if _norm(name) not in env],
    }


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------


def _print_rows(keys: Sequence[str], env: Dict[str, dict], manifest: Dict[str, Tuple[str, str, str]]) -> None:
    """打印包名清单（超过上限时省略尾部）。"""
    for key in keys[:MAX_LISTED]:
        row = env.get(key)
        if row:
            print(f"    - {row['name']} {row['version']} ({row['license']})")
        else:
            name, version, license_text = manifest.get(key, (key, "?", "?"))
            print(f"    - {name} {version} ({license_text})")
    if len(keys) > MAX_LISTED:
        print(f"    … 其余 {len(keys) - MAX_LISTED} 个已省略")


def print_report(sbom: dict, diff: dict, manifest: Dict[str, Tuple[str, str, str]],
                 installed: Sequence[dict], requirements: Sequence[str],
                 nuget: Sequence[Tuple[str, str]], framework: str, scope: str,
                 closure_missing: Sequence[str], check: bool, allow_extra: bool) -> None:
    packages = sbom["packages"]
    env = {_norm(row["name"]): row for row in installed}
    python_packages = [entry for entry in packages if entry["SPDXID"].startswith("SPDXRef-Package-pypi")]
    nuget_packages = [entry for entry in packages if entry["SPDXID"].startswith("SPDXRef-Package-nuget")]

    print("=" * 72)
    print("EchoDesk SBOM 生成 / 核对")
    print("=" * 72)
    print(f"文档名称      : {sbom['name']}")
    print(f"SPDX 版本     : {sbom['spdxVersion']}（包条目 {len(packages)} 个："
          f"Python {len(python_packages)} + NuGet {len(nuget_packages)}）")
    print(f"作用域        : {scope}（release = requirements 传递闭包 ∪ 许可证清单）")
    print(f"Python 发行包 : 本机环境共 {len(installed)} 个")
    print(f"NuGet 直接依赖: {len(nuget)} 个" + (f"（目标框架 {framework}）" if framework else ""))
    print(f"requirements  : {len(requirements)} 个直接依赖声明")
    print(f"对照清单      : {len(manifest)} 个包")
    print()

    print("[1] requirements.txt 直接依赖是否都在环境中")
    if diff["missing_declared"]:
        for name in diff["missing_declared"]:
            print(f"    ✗ 未安装: {name}")
    else:
        print("    ✓ 全部已安装")
    if closure_missing:
        print(f"    闭包内但本机未安装（无法写入 SBOM）：{len(closure_missing)} 个")
        print("    （闭包计算忽略环境标记，这些通常是标记限定、当前平台不安装的可选依赖）")
        for name in closure_missing[:MAX_LISTED]:
            print(f"    - {name}")
    print()

    print("[2] SBOM 内包的许可证分布（按数量排序）")
    counts: Dict[str, int] = {}
    for entry in python_packages:
        counts[entry["licenseConcluded"]] = counts.get(entry["licenseConcluded"], 0) + 1
    for license_text, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]:
        print(f"    {count:>3}  {license_text}")
    risky = [(entry["name"], entry["versionInfo"], entry["licenseConcluded"])
             for entry in packages
             if entry["licenseConcluded"].upper().startswith(RISKY_PREFIXES)]
    if risky:
        print("    ⚠ 高风险许可证组件（需人工确认是否随发布物分发）：")
        for name, version, license_text in risky:
            print(f"      ! {name} {version} — {license_text}")
    else:
        print("    ✓ 未发现 AGPL/GPL/LGPL/SSPL 类组件")
    print()

    print("[3] 与 docs/THIRD_PARTY_LICENSES.md 的差异（对照本机完整环境）")
    print(f"    A. 清单里有、本机环境里没有：{len(diff['only_in_manifest'])} 个")
    _print_rows(diff["only_in_manifest"], env, manifest)
    print(f"    B. 发布闭包里有、清单里没有：{len(diff['only_in_closure'])} 个")
    _print_rows(diff["only_in_closure"], env, manifest)
    print(f"    C. 版本不一致：{len(diff['version_mismatch'])} 个")
    for name, md_version, env_version in diff["version_mismatch"]:
        print(f"    - {name}: 清单 {md_version} / 本机 {env_version}")
    print(f"    D. 许可证标注不一致：{len(diff['license_mismatch'])} 个")
    for name, md_license, env_license in diff["license_mismatch"]:
        print(f"    - {name}: 清单 {md_license} / 本机 {env_license}")
    print(f"    E. 环境里有但不在发布闭包内、也不在清单里（开发机残留，已排除出 SBOM）："
          f"{len(diff['unrelated'])} 个")
    if not allow_extra:
        _print_rows(diff["unrelated"], env, manifest)
    else:
        print("        （按 --allow-extra 省略明细）")
    print()

    blocking = (
        len(diff["only_in_manifest"])
        + len(diff["only_in_closure"])
        + len(diff["missing_declared"])
        + len(diff["version_mismatch"])
        + len(diff["license_mismatch"])
    )
    if check:
        if blocking:
            print(f"[--check] 不一致项共 {blocking} 处 → 退出码 1")
            print("          处理办法：在与发布物一致的环境里运行 python tools/gen_licenses.py "
                  "重新生成清单，或修正依赖后重跑本脚本。")
        else:
            print("[--check] 与许可证清单一致 → 退出码 0")
    else:
        print(f"[生成] 已写入 {OUT_JSON_PATH}（{len(packages)} 个包条目）")
        if blocking:
            print(f"       注意：存在 {blocking} 处清单差异（A–D），可用 --check 作为 CI 失败条件。")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 SPDX 风格 SBOM（docs/SBOM.json）并与第三方许可证清单核对。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true",
                        help="只核对不写文件；发现不一致时以退出码 1 结束（供 CI 使用）。")
    parser.add_argument("--scope", choices=("release", "env"), default="release",
                        help="release（默认）= requirements 传递闭包 ∪ 许可证清单；env = 整机环境清单。")
    parser.add_argument("--allow-extra", action="store_true",
                        help="不列出「环境里有但不在闭包内」的包（开发机残留很多时用）。")
    parser.add_argument("--strict-env", action="store_true",
                        help="把「环境里有但不在闭包内」的包也当作不一致（干净环境校验用）。")
    parser.add_argument("--output", default=OUT_JSON_PATH,
                        help=f"SBOM 输出路径，默认 {OUT_JSON_PATH}")
    args = parser.parse_args(argv)

    version = app_version()
    requirements = parse_requirements(REQUIREMENTS_PATH)
    nuget, framework = parse_csproj(CSPROJ_PATH)
    installed, by_name, requires = collect_installed()
    manifest = parse_license_md(LICENSE_MD_PATH)

    closure, closure_missing = release_closure(requirements, by_name, requires)
    closure |= set(manifest)                       # 清单里的包一律纳入，双向往核对齐
    scope_keys = closure if args.scope == "release" else set(by_name)
    scoped_rows = [row for row in installed if _norm(row["name"]) in scope_keys]

    sbom = build_sbom(scoped_rows, nuget, version, args.scope)
    diff = diff_against_manifest(installed, manifest, requirements, closure)

    if not args.check:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(sbom, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    print_report(sbom, diff, manifest, installed, requirements, nuget, framework,
                 args.scope, closure_missing, args.check, args.allow_extra)

    if args.check:
        blocking = (
            len(diff["only_in_manifest"])
            + len(diff["only_in_closure"])
            + len(diff["missing_declared"])
            + len(diff["version_mismatch"])
            + len(diff["license_mismatch"])
            + (len(diff["unrelated"]) if args.strict_env else 0)
        )
        return 1 if blocking else 0
    return 0


def _reconfigure_stdout() -> None:
    """尽量把 stdout 设为 UTF-8，避免中文摘要在某些控制台编码下抛 UnicodeEncodeError。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 老环境没有 reconfigure 时忽略
        pass


if __name__ == "__main__":
    _reconfigure_stdout()
    sys.exit(main())
