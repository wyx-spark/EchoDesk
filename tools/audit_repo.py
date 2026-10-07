#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库双语与三平台友好度审计 / 门禁（v0.2.2 全面双语与三平台友好 任务一、任务八）。

**为什么要有这个工具**：任务书要求"扫描所有未被 `.gitignore` 忽略的文件，逐一判断是否需要
双语与三平台适配"。252 个被跟踪文件靠人眼过一遍，结论既不可复核也会随提交漂移。
这里把"审计"变成**可重复执行、可进 CI 的东西**：审计报告由脚本生成，门禁断言由脚本执行。

两条命令：

    python tools/audit_repo.py --report            # 生成审计清单（写入 records/v0.2.2-审计清单.md）
    python tools/audit_repo.py --check             # 门禁：断言双语成对性、文首互指、脚本等价物登记

**作用域与一个实测教训**：审计对象是 `git ls-files` 的**被跟踪文件**（任务书就是这么要求的），
所以**未跟踪的新文件不在射程内** —— 做完反证测试才发现：往 `tools/` 放一个未登记的新 `.ps1`
时门禁照样通过；`git add` 之后才报出来。CI 上跑的是已提交代码，因此门禁在 CI 里有效；
但在本地"刚写完还没 add"时它不会提醒你。要知道这一点，别把它当成万能检查。

**启发式的边界（必须说清楚，免得把"候选"当成"判决"）**：

* 「三平台友好」是按**关键词**猜的：出现 `powershell`/`.ps1`/`C:\\`/`%APPDATA%` 记一次 Windows
  倾向，出现 `#!/bin/sh`/`chmod`/`~/`/`export` 记一次 POSIX 倾向。它只能回答
  "这个文件里有没有平台专属痕迹、有没有同时提到三平台"，**不能**回答"这段说明在 macOS 上对不对"。
  因此报告里给的是分类与线索，最终的"做了什么"由人补写。
* 「双语」对文档是**成对性**（有没有对应语种的同名文件）；对代码/脚本是**注释语言倾向**
  （注释行里 CJK 字符占比）。代码注释一律**不改逻辑**，这一项只用于统计与决策。

与任务书的偏差：任务书说 `records/*.md` 与 `prompts/*.md` 不要求双语 ——
本工具据此把它们排除在语言要求之外（`prompts/` 本身还在 `.gitignore` 里，不进被跟踪文件）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REPORT = os.path.join(ROOT, "records", "v0.2.2-审计清单.md")

#: 任务书表格里的类型 → 判定规则（顺序即优先级，用 re.search 而非 re.match ——
#: 用 match 时 `\.(ico|png)$` 这类"不带 ^ 的模式"永远匹配不上，实测把 assets/ 全漏进了「其它」）。
CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    ("合规文档", r"(^|/)(LICENSE|NOTICE)(\.zh-Hans)?(\.md)?$"
                r"|(^|/)(SECURITY|CODE_OF_CONDUCT)(\.zh-Hans)?\.md$"
                r"|^docs/(EULA|PRIVACY|THIRD_PARTY_LICENSES|ENCRYPTED_DOCUMENTS)(\.zh-Hans)?\.md$"),
    ("仓库级文件", r"^(README|CHANGELOG|CONTRIBUTING)(\.zh-Hans)?\.md$|^USAGE\.txt$"
                  r"|^\.gitignore$|^\.gitattributes$"),
    ("面向用户的文档", r"^docs/.*\.md$|^assets/.*\.md$|^examples/.*/README\.md$|^deploy/.*\.md$"),
    ("CI/工作流", r"^\.github/"),
    # 语言包是"成对资源"，既不是文档也不是代码：按语种文件成对存在，单独一类更好审。
    ("语言包", r"(^|/)lang/.*\.json$|/Resources/Lang/.*\.json$"),
    ("配置示例", r"^config\.example\.json$|^(deploy|examples)/.*\.json$"),
    ("配置与构建描述", r"^backend/(config\.json|requirements\.txt|EchoBackend\.spec)$"
                     r"|\.(csproj|iss|manifest)$|^docs/SBOM\.json$"),
    ("示例代码", r"^examples/.*\.(py|cs)$"),
    ("记录文档", r"^records/"),
    ("任务书", r"^prompts/"),
    ("二进制/资源", r"\.(ico|png|icns|gguf|dll|exe|zip|gz)$"),
    ("前端代码", r"^frontend/.*\.(cs|xaml)$|^frontend-avalonia/.*\.(cs|axaml)$"),
    ("后端代码", r"^backend/.*\.py$"),
    ("脚本", r"^tools/.*\.(ps1|py|bat|sh|cmd)$"),
    ("其它", r".*"),
)

