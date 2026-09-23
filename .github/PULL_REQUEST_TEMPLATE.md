<!--
  提交 PR 前请完整填写；信息缺失的 PR 会被要求补充后再审。
  Fill this in completely — incomplete PRs will be sent back for more information.

  提示：保留小标题，删掉不适用的【】说明即可。
  Tip: keep the headings, delete the bracketed guidance that does not apply.
-->

## 这个 PR 做了什么 / What does this PR change?

<!--
  一句话概括 + 必要的背景。若改动涉及界面、打包体积或配置字段，请明确说明。
  One-paragraph summary plus context. Call out UI changes, package size impact,
  or configuration field changes explicitly.
-->

## 关联 Issue / Related issue

<!--
  写法：Closes #123 / Fixes #123 / Refs #123。
  没有对应 Issue 的改动（例如错别字、极小修复）写「无 / None」并说明原因。
  Use Closes/Fixes/Refs. Write "无 / None" with a reason when there is no issue.
-->

Closes #

## 变更类型 / Type of change

<!-- 勾选适用的项（可多选）/ Tick all that apply -->

- [ ] 缺陷修复 / Bug fix
- [ ] 新功能 / Feature
- [ ] 文档 / Documentation
- [ ] 翻译 / Translation
- [ ] 技能或插件 / Skill or plugin
- [ ] 重构或工程（构建、CI、依赖）/ Refactor or engineering (build, CI, dependencies)
- [ ] 破坏性变更 / Breaking change（请在下方说明影响与迁移方式）

## 测试证据 / Test evidence

<!--
  请粘贴你实际执行的命令与结果摘要，不要只写「测试通过」。
  Paste the commands you ran and a summary of the results — "tests pass" is not enough.

  后端自检（提交前请至少跑这一条，退出码 0 才算通过）：
    python tools/selftest.py
  其他可按改动范围选择：
    python tools/test_file_parsers.py     # 文件解析（会现场生成 docx/xlsx/pptx/pdf/txt/csv）
    python tools/test_plaintext.py        # 结果文件的纯文本化
    python tools/test_results_dir.py      # 批量处理结果目录结构
  PowerShell 脚本改动请同时验证 Windows PowerShell 5.1 下可运行：
    powershell -ExecutionPolicy Bypass -File tools\<脚本>.ps1
  文档改动请运行链接检查：
    powershell -ExecutionPolicy Bypass -File tools\check_docs_links.ps1
-->

```
$ python tools/selftest.py
（粘贴结果汇总：共 N 项，通过 N 项，失败 0 项）

$ python tools/test_plaintext.py
（N/N 通过）
```

- [ ] 我在本机跑过 `python tools/selftest.py`，结果为全部通过 / I ran `python tools/selftest.py` locally with all checks passing
- [ ] 我手动验证了改动涉及的功能路径 / I manually verified the affected feature path

## 打包体积是否变化 / Did package size change?

<!--
  体积是本项目的持续关注项（任务书全局要求）。请说明你的改动对发布物的影响：
  - 未变化：写「无变化 / No change」
  - 有变化：给出前后数字与来源（新增依赖、语言包、资源文件等）
  - 无法测量（例如纯文档改动）：写「不适用 / N/A」
  CI 的 size-check 任务会把每次构建的体积记入构建摘要，可作为参考。
  State "no change", give before/after numbers with the cause, or "N/A".
-->

- [ ] 无变化 / No change
- [ ] 有变化（请给出数字与原因）/ Changed (numbers and cause below)
- [ ] 不适用 / N/A

## 文档与变更日志 / Documentation and changelog

- [ ] 已更新 `docs/CHANGELOG.md` 的「未发布 / Unreleased」小节（用户可见的改动必须记录）
      / Updated the Unreleased section of `docs/CHANGELOG.md` (required for user-visible changes)
- [ ] 相关文档已同步更新（`docs/zh-Hans/`、`docs/en/` 对应文件；多语言文档需保持内容一致）
      / Matching docs updated in both `docs/zh-Hans/` and `docs/en/`
- [ ] 不涉及文档改动 / No documentation change needed

## 检查清单 / Checklist

- [ ] 我不修改 `backend/`、`frontend/`、`tools/` 之外无关的文件，改动范围与 PR 描述一致 / The diff matches the description and stays in scope
- [ ] 我未提交模型文件、发布产物、用户数据（会话、常用语、界面状态、自动化结果、市场缓存、崩溃日志）/ No models, build outputs, or user data are committed
- [ ] 我未提交任何 Token、密码、个人路径等敏感信息 / No tokens, passwords, or personal paths are committed
- [ ] 新增/修改的文本为 UTF-8（`.md`/`.json`/`.yml` 不带 BOM；`.ps1`/`.bat` 由 `.gitattributes` 统一为 CRLF）/ Text files are UTF-8 as required by `.gitattributes`
- [ ] 新增的 PowerShell 脚本兼容 Windows PowerShell 5.1（不使用 `?.`、`??`、三元运算符）/ New PowerShell scripts are 5.1-compatible
- [ ] 翻译类改动保持键名、占位符、代码与文件路径不变 / Translations keep key names, placeholders, code and paths unchanged
- [ ] 我已阅读 `CONTRIBUTING.md` 并同意以项目许可证授权本次贡献 / I have read `CONTRIBUTING.md` and license this contribution accordingly

## 补充说明 / Additional notes

<!--
  截图（界面改动请附前后对比）、已知限制、需要维护者决策的点、后续计划。
  Screenshots for UI changes, known limitations, decisions needed from maintainers, follow-ups.
-->
