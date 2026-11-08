"""
[EN]
Version metadata for EchoDesk.

[ZH]
EchoDesk 版本与署名信息。

版本号口径（v0.2.2 起明确）：
  * **此前版本 = 跨平台之前**的版本 —— 不含 `frontend-avalonia/`，也没有平台抽象；
    标签 `此前版本` 指向那个状态的最后一个提交，它的安装包与发布物由该提交重建。
  * **v0.2.2 = 跨平台版本** —— 跨平台前端（Avalonia）迁移 + 后端平台化 +
    三平台（Windows/Linux/macOS）后端分发与打包 + 两条降级边界。
不要把这两者混起来。
`records/` 下的文件名一律用**发布版本号**（如 `v0.2.2-跨平台-2记录.md`），
**不要**用撰写时的设计规范编号 —— 维护者在 v0.2.2 交付阶段更正过这条口径：
按设计规范编号命名会让"按版本号找记录"直接找不到，而改名的一次性成本（批量更新引用）
远小于归属错误带来的长期代价。设计规范编号只在记录正文里引用，不进文件名。
"""
APP_NAME = "EchoDesk"
# [EN] v0.2.5 = "Release hardening". Writable state paths for read-only mounts,
# [EN] a smaller and faster release pipeline, and a repository-wide bilingual
# [EN] normalization of comments, docstrings and UI strings. No user-facing
# [EN] behaviour changed beyond the existing feature set.
# [EN] Every downstream consumer derives its version from ``APP_VERSION``, so there is no
# [EN] second source of truth. Windows file-version resources can only hold four numeric
# [EN] fields, therefore ``app_version_4()`` below is the only place allowed to derive that
# [EN] form; do not re-implement it downstream.
# [ZH] v0.2.5 =「发布加固」。为只读挂载点提供可写状态路径，缩小并加速发布流水线，
# [ZH] 并对全仓库的注释、docstring 与界面文案做双语规范化。除既有功能外不改变
# [ZH] 用户可见行为。
# [ZH] 下游全部按 ``APP_VERSION`` 派生，不再有第二处真相。注意：Windows 的**文件版本资源**
# [ZH] 只能是四段纯数字，这件事只由下面的 ``app_version_4()`` 负责，不要在下游各写一份。
APP_VERSION = "v0.2.5"


def app_version_4() -> str:
    """
    [EN]
    Return the four-part application version used by version resources.

    Why it lives here: the Windows executable version resource has four fields, whereas
    `APP_VERSION` is the human-readable three-part form (with a ``v`` prefix). The build
    chain used to hard-code ``0.2.1.0`` in two places (`backend/EchoBackend.spec` and
    `tools/set_metadata.bat`); when the version was bumped nobody updated them, so the
    shipped binaries kept reporting an old version (verified during the v0.2.2 delivery:
    both executables in the frozen release tree reported 0.2.1.0). Keeping the calculation
    in the single source of truth means the spec, the packaging scripts and the release
    gate all call one implementation instead of maintaining two truths.

    The prerelease handling was added in v0.2.3: splitting ``v0.2.3-alpha.1`` on dots
    yields ``3-alpha``, which is neither numeric nor a legal version resource for rcedit.
    The function therefore truncates to the SemVer core segment first and drops the
    prerelease suffix, so ``v0.2.3-alpha.1`` becomes ``0.2.3.0`` — the same rule the
    packaging scripts and the release gate apply.

    [ZH]
    **版本资源**用的四段式版本号（如 ``0.2.2.0``）。

    为什么放在这里：Windows 的 exe 版本资源是四段式，而 `APP_VERSION` 是给人看的三段式
    （还带 ``v`` 前缀）。以前打包链有两处各自写死 ``0.2.1.0``（`backend/EchoBackend.spec`
    与 `tools/set_metadata.bat`），升版本时没人记得改，发布物就报着旧版本号
    （v0.2.2 交付时验证：冻结发布树里两个 exe 都是 0.2.1.0）。
    把算法收在唯一来源里，spec / 打包脚本 / 门禁都调它，就不会再有两份真相。

    v0.2.3 补的**预发布后缀**处理：``v0.2.3-alpha.1`` 直接按点切会得到 ``3-alpha``，
    既不是数字、又会被 rcedit 判为非法版本资源。这里先截到 SemVer 的**核心段**再去预发布
    后缀，于是 ``v0.2.3-alpha.1`` → ``0.2.3.0``（与打包脚本、门禁同一口径）。
    """
    raw = APP_VERSION.lstrip("vV").strip()
    # [EN] Drop the prerelease (-alpha.1) and build-metadata (+build.7) suffix, keep the numeric core.
    # [ZH] 去掉预发布（-alpha.1）与构建元数据（+build.7）后缀，只留数字核心段
    for separator in ("-", "+"):
        raw = raw.split(separator, 1)[0]
    parts = (raw.split(".") + ["0", "0", "0", "0"])[:4]
    return ".".join(parts)
STUDIO = "Void Entropia Studio"