#: 出现即说明"这个文件里有 Windows 专属痕迹"。
WINDOWS_HINTS = (r"powershell", r"\.ps1\b", r"[A-Z]:\\", r"%APPDATA%", r"%USERPROFILE%", r"%LOCALAPPDATA%",
                 r"\bpy -3", r"\.exe\b", r"cmd /c", r"setx\b", r"New-Item", r"Write-Host", r"\bISCC\b",
                 r"\bwinget\b", r"\bMSBuild\b", r"注册表", r"开始菜单", r"桌面快捷方式")
#: 出现即说明"有 POSIX 痕迹"。
POSIX_HINTS = (r"#!/bin/sh", r"#!/bin/bash", r"chmod \+x", r"\bexport \w+=", r"~/\.", r"\$HOME",
               r"\bapt(-get)? install", r"\bbrew install", r"\bxattr\b", r"tar -xzf", r"\bchmod\b",
               r"/usr/bin/", r"\.sh\b")

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
#: 注释行的粗略识别（Python / C# / XAML / shell / PowerShell / JSON 注释）。
COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*|<!--|;|rem\b|REM\b)")

#: **真正**的平台专属内容（不是"提到过 Windows"）。只有命中这些才值得要求补三平台示例：
#: 驱动器盘符路径、Windows 环境变量、PowerShell 命令、Windows 专属构建工具。
PLATFORM_SPECIFIC = (
    r"[A-Za-z]:\\\\", r"[A-Za-z]:\\", r"%APPDATA%", r"%USERPROFILE%", r"%LOCALAPPDATA%",
    r"\bpowershell\b", r"\.ps1\b", r"\bpy -3\b", r"\bwinget\b", r"\bISCC\b", r"\bcmd /c\b",
    r"\bshell:startup\b", r"Start-EchoDesk",
)

#: 判定为「单文件双语」的清单 → 理由。任务书要求成对（英文默认 + `.zh-Hans.md`），
#: 但这几个文件**成对反而不合适**（GitHub 模板只认固定文件名；指针文件拆两份会违背
#: "策略只留一处"的初衷）。在这里登记 = 明确判断过，不是漏了。
SINGLE_FILE_BILINGUAL: dict[str, str] = {
    ".github/PULL_REQUEST_TEMPLATE.md": "GitHub 只认固定文件名，无法提供 .zh-Hans 变体；文件内中英并排",
    ".github/ISSUE_TEMPLATE/bug_report.yml": "同上（issue 模板文件名固定）",
    ".github/ISSUE_TEMPLATE/feature_request.yml": "同上",
    ".github/ISSUE_TEMPLATE/skill_submission.yml": "同上",
    ".github/release_notes_template.md": "发布说明模板，中英两半同在一个文件里（Release 正文本身也是双语的）",
    "assets/screenshots/README.md": "目录使用约定，中英并排；拆两份会让「该放哪些图」出现两个版本",
    "docs/SECURITY.md": "指针文件：策略正文只保留在根目录两个文件里，本文件中英并排说明「去哪里读」",
    "deploy/linux/README.md": "Linux 安装说明，一屏长度，中英并排比拆两份更好对照",
    "assets/icons/README.md": "生成物说明，中英并排（图标由工具产出，改源 ico 后要重跑）",
}

#: 需要成对的语言的类别（其余类别不要求成对）。
PAIRING_REQUIRED = ("面向用户的文档", "合规文档", "仓库级文件")

