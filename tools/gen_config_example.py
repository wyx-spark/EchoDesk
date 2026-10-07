#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 `config.example.json`：后端默认配置的副本 + 双语说明 + 三平台路径说明。

**为什么不是简单 `Copy-Item backend/config.json config.example.json`**（构建脚本原来的做法）：
用户拿到这个文件时，看到的是 200 多行没有任何解释的 JSON —— 字段什么意思、改了会不会生效、
"用户数据目录"在他的平台上到底是哪个路径，全都要去翻文档。任务书 5.1 要求的正是这件事：
说明性内容双语、路径类字段注明三平台差异。

**为什么用"生成时注入"而不是直接往 `backend/config.json` 里加 `_comment` 字段**：
`backend/config.json` 是**运行期**配置，会被 pydantic 模型加载。往里塞非模型字段是在赌
"模型允许额外字段"，赌输的直接后果是后端启动即失败。注入放在**副本**上，运行期配置一个字不动。

注入的内容（都在 JSON 顶层，不影响任何被程序读取的键）：

    "_comment"          —— 这个文件是什么、怎么用它、覆盖机制是什么（中英双语）
    "_platform_paths"   —— 用户数据 / 结果 / 模型 三个目录在三平台上的位置（中英双语）

用法：

    python tools/gen_config_example.py                     # 默认写 config.example.json
    python tools/gen_config_example.py --out <路径>
    python tools/gen_config_example.py --check <路径>      # 校验已生成的文件（门禁用）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "backend", "config.json")
DEFAULT_OUT = os.path.join(ROOT, "config.example.json")

#: 双语说明。**事实必须与代码一致**，写这几条时核对过的位置：
#:   * 覆盖层 `config.user.json`：backend/config.py 的 Config.load()（同目录、字段优先）
#:   * 用户数据根：backend/config.py 的 user_data_root()（ECHODESK_DATA → 上层 app 目录 → 父目录）
#:   * 前端在非便携布局下才按平台惯例回退：frontend-avalonia/.../Services/UserPaths.cs
COMMENT: list[str] = [
    "EchoDesk 配置示例 / EchoDesk configuration example.",
    "",
    "这是什么：后端默认配置（backend/config.json）的一份副本，供你比对或恢复默认值。",
    "只读参考即可；直接改本文件不会影响程序 —— 程序读的是 app/backend/config.json。",
    "",
    "What this is: a copy of the backend defaults (backend/config.json), for comparison or for",
    "restoring a default. It is a reference: the program reads app/backend/config.json, so editing",
    "this file alone has no effect.",
    "",
    "要改配置：在同目录新建 config.user.json，只写你想改的字段（同名字段优先，其余继承默认值）。",
    "该文件被 .gitignore 忽略，适合放密钥/个人路径之类的本机设置。",
    "",
    "To change settings: create config.user.json next to the real config.json and put only the keys",
    "you want to override there. Fields present in config.user.json win; everything else keeps the",
    "default. It is git-ignored, so it is the right place for machine-specific values.",
    "",
    "路径写法：本文件是 JSON，反斜杠必须转义（写一个 Windows 路径要敲两个反斜杠），很容易写错。",
    "Windows 也接受正斜杠，所以推荐一律写成 C:/Users/you/... 这种形式，三平台通用、不用转义。",
    "",
    "Paths: this is JSON, so a backslash must be escaped (one Windows backslash becomes two in the",
    "file), which is easy to get wrong. Windows accepts forward slashes too, so prefer spellings like",
    "C:/Users/you/... — they work on all three platforms and need no escaping.",
]

