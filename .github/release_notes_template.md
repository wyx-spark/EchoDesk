<!--
  EchoDesk 发布说明模板（打标签发布时由 .github/workflows/release.yml 套用为 Release 正文）。

  占位符（workflow 会做纯文本替换，全部必须保留）：
    {{VERSION}}          版本号，例如 0.2.1
    {{TAG}}              标签名，例如 v0.2.1
    {{DATE}}             生成日期
    {{PACKAGE_LIST}}     本次实际构建出来的下载包清单（含体积）
    {{CHANGELOG_EXCERPT}} docs/CHANGELOG.md 中对应版本小节

  维护者：发布前请核对「已知限制 / 系统要求 / 支持的语言」三节是否仍与
  README.md、docs/CHANGELOG.md 一致；若希望人工润色正文，
  可先把 release.yml 里的 draft 改为 true，在草稿里改完再点发布。
-->

# EchoDesk {{VERSION}}

> 发布标签 `{{TAG}}` · 生成于 {{DATE}}
> 本说明由 `.github/workflows/release.yml` 套用模板生成，变更内容节选自 `docs/CHANGELOG.md`。

## 下载 / Downloads

{{PACKAGE_LIST}}

## 本版变更 / What's changed

{{CHANGELOG_EXCERPT}}

完整变更日志见随包附带的 `docs/CHANGELOG.md`。

## 已知限制 / Known limitations

- **扫描件 PDF 读不出内容**：没有文本层的 PDF（扫描件）解析结果为空，需要 OCR；本版未内置 OCR，请先用 WPS/Office 另存为可复制文本的格式。
- **浏览器自动化只支持有头模式**：运行时浏览器窗口可见（这是刻意的安全设计），因此不适合无人值守的 Windows 服务会话下运行；定时任务需要程序本身处于运行状态。
- **旧版二进制格式依赖本机 Office 套件**：`.doc` / `.ppt` 与加密文档通过 WPS/Office COM 读取，未安装 WPS 或 Microsoft Office 时只能处理 `.docx` / `.pptx` 等新格式。
- **未做代码签名**：发布包默认不签名（构建脚本的 `-Sign` 需自备证书），Windows SmartScreen 可能首次提示未知发布者。
- **模型不随包分发（不含模型的包）**：首次启动会询问是否下载模型（约 508MB），也可以自行把 `.gguf` 放到 `backend\models\`。
- **自动化对目标站点的兼容性有限**：页面改版、验证码、二次验证需要人工介入；建议先用设置窗口里的「测试选择器」向导验证再批量运行。

## 系统要求 / System requirements

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 x64（1809 及以上）或 Windows 11 x64；便携版无需安装 |
| 运行时 | 无需另装 .NET：前端为 `win-x64` 自包含发布（net10.0-windows） |
| CPU / 内存 | 4 核 CPU、8GB 内存可运行（推荐 16GB）；纯 CPU 推理，无独立显卡也可用 |
| 磁盘 | 不含模型约 500MB；含模型的离线包额外约 1GB |
| 浏览器 | 使用浏览器自动化时需本机安装 Microsoft Edge（Playwright 以 `channel=msedge` 驱动） |
| Office | 解析 `.doc`/`.ppt` 旧格式或加密文档时需本机安装 WPS 或 Microsoft Office |
| 网络 | 全本地推理，可完全离线使用；仅「下载模型 / 联网搜索 / 技能市场 / 模型中心」需要联网 |

## 升级指引 / Upgrade instructions

**便携版（推荐）**

1. 下载上面的压缩包并解压到**新目录**（不要覆盖旧目录，便于回退）；
2. 从旧目录复制这些用户数据到新目录的对应位置：
   - `backend\config.json`（配置）、`backend\session.json`（会话）、`snippets.json`（常用语）、`ui_state.json`（界面状态）
   - `backend\models\*.gguf`（已下载的模型，避免重新下载）
   - `backend\skills\` 与 `backend\skills_registry.json`（已安装技能）
   - 如需保留自动化登录态：`backend\browser_state.json`（含 Cookie，请自行判断是否跨机复制）
3. 双击新目录内的「EchoDesk 启动.lnk」启动；确认无误后再删除旧目录。

**安装版**：直接用新版安装包覆盖安装即可，配置与模型都保留在安装目录内。

**回退**：直接改回旧目录启动，或将上面的用户数据复制回旧版本（配置项向后兼容，但新版本新增的字段在旧版本中会被忽略）。

## 支持的语言 / Supported languages

- 界面与提示：**简体中文（zh-Hans）**、**English（en）**；缺失的键会按 `en → zh-Hans → 键名` 顺序回退。
- 文档：`docs/zh-Hans/`、`docs/en/`（用户手册、FAQ、技能开发指南、自动化配置填写指南）。
- 其他语言（繁体中文、日语、韩语、德语、法语等）按社区贡献逐步补充，欢迎通过 Issue 模板「翻译贡献 / Translation」认领。

---

问题反馈 / Feedback：优先使用仓库的 Issue 模板（附版本、日志与复现步骤），安全问题请按 `SECURITY.md` 私下报告。
