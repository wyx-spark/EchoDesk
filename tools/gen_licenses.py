"""生成第三方组件许可证清单（docs/THIRD_PARTY_LICENSES.md）。

用途：开源合规与 SBOM 的基础清单。后端由 PyInstaller 打包，实际随发布物
分发的是以下 Python 发行包（及其内嵌的原生库，如 llama.cpp）。
前端为自包含 .NET 部署，随包分发的是 Microsoft .NET / WPF 运行时程序集
（MIT 许可），无第三方 NuGet 依赖。

说明：许可证字段取自各发行包元数据（importlib.metadata），并非逐包人工核对，
正式对外发布前建议再用专门的合规工具（如 pip-licenses / Syft）复核一遍。

用法：python tools/gen_licenses.py
"""
import os
import re
from importlib.metadata import distributions
from typing import Dict, List, Tuple

# [EN] There is deliberately **no** hand-written root-package list here any more. The
# [EN] direct dependencies come from ``backend/requirements.txt`` -- the only place that
# [EN] declares what this backend actually needs -- and are filled in by
# [EN] ``declared_direct()`` below. A hand-copied list survived the Reader strip and kept
# [EN] claiming dependencies the build no longer ships, so the generated inventory
# [EN] described components that are not there. Deriving the list makes any future strip
# [EN] automatically correct; the transitive closure still comes from the installed
# [EN] environment inside ``collect()``.
# [ZH] 这里**刻意不再有**手写的根包清单。直接依赖来自 ``backend/requirements.txt`` ——
# [ZH] "本后端实际依赖什么"的唯一声明处 —— 由下面的 ``declared_direct()`` 填充。此前那份
# [ZH] 手抄列表在 Reader 裁剪后没有同步，一直声明着构建早已不随包分发的依赖，于是生成的
# [ZH] 清单描述了根本不存在的组件。改为派生后，任何一次裁剪都会自动正确；传递闭包仍由
# [ZH] ``collect()`` 遍历已安装环境得到。

OUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "docs", "THIRD_PARTY_LICENSES.md"
)
REQUIREMENTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "backend", "requirements.txt"
)


def _norm(name: str) -> str:
    """依赖名归一化（PEP 503）：比较发行名与 ``importlib.metadata`` 报的 Name 时必需。

    为什么必须有它：``requirements.txt`` 写的是发行名（连字符），而 ``metadata['Name']``
    常用下划线，两条字符串直接比较会把**明明是直接依赖的包**漏掉 —— 合规清单少列直接依赖，
    比多列更危险（实测由发布复核发现）。
    """
    return name.strip().lower().replace("_", "-").replace(".", "-")


