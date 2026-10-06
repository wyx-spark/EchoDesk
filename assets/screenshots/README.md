# Screenshots

**v0.2.2 ships without screenshots.** This folder exists so that the convention is unambiguous and
no document has to link to an image that is not there yet: README and the user guides describe
*what to look at on screen* instead of embedding pictures.

Future releases will place interface previews here, for example:

| File | What it should show |
| --- | --- |
| `main-window.png` | the main window (both themes) |
| `long-document-summary.png` | a long document being summarised in chunks |
| `automation-tab.png` | the Settings → Automation tab, including the safety protocol area |
| `selector-test.png` | the "Test selectors" wizard succeeding |
| `automation-run.png` | a headed automation run in progress |
| `skill-marketplace.png` | the skill marketplace |

## Contributing a screenshot

1. Run the portable build, set the language and theme you want to document, and resize the window
   to a sensible size (about 1280×800).
2. **Redact everything personal first**: file paths, user names, document content, URLs, mail
   addresses, tokens. Screenshots are published — treat them like any other release asset
   (see [`../../SECURITY.md`](../../SECURITY.md) for what must never be committed).
3. Use PNG, keep each file under ~500 KB, and add the file to the table above in a PR.

Because personal data leaks easily through screenshots, they are reviewed like code.

---

# 截图

**v0.2.2 不随包提供截图。** 本目录存在的意义是把约定写明确，让任何文档都不必链接一张还不存在的图片：
README 与用户手册改为说明*屏幕上该看什么*，而不是嵌入图片。

后续版本会在这里放入界面预览，例如：

| 文件 | 应展示的内容 |
| --- | --- |
| `main-window.png` | 主窗口（两种主题） |
| `long-document-summary.png` | 长文档正在分块生成摘要 |
| `automation-tab.png` | 设置窗口 → 自动化标签页，含安全协议区 |
| `selector-test.png` | 「测试选择器」向导执行成功 |
| `automation-run.png` | 有头自动化正在运行 |
| `skill-marketplace.png` | 技能市场 |

## 贡献截图

1. 运行便携版，把语言与主题设成你要记录的状态，并把窗口调到合适的尺寸（约 1280×800）。
2. **先把所有个人信息处理干净**：文件路径、用户名、文档内容、网址、邮箱地址、Token。截图是要对外
   发布的 —— 请像对待其他发布附件一样对待它（哪些东西绝不能提交，见 [`../../SECURITY.md`](../../SECURITY.md)）。
3. 使用 PNG，单个文件控制在 500 KB 左右，并在 PR 里把文件加进上面的表格。

截图很容易泄露个人信息，所以它们和代码一样需要评审。
