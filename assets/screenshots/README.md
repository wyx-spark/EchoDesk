# Screenshots

These are **real captures of the current release**, not mockups. They are produced by the
Avalonia front end's own screenshot mode (`EchoDesk --probe shots`), which fixes the theme and the
language, drives the product's regular rendering path and writes PNGs through
`RenderTargetBitmap` at 2× pixels. No backend and no network are involved, so the images do not
depend on which model the build machine happens to have installed.

| File | What it shows | Size |
| --- | --- | --- |
| `screenshot-main.png` | the main window streaming a structured contract review (dark theme, English) | 2560×1640 |
| `screenshot-main.zh-Hans.png` | the same window in Simplified Chinese | 2560×1640 |
| `screenshot-model-market.png` | Settings → Model: GGUF marketplace results with the facet summary, pager and status line | 2560×1840 |
| `screenshot-model-market.zh-Hans.png` | the same page in Simplified Chinese | 2560×1840 |
| `screenshot-automation.png` | Settings → Automation: safety protocol accepted, task and selector fields filled in | 2560×1840 |
| `screenshot-automation.zh-Hans.png` | the same page in Simplified Chinese | 2560×1840 |

The copies the READMEs embed live in `docs/images/`; both directories hold the same six files,
regenerated together.

## Regenerating them

1. Build the front end: `dotnet build frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj -c Debug`.
2. Run the probe in screenshot mode, pointing it at an output directory:
   `EchoDesk.exe --probe shots --out <dir>`. The exits code is 0 only when every image was written,
   and the run fails on purpose if the title bar would carry no version or if the marketplace demo
   rows were blanked before the shutter fired.
3. Copy the six PNGs into `docs/images/` and into this directory.

The captures are deterministic on Windows, macOS and Linux because the same Avalonia render path
and the same demo data are used on all three; only font rasterisation differs slightly.

## Contributing a screenshot

1. Do **not** hand-crop a real session. Switch the app to the language and theme you want to
   document, then extend the screenshot mode so the image is reproducible.
2. **Redact everything personal first**: file paths, user names, document content, URLs, mail
   addresses, tokens. Screenshots are published — treat them like any other release asset
   (see [`../../SECURITY.md`](../../SECURITY.md) for what must never be committed).
3. Use PNG, keep each file under ~500 KB, and add the file to the table above in a PR.

Because personal data leaks easily through screenshots, they are reviewed like code.

---

# 截图

这些是**当前发布版的真实截图**，不是示意图。它们由 Avalonia 前端自带的截图模式
（`EchoDesk --probe shots`）产出：固定主题与语言、走产品自己的渲染路径、再用
`RenderTargetBitmap` 以 2 倍像素写成 PNG。全程不拉后端、不联网，因此图不依赖构建机上恰好装了
哪个模型。

| 文件 | 展示的内容 | 尺寸 |
| --- | --- | --- |
| `screenshot-main.png` | 主窗口正在流式输出结构化合同审阅（深色主题、英文） | 2560×1640 |
| `screenshot-main.zh-Hans.png` | 同一窗口的简体中文界面 | 2560×1640 |
| `screenshot-model-market.png` | 设置 → 模型：GGUF 模型市场结果，含分面摘要、分页条与状态行 | 2560×1840 |
| `screenshot-model-market.zh-Hans.png` | 同一页的简体中文界面 | 2560×1840 |
| `screenshot-automation.png` | 设置 → 自动化：已接受安全协议，任务与选择器字段已填写 | 2560×1840 |
| `screenshot-automation.zh-Hans.png` | 同一页的简体中文界面 | 2560×1840 |

README 里内嵌的那几份放在 `docs/images/`；两个目录持有同样的六张图，同批重新生成。

## 如何重新生成

1. 构建前端：`dotnet build frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj -c Debug`。
2. 以截图模式运行探针并指定输出目录：`EchoDesk.exe --probe shots --out <目录>`。只有每张图都
   写成功了退出码才是 0；若标题栏会没有版本号、或市场演示行在快门之前被清空，这次运行会**刻意
   失败**。
3. 把六张 PNG 拷进 `docs/images/` 与本目录。

在 Windows、macOS、Linux 上这些图是一致的：三者走同一条 Avalonia 渲染路径、用同一批演示数据，
差别仅在字体的光栅化细节。

## 贡献截图

1. **不要**手工裁剪一次真实会话。把程序切到你要记录的语言与主题，然后扩展截图模式，使这张图
   可复现。
2. **先把所有个人信息处理干净**：文件路径、用户名、文档内容、网址、邮箱地址、Token。截图是要
   对外发布的 —— 请像对待其他发布附件一样对待它（哪些东西绝不能提交，见
   [`../../SECURITY.md`](../../SECURITY.md)）。
3. 使用 PNG，单个文件控制在 500 KB 左右，并在 PR 里把文件加进上面的表格。

截图很容易泄露个人信息，所以它们和代码一样需要评审。
