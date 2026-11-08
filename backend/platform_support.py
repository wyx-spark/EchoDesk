"""
[EN]
Platform detection and capability boundaries.

This is the single backend authority for the current platform, the capabilities
that cannot exist on that platform, and the supported alternatives. Keeping the
boundaries enumerable (``capabilities()``) ensures that degradation messages
explain both the cause and the alternative path, and that the frontend platform
report is compared against the same source of truth. Packaging and documentation
also consume this list, preventing documentation from claiming capabilities that
the code does not provide.

Non-Windows branches are covered by forced platform injection
(``ECHODESK_FORCE_PLATFORM``; see tools/test_platform.py) rather than by an
untested assumption.

[ZH]
平台探测与能力边界。

本模块是后端唯一负责回答“当前平台是什么、该平台有哪些能力不可能存在、替代做法是什么”
的位置。能力边界以可枚举清单（``capabilities()``）的形式提供，使降级文案能够同时说明
原因与替代路径，并让前端平台报告与后端使用同一份事实来源。打包与文档也引用该清单，
避免文档宣称代码并不提供的能力。

非 Windows 分支通过强制平台注入（``ECHODESK_FORCE_PLATFORM``，见
tools/test_platform.py）覆盖，而不是依赖未经测试的假设。
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

#: [EN] Capabilities available only on Windows. New platform-exclusive
#: [EN] capabilities must be registered here so that the frontend banner and
#: [EN] documentation receive them from the same source.
#: [ZH] 仅 Windows 可用的能力。新增平台专属能力时必须登记在此，使前端横幅与
#: [ZH] 文档从同一份来源获取。
WINDOWS_ONLY_FEATURES = (
    "wps_com",       # [EN] Read encrypted documents through the WPS COM API. / [ZH] 通过 WPS COM 接口读取加密文档。
    "automation",    # [EN] Enterprise browser automation (headed Edge via Playwright). / [ZH] 企业内网自动化（Playwright 驱动本机 Edge，有头模式）。
)

#: [EN] Capabilities available on every platform. They are listed explicitly so
#: [EN] that the capability inventory is complete rather than containing only
#: [EN] unsupported entries.
#: [ZH] 全平台可用的能力。显式列出这些能力，使能力清单完整，而不是只包含负项。
PORTABLE_FEATURES = (
    "chat",          # [EN] Chat and streaming output. / [ZH] 对话与流式输出。
    "files",         # [EN] File parsing through the generic parser. / [ZH] 通用解析器文件解析。
    "retrieval",     # [EN] Local retrieval (RAG). / [ZH] 本地检索（RAG）。
    "agent",         # [EN] Agent execution platform. / [ZH] Agent 执行平台。
    "mcp",           # [EN] MCP client. / [ZH] MCP 客户端。
    "skills",        # [EN] Plugins and skills. / [ZH] 插件与技能。
    "models",        # [EN] Model management and local inference. / [ZH] 模型管理与本地推理。
)


def _forced_kind() -> str:
    """
    [EN]
    Test hook: ``ECHODESK_FORCE_PLATFORM=linux`` forces the platform decision.

    The hook exists because non-Windows degradation paths must be executable on
    a Windows development host; otherwise the degradation claim would have no
    verification evidence in this repository.

    [ZH]
    测试钩子：``ECHODESK_FORCE_PLATFORM=linux`` 可强制平台判断。

    该钩子用于在 Windows 开发机上执行非 Windows 降级路径；否则本仓库中的降级
    声明将缺少可执行证据。
    """
    forced = (os.environ.get("ECHODESK_FORCE_PLATFORM") or "").strip().lower()
    if forced in (WINDOWS, MACOS, LINUX, UNKNOWN):
        return forced
    return ""


def kind() -> str:
    """
    [EN]
    Platform category: windows, macos, linux, or unknown. The value matches the
    frontend ``PlatformInfo.KindName`` contract.

    [ZH]
    平台大类：windows、macos、linux 或 unknown。该值与前端
    ``PlatformInfo.KindName`` 的契约一致。
    """
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
    """
    [EN]
    Return whether the current decision came from forced injection. Reports mark
    this so that test results are not mistaken for the real platform.

    [ZH]
    返回当前判断是否来自强制注入。报告会标注该状态，避免把测试结果误认为真实平台。
    """
    return bool(_forced_kind())


def name() -> str:
    """
    [EN]
    Human-readable platform name used by interface banners.

    [ZH]
    供界面横幅显示的平台可读名称。
    """
    # [EN] Delayed import, for the same reason as _cap_reason(): avoid a module-level import cycle.
    # [ZH] 延迟导入，原因与 _cap_reason() 相同：避免模块级循环导入。
    from i18n import t as _t

    return {
        WINDOWS: "Windows",
        MACOS: "macOS",
        LINUX: "Linux",
    }.get(kind(), _t("platform.unknown_name", platform=sys.platform))


def architecture() -> str:
    """
    [EN]
    CPU architecture: x86_64, arm64, and so on. Names are normalized so that the
    value can be composed into a .NET RID.

    [ZH]
    CPU 架构：x86_64、arm64 等。名称会统一为短名，便于拼接 .NET RID。
    """
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
    """
    [EN]
    .NET RID such as win-x64, linux-x64, or osx-arm64. Release scripts and the CI
    matrix use this value as their slice key.

    [ZH]
    .NET RID，例如 win-x64、linux-x64 或 osx-arm64。发布脚本与 CI 矩阵以该值
    作为切片键。
    """
    arch = architecture()
    suffix = {
        "x86_64": "x64",
        "arm64": "arm64",
        "x86": "x86",
    }.get(arch, arch)
    prefix = {WINDOWS: "win", MACOS: "osx", LINUX: "linux"}.get(kind(), "unknown")
    return f"{prefix}-{suffix}"


def os_description() -> str:
    """
    [EN]
    Operating-system description including its version, used for diagnostics.

    [ZH]
    带版本号的系统描述，用于故障排查。
    """
    try:
        return f"{_platform.system()} {_platform.release()} ({_platform.version()})"
    # [EN] Return an empty string when the platform cannot be queried; diagnostics must not block startup.
    # [ZH] 平台信息无法获取时返回空串；诊断信息不得阻断启动。
    except Exception:  # noqa: BLE001
        return ""


def python_info() -> Dict[str, Any]:
    """
    [EN]
    Interpreter information. It distinguishes a frozen build from a source-tree
    run after packaging.

    [ZH]
    解释器信息。打包后可据此区分冻结构建与源码运行。
    """
    return {
        "version": _platform.python_version(),
        "implementation": _platform.python_implementation(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
    }


def wps_com_available() -> bool:
    """
    [EN]
    Return whether WPS COM is actually usable: the platform must be Windows and
    ``win32com`` must be importable.

    Both conditions are required. A Windows host may lack pywin32, or may have
    pywin32 but no WPS. In either case the generic parser is used and the reason
    is reported explicitly instead of leaving the user to infer why parsing failed.

    [ZH]
    返回 WPS COM 是否实际可用：平台必须为 Windows，且 ``win32com`` 必须可导入。

    两个条件都必须满足。Windows 主机可能未安装 pywin32，或已安装 pywin32 但缺少
    WPS。此时应降级到通用解析器并明确报告原因，而不是让用户自行推测解析失败的原因。
    """
    if not is_windows():
        return False
    try:
        import win32com.client  # noqa: F401
    # [EN] A missing pywin32 is a normal degradation path.
    # [ZH] 缺少 pywin32 属于正常降级路径。
    except Exception:  # noqa: BLE001
        return False
    return True


def automation_available() -> bool:
    """
    [EN]
    Return whether enterprise browser automation is available. It is Windows-only
    because it depends on a headed instance of the locally installed Edge.

    This function deliberately does not check whether Playwright is installed.
    A missing dependency has its own explicit error message and is distinct from
    a capability that cannot exist on the current platform; conflating the two
    would suggest that installing a package enables automation on macOS.

    [ZH]
    返回企业内网自动化是否可用。该能力仅限 Windows，因为它依赖本机已安装 Edge
    的有头实例。

    本函数刻意不检查 Playwright 是否安装。依赖缺失具有独立的明确错误文案，
    与当前平台不支持该能力是两类问题；混淆二者会使用户误以为安装依赖即可在
    macOS 上运行自动化。
    """
    return is_windows()


def _cap_reason(feature: str) -> str:
    """
    [EN]
    Return the localized capability description. The i18n import is delayed to
    avoid a module-level import cycle.

    [ZH]
    返回本地化的能力说明。延迟导入 i18n，以避免模块级循环导入。
    """
    try:
        from i18n import t  # type: ignore

        return t(f"platform.{feature}")
    # [EN] If the localized text is unavailable, return the machine name rather than an empty string.
    # [ZH] 本地化文案不可用时返回机器名，绝不返回空串。
    except Exception:  # noqa: BLE001
        return feature


def _cap_alternative(feature: str) -> str:
    try:
        from i18n import t  # type: ignore

        return t(f"platform.{feature}_alt")
    # [EN] An unavailable alternative is represented by an empty string.
    # [ZH] 替代路径不可用时以空串表示。
    except Exception:  # noqa: BLE001
        return ""


def capabilities() -> Dict[str, Dict[str, Any]]:
    """
    [EN]
    Capability inventory in the form
    ``{capability: {available, reason, alternative, windows_only}}``.

    Portable and Windows-only capabilities are both included. Degradation
    messages are only interpretable when presented in a complete inventory.

    [ZH]
    能力清单，结构为
    ``{能力名: {available, reason, alternative, windows_only}}``。

    全平台能力与 Windows 专属能力都会列出。降级说明只有放在完整清单中才具有
    可解释性。
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
        # [EN] On Windows the cause may also be "the system is installed but
        # [EN] pywin32 is not": this must be kept separate from a platform
        # [EN] limitation.
        # [ZH] Windows 上也可能是"装了系统但没装 pywin32"：这与平台限制要分开说。
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
    """
    [EN]
    Windows-only capabilities that are unavailable on the current platform. The
    interface banner consumes this list directly.

    [ZH]
    当前平台上不可用的 Windows 专属能力。界面横幅直接使用该列表。
    """
    return [f for f in WINDOWS_ONLY_FEATURES if not capabilities().get(f, {}).get("available")]


