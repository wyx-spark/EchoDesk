"""平台探测与能力边界（任务书 任务三 / 任务五）。

这是后端**唯一**回答"我在哪个平台、这个平台上哪些能力不可能有、替代做法是什么"的地方。

为什么要单独立一个模块，而不是到处写 ``if os.name == "nt"``：

* 任务五要求降级时给出**明确文案 + 替代路径**，文案要说清"为什么没有"，所以能力边界
  必须是一份可枚举的清单（``capabilities()``），而不是散落的 if；
* 任务一要求前端与后端对平台的判断一致：``ready`` 行里上报本模块的 ``report()``，
  前端（Avalonia 版 ``Services/PlatformInfo.cs``）拿它和本机探测结果比对，不一致就是 bug；
* 打包与文档（README / USER_GUIDE）也要引用同一份清单，避免"文档说有、代码里没有"。

本机只有 Windows 可用（无 WSL/Docker/macOS），因此非 Windows 分支的行为靠
**强制平台注入**测试覆盖（``ECHODESK_FORCE_PLATFORM``，见 tools/test_platform.py），
而不是靠"应该没问题"。
"""
from __future__ import annotations

import os
import platform as _platform
import sys
from typing import Any, Dict, List, Optional

WINDOWS = "windows"
MACOS = "macos"
LINUX = "linux"
UNKNOWN = "unknown"

#: 仅 Windows 可用的能力。键是机器名，值是 i18n 文案键前缀。
#: 新增"只在某平台才有"的能力时必须登记在这里，否则前端横幅与文档都收不到它。
WINDOWS_ONLY_FEATURES = (
    "wps_com",       # 用 WPS 的 COM 接口读加密文档
    "automation",    # 企业内网自动化（Playwright 驱动本机 Edge，有头模式）
)

#: 全平台可用的能力（列出来是为了让"能力清单"是完整的，而不是只有负项）。
PORTABLE_FEATURES = (
    "chat",          # 对话与流式输出
    "files",         # 文件解析（通用解析器）
    "retrieval",     # 本地检索（RAG）
    "agent",         # Agent 平台
    "mcp",           # MCP 客户端
    "skills",        # 插件/技能
    "models",        # 模型管理与本地推理
)


def _forced_kind() -> str:
    """测试用：``ECHODESK_FORCE_PLATFORM=linux`` 可强制平台判断。

    为什么需要它：验证非 Windows 的降级路径必须能在 Windows 开发机上跑，
    否则"降级"永远只有一句声明、没有证据（本仓库只有 Windows 运行环境）。
    """
    forced = (os.environ.get("ECHODESK_FORCE_PLATFORM") or "").strip().lower()
    if forced in (WINDOWS, MACOS, LINUX, UNKNOWN):
        return forced
    return ""


def kind() -> str:
    """平台大类：windows / macos / linux / unknown（与前端 PlatformInfo.KindName 同名）。"""
    forced = _forced_kind()
    if forced:
        return forced
    if sys.platform.startswith("win"):
        return WINDOWS
    if sys.platform == "darwin":
        return MACOS
    if sys.platform.startswith("linux"):
        return LINUX
    return UNKNOWN


def is_windows() -> bool:
    return kind() == WINDOWS


def is_forced() -> bool:
    """当前判断是否来自强制注入（报告里会标注，免得把测试结果当成真实平台）。"""
    return bool(_forced_kind())


def name() -> str:
    """给人看的平台名（界面横幅用）。"""
    from i18n import t as _t  # 延迟导入：与本文件 _cap_reason() 同样的理由（避免模块级循环依赖）

    return {
        WINDOWS: "Windows",
        MACOS: "macOS",
        LINUX: "Linux",
    }.get(kind(), _t("platform.unknown_name", platform=sys.platform))


def architecture() -> str:
    """CPU 架构：x86_64 / arm64 / …（统一成短名，便于和 RID 拼接）。"""
    machine = (_platform.machine() or "").lower()
    return {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "i386": "x86",
        "i686": "x86",
    }.get(machine, machine or "unknown")


def rid() -> str:
    """.NET RID（win-x64 / linux-x64 / osx-arm64…）：发布脚本与 CI 矩阵按它切片。"""
    arch = architecture()
    suffix = {
        "x86_64": "x64",
        "arm64": "arm64",
        "x86": "x86",
    }.get(arch, arch)
    prefix = {WINDOWS: "win", MACOS: "osx", LINUX: "linux"}.get(kind(), "unknown")
    return f"{prefix}-{suffix}"


