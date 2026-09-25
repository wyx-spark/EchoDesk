# EchoDesk 变更日志

本文件记录各版本的功能变更、修复与已知限制。
版本号遵循 `MAJOR.MINOR.PATCH`；文件版本资源统一为 `X.Y.Z.0`。

---

## v0.2.1

> 面向**开源发布**的第一个版本。完整实现方案、关键决策、与任务书的偏差、
> 未完成项与体积记录见内部研发记录（`v0.2.1-研发记录.md`，**不随发布物分发**）。

### 发布准备（发布任务书）

**交付物**
- 新增两份自解压绿化安装包：`EchoDesk-v0.2.1-Setup.exe`（含模型）与
  `EchoDesk-v0.2.1-NoModel-Setup.exe`（不含模型），由 `7zS.sfx` + `config.txt` + `payload.7z`
  拼接而成，解压后自动执行 `install-complete.bat` 创建启动快捷方式，并用 `rcedit`
  写入九个版本资源字段与图标（`FileVersion`/`ProductVersion` = `0.2.1.0`）。
- 新增 `SHA256SUMS.txt`（安装包与合规资产的 SHA256 清单）、`USAGE.txt`（结构与操作说明，
  中英双语）、`config.example.json`（后端默认配置副本）、`install-complete.bat`。
- 构建脚本新增 `-MakeSfx` 开关：一条命令产出完整交付物（`tools/make_sfx.ps1` 承担实际打包）。