def declared_direct() -> List[str]:
    """从 backend/requirements.txt 读出**直接声明**的依赖名。

    清单的「直接依赖」一节必须由这个文件驱动，而不是靠一份手抄的列表：
    手抄列表会随着 requirements.txt 增删而悄悄过期（Reader 裁剪后仍残留已删组件，
    就是这条路径造成的），而派生只会在源文件变化时变化。
    """
    names: List[str] = []
    try:
        with open(REQUIREMENTS_PATH, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                # [EN] Split on the PEP 508 separators **and** on the direct-URL marker
                # [EN] (``name @ https://...``): without ``@`` a pinned wheel URL was parsed as
                # [EN] part of the distribution name, so the release got a bogus
                # [EN] "not installed" warning and the generated inventory carried a
                # [EN] corrupted entry. Neither ``@`` nor ``=<>![];`` may appear in a name.
                # [ZH] 除 PEP 508 的版本/extras/标记分隔符外，必须同时按**直接 URL 标记**
                # [ZH] （``name @ https://...``）切分：以前没切 ``@``，被钉死的轮子 URL 会被
                # [ZH] 当成包名的一部分，于是发布清单出现"未安装"的假告警并把网址写进表格。
                # [ZH] 合法的发行包名不允许包含 ``@`` 或 ``=<>![];``。
                name = re.split(r"[<>=!\[;@]", line, maxsplit=1)[0].strip()
                if name and _norm(name) not in {_norm(n) for n in names}:
                    names.append(name)
    except OSError as exc:  # noqa: BLE001 - 读不到就没有直接依赖可列
        # [EN] No hand-written fallback list exists any more: silently inventing one is what
        # [EN] produced a stale inventory. The caller still emits the installed closure, and
        # [EN] this line tells the operator to fix the source file and re-run.
        # [ZH] 不再回退到手写清单：凭空补一份正是过期清单的来源。调用方仍会按已安装环境输出
        # [ZH] 传递闭包，这一行提示操作者修好源文件后重跑。
        print(f"（无法读取 {REQUIREMENTS_PATH}: {exc}；直接依赖为空，请修复后重跑）")
    return names


# [EN] The former hard-coded root-package list is gone; this name is kept as the derived
# [EN] view so nothing downstream has to change its vocabulary.
# [ZH] 原硬编码根包清单已删除；保留该名字作为派生视图，下游用词无需改动。
def root_packages() -> List[str]:
    """[EN] Direct dependencies of this build, derived from requirements.txt. [ZH] 由 requirements.txt 派生的直接依赖。"""
    return declared_direct()


ROOT_PACKAGES: List[str] = declared_direct()


# 需要人工关注的高风险许可证（会影响分发方式）
RISKY = {
    "AGPL": "强著佐权：以网络服务方式提供或对外分发时须提供完整对应源码，商用需评估",
    "GPL": "著佐权：对外分发衍生作品须以同许可证开源",
    "LGPL": "弱著佐权：动态链接可闭源分发，但需允许替换该库并提供其源码",
    "SSPL": "非 OSI 认证：限制作为服务提供",
}

# 已知的许可证例外与需特别说明的包
EXCEPTIONS = {
    "pyinstaller": "GPLv2 + 官方特别例外：明确允许用 PyInstaller 打包并分发专有程序，"
                   "打包产物本身不受 GPL 约束（构建工具，不随产物以库形式使用）",
    "pypdf": "BSD-3-Clause（纯 Python，无原生扩展）。v0.2.1 起取代 AGPL 的 PyMuPDF "
             "用于 PDF 文本提取，消除对外分发时的著佐权义务，同时把该依赖的安装体积"
             "从 52.9MB 降到约 1.4MB（见 records/v0.2.1-研发记录.md）。",
    "extract-msg": "GPL-3.0；由 pyxtxt 解析邮件类文档时可能被引入，"
                   "若被打进发布物则整体分发需遵循 GPL",
    "pcodedmp": "GPL-3.0；extract-msg 的依赖",
    "rtfde": "LGPL-3.0；extract-msg 的依赖",
    "python-bidi": "LGPL；文本方向处理依赖",
    "pygame": "LGPL；pyxtxt 依赖链引入",
    "pynput": "LGPL-3.0；pyxtxt 依赖链引入",
}


def _license_of(dist) -> str:
    meta = dist.metadata
    value = meta.get("License-Expression") or meta.get("License") or ""
    value = " ".join(str(value).split())
    if not value or len(value) > 60:
        classifiers = meta.get_all("Classifier") or []
        for item in classifiers:
            if item.startswith("License ::"):
                value = item.split("::")[-1].strip()
                break
    return value or "未标注"


def collect() -> List[Tuple[str, str, str, str]]:
    """返回 [(包名, 版本, 许可证, 主页)]，按包名排序。"""
    rows: List[Tuple[str, str, str, str]] = []
    for dist in distributions():
        name = dist.metadata.get("Name") or ""
        if not name:
            continue
        rows.append((
            name,
            dist.version or "",
            _license_of(dist),
            dist.metadata.get("Home-page") or dist.metadata.get("Project-URL") or "",
        ))
    rows.sort(key=lambda r: r[0].lower())
    return rows


def main() -> None:
    rows = collect()
    by_name = {name.lower(): lic for name, _v, lic, _h in rows}

    lines: List[str] = [
        "# EchoDesk 第三方组件与许可证清单",
        "",
        "本文件列出 EchoDesk 分发时随附的第三方组件及其许可证，用于开源合规审查。",
        f"扫描到的 Python 发行包共 {len(rows)} 个（含传递依赖）。",
        "",
        "> **v0.2.1 合规变更（任务书 1.3）**：对外开源发布前，PDF 解析已从 **PyMuPDF**",
        "> （AGPL-3.0 或 Artifex 商业许可）换成 **pypdf**（BSD-3-Clause，纯 Python）。",
        "> 项目本体采用 **Apache-2.0**，与 AGPL 不兼容；替换后发布物中不再含 AGPL 组件，",
        "> 剩余的唯一 GPL 组件是构建工具 PyInstaller（GPLv2 带官方例外，允许打包分发专有程序）。",
        "> 同步收益：该依赖的安装体积从 52.9MB 降到约 1.4MB。",
        "",
        # v0.2.2 硬编码清查第 6 批收尾：给 SBOM 生成器一个**语言无关**锚点
        # （tools/gen_sbom.py 优先按它定位表格；文档翻译后也不会失配）
        "<!-- sbom:direct-deps -->",
        "## 一、后端直接依赖",
        "",
        "| 组件 | 版本 | 许可证 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    # [EN] Direct dependencies come from exactly one place: requirements.txt. The former
    # [EN] union with a hand-written list is what let removed components linger.
    # [ZH] 直接依赖只有一个来源：requirements.txt。以前与手写清单取并集，正是过期条目残留的
    # [ZH] 原因。
    direct = {_norm(n) for n in declared_direct()}
    listed = 0
    for dist in distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if _norm(name) not in direct:
            continue
        lic = _license_of(dist)
        note = RISKY.get(lic.split()[0].upper(), "") if lic else ""
        lines.append(f"| {name} | {dist.version} | {lic} | {note} |")
        listed += 1
    # 声明了但当前环境里没装的直接依赖要显式暴露：清单缺条目比多条目危险得多
    installed = {_norm((d.metadata.get("Name") or "").strip()) for d in distributions()}
    absent = sorted(n for n in direct if n and n not in installed)
    if absent:
        lines.append("")
        lines.append(
            "> ⚠️ 以下直接依赖在当前扫描环境里未安装，因此无法给出许可证："
            + "、".join(f"`{n}`" for n in absent)
            + "。请在装全 `backend/requirements.txt` 的环境里重新生成本清单。"
        )
    print(f"「一、后端直接依赖」列出 {listed} 个（requirements.txt 声明 {len(declared_direct())} 个）")
    if absent:
        print(f"⚠️ 未安装的直接依赖 {len(absent)} 个: {absent}")

    lines += [
        "",
        "## 二、高风险许可证提示",
        "",
    ]
    hits = [r for r in rows if any(k in r[2].upper() for k in RISKY)]
    if hits:
        for name, version, lic, _home in hits:
            key = next((k for k in RISKY if k in lic.upper()), "")
            note = EXCEPTIONS.get(name.lower()) or RISKY.get(key, "")
            lines.append(f"- **{name} {version}** — {lic}")
            lines.append(f"  - {note}")
    else:
        lines.append("- 本次扫描未发现 AGPL/GPL/LGPL/SSPL 类许可证组件。")

    lines += [
        "",
        "### 合规结论与建议",
        "",
        "- **自用 / 组织内部使用**：不构成「对外分发」，上述著佐权义务不触发，当前依赖可直接使用。",
        "- **对外分发（含免费公开）**：v0.2.1 起发布物中已无 AGPL/GPL 运行时组件。",
        "  历史情况：v0.2.0 及更早版本的 PDF 解析用的是 AGPL 的 PyMuPDF，",
        "  v0.2.1 换成了 BSD 的 `pypdf`，因此**同一个 Apache-2.0 许可即可覆盖整个发布物**。",
        "- **文件解析全部为宽松许可**：PDF 用 `pypdf`（BSD-3），Word 用 `python-docx`（MIT）、",
        "  Excel 用 `openpyxl`（MIT）/`xlrd`（BSD），PPTX 用 `python-pptx`（MIT），",
        "  TXT/CSV 直接读取——均可闭源或开源分发。",
        "- **PyInstaller 无需担心**：其 GPLv2 带官方例外，明确允许打包分发专有程序，",
        "  且它只用于构建、不以库的形式随产物使用。",
        "- **技能（插件）由第三方提供**：用户自行安装的技能可能有各自的许可证，",
        "  EchoDesk 不以任何方式再分发技能代码；设置窗口会展示技能声明的许可证供用户判断。",
        "",
    ]

    lines += [
        "## 三、前端组件",
        "",
        "- 自包含 .NET 运行时与 WPF 程序集：MIT 许可（Microsoft .NET）。",
        "- 未引用任何第三方 NuGet 包（JSON 通信使用内置 System.Text.Json），",
        "  因此前端侧无额外开源合规负担。",
        "",
        "## 四、随包分发的原生库",
        "",
        "- 随包分发的 llama.cpp 原生库（llama / ggml / ggml-base / ggml-cpu / mtmd）：MIT 许可。",
        "  该动态库随 ``backend/native/`` 分发，不通过任何 Python 推理轮子引入。",
        "",
        "<!-- sbom:python-detail -->",
        "## 五、全部 Python 发行包明细",
        "",
        "| 包名 | 版本 | 许可证 |",
        "| --- | --- | --- |",
    ]
    for name, version, lic, _home in rows:
        lines.append(f"| {name} | {version} | {lic} |")

    lines += [
        "",
        "---",
        "",
        "生成方式：`python tools/gen_licenses.py`（读取本机已安装发行包的元数据）。",
        "复核建议：正式发布前用 `pip-licenses --format=markdown` 或 Syft 生成 SBOM 再核对一次。",
        "注意：本脚本读的是**本机环境**，若开发环境里残留已废弃的包（例如为了对照而没卸掉的",
        "`pymupdf`），它会出现在清单里。发布前请在干净的 venv（只装 backend/requirements.txt）",
        "里重新生成，或手工删除这些行。",
        "",
    ]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    # [EN] ``newline=""`` keeps the generated Markdown at LF on Windows: ``.gitattributes``
    # [EN] declares ``*.md text eol=lf``, and the default text mode would otherwise write
    # [EN] CRLF and make the line-ending gate fail right after regeneration.
    # [ZH] ``newline=""`` 让生成的 Markdown 在 Windows 上仍以 LF 落盘：``.gitattributes``
    # [ZH] 规定 ``*.md text eol=lf``，默认文本模式会写成 CRLF，刚重新生成就会被
    # [ZH] 换行符门禁判红。
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines))
    print(f"已生成 {os.path.normpath(OUT_PATH)}（{len(rows)} 个包，高风险 {len(hits)} 个）")


if __name__ == "__main__":
    main()
