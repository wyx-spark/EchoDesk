"""EchoDesk 版本与署名信息。

版本号口径（v0.2.2 起明确）：
  * **v0.2.1 = 跨平台之前**的版本 —— 不含 `frontend-avalonia/`，也没有平台抽象；
    标签 `v0.2.1` 指向那个状态的最后一个提交，它的安装包与发布物由该提交重建。
  * **v0.2.2 = 跨平台版本** —— 跨平台前端（Avalonia）迁移 + 后端平台化 +
    三平台（Windows/Linux/macOS）后端分发与打包 + 两条降级边界。
不要把这两者混起来：`records/` 下的文件名（如 `v0.2.1-跨平台-2记录.md`）保留的是
撰写时的**任务书编号**，不是发布版本号。
"""
APP_NAME = "EchoDesk"
APP_VERSION = "v0.2.2"
STUDIO = "Void Entropia Studio"
