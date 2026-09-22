# EchoDesk 变更日志

本文件记录各版本的功能变更、修复与已知限制。
版本号遵循 `MAJOR.MINOR.PATCH`；文件版本资源统一为 `X.Y.Z.0`。

---

## v0.2.0

### 新增

**上下文与长文档**
- 默认上下文窗口 4096 → **8192**，新增 `max_context` 上限校验（防止配置超限）；
  实际窗口按「配置值、模型训练窗口、上限」三者最小值自动收敛。
- 新增 `gguf_meta.py`：不加载模型即可读取 GGUF 元数据，取得模型训练窗口、
  层数、KV 注意力头数，并估算 **KV Cache 内存占用**（fp16）。
- 模型加载/重载时输出窗口大小与 KV Cache 估算日志。
- 新增 `summarizer.py`：长文档 **map-reduce 分段摘要**——按段长切分并保留重叠、
  切点落在段落/句号边界、逐段摘要后按预算递归归并、输出保留**原文指针**
  （源文件 + 各段字符区间）。超长文档从此不再被直接截断。
- 新增 `/summarize` 指令（手动触发全文摘要）；摘要进度实时显示在对话框中。
- 生成速度低于阈值时给出提示，并说明可通过调小窗口/增加线程改善。
- `file_max_chars` 10000 → 200000（0 = 不限），配合分段摘要处理长文档。

**模型下载与启动体验**
- **下载模型前先征询用户**：后端输出 `need_download`（含文件名与体积），
  前端弹确认框；同意才下载，拒绝则后端退出并给出后续指引。
- **启动时检测启动快捷方式**：不存在则询问创建（WScript.Shell COM 创建，
  指向主程序、工作目录为程序目录、使用 EchoDesk 图标）；用户拒绝后不再重复询问；
  移动文件夹导致快捷方式失效时提示重新创建。

**界面与可访问性**
- 消息内容全部**可选中复制**：纯文本消息改为只读文本框，助手消息保留 Markdown 渲染。
- 每条消息右下角新增复制操作条：复制 / Markdown / 代码（有代码块时）/ 全选。
- 新增快捷键：`Esc` 停止、`Ctrl+L` 清空、`Ctrl+O` 上传、`Ctrl+Shift+C` 复制全部、
  `Ctrl+加/减/0` 字号、`Ctrl+T` 主题、`Ctrl+,` 设置、`Ctrl+M` 技能市场。
- **窗口状态记忆**（大小/位置/最大化）与主题、字号持久化到 `ui_state.json`；
  多显示器与高 DPI 下不会还原到屏幕外。
- 状态栏新增模型、搜索、自动化、市场连接状态；新增底部通知条（下载完成/失败等）。
- 首次启动引导（欢迎页 + 模型下载/快捷方式/长文档/复制与快捷键说明）。
- 禁用消息控件内部滚动，滚轮统一滚动对话列表。

**发布工程**
- 发布物改为**单文件夹、多文件、单入口**：前端多文件发布（依赖 DLL 同目录，启动更快），
  后端 PyInstaller **onedir**（依赖集中在 `backend\_internal`），
  统一命名 `EchoDesk.exe` / `EchoDeskBackend.exe`，根目录只保留一个可执行文件。
- 新增 `tools\build_release.ps1` 一键发布脚本与 `tools\installer.iss`（Inno Setup 安装版模板）；
  构建脚本自动创建唯一入口快捷方式「EchoDesk 启动.lnk」。
- 新增 rcedit 元数据写入（`tools\set_metadata.bat`）与六尺寸图标生成
  （`tools\make_icon.py`：深靛蓝→深紫渐变 + 暖陶土回声纹，16/32/48/64/128/256）。
- **后端体积 976MB → 178MB**：移除 pyxtxt 依赖（见「变更」）。

### 变更

- **文件解析不再依赖 pyxtxt**：改为按格式直接解析（python-docx / openpyxl /
  python-pptx / PyMuPDF / xlrd + 多编码文本探测）。原因见
  `docs/THIRD_PARTY_LICENSES.md`：pyxtxt 的解析器子包必须整目录打包，
  会连带引入 easyocr→torch 等约 800MB 依赖，且其依赖链含 GPL/LGPL 组件。
- 后端元数据改由 PyInstaller 的 `version`/`icon` 在打包时写入
  （rcedit 会破坏 PyInstaller 追加的 PKG 归档）；rcedit 仍用于前端 exe。
