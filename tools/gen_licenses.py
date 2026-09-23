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
from importlib.metadata import distributions
from typing import Dict, List, Tuple

# 后端实际依赖的顶层包（与 backend/requirements.txt 对应，含传递依赖）
#
# v0.2.1（任务书 1.3）变更：
#   * 移除 pymupdf —— 它是 AGPL-3.0，对外分发会产生著佐权义务，已换成 BSD 的 pypdf；
#   * pyxtxt 在 v0.2.0 就已移除（其依赖链含 GPL/LGPL：extract-msg、pcodedmp、
#     RTFDE、python-bidi、pygame、pynput），这里不再列出；
#   * 新增 pypdf 与文件解析相关包（python-docx / openpyxl / python-pptx / xlrd），
#     它们此前虽被使用但漏登记在清单里。
ROOT_PACKAGES = [
    "llama-cpp-python",
    "duckduckgo-search",
    "pydantic",
    "huggingface_hub",
    "requests",
    "pywin32",
    "pyinstaller",
    "playwright",
    "schedule",
    "pypdf",
    "python-docx",
    "openpyxl",
    "python-pptx",
    "xlrd",
    "tqdm",
]

OUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "docs", "THIRD_PARTY_LICENSES.md"
)

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
             "从 52.9MB 降到约 1.4MB（见 docs/v0.2.1-研发记录.md）。",
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
        "## 一、后端直接依赖",
        "",
        "| 组件 | 版本 | 许可证 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for dist in distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if name.lower() not in {p.lower() for p in ROOT_PACKAGES}:
            continue
        lic = _license_of(dist)
        note = RISKY.get(lic.split()[0].upper(), "") if lic else ""
        lines.append(f"| {name} | {dist.version} | {lic} | {note} |")

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
        "- llama.cpp（llama-cpp-python 内嵌的 llama.dll / ggml 系列）：MIT 许可。",
        "- Playwright 驱动：Apache-2.0；其驱动的浏览器（Microsoft Edge）由用户系统安装，",
        "  EchoDesk 不重新分发浏览器本体。",
        "",
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
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"已生成 {os.path.normpath(OUT_PATH)}（{len(rows)} 个包，高风险 {len(hits)} 个）")


if __name__ == "__main__":
    main()