#: 只有这些类别才要求"补三平台命令/路径示例"（任务二）。
#: 源码与脚本不在此列：任务三对它们的要求是「注明适用平台」，任务四的要求是「跨平台等价物」，
#: 三者是不同的事，混在一起会把 200 个文件都算成待办（实测噪音极大）。
DOC_CATEGORIES_FOR_PLATFORM = ("面向用户的文档", "仓库级文件", "合规文档",
                               "CI/工作流", "配置示例", "配置与构建描述")

#: 平台相关**源码**的判定（任务三：这些文件应当注明适用平台）。
PLATFORM_DEPENDENT_CODE = (
    r"win32com", r"win32api", r"pythoncom", r"winreg", r"msvcrt", r"%APPDATA%", r"ShortcutService",
    r"System\.Windows", r"PresentationFramework", r"\.ps1\b", r"playwright", r"msedge",
    r"Application Support", r"XDG_DATA_HOME", r"XDG_CONFIG_HOME", r"sys\.platform",
    r"RuntimeInformation", r"OperatingSystem\.Is", r"PlatformKind", r"SupportsAutomation",
    r"SupportsWpsCom",
)

#: 「这个文件已经写明了自己只在某平台成立」的判据（中英都算）。
PLATFORM_NOTE_HINTS = (
    r"Windows only", r"POSIX only", r"macOS only", r"Linux only", r"仅 Windows", r"只(在|有) Windows",
    r"Windows 专属", r"平台专属", r"仅 POSIX", r"非 Windows", r"off Windows", r"non-Windows",
    r"适用平台", r"平台限制", r"platform limitation", r"platform-specific",
)

#: **工程级**平台声明：整个目录都只能在某一个平台上构建/运行，那么要求其中每个文件
#: 各自写一行 `Windows only` 只是仪式感 —— 30 个 WPF 文件里那 30 行不会让任何人更快理解，
#: 反而把真正需要标注的分支逻辑淹没在噪音里。
#: 正确做法是"在入口处声明一次 + 让读者一眼看到"，这张表记录的就是那一次声明落在哪里。
PROJECT_PLATFORM_NOTES: dict[str, str] = {
    "frontend/": "Windows 专属（WPF，目标框架 net10.0-windows）—— 声明写在 frontend/EchoDesk.csproj 的文件头与 docs/*/CROSS_PLATFORM.md 第一节",
}


def tracked_files() -> list[str]:
    """`git ls-files -z` 取全部被跟踪文件。

    **必须用 `-z`**（原因实测踩到）：默认输出会对非 ASCII 路径做 C 风格转义，
    中文文件名会变成 `records/v0.2.1-/344/277/256/…` 这样的八进制串，
    于是分类、成对性判断、报告全部错位。`-z` 用 NUL 分隔且不转义。
    """
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True)
    text = out.stdout.decode("utf-8", errors="replace")
    return [item.replace("\\", "/") for item in text.split("\0") if item.strip()]


def categorize(path: str) -> str:
    for name, pattern in CATEGORY_RULES:
        if re.search(pattern, path):
            return name
    return "其它"


