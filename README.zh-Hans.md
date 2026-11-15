# EchoDesk Reader

> **版本 0.2.6-reader。** 本分支发布 Reader 版：在 v0.2.6 代码基上移除浏览器自动化、多模态与检索栈，
> 专注于阅读与分析本机的长篇文档。下面每一句关于功能的描述都适用于**这一个**构建；被移除的部分在
> [本版不包含什么](#本版不包含什么) 里逐项列出。

![EchoDesk Reader 主界面 —— 本地模型正在回答关于一份 42 页招标文件的提问](docs/images/screenshot-main.zh-Hans.png)

**本地优先的文档阅读与分析助手 —— 不联网、不要账号、没有遥测。**

EchoDesk Reader 把整条链路留在你的机器上：模型经 llama.cpp 在本机推理，文档在本机解析，对话记录
不出磁盘。它面向的是**不能把合同与报告发给云端 AI** 的人 —— 受合规约束的团队、隔离网络，以及单纯
希望文件留在本地的用户。

[English →](README.md)（本文为英文版的中文参考译本，如两者有歧义，**以英文版为准**。）

| 解析完成的文档 | 正在流式输出的合同改写 |
| :---: | :---: |
| ![文档解析：先给结构表，再给抽取出的付款条款](docs/images/screenshot-file-parse.zh-Hans.png) | ![把合同条款改写成承诺函，流式输出并带实时光标](docs/images/screenshot-chat.zh-Hans.png) |

- **平台**：Windows 10/11（x64）优先；同一套 Avalonia 前端可在 macOS 与 Linux 上构建与运行。
  WPF 前端仍留在仓库里服务 Windows 长期支持线，但**不参与** Reader 的构建。
- **推理**：本地 GGUF 模型，经 llama.cpp（`llama-cpp-python`），默认走 CPU
- **模型画像**：按 Phi-3-Mini-128K 调优 —— 它的 128K 窗口是文档分配的硬上限；任意 GGUF 模型仍可加载
- **KV 缓存**：量化为 8 位（`Q8_0`）而非 F16，同一窗口下缓存占用约减半
- **界面**：Avalonia（C#）桌面程序 + Python 3.13 打包后端
- **语言**：简体中文与英语
- **许可证**：[Apache-2.0](LICENSE)

## 动态上下文：本版存在的理由

Reader 的上下文窗口**按文档分配，而不是按固定设置**：文件解析完成后，窗口按

```
文档 tokens + 4096（回答预算）+ 512（安全余量）
```

规划，并收敛到 `[初始窗口, 131072]`；只有窗口需要变大时才重建模型。同一进程内窗口只增不减，因此
再加一份更小的文档不会为更小的数值重载一次模型。若文档的 token 数在 128K 上限下仍装不下，
程序**直接拒绝并说明原因，绝不做静默截断** —— 你始终知道某个文件为什么没被接受。

## 本版不包含什么

下列能力属于上游 v0.2.6，**在本构建中被整体移除**（连同它们的设置页、测试与打包步骤）：

| 已移除 | 原先的用途 |
| --- | --- |
| 浏览器自动化 | Playwright 驱动的任务清单批量处理（选择器向导、安全协议、结果文件） |
| 多模态 | 本地视觉（VLM）、录音与转写、媒体路径管控 |
| 检索栈 | SQLite FTS5 知识库、长期记忆、BM25 检索与抽取式摘要器 |

本分支的 `tools/verify_release.ps1` 会断言这些模块没有任何残留被导入或被打包，而不是去检查它们。

## 快速上手

### 便携版（无需安装）

1. 把 `EchoDesk-Reader-v0.2.6-reader` 整个文件夹解压到任意位置 —— 不往文件夹外写任何东西，
   删掉文件夹即完成卸载。
2. 双击 **`app\EchoDesk.exe`**。它会自己启动旁边的后端；**不要**单独运行
   `app\backend\EchoDeskBackend.exe`，它只会说 JSON 行协议。
3. 首次启动会询问是否下载模型；也可以自己把 `.gguf` 放进 `app\backend\models\`，再到
   **设置 → 模型** 里选中它。

目录结构：

| 路径 | 是什么 |
| --- | --- |
| `README.md`、`LICENSE`、`NOTICE` | 本文与许可证 |
| `app\EchoDesk.exe` | Avalonia 前端；界面语言包已内嵌其中 |
| `app\backend\EchoDeskBackend.exe` | 打包后的 Python 后端（PyInstaller onedir） |
| `app\backend\lang\` | 后端语言包（消息、错误、指令帮助） |
| `app\backend\config.json` | 全部配置 |
| `app\backend\models\` | 本地 `.gguf` 模型 |
| `app\backend\session.json` | 对话记录（自动保存与恢复） |

可选的 `-MakeInstaller` 步骤会产出 `EchoDesk-Reader-Setup.exe`，装出来的目录结构与此相同。

设置环境变量 `ECHODESK_DATA` 可以把外层数据目录挪到别处（适用于程序目录只读的被管机器）。

### 从源码运行

```
python -m venv .venv
.venv\Scripts\python -m pip install -r backend\requirements.lock
dotnet run --project frontend-avalonia\EchoDesk.Avalonia\EchoDesk.Avalonia.csproj
```

后端由前端自行拉起。若要单独试协议，请**把 `backend\` 作为工作目录**运行 `python backend\main.py`
（后端按工作目录解析 `models/` 与技能目录），然后逐行输入 JSON 请求。macOS/Linux 上把
`python`/`.venv\Scripts` 换成 `python3`/`.venv/bin`。`tools/build_reader.ps1` 产出 Windows
便携目录，加上 `-MakeInstaller` 还会产出 Inno Setup 安装包。

## 能读哪些文档

`.docx`、`.xlsx`、`.xlsm`、`.xls`、`.pptx`、`.pdf`、`.txt`、`.md`、`.csv` 以及类似的纯文本格式。
Windows 上，旧式 `.doc` / `.ppt` 与加密文档借本机已授权的 WPS/Office COM 读取；没有 WPS/Office 时，
内置解析器仍能处理现代格式，旧式格式会明确报告不可用，而不是静默失败。

长文档既不会被截断、也不会整段硬塞：它们被切分、逐段摘要（map-reduce）后合并，并保留
**原文指针**（文件名 + 字符区间），让答案可以回溯到原文。

## 隐私与联网

- 没有遥测、没有账号、不监听任何端口。除非你自己开启联网功能，否则文档的任何内容都不会离开本机。
- **默认关闭**：联网搜索、模型下载、更新检查。开启搜索后，只发送当前问题的关键词。
- 外部模型服务默认不启用，且必须显式勾选同意书：数据将要离开本机的那一刻，程序会说明并先征求同意。

## 文档

- [`docs/zh-Hans/USER_GUIDE.md`](docs/zh-Hans/USER_GUIDE.md) —— 使用手册，含 Reader 版的范围说明。
- [`docs/zh-Hans/FAQ.md`](docs/zh-Hans/FAQ.md) —— 快速上手与排障。
- [`docs/zh-Hans/CROSS_PLATFORM.md`](docs/zh-Hans/CROSS_PLATFORM.md) —— 各平台的差异。
- [`CHANGELOG.zh-Hans.md`](CHANGELOG.zh-Hans.md) —— 版本历史。
- [`SECURITY.zh-Hans.md`](SECURITY.zh-Hans.md) —— 如何私下报告安全问题。
- [`assets/screenshots/`](assets/screenshots/) —— 上面的截图，以及重新生成的步骤。

## 出品方

[Void-Entropia-Studio](https://github.com/Void-Entropia-Studio)。EchoDesk 是独立项目，与任何使用相同
或相似名称的产品都没有关联。
