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
APP_VERSION = "v0.2.2"


def app_version_4() -> str:
    """
    [EN]
    Return the four-part application version.

    [ZH]
    **版本资源**用的四段式版本号（如 ``0.2.2.0``）。

    为什么放在这里：Windows 的 exe 版本资源是四段式，而 `APP_VERSION` 是给人看的三段式
    （还带 ``v`` 前缀）。以前打包链有两处各自写死 ``0.2.1.0``（`backend/EchoBackend.spec`
    与 `tools/set_metadata.bat`），升版本时没人记得改，发布物就报着旧版本号
    （v0.2.2 交付时验证：冻结发布树里两个 exe 都是 0.2.1.0）。
    把算法收在唯一来源里，spec / 打包脚本 / 门禁都调它，就不会再有两份真相。
    """
    raw = APP_VERSION.lstrip("vV").strip()
    parts = (raw.split(".") + ["0", "0", "0", "0"])[:4]
    return ".".join(parts)
STUDIO = "Void Entropia Studio"
