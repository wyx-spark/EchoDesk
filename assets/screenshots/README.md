# Screenshots

These are **real captures of the current release**, not mockups. They are produced by the Avalonia
front end's own screenshot mode (`EchoDesk --probe shots`), which fixes the dark theme and the
language, drives the product's regular rendering path and writes PNGs through `RenderTargetBitmap`
at 2× pixels. No backend and no network are involved, so the images do not depend on which model the
build machine happens to have installed.

| File | What it shows | Size |
| --- | --- | --- |
| `screenshot-main.png` | the main window answering a question about a 42-page tender document (English) | 2560×1640 |
| `screenshot-main.zh-Hans.png` | the same scene in Simplified Chinese | 2560×1640 |
| `screenshot-file-parse.png` | a parsed contract: document structure table plus the extracted payment terms | 2560×1640 |
| `screenshot-file-parse.zh-Hans.png` | the same scene in Simplified Chinese | 2560×1640 |
| `screenshot-chat.png` | a contract clause being rewritten as a commitment letter, streaming with a live cursor | 2560×1640 |
| `screenshot-chat.zh-Hans.png` | the same scene in Simplified Chinese | 2560×1640 |

The three scenes are exactly the three things the Reader edition does — read the window, parse a
document, stream an answer — so there is deliberately no browser-automation or marketplace capture
in this build.

The copies the READMEs embed live in `docs/images/`; both directories hold the same six files,
regenerated together.

## Regenerating them

1. Build the front end: `dotnet build frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj -c Debug`.
2. Run the probe in screenshot mode, pointing it at an output directory:
   `EchoDesk.exe --probe shots --out <dir>`. The exit code is 0 only when every image was written,
   and the run fails on purpose if a title bar would carry no version.
3. Copy the six PNGs into `docs/images/` and into this directory.

The captures are deterministic on Windows, macOS and Linux because the same Avalonia render path and
the same demo data are used on all three; only font rasterisation differs slightly.

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
（`EchoDesk --probe shots`）产出：固定深色主题与语言、走产品自己的渲染路径、再用
`RenderTargetBitmap` 以 2 倍像素写成 PNG。全程不拉后端、不联网，因此图不依赖构建机上恰好装了
哪个模型。

| 文件 | 展示的内容 | 尺寸 |
| --- | --- | --- |
| `screenshot-main.png` | 主界面正在回答关于一份 42 页招标文件的提问（英文） | 2560×1640 |
| `screenshot-main.zh-Hans.png` | 同一场景的简体中文界面 | 2560×1640 |
| `screenshot-file-parse.png` | 已解析的合同：文档结构表 + 抽取出的付款条款 | 2560×1640 |
| `screenshot-file-parse.zh-Hans.png` | 同一场景的简体中文界面 | 2560×1640 |
| `screenshot-chat.png` | 把合同条款改写成承诺函，流式输出并带实时光标 | 2560×1640 |
| `screenshot-chat.zh-Hans.png` | 同一场景的简体中文界面 | 2560×1640 |

三幕正是 Reader 版做的三件事 —— 看窗口、解析文档、流式回答 —— 所以本构建刻意不提供浏览器自动化
或市场的截图。

README 里内嵌的那几份放在 `docs/images/`；两个目录持有同样的六张图，同批重新生成。

## 如何重新生成

1. 构建前端：`dotnet build frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj -c Debug`。
2. 以截图模式运行探针并指定输出目录：`EchoDesk.exe --probe shots --out <目录>`。只有每张图都
   写成功了退出码才是 0；若某张图的标题栏会没有版本号，这次运行会**刻意失败**。
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