#: 三平台目录说明。注意这里**不能**写成"macOS 一定用 Application Support"：
#: 发布物是便携布局（.../app/ 内是程序本体），用户数据就放在发布目录外层；
#: 只有当程序目录不叫 app（例如装到 /usr 或裸二进制运行）时，**前端**才按平台惯例回退。
# 这里的三平台路径**刻意保留各平台的原样写法**（v0.2.2 硬编码清查裁定 §3）：
#   * 它是**示例文件**的一部分，作用是让用户看到「我这个平台上路径长什么样」；
#     把 Windows 路径改写成英文词、或换成占位符，反而失去示例价值；
#   * 目标文件 `config.example.json` 不参与运行（程序读的是 config.json），
#     所以这些路径不会被任何代码解释，也不构成界面文本。
# 结论：保留原样，并按裁定要求登记理由（不是漏掉）。
PLATFORM_PATHS: dict[str, dict[str, str]] = {
    "note": (
        "发布物是便携布局（用户数据在发布目录外层，不在 app/ 里）；"
        "下表给出「程序目录不是 app/ 时」各平台回退到的位置，以及典型写法。"
        " The shipped bundle is a portable layout (user data lives in the outer folder, not in"
        " app/); the table below lists where each platform falls back to when the program folder is"
        " not an app/ folder, plus the typical spellings."
    ),
    "user_data": {
        "override": "ECHODESK_DATA 环境变量（最高优先）/ the ECHODESK_DATA environment variable (highest priority)",
        "windows": "程序目录本身（安装版/便携版里就是发布目录外层；**不是** %APPDATA%）",
        "macos": "~/Library/Application Support/EchoDesk（仅当程序目录不是 app/ 时）",
        "linux": "~/.local/share/EchoDesk（设了 XDG_DATA_HOME 时为 $XDG_DATA_HOME/EchoDesk）",
    },
    "results": {
        "field": "results_location（留空 = 用户数据目录下的 results/）",
        "windows": "发布目录外层\\results\\",
        "macos": "~/Library/Application Support/EchoDesk/results/",
        "linux": "~/.local/share/EchoDesk/results/",
    },
    "models": {
        "field": "models_dir（默认 \"models\"，相对后端工作目录）",
        "windows": "app\\backend\\models\\",
        "macos": "app/backend/models/",
        "linux": "app/backend/models/",
    },
}


def build_example() -> dict:
    with io.open(SOURCE, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"{SOURCE} 顶层不是对象，无法作为配置示例")
    # 注入字段放在**最前面**，用户打开文件第一眼就能看到；JSON 对象键顺序在 Python 3.7+
    # 与 json.dump 里都是保留的。
    example: dict = {"_comment": COMMENT, "_platform_paths": PLATFORM_PATHS}
    for key, value in data.items():
        if key in example:      # 理论上不会撞，但撞了就明确报错而不是静默覆盖
            raise SystemExit(f"注入字段与配置键冲突：{key}")
        example[key] = value
    return example


def dump(example: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(example, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def check(path: str) -> int:
    """门禁：产物必须是"当前默认配置 + 注入说明"的精确结果（防止改了默认值忘了重新生成）。"""
    print("== config.example.json 门禁 ==")
    if not os.path.isfile(path):
        print(f"  [--]  {os.path.relpath(path, ROOT)} 不存在（跳过；发布构建时生成）")
        return 0
    expected = build_example()
    with io.open(path, encoding="utf-8") as handle:
        try:
            actual = json.load(handle)
        except json.JSONDecodeError as exc:
            print(f"  [FAIL] {path} 不是合法 JSON：{exc}")
            return 1
    if actual == expected:
        print(f"  [OK]   {os.path.relpath(path, ROOT)} 与默认配置 + 注入说明完全一致"
              f"（{len(expected)} 个顶层键）")
        return 0
    only_expected = sorted(set(expected) - set(actual))
    only_actual = sorted(set(actual) - set(expected))
    print(f"  [FAIL] 与预期不一致：缺少 {only_expected}；多出 {only_actual}")
    if not only_expected and not only_actual:
        print("         键相同但取值不同（默认配置改过？重新生成即可）")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 config.example.json（含双语说明与三平台路径）")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"输出路径（默认 {os.path.relpath(DEFAULT_OUT, ROOT)}）")
    parser.add_argument("--check", default="", help="校验指定文件是否与预期一致（门禁模式）")
    args = parser.parse_args()

    if args.check:
        return check(args.check)

    example = build_example()
    dump(example, args.out)
    size = os.path.getsize(args.out)
    print(f"已生成 {os.path.relpath(args.out, ROOT)}（{len(example)} 个顶层键，{size} 字节）")
    print(f"  注入：_comment（{len(COMMENT)} 行双语说明）、_platform_paths（三平台目录对照）")
    print(f"  来源：{os.path.relpath(SOURCE, ROOT)}（运行期配置未被改动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
