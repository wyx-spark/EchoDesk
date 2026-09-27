# 为 EchoDesk 贡献代码

> 本文件是 [`CONTRIBUTING.md`](CONTRIBUTING.md) 的简体中文参考译本。如两者有歧义，**以英文版为准**。

感谢你抽出时间提供帮助。EchoDesk 是一款面向 Windows 的本地优先 AI 桌面助手，
任何形式的贡献都有价值：缺陷报告、修复、文档、翻译与新的技能。

参与本项目即表示你同意遵守[行为准则](CODE_OF_CONDUCT.md)。

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
- **环境**：Windows 版本与内部版本号，是否安装了 WPS Office / Microsoft Office 与
  Microsoft Edge，以及你运行的是便携版还是从源码构建的版本。
- **准确步骤**：按顺序列出你点击或输入的内容。自动化相关问题请附上
  选择器配置与 URL 模式（涉密内容请脱敏）。
- **预期行为与实际行为**：你期望发生什么，实际发生了什么。
- **日志**：日志位置见 [SECURITY.md](SECURITY.md)——两份崩溃日志分别是
  程序目录下的 `frontend_crash.log` 和 `backend\` 内的 `backend_crash.log`。
  两者都在失败时追加写入，因此文件为空表示「没有记录到崩溃」。

对于任何看起来像安全问题的内容，**不要开公开 issue**——请遵循
[SECURITY.md](SECURITY.md) 中的私密报告流程。

## 开发环境

要求：

- 后端（`backend/`）需要 **Python 3.13**。
- 一个能构建前端目标框架 `net10.0-windows` 的 **.NET SDK**
  （见 `frontend/EchoDesk.csproj`）。
- 自动化相关工作可选：本地安装 **Microsoft Edge**，因为自动化引擎
  通过 Playwright 驱动系统 Edge（`channel = msedge`）。

```powershell
# Backend: create a virtual environment and install the pinned dependency set
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt

# Frontend
dotnet build frontend\EchoDesk.csproj -c Release
```

WPF 前端启动的是**打包好的**后端可执行文件（发布版中为 `backend\EchoDeskBackend.exe`，
用 PyInstaller 构建时为 `dist\EchoDeskBackend\EchoDeskBackend.exe`）。
`ECHODESK_BACKEND` 可以覆盖该路径，这是前端开发最快的循环方式：

```powershell
# Build the backend once (product: dist\EchoDeskBackend\EchoDeskBackend.exe)
pyinstaller --noconfirm --distpath dist --workpath build\pyi backend\EchoBackend.spec

# Run the frontend from source against it
$env:ECHODESK_BACKEND = "$PWD\dist\EchoDeskBackend\EchoDeskBackend.exe"
dotnet run --project frontend\EchoDesk.csproj
```

后端也可以独立运行以便调试协议（`python backend\main.py`）：它从 stdin 每行读取一个
JSON 请求，并向 stdout 每行写入一个 JSON 响应。动作列表、响应类型与模块映射见
[docs/DEVELOPER.md](docs/DEVELOPER.md)。

只有真正做推理时才需要模型：把 `.gguf` 文件放入 `backend\models\`（源码运行）
或打包后端的 `models\` 文件夹。其他一切——解析、自动化管线、
i18n、协议——都可以在没有模型的情况下开发与测试。

## 构建与打包

```powershell
# Full portable build -> publish\EchoDesk-v0.2.1\
& tools\build_release.ps1

& tools\build_release.ps1 -SkipBackend     # only rebuild the WPF frontend
& tools\build_release.ps1 -SkipFrontend    # only rebuild the packaged Python backend
& tools\build_release.ps1 -SkipClean -SkipBackend -SkipFrontend   # re-run resource/metadata steps
& tools\build_release.ps1 -IncludeModels   # include local .gguf models (large offline bundle)
```

构建中有两点容易出错，脚本里都加了注释：

- `rcedit` **不得**触碰 PyInstaller 的输出（它会重建 PE 映像并破坏追加的
  PKG 归档）；后端改为通过 spec 写入自己的版本资源与图标。
- PowerShell 可能把原生命令的 stderr 当作终止性错误，这曾导致构建在出现
  警告时中止；`tools\build_release.ps1` 因此封装了原生调用。

另请注意**包体积**：EchoDesk 发布的是自包含前端加 PyInstaller 后端，因此新增一个体积大
——或会引入庞大传递依赖树——的依赖是实实在在的代价。优先使用标准库，优先选择体积小、
许可宽松的库，并在拉取请求中说明新依赖对体积的影响。

## 测试

```powershell
# Required checks for any pull request
python tools\selftest.py             # backend unit-level self-test (exit code 0 = pass)
python tools\test_file_parsers.py    # smoke test for every supported file format

# Additional suites, depending on what you touched
python tools\test_plaintext.py       # Markdown -> plain-text conversion (result files)
python tools\test_results_dir.py     # automation result folder layout and result.txt
python tools\test_automation_e2e.py  # end-to-end automation against a local static site (needs Edge + Playwright)
python tools\test_model_hub_e2e.py   # model-hub protocol against the packaged backend
powershell -File tools\ui_probe.ps1  # WPF UI probe: settings window opens fast, no crash log
python tools\gen_licenses.py         # regenerate docs\THIRD_PARTY_LICENSES.md
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
- 围栏代码块要带语言标记（`powershell`、`jsonc`、`text`）。
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

   ```powershell
   python tools\extract_strings.py --scaffold <locale>
   ```

   它会基于源语言为新语言区域生成脚手架文件。键是语义化的
   （`settings.automation.task_url.label`），条目可以带注释说明其上下文与
   占位符——请保留这些注释，以便后续译者也能保留上下文。
3. **只翻译值。** 不要重命名键、调整分区顺序，也不要翻译
   `{count}` / `{path}` 这类占位符；代码会原样替换它们。
4. 在提交拉取请求前**检查完整性**：

   ```powershell
   python tools\check_lang.py --locale <locale>
   ```

   它会报告缺失的键以及上游已不存在的键。运行时的回退顺序是
   `requested locale → en → zh-Hans → the key itself`，因此缺失的键是可见的而不是
   静默的，但合并时仍应提交完整的语言包。
5. **在应用中测试。** 把 `backend\config.json` 中的 `language` 设为你的语言区域（或在
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
- 任何包含 token、密码或个人路径的文件——包括 `backend\config.json` 中的真实 GitHub token
  （仓库中 `marketplace.github_token` 必须保持为空；个人取值放在
  `backend\config.user.json`，该文件已被忽略）。
- 包含真实文档、客户名称或内部 URL 的截图或日志。

提交拉取请求前，运行 `git status` 确认没有意料之外的文件被暂存。
保持现有的 `.gitattributes` 换行符规则：`.ps1`/`.bat`/`.cmd` 为 CRLF，
其他文件为 LF。不要为纯粹的空白改动重新格式化整个文件。

## 贡献的许可

EchoDesk 以 [Apache License 2.0](LICENSE) 发布，贡献按同一许可接受
（入站 = 出站）；无需单独的贡献者许可协议。提交拉取请求即表示你确认
该贡献由你编写，或你有权提交它，并且它不会引入许可与 Apache-2.0 冲突的依赖。

如果你的改动新增、移除或升级依赖，请重新生成清单
（`python tools\gen_licenses.py`）并在拉取请求中说明，以便
[NOTICE](NOTICE) 与 [docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md) 保持准确。
