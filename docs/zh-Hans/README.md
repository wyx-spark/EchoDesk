# EchoDesk 中文文档（简体中文 / zh-Hans）

EchoDesk 是一个**本地、可审计**的 AI 桌面助手：模型推理、文档解析与批量自动化都在你自己的机器上完成，数据不出本机。
本目录是简体中文文档的入口。

English documentation: [`docs/en/README.md`](../en/README.md)。

---

## 本目录文档

| 文档 | 内容 | 适合谁 |
| --- | --- | --- |
| [`USER_GUIDE.md`](USER_GUIDE.md) | 用户手册：安装启动、日常问答、长合同审查、批量自动化、技能市场、快捷键 | 所有用户 |
| [`FAQ.md`](FAQ.md) | 常见问题与故障排除：启动、模型、文件解析、自动化、技能市场、界面 | 遇到问题时先看这里 |
| [`AUTOMATION_GUIDE.md`](AUTOMATION_GUIDE.md) | **自动化配置填写指南**：字段总览、逐项填写说明、合同审核完整示例、测试选择器流程、常见错误排查、预置模板、导入导出 | 想用浏览器批量处理网页任务的用户 |
| [`SKILL_GUIDE.md`](SKILL_GUIDE.md) | 技能（插件）开发指南：清单规范、发布到市场、安全要求与审核流程 | 技能开发者 |
| [`COMPLIANCE.md`](COMPLIANCE.md) | **企业环境适配与合规说明**：代理、证书、防火墙域名白名单、组策略/受限环境、审计与留痕 | IT 管理员、合规与安全审查人员 |

## 其他语言

| 语言 | 文档入口 |
| --- | --- |
| English (en) | [`docs/en/README.md`](../en/README.md) |

## 全语言共用文档

这些文档不按语言分目录：每份都是「英文默认版 + `.zh-Hans.md` 中文参考译本」，
如有歧义**以英文版为准**。

- [`../../CHANGELOG.md`](../../CHANGELOG.md) —— 变更日志（各版本新增、变更、修复与已知限制）。放在**仓库根目录**，便于 GitHub 直接识别；中文译本 [`../../CHANGELOG.zh-Hans.md`](../../CHANGELOG.zh-Hans.md)
- [`../DEVELOPER.md`](../DEVELOPER.md) —— 开发者文档：架构、模块划分、JSON 行协议、扩展点。这是短指针，正文在本目录的 [`DEVELOPER.md`](DEVELOPER.md) 与英文版 [`../en/DEVELOPER.md`](../en/DEVELOPER.md)
- [`../PRIVACY.zh-Hans.md`](../PRIVACY.zh-Hans.md) —— 隐私说明：联网行为、数据存放位置与清理方式（英文版 [`../PRIVACY.md`](../PRIVACY.md)）
- [`../EULA.zh-Hans.md`](../EULA.zh-Hans.md) —— 最终用户许可协议（英文版 [`../EULA.md`](../EULA.md)）
- [`../ENCRYPTED_DOCUMENTS.zh-Hans.md`](../ENCRYPTED_DOCUMENTS.zh-Hans.md) —— 读取企业加密系统保护的文档：前提条件、不绕过的声明、责任归属、哪些内容只在内存中（英文版 [`../ENCRYPTED_DOCUMENTS.md`](../ENCRYPTED_DOCUMENTS.md)）
- [`../THIRD_PARTY_LICENSES.zh-Hans.md`](../THIRD_PARTY_LICENSES.zh-Hans.md) —— 第三方组件与许可证清单（英文版 [`../THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md)）
- [`../SBOM.json`](../SBOM.json) —— 软件物料清单（SPDX 风格，由 `python tools/gen_sbom.py` 生成）
- [`../../SECURITY.zh-Hans.md`](../../SECURITY.zh-Hans.md) —— 安全策略：支持版本、私有报告渠道、响应时限、安全设计与受理范围（英文版 [`../../SECURITY.md`](../../SECURITY.md)）

`docs/SECURITY.md` 与 `docs/DEVELOPER.md` 都只是短指针，分别指向根目录的正式安全策略、
以及本目录与 `docs/en/` 下的开发者文档正文。欢迎补充这些共用文档的其他语言译本，
见下文的翻译章节。

## 项目级文档（仓库根目录）

- [`../../README.zh-Hans.md`](../../README.zh-Hans.md) —— 中文项目说明与快速上手
- [`../../README.md`](../../README.md) —— 英文项目说明（GitHub 首页展示）
- [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) —— 如何提交 Issue、贡献代码与**贡献翻译**；中文译本 [`../../CONTRIBUTING.zh-Hans.md`](../../CONTRIBUTING.zh-Hans.md)
- [`../../SECURITY.md`](../../SECURITY.zh-Hans.md) —— 如何私下报告安全漏洞
- [`../../CODE_OF_CONDUCT.md`](../../CODE_OF_CONDUCT.zh-Hans.md) —— 社区行为准则

---

## 翻译贡献

欢迎补充其他语言。文档翻译的目录约定是 `docs/<locale>/`（例如 `docs/ja/`），
语言代码与语言包文件名保持一致；界面与后端提示的语言包位置见
`frontend/Resources/Lang/<locale>.json` 与 `backend/lang/<locale>.json`。

认领或提交翻译请使用仓库的「翻译贡献 / Translation contribution」Issue 模板，
并在提交前运行：

```powershell
python tools/check_lang.py --locale <locale>
```
