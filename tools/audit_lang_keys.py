# -*- coding: utf-8 -*-
"""反向语言键审计：Avalonia 前端引用的语言键，是否都在语言包里存在。

为什么要它（与 tools/check_lang.py 的分工）
------------------------------------------
``tools/check_lang.py`` 查的是**语言包自身的完整性**（各语言键集合对齐、
代码引用的键在不在包里 —— 缺键会报错）。而 Avalonia 的写法是
``Loc.T("key", "中文兜底")``：语言包里**没有**这个键时它不报错，而是安静地
用中文兜底，**英文界面里照样显示中文**。这正是 P2「英文界面仍显示中文」的
一条真实成因，方向与 check_lang.py 恰好相反，因此需要一个反向核对：
**代码引用了、而语言包里没有的键**。

判定与退出码
------------
* 退出码 0：两个语言包都没有缺失键（当前基线：0 缺失）。
* 退出码 1：有缺失键（en 或 zh-Hans 任一），逐条打印键名、出处与兜底文案。
* 退出码 2：输入目录不存在 —— 避免"目录没了反而静默通过"（门禁变瞎）。

末节「键存在、代码里又传了中文兜底」只打印清单、**不计入退出码**：
兜底本身是给"语言包损坏"用的，它是否算问题需要人逐条看，不由机器判死。

来源
----
v0.2.2 的 ``artifacts/_audit_avalonia_keys.py``（当时是开发期临时脚本，
``artifacts/`` 不进库）。本轮移入 ``tools/`` 并接入 ``tools/verify_release.ps1``
的静态检查一节；**判定逻辑未改**（缺键判定与前缀规则原样保留），
只补了退出码与输入存在性检查，以便长期挂在门禁上。

用法
----
    python tools/audit_lang_keys.py
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANG_DIR = os.path.join(ROOT, "frontend", "Resources", "Lang")
AVALONIA = os.path.join(ROOT, "frontend-avalonia")

if not os.path.isdir(AVALONIA) or not os.path.isdir(LANG_DIR):
    print("反向审计无法进行：缺少输入目录")
    print("  语言包目录：%s（%s）" % (LANG_DIR, "存在" if os.path.isdir(LANG_DIR) else "不存在"))
    print("  前端目录：  %s（%s）" % (AVALONIA, "存在" if os.path.isdir(AVALONIA) else "不存在"))
    sys.exit(2)

lang = {}
for name in os.listdir(LANG_DIR):
    if not name.endswith(".json"):
        continue
    locale = name[:-5].split(".")[0]
    if locale not in ("en", "zh-Hans"):
        continue
    with io.open(os.path.join(LANG_DIR, name), encoding="utf-8") as fh:
        data = json.load(fh)

    def walk(node, prefix=""):
        for key, value in node.items():
            if key.startswith("_") or key.startswith("//"):
                continue
            if isinstance(value, dict):
                walk(value, prefix + key + ".")
            else:
                lang.setdefault(locale, set()).add(prefix + key)

    walk(data)

PATTERN = re.compile(r'Loc\.T\(\s*"([^"]+)"\s*(?:,\s*"([^"]*)")?', re.S)
used = {}
for base, _, files in os.walk(AVALONIA):
    for filename in files:
        if not filename.endswith(".cs"):
            continue
        path = os.path.join(base, filename)
        with io.open(path, encoding="utf-8") as fh:
            source = fh.read()
        for match in PATTERN.finditer(source):
            key, fallback = match.group(1), match.group(2) or ""
            used.setdefault(key, (fallback, os.path.relpath(path, ROOT)))

def satisfied(key, keys):
    """键是否可用：**自身在**，或**它是某一组键的前缀**。

    后半句是为运行期拼出来的键准备的：`Loc.T("settings.automation.wizardStep" + step, …)`
    里正则只能截到前缀 `settings.automation.wizardStep`，而语言包里真正存在的是
    `…wizardStep1` … `…wizardStep4`。把它算成"缺失"是误报，会让审计长期挂着一个
    已知假阳性，久了就没人看这张表了（与 tools/check_lang.py 的 IGNORED_MISSING 同一考虑，
    那里是白名单，这里用前缀判定，不必维护清单）。
    """
    if key in keys:
        return True
    # 注意是 `startswith(key)` 而**不是** `startswith(key + ".")`：
    # 拼出来的键是 `…wizardStep` + `1` → `…wizardStep1`，中间**没有**点号。
    return any(other.startswith(key) for other in keys)


missing_en = sorted(k for k in used if not satisfied(k, lang.get("en", set())))
missing_zh = sorted(k for k in used if not satisfied(k, lang.get("zh-Hans", set())))
print("Avalonia 引用的键：%d" % len(used))
print("en 语言包里缺失：%d" % len(missing_en))
print("zh-Hans 语言包里缺失：%d" % len(missing_zh))
for key in sorted(set(missing_en) | set(missing_zh)):
    fallback, where = used[key]
    print("  %-52s %s   （兜底：%s）" % (key, where, fallback[:40]))

# 调试用：按"末段名"再核对一次。语言包是多文件分片合并的，
# 同一末段名可能出现在不同的父节点下（check_lang 按点路径算，这里按名字算），
# 两者结论不同时以**点路径**为准，但把差异打出来便于判断是不是放错了层级。
for key in sorted(set(missing_en) | set(missing_zh)):
    leaf = key.split(".")[-1]
    present = sorted(k for k in lang.get("en", set()) if k.split(".")[-1] == leaf)
    print("    [%s] 语言包里同名末段：%s" % (key, present or "（完全没有）"))

# P2 的第二半：**键存在但取值被硬编码**。上面查的是"键不存在"，
# 这里把"代码里传了中文兜底、且这个键在语言包里也存在"的全都列出来 ——
# 它们不一定有问题（兜底是给"语言包损坏"用的），但**兜底是中文而当前语言是英文**
# 时就会露出来，所以值得逐个核对一遍。
hardcoded = []
for key, (fallback, where) in sorted(used.items()):
    if key not in lang.get("zh-Hans", set()):
        continue
    if any("\u4e00" <= ch <= "\u9fff" for ch in fallback):
        hardcoded.append((key, fallback, where))
print()
print("代码里带中文兜底的键：%d（这些是「键存在、兜底也是中文」的，逐条核对用）" % len(hardcoded))
for key, fallback, where in hardcoded:
    print("  %-52s %s" % (key, fallback[:44]))

# 结论行放在最后一行：verify_release.ps1 的汇总表取"最后一行输出"当明细。
print()
print("反向审计结论：en 缺失 %d / zh-Hans 缺失 %d（引用键 %d）"
      % (len(missing_en), len(missing_zh), len(used)))

# ---------------------------------------------------------------------------
# v0.2.2 硬编码清查 §3.4：把同一个"反向核对"扩到**另外两个扫描根**
#
# 为什么必须扩（本轮实测的盲区）：上面这一段只看 frontend-avalonia/，而
# WPF 的 `frontend/**` 与后端的 `backend/*.py` 都不在其中 —— 于是
# `capability.unknown` / `automation.no_url`（只存在于 backend/lang、WPF 却按
# 前端包调用）这种"跨包缺键"两个门禁**都看不见**。这里按"谁读哪份包"分别核对：
#   * frontend/**  （WPF）        → 前端包（frontend/Resources/Lang/*.json）
#   * backend/**    （Python）    → 后端包（backend/lang/*.json）
#   * frontend-avalonia/**（上面已查）→ 前端包
# 判定口径与上面完全一致（缺键 = 直接缺失且不构成任何已有键的前缀）。
# ---------------------------------------------------------------------------
BACKEND_LANG_DIR = os.path.join(ROOT, "backend", "lang")
WPF_DIR = os.path.join(ROOT, "frontend")
BACKEND_DIR = os.path.join(ROOT, "backend")

import re  # noqa: E402

WPF_PATTERNS = (
    re.compile(r'Loc\.T\(\s*"([A-Za-z0-9_./@-]+)"\s*(?:,\s*"([^"]*)")?', re.S),
    re.compile(r'Loc\.Has\(\s*"([A-Za-z0-9_./@-]+)"'),
    re.compile(r'(?:svc:)?Loc\.(?:Key|TipKey|HelpKey)\s*=\s*"([A-Za-z0-9_./@-]+)"'),
)
BACKEND_PATTERNS = (
    re.compile(r'(?<![\w.])(?:_t|t|api\.lang)\(\s*"([A-Za-z0-9_./@-]+)"'),
    re.compile(r'log_user\(\s*[^,]+,\s*"([A-Za-z0-9_./@-]+)"'),
)


def build_lang(directory):
    """读一个语言目录（主文件 + 模块分片），返回 {locale: {key}}。"""
    packs = {}
    if not os.path.isdir(directory):
        return packs
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        locale = name[:-5].split(".")[0]
        try:
            with io.open(os.path.join(directory, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        found = packs.setdefault(locale, set())

        def walk(node, prefix=""):
            for key, value in node.items():
                if not isinstance(key, str) or key.startswith(("_", "//")):
                    continue
                if isinstance(value, dict):
                    walk(value, prefix + key + ".")
                else:
                    found.add(prefix + key)

        walk(data)
    return packs


#: 与 tools/check_lang.py 的 SKELETON_KEYS 同一份理由：`backend/skills_runtime.py` 里
#: `api.lang("hello.reply", …)` 这类调用出现在**给新插件生成的入口骨架**里，
#: 那些键属于插件自己的语言包（骨架会同时生成 `<plugin>/lang/*.json`），不在主程序包里。
#: 不豁免就会长期挂一个已知假阳性 —— 那样的门禁没人看（理由与 `satisfied()` 里的前缀判定同源）。
SKELETON_KEYS = frozenset({"hello.default_name", "hello.reply", "panel.result"})

#: 扫描时要跳过的目录（第三方库、构建产物、缓存）
SKIP_DIRS = frozenset({"bin", "obj", "__pycache__", ".venv", "site-packages", "node_modules", ".git"})


def scan(root, patterns, exts):
    used = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in files:
            if not filename.endswith(exts):
                continue
            path = os.path.join(base, filename)
            try:
                with io.open(path, encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
            except OSError:
                continue
            for pattern in patterns:
                for match in pattern.finditer(source):
                    key = match.group(1)
                    if key in SKELETON_KEYS:
                        continue
                    fallback = match.group(2) if (match.lastindex or 0) > 1 else ""
                    used.setdefault(key, (fallback or "", os.path.relpath(path, ROOT)))
    return used


extra_problems = 0
summary = {}
for label, root, patterns, exts, lang_dir in (
    ("WPF（frontend/**）", WPF_DIR, WPF_PATTERNS, (".cs", ".xaml"), LANG_DIR),
    ("后端（backend/*.py）", BACKEND_DIR, BACKEND_PATTERNS, (".py",), BACKEND_LANG_DIR),
):
    pack = build_lang(lang_dir)
    if not pack:
        print("\n反向审计（%s）无法进行：找不到语言包目录 %s" % (label, lang_dir))
        extra_problems += 1
        summary[label] = -1
        continue
    refs = scan(root, patterns, exts)
    if root == BACKEND_DIR:
        # 语言包目录自己也含中文字面量（那是文案本体，不是引用），排除
        refs = {k: v for k, v in refs.items()
                if not v[1].replace("\\", "/").startswith("backend/lang")}
    miss_en = sorted(k for k in refs if not satisfied(k, pack.get("en", set())))
    miss_zh = sorted(k for k in refs if not satisfied(k, pack.get("zh-Hans", set())))
    miss = sorted(set(miss_en) | set(miss_zh))
    summary[label] = len(miss)
    print()
    print("-- %s：引用键 %d，缺失 %d（en %d / zh-Hans %d）"
          % (label, len(refs), len(miss), len(miss_en), len(miss_zh)))
    for key in miss:
        fallback, where = refs[key]
        print("     ? %-46s %s   （兜底：%s）" % (key, where, fallback[:30]))
    extra_problems += len(miss)

print()
print("反向审计（扩面后，逐行汇总）：")
for label, count in summary.items():
    print("   %-22s 缺失 %s" % (label, "无法进行" if count < 0 else count))
print("结论：Avalonia 缺失 %d / 其余两处合计缺失 %d（判定阈值仍是 0 缺失）"
      % (len(set(missing_en) | set(missing_zh)), extra_problems))


# ---------------------------------------------------------------------------
# 静态自检：用 `_t(` / `log_user(` 却没有把名字导入进来的文件
#
# 为什么加（两次真实事故，见 records/v0.2.2-硬编码修复记录.md 第 6 批）：
#   * platform_support.py 在模块级用 `_t(...)`，而该模块**刻意**用函数内延迟导入 i18n
#     → `NameError`，后端连 ready 握手都发不出；
#   * agent_tools.py 只改了字符串、忘了补 `from i18n import t as _t` → 全部工具调用 NameError。
# 两次都是"跑到真实调用才发现"。这件事**静态就能查**，所以放进门禁。
#
# 判定：文件里出现 `_t(` / `log_user(` 的调用，但没有任何"绑定该名字"的 i18n 导入：
#   * `_t`      ← `from i18n import t as _t`（模块级或函数内都算）
#   * `log_user`← `from i18n import log_user ...`
#   * `i18n.t(` ← `import i18n`
# 只查 backend/：前端是 C#，另有自己的反向审计（上面几节）。
# ---------------------------------------------------------------------------
#: —— 判定调用用 **AST**（注释与文档字符串里的 `i18n.t(` 不算调用；`def log_user(` 也不算）
def _i18n_calls(source: str) -> set:
    """返回文件里真正**调用**了哪些 i18n 名字：{"t"} / {"log_user"} / {"i18n.t"}。"""
    import ast as _ast

    found = set()
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return found
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        if isinstance(func, _ast.Name) and func.id in ("_t", "t", "log_user"):
            found.add(func.id)
        elif isinstance(func, _ast.Attribute) and func.attr == "t" \
                and isinstance(func.value, _ast.Name) and func.value.id == "i18n":
            found.add("i18n.t")
    return found


#: 导入名：**行尾注释靠正则剥离**（`([^#\n]+)`），不能要求整行只剩名字 ——
#: `from i18n import t as _t  # 延迟导入…` 那种写法第一次就被误报成"没导入"。
_IMPORT_RE = re.compile(r"^\s*from\s+i18n\s+import\s+([^#\n]+)", re.M)
_PLAIN_IMPORT_RE = re.compile(r"^\s*import\s+i18n\s*(?:#.*)?$", re.M)


def _import_selfcheck(root: str) -> int:
    """返回问题条数（0 = 通过）。"""
    problems = []
    checked = 0
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        if name == "i18n.py":
            # 定义处：`t` / `log_user` 就是本模块定义的，自己调用自己当然不需要导入
            continue
        path = os.path.join(root, name)
        source = io.open(path, encoding="utf-8", errors="replace").read()
        checked += 1
        bound = set()
        for match in _IMPORT_RE.finditer(source):
            for piece in match.group(1).split(","):
                piece = piece.strip()
                if " as " in piece:
                    bound.add(piece.split(" as ")[-1].strip())
                else:
                    bound.add(piece.split(".")[0].strip())
        has_plain = bool(_PLAIN_IMPORT_RE.search(source))
        calls = _i18n_calls(source)
        needs = []
        # `t` 与 `_t` 是同一个名字的两种写法（`from i18n import t` / `... import t as _t`）
        if ({"t", "_t"} & calls) and not ({"t", "_t"} & bound):
            needs.append("`_t(`（需要 `from i18n import t as _t`）")
        if "log_user" in calls and "log_user" not in bound:
            needs.append("`log_user(`（需要 `from i18n import log_user`）")
        if "i18n.t" in calls and not has_plain:
            needs.append("`i18n.t(`（需要 `import i18n`）")
        if needs:
            problems.append((name, needs))

    print()
    print("-- i18n 导入自检（用 _t(/log_user( 却没导入）")
    print(f"   受检文件 {checked} 个；问题 {len(problems)} 个")
    for name, needs in problems:
        print(f"     ? backend/{name}：{'、'.join(needs)}")
    if not problems:
        print("   ✓ 所有用到 `_t(` / `log_user(` 的文件都已有对应导入")
    return len(problems)


_i18n_import_problems = _import_selfcheck(os.path.join(ROOT, "backend"))
if _i18n_import_problems:
    sys.exit(1)

sys.exit(1 if ((missing_en or missing_zh) or extra_problems) else 0)
