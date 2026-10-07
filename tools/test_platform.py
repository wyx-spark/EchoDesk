# -*- coding: utf-8 -*-
"""跨平台能力边界测试（v0.2.1 跨平台 任务三 / 任务五）。

**这组测试要解决一个诚实性问题**：本机只有 Windows，Linux/macOS 既跑不了图形界面，
也装不了（无 WSL/Docker）。于是"非 Windows 上会怎么降级"如果只靠"应该是这样"，
就等于没有证据。做法是给 `platform_support` 加一个显式的平台注入开关
（环境变量 `ECHODESK_FORCE_PLATFORM`），在 Windows 上把平台判断强制成 linux/macos，
再断言降级行为——**并且报告里会标记 forced，避免把测试结果当成真实平台**。

| 任务书要求 | 对应断言 |
| --- | --- |
| 平台探测（OS / 架构 / 数据目录） | kind/name/architecture/rid/python/user_data_root 齐全，RID 与 kind 对得上 |
| 能力按平台降级 | 强制 linux/macos → WPS COM 与自动化不可用，且都给替代做法 |
| 降级要有明确文案 | 读 .doc 时返回"仅 Windows 提供"+替代做法，而不是笼统的"解析失败" |
| 依赖缺失与平台限制要分清 | 强制非 Windows 说"平台不支持"；Windows 缺 pywin32 说"缺 pywin32" |
| 自动化页面按平台禁用 | 强制非 Windows 时 validate() 报 platform_unsupported；Windows 上不报 |
| 协议层照样通 | `platform` 动作返回同一份报告；ready 行带 platform |

跑法：``python tools\\test_platform.py``；退出码 0 = 全部通过。
"""
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def force(platform):
    """强制平台判断（None = 恢复真实判断）。"""
    if platform is None:
        os.environ.pop("ECHODESK_FORCE_PLATFORM", None)
    else:
        os.environ["ECHODESK_FORCE_PLATFORM"] = platform
    # 注意：platform_support 每次调用都读环境变量，不做缓存 —— 这正是能被测试注入的原因


# 必须在 import 之前设好真实平台，供后面恢复用
REAL_IS_WINDOWS = sys.platform.startswith("win")
force(None)

import platform_support as ps      # noqa: E402
import i18n                        # noqa: E402

i18n.set_locale("zh-Hans")

print("=== 1. 平台报告字段齐全，且 RID 与平台名对得上 ===")
report = ps.report()
for field in ("kind", "name", "os", "architecture", "rid", "python",
              "capabilities", "degraded", "user_data_root", "forced"):
    check(f"报告里有 {field}", field in report, str(sorted(report.keys())))
check("真实判断时 forced=False", report["forced"] is False)
check("kind 与实际运行平台一致",
      report["kind"] == ("windows" if REAL_IS_WINDOWS else "linux"), report["kind"])
_prefix = {"windows": "win-", "macos": "osx-", "linux": "linux-"}[report["kind"]]
check("RID 前缀与平台一致", report["rid"].startswith(_prefix), report["rid"])
check("RID 带架构后缀", report["rid"].split("-")[-1] in ("x64", "x86", "arm64", "arm"), report["rid"])
check("python 信息里有解释器版本", bool(report["python"].get("version")), str(report["python"]))
check("用户数据目录非空", bool(report["user_data_root"]), report["user_data_root"])
check("summary_line 含平台名与 RID",
      report["name"] in ps.summary_line() and report["rid"] in ps.summary_line(), ps.summary_line())

print("=== 2. 能力清单：全平台能力都在，Windows 专属能力按平台给出 ===")
_caps = report["capabilities"]
for feature in ps.PORTABLE_FEATURES:
    check(f"跨平台能力 {feature} 标记为可用", _caps.get(feature, {}).get("available") is True,
          str(_caps.get(feature)))
for feature in ps.WINDOWS_ONLY_FEATURES:
    check(f"Windows 专属能力 {feature} 出现在清单里", feature in _caps)