**命名规范：程序生成的名字也改成英文**（面向全球用户）
- `使用说明.txt` → `USAGE.txt`；结果目录 `批量处理结果\` → `results\`；
  结果文件 `结果.txt` → `result.txt`；默认任务目录名 `自动化任务` → `auto-task`；
  失败截图 `失败截图_任务N_时间.png` → `failure-screenshot-taskN-时间.png`；
  快捷方式 `EchoDesk-v0.2.1.lnk` → `Start-EchoDesk-v0.2.1.lnk`（与自解压安装包一致）。
- **发布物不再预置 `.lnk` 快捷方式**：Windows 快捷方式里存的是**绝对路径**，
  在构建机上生成的 `.lnk` 会带上维护者的本机路径（`C:\Users\<用户名>\...`），
  既违反任务书「全局要求五」（个人绝对路径绝对禁止），在用户机器上又必然是死链。
  现在快捷方式由 `install-complete.bat` 在目标机器上创建：自解压安装包解压后**自动**
  执行它；使用 zip 免安装解压版的用户需手动运行一次（或直接启动 `app\EchoDesk.exe`，
  程序首次运行会询问是否创建快捷方式）。
- **升级注意**：结果目录名是用户数据，改名后**不会自动迁移**旧目录。升级前请把
  旧的 `批量处理结果\` 手动改名为 `results\`（或把旧目录内容移入新目录），
  否则新版本会在新名字下重新开始，历史结果仍留在旧目录里（数据不会丢，只是分两处）。

**合规文档双语化（7 对）**
- `LICENSE` / `NOTICE` / `SECURITY.md` / `CODE_OF_CONDUCT.md` / `docs/PRIVACY.md` /
  `docs/EULA.md` / `docs/THIRD_PARTY_LICENSES.md` 全部改为「英文为默认名 + `.zh-Hans.md`
  中文参考译本」，中文版文首注明「如与英文版有歧义，以英文版为准」。
- `docs/PRIVACY.md`、`docs/EULA.md`、`docs/THIRD_PARTY_LICENSES.md` 原先正文是中文，
  本次改为英文为默认版、中文移入 `.zh-Hans.md`；安全性策略正本移到仓库根目录，
  `docs/SECURITY.md` 改为短指针。
- `CHANGELOG.md` 移到**仓库根目录**（GitHub 直接识别），docs\ 下不再保留副本。

**反馈渠道统一**
- 公开反馈统一指向 GitHub Issues 与 Discussions；安全漏洞指向仓库 Security 标签页的
  私密公告入口（首选）与 GitHub noreply 邮箱（兜底）。清除了全部占位邮箱
  （`security@example.com`、`conduct@example.com`）与示例捐赠 URL。
- 新增门禁 `tools/check_feedback.ps1`：校验上述渠道存在、并扫描个人邮箱、
  实体地址、个人绝对路径、凭证明文与 AGPL 残留。

**合规收口发现并修掉的问题（干净 venv 复核）**
- **`backend/requirements.txt` 漏声明 3 个直接 import 的包**：`requests`、`urllib3`
  （`marketplace.py`）与 `tqdm`（`download_utils.py`）。它们在开发机/打包机上恰好作为
  传递依赖存在，所以一直被掩盖；按本文件从零建环境会 ImportError。
  新增 `tools/check_requirements.py`（AST 取导入 + 实测 import）作为门禁。
- **发布物含未声明的第三方组件**：`cryptography` 被 PyInstaller 打进后端
  （来自开发机上与本项目无关的 `msoffcrypto-tool`），却不在许可证清单与 SBOM 里。
  它**确实被功能需要**（`file_parser.py` 对空口令加密 PDF 调 `reader.decrypt("")`，
  pypdf 的 AES 支持依赖它），因此选择**显式声明**而不是排除。
- 同时发现 `chardet`、`bidi`（均为 LGPL）与 `tzdata` 只是被依赖里的**可选 import**
  带进包，本项目用不到，已加入 `backend/EchoBackend.spec` 的 `EXCLUDES`
  （发布物因此不再含这两个 LGPL 组件，也少了约 2MB）。
  新增 `tools/check_bundled_packages.py` 比对「干净 venv 声明的包」与
  「`_internal` 里实际存在的模块」，防止这类偏差再次悄悄出现。
- **`tools/gen_licenses.py` 的名称归一化缺陷**：用 `name.lower()` 直接比较发行名，
  而 `llama-cpp-python`/`duckduckgo-search` 的元数据名是下划线形式，导致这两个
  **直接依赖被漏出「后端直接依赖」表**。已改为按 requirements.txt 驱动 + PEP 503 归一化。
- **`tools/gen_sbom.py` 的闭包不计环境标记**：`macholib`、`tomli`、`exceptiongroup`
  等跨平台标记依赖会被算进发布闭包，使 `--check` 在干净环境里永远失败（门禁失效）。
  已改为按当前解释器评估 `Requires-Dist` 标记（`packaging` 为可选依赖，缺失时退化并提示）。
- 收紧 `backend/EchoBackend.spec` 的 `EXCLUDES` 说明与文档：经二进制内嵌字符串核对，
  `publish\7zS.sfx` 只支持 `Title`/`BeginPrompt`/`Progress`/`RunProgram`/`Directory`/
  `ExecuteFile`/`ExecuteParameters`，**没有** `OverwriteMode` 等第三方改版才有的键。

**构建流程修复**
- **`tools/build_release.ps1` 失败仍返回退出码 0**：脚本内部抛错时不会设置退出码，
  `& tools\build_release.ps1 -MakeSfx` 曾出现"自解压包一个都没生成却报告成功"。
  已加顶层 `trap`（统一非 0 退出）与 `-MakeSfx` 后的交付物存在性断言。
- 发布物不再包含研发/修复记录等过程文档（`-Recurse` 复制时按名字排除 + 结构断言）。
- 发布文档不再含"截图占位/待拍"这类未完成表述（v0.2.1 不随包提供截图）。

**新增/更新的自动检查**
- `tools/make_sfx.ps1`（可行性与检查 + payload + 拼接 + rcedit + SHA256 全流程，可独立运行）、
  `tools/check_feedback.ps1`、`tools/check_requirements.py`、
  `tools/check_bundled_packages.py`。
- `tools/verify_release.ps1` 结构断言扩展到 34 项必备文件 + 全英文命名 + 无过程文档 +
  无运行期残留 + 自解压包哈希/版本资源/可解压性 + 两项发布门禁。
- CI 新增 `release-gates` 作业（反馈渠道、依赖声明、SBOM 一致性、发布脚本语法）；
  发布工作流改为产出两份自解压包并附上 SBOM 与许可证清单，Release 默认存为**草稿**。

### 新增

**开源化**
- 新增 `LICENSE`（**Apache-2.0**）、`NOTICE`、`README.md`（英文主）、`README.zh-Hans.md`、
  `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`（Contributor Covenant 2.1）、`SECURITY.md`。
- 新增 `.github/`：`workflows/ci.yml`（后端测试 / 前端构建 / 体积检查 / 文档检查 / 语言包检查）、
  `workflows/release.yml`（打标签自动出包并创建 Release）、4 类 Issue 模板
  （Bug / 功能请求 / **技能提交** / **翻译贡献**）、PR 模板、Dependabot 配置。
- 文档按语言重组：`docs/zh-Hans/`（用户手册、FAQ、技能开发指南、自动化填写指南）、
  `docs/en/`（同上一套英文版）。
- **合规文档与物料清单补齐**：`docs/PRIVACY.md` 逐项列明 5 个联网功能（域名、发送内容、
  不发送内容、数据去向、保留时长、关闭方式）、数据出境说明与"彻底禁网"清单；
  `docs/EULA.md` 补齐责任限制、第三方开源许可声明、自动化与技能的责任归属、
  联系方式与协议变更方式；新增 `docs/SECURITY.md`（中文安全策略，与根目录 `SECURITY.md`
  同一套承诺）与 `docs/zh-Hans/COMPLIANCE.md`、`docs/en/COMPLIANCE.md`
  （企业环境适配：代理、证书、防火墙域名白名单、组策略与受限环境、审计留痕）；
  新增 `docs/SBOM.json`（SPDX 2.3 风格物料清单）及生成/核对工具 `tools/gen_sbom.py`
  （纯标准库，`--check` 可在 CI 中作为不一致的失败条件）。

**多语言（简体中文 + 英语）**
- 后端 `backend/i18n.py`、前端 `frontend/Services/Localization.cs`：语言包加载、
  **回退链**（当前语言 → en → zh-Hans → 键名本身）、按需加载、模块分片
  （`<locale>.<模块>.json`）。
- 设置窗口「常规」页新增**界面语言**下拉：切换后界面、错误提示、指令帮助立即生效，
  **无需重启**；选择结果记入 `ui_state.json`；首次启动按系统区域自动选择。
- 语言包**外置**在程序目录的 `lang\`：社区可以直接丢一个新的 `*.json` 贡献语言，无需重新编译。
- 指令帮助与指令面板改为共用后端的本地化指令元数据（`{"action":"commands"}`），
  技能注册的指令会自动出现在候选列表里。
- 新增工具：`tools/check_lang.py`（键与**占位符**一致性检查、`--scaffold` 生成新语言模板）、
  `tools/extract_strings.py`（盘点尚未收编的界面文本、生成模板）。

**技能运行期加载器与本地插件接口**
- 新增 `backend/skills_runtime.py`：**装上就能用**——技能的命令真正挂到 `/` 指令表，
  行为与内置指令一致；导入/注册/执行三层异常隔离；卸载时清理模块缓存、`sys.path`、
  命令与解析器与钩子。
- 技能 API 六类能力：命令注册、UI 面板（声明式，前端用原生控件渲染）、
  自动化钩子（`before_task` / `after_download` / `after_summary` / `after_task`）、
  文件解析扩展、技能私有配置、技能自带语言包。
- 本地插件接口：`backend/skills_dev/`（开发，不写注册表）+ `backend/skills/`（正式安装）
  + `skills_local.json`（任意路径、别名、启用禁用、加载顺序、信任状态）；
  优先级 `skills_dev` > 清单 > `skills`，冲突时明确提示"当前加载的是开发版本，已覆盖已安装版本"。
- 设置窗口新增「**已安装**」页（插件清单、加载状态、命令/格式/钩子/面板、来源标签
  `🏪 市场`/`📁 本地`/`🔧 开发`、错误详情、打开目录、导出 zip、发布自检、卸载）
  与「**开发者**」页（开发者模式开关、插件目录、热重载、日志查看/导出/清空、
  新建插件向导、本地插件清单可视化编辑、已挂接钩子、插件面板）。
- 安全：执行技能前**临时移除**名称含 KEY/SECRET/TOKEN/PASSWORD 的环境变量；
  正式加载走 `skills_registry.json` 白名单；首次加载未知来源本地插件弹确认框；
  界面展示技能声明的权限。
- 新增两个官方示例插件：`examples/skills/hello-world/`（命令 + 面板 + 钩子 + 双语）、
  `examples/skills/ofd-reader/`（`.ofd` 文件解析扩展，零第三方依赖）。

**自动化配置向导与填写帮助**
- **🎯 可视化选取**：打开浏览器，在页面上直接点一下目标元素即自动生成 CSS 选择器并回填；
  悬停高亮并实时显示选择器与匹配数量；支持任务列表元素 / 任务行容器 / 下一页按钮 /
  详情页附件四种目标（详情页会先自动进入第一个任务）；登录态与自动化共用
  `browser_state.json`；按 Esc 取消。
- **字段级帮助**：27 个字段的通俗说明与示例全部来自语言包；核心字段旁有「?」按钮
  （悬停看说明、点击看详情），文本框获得焦点时下方内联显示说明；「📖 填写帮助」
  可一次看完全部字段。
- **配置模板库**：合同财务审核 / 发票信息提取 / 报告要点汇总 / 表格数据核对四个预置模板，
  一键填入；支持「存为自定义模板」。
- **四步测试向导**：任务列表元素 → 下一页按钮（真的点一次并报告是否跳转）→
  详情页附件（统计后缀命中数）→ **试运行一个任务**（不写结果文件、不记处理记录）；
  每步给出失败原因与修正建议。
- **配置导出/导入**（`.echodesk-auto`）：导出排除登录态与临时目录；
  导入不继承安全协议与总开关（必须本机亲自勾选）；语言不同时提示但不阻断。
- 新增文档 `docs/zh-Hans/AUTOMATION_GUIDE.md` 与 `docs/en/AUTOMATION_GUIDE.md`
  （字段总览、逐字段说明、完整示例、向导流程、15 行排查表、模板说明、导入导出、安全与审计）。

**自动更新（方案 A：只检查与提示）**
- 新增 `backend/update_check.py`：检查 GitHub Releases 并做语义化版本比较；
  发现新版本时状态区出现可点提示条，点击用系统浏览器打开下载页。
  **不下载、不替换任何文件、不静默安装**；尊重系统代理与证书；可通过配置整体禁用；
  默认 24 小时内不重复检查；只用标准库 `urllib` + `json`，未引入新依赖。

### 变更

- **移除 AGPL 依赖**：PDF 解析从 **PyMuPDF**（AGPL-3.0/商业双许可）换成 **pypdf**
  （BSD-3-Clause，纯 Python）。项目本体为 Apache-2.0，与 AGPL 不兼容；替换后发布物中
  不再含 AGPL 组件，剩余唯一 GPL 组件是构建工具 PyInstaller（GPLv2 带官方例外）。
  同步收益：该依赖的安装体积从 **52.9MB 降到约 1.4MB**。
- 状态栏的搜索/模式状态改由后端响应的**机器可读字段**驱动（此前靠匹配中文文本，
  切到英文界面后会失效）。
- 安全协议正文更新：第 3 条从"下载文件处理完成后立即删除"改为
  "**已下载的附件会保留在结果目录中**便于复核"——与 v0.2.0 新增三的结果目录设计一致。
- 设置窗口「技能市场 → 已安装」按钮改为跳到独立的「已安装」页。
- 版本号 `0.2.0` → `0.2.1`（前后端 exe 文件版本统一为 `0.2.1.0`）。

### 修复

- `tools/test_file_parsers.py` 不再依赖 PyMuPDF 造 PDF 样本（改用裸 PDF 语法手工生成
  含 xref 表的单页 PDF），使该测试在只装 `backend/requirements.txt` 的干净环境里也能通过。
- 打包脚本的文档收集改为递归：`docs/zh-Hans/`、`docs/en/` 下的本地化文档现在会进发布包。
- 修正安全协议与 PRIVACY/USER_GUIDE/FAQ 中关于"下载文件被删除"的过时描述。

### 修复（v0.2.1 修复任务书，P0/P1 共七项）

> 完整排查过程、决策理由与逐项验证证据见内部修复记录（`v0.2.1-修复记录.md`，**不随发布物分发**）。

**P0 · 开发者模式弹窗循环与无法关闭**
- 逐个弹窗改为**一次性汇总确认框**（新窗口 `frontend/Utilities/TrustDialog.cs`）：列出每个待确认
  插件的来源、路径、声明能力与权限，支持逐个勾选 / 全部启用 / 全部禁用；后端新增
  `skills_trust_bulk` 动作一次接收全部决定。
- `skills_local.json` 新增 `denied` 列表（schema 1 → 2，旧文件兼容）：**用户明确拒绝过的插件
  不再重复询问**，也不再加载（原实现只记 trusted，点"否"等于没答，于是永远问下去）。
- 修复**"一旦开启就关不掉"**：新增 `dev_mode_explicit` 配置。用户显式设置过开关后，
  开发者模式完全以配置为准，不再被"`skills_dev/` 里有插件就自动启用"的逻辑覆盖。
- 关闭开发者模式时**真的卸载** dev/local 插件（新增 `SkillLoader.unload_dev()`）并从指令表移除，
  同时停止热重载、清空开发日志。
- **内置示例插件静默**：`builtin_ids()` 按随包 `examples/skills/` 目录判定，示例插件默认启用且
  不弹确认框；第三方插件仍需确认（不采用"插件自报 official"的做法，那等于把确认框变成摆设）。
- 开发者 Tab 新增"当前状态 + 由谁决定 + 已启用的开发插件清单"，关闭后清单为空，一眼可确认。
- 回归测试：新增 `tools/test_dev_mode.py`（46 项）。

**P0 · 深色模式失效与对比度不足**
- 修复**深色模式永远出不来**：`MainWindow_Loaded` 原来只在"配置为浅色"时才应用主题，
  配置为深色时 `App.xaml` 里的浅色字典原样留着。现在启动无条件经 `ThemeService.Apply()` 应用。
- 主题状态收敛到新的 `frontend/Services/ThemeService.cs`：三态（**跟随系统** / 浅色 / 深色），
  跟随系统通过 `SystemEvents.UserPreferenceChanged` + 注册表 `AppsUseLightTheme` 实现，
  设置窗口的复选框换成 `ThemeCombo` 并显示"当前实际生效的是哪一种"。
- 语义色板从 10 个键扩到 **47 个键**（背景/表面/文字/强调/状态/按钮四态/选中/页签/滚动条/浮层/
  角色/Markdown），深浅两套键名完全一致；为 `ComboBox`、`ListBox(Item)`、`TabControl(TabItem)`、
  `CheckBox`、`RadioButton`、`ScrollBar`、`ToolTip`、`ContextMenu`、`ProgressBar`、`RichTextBox`
  补齐控件模板，全部用 `{DynamicResource}`。
- 修复**"切换后残留旧色"**：原来代码用 `TryFindResource` 取的是**取值快照**，已渲染的消息保留旧色；
  改为 `SetResourceReference`（资源引用），`MarkdownRenderer` 的 `FlowDocument` 画刷同样改为资源引用，
  无需重建文档即可跟随主题。
- 对比度按 WCAG 2.1 复核并修正：输入框/按钮边框 1.53:1 → 3.13:1（浅色）、1.52:1 → 3.6:1（深色）；
  滚动条滑块 1.76:1 → 3.11:1；`Brushes.IndianRed`/`MediumSeaGreen`/`#e06c6c` 三处硬编码颜色改走色板。
