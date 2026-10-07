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
sys.exit(1 if (missing_en or missing_zh) else 0)
