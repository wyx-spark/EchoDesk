"""语言包批量补丁工具（研发/维护用）。

用途：一次给**所有**语言包补上同一批键，避免"只加了中文忘了英文"导致
``tools/check_lang.py`` 报缺键、或界面上出现键名。

补丁文件是一个 JSON，结构为 ``{"<模块>": {"<键>": {"zh-Hans": "...", "en": "..."}}}``，
也可以直接写成 ``{"<模块>": {"<键>": "<所有语言共用的值>"}}``（少见的无文案键）。

例：``python tools/lang_edit.py patch.json --target backend``

选项：
  ``--target backend|frontend|both``  默认 both
  ``--lang zh-Hans,en``               只处理指定语言（默认两个都处理）
  ``--dry-run``                       只打印将要写入的内容
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANGS = ("zh-Hans", "en")

TARGETS = {
    # 目标 → {语言: 文件路径}
    "backend": {lang: os.path.join(ROOT, "backend", "lang", f"{lang}.json") for lang in LANGS},
    "frontend": {
        # 界面语言包按模块拆分：<locale>.main.json / <locale>.settings.json
        # 这里给的是"主文件"，模块分片由 --module 决定，见下方 resolve_path
        lang: os.path.join(ROOT, "frontend", "Resources", "Lang", f"{lang}.json") for lang in LANGS
    },
}

FRONTEND_MODULES = ("main", "settings")


def frontend_paths() -> dict:
    """前端语言包路径：主文件 + 模块分片。"""
    out: dict = {}
    base = os.path.join(ROOT, "frontend", "Resources", "Lang")
    for lang in LANGS:
        out[lang] = {
            "": os.path.join(base, f"{lang}.json"),
            "main": os.path.join(base, f"{lang}.main.json"),
            "settings": os.path.join(base, f"{lang}.settings.json"),
        }
    return out


def load(path: str) -> collections.OrderedDict:
    if not os.path.exists(path):
        return collections.OrderedDict()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=collections.OrderedDict)


def save(path: str, data: collections.OrderedDict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def apply_patch(patch: dict, target: str, langs: list, dry: bool) -> int:
    """把补丁写进目标语言包；返回写入的键数量。"""
    count = 0
    if target == "backend":
        tables = TARGETS["backend"]
    else:
        tables = frontend_paths()

    for module, entries in patch.items():
        for key, values in entries.items():
            if isinstance(values, str):
                values = {lang: values for lang in LANGS}
            for lang in langs:
                if lang not in values:
                    print(f"[lang_edit] 跳过 {target}/{lang} {module}.{key}（补丁未提供该语言）")
                    continue
                text = values[lang]
                if target == "backend":
                    path = tables[lang]
                    data = load(path)
                    data.setdefault(module, collections.OrderedDict())[key] = text
                    if not dry:
                        save(path, data)
                    count += 1
                else:
                    # 前端：key 是**完整点分路径**（如 "settings.general.themeLabel"）。
                    # 放在哪个文件由第一段决定，但节点结构必须保留完整路径，
                    # 否则扁平化后键名会少一层（踩过：写成 general.themeLabel 导致找不到）。
                    full_key = key if key.startswith(module + ".") else f"{module}.{key}"
                    path = pick_frontend_file(tables[lang], module, full_key)
                    data = load(path)
                    node = data
                    parts = full_key.split(".")
                    for part in parts[:-1]:
                        node = node.setdefault(part, collections.OrderedDict())
                    node[parts[-1]] = text
                    if not dry:
                        save(path, data)
                    count += 1
                if dry:
                    print(f"[lang_edit] {target}/{lang} {module}.{key} = {text[:60]}")
    return count


def pick_frontend_file(paths: dict, module: str, key: str) -> str:
    """选择前端语言包文件：按模块前缀放进对应分片，其余进主文件。"""
    if module in FRONTEND_MODULES:
        return paths[module]
    head = key.split(".")[0]
    if head in FRONTEND_MODULES:
        return paths[head]
    return paths[""]


def main() -> int:
    parser = argparse.ArgumentParser(description="EchoDesk 语言包批量补丁")
    parser.add_argument("patch", help="补丁 JSON 文件")
    parser.add_argument("--target", choices=("backend", "frontend", "both"), default="both")
    parser.add_argument("--lang", default=",".join(LANGS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.patch, "r", encoding="utf-8") as f:
        patch = json.load(f, object_pairs_hook=collections.OrderedDict)
    langs = [x.strip() for x in args.lang.split(",") if x.strip()]

    total = 0
    targets = ("backend", "frontend") if args.target == "both" else (args.target,)
    for target in targets:
        n = apply_patch(patch, target, langs, args.dry_run)
        print(f"[lang_edit] {target}: {'将' if args.dry_run else '已'}写入 {n} 条")
        total += n
    print(f"[lang_edit] 合计 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
