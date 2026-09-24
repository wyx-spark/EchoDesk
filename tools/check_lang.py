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
    "app", "cmd", "sys", "file", "err", "market", "hub",
    "automation", "update", "skill",
    "capability", "task", "mode", "export",
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

    # 代码用到的键是否都在源语言包里
    source_all = {}
    for found in packs.values():
        source_all.update(found.get(SOURCE_LOCALE, {}))
    unknown = sorted(k for k in used
                      if k not in source_all and k not in IGNORED_MISSING and k not in SKELETON_KEYS)
    if unknown:
        print(f"\n-- 代码中引用但源语言包里没有的键（{len(unknown)} 个）：")
        for key in unknown[:60]:
            where = ", ".join(sorted(set(used[key]))[:2])
            print(f"     ? {key}   （{where}）")
        problems += len(unknown)
    elif not quiet:
        print(f"\n✓ 代码引用的 {len(used)} 个键都能在源语言包里找到")

    # 源语言自身有没有"定义了但没人用"的键
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

    print()
    if problems:
        print(f"✗ 发现问题 {problems} 处")
    else:
        print("✓ 语言包检查通过")
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