- 新增 `tools/check_theme.py`（色板一致性 + **43 组强制对比度** + 禁止硬编码色值 + 校验所有资源引用存在）
  与 `tools/check_ui_pixels.ps1`（**真实渲染**校验：启动程序、按主题截图、量主窗口平均亮度与
  逐控件局部对比度；实测深色 0.076 / 浅色 0.850，文字最差 6.24:1 / 5.99:1）。

**P0 · 发布目录结构**
- 改为**便携版三层结构**：程序本体全部收进 `app\`，外层只留快捷方式、用户数据、结果目录、
  语言包、示例与文档；构建脚本末尾新增断言，外层出现任何 exe/dll/pdb 即构建失败。
- 新增 `ShortcutService.UserDataDirectory`（环境变量 `ECHODESK_DATA` → 父目录名为 `app` 且可写 → 程序目录）
  与后端 `config.user_data_root()`（判定顺序保持一致）；`ui_state.json`、`snippets.json`、
  `results\` 全部落在**外层**。
- 快捷方式改名 `EchoDesk-v0.2.1.lnk` 放外层，`WorkingDirectory` = 外层；失效判定新增"工作目录不对"
  这一情况。语言包搜索链同时纳入外层与 `app\`，用户丢一个新的 `lang\ui\<locale>.json` 即可加语言，
  升级不覆盖。
- 构建脚本新增 `-ReuseBackendFrom`（只改前端时复用既有后端跳过 PyInstaller），并在其指向本次输出
  目录时直接报错。`tools/installer.iss` 同步对齐安装版结构。

**P0 · Emoji 显示为方块**
- 根因实测确认：`Segoe UI` / `Consolas` / `Microsoft YaHei UI` 对项目在用的 emoji **覆盖率为 0/20**，
  而字体链里根本没有 emoji 字体。新增三条带回退链的字体资源 `UiFontFamily` / `MonoFontFamily` /
  `IconFontFamily`（含 `Segoe UI Emoji` 与 `Segoe UI Symbol`），`MarkdownRenderer` 同步改用。
- 新增后端 `backend/emoji.py`（启动即切 UTF-8、按编码与控制台代码页判断能否输出 emoji、
  不能则降级为 `[OK]`/`[!]`，支持 `--no-emoji` 与 `ECHODESK_NO_EMOJI`）与前端
  `frontend/Services/Emoji.cs`（同表降级；设置窗口新增"使用 emoji"三态选项）；
  未覆盖的 emoji 一律降级为 `?`，**绝不显示方块**。
- 新增 `tools/check_emoji.ps1`（扫出在用的 61 个 emoji，逐个查三条字体链的字形覆盖率并集，
  并校验前后端 ASCII 降级表覆盖完整）。

**P1 · 双语翻译不完整**
- 界面与后端用户可见文本全面收编进语言包（主窗口、设置窗口六个 Tab、自动化、技能市场、
  模型中心、开发者 Tab、对话框、系统消息、错误提示）；补充**术语表**统一关键译法；
  含变量的文本一律改为占位符。
- 前端与后端语言包同步扩充（键数见修复记录 §10），`tools/check_lang.py` 的"缺键"与
  "占位符不一致"检查作为 CI 门禁；`docs/<locale>/` 四份文档齐备。

**P1 · 模型能力辅助与上下文管理**
- 新增 `backend/model_profile.py`：参数量（元数据/文件名推断，**标注来源**）、量化格式
  （`general.file_type` 数字枚举 → `Q4_K_M` 等可读名）、训练窗口、实际可用窗口、KV Cache 估算、
  实测生成速度；五种**可操作的**能力提示；按内存/核数的硬件推荐（附推荐依据）。
- 新增 `backend/prompts.py`：6 个任务型提示词模板（摘要/合同审查/结构化抽取/翻译/代码排错/分步推理），
  每个都含具体角色、输出格式约束、思维链引导与少样本示例；`/task` 套用。
- 新增 `/preset light|balanced|quality` 三档生成参数（温度/top_p/重复惩罚/回复长度），
  `llm_engine` 相应支持 `top_p` 与 `repeat_penalty`。
- **上下文管理增强**：状态区新增 `🗜 压缩上下文` 按钮（`/compact` 同源），新增 `context_mode`
  滑动窗口模式，分段摘要参数（`chunk_chars`/`overlap_chars`/`max_summary_tokens`/…）可运行期调整。
- 状态区新增模型能力项（参数量 · 量化 · 可用窗口 · 实测速度，实时刷新）与能力提示按钮；
  模糊提问改为**追问**而不是硬答（可关闭）。
- 新增 `SummaryCache`：分段摘要按"文件指纹 + 摘要参数"缓存，同一文件二次上传不重算；
  文件改动后自动失效。
- 回归测试：新增 `tools/test_capability.py`（58 项）。

**P1 · 合规发布事宜**
- `docs/PRIVACY.md` 扩写为"本机/联网"约定 + 5 个联网功能逐项表（目标域名、发送什么、不发送什么、
  数据去向、保留多久、如何关闭）+ **数据出境与跨境传输专节** + 企业"彻底禁网"清单。
- `docs/EULA.md` 补齐许可授予（Apache-2.0 优先条款）、使用限制、第三方组件声明、自动化与技能
  责任归属、独立的责任限制条款、适用法律、联系方式与协议变更方式；并纠正 pyxtxt/AGPL 等过时表述。
- 新增 `docs/SECURITY.md`（支持版本、报告渠道、**响应时间承诺**、不受理范围、安全设计说明与
  已知限制），根目录 `SECURITY.md` 与之对齐；新增 `docs/zh-Hans/COMPLIANCE.md` 与
  `docs/en/COMPLIANCE.md`（代理、证书、防火墙最小放行清单、组策略/受限环境、审计留痕）。
- 新增 `tools/gen_sbom.py`（纯标准库）与 `docs/SBOM.json`（SPDX 2.3，52 个包）；
  `--check` 可对照 `THIRD_PARTY_LICENSES.md` 报差异，已实际发现并记录清单缺登记项。

### 已知限制

- 自动化、技能市场、模型中心的**逐条进度与深层诊断消息**仍为中文（界面与指令帮助已全部
  双语）。可用 `python tools/extract_strings.py` 复现待收编清单。
- 只提供**简体中文与英语**语言包；`zh-Hant`/`ja`/`ko`/`de`/`fr`/`es`/`pt-BR` 的机制与
  模板生成工具已就绪，待社区贡献。
- 示例技能尚未真正发布到 GitHub marketplace（需要仓库凭据）。
- 自动更新只到"检查与提示"，不含自动下载替换与增量更新。
- **v0.2.1 不随包提供界面截图**：README 与文档不再留"待拍截图"的占位指引，
  改为说明"后续版本会放入 `assets/screenshots/`"。
- `docs/THIRD_PARTY_LICENSES.md` 与 `docs/SBOM.json` 现已**在只装
  `backend/requirements.txt` 的干净 venv 里重新生成并逐项核对一致**
  （55 个发行包、0 处差异；见上文「合规收口发现并修掉的问题」）。
  剩余注意点是**生成基线依赖环境**：清单口径是"干净 venv 的实际安装结果"，
  下次发布仍须在干净环境里重跑 `python tools/gen_licenses.py` 与
  `python tools/gen_sbom.py --check`（CI 的 `release-gates` 作业已自动执行后者）。
- 修复版遗留（详见内部修复记录 `v0.2.1-修复记录.md` §11，**不随发布物分发**）：
  - **未在 Windows 10 上实测**（Emoji 与主题结论基于字形覆盖率与色板数值，非系统版本）。
  - **安装版（Inno Setup）未实际编译验证**：本机无 Inno Setup 6，`tools/installer.iss` 已按
    便携版结构对齐，但未产出过 `setup.exe`。
  - **自动化安全协议未落地独立审计日志**（协议文本与勾选状态存在配置里，没有不可篡改流水）。
  - `model_hub.enabled` 与 `automation.start_with_windows` 两个配置项**未被代码读取**，
    不要把前者当禁网开关；开机自启请把快捷方式放进 `shell:startup`。
  - 向量检索（本地 RAG）**本版未做**：向量库依赖与全局体积要求冲突，建议下版先做零依赖的
    关键词检索方案验证收益。
  - `docs/DEVELOPER.md` 仍只有中文（`PRIVACY.md`/`EULA.md` 已完成双语化）。
  - **结果目录改名不自动迁移**（`批量处理结果\` → `results\`）：升级用户需手动改名，
    见上文「命名规范：程序生成的名字也改成英文」的升级注意。
  - **自解压包未做代码签名**：`-Sign` 需要证书，本版两份 `Setup.exe` 均未签名，
    首次运行会有 SmartScreen 提示。
  - **`7zS.sfx` 没有覆盖询问**：7-Zip 官方精简 SFX 模块不支持 `OverwriteMode`
    （已用二进制内嵌字符串核对），解压时同名文件会被直接覆盖；已在 `BeginPrompt`
    里提前明确告知，并在 `install-complete.bat` 创建快捷方式前询问是否覆盖。

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

### 修复与补强（v0.2.0 修复任务书）

> 对应根目录 `指示.md` 的五项任务，**不升版本号**，仍为 v0.2.0 / `0.2.0.0`。
> 完整实现方案、关键决策、偏差与未完成项见内部修复记录（`v0.2.0-修复记录.md`，**不随发布物分发**）。

**修复一：设置窗口卡死（P0 致命）**
- 真实根因并非死锁，而是 `SettingsWindow.xaml` 的滑块
  `<Slider Minimum="10" .../>` 在 **XAML 解析期**把 `Value` 从 0 强制抬到 10 并触发
  `ValueChanged`，此时 `FontSizeLabel` 尚未创建 → `FontSizeSlider_ValueChanged` 抛
  `NullReferenceException`；当时没有任何全局异常处理器，UI 线程未处理异常直接终止进程
  （WER 转储 29MB），表现就是"界面无响应，只能从任务管理器结束"，且不留线索。
- 修复：新增 `_uiReady` 门闩（`InitializeComponent` 完成前所有 XAML 挂接的处理器直接返回）、
  滑块回调判空、XAML 补默认值。
- 新增**全局异常兜底**：`DispatcherUnhandledException` / `AppDomain` / `TaskScheduler`
  统一写入程序目录 `frontend_crash.log`，并提示后让程序继续可用。
- **通信层重构**：stdout 改为唯一泵线程投递到队列，请求方从队列取行——
  这样设置超时才**不会留下悬挂读取**（原先"放弃读取"会吃掉下一条响应）。
  请求闸门等待默认 15 秒超时，响应空闲超时默认 10 分钟（重任务 45 分钟）。
- 设置窗口改为"打开即见"：构造/显示不做任何后端等待，数据在 `ContentRendered` 后异步加载，
  底部常驻加载状态；遇到后端忙自动重试并显示等待提示；关闭窗口取消在途请求。
- 启动期三个 `MessageBox` 补上 owner，避免对话框出现在主窗口后方造成"看不到提示、界面像卡住"。
- 实测：设置窗口 **207ms** 打开（要求 <500ms），Responding 全程 True，
  主/设置窗口交替操作互不阻塞，无崩溃日志。

**修复二：命令栏 Enter 误执行（P0 数据风险）**
- 原先指令列表只做前缀过滤且保持声明顺序，输入 `/clear` 时首项是 `/clearcache`，
  直接按 Enter 会清空 KV 缓存。
- 新增排序规则：**完全一致优先 → 命令名长度升序（前缀占比更高者更具体）→ 指令表原有顺序**。
- 候选**多于一个且首项非精确匹配**时，Enter **只补全命令名**并提示，由用户确认后再执行。
- 指令列表显示**完整名称与参数格式**（`用法：/model <名称|别名|list>`），
  列表项补齐可访问性名称。
- 新增**危险指令二次确认**：`/clear`、`/clearcache` 执行前提示后果，
  取消时保留输入；可在「设置 → 常规」关闭（默认开启）。
- 实测：`/clear`→首项 `/clear`，`/c`→`/clear`，`/cl`→`/clear`，`/sum`→`/summarize`；
  在 `/cl` 上按 Enter 只补全为 `/clear `；`/clear` 确认框明确指向 `/clear`。

**新增三：results目录（P1）**
- 结果按 `程序根目录\results\YYYY-MM-DD\任务名\` 分层保存（顶层名与保存位置可自定义，
  顶层留空 = 程序根目录）；同日多次运行合并到同一日期目录，同名任务自动追加 `-1`、`-2`。
- 任务目录内保存**该轮下载的原始文件**（重名自动加序号）与 `result.txt`；
  失败任务的失败原因与失败截图文件名同样写入 `result.txt`，截图保存在任务目录内。
- `result.txt` 为**纯文本、无任何 Markdown 标记**（记事本无渲染能力）：新增
  `backend\plaintext.py` 做 Markdown → 纯文本转换，标题转下划线、表格转**按显示宽度对齐**
  的纯文本（中日韩按 2 列计）、列表保留 `-`/数字、链接转 `文字（地址）`、代码块缩进。
- 每条任务完成/失败即整份重写 `result.txt`，中途崩溃或停止也不丢已处理结果。
- 新增自测：`tools\test_plaintext.py`（20 项）、`tools\test_results_dir.py`（26 项）、
  `tools\test_automation_e2e.py`（真实 Playwright/Edge 全链路 21 项，实测跑出三层目录、
  纯文本结果、原始文件保留、同名 `-1`）。

**新增四：模型选择栏（P1）**
- 设置窗口新增「模型」Tab：连接 **hf-mirror.com（镜像）** 与 **huggingface.co（官方）**，
  支持自动探测与不可达回退，并始终明确显示当前使用的源。
- 支持搜索（名称/作者/标签，400ms 防抖）、按下载量/更新时间/点赞/名称排序、
  最低下载量与量化格式筛选；结果显示作者、下载量、更新时间与「已下载/需登录」徽标。
- 详情面板列出该仓库**全部 GGUF 量化版本与精确大小**、量化格式、sha256、许可证与是否已下载。
- 下载前确认文件大小与预计磁盘占用；下载中显示百分比、速度、剩余时间并支持取消；
  支持**断点续传**（`.part` + Range）、失败重试、**大小/哈希完整性校验**、
  Gated 模型 401/403 时提示配置 Token。
- 下载完成自动登记 `backend\models_registry.json`（模型名/路径/仓库/量化/大小/下载时间/来源），
  并按 `models\` 目录核对（手动放入的 `.gguf` 也会登记）。
- 模型库列表显示量化、来源、磁盘占用、下载时间与**最后使用时间**；
  当前使用中的模型标记「正在使用」且禁止删除；删除前二次确认。
- 与 `/model` 协作：下载目录就是 `/model` 使用的 `models\`，`/model list` 能直接识别；
  模型库反向标记当前加载的模型。
- 新增 `tools\test_model_hub_e2e.py`（驱动打包后端走真实仪表协议，19 项全通过）。

**优化五：状态栏信息分层（P2）**
- 顶部状态栏**只保留**运行状态、当前模型、当前模式（模型与模式变为可点击胶囊，
  分别跳到设置→模型 / 设置→常规）；搜索、自动化、市场三段移出顶部。
- **输入框下方**新增一行轻量状态区：🔍 搜索 · ⚙️ 自动化 · 🌐 市场 ·
  📊 上下文百分比 · 💾 KV Cache 估算，全部"图标 + 简短短语"。
- 点击任一项跳转到对应设置（搜索项直接切换开关，为此 `/search` 新增 `toggle`）；
  右侧 `▾/▸` 可折叠，状态记入 `ui_state.json` 下次启动沿用。
- 可访问性：各项带 HelpText；**不再**用 `AutomationProperties.Name` 覆盖状态文本
  （否则屏幕阅读器只能读到"运行状态"而丢掉"已就绪"）。
- 后端 `context` 响应新增 `n_ctx` / `kv_cache_text` / `model` / `search` 字段，
  前端不必解析中文状态文本。

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
- 本次修复任务书的偏差与未完成项集中记录在内部修复记录（`v0.2.0-修复记录.md`，**不随发布物分发**）第七节，摘要：
  `/exit` 未纳入危险指令确认（已有可中断倒计时）；结果目录的"任务名称"为运行批次名、
  一轮一份聚合 `result.txt`；模型列表不显示文件大小（改由详情给出精确值）；
  模型选择栏同一时刻只允许一个下载；未实现结果目录再分子目录与 Windows DPAPI 加密 Token。

---

## v0.1.0

- 首个版本：Python 后端（llama.cpp 本地推理）+ C# WPF 前端（JSON 行协议）。
- 对话模式、温度预设、联网搜索、文件上传（Office/TXT/PDF）、
  会话持久化、常用语、Markdown 渲染、深浅色主题、字号调节。
- 指令集：`/help` `/mode` `/file` `/search` `/model` `/clearcache` `/prompt` `/temp`
  `/darkmode` `/stop` `/snippet` `/status` `/clear` `/undo` `/save` `/about` `/exit`。
