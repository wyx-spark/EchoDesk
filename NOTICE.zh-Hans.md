# 第三方组件声明（简体中文译本）

> **本文为英文版 [`NOTICE`](NOTICE) 的中文译本。** 如与英文版有歧义，**以英文版为准**。
> 英文原版随本文件同目录提供；第三方组件清单的可机读形式软件物料清单为 `docs/SBOM.json`，按随包分发的分发版生成

> This is the Chinese translation of the English [`NOTICE`](NOTICE). If the two ever disagree,
> the English version prevails. A machine-readable component list is in [`docs/SBOM.json`](docs/SBOM.json).

EchoDesk
版权所有 (C) 2026 熵寂工作室（Void-Entropia-Studio）

> 工作室中文名与英文名的对应关系：**熵寂工作室 = Void-Entropia-Studio**；
> 本声明中的**法律主体以英文版 [`NOTICE`](NOTICE) 为准**（英文版使用 `Void Entropia Studio`）。
> 组织主页：<https://github.com/Void-Entropia-Studio>。

本产品包含由第三方开发的软件。下列组件是 EchoDesk 发行版中随附的主要组件；完整、
由工具自动生成的清单（每一个 Python 发行包及其许可证，包括传递依赖）见
`docs/THIRD_PARTY_LICENSES.md`，该文件由
`python tools/gen_licenses.py` 生成。

--------------------------------------------------------------------------------
EchoDesk 自身
--------------------------------------------------------------------------------

EchoDesk 本身以 Apache License, Version 2.0 授权（见 `LICENSE`）。

--------------------------------------------------------------------------------
本地推理
--------------------------------------------------------------------------------

* llama-cpp-python - MIT License
  用于运行本地 GGUF 模型的 Python 绑定。
* llama.cpp（作为 llama.dll 与 ggml 系列库原生内置在 llama-cpp-python 中）-
  MIT License

--------------------------------------------------------------------------------
后端运行时（Python 3.13）
--------------------------------------------------------------------------------

* pydantic - MIT License
* huggingface_hub - Apache License 2.0
* duckduckgo-search - MIT License
* pywin32 - Python Software Foundation License (PSF)

--------------------------------------------------------------------------------
文档解析
--------------------------------------------------------------------------------

* python-docx - MIT License (.docx)
* openpyxl - MIT License (.xlsx, .xlsm)
* python-pptx - MIT License (.pptx)
* pypdf - BSD 3-Clause License (.pdf)
* xlrd - BSD License (.xls)

说明：在 v0.2.0 及之前，PDF 文本提取使用 PyMuPDF（AGPL-3.0 或 Artifex 商业许可证）。
在 v0.2.1 中已替换为 pypdf（BSD-3-Clause），因此分发的产品不再包含 AGPL 许可的组件。

--------------------------------------------------------------------------------
浏览器自动化
--------------------------------------------------------------------------------

* Playwright for Python - Apache License 2.0
* Playwright driver（随附的 Node.js driver，包含 playwright-core）-
  Apache License 2.0

Microsoft Edge **不**随 EchoDesk 重新分发：自动化功能操作的是用户机器上已安装的那份
Microsoft Edge。

--------------------------------------------------------------------------------
前端（C# WPF、.NET）
--------------------------------------------------------------------------------

* .NET 运行时与 WPF 程序集，以自包含方式发布 - MIT License
  （Microsoft .NET）

前端不引用任何第三方 NuGet 包；与后端之间的 JSON 通信使用内置的 System.Text.Json。

--------------------------------------------------------------------------------
构建工具（不属于随附的运行时）
--------------------------------------------------------------------------------

* PyInstaller - GNU General Public License v2.0（附官方特别例外）
  PyInstaller 是用于打包 Python 后端的构建工具。其例外条款明确允许打包并分发本身
  并非 GPL 许可的程序；PyInstaller 不会被最终产品链接进来，也不会作为库使用。

--------------------------------------------------------------------------------
本地模型
--------------------------------------------------------------------------------

EchoDesk 不包含大语言模型权重。模型由用户下载或提供，其许可仍由模型的发布方决定
（默认模型仓库见 `backend/config.json`）。

--------------------------------------------------------------------------------
合规说明
--------------------------------------------------------------------------------

* 随附组件及其许可证的权威清单是 `docs/THIRD_PARTY_LICENSES.md`；每次发布前都应重新
  生成并重新审查该文件（`python tools/gen_licenses.py`）。
* 第三方组件仍受其各自许可证约束。EchoDesk 的 Apache License 2.0 不会改变你在这些
  许可证下享有的权利或承担的义务。
