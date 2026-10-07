# -*- coding: utf-8 -*-
"""清点脚本：对比两个前端各自用到的后端动作名（只读，不写任何文件）。

用途：作为「先清点、不写码」那一步的机械证据 —— 光看控件名容易漏，
而**后端动作名**是最硬的锚：某个动作只有 WPF 调用、Avalonia 从不调用，
那片功能就一定是整块缺失。

为什么它**不**进 tools/verify_release.ps1（与 tools/audit_lang_keys.py 的分工）
----------------------------------------------------------------------------
它的输出是**两份差集**，而"两个前端用到的动作不同"本身就是预期状态
（WPF 专属控件、已在记录里登记的未迁移项都会体现为差集），
没有"应该相等"这个前提，也就没有可判定的通过/失败 —— 除非引入一份基线清单，
那是另一个任务。所以它保持为**人来读的清点工具**，不挂门禁。

来源：v0.2.2 的 ``artifacts/_inventory_actions.py``（当时是开发期临时脚本）。
本轮移入 ``tools/``；**判定逻辑未改**，只把"相对当前工作目录"的路径改成
"相对本文件推导出的仓库根"，否则从别的目录调用会静默清点出 0 个动作。

用法
----
    python tools/inventory_frontend_actions.py
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PATTERN = re.compile(r'action\s*=\s*"([a-z_0-9]+)"')
PER_FRONTEND = {}

for label, root in (("WPF", "frontend"), ("AVALONIA", "frontend-avalonia")):
    found = {}
    for base, _, files in os.walk(os.path.join(ROOT, root)):
        for name in files:
            if not name.endswith((".cs", ".xaml")):
                continue
            path = os.path.join(base, name)
            try:
                with io.open(path, encoding="utf-8") as fh:
                    source = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for match in PATTERN.finditer(source):
                found.setdefault(match.group(1), set()).add(
                    os.path.relpath(path, ROOT).replace("\\", "/"))
    PER_FRONTEND[label] = found
    print("%s 用到的动作数：%d" % (label, len(found)))

wpf = PER_FRONTEND["WPF"]
avalonia = PER_FRONTEND["AVALONIA"]

print()
print("== 只有 WPF 调用（Avalonia 没有）==")
for action in sorted(set(wpf) - set(avalonia)):
    print("  %-32s %s" % (action, ", ".join(sorted(wpf[action]))))

print()
print("== 只有 Avalonia 调用 ==")
for action in sorted(set(avalonia) - set(wpf)):
    print("  %-32s %s" % (action, ", ".join(sorted(avalonia[action]))))
