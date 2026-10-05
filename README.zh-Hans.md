# EchoDesk

**为在严格管控环境下工作的用户提供的本地、可审计、可扩展 AI 桌面助手。**

EchoDesk 是一款 Windows 桌面应用，把整套 AI 能力都留在你自己的机器上：推理、文件解析、
对话记录与浏览器自动化全部在本机完成。它面向这样的人群——因为数据合规、网络隔离或企业
政策而不能把文档交给云端 AI 服务的企业用户，以及任何希望文件不出本机的人。

无需云端账号，无需 API Key，无遥测。

[English →](README.md)

- **平台**：Windows 10 / 11（x64），单文件夹便携发布，唯一入口
- **推理**：通过 llama.cpp（`llama-cpp-python`）加载本地 GGUF 模型，默认使用 CPU
- **界面**：C# WPF 桌面程序，后端为 Python 3.13 打包的可执行文件
- **语言**：内置简体中文与英语，其他语言由社区语言包补齐
- **许可证**：[Apache-2.0](LICENSE)

## 与同类工具的差异

| 差异点 | 实际意味着什么 |
| --- | --- |
| **全本地推理** | 提问、文档、模型输出与对话记录都不离开本机。后端用 llama.cpp 加载本地 `.gguf` 模型，没有云端回退路径，也没有隐藏的上传通道。只有你自己主动开启的功能才会联网（模型下载、联网搜索、技能市场、自动化）。 |
| **兼容 WPS COM，可读取加密文档** | 文件解析优先走 WPS/Office COM，因此能读取带密码的文档以及旧版 `.doc` / `.ppt`——这是企业内部非常常见的需求。未安装 WPS/Office 时，回退到内置的轻量解析器处理 `.docx`、`.xlsx`、`.xlsm`、`.xls`、`.pptx`、`.pdf` 与纯文本。 |
| **批量网页自动化，可视化配置** | 填好任务列表页面与任务链接的位置，EchoDesk 就会逐条进入任务、下载匹配的附件、用本地模型审阅，并为每个任务生成结果文件。配置过程提供点选式选择器与字段级帮助，并可在真正运行前用「测试选择器」打开页面查看匹配结果。 |
| **审计日志与安全协议** | 启用浏览器自动化前必须显式勾选安全协议，协议版本与同意时间写入审计日志。每次自动化都会记录任务、时间与下载的文件名，但不记录文档正文、模型输出、Token 或 Cookie。下载文件放在临时目录，任务处理完立即删除。 |
| **技能市场 + 本地插件接口** | 通过 GitHub topic `echodesk-plugin` 发现并安装技能；开发中的插件也可以直接从磁盘加载，不必先发布到 GitHub。技能可注册指令、UI 面板、自动化钩子与额外的文件解析格式。 |
| **多语言** | 界面、系统消息、错误提示与指令帮助都由语言包驱动。当前随版本提供简体中文与英语，其他语言由社区贡献（见[多语言](#多语言)）。 |

## 核心能力

| 能力 | 说明 |
| --- | --- |
| **本地对话** | 本地模型流式回答，可调整温度、临时切换助手角色、按模型档案切换，并支持桌面快捷键。`/help` 列出全部指令。 |
| **上下文管理** | 默认上下文窗口 8192 tokens（硬上限 32768），并按 GGUF 元数据中模型真实训练窗口自动收敛。`/status` 显示当前窗口与 fp16 KV Cache 估算。 |
| **长文档处理** | 超过窗口一定比例时不再截断：分段摘要（map-reduce）后归并，并保留**原文指针**（文件名 + 字符区间）便于回查原文。也可用 `/summarize` 手动触发。 |
| **文件格式** | 支持 `.docx`、`.xlsx`、`.xlsm`、`.xls`、`.pptx`、`.pdf`、`.txt`、`.md`、`.csv` 等；`.doc` / `.ppt` 与加密文档通过 WPS/Office COM 读取。 |
| **模型中心** | 从 Hugging Face 镜像（默认 `hf-mirror.com`，可切换官方站点）搜索并下载 GGUF 模型，校验 SHA-256，登记到本地模型库，用 `/model` 热切换。 |
| **浏览器自动化** | Playwright 驱动本机已安装的 Microsoft Edge，**始终有头运行**，你能看到全过程。支持翻页、任务去重、逐个下载与审阅、遇到验证码或二次验证时暂停等待人工处理、失败截图、定时批量执行与纯文本结果文件。 |
| **技能** | 在市场窗口（`Ctrl+M`）安装、验证与卸载技能；市场会校验 `skill.json`，未验证的技能不可安装，且**不会自动执行**技能包内的脚本。 |
| **可选联网搜索** | 默认关闭。用 `/search on` 开启后，只有当次提问的关键词会发送给 DuckDuckGo。 |

## 系统要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | 正式交付物为 Windows 10 / 11（x64）。Linux 与 macOS 由跨平台前端（Avalonia，`frontend-avalonia/`）支持，后端代码三平台通用；两类平台上的能力差异与降级做法见 [docs/zh-Hans/CROSS_PLATFORM.md](docs/zh-Hans/CROSS_PLATFORM.md)。 |
| 前端运行时 | 无需额外安装。Windows 前端是 WPF 程序（目标框架 `net10.0-windows`），跨平台前端是 Avalonia 程序（目标框架 `net10.0`），两者都以自包含方式发布，便携发布物自带运行时。 |
| CPU | 任意 x64 处理器。默认使用 CPU 推理（`backend\config.json` 中 `n_gpu_layers: 0`）。 |
| 内存 | 最低 4 GB，建议 8 GB 以上。默认模型文件约 472 MB，默认 8192 tokens 窗口的 KV Cache 另需约 0.3–0.4 GB；窗口越大、模型越大，占用越高。 |
| 磁盘 | 便携文件夹约 1 GB（含模型约 0.85 GB，不含模型约 0.4 GB）。下载模型期间建议保留 2 GB 可用空间。 |
| WPS Office 或 Microsoft Office（可选） | 读取加密文档与旧版 `.doc` / `.ppt` 时需要（走 COM）。没有时应改用上面列出的现代格式。 |
| Microsoft Edge（可选） | 仅浏览器自动化功能需要，它驱动本机已安装的 Edge（`channel = msedge`）。EchoDesk 不分发浏览器本体。 |
| 网络 | 可选。除模型下载、联网搜索、技能市场与自动化外，其余功能均可离线使用。 |

## 快速上手

### 方案 A —— 安装或解压发布物（推荐，无需安装任何运行时）

1. 获取发布物：可以从本仓库的
   [Releases 页面](https://github.com/VoidEntropiaStudio/EchoDesk/releases) 下载（发布归档上线后），
   也可以自己构建（见[方案 B](#方案-b--从源码构建)），产物位于 `publish\EchoDesk-v0.2.1\`。
2. 安装或解压：
   - **安装程序** —— 双击 `EchoDesk-v0.2.1-Setup.exe`（只有这一份，**不含内置模型**，
     首次启动会询问是否下载）。默认装到**安装程序自己所在的文件夹**，向导里可以改到别处。
     它会创建开始菜单快捷方式、勾选后创建桌面快捷方式，并登记卸载入口（之后可在
     「应用和功能」里卸载）。装出来的目录结构与便携版完全一致：程序本体在 `app\`，
     用户数据与 `results\` 留在安装目录。卸载时会一并清掉程序运行时生成的文件
     （会话、模型、缓存、日志等），但 **`results\` 原样保留**。
   - **zip 免安装版** —— 解压到任意位置：桌面、其他磁盘、甚至 U 盘都可以。不会往该文件夹
     以外写任何东西，删除文件夹即卸载。
3. 启动 EchoDesk：用**开始菜单快捷方式**（安装程序装的），或双击文件夹里的
   **`Start-EchoDesk-v0.2.1.lnk`**（zip 免安装版；若没有这个快捷方式，先运行一次
   `install-complete.bat`）。两种方式创建的快捷方式都指向**你自己机器上的**这份程序。
   无论走哪条路，这个快捷方式都是唯一需要的入口；`app\EchoDesk.exe` 是界面主程序，
   `app\backend\EchoDeskBackend.exe` 由界面自动拉起，**请不要单独运行后端**。
4. 首次启动时回答下面的模型下载询问，然后即可开始使用。
   常用首批指令：`/help`、`/file <路径>`、`/model list`、`/status`。

值得记住的快捷键：`Ctrl+Enter` 发送、`Esc` 停止生成、`Ctrl+O` 上传文件、
`Ctrl+L` 清空对话、`Ctrl+,` 打开设置、`Ctrl+M` 打开技能市场。

### 方案 B —— 从源码构建

需要 Python 3.13（后端）以及能够构建目标框架 `net10.0-windows` 的 .NET SDK（前端）。

```powershell
# 1) 后端依赖（Python 3.13；依赖清单在 requirements.txt）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt

# 2) 前端
dotnet build frontend\EchoDesk.csproj -c Release

# 3) 测试（不需要模型文件）
python tools\selftest.py            # 后端单元级自检，退出码 0/1
python tools\test_file_parsers.py   # 全部支持格式的解析冒烟测试

# 4) 完整便携构建 -> publish\EchoDesk-v0.2.1\
& tools\build_release.ps1
& tools\build_release.ps1 -SkipBackend     # 只重建 WPF 前端
& tools\build_release.ps1 -SkipFrontend    # 只重建打包后的后端
& tools\build_release.ps1 -IncludeModels   # 把本地 .gguf 模型打进发布物（离线包）
```

WPF 前端连接的是**打包后**的后端可执行文件，因此只调前端时也需要先构建一次它。
用环境变量 `ECHODESK_BACKEND` 可以把界面指向自定义位置的后端：

```powershell
# 用 PyInstaller 构建后端（产物：dist\EchoDeskBackend\EchoDeskBackend.exe）
pyinstaller --noconfirm --distpath dist --workpath build\pyi backend\EchoBackend.spec

# 从源码运行前端并指向它
$env:ECHODESK_BACKEND = "$PWD\dist\EchoDeskBackend\EchoDeskBackend.exe"
dotnet run --project frontend\EchoDesk.csproj
```

后端也可以单独启动（`python backend\main.py`）：它通过 stdin/stdout 使用「每行一个 JSON」的
协议，详见 [docs/DEVELOPER.md](docs/DEVELOPER.md)。把 `.gguf` 模型放进 `backend\models\`
（源码运行）或打包后端的 `models\` 文件夹即可。

### 首次启动：还没有模型？程序照样能用

EchoDesk **不自带**模型权重，也**不会静默下载**任何东西：

1. 启动时后端检查 `backend\config.json` 中指定的模型（默认为 `Qwen3-0.6B-Q6_K.gguf`，
   约 472 MB）。
2. 若本地缺少该文件，后端会**继续运行**在"尚无模型"状态：上报 `need_download`
   （带 `blocking: false`）与 `ready`（带 `model_ready: false`）并在其中给出引导。
   界面保持可交互——可以进设置、解析文件、打开模型市场；**不会**在你没明确操作前下载任何东西。
3. 三种拿到模型的方式，**都不需要重启程序**：
   * **模型市场**：设置 → 模型 → 模型市场。列表显示**全部** GGUF 仓库（不按硬件过滤隐藏），
     适合本机的条目标有「(推荐)」，当前默认模型对应的仓库固定在列表最前。
   * **手动放置**：把任意 `.gguf` 文件放进 `app\backend\models\`，点「重新扫描模型文件夹」；
     若只扫描到一个模型会自动加载。
   * **直接加载**：在「本地模型库」里点已下载模型旁的「加载」。
4. 下载完成或加载成功后**热加载**模型：状态栏从"无模型"切换为模型名，推理立即可用，
   后端与前端都不需要重启。加载失败（文件损坏 / 格式不支持 / 内存不足）时保持无模型状态、
   给出原因，并可重试。
5. 下载地址取自配置的 Hugging Face 端点——默认 `https://hf-mirror.com`；可在
   `backend\config.json` 中修改 `hf_endpoint` / `model_repo` / `model_path`，
   改为官方 `https://huggingface.co` 或内部镜像。
6. 若文件夹内没有启动快捷方式，EchoDesk 会询问一次是否创建，然后显示一次上手引导。
   个人设置可写入 `app\backend\config.user.json`，它优先于 `config.json`，且不会进版本库。

### 发布文件夹里有什么

便携版把**程序**和**你的数据**分开放：你要改的、要备份的、要跟着文件夹搬走的东西都在外层，
程序本体统一收进 `app\`。

| 路径 | 内容 |
| --- | --- |
| `Start-EchoDesk-v0.2.1.lnk` | 唯一入口（指向 `app\EchoDesk.exe`，工作目录为本文件夹）。 |
| `results\` | 自动化结果（按 `日期\任务名\` 分层的纯文本报告与下载的原始文件）。 |
| `ui_state.json`、`snippets.json` | 窗口状态、字号、主题、语言与常用语。 |
| `lang\` | 后端语言包（系统消息、错误提示、指令帮助）。丢一个新的 `<locale>.json` 进去即可加语言。 |
| `examples\skills\` | 示例插件源码（Hello World 与 OFD 读取）。 |
| `docs\` | 随发布物提供的文档（`docs\zh-Hans\`、`docs\en\`，以及隐私政策、EULA、第三方许可证）。 |
| `LICENSE`、`NOTICE`、`SECURITY.md` | 许可证、署名与安全策略。 |
| `app\EchoDesk.exe` | WPF 前端；.NET 依赖以 DLL 形式放在同目录。 |
| `app\lang\ui\` | 界面语言包（按钮、标签、对话框文案）。 |
| `app\backend\EchoDeskBackend.exe` | 打包后的 Python 后端（PyInstaller onedir），依赖在 `app\backend\_internal\`。 |
| `app\backend\config.json` | 全部配置：模型、上下文窗口、分段摘要、自动化、技能市场、模型中心。 |
| `app\backend\models\` | 本地 `.gguf` 模型目录。 |
| `app\backend\session.json` | 对话记录（自动保存与恢复）。 |

把整个文件夹搬到别处后，双击 `app\EchoDesk.exe` 一次，程序会提示并重建快捷方式
（工作目录也会一并修正）。若要让用户数据放到别的位置（例如受管机器上程序目录只读），
设置环境变量 `ECHODESK_DATA` 指向目标目录即可。

## 截图与演示

**v0.2.1 发布物不包含截图**。目前随包提供的图片资源只有 `assets/EchoDesk.ico`（程序图标）与
`assets/EchoDesk.png`（Logo）；本 README 里没有任何指向不存在图片的链接。

界面预览图——主界面、长文档分段摘要、带「测试选择器」的自动化页、有头模式的自动化运行，
以及双语界面下的技能市场——将在后续版本中放进 `assets/screenshots/`，届时本节会直接嵌入这些图片。

在那之前，想尽快看到界面，直接运行便携版即可：无需安装、无需云端账号，本地对话也不需要联网
（见[快速上手](#快速上手)）。

## 多语言

EchoDesk 面向全球用户，多语言是产品的一部分而不是事后补充：

- **当前内置**：简体中文（`zh-Hans`）与英语（`en`）。
- **语言包位置**：后端消息、错误提示与指令帮助在 `backend/lang/<locale>.json`；
  界面文本在 `frontend/Resources/Lang/<locale>.json`；文档放在 `docs/<locale>/`。
  每条文本使用语义化键名（如 `settings.automation.task_url.label`），并可附带说明
  上下文与占位符的注释。
- **自动识别**：`language` 默认为 `auto`，首次启动跟随系统区域；也可以在设置里手动切换，
  选择结果会被记住。
- **回退链**：`请求语言 → en → zh-Hans → 键名本身`。回退到键名是刻意设计：缺失的翻译会
  直接显示出来，而不是悄悄留空。
- **社区语言**：`zh-Hant`、`ja`、`ko`、`de`、`fr`、`es`、`pt-BR` 是下一批候选，
  也欢迎任何语言——翻译流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 扩展 EchoDesk：技能与本地插件

EchoDesk 有两种扩展方式，运行期使用同一套接口：

- **技能市场** —— 用 `Ctrl+M` 打开。技能通过 GitHub topic `echodesk-plugin` 发现；仓库根目录
  存在 `skill.json` 且含 `echodesk.bundle` 字段时标记为**已验证**。未验证的仓库无法安装，
  技能包内的 `prepare` 脚本永远不会被自动执行，市场结果会本地缓存，因此 GitHub API 离线或
  限流时只会降级而不会报错。
- **本地插件接口** —— 把插件文件夹放到本机约定位置，EchoDesk 就能识别并加载，与市场安装的
  技能走同一套运行期机制，不需要先发布到 GitHub。开发中的插件建议放在开发目录
  （`backend\skills_dev\`）；具体目录约定与 API 以技能开发指南为准。

从这里开始：

- [技能开发指南（中文）](docs/zh-Hans/SKILL_GUIDE.md) —— 清单格式、能力声明、安全审查要求与发布步骤。
- [docs/zh-Hans/SKILL_GUIDE.md](docs/zh-Hans/SKILL_GUIDE.md) 与
  [docs/en/SKILL_GUIDE.md](docs/en/SKILL_GUIDE.md) —— 该指南的本地化版本（随 v0.2.1 文档集提供）。
- [docs/DEVELOPER.md](docs/DEVELOPER.md) —— 架构、stdin/stdout 协议、模块划分，以及指令、
  协议动作与文件格式的扩展点。

## 隐私、审计与合规

EchoDesk 的本地化是设计目标，并且写成了公开文档：

- **始终本机**：对话与推理、文件解析、长文档分段摘要、对话记录、常用语与界面状态，
  以及自动化在浏览器中的操作。
- **仅在你主动开启时联网**：模型下载（需你确认）、联网搜索（`/search on`）、技能市场的
  GitHub API 查询，以及自动化访问你自己填写的目标站点。
- **无遥测**：没有统计、埋点、使用数据或崩溃上报。
- **可审计的自动化**：启用自动化前必须勾选安全协议，协议版本与同意时间写入
  `backend\automation_log.jsonl`；日志记录执行了什么、下载了哪些文件——只有文件名，
  不含文档正文、模型输出、Token、Cookie 或密码。
- **数据由你掌控**：随时删除 `backend\session.json`、`snippets.json`、`ui_state.json`、
  `backend\models\*.gguf` 或 `backend\browser_state.json`，即可清除历史、偏好、模型或
  已保存的浏览器登录态。逐项说明见 [docs/PRIVACY.zh-Hans.md](docs/PRIVACY.zh-Hans.md)，
  使用条款见 [docs/EULA.zh-Hans.md](docs/EULA.zh-Hans.md)——其中也说明了本地小模型输出可能有误，
  不构成专业意见。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/zh-Hans/USER_GUIDE.md](docs/zh-Hans/USER_GUIDE.md) | 用户手册：安装启动、日常问答、长合同、批量自动化、技能。 |
| [docs/zh-Hans/CROSS_PLATFORM.md](docs/zh-Hans/CROSS_PLATFORM.md) | 跨平台支持与能力边界：三平台能力矩阵、两条降级边界与替代做法、平台探测、目录与环境变量、打包与签名、验证证据。 |
| [docs/zh-Hans/FAQ.md](docs/zh-Hans/FAQ.md) | 快速上手与故障排除：启动、模型、文件、自动化、技能市场。 |
| [docs/zh-Hans/AUTOMATION_GUIDE.md](docs/zh-Hans/AUTOMATION_GUIDE.md) | 自动化配置逐字段填写指南与「测试选择器」。 |
| [docs/DEVELOPER.md](docs/DEVELOPER.md) | 架构、JSON 协议、模块职责、配置参考、构建与发布。 |
| [docs/zh-Hans/SKILL_GUIDE.md](docs/zh-Hans/SKILL_GUIDE.md) | 如何编写、提交与发布技能。 |
| [docs/PRIVACY.zh-Hans.md](docs/PRIVACY.zh-Hans.md) | 哪些在本机运行、哪些可能联网、日志脱敏规则。 |
| [docs/EULA.zh-Hans.md](docs/EULA.zh-Hans.md) | 发布物的最终用户许可协议。 |
| [docs/ENCRYPTED_DOCUMENTS.zh-Hans.md](docs/ENCRYPTED_DOCUMENTS.zh-Hans.md) | EchoDesk 读取企业加密系统保护的文档时怎么工作：前提条件、不绕过的声明、责任归属、哪些内容只在内存中。 |
| [docs/THIRD_PARTY_LICENSES.zh-Hans.md](docs/THIRD_PARTY_LICENSES.zh-Hans.md) | 第三方组件与许可证完整清单。 |
| [CHANGELOG.md](CHANGELOG.md) | 各版本的功能变更、修复与已知限制。 |
| [NOTICE](NOTICE) | 随包分发的主要第三方组件署名。 |

本地化文档按两种内置语言各提供一套：`docs/zh-Hans/`（简体中文，即上表所链接的版本）与
`docs/en/`（英语，同一套指南）。

## 获取帮助与参与贡献

欢迎各种形式的贡献——缺陷报告、代码修复、技能、翻译与文档都算。所有公开渠道都在 GitHub 上：
本项目**不提供**支持邮箱、电话号码或邮寄地址。

- **反馈问题或提功能请求** —— 请开 GitHub Issue：
  <https://github.com/VoidEntropiaStudio/EchoDesk/issues>。
  缺陷报告、功能请求、技能提交与翻译协作各有对应模板；请附上版本号、操作系统、可复现步骤
  与相关日志。
- **提问、分享技能、协作翻译** —— 请到
  [GitHub Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions) 发帖。
- **提交改动** —— 请开 Pull Request；[CONTRIBUTING.md](CONTRIBUTING.md) 里有开发环境、
  代码风格、提交信息规范与评审流程。日志位置见 [SECURITY.zh-Hans.md](SECURITY.zh-Hans.md)。
- **安全漏洞** —— **请不要开公开 Issue**，按 [SECURITY.zh-Hans.md](SECURITY.zh-Hans.md) 的私有渠道报告
  （支持的版本、报告需包含什么、响应时间承诺也在那里）。
- **社区行为** —— [CODE_OF_CONDUCT.zh-Hans.md](CODE_OF_CONDUCT.zh-Hans.md)。

## 许可证

EchoDesk 采用 **Apache License 2.0** 发布，全文见 [LICENSE](LICENSE)。该许可证包含明确的
专利授权，允许商用与企业内部使用、修改与再分发，这也是它优于更简短宽松许可证的原因。

- 随包分发的主要第三方组件署名：[NOTICE](NOTICE)。
- 完整的组件与许可证清单：[docs/THIRD_PARTY_LICENSES.zh-Hans.md](docs/THIRD_PARTY_LICENSES.zh-Hans.md)。
- 模型权重**不属于**本项目，仍归其发布方的许可证管辖；下载模型前请自行确认其条款。
- 版权所有 (C) 2026 熵寂工作室（VoidEntropiaStudio）。