check("能力说明不是裸键（i18n 命中了）",
      all(not str(v.get("reason", "")).startswith("platform.") for v in _caps.values()),
      str({k: v.get("reason") for k, v in _caps.items()})[:200])

print("=== 3. 强制 Linux：两个 Windows 专属能力都要降级且给替代做法 ===")
force("linux")
_linux = ps.report()
check("强制后 kind=linux 且 forced=True", _linux["kind"] == "linux" and _linux["forced"] is True,
      f"{_linux['kind']} forced={_linux['forced']}")
check("RID 变成 linux-*", _linux["rid"].startswith("linux-"), _linux["rid"])
check("WPS COM 标记不可用", _linux["capabilities"]["wps_com"]["available"] is False)
check("自动化标记不可用", _linux["capabilities"]["automation"]["available"] is False)
check("降级清单列出两项", sorted(_linux["degraded"]) == sorted(ps.WINDOWS_ONLY_FEATURES),
      str(_linux["degraded"]))
check("WPS 降级说明指出平台限制",
      "Windows" in _linux["capabilities"]["wps_com"]["reason"], _linux["capabilities"]["wps_com"]["reason"])
check("WPS 降级给了替代做法",
      len(_linux["capabilities"]["wps_com"]["alternative"]) > 10,
      _linux["capabilities"]["wps_com"]["alternative"])
check("自动化降级给了替代做法（MCP/插件）",
      "MCP" in _linux["capabilities"]["automation"]["alternative"],
      _linux["capabilities"]["automation"]["alternative"])
check("跨平台能力在 Linux 上仍然可用",
      all(_linux["capabilities"][f]["available"] for f in ps.PORTABLE_FEATURES))

print("=== 4. 强制 macOS：同样降级（不是只对 Linux 生效）===")
force("macos")
_mac = ps.report()
check("kind=macos", _mac["kind"] == "macos", _mac["kind"])
check("RID 是 osx-*", _mac["rid"].startswith("osx-"), _mac["rid"])
check("macOS 上 WPS COM 不可用", _mac["capabilities"]["wps_com"]["available"] is False)
check("macOS 上自动化不可用", _mac["capabilities"]["automation"]["available"] is False)

print("=== 5. 未知平台：不假装认识，但也不阻止启动 ===")
force("unknown")
_unknown = ps.report()
check("kind=unknown", _unknown["kind"] == "unknown", _unknown["kind"])
check("未知平台也不崩（RID 仍可拼出）", bool(_unknown["rid"]), _unknown["rid"])
check("未知平台按「能力最少」处理（两个 Windows 专属能力都不可用）",
      not _unknown["capabilities"]["wps_com"]["available"]
      and not _unknown["capabilities"]["automation"]["available"])
force(None)

print("=== 6. 文件解析：非 Windows 上读旧版格式给出「平台不支持 + 替代做法」 ===")
import file_parser as fp          # noqa: E402

workdir = tempfile.mkdtemp(prefix="echodesk-platform-")
doc_path = os.path.join(workdir, "legacy.doc")
with io.open(doc_path, "wb") as handle:
    handle.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy-binary-not-really-doc")

force("linux")
_linux_doc = fp.parse_file_with_wps(doc_path)
check("非 Windows 读 .doc 明确说「仅 Windows 提供」",
      "仅 Windows" in _linux_doc, _linux_doc[:160])
check("并且给出替代做法（另存为 / 技能 / MCP）",
      "另存为" in _linux_doc and "技能" in _linux_doc, _linux_doc[:200])
check("不是笼统的「解析失败」（用户看不出该怎么办）",
      not _linux_doc.startswith("解析失败"), _linux_doc[:80])
check("也没有把「装 WPS 后重试」这条 Windows 建议误伤给 Linux 用户",
      "请安装 WPS/Office 后重试" not in _linux_doc, _linux_doc[:200])