def read_text(path: str, limit: int = 400_000) -> str:
    full = os.path.join(ROOT, path)
    try:
        with io.open(full, encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def language_traits(text: str) -> tuple[int, int]:
    """返回 (注释行数, 注释行里含中文的行数)。"""
    comments = cjk_comments = 0
    for line in text.splitlines():
        if not COMMENT_LINE.match(line):
            continue
        comments += 1
        if CJK.search(line):
            cjk_comments += 1
    return comments, cjk_comments


def platform_traits(text: str) -> tuple[int, int, bool, bool, bool]:
    """返回 (Windows 命中数, POSIX 命中数, 提到 Windows, 提到 macOS, 提到 Linux)。"""
    win = sum(1 for p in WINDOWS_HINTS if re.search(p, text, re.IGNORECASE))
    posix = sum(1 for p in POSIX_HINTS if re.search(p, text))
    mentions_win = bool(re.search(r"Windows|windows|win32|win-x64", text))
    mentions_mac = bool(re.search(r"macOS|Mac OS|macos|osx|Darwin|苹果", text))
    mentions_linux = bool(re.search(r"Linux|linux|manylinux|Ubuntu|Debian", text))
    return win, posix, mentions_win, mentions_mac, mentions_linux


def doc_group(path: str) -> str:
    """把一对双语文档归到同一个"基名"，用于判定成对性。

    支持三种布局：
      * `X.md` ↔ `X.zh-Hans.md`
      * `docs/en/X.md` ↔ `docs/zh-Hans/X.md`
      * **无扩展名**的合规文件：`LICENSE` ↔ `LICENSE.zh-Hans.md`（实测漏判过 ——
        只剥 `.zh-Hans.md` 会得到 `LICENSE` 与 `LICENSE.md` 两个不同的基名，
        于是一对完整成对的文件被报成"两个单语"）
    """
    p = re.sub(r"\.zh-Hans(\.md)?$", "", path)
    p = re.sub(r"\.md$", "", p)
    p = re.sub(r"^docs/en/", "docs/", p)
    p = re.sub(r"^docs/zh-Hans/", "docs/", p)
    return p


def build_audit() -> list[dict]:
    files = tracked_files()
    docs: list[dict] = []
    index: dict[str, dict] = {}
    for path in files:
        category = categorize(path)
        text = read_text(path)
        comments, cjk_comments = language_traits(text)
        win, posix, m_win, m_mac, m_linux = platform_traits(text)
        entry = {
            "path": path,
            "category": category,
            "bytes": os.path.getsize(os.path.join(ROOT, path)) if os.path.exists(os.path.join(ROOT, path)) else 0,
            "lang": "zh-Hans" if path.endswith(".zh-Hans.md") else ("zh-Hans" if path.startswith("docs/zh-Hans/") else "en"),
            "comment_lines": comments,
            "comment_lines_cjk": cjk_comments,
            "windows_hints": win,
            "posix_hints": posix,
            "mentions_windows": m_win,
            "mentions_macos": m_mac,
            "mentions_linux": m_linux,
            "platform_specific": bool(re.search("|".join(PLATFORM_SPECIFIC), text, re.IGNORECASE)),
            "platform_dependent_code": bool(re.search("|".join(PLATFORM_DEPENDENT_CODE), text)),
            "has_platform_note": bool(re.search("|".join(PLATFORM_NOTE_HINTS), text, re.IGNORECASE)),
        }
        docs.append(entry)
        index[path] = entry

    # 成对性
    groups: dict[str, list[str]] = {}
    for entry in docs:
        # 无扩展名的合规文件（LICENSE / NOTICE）同样要参与分组 ——
        # 只收 `.md` 会把它们排除在外，于是一对完整成对的文件被报成两个"单语"（实测漏判）。
        if entry["path"].endswith(".md") or re.search(r"(^|/)(LICENSE|NOTICE)$", entry["path"]):
            groups.setdefault(doc_group(entry["path"]), []).append(entry["path"])
    for base, members in groups.items():
        for member in members:
            entry = index[member]
            entry["pair_base"] = base
            entry["pair_members"] = members
            if len(members) > 1:
                entry["bilingual"] = "成对"
            elif member in SINGLE_FILE_BILINGUAL:
                entry["bilingual"] = "单文件双语"
            else:
                entry["bilingual"] = "单语-zh" if entry["lang"] == "zh-Hans" else "单语-en"
    for entry in docs:
        if "bilingual" not in entry:
            if entry["comment_lines"]:
                ratio = entry["comment_lines_cjk"] / entry["comment_lines"]
                entry["bilingual"] = ("注释中文" if ratio > 0.6
                                      else "注释英文" if ratio < 0.1
                                      else "注释中英混用")
            else:
                entry["bilingual"] = "不适用"
        if entry["category"] in ("记录文档", "任务书"):
            entry["bilingual"] = "不适用（内部文档）"

    # 三平台友好度：只看"有没有痕迹 + 有没有同时讲三平台"
    for entry in docs:
        if entry["category"] in ("记录文档", "任务书", "二进制/资源", "后端代码", "前端代码"):
            entry["platform"] = "不适用"
            continue
        win, posix = entry["windows_hints"], entry["posix_hints"]
        three = entry["mentions_windows"] and entry["mentions_macos"] and entry["mentions_linux"]
        if three:
            entry["platform"] = "覆盖三平台"
        elif win and posix:
            entry["platform"] = "Win+POSIX 混用"
        elif win:
            entry["platform"] = "仅 Windows"
        elif posix:
            entry["platform"] = "仅 POSIX"
        else:
            entry["platform"] = "平台中立"
    return docs


def summarize(docs: list[dict]) -> dict:
    out: dict = {"total": len(docs), "by_category": {}, "needs_work": []}
    for entry in docs:
        bucket = out["by_category"].setdefault(entry["category"], {"count": 0, "bilingual": {}, "platform": {}})
        bucket["count"] += 1
        bucket["bilingual"][entry["bilingual"]] = bucket["bilingual"].get(entry["bilingual"], 0) + 1
        bucket["platform"][entry["platform"]] = bucket["platform"].get(entry["platform"], 0) + 1
    for entry in docs:
        if entry["category"] in ("记录文档", "任务书"):
            continue
        todo = ""
        if entry["bilingual"].startswith("单语") and entry["category"] in PAIRING_REQUIRED:
            todo += "补另一语；"
        # 只有**文档类**且**真的含平台专属内容**（盘符路径、Windows 环境变量、PowerShell 命令…）
        # 且通篇没讲 macOS/Linux 的，才要求补三平台示例。
        # 源码/脚本另有任务三、任务四的口径，见下面 summarize 的单独小节。
        if (entry["platform_specific"] and entry["category"] in DOC_CATEGORIES_FOR_PLATFORM
                and not (entry["mentions_macos"] and entry["mentions_linux"])):
            todo += "补三平台命令/路径示例；"
        if todo:
            out["needs_work"].append({"path": entry["path"], "category": entry["category"],
                                      "todo": todo.rstrip("；")})

    # 任务三专用小清单：平台相关源码里，还没有写明"适用平台"的那些。
    # 工程级声明覆盖的文件不算"没写明"（见 PROJECT_PLATFORM_NOTES 的理由）。
    def covered_by_project_note(path: str) -> bool:
        return any(path.startswith(prefix) for prefix in PROJECT_PLATFORM_NOTES)

    out["platform_code_without_note"] = [
        {"path": e["path"], "category": e["category"]}
        for e in docs
        if e["platform_dependent_code"] and not e["has_platform_note"]
        and not covered_by_project_note(e["path"])
        and e["category"] in ("后端代码", "前端代码", "脚本")
    ]
    out["platform_code_with_note"] = [
        {"path": e["path"], "category": e["category"]}
        for e in docs
        if e["platform_dependent_code"] and (e["has_platform_note"] or covered_by_project_note(e["path"]))
        and e["category"] in ("后端代码", "前端代码", "脚本")
    ]
    out["project_platform_notes"] = PROJECT_PLATFORM_NOTES
    return out


def write_report(docs: list[dict], path: str) -> None:
    stats = summarize(docs)
    lines: list[str] = []
    add = lines.append
    add("# v0.2.2 审计清单：双语与三平台友好度")
    add("")
    add("> **本文件由 `python tools/audit_repo.py --report` 生成，不要手工编辑。**")
    add("> 数据来源是 `git ls-files`（被跟踪文件就是「未被 .gitignore 忽略」的权威集合）。")
    add("> 判定口径与启发式的边界见该脚本的模块 docstring —— 它是**线索**，不是判决；")
    add("> 「做了什么」一栏由人工在 `records/v0.2.2-全面双语与三平台友好记录.md` 中补写。")
    add("")
    add(f"生成时间：{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}"
        f"　被跟踪文件：**{stats['total']}** 个　需要动工：**{len(stats['needs_work'])}** 项")
    add("")
    add("## 一、按类型汇总")
    add("")
    add("| 类型 | 文件数 | 双语状态分布 | 三平台状态分布 |")
    add("| :--- | ---: | :--- | :--- |")
    for name, bucket in sorted(stats["by_category"].items(), key=lambda kv: -kv[1]["count"]):
        bi = "、".join(f"{k} {v}" for k, v in sorted(bucket["bilingual"].items(), key=lambda kv: -kv[1]))
        pl = "、".join(f"{k} {v}" for k, v in sorted(bucket["platform"].items(), key=lambda kv: -kv[1]))
        add(f"| {name} | {bucket['count']} | {bi} | {pl} |")
    add("")
    add("## 二、逐文件审计")
    add("")
    for name, bucket in sorted(stats["by_category"].items(), key=lambda kv: -kv[1]["count"]):
        members = [d for d in docs if d["category"] == name]
        add(f"### {name}（{len(members)}）")
        add("")
        add("| 文件 | 双语状态 | 三平台状态 | 备注 |")
        add("| :--- | :--- | :--- | :--- |")
        for entry in sorted(members, key=lambda d: d["path"]):
            note = ""
            if entry["platform"] == "不适用" and entry["category"] in ("后端代码", "前端代码"):
                if entry["comment_lines"]:
                    note = f"注释 {entry['comment_lines']} 行，含中文 {entry['comment_lines_cjk']} 行"
            elif entry["platform"] != "不适用":
                marks = []
                if entry["mentions_windows"]:
                    marks.append("W")
                if entry["mentions_macos"]:
                    marks.append("M")
                if entry["mentions_linux"]:
                    marks.append("L")
                note = ("提到 " + "/".join(marks)) if marks else "未提到任何平台"
                if entry["windows_hints"] or entry["posix_hints"]:
                    note += f"；痕迹 W{entry['windows_hints']}/P{entry['posix_hints']}"
            add(f"| `{entry['path']}` | {entry['bilingual']} | {entry['platform']} | {note} |")
        add("")
    add("## 三、需要动工的项（判定为「线索」级别，需人工确认）")
    add("")
    if not stats["needs_work"]:
        add("（无）")
    else:
        add("| 文件 | 类型 | 建议动作 |")
        add("| :--- | :--- | :--- |")
        for item in stats["needs_work"]:
            add(f"| `{item['path']}` | {item['category']} | {item['todo']} |")
    add("")
    add("## 四、平台相关源码的平台标注情况（任务三）")
    add("")
    add("> 判定：源码/脚本里引用了平台专属 API 或路径（`win32com`、`System.Windows`、")
    add("> `Application Support`、`XDG_DATA_HOME`、`playwright`、`sys.platform`…），")
    add("> 但文件里没有任何「Windows only / 仅 Windows / 适用平台」之类的说明。")
    add("")
    no_note = stats["platform_code_without_note"]
    with_note = stats["platform_code_with_note"]
    add(f"- 已写明适用平台：**{len(with_note)}** 个")
    add(f"- **尚未写明：{len(no_note)} 个**")
    add("")
    if no_note:
        add("| 文件 | 类型 |")
        add("| :--- | :--- |")
        for item in no_note:
            add(f"| `{item['path']}` | {item['category']} |")
        add("")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"审计清单已写入 {os.path.relpath(path, ROOT)}（{len(lines)} 行）")
    print(f"  被跟踪文件 {stats['total']} 个；需要动工 {len(stats['needs_work'])} 项")
    for name, bucket in sorted(stats["by_category"].items(), key=lambda kv: -kv[1]["count"]):
        print(f"    {name:<16} {bucket['count']:>3} 个　双语：{bucket['bilingual']}")


