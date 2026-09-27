# Developer documentation — where to read it / 开发者文档在哪里

This file is a **pointer**, not a copy. The developer documentation is kept in one place per language, so the
repository cannot end up with several copies that drift apart. This follows the same rule as
[`SECURITY.md`](SECURITY.md), which points at the root security policy.

本文件只是一个**指针**，不是副本。开发者文档按语言各保留一份正文，避免仓库里出现多份逐渐失同步的副本。
这与 [`SECURITY.md`](SECURITY.md)（指向根目录安全策略正文）采用同一条规则。

## The documentation itself / 文档正文

| Language | File | Notes |
| --- | --- | --- |
| English | [`en/DEVELOPER.md`](en/DEVELOPER.md) | Architecture, the stdin/stdout JSON protocol, module notes, configuration reference, build and release, extension points. |
| 简体中文 | [`zh-Hans/DEVELOPER.md`](zh-Hans/DEVELOPER.md) | 架构、stdin/stdout JSON 协议、模块要点、配置参考、构建与发布、扩展点。 |

The two files cover the same sections in the same order and differ only in language. If they ever disagree,
**the English version at [`en/DEVELOPER.md`](en/DEVELOPER.md) prevails**.

两个文件的章节与顺序一致，仅语言不同。如两者有歧义，**以英文版 [`en/DEVELOPER.md`](en/DEVELOPER.md) 为准**。

Why a pointer instead of a copy at this path: several documents (the root `README`, the user guide, the
automation guide, `CONTRIBUTING.md`) link to `docs/DEVELOPER.md`. A short pointer here keeps those links
working while the full text lives next to the other localised documents.

为什么在这里放指针而不是副本：根目录 `README`、用户手册、自动化指南、`CONTRIBUTING.md` 都链到
`docs/DEVELOPER.md`。放一个短指针既能让这些链接继续有效，又能让正文与其它本地化文档放在一起。

## Related reading / 相关阅读

| Document | 说明 |
| --- | --- |
| [`en/AUTOMATION_GUIDE.md`](en/AUTOMATION_GUIDE.md) | Automation configuration, sign-in state reuse, href extraction / 自动化配置、登录态复用、href 提取 |
| [`en/SKILL_GUIDE.md`](en/SKILL_GUIDE.md) | Skill (plugin) development / 技能（插件）开发 |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Development environment, tests, pull requests / 开发环境、测试、拉取请求 |
| [`../CHANGELOG.md`](../CHANGELOG.md) | What changed in each version / 各版本变更记录 |
