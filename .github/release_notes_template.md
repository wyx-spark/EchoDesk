<!--
  EchoDesk 发布说明模板（打标签发布时由 .github/workflows/release.yml 套用为 Release 正文）。

  占位符（workflow 会做纯文本替换，全部必须保留）：
    {{VERSION}}          版本号，例如 0.2.1
    {{TAG}}              标签名，例如 v0.2.1
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
- **界面截图暂缺**：v0.2.1 未随包提供截图，文档中写的是"在屏幕上该看什么"；界面预览会在后续版本放入 `assets/screenshots/`。
- **结果目录名在本版改为 `results\`**（原 `批量处理结果\`），程序不会自动迁移旧目录 —— 见下方升级指引第 3 步。

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

1. 双击 `EchoDesk-v{{VERSION}}-Setup.exe`（或 `-NoModel-Setup.exe`）。**默认装到安装程序自己
   所在的文件夹**，向导里有路径选择页，可以改到别处；
2. 装好后从开始菜单快捷方式启动（勾选了桌面图标就用桌面那个）；卸载走系统「应用和功能」；
3. 想保留旧版的配置与模型时，把下列用户数据从旧目录复制到**新安装目录**的对应位置：
   - `app\backend\config.json`（配置）、`app\backend\session.json`（会话）、`snippets.json`（常用语）、`ui_state.json`（界面状态）
   - `app\backend\models\*.gguf`（已下载的模型，避免重新下载）
   - `app\backend\skills\` 与 `app\backend\skills_registry.json`（已安装技能）
   - 如需保留自动化登录态：`app\backend\browser_state.json`（含 Cookie，请自行判断是否跨机复制）
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
