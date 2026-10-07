# -*- coding: utf-8 -*-
"""语言包完整性检查（任务书 任务三 3.5）。

用途
----
* 对比各语言包与**源语言**（简体中文）的键集合，报告缺失键与多余键；
* 扫描代码里实际用到的键（后端 ``t("...")`` / ``api.lang("...")``，
  前端 ``Loc.T("...")`` / ``Loc.Key="..."``），报告"代码用了但语言包里没有"的键；
* 检查占位符是否一致（``{0}`` / ``{name}`` 这类，翻译时最容易漏）；
* ``--scaffold <locale>`` 可以为新语言生成模板文件，供社区翻译。

退出码：有缺失键或多义占位符时返回 1（供 CI 的 lang-check 步骤使用）。

用法
----
    python tools/check_lang.py                    # 检查全部语言
    python tools/check_lang.py --locale en        # 只检查英文
    python tools/check_lang.py --scaffold ja      # 生成日语模板
    python tools/check_lang.py --quiet            # 只在有问题时输出
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_LANG = os.path.join(ROOT, "backend", "lang")
FRONTEND_LANG = os.path.join(ROOT, "frontend", "Resources", "Lang")

#: 源语言（所有键的权威来源）
SOURCE_LOCALE = "zh-Hans"

#: 中文数字 → 整数。用来比较"两版文档的章节编号序列"：
#: 英文版常写 `## 3. Title`，中文版可能写 `## 三、标题`，编号体系不同但顺序必须一致。
CN_NUMERALS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15, "十六": 16, "十七": 17,
    "十八": 18, "十九": 19, "二十": 20,
}

#: 共用双语文档里，中文译本必须出现的"以英文版为准"声明片段
ZH_PREVAILS_MARKER = "以英文版为准"

#: 声明检查的**已知偏差**白名单（结构检查对它们照旧强制执行）。
#:
#: docs/THIRD_PARTY_LICENSES.md 由 tools/gen_licenses.py 生成，正文是**中文**，
#: 但按本仓库约定它应当是"英文默认版 + .zh-Hans.md 中文译本"。所以
#: 「英文版给出中文译本的路径」这条在它身上必然不成立 —— 它是中文写的中文版，
#: 而那个 .zh-Hans.md 只是按文件名约定另外写的中文译本，两者内容基本重合。
#: 彻底修法是让生成器输出英文默认版（要把整份清单与说明翻译一遍，属独立任务）。
#: 这里降级为警告，而不是用一条当前无法满足的规则把整条发布门禁卡死。
DECLARATION_WARNINGS = {"THIRD_PARTY_LICENSES.md"}

#: 需要在双语版之间**逐行核对数据表**的文档：英文默认版文件名 → 表头里必须出现的列名。
#:
#: 为什么只针对这一份、而不对所有共用双语文档一刀切：
#: 另外三对（EULA / PRIVACY / ENCRYPTED_DOCUMENTS）的表格单元格是**叙述文字**，
#: 译文与英文版本就不该逐字相同，逐行比对必然误报。
#: 而许可证清单的表格是**数据** —— 包名、版本号、许可证标识符都不随语言变化，
#: 逐行必须一致：少一行、或版本号写错，清单描述的组件就与实际分发的对不上了。
#:
#: 这个洞是 v0.2.1 收口时实测踩出来的：英文默认版已是 55 个包 / 17 条直接依赖，
#: 中文译本还停在 52 个 / 13 条 —— 缺 cffi / cryptography / pycparser 三行明细，
#: 直接依赖表还缺 cryptography / duckduckgo_search / llama_cpp_python / urllib3 四行。
#: 而当时这里只比章节结构（两份都是 5 个二级标题），行数差异一路放行。
DATA_TABLE_PAIRS = {"THIRD_PARTY_LICENSES.md": ("包名", "组件")}


def md_tables(text):
    """把 Markdown 文本切成一串表格：返回 ``[(表头单元格列表, [数据行单元格列表]), ...]``。

    只认连续以 ``|`` 开头的行，并用"整行都是 ``-``/``:``/空格"识别分隔行。
    """
    tables = []
    current = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if current is None:
                current = (cells, [])
                continue
            if all(set(cell) <= set("-: ") for cell in cells):
                continue                      # |---|---| 分隔行
            current[1].append(cells)
        elif current is not None:
            tables.append(current)
            current = None
    if current is not None:
        tables.append(current)
    return tables


def check_data_tables(en_name, en_text, zh_name, zh_text, header_words):
    """逐行核对双语版之间的数据表，返回 ``(问题数, 输出行列表)``。

    只比对"表头里出现了 ``header_words`` 之一"的表格（见 ``DATA_TABLE_PAIRS``）。
    **按表分别比对**，不把所有表合成一个集合 —— 许可证清单里有"直接依赖"与
    "全部发行包明细"两张表，包名大量重叠，合并后某张表整段缺失会被另一张表掩盖
    （实测：直接依赖表缺的 cryptography / duckduckgo_search / llama_cpp_python /
    urllib3 四行，因为明细表里有同名行而没被发现）。
    行首单元格（包名）比较时做 PEP 503 归一化 —— ``llama_cpp_python``
    与 ``llama-cpp-python`` 是同一个包，不该因为写法不同就报差异。
    """

    def collect(text):
        tables = {}
        for header, data in md_tables(text):
            if not any(any(word in cell for word in header_words) for cell in header):
                continue
            signature = " | ".join(cell.lower() for cell in header)
            rows = tables.setdefault(signature, {})
            for cells in data:
                if not cells or not cells[0]:
                    continue
                rows[cells[0].lower().replace("_", "-").replace(".", "-")] = cells
        return tables

    en_tables = collect(en_text)
    zh_tables = collect(zh_text)
    problems = 0
    lines = []
    total_rows = 0

    for signature in sorted(set(en_tables) | set(zh_tables)):
        en_rows = en_tables.get(signature, {})
        zh_rows = zh_tables.get(signature, {})
        total_rows += len(en_rows)
        label = f"数据表「{signature}」"

        only_en = sorted(set(en_rows) - set(zh_rows))
        only_zh = sorted(set(zh_rows) - set(en_rows))
        if only_en or only_zh:
            problems += 1
            # 报原始写法（表格里的包名），而不是内部用的归一化键，
            # 否则 `duckduckgo_search` 会被打印成 `duckduckgo-search`，读者对不上表格。
            def shown(key):
                return (en_rows.get(key) or zh_rows.get(key))[0]

            detail = []
            if only_en:
                detail.append(f"{zh_name} 缺 {len(only_en)} 行"
                              f"（{', '.join(shown(key) for key in only_en)}）")
            if only_zh:
                detail.append(f"{zh_name} 多出 {len(only_zh)} 行"
                              f"（{', '.join(shown(key) for key in only_zh)}）")
            lines.append(f"   ✗ {en_name} 与 {zh_name} 的{label}行不一致：{'；'.join(detail)}")

        changed = [
            (en_rows[key][0], en_rows[key][1:], zh_rows[key][1:])
            for key in sorted(set(en_rows) & set(zh_rows))
            if [cell.lower() for cell in en_rows[key][1:]]
            != [cell.lower() for cell in zh_rows[key][1:]]
        ]
        if changed:
            problems += 1
            lines.append(f"   ✗ {en_name} 与 {zh_name} 的{label}有 {len(changed)} 行内容不一致：")
            for name, en_cells, zh_cells in changed[:10]:
                lines.append(f"     - {name}: {en_name} 「{' | '.join(en_cells)}」 / "
                             f"{zh_name} 「{' | '.join(zh_cells)}」")

    if not problems:
        lines.append(f"   ✓ {en_name} ↔ {zh_name} 的数据表逐行一致"
                     f"（{len(en_tables)} 张表 / {total_rows} 行）")
    return problems, lines


def heading_ordinal(line):
    """从 '## 3. Foo' / '## 三、标题' 里取出章节序号；取不到返回 None。

    只认二级标题（`## `），且要求编号后紧跟 `.` 或 `、` —— 这样
    '## In this document' 这类无编号标题会返回 None，由调用方退化为"只比数量"。
    """
    match = re.match(r'^##\s*([0-9]+|[一二三四五六七八九十]+)\s*[.、]', line.strip())
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    return CN_NUMERALS.get(token)


def check_bilingual_pairs(docs_root):
    """检查 docs/ 下成对共用的双语文档（X.md + X.zh-Hans.md）结构是否对齐。

    为什么需要这个检查（v0.2.1 新增 ENCRYPTED_DOCUMENTS 时定下的规则）：
    "译文翻得对不对"脚本判断不了，但"结构有没有漂移"脚本完全判断得了 ——
    两版的章节数量或编号顺序一旦错位，读者按编号互相对照就会对不上，
    而这种漂移恰恰是后续增删章节时最容易发生的。
    另外还校验两版文首的互相声明（中文版"以英文版为准"、英文版给出中文版路径），
    这是本仓库所有共用双语文档的统一约定。
    对 ``DATA_TABLE_PAIRS`` 里列出的文档，还会逐行核对两版的**数据表**
    （内容不随语言变化，因此必须完全一致）。

    返回 (问题数, 输出行列表)。
    """
    problems = 0
    lines = []
    pairs = []
    for name in sorted(os.listdir(docs_root)):
        if not name.endswith(".md") or name.endswith(".zh-Hans.md"):
            continue
        zh_name = name[:-3] + ".zh-Hans.md"
        if os.path.isfile(os.path.join(docs_root, zh_name)):
            pairs.append((name, zh_name))
    if not pairs:
        lines.append("   未找到成对的双语文档")
        return problems, lines

    for en_name, zh_name in pairs:
        with io.open(os.path.join(docs_root, en_name), encoding="utf-8") as handle:
            en_text = handle.read()
        with io.open(os.path.join(docs_root, zh_name), encoding="utf-8") as handle:
            zh_text = handle.read()
        en_headings = [line for line in en_text.splitlines() if line.startswith("## ")]
        zh_headings = [line for line in zh_text.splitlines() if line.startswith("## ")]

        ok = True
        if len(en_headings) != len(zh_headings):
            problems += 1
            ok = False
            lines.append(f"   ✗ {en_name} 与 {zh_name} 的二级章节数量不一致："
                         f"{len(en_headings)} vs {len(zh_headings)}")
        else:
            en_ordinals = [heading_ordinal(line) for line in en_headings]
            zh_ordinals = [heading_ordinal(line) for line in zh_headings]
            # 只有在两侧**每一章**都带可识别编号时才比编号序列；
            # 否则（例如 PRIVACY.md 有 'In this document' 这类无编号标题）只比数量。
            if all(o is not None for o in en_ordinals) and all(o is not None for o in zh_ordinals):
                if en_ordinals != zh_ordinals:
                    problems += 1
                    ok = False
                    lines.append(f"   ✗ {en_name} 与 {zh_name} 的章节编号序列不一致："
                                 f"{en_ordinals} vs {zh_ordinals}")

        declaration_msg = ""
        if ZH_PREVAILS_MARKER not in zh_text:
            declaration_msg = f"{zh_name} 缺少「{ZH_PREVAILS_MARKER}」声明"
        elif zh_name not in en_text:
            declaration_msg = f"{en_name} 未给出中文译本的路径（找不到 {zh_name}）"

        if declaration_msg:
            if en_name in DECLARATION_WARNINGS:
                lines.append(f"   ! {en_name}：{declaration_msg}"
                             f"（已知偏差，见 DECLARATION_WARNINGS 注释）")
            else:
                problems += 1
                ok = False
                lines.append(f"   ✗ {declaration_msg}")

        table_info = []
        if en_name in DATA_TABLE_PAIRS:
            table_problems, table_info = check_data_tables(
                en_name, en_text, zh_name, zh_text, DATA_TABLE_PAIRS[en_name])
            if table_problems:
                problems += table_problems
                ok = False

        if ok:
            lines.append(f"   ✓ {en_name} ↔ {zh_name}（{len(en_headings)} 章，编号与声明一致）")
        lines.extend(table_info)
    return problems, lines

#: 代码里会被扫描的键引用形式
BACKEND_KEY_PATTERNS = (
    re.compile(r'\bt\(\s*"([A-Za-z0-9_./-]+)"'),
    re.compile(r'_t\(\s*"([A-Za-z0-9_./-]+)"'),
    re.compile(r'api\.lang\(\s*"([A-Za-z0-9_./-]+)"'),
    re.compile(r'api\.lang\(\s*"@?([A-Za-z0-9_./-]+)"'),
)
FRONTEND_KEY_PATTERNS = (
    re.compile(r'Loc\.T\(\s*"([A-Za-z0-9_./-]+)"'),
    re.compile(r'Loc\.Has\(\s*"([A-Za-z0-9_./-]+)"'),
    re.compile(r'(?:svc:)?Loc\.(?:Key|TipKey|HelpKey)="([A-Za-z0-9_./-]+)"'),
)

#: 明确允许"代码用到但语言包里没有"的键（一般是动态拼接的一部分前缀）
IGNORED_MISSING = frozenset({
    # 动态拼接的键前缀：代码里是 "mode." + key / "cmd.items." + name 这种写法，
    # 正则只能截到前缀本身，完整键由源码在运行期拼出来。
    "cmd.items", "cmd.items.",
    "mode.", "settings.automation.wizardStep", "settings.developer.hookStages.",
    "automation.help.", "automation.template.",
})

#: 脚手架模板里的键：backend/skills_runtime.py 生成的入口骨架里写了
#: api.lang("hello.reply", ...) 之类的示例，这些键属于"新插件自己的语言包"，
#: 不在主程序语言包里（骨架会同时生成对应的 lang/zh-Hans.json）。
SKELETON_KEYS = frozenset({"hello.default_name", "hello.reply", "panel.result"})

#: 占位符：{0} 与 {name} 两种写法都要检出
_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")


def flatten(node, prefix, out):
    """把嵌套语言包拍平成 a.b.c -> 文本（与 backend/i18n.py 的规则一致）。"""
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            if prefix:
                out[prefix] = node["text"]
            return
        for key, value in node.items():
            if not isinstance(key, str) or key.startswith(("_", "//", "$")):
                continue
            flatten(value, f"{prefix}.{key}" if prefix else key, out)
        return
    if isinstance(node, str) and prefix:
        out[prefix] = node


def _duplicate_paths(data):
    """找出"同一路径既是扁平点号键、又由嵌套产生"的键，返回 ``[(路径, 扁平值, 嵌套值)]``。

    为什么必须回到**原始 JSON**：``flatten`` 会把两种写法归并成同一条路径
    （后写的覆盖先写的），只看拍平结果永远发现不了重复。

    两种重复的严重程度不同，所以分开报：
      * **两边内容不同** → 真正的问题：改了一份、运行时读另一份，
        表现为"文案改了却不生效"（v0.2.1 能力扩展-1 实测踩到：`/agent` 的提示句
        写进了嵌套那份，运行时读扁平那份）。这类会**判失败**；
      * 两边内容相同 → 只是冗余（历史遗留），不影响行为，只报个数。
    """
    found = []
    if not isinstance(data, dict):
        return found
    for section, node in data.items():
        if not isinstance(node, dict):
            continue
        nested = {}
        stack = [(node, "")]
        while stack:
            current, prefix = stack.pop()
            for key, value in current.items():
                if not isinstance(key, str) or key.startswith(("_", "//", "$")):
                    continue
                # 含点的键是**扁平写法**，不再当路径展开 —— 否则"只有扁平键"
                # 会被自己匹配成重复（自检时抓到的假阳性）。
                if "." in key:
                    continue
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    stack.append((value, path))
                else:
                    nested[path] = value
        for key, value in node.items():
            if isinstance(key, str) and "." in key and key in nested:
                found.append((f"{section}.{key}", value, nested[key]))
    return found


def load_dir(directory):
    """读取一个语言目录：{locale: {key: text}}（主文件 + 模块分片合并）。"""
    packs = {}
    if not os.path.isdir(directory):
        return packs
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".json"):
            continue
        locale = name[:-5].split(".")[0]
        path = os.path.join(directory, name)
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  ✗ {path} 读取失败：{e}")
            continue
        flat = packs.setdefault(locale, {})
        flatten(data, "", flat)
    return packs


def collect_used_keys():
    """扫描代码里实际用到的语言键，返回 {键: 出现位置列表}。"""
    used = {}
    targets = []
    # 注意：不扫描 examples/skills —— 示例插件是独立的技能包，语言键属于它们
    # 自己的 lang/*.json，与主程序语言包无关（由 tools/test_skills_runtime.py 覆盖）。
    for base, patterns in ((os.path.join(ROOT, "backend"), BACKEND_KEY_PATTERNS),
                           (os.path.join(ROOT, "frontend"), FRONTEND_KEY_PATTERNS)):
        if not os.path.isdir(base):
            continue
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ("bin", "obj", "__pycache__", ".venv", "node_modules")]
            for name in files:
                if name.endswith((".py", ".cs", ".xaml")):
                    targets.append((os.path.join(current, name), patterns))
    for path, patterns in targets:
        try:
            with io.open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        rel = os.path.relpath(path, ROOT)
        for pattern in patterns:
            for match in pattern.finditer(text):
                key = match.group(1)
                used.setdefault(key, []).append(rel)
    return used


def placeholders(text):
    return sorted(set(_PLACEHOLDER.findall(text)))


BACKEND_SCAFFOLD_MODULES = (
    # 后端语言包的顶层分组（键的第一个点分段）。新增分组时必须同步加进来，
    # 否则 --scaffold 生成的模板会**静默漏掉**这一组，翻译者根本看不到它们。
    # 例：修复四新增了 capability.*（模型能力提示与推荐理由）、task.*（任务模板）、
    # mode.* / export.*（对话模式说明与导出文档标题），漏了就会少 50 条。
    # v0.2.1 综合优化又新增了 summary.*（长文档分段摘要的进度与故障提示，
    # 任务四），同样的道理必须同步加进来，否则新语言模板会整组缺失。
    "app", "cmd", "sys", "file", "err", "market", "hub",
    "automation", "update", "skill",
    "capability", "task", "mode", "export", "summary",
)


def scaffold(locale, sources):
    """为新语言生成模板：结构照抄源语言，值留空并带上源文本作为参考。

    ``sources`` 必须是 **按目录分开的源语言包** ``{目录: {键: 文案}}``，
    不能是一份合并过的包 —— 后端与前端语言包的 locale 名字相同（都是 `zh-Hans`），
    合并时后加载的会覆盖先加载的，于是后端模板会从**前端**的键里筛，
    结果只剩 6 条（几乎为空）。这个坑在修复轮次里实测到过：
    `--scaffold ja` 生成的后端模板只有 6 条待翻译，等于社区拿不到可用的后端模板。

    前端语言包整份照搬（没有分组过滤）；后端按 :data:`BACKEND_SCAFFOLD_MODULES`
    筛选——因为后端源语言包里混了一些"翻译者不该碰"的键（见 check 的说明）。
    """
    for directory in (BACKEND_LANG, FRONTEND_LANG):
        if not os.path.isdir(directory):
            continue
        source = sources.get(directory, {})
        if not source:
            print(f"  ✗ 找不到 {directory} 的源语言 {SOURCE_LOCALE} 语言包，跳过")
            continue
        out = {}
        for key, text in source.items():
            if key.startswith("_"):
                continue          # _meta 之类的元信息键不需要翻译
            if directory == BACKEND_LANG and key.split(".")[0] not in BACKEND_SCAFFOLD_MODULES:
                continue
            out[key] = {
                "text": "",
                "comment": f"源文案（{SOURCE_LOCALE}）：{text}",
            }
        path = os.path.join(directory, f"{locale}.json")
        if os.path.exists(path):
            print(f"  ✗ {path} 已存在，未覆盖")
            continue
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"//": f"{locale} 语言包模板：把 text 填成 {locale} 的文案即可，"
                             f"comment 里是源文案供参考。", **out}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"  ✓ 已生成模板 {path}（共 {len(out)} 条待翻译）")
    return 0


def check(locales, quiet):
    packs = {}
    for directory, label in ((BACKEND_LANG, "后端"), (FRONTEND_LANG, "前端")):
        found = load_dir(directory)
        packs[label] = found
        if not quiet:
            print(f"== {label}语言包（{directory}）==")
            for locale, flat in sorted(found.items()):
                print(f"  {locale}: {len(flat)} 条")

    used = collect_used_keys()
    problems = 0
    for label, found in packs.items():
        if SOURCE_LOCALE not in found:
            print(f"✗ {label}：缺少源语言 {SOURCE_LOCALE} 语言包")
            problems += 1
            continue
        source = found[SOURCE_LOCALE]
        for locale, flat in sorted(found.items()):
            if locale == SOURCE_LOCALE:
                continue
            if locales and locale not in locales:
                continue
            missing = sorted(k for k in source if k not in flat)
            extra = sorted(k for k in flat if k not in source)
            mismatched = []
            for key in sorted(set(source) & set(flat)):
                if placeholders(source[key]) != placeholders(flat[key]):
                    mismatched.append((key, placeholders(source[key]), placeholders(flat[key])))
            if not quiet or missing or extra or mismatched:
                print(f"-- {label} / {locale}：源语言 {len(source)} 条，本语言 {len(flat)} 条")
            if missing:
                problems += len(missing)
                print(f"   缺失 {len(missing)} 条：")
                for key in missing[:40]:
                    print(f"     - {key}")
                if len(missing) > 40:
                    print(f"     …（还有 {len(missing) - 40} 条）")
            if extra:
                print(f"   多余 {len(extra)} 条（源语言里没有，可能是拼写错误或已废弃）：")
                for key in extra[:20]:
                    print(f"     + {key}")
            if mismatched:
                problems += len(mismatched)
                print(f"   占位符不一致 {len(mismatched)} 条：")
                for key, want, got in mismatched[:20]:
                    print(f"     ! {key}: 源 {want} ≠ 本语言 {got}")
            if not missing and not extra and not mismatched:
                print("   ✓ 完整且占位符一致")

    # 代码用到的键是否都在**它自己读的那份**源语言包里。
    #
    # 这里修的是一个真实缺陷（v0.2.2 硬编码清查 §3.3/§3.4）：原先把后端与前端源语言包
    # **并成一个集合**再判断，于是"只存在于另一个包"的键会被判成存在 ——
    # 而运行期两个前端只读 `frontend/Resources/Lang`（+ 磁盘 `lang/ui`），后端读 `lang/`，
    # 两边互不可见。实测漏掉的就是 `capability.unknown` 与 `automation.no_url`：
    # 它们在 `backend/lang` 里有、`frontend/Resources/Lang` 里没有，WPF 调用时
    # Loc.T 直接返回键名本身（弹框里显示 `automation.no_url`）。
    # 现在按文件所在目录分别判：`backend/**` 查后端包，其余（`frontend/**`）查前端包。
    unknown = []
    for key in sorted(used):
        if key in IGNORED_MISSING or key in SKELETON_KEYS:
            continue
        for rel in sorted(set(used[key])):
            target = "后端" if rel.replace("\\", "/").startswith("backend/") else "前端"
            if key not in packs.get(target, {}).get(SOURCE_LOCALE, {}):
                unknown.append(f"{key}   （{rel}，应有于{target}语言包）")
                break
    if unknown:
        print(f"\n-- 代码中引用但**自己读的那份**源语言包里没有的键（{len(unknown)} 个）：")
        for item in unknown[:60]:
            print(f"     ? {item}")
        problems += len(unknown)
    elif not quiet:
        print(f"\n✓ 代码引用的 {len(used)} 个键都能在**各自读取的**源语言包里找到"
              f"（后端 / 前端分别判，不再并成一个集合）")

    # 同一个键是否同时以"嵌套对象"和"扁平点号键"两种形式存在。
    #
    # 为什么必须查（v0.2.1 能力扩展-1 实测踩到）：`cmd` 段历史上混用两种写法 ——
    # 早期是嵌套（`"items": {"/help": {"name": …}}`），后来新增的键是扁平点号
    # （`"items./task.name": …`）。解析器只认其中一种，于是**改错那一份不会报错、
    # 只会表现为"文案没变"**：给 `/agent` 补的那句"不提供写文件工具"写进了嵌套那份，
    # 而运行时读扁平那份，冒烟测试看到旧文案才发现。
    # 语言包完整性与占位符检查都发现不了它（两种形式各自都"完整"），所以单独查。
    duplicates = []
    redundant = 0
    for directory, label in ((BACKEND_LANG, "后端"), (FRONTEND_LANG, "前端")):
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.lower().endswith(".json"):
                continue
            try:
                with io.open(os.path.join(directory, name), "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                continue
            for key, flat_value, nested_value in _duplicate_paths(raw):
                if flat_value == nested_value:
                    redundant += 1
                else:
                    duplicates.append((f"{label}/{name}", key, flat_value, nested_value))
    if duplicates:
        print(f"\n-- 同一个键两种写法**内容不一致**（{len(duplicates)} 处）：")
        for where, key, flat_value, nested_value in duplicates[:10]:
            print(f"     ! {where}: {key}")
            print(f"         扁平键（运行时读这条）：{str(flat_value)[:60]!r}")
            print(f"         嵌套键（改这条不生效）：{str(nested_value)[:60]!r}")
        problems += len(duplicates)
    elif not quiet:
        print(f"\n✓ 没有「同键两写法内容不一致」的情况"
              f"（有 {redundant} 处内容相同的冗余写法，不影响行为）")

    # 源语言自身有没有"定义了但没人用"的键
    # （这里仍用两份源语言包的**并集**：这条只是"提示"，不判失败；
    #   而上面的"引用键在不在自己那份包里"必须是**分开判**的，见那段注释）
    source_all = {}
    for found in packs.values():
        source_all.update(found.get(SOURCE_LOCALE, {}))
    unused = sorted(k for k in source_all if k not in used)
    if unused and not quiet:
        print(f"\n提示：源语言包里有 {len(unused)} 条未被代码直接引用的键")
        print("（动态拼接的键、技能示例里的键或保留键都可能出现在这里，仅供参考）")

    # 本地化文档同步（任务书修复三：docs/<locale>/ 下至少 README、用户手册、FAQ、
    # 自动化指南四份）。语言包完整但文档缺一份，英文用户仍然看不懂怎么用。
    print("\n-- 本地化文档（docs/<locale>/）")
    docs_root = os.path.join(ROOT, "docs")
    if not os.path.isdir(docs_root):
        print("   docs/ 不存在，跳过")
    else:
        required_docs = ("README.md", "USER_GUIDE.md", "FAQ.md", "AUTOMATION_GUIDE.md")
        locale_dirs = {}
        for name in sorted(os.listdir(docs_root)):
            path = os.path.join(docs_root, name)
            if not os.path.isdir(path):
                continue
            files = sorted(f for f in os.listdir(path) if f.endswith(".md"))
            if files:
                locale_dirs[name] = files
        if not locale_dirs:
            print("   未找到按语言分的文档目录")
        for locale, files in locale_dirs.items():
            missing = [name for name in required_docs if name not in files]
            if missing:
                problems += len(missing)
                print(f"   ✗ docs/{locale}/ 缺少：{', '.join(missing)}")
            else:
                print(f"   ✓ docs/{locale}/ 四份必需文档齐全")
        # 各语言目录之间必须一一对应（避免"中文更新了英文没跟上"）
        if len(locale_dirs) > 1:
            names = list(locale_dirs)
            baseline = set(locale_dirs[names[0]])
            for locale in names[1:]:
                only_first = sorted(baseline - set(locale_dirs[locale]))
                only_this = sorted(set(locale_dirs[locale]) - baseline)
                if only_first or only_this:
                    problems += len(only_first) + len(only_this)
                    print(f"   ✗ docs/{locale}/ 与 docs/{names[0]}/ 文档清单不一致："
                          f"缺少 {only_first or '无'}，多出 {only_this or '无'}")
                else:
                    print(f"   ✓ docs/{locale}/ 与 docs/{names[0]}/ 一一对应（{len(baseline)} 份）")

        # 共用双语文档（docs/ 根目录的 X.md + X.zh-Hans.md）结构对齐检查：
        # 章节数量、章节编号序列、以及两版文首的互相声明。
        print("\n-- 共用双语文档结构（docs/*.md + *.zh-Hans.md）")
        pair_problems, pair_lines = check_bilingual_pairs(docs_root)
        problems += pair_problems
        for line in pair_lines:
            print(line)

    print()
    # 已经**记在案的偏差**（DECLARATION_WARNINGS）不该把整条发布门禁判成失败。
    # 它们由 check_bilingual_pairs 以 " ! " 形式输出、且刻意不计入 problems；
    # 这里把它们的条数单独数出来，让退出码只反映"真正待修的问题"。
    deferred = sum(1 for line in pair_lines if line.lstrip().startswith("!")) \
        if "pair_lines" in dir() else 0
    if problems:
        print(f"✗ 发现问题 {problems} 处" + (f"（另有 {deferred} 处已记在案的偏差）" if deferred else ""))
    else:
        print("✓ 语言包检查通过" + (f"（{deferred} 处已记在案的偏差，见 DECLARATION_WARNINGS）" if deferred else ""))
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser(description="EchoDesk 语言包完整性检查")
    parser.add_argument("--locale", action="append", help="只检查指定语言（可重复）")
    parser.add_argument("--scaffold", metavar="LOCALE", help="为某语言生成语言包模板")
    parser.add_argument("--quiet", action="store_true", help="只在有问题时输出")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.scaffold:
        # 模板生成必须**按目录分别取源语言包**：后端与前端语言包的 locale 名字相同
        # （都是 zh-Hans），合并成一份会让后加载的前端包覆盖后端包，
        # 于是后端模板从前端的键里筛，只剩 6 条 —— 实测踩到过。
        sources = {}
        for directory in (BACKEND_LANG, FRONTEND_LANG):
            sources[directory] = load_dir(directory).get(SOURCE_LOCALE, {})
        return scaffold(args.scaffold, sources)

    packs = {}
    for directory in (BACKEND_LANG, FRONTEND_LANG):
        packs.update(load_dir(directory))
    return check(args.locale, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
