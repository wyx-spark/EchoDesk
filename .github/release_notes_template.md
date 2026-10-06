<!--
  EchoDesk 发布说明模板（打标签发布时由 .github/workflows/release.yml 套用为 Release 正文）。

  占位符（workflow 会做纯文本替换，全部必须保留）：
    {{VERSION}}          版本号，例如 0.2.2
    {{TAG}}              标签名，例如 v0.2.2
    {{DATE}}             生成日期
    {{PACKAGE_LIST}}     本次实际构建出来的下载包清单（含体积）
    {{SHA256_LIST}}      SHA256SUMS.txt 的内容（校验哈希）
    {{CHANGELOG_EXCERPT}} CHANGELOG.md 中对应版本小节

  维护者：发布前请核对「已知限制 / 系统要求 / 支持的语言」三节是否仍与
  README.md、CHANGELOG.md 一致。release.yml 已把 Release 默认存为**草稿**
  （发布任务书「任务七」要求），请审阅正文与附件后再手动 Publish。
-->

# EchoDesk {{VERSION}}

> 发布标签 `{{TAG}}` · 生成于 {{DATE}}
> 本说明由 `.github/workflows/release.yml` 套用模板生成，变更内容节选自 `CHANGELOG.md`。

## 下载 / Downloads

{{PACKAGE_LIST}}

## 本版变更 / What's changed

{{CHANGELOG_EXCERPT}}

完整变更日志见随包附带的 `CHANGELOG.md`。

## 校验哈希 / Checksums

下载后请核对 SHA256；下面的清单同时也是 Release 附件 `SHA256SUMS.txt` 的内容。

```text
{{SHA256_LIST}}
```

Windows 上核对：`certutil -hashfile <文件名> SHA256`。

## 已知限制 / Known limitations