def os_description() -> str:
    """带版本号的系统描述（排障用）。"""
    try:
        return f"{_platform.system()} {_platform.release()} ({_platform.version()})"
    except Exception:  # noqa: BLE001 - 极端环境取不到就返回空串，不让它阻断启动
        return ""


def python_info() -> Dict[str, Any]:
    """解释器信息（打包后才看得出来是冻结环境还是源码运行）。"""
    return {
        "version": _platform.python_version(),
        "implementation": _platform.python_implementation(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
    }


def wps_com_available() -> bool:
    """WPS COM 是否真的可用：平台必须是 Windows，且 win32com 能导入。

    两段判断都要做：Windows 上也可能没装 pywin32（或装了但缺 WPS），
    此时应当降级到通用解析器并说明原因，而不是让用户在"文档解析失败"上猜。
    """
    if not is_windows():
        return False
    try:
        import win32com.client  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺 pywin32 属正常降级
        return False
    return True


def automation_available() -> bool:
    """企业内网自动化是否可用：Windows 专属（依赖本机 Edge 的有头模式）。

    这里刻意**不**检查 playwright 是否安装 —— 那是"依赖没装"（有明确报错文案），
    与本平台的"能力不存在"（任务五的降级边界）是两件事，混在一起会让用户以为
    装个包就能在 macOS 上跑，而实际不行。
    """
    return is_windows()


def _cap_reason(feature: str) -> str:
    """取能力的说明文案（i18n）。延迟导入 i18n 以避免模块级循环依赖。"""
    try:
        from i18n import t  # type: ignore

        return t(f"platform.{feature}")
    except Exception:  # noqa: BLE001 - 文案取不到时给机器名，绝不返回空串
        return feature


def _cap_alternative(feature: str) -> str:
    try:
        from i18n import t  # type: ignore

        return t(f"platform.{feature}_alt")
    except Exception:  # noqa: BLE001
        return ""


def capabilities() -> Dict[str, Dict[str, Any]]:
    """能力清单：``{能力名: {available, reason, alternative, windows_only}}``。

    全平台能力与 Windows 专属能力都列出来 —— 降级说明只有放在完整清单里才读得懂。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for feature in PORTABLE_FEATURES:
        out[feature] = {
            "available": True,
            "reason": _cap_reason(feature),
            "alternative": "",
            "windows_only": False,
        }

    wps = wps_com_available()
    wps_reason = _cap_reason("wps_com")
    if is_windows() and not wps:
        # Windows 上也可能是"装了系统但没装 pywin32"：这与平台限制要分开说
        wps_reason = _cap_reason("wps_com_no_pywin32")
    out["wps_com"] = {
        "available": wps,
        "reason": wps_reason,
        "alternative": "" if wps else _cap_alternative("wps_com"),
        "windows_only": True,
    }

    automation = automation_available()
    out["automation"] = {
        "available": automation,
        "reason": _cap_reason("automation") if automation else _cap_reason("automation_unavailable"),
        "alternative": "" if automation else _cap_alternative("automation"),
        "windows_only": True,
    }
    return out


def degraded_features() -> List[str]:
    """本平台上不可用的 Windows 专属能力（界面横幅直接用它）。"""
    return [f for f in WINDOWS_ONLY_FEATURES if not capabilities().get(f, {}).get("available")]


def report() -> Dict[str, Any]:
    """上报给前端的完整平台报告（``ready.platform`` 与 ``platform`` 动作都用它）。"""
    caps = capabilities()
    return {
        "kind": kind(),
        "name": name(),
        "os": os_description(),
        "architecture": architecture(),
        "rid": rid(),
        "python": python_info(),
        "forced": is_forced(),          # 强制注入时前端/日志要能看出来这不是真实平台
        "capabilities": caps,
        "degraded": degraded_features(),
        "user_data_root": _user_data_root_safe(),
    }


def _user_data_root_safe() -> str:
    """当前用户数据目录（报告里带上它，便于用户找到日志与结果文件）。"""
    try:
        from config import user_data_root  # type: ignore

        return user_data_root()
    except Exception:  # noqa: BLE001
        return ""


def summary_line() -> str:
    """一行摘要（日志启动横幅用，与前端 PlatformInfo.Summary() 同口径）。"""
    return f"{name()} / {architecture()} / {rid()} / Python {_platform.python_version()}"


def find_python() -> Optional[str]:
    """找一个可用的 Python 解释器（前端在未打包时用它拉起后端）。

    前端自己也会找（PlatformInfo.FindPython），这里保留一份是为了：
    打包脚本/CI 与"后端自己需要重启自己"的场合不必依赖前端。
    """
    import shutil

    for candidate in ("python3", "python"):
        found = shutil.which(candidate)
        if found:
            return found
    return None