- 前端与后端通信增加**请求闸门**，主窗口与设置窗口并发请求不再互相读错响应。

### 新增功能模块

- **自动化（浏览器）**：`automation.py` + 设置窗口「自动化」Tab。
  Playwright 驱动本机 Microsoft Edge，**有头模式**运行；按选择器抓取任务列表、
  逐个进入详情页、按后缀筛选并下载文件、调用本地模型生成摘要与审核意见、
  结果展示/落盘、临时文件即用即删。支持验证码/MFA 暂停等待人工处理、
  任务去重、分页、失败截图与跳过、审计日志（脱敏）。
  **安全协议未勾选时开关强制关闭**；协议版本与同意时间记入审计日志。
- **技能市场**：`marketplace.py` + 设置窗口「技能市场」Tab。
  按 GitHub topic `echodesk-plugin` 搜索（Star/最近更新/最佳匹配排序）、
  本地缓存 + TTL + 离线降级、`skill.json` 验证（未验证置灰）、
  详情查看、安装（tarball 校验 + 注册表登记 + 安装后回读校验）、卸载。
  **不自动执行第三方 prepare 脚本**，只提示。

### 文档与合规

- 新增 `docs/PRIVACY.md`（隐私政策：逐项说明哪些功能本机运行、哪些会联网）、
  `docs/EULA.md`（最终用户许可协议）、
  `docs/THIRD_PARTY_LICENSES.md`（自动生成的许可证清单 + 合规结论）、
  `docs/FAQ.md`（快速上手 + 故障排除）、`docs/DEVELOPER.md`（架构与协议）、
  `docs/SKILL_GUIDE.md`（技能开发指南）。
- 新增 `tools\gen_licenses.py`（扫描已安装发行包生成许可证清单）。

### 修复

- **后端在英文 locale 的 Windows 上静默退出**：Python 默认用 cp1252 编码 stdout，
  协议含中文/emoji 时抛 `UnicodeEncodeError` 直接崩溃。已强制 stdout/stderr 为 UTF-8
  并使用 `errors="replace"` 兜底；前端显式以 UTF-8 读取。这是 v0.1.0 在两台
  非开发机上"后端进程过会儿就自动退出"的真实原因。
- 新增崩溃日志 `backend\backend_crash.log`（`faulthandler` + `sys.excepthook`），
  异常不再静默退出。
- 下载/预热进度与后端异常退出提示改为同时显示在对话框中（不再只出现在状态栏）。

### 已知限制

- `.doc` / `.ppt`（旧版二进制）需要本机安装 WPS/Office；否则请另存为 `.docx` / `.pptx`。
- 扫描件 PDF 无文本层，v0.2.0 未内置 OCR。
- 自动化驱动的是**本机已安装的 Microsoft Edge**（`channel=msedge`）；程序未内置 Chromium，
  若 Edge 不可用会给出含 `playwright install chromium` 指引的提示。
- 自动化单文件下载上限 200MB，超过则跳过该文件并记录。
- 自动化对 Shadow DOM 的穿透是简化实现；实测 Playwright 的 CSS 引擎本身已能穿透
  open shadow root，复杂页面仍可能需要调整选择器。
- 自动化的定时触发依赖后端 `schedule` 线程（程序需保持运行）；
  开机自启需用户自行把快捷方式放入系统「启动」文件夹。
- 技能市场安装**依赖不自动 pip 安装**，只提示用户手动处理（安全优先）。
- 技能市场当前 topic `echodesk-plugin` 下尚无仓库，市场会显示空结果（附说明）。
- 技能运行期加载器（把技能命令挂到 `/` 指令表）接口已定义，运行期接线在后续版本完成。
- 自动更新与代码签名：构建脚本已预留 `-Sign` 钩子，但仓库内不含证书；
  自签名证书仍可能触发 SmartScreen 提示。
- `tools\build_release.ps1` 需在 PowerShell 5.1+ 运行；中文提示依赖脚本为 UTF-8 with BOM。

---

## v0.1.0

- 首个版本：Python 后端（llama.cpp 本地推理）+ C# WPF 前端（JSON 行协议）。
- 对话模式、温度预设、联网搜索、文件上传（Office/TXT/PDF）、
  会话持久化、常用语、Markdown 渲染、深浅色主题、字号调节。
- 指令集：`/help` `/mode` `/file` `/search` `/model` `/clearcache` `/prompt` `/temp`
  `/darkmode` `/stop` `/snippet` `/status` `/clear` `/undo` `/save` `/about` `/exit`。