# ---------------------------------------------------------------------------
# 门禁模式
# ---------------------------------------------------------------------------
#: 允许保持"只有 .ps1"的脚本 → 理由。任务书 4.2 要求逐一评估跨平台等价物，
#: 这张表就是评估结论：**在这里登记 = 明确判断过"不需要 .sh/.py 等价物"**，
#: 没登记又只有 .ps1 的会被门禁点出来（防止新增脚本时忘了评估）。
PS1_ALLOWLIST: dict[str, str] = {
    "tools/build_release.ps1": "Windows 发布链（PyInstaller + rcedit + Inno Setup），三平台各自有别的打包脚本",
    "tools/make_installer.ps1": "调用 Inno Setup 编译器（Windows 专属工具）",
    "tools/check_installer_deliverables.ps1": "验收 Inno Setup 安装包（Windows 专属产物）",
    "tools/check_ui_pixels.ps1": "需要 WPF 窗口与交互式桌面（Windows 专属）",
    "tools/ui_probe.ps1": "WPF UI 自动化探针（Windows 专属）",
    "tools/ui_probe_avalonia.ps1": "Avalonia 界面探针的判定脚本 —— **本体跨平台**（被测程序三平台都能跑），"
                                  "但按本仓库约定门禁脚本走 PowerShell（Windows 发布链）；"
                                  "非 Windows 上可直接跑 `app/EchoDesk --probe` 再用同样的结论行判定",
    "tools/ui_shot.ps1": "WPF 窗口截图（Windows 专属）",
    "tools/check_emoji.ps1": "用 WPF GlyphTypeface 查字体链（Windows 专属能力）",
    "tools/check_docs_links.ps1": "**应补 .py 等价物** —— 见未完成项；本版先登记",
    "tools/check_feedback.ps1": "**应补 .py 等价物** —— 见未完成项；本版先登记",
    "tools/check_git_ssh.ps1": "**应补 .py 等价物** —— 见未完成项；本版先登记",
    "tools/push_release.ps1": "推送要 SSH 私钥，而三平台都可能有私钥 → 见未完成项",
    "tools/verify_release.ps1": "顶层编排，内含 Windows 专属项（安装包/UI 探针）",
}