check("_wps_available() 在强制非 Windows 时为 False", fp._wps_available() is False)
force(None)
_win_doc = fp.parse_file_with_wps(doc_path)
if REAL_IS_WINDOWS:
    # Windows 上原因必须是"缺 pywin32"或真的去调 COM 了，不能是"平台不支持"
    check("Windows 上不会把原因归为平台限制", "仅 Windows" not in _win_doc, _win_doc[:160])
else:
    check("非 Windows 真实环境下仍是平台限制", "仅 Windows" in _win_doc, _win_doc[:160])
force(None)

# 回归保护：加了容错导入之后，普通文本/未知格式的老路径不能坏
txt_path = os.path.join(workdir, "note.txt")
with io.open(txt_path, "w", encoding="utf-8") as handle:
    handle.write("跨平台解析回归检查")
check("普通文本文件照常解析",
      "跨平台解析回归检查" in fp.parse_file_with_wps(txt_path),
      fp.parse_file_with_wps(txt_path)[:80])
# v0.2.2 硬编码清查第 6 批（一）：解析失败串改成「语言无关标记 + 语言包正文」，
# 所以不能按中文前缀断言（英文界面下正文不是中文）。改成先判"是失败串"、再取当前语言正文来断言。
_zzz = fp.parse_file_with_wps(os.path.join(workdir, "x.zzz"))
check("不支持的扩展名照常报「不支持」",
      fp.is_parse_error(_zzz) and "不支持" in fp.localize_error(_zzz),
      _zzz[:60])

print("=== 7. 自动化：非 Windows 上 validate() 说明原因，不是「没装 Playwright」 ===")
import automation as am           # noqa: E402
import config as cf               # noqa: E402

# AutomationEngine 的字段是只读属性（代理到 cfg），所以配置要设在 cfg 上
_cfg = cf.AutomationConfig()
_cfg.enabled = True
_cfg.safety_accepted = True
_cfg.task_list_url = "https://example.invalid/tasks"
_engine = am.AutomationEngine(_cfg)
force("linux")
_linux_errors = _engine.validate()
check("非 Windows 上给出平台不支持的错误",
      any("Windows" in e for e in _linux_errors), str(_linux_errors))
check("并且说明这不是依赖缺失",
      any("不是依赖缺失" in e for e in _linux_errors), str(_linux_errors))
force(None)
_win_errors = _engine.validate()
if REAL_IS_WINDOWS:
    check("Windows 上不会出现平台不支持这条",
          not any("平台不支持" in e for e in _win_errors), str(_win_errors))

print("=== 8. 后端接线：platform 动作返回同一份报告（协议层不变）===")
import main as backend_main       # noqa: E402

os.chdir(workdir)
_backend = backend_main.Backend(cf.Config())
_reply = _backend.handle({"action": "platform"})
check("platform 动作有返回", isinstance(_reply, dict), str(type(_reply)))
_report = _reply.get("platform") or {}
check("返回体带 platform 报告", _report.get("kind") == ps.kind(), str(_report.get("kind")))
check("报告里带能力清单", isinstance(_report.get("capabilities"), dict)
      and "wps_com" in _report["capabilities"], str(list((_report.get("capabilities") or {}).keys())))

# ready 行里也必须带 platform：前端启动时一次就能拿到，不必额外请求
_ready_src = io.open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
check("ready 行里带 platform 字段（前端启动即可拿到降级依据）",
      '"platform": platform_support.report()' in _ready_src)

print("=== 9. 语言包：新增的 platform.* 文案两套语言都有 ===")
_zh = i18n.load_pack("zh-Hans")
_en = i18n.load_pack("en")
for key in ("platform.wps_com", "platform.wps_com_alt", "platform.automation",
            "platform.automation_unavailable", "platform.automation_alt"):
    check(f"zh-Hans 有 {key}", key in _zh)
    check(f"en 有 {key}", key in _en)
check("en 的自动化替代做法是英文（没有漏译成中文）",
      "Workaround" in _en.get("platform.automation_alt", ""), _en.get("platform.automation_alt", "")[:80])

print()
print("=" * 60)
print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
if FAILED:
    for name in FAILED:
        print(f"  - {name}")
sys.exit(0 if not FAILED else 1)