def report() -> Dict[str, Any]:
    """
    [EN]
    Return the complete platform report consumed by the frontend through
    ``ready.platform`` and the ``platform`` action.

    [ZH]
    返回供前端使用的完整平台报告，``ready.platform`` 与 ``platform`` 动作均使用它。
    """
    caps = capabilities()
    return {
        "kind": kind(),
        "name": name(),
        "os": os_description(),
        "architecture": architecture(),
        "rid": rid(),
        "python": python_info(),
        # [EN] When injection is active, the frontend and logs must be able to distinguish it from the real platform.
        # [ZH] 启用强制注入时，前端与日志必须能够将其与真实平台区分。
        "forced": is_forced(),
        "capabilities": caps,
        "degraded": degraded_features(),
        "user_data_root": _user_data_root_safe(),
    }


def _user_data_root_safe() -> str:
    """
    [EN]
    Return the current user data directory. The report includes it so that users
    can locate logs and result files.

    [ZH]
    返回当前用户数据目录。报告会包含该路径，便于用户定位日志与结果文件。
    """
    try:
        from config import user_data_root  # type: ignore

        return user_data_root()
    # [EN] An unavailable data directory is represented by an empty string.
    # [ZH] 数据目录不可用时以空串表示。
    except Exception:  # noqa: BLE001
        return ""


def summary_line() -> str:
    """
    [EN]
    Return a one-line summary for the startup log banner. It follows the same
    contract as the frontend ``PlatformInfo.Summary()``.

    [ZH]
    返回用于启动日志横幅的一行摘要，与前端 ``PlatformInfo.Summary()`` 保持同一契约。
    """
    return f"{name()} / {architecture()} / {rid()} / Python {_platform.python_version()}"


def find_python() -> Optional[str]:
    """
    [EN]
    Locate a usable Python interpreter. The frontend uses it to start the backend
    from a source tree.

    The frontend has its own lookup (``PlatformInfo.FindPython``). The backend
    keeps an independent implementation so that packaging scripts, CI, and
    backend self-restart paths do not depend on the frontend.

    [ZH]
    查找可用的 Python 解释器。前端在源码树运行时用它启动后端。

    前端也具有独立实现（``PlatformInfo.FindPython``）。后端保留一份独立实现，
    使打包脚本、CI 与后端自重启路径不必依赖前端。
    """
    import shutil

    for candidate in ("python3", "python"):
        found = shutil.which(candidate)
        if found:
            return found
    return None