def check() -> int:
    """门禁：只断言**没有别的门禁在管**的三件事。

    **刻意不重复 `tools/check_lang.py` 的活**（第一版犯过这个错，必须写下来）：
    `check_lang.py` 已经在查 `docs/` 下成对文档的章节数量/编号序列、中文版「以英文版为准」
    声明、英文版给出中文版路径，还带着一张写明原因的已知偏差表（`DECLARATION_WARNINGS`）。
    本脚本第一版又按自己的规则查了一遍标题层级，结果一次报出 18 项"问题"，
    其中**没有一项是真缺陷** —— 全是"我的规则 ≠ 项目既定规则"（法律文本不该加译者声明、
    指针式文档本来就与正文结构不同…）。两个门禁对同一件事各有一套口径，只会制造噪音和
    互相打架的修复方向。所以这里只保留三件确实没人管的事：

    1. **根目录的成对文件**（`check_lang` 只扫 `docs/`，根目录不在它的射程内）；
    2. **仓库内嵌套的说明文档**（`examples/`、`assets/`、`deploy/` 下的 README）；
    3. **`tools/` 下 `.ps1` 的跨平台等价物评估**（任务书 4.2 的登记表）。
    """
    docs = build_audit()
    problems: list[str] = []

    # 1) 根目录成对文件 + 嵌套说明文档：要么成对，要么登记为"单文件双语"
    for entry in docs:
        if entry["category"] not in ("仓库级文件", "合规文档", "面向用户的文档", "配置与构建描述"):
            continue
        if not entry["bilingual"].startswith("单语"):
            continue
        # 只关心"仓库级/嵌套"的说明文档；docs/ 下的交给 check_lang.py
        if entry["path"].startswith("docs/"):
            continue
        problems.append(f"说明文档缺少另一半（或未登记为单文件双语）：{entry['path']}"
                        f"（{entry['bilingual']}）")

    # 2) 根目录成对文件的互指声明（法律文本豁免：LICENSE / NOTICE 不加译者声明）
    for entry in docs:
        path = entry["path"]
        if not (entry.get("pair_members") and len(entry["pair_members"]) > 1):
            continue
        if path.startswith("docs/") or path.startswith("assets/") or path.startswith("examples/"):
            continue
        if re.search(r"(^|/)(LICENSE|NOTICE)(\.zh-Hans)?(\.md)?$", path):
            continue
        text = read_text(path, 4000)
        if entry["lang"] == "zh-Hans":
            if "以英文版为准" not in text:
                problems.append(f"根目录中文版缺少「以英文版为准」声明：{path}")
        else:
            if not re.search(r"zh-Hans|中文", text[:4000]):
                problems.append(f"根目录英文版缺少中文版指引：{path}")

    # 3) tools/ 下的 .ps1 必须登记在 PS1_ALLOWLIST（= 评估过跨平台等价物）
    ps1 = [e for e in docs if e["path"].endswith(".ps1")]
    for entry in ps1:
        if entry["path"] not in PS1_ALLOWLIST:
            problems.append(f"新增的 PowerShell 脚本未登记跨平台等价物评估：{entry['path']}")

    # 4) .bat 的编码安全
    #
    # **这条规则是拿真实事故换来的**（v0.2.2 全面双语与三平台友好）：
    # 我给 `tools/set_metadata.bat` 在文件最前面加了一行中文 `rem`，而该文件是
    # `@echo off` → `chcp 65001` 才切到 UTF-8 的。于是 cmd.exe 用**传统代码页**（简中 Windows 上
    # 是 GBK）解析那行 UTF-8 中文，多字节序列把行尾吃掉，后面的内容被当成命令执行，
    # **发布构建直接失败**：`'get' is not recognized as an internal or external command`。
    # 规则就是那时总结出来的：**非 ASCII 不得出现在代码页切换之前**（没有 chcp 行则整个文件都在风险下）。
    bat = [e for e in docs if e["path"].endswith(".bat")]
    for entry in bat:
        path = os.path.join(ROOT, entry["path"])
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            raw = handle.read()
        lines = raw.split(b"\n")
        switch = None
        for index, line in enumerate(lines):
            if b"chcp" in line and b"65001" in line:
                switch = index
                break
        limit = switch if switch is not None else len(lines)
        offenders = [i + 1 for i, line in enumerate(lines[:limit])
                     if any(byte > 0x7F for byte in line)]
        if offenders:
            where = "chcp 65001 之前" if switch is not None else "整个文件（没有 chcp 65001 行）"
            problems.append(
                f"{entry['path']} 的非 ASCII 文本出现在{where}（第 {offenders[:5]} 行）："
                "cmd.exe 会按传统代码页解析这些字节，可能吃掉行尾并把后续内容当成命令执行 —— "
                "把 chcp 65001 >nul 提到最前，或把这些行改成 ASCII")

    print("== 仓库双语与三平台友好度门禁 ==")
    print(f"被跟踪文件 {len(docs)} 个；.ps1 {len(ps1)} 个（登记 {len(PS1_ALLOWLIST)} 个）；.bat {len(bat)} 个")
    print("说明：docs/ 下成对文档的结构与文首声明由 tools/check_lang.py 负责，本门禁不重复检查。")
    if problems:
        print(f"\n发现问题 {len(problems)} 项：")
        for item in problems[:40]:
            print(f"  - {item}")
        if len(problems) > 40:
            print(f"  …… 另有 {len(problems) - 40} 项")
        return 1
    print("\n全部通过。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="仓库双语与三平台友好度审计 / 门禁")
    parser.add_argument("--report", nargs="?", const=DEFAULT_REPORT, default="",
                        help=f"生成审计清单（默认 {os.path.relpath(DEFAULT_REPORT, ROOT)}）")
    parser.add_argument("--check", action="store_true", help="门禁模式")
    parser.add_argument("--json", default="", help="把审计数据写成 JSON（供其它脚本消费）")
    args = parser.parse_args()

    if args.check:
        return check()
    docs = build_audit()
    if args.json:
        with io.open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(docs, handle, ensure_ascii=False, indent=2)
        print(f"审计数据已写入 {os.path.relpath(args.json, ROOT)}")
    if args.report or not args.json:
        write_report(docs, args.report or DEFAULT_REPORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