- **扫描件 PDF 读不出内容**：没有文本层的 PDF（扫描件）解析结果为空，需要 OCR；本版未内置 OCR，请先用 WPS/Office 另存为可复制文本的格式。
- **浏览器自动化只支持有头模式**：运行时浏览器窗口可见（这是刻意的安全设计），因此不适合无人值守的 Windows 服务会话下运行；定时任务需要程序本身处于运行状态。
- **旧版二进制格式依赖本机 Office 套件**：`.doc` / `.ppt` 与加密文档通过 WPS/Office COM 读取，未安装 WPS 或 Microsoft Office 时只能处理 `.docx` / `.pptx` 等新格式。
- **安装程序未做代码签名**：发布包默认不签名（构建脚本的 `-Sign` 需自备证书），Windows SmartScreen 可能首次提示未知发布者。
- **安装程序界面目前只有英文**：Inno Setup 自带的语言目录里没有简体中文语言文件（官方把它列为需单独下载的第三方翻译）；把 `ChineseSimplified.isl` 放进 Inno Setup 的 `Languages\` 目录即可自动启用中文界面。
- **模型不随包分发（不含模型的包）**：首次启动会询问是否下载模型（约 508MB），也可以自行把 `.gguf` 放到 `backend\models\`。
- **自动化对目标站点的兼容性有限**：页面改版、验证码、二次验证需要人工介入；建议先用设置窗口里的「测试选择器」向导验证再批量运行。
- **界面截图暂缺**：v0.2.2 未随包提供截图，文档中写的是"在屏幕上该看什么"；界面预览会在后续版本放入 `assets/screenshots/`。
- **结果目录名在本版改为 `results\`**（原 `批量处理结果\`），程序不会自动迁移旧目录 —— 见下方升级指引第 4 步。
- **自动化登录态改为独立数据目录**：登录状态存在 `app\backend\browser_profile\`（可在「设置 → 自动化 → 登录态」里改），不再只依赖 `browser_state.json`；升级后首次运行仍会自动导入旧 Cookie，通常不用重新登录。

## 系统要求 / System requirements

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 x64（1809 及以上）或 Windows 11 x64；便携版无需安装 |
| 运行时 | 无需另装 .NET：前端为 `win-x64` 自包含发布（net10.0-windows） |
| CPU / 内存 | 4 核 CPU、8GB 内存可运行（推荐 16GB）；纯 CPU 推理，无独立显卡也可用 |
| 磁盘 | 安装/解压后：不含模型约 500MB、含模型约 890MB；另需留出模型下载空间 |
| 浏览器 | 使用浏览器自动化时需本机安装 Microsoft Edge（Playwright 以 `channel=msedge` 驱动） |
| Office | 解析 `.doc`/`.ppt` 旧格式或加密文档时需本机安装 WPS 或 Microsoft Office |
| 网络 | 全本地推理，可完全离线使用；仅「下载模型 / 联网搜索 / 技能市场 / 模型中心」需要联网 |

## 升级指引 / Upgrade instructions

**安装程序（推荐）**

1. 双击 `EchoDesk-v{{VERSION}}-Setup.exe`。**默认装到安装程序自己
   所在的文件夹**，向导里有路径选择页，可以改到别处（安装程序不含模型，首次启动会询问是否下载）；
2. 装好后从开始菜单快捷方式启动（勾选了桌面图标就用桌面那个）；卸载走系统「应用和功能」；
3. 想保留旧版的配置与模型时，把下列用户数据从旧目录复制到**新安装目录**的对应位置：
   - `app\backend\config.json`（配置）、`app\backend\session.json`（会话）、`snippets.json`（常用语）、`ui_state.json`（界面状态）
   - `app\backend\models\*.gguf`（已下载的模型，避免重新下载）
   - `app\backend\skills\` 与 `app\backend\skills_registry.json`（已安装技能）
   - 如需保留自动化登录态：`app\backend\browser_profile\`（v0.2.1 起登录态存在这里）
     与 `app\backend\browser_state.json`（旧版与备份文件，含 Cookie，请自行判断是否跨机复制）
4. **结果目录改名**：v0.2.1 起自动化结果目录由 `批量处理结果\` 改为 `results\`，程序不会自动迁移。
   请把旧目录**改名**为 `results\`（或把旧目录内容移入新的 `results\`），让历史结果仍在同一个地方。
   不迁移也不会丢数据，只是新旧结果分处两个目录。

**便携版 / zip 免安装解压版**

1. 把 zip 解压到**新目录**（不要覆盖旧目录，便于回退）；
2. 按上面第 3、4 步复制用户数据并改名结果目录；
3. 运行一次新目录里的 `install-complete.bat` 创建快捷方式（zip 里没有预置快捷方式 —— 因为
   .lnk 记的是绝对路径，预置的快捷方式在别人机器上必然是死链），或直接双击 `app\EchoDesk.exe`
   （程序首次运行也会询问是否创建快捷方式）；
4. 双击 `Start-EchoDesk-v{{VERSION}}.lnk` 启动；确认无误后再删除旧目录。

**回退**：直接改回旧目录启动，或将上面的用户数据复制回旧版本（配置项向后兼容，但新版本新增的字段在旧版本中会被忽略）。

## 支持的语言 / Supported languages

- 界面与提示：**简体中文（zh-Hans）**、**English（en）**；缺失的键会按 `en → zh-Hans → 键名` 顺序回退。
- 文档：`docs/zh-Hans/`、`docs/en/`（用户手册、FAQ、技能开发指南、自动化配置填写指南）。
- 合规文档（LICENSE、NOTICE、SECURITY、PRIVACY、EULA、THIRD_PARTY_LICENSES、CODE_OF_CONDUCT）：
  **英文为默认版 + `.zh-Hans.md` 中文参考译本**，如有歧义以英文版为准。
- 其他语言（繁体中文、日语、韩语、德语、法语等）按社区贡献逐步补充，欢迎通过 Issue 模板「翻译贡献 / Translation」认领。

---

## 反馈渠道 / Feedback

- **问题与功能请求**：请开 [GitHub Issue](https://github.com/VoidEntropiaStudio/EchoDesk/issues)，
  并附上版本号、Windows 版本、复现步骤与相关日志（`app\backend\backend_crash.log`）。
- **提问、交流技能与协作翻译**：[GitHub Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions)。
- **安全漏洞**：**请勿开公开 Issue**，请按仓库根目录 [`SECURITY.md`](https://github.com/VoidEntropiaStudio/EchoDesk/blob/master/SECURITY.md)
  的私有渠道报告（Security 标签页 → Report a vulnerability；兜底为 GitHub noreply 邮箱）。

本项目**不提供**支持邮箱、电话号码或邮寄地址 —— 以上 GitHub 渠道是全部公开联系方式。

---

<!--
  EchoDesk release notes template (applied by .github/workflows/release.yml as the Release body when a release is tagged).

  Placeholders (the workflow does plain-text substitution; all of them must stay):
    {{VERSION}}           version number, for example 0.2.2
    {{TAG}}               tag name, for example v0.2.2
    {{DATE}}              generation date
    {{PACKAGE_LIST}}      the download packages actually built in this run (with sizes)
    {{SHA256_LIST}}      the contents of SHA256SUMS.txt (checksum hashes)
    {{CHANGELOG_EXCERPT}} the matching version section in CHANGELOG.md

  Maintainers: before releasing, check that the "Known limitations / System requirements /
  Supported languages" sections still match README.md and CHANGELOG.md. release.yml saves the
  Release as a **draft** by default (required by "task seven" of the release brief); review the
  body and the attachments, then publish it by hand.
-->

# EchoDesk {{VERSION}}

> Release tag `{{TAG}}` · generated on {{DATE}}
> These notes are produced by `.github/workflows/release.yml` from this template; the changes section is excerpted from `CHANGELOG.md`.

## Downloads

{{PACKAGE_LIST}}

## What's changed

{{CHANGELOG_EXCERPT}}

The full changelog is in the `CHANGELOG.md` that ships with the package.

## Checksums

Check the SHA256 after downloading; the list below is also the contents of the `SHA256SUMS.txt` release asset.

```text
{{SHA256_LIST}}
```

On Windows, check with `certutil -hashfile <file name> SHA256`.

## Known limitations

- **Scanned PDFs yield no text**: a PDF with no text layer (a scan) parses to an empty result and needs OCR; this version has no built-in OCR, so save it from WPS/Office in a format whose text can be copied first.
- **Browser automation is headed only**: the browser window stays visible while it runs (a deliberate safety design), so it is not suited to unattended Windows service sessions; scheduled tasks need the program itself to be running.
- **Legacy binary formats need a local Office suite**: `.doc` / `.ppt` files and encrypted documents are read through WPS/Office COM, so without WPS or Microsoft Office installed only newer formats such as `.docx` / `.pptx` can be handled.
- **The installer is not code-signed**: release packages are unsigned by default (the build script's `-Sign` needs a certificate of your own), so Windows SmartScreen may warn about an unknown publisher the first time.
- **The installer interface is English only for now**: Inno Setup's own language directory has no Simplified Chinese language file (upstream lists it as a third-party translation to download separately); put `ChineseSimplified.isl` into Inno Setup's `Languages\` directory and the Chinese interface is enabled automatically.
- **Models are not distributed with the packages that omit them**: the first start asks whether to download the model (about 508MB), and you can also put a `.gguf` file into `backend\models\` yourself.
- **Automation copes only so far with a target site**: page redesigns, captchas and two-factor verification need a human; verify with the "Test selectors" wizard in the Settings window before running in bulk.
- **No interface screenshots yet**: v0.2.2 ships without screenshots, so the documents describe "what to look at on screen"; interface previews will go into `assets/screenshots/` in a later version.
- **The results folder is renamed in this version to `results\`** (it was `批量处理结果\`), and the program does not migrate the old folder automatically — see step 4 of the upgrade instructions below.
- **Automation sign-in state moved to a data directory of its own**: the sign-in state is stored in `app\backend\browser_profile\` (change it under "Settings → Automation → Sign-in state") instead of relying on `browser_state.json` alone; after upgrading, the first run still imports the old cookies automatically, so you normally do not sign in again.

## System requirements

| Item | Requirement |
| --- | --- |
| Operating system | Windows 10 x64 (1809 or later) or Windows 11 x64; the portable build needs no installation |
| Runtime | No separate .NET install: the front end is published self-contained for `win-x64` (net10.0-windows) |
| CPU / memory | A 4-core CPU and 8GB of memory will run it (16GB recommended); inference is CPU-only, so no discrete graphics card is needed |
| Disk | After installing or unzipping: about 500MB without models, about 890MB with models; leave room for the model download as well |
| Browser | Browser automation needs Microsoft Edge installed on the machine (Playwright drives it with `channel=msedge`) |
| Office | Parsing the legacy `.doc`/`.ppt` formats or encrypted documents needs WPS or Microsoft Office installed on the machine |
| Network | All inference is local and works fully offline; only "download models / web search / skill marketplace / model hub" need a network connection |

## Upgrade instructions

**Installer (recommended)**

1. Double-click `EchoDesk-v{{VERSION}}-Setup.exe`. **It installs into the
   folder the installer itself sits in by default**; the wizard has a path page, so you can move it
   elsewhere (the installer bundles no model; the first launch asks whether to download one);
2. After installing, start it from the Start menu shortcut (use the desktop one if you ticked that
   box); uninstall through *Apps & features*;
3. To keep the configuration and models from the old version, copy the following user data from the
   old directory into the matching place in the **new installation directory**:
   - `app\backend\config.json` (configuration), `app\backend\session.json` (sessions), `snippets.json` (snippets), `ui_state.json` (interface state)
   - `app\backend\models\*.gguf` (models already downloaded, so you do not download them again)
   - `app\backend\skills\` and `app\backend\skills_registry.json` (installed skills)
   - To keep the automation sign-in state: `app\backend\browser_profile\` (where the sign-in state lives
     from v0.2.1 on) and `app\backend\browser_state.json` (the older file and the backup; both contain
     cookies, so decide for yourself whether copying them to another machine is appropriate)
4. **The results folder is renamed**: from v0.2.1 the automation results folder changed from `批量处理结果\`
   to `results\`, and the program does not migrate it automatically. Rename the old folder to `results\`
   (or move its contents into the new `results\`), so that past results stay in the same place.
   Not migrating loses no data; old and new results simply sit in two different folders.

**Portable / no-install zip**

1. Unzip into a **new directory** (do not overwrite the old one, so you can roll back);
2. Follow steps 3 and 4 above to copy your user data and rename the results folder;
3. Run `install-complete.bat` in the new directory once to create shortcuts (the zip contains no
   ready-made shortcuts — a `.lnk` records an absolute path, so a shipped shortcut would always be a
   dead link on someone else's machine), or double-click `app\EchoDesk.exe` directly (the program
   also asks whether to create shortcuts on its first run);
4. Double-click `Start-EchoDesk-v{{VERSION}}.lnk` to start it; delete the old directory once you are
   satisfied.

**Rolling back**: start from the old directory again, or copy the user data above back to the old version (configuration keys are backward compatible, but fields added by the new version are ignored by the old one).

## Supported languages

- Interface and messages: **Simplified Chinese (zh-Hans)** and **English (en)**; a missing key falls back in the order `en → zh-Hans → key name`.
- Documentation: `docs/zh-Hans/`, `docs/en/` (user guide, FAQ, skill development guide, automation configuration guide).
- Compliance documents (LICENSE, NOTICE, SECURITY, PRIVACY, EULA, THIRD_PARTY_LICENSES, CODE_OF_CONDUCT):
  **English is the default version plus a `.zh-Hans.md` Chinese reference translation**; where they differ, the English version decides.
- Other languages (Traditional Chinese, Japanese, Korean, German, French and so on) are added as the community contributes; claim one through the "Translation" issue template.

---

## Feedback

- **Bugs and feature requests**: open a [GitHub issue](https://github.com/VoidEntropiaStudio/EchoDesk/issues)
  and include the version number, your Windows version, the steps to reproduce and the relevant log
  (`app\backend\backend_crash.log`).
- **Questions, skill talk and translation help**: [GitHub Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions).
- **Security vulnerabilities**: **do not open a public issue**; report through the private channel
  described in [`SECURITY.md`](https://github.com/VoidEntropiaStudio/EchoDesk/blob/master/SECURITY.md)
  at the repository root (Security tab → Report a vulnerability; the fallback is a GitHub noreply
  email address).

This project **does not provide** a support email address, phone number or postal address — the GitHub channels above are the complete set of public contact points.
