# 为 EchoDesk 贡献代码

> 本文件是 [`CONTRIBUTING.md`](CONTRIBUTING.md) 的简体中文参考译本。如两者有歧义，**以英文版为准**。

感谢你抽出时间提供帮助。EchoDesk 是一款本地优先的 AI 桌面助手，任何形式的贡献都有价值：
缺陷报告、修复、文档、翻译与新的技能。Windows 是完整的正式交付平台；Linux 与 macOS 由同一套
Python 后端之上的跨平台（Avalonia）前端提供支持，因此贡献时请把三个平台都考虑进去。
平台支持矩阵与"哪些能力仅限 Windows"的准确边界见
[docs/zh-Hans/CROSS_PLATFORM.md](docs/zh-Hans/CROSS_PLATFORM.md)。

参与本项目即表示你同意遵守[行为准则](CODE_OF_CONDUCT.md)。

**本文的命令写法**：优先给三平台通用的写法（`python`、`git`、正斜杠、`~/.ssh`），只在确实有差异时
展开并标注平台——虚拟环境激活、环境变量、Windows 专属的发布链。你自己写文档时也请遵循同一口径。

- [参与方式](#参与方式)
- [提交 issue](#提交-issue)
- [开发环境](#开发环境)
- [构建与打包](#构建与打包)
- [测试](#测试)
- [代码风格](#代码风格)
- [提交信息](#提交信息)
- [分支与拉取请求流程](#分支与拉取请求流程)
- [贡献翻译](#贡献翻译)
- [贡献技能](#贡献技能)
- [仓库卫生](#仓库卫生)
- [贡献的许可](#贡献的许可)

## 参与方式

| 我想要…… | 从这里开始 |
| --- | --- |
| 报告缺陷 | 提交 [bug 报告 issue](https://github.com/VoidEntropiaStudio/EchoDesk/issues)（见[提交 issue](#提交-issue)）；附上版本、操作系统、复现步骤与日志。 |
| 请求新功能 | 提交 [功能请求 issue](https://github.com/VoidEntropiaStudio/EchoDesk/issues)，描述你想要的场景与工作流。 |
| 提问或讨论想法 | 在 [GitHub Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions) 发起话题——问答、技能分享与翻译协调都在那里。 |
| 修复或改进代码 | 阅读[开发环境](#开发环境)，然后提交拉取请求。 |
| 翻译界面、消息或文档 | 参照[贡献翻译](#贡献翻译)。 |
| 发布技能 | 参照[贡献技能](#贡献技能)。 |
| 改进文档 | 文档位于 `docs/`（本地化版本在 `docs/<locale>/`）；欢迎提交 PR。 |

## 提交 issue

所有公开反馈都通过 GitHub 上的本仓库进行。本项目**不提供支持邮箱、电话号码和邮寄地址**，
因此 GitHub 是唯一渠道——不会有内容丢在私人收件箱里：

- **缺陷与其他可复现的问题** → 在
  <https://github.com/VoidEntropiaStudio/EchoDesk/issues> 提交 issue。
- **问题、想法、技能分享与翻译协调** → 在
  [GitHub Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions) 发起话题。
- **代码、文档或翻译改动** → 提交拉取请求，见
  [分支与拉取请求流程](#分支与拉取请求流程)。

Issue 模板位于 [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/)——请选择与你的报告相符的
模板（bug、功能请求、技能提交、翻译）。好的报告能让维护者不必猜测就能复现问题，因此请包含：

- **版本**：`/status` 显示的版本号，或发布文件夹名中的版本号
  （例如 `v0.2.1`）。
- **环境**：你的操作系统与版本——Windows 的内部版本号，或 macOS / Linux 的发行版与内核版本；
  是否安装了 WPS Office / Microsoft Office 与 Microsoft Edge（这两项只对"仅限 Windows 的能力"有影响）；
  以及你运行的是发布版（Windows 安装包，或 Linux/macOS 便携包）还是从源码构建的版本。
- **准确步骤**：按顺序列出你点击或输入的内容。自动化相关问题请附上
  选择器配置与 URL 模式（涉密内容请脱敏）。
- **预期行为与实际行为**：你期望发生什么，实际发生了什么。
- **日志**：日志位置见 [SECURITY.md](SECURITY.md)——两份崩溃日志分别是
  程序目录下的 `frontend_crash.log` 和 `backend/` 内的 `backend_crash.log`。
  两者都在失败时追加写入，因此文件为空表示「没有记录到崩溃」。
  用户数据（对话历史、界面状态、`results/`）在**程序旁边**，也就是发布目录的外层，不在 `app/` 里：
  Windows 上就是程序目录本身；macOS 与 Linux 上，当程序目录不叫 `app` 时前端会回退到
  `~/Library/Application Support/EchoDesk` 与 `$XDG_DATA_HOME/EchoDesk`（或 `~/.local/share/EchoDesk`）。
  `ECHODESK_DATA` 可以覆盖以上全部。**它不是 `%APPDATA%`** —— 代码里没有任何一条路径写在那里，
  那个写法只出现在两处过时注释里，v0.2.2 已改正。

对于任何看起来像安全问题的内容，**不要开公开 issue**——请遵循
[SECURITY.md](SECURITY.md) 中的私密报告流程。

## 开发环境

### 获取代码（统一使用 SSH）

克隆与推送都用 SSH（本仓库不使用 HTTPS 形式的远端地址）：

```bash
git clone git@github.com:VoidEntropiaStudio/EchoDesk.git
cd EchoDesk
```

已有工作副本时，确认 remote 是 SSH：

```bash
git remote -v
# origin  git@github.com:VoidEntropiaStudio/EchoDesk.git (fetch)
# origin  git@github.com:VoidEntropiaStudio/EchoDesk.git (push)

# 若显示的是 https://…，一条命令改过来：
git remote set-url origin git@github.com:VoidEntropiaStudio/EchoDesk.git
```

需要先准备密钥：`ssh-keygen -t ed25519 -C "本机标签"`，把 `~/.ssh/id_ed25519.pub`
的**全文**加到 GitHub 的 Settings → SSH and GPG keys；然后 `ssh -T git@github.com`
应回答 `Hi <用户名>! You've successfully authenticated`。
这些命令三平台完全一致：Windows 上 `~/.ssh` 就是 `%USERPROFILE%\.ssh`（同一个目录，PowerShell 7
与 Git Bash 都认 `~`）；macOS/Linux 上私钥不能对同组或其他用户可读，创建后执行一次
`chmod 600 ~/.ssh/id_ed25519`。
公司网络挡 22 端口时改用 443，写法见
[docs/zh-Hans/DEVELOPER.md](docs/zh-Hans/DEVELOPER.md) 的「版本控制与推送（统一 SSH）」。
完整的密钥/ssh-agent 清单（含各平台路径）见
[docs/zh-Hans/DEVELOPER.md](docs/zh-Hans/DEVELOPER.md) 第五节。

要求：

- 后端（`backend/`）需要 **Python 3.13**——三个平台用同一个版本。
- 一个能构建你要改的前端的 **.NET SDK**：
  - 跨平台（Avalonia）前端的目标框架是 `net10.0`，Windows、macOS、Linux 都能构建
    （`frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj`）；
  - WPF 前端的目标框架是 `net10.0-windows`，因此只能在 Windows 上构建
    （`frontend/EchoDesk.csproj`）。
- 自动化相关工作可选（**仅限 Windows**）：本地安装 **Microsoft Edge**，因为自动化引擎
  通过 Playwright 驱动系统 Edge（`channel = msedge`）。

```bash
# 后端：建立虚拟环境并安装锁定版本的依赖（三平台命令相同）
python -m venv .venv
python -m pip install -r backend/requirements.txt

# macOS / Linux：激活环境（bash、zsh）
. .venv/bin/activate
```

```powershell
# Windows：激活环境（PowerShell；cmd.exe 里改用 .venv\Scripts\activate.bat）
.\.venv\Scripts\Activate.ps1
```

```bash
# 前端（.NET CLI 本身三平台行为一致）
dotnet build frontend/EchoDesk.csproj -c Release                                       # WPF，仅 Windows（net10.0-windows）
dotnet build frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj -c Release   # Avalonia，Windows + macOS + Linux（net10.0）
```

WPF 前端启动的是**打包好的**后端可执行文件（发布版中为 `backend/EchoDeskBackend.exe`，
用 PyInstaller 构建时为 `dist/EchoDeskBackend/EchoDeskBackend.exe`）。
`ECHODESK_BACKEND` 可以覆盖该路径，这是前端开发最快的循环方式：

```powershell
# Windows：先构建一次后端（产物：dist\EchoDeskBackend\EchoDeskBackend.exe）
pyinstaller --noconfirm --distpath dist --workpath build/pyi backend/EchoBackend.spec

# Windows：用源码里的 WPF 前端连上它
$env:ECHODESK_BACKEND = "$PWD\dist\EchoDeskBackend\EchoDeskBackend.exe"
dotnet run --project frontend/EchoDesk.csproj
```

```bash
# macOS / Linux：跨平台前端会自己找到 backend/main.py（从 bin/ 逐级向上找到仓库根）
dotnet run --project frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj

# macOS / Linux：也可以显式指定后端——可执行文件，甚至直接用 .py 入口
export ECHODESK_BACKEND="$PWD/backend/main.py"
dotnet run --project frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj
```

后端也可以独立运行以便调试协议（`python backend/main.py`）：它从 stdin 每行读取一个
JSON 请求，并向 stdout 每行写入一个 JSON 响应。动作列表、响应类型与模块映射见
[docs/zh-Hans/DEVELOPER.md](docs/zh-Hans/DEVELOPER.md)。

只有真正做推理时才需要模型：把 `.gguf` 文件放入 `backend/models/`（源码运行）
或打包后端的 `models/` 文件夹（发布物里是 `app/backend/models/`）。其他一切——解析、自动化管线、
i18n、协议——都可以在没有模型的情况下开发与测试。

## 构建与打包

Windows 的发布链只能用 PowerShell 与 `cmd`（它调用 Inno Setup、`rcedit` 与 WPF UI 探针）。
三平台的便携包由纯 Python 工具构建，在任何平台上都能跑，因此 macOS/Linux 贡献者用后者打包：

```powershell
# Windows：完整便携构建 -> publish\EchoDesk-v0.2.2\
& tools\build_release.ps1

& tools\build_release.ps1 -SkipBackend     # 只重建 WPF 前端
& tools\build_release.ps1 -SkipFrontend    # 只重建打包的 Python 后端
& tools\build_release.ps1 -SkipClean -SkipBackend -SkipFrontend   # 只重跑资源/元数据步骤
& tools\build_release.ps1 -IncludeModels   # 一并带上本地 .gguf 模型（离线大包）
& tools\build_release.ps1 -MakeInstaller   # 同时编译 Inno Setup 安装包
```

```bash
# macOS / Linux（Windows 同样适用）：跨平台打包，三平台命令完全一致
python tools/pack_backend.py                        # 三个 RID 的后端（standalone，输出 publish/backend-<rid>/）
python tools/pack_backend.py --platform linux-x64   # 只产一个 RID（可重复传该参数）
python tools/pack_crossplatform.py --rid linux-x64  # 前端 + 后端整包 -> dist/EchoDesk-<版本>-<rid>.tar.gz
python tools/pack_crossplatform.py --rid osx-arm64
python tools/pack_crossplatform.py --rid win-x64    # Windows 目标产出 .zip
python tools/pack_crossplatform.py --plan           # 演练：只打印步骤不构建
python tools/gen_platform_icons.py                  # .icns / Linux hicolor 图标 / .desktop 条目
```

`tools/pack_crossplatform.py` 会同时在 `publish/backend-<rid>/` 与 `dist/backend-<rid>/` 里找
standalone 后端，因此按各自默认值依次运行 `pack_backend.py`、`pack_crossplatform.py` 即可。
macOS 分发还需要代码签名与公证，这需要维护者的 Apple 凭据；打包脚本只打印这些命令，
并把它们留在产物里的 `SIGNING.txt` 中。

安装刚构建好（或刚下载）的版本，各平台做法不同：

```powershell
# Windows：运行安装包，或解压便携 zip 后直接启动
.\publish\EchoDesk-v0.2.2-Setup.exe

.\publish\EchoDesk-v0.2.2\app\EchoDesk.exe
```

```bash
# macOS / Linux：发布包就是普通归档，没有安装程序
tar -xzf dist/EchoDesk-v0.2.2-linux-x64.tar.gz
chmod +x EchoDesk-v0.2.2-linux-x64/start.sh EchoDesk-v0.2.2-linux-x64/app/EchoDesk   # 仅在可执行位丢失时需要
cd EchoDesk-v0.2.2-linux-x64 && ./start.sh
```

构建中有两点容易出错，脚本里都加了注释：

- `rcedit` **不得**触碰 PyInstaller 的输出（它会重建 PE 映像并破坏追加的
  PKG 归档）；后端改为通过 spec 写入自己的版本资源与图标。
  这一步是 Windows 专属的，所以下载 `rcedit` 的 `tools/fetch_tools.bat` 没有 macOS/Linux 对应物：
  跨平台包根本不用 `rcedit`。
- PowerShell 可能把原生命令的 stderr 当作终止性错误，这曾导致构建在出现
  警告时中止；`tools/build_release.ps1` 因此封装了原生调用。

另请注意**包体积**：EchoDesk 发布的是自包含前端加 PyInstaller 后端，因此新增一个体积大
——或会引入庞大传递依赖树——的依赖是实实在在的代价。优先使用标准库，优先选择体积小、
许可宽松的库，并在拉取请求中说明新依赖对体积的影响。

## 测试

```bash
# 任何拉取请求都必须跑的检查（Windows、macOS、Linux 通用）
python tools/selftest.py             # 后端单元级自检（退出码 0 = 通过）
python tools/test_file_parsers.py    # 所有支持格式的解析冒烟测试

# 视改动范围追加
python tools/test_plaintext.py       # Markdown -> 纯文本转换（结果文件）
python tools/test_results_dir.py     # 自动化结果目录布局与 result.txt
python tools/test_reasoning.py       # 思考内容分离：标签边界、协议双通道、历史持久化
python tools/test_model_hub_e2e.py   # 模型中心协议对接打包后的后端
python tools/test_platform.py        # 平台探测与降级分支（ECHODESK_FORCE_PLATFORM）
python tools/check_standalone_backend.py       # 打包后的 standalone 后端：结构 + 协议
python tools/check_crossplatform_release.py    # 打包后的发布包：归档格式、可执行位、校验和
python tools/gen_licenses.py         # 重新生成 docs/THIRD_PARTY_LICENSES.md
```

有两项测试只限 Windows，因为它们测的功能本身就只在 Windows 上可用
（在其他平台上会打印跳过信息，而不是判失败）：

```powershell
powershell -File tools\ui_probe.ps1  # WPF UI 探针：设置窗口快速打开、不产生崩溃日志
python tools/test_automation_e2e.py  # 对本地静态站点跑端到端自动化（需要 Edge + Playwright）
```

如果某个测试因为你的机器缺少模型、Edge 或打包好的后端而打印跳过信息，
请在拉取请求中说明，不要只字不提。

## 代码风格

**Python（后端、工具）**

- 4 空格缩进；不使用制表符。
- 函数签名加类型注解（`def parse(path: str) -> List[str]:`），模块中已在
  使用的地方保留 `from __future__ import annotations`。
- **文档字符串与注释用中文书写**——这是 `backend/` 与 `tools/` 中既有的约定，
  应当保持不变，以让代码库风格一致。模块级文档字符串说明*为什么*选择某种
  设计，这是本代码库最有价值的部分。
- 能让语言包条目承载的用户可见字符串，不要留在代码路径里
  （见[贡献翻译](#贡献翻译)）。
- 失败要柔和：后端绝不能因为单个坏文件、坏技能或自动化任务而让进程
  挂掉。捕获、记录、继续。

**C# / XAML（前端）**

- 文件作用域命名空间（`namespace EchoDesk.Services;`），启用可空引用类型
  （`<Nullable>enable</Nullable>`），并开启隐式 using。
- 不使用第三方 NuGet 包：前端协议处理使用 `System.Text.Json`。
  请保持这样。
- 界面文案遵循与后端相同的本地化规则；长时间运行的工作不得阻塞
  UI 线程，设置窗口相关的加载在窗口显示后异步进行。

**Markdown 与文档**

- 不强制一行写一句话，但请保持表格与围栏代码块整洁。
- 围栏代码块要带语言标记（`bash`、`powershell`、`jsonc`、`text`）。
- **命令与路径按三平台来写**：优先用三平台通用写法（`python`、`git`、正斜杠、`~/.ssh`、
  `backend/models/`），只在确有差异时展开并给每个代码块标注平台——`# Windows` / `# macOS / Linux`——
  不要把每条命令都写三遍。
- 不使用制表符、不留行尾空格，相对链接使用正斜杠。
- 不要提交包含真实客户数据、文件路径或凭据的截图。

## 提交信息

本项目使用中文、类似约定式的前缀，可带任务编号与优先级标签，后接全角冒号与改动说明：

```text
修复一+修复二 P0：设置窗口卡死（空引用致进程终止）与命令栏 Enter 误执行
新增四 P1：模型选择栏（HF 镜像/官方双源搜索 + 详情 + 下载入库 + 模型库管理）
优化五 P2：状态栏信息分层（顶部精简 + 输入框下方可折叠轻量状态区）
配置：config.json 补齐 automation 与 marketplace 配置段
构建脚本：新增 .gitattributes 固定换行符
交付：v0.2.0 修复记录 + 变更日志 + 发布物纳入 docs 与最终实测
```

- 在用前缀：`新增` / `修复` / `优化` / `配置` / `构建脚本` / `交付`，当改动对应版本任务书中的
  编号任务时，再加 `任务N`。
- 较大的改动使用优先级标签 `P0`–`P3`；`P0` 表示数据丢失或崩溃风险。
- 主题保持一行，并描述用户可见的效果，而不只是改动了哪个文件。
- 仅文档或工具类改动可以用英文主题，但最好与周边历史保持一致。

## 分支与拉取请求流程

1. Fork 仓库（如果有写权限，也可以直接建分支），并从 `master` 拉出分支，
   例如 `fix/settings-window-focus` 或 `docs/en-skill-guide`。
2. 一个拉取请求聚焦一个主题。无关的重构会让评审——以及可能的回退——
   困难得多。
3. 运行[测试](#测试)中要求的测试，并在描述中说明结果。
4. 在拉取请求描述中说明：改了什么、为什么改、如何验证，以及该改动是否
   影响包体积或第三方许可清单（新增依赖意味着必须重新生成
   `docs/THIRD_PARTY_LICENSES.md`）。
5. 迭代过程中可以随意使用 `draft` 拉取请求；测试通过后再标记为就绪。
6. 维护者评审，可能要求修改，然后合并。小而说明清楚的拉取请求
   评审最快。

绝不要提交模型、构建产物、用户数据或凭据——见
[仓库卫生](#仓库卫生)。

## 贡献翻译

EchoDesk 的本地化基于语言包，其设计让一份翻译成为自包含的贡献。一个语言区域由以下文件组成：

| 文件 | 内容 |
| --- | --- |
| `backend/lang/<locale>.json` | 后端系统消息、错误文本与指令帮助。 |
| `frontend/Resources/Lang/<locale>.json` | 界面字符串：标签、按钮、菜单、对话框、工具提示。 |
| `docs/<locale>/` | 本地化文档（README、用户指南、FAQ、自动化指南）。 |

规则与流程：

1. **选择语言区域标签。** 使用与现有文件一致的 BCP 47 风格标签：
   `zh-Hans`、`en`、`zh-Hant`、`ja`、`ko`、`de`、`fr`、`es`、`pt-BR`。
   `zh-Hans` 是键覆盖率的基准来源；`en` 是第一回退。
2. **提取待翻译的字符串。** 运行

   ```bash
   python tools/extract_strings.py --scaffold <locale>
   ```

   它会基于源语言为新语言区域生成脚手架文件。键是语义化的
   （`settings.automation.task_url.label`），条目可以带注释说明其上下文与
   占位符——请保留这些注释，以便后续译者也能保留上下文。
3. **只翻译值。** 不要重命名键、调整分区顺序，也不要翻译
   `{count}` / `{path}` 这类占位符；代码会原样替换它们。
4. 在提交拉取请求前**检查完整性**：

   ```bash
   python tools/check_lang.py --locale <locale>
   ```

   它会报告缺失的键以及上游已不存在的键。运行时的回退顺序是
   `requested locale → en → zh-Hans → the key itself`，因此缺失的键是可见的而不是
   静默的，但合并时仍应提交完整的语言包。
5. **在应用中测试。** 把 `backend/config.json` 中的 `language` 设为你的语言区域（或在
   设置中切换），检查界面是否立即更新、错误消息与 `/help` 是否跟随
   该语言，以及没有字符串溢出所在单元格或按钮——CJK 与德文文本尤其
   需要检查宽度。
6. **提交拉取请求**，标题类似 `新增 i18n：<locale> 语言包（界面 + 后端 + 文档）`，并列出
   你新增了哪些文件、哪些部分仍然缺失或由机器辅助完成。
7. 机器翻译可以作为起点，但要在拉取请求中说明，并且在把该语言区域
   宣传为受支持之前，预期会由母语者做一轮评审。

本地化文档应放在 `docs/<locale>/`。代码片段与命令名保持英文，
翻译其周围的说明文字。

## 贡献技能

技能是一个包含 `skill.json` 清单与条目包的文件夹——无需改动 EchoDesk
本身，这使技能成为扩展应用最简单的方式：

1. 阅读技能开发指南：[docs/zh-Hans/SKILL_GUIDE.md](docs/zh-Hans/SKILL_GUIDE.md)
   （中文）与 [docs/en/SKILL_GUIDE.md](docs/en/SKILL_GUIDE.md)（英文）。两个版本都涵盖
   清单格式、能力声明与评审规则。
2. 只声明你实际使用的能力（`commands`、`ui_panels`、`automation_hooks`、
   `file_parsers`），写明许可证，并让清单如实说明网络访问情况。
3. 发布前先在本地测试：从本地插件路径运行该技能，确认它的指令会
   出现并能执行，并确认失败的技能产生日志条目而不是让应用挂掉。
4. 在 GitHub 上发布并附 `README.md`，并给仓库添加 **`echodesk-plugin`** 主题。
   EchoDesk 只发现带有该主题的仓库，根目录 `skill.json` 缺少 `echodesk.bundle`
   的仓库会显示为未验证，无法安装。
5. 可选：使用 [`.github/`](.github/) 中的模板提交技能提交 issue，让维护者
   评审权限声明，如果一切正常，会在发布说明中提及。

技能以你的用户权限运行。EchoDesk 不会自动执行打包的 `prepare` 脚本，
并且在任何此类执行之前，会剥离名称中包含 `KEY`、`SECRET`、`TOKEN` 或 `PASSWORD`
的环境变量——不要设计依赖环境中存在密钥的技能。

## 仓库卫生

以下内容绝不要提交（`.gitignore` 已覆盖其中大部分）：

- 模型文件（`*.gguf`）以及 `publish/`、`build/`、`dist/` 输出目录树。
- 用户数据：`session.json`、`snippets.json`、`ui_state.json`、`browser_state.json`、
  `automation_log.jsonl`、`results/`、`skills/`、
  `skills_registry.json`、`models_registry.json`、`market_cache.json`。
- 崩溃日志（`frontend_crash.log`、`backend_crash.log`）。
- 任何包含 token、密码或个人路径的文件——包括 `backend/config.json` 中的真实 GitHub token
  （仓库中 `marketplace.github_token` 必须保持为空；个人取值放在
  `backend/config.user.json`，该文件已被忽略）。
- 包含真实文档、客户名称或内部 URL 的截图或日志。

提交拉取请求前，运行 `git status` 确认没有意料之外的文件被暂存。
`git status`、`git add`、`git commit` 在 Windows、macOS、Linux 上行为一致，
唯一的差别在 `.gitattributes`：`.ps1`/`.bat`/`.cmd` 为 CRLF，其他文件为 LF。
不要为纯粹的空白改动重新格式化整个文件。

## 贡献的许可

EchoDesk 以 [Apache License 2.0](LICENSE) 发布，贡献按同一许可接受
（入站 = 出站）；无需单独的贡献者许可协议。提交拉取请求即表示你确认
该贡献由你编写，或你有权提交它，并且它不会引入许可与 Apache-2.0 冲突的依赖。

如果你的改动新增、移除或升级依赖，请重新生成清单
（`python tools/gen_licenses.py`，三平台同一条命令）并在拉取请求中说明，以便
[NOTICE](NOTICE) 与 [docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md) 保持准确。
