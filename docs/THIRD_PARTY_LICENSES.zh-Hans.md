# 第三方组件与许可证清单（简体中文译本）

> **本文为 [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) 的中文译本。** 如与英文版有歧义，**以英文版为准**。
> 两份清单逐条一致；v0.2.7 起同时重新生成：移除 `llama-cpp-python` 与 numpy，
> 新增随仓库分发的 vendored `llama.cpp` 0.4.7（MIT，三平台）。

本文件列出 EchoDesk 分发时随附的第三方组件及其许可证，用于开源合规审查。
扫描到的 Python 发行包共 43 个（含传递依赖）。

> **v0.2.1 合规变更（任务书 1.3）**：对外开源发布前，PDF 解析已从 **PyMuPDF**
> （AGPL-3.0 或 Artifex 商业许可）换成 **pypdf**（BSD-3-Clause，纯 Python）。
> 项目本体采用 **Apache-2.0**，与 AGPL 不兼容；替换后发布物中不再含 AGPL 组件，
> 剩余的唯一 GPL 组件是构建工具 PyInstaller（GPLv2 带官方例外，允许打包分发专有程序）。
> 同步收益：该依赖的安装体积从 52.9MB 降到约 1.4MB。

<!-- sbom:direct-deps -->
## 一、后端直接依赖

| 组件 | 版本 | 许可证 | 说明 |
| --- | --- | --- | --- |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |  |
| duckduckgo_search | 8.1.1 | MIT License |  |
| huggingface_hub | 2.4.0 | Apache-2.0 |  |
| openpyxl | 3.1.5 | MIT |  |
| playwright | 1.63.0 | Apache-2.0 |  |
| pydantic | 2.13.5 | MIT |  |
| pypdf | 6.21.4 | BSD-3-Clause |  |
| python-docx | 1.2.0 | MIT |  |
| python-pptx | 1.0.2 | MIT |  |
| pywin32 | 312 | PSF |  |
| requests | 2.34.2 | Apache-2.0 |  |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |  |
| urllib3 | 2.8.0 | MIT |  |
| xlrd | 2.0.2 | BSD |  |

## 二、高风险许可证提示

- 本次扫描未发现 AGPL/GPL/LGPL/SSPL 类许可证组件。

### 合规结论与建议

- **自用 / 组织内部使用**：不构成「对外分发」，上述著佐权义务不触发，当前依赖可直接使用。
- **对外分发（含免费公开）**：v0.2.1 起发布物中已无 AGPL/GPL 运行时组件。
  历史情况：v0.2.0 及更早版本的 PDF 解析用的是 AGPL 的 PyMuPDF，
  v0.2.1 换成了 BSD 的 `pypdf`，因此**同一个 Apache-2.0 许可即可覆盖整个发布物**。
- **文件解析全部为宽松许可**：PDF 用 `pypdf`（BSD-3），Word 用 `python-docx`（MIT）、
  Excel 用 `openpyxl`（MIT）/`xlrd`（BSD），PPTX 用 `python-pptx`（MIT），
  TXT/CSV 直接读取——均可闭源或开源分发。
- **PyInstaller 无需担心**：其 GPLv2 带官方例外，明确允许打包分发专有程序，
  且它只用于构建、不以库的形式随产物使用。
- **技能（插件）由第三方提供**：用户自行安装的技能可能有各自的许可证，
  EchoDesk 不以任何方式再分发技能代码；设置窗口会展示技能声明的许可证供用户判断。

## 三、前端组件

- 自包含 .NET 运行时与 WPF 程序集：MIT 许可（Microsoft .NET）。
- 未引用任何第三方 NuGet 包（JSON 通信使用内置 System.Text.Json），
  因此前端侧无额外开源合规负担。

## 四、随包分发的原生库

- **llama.cpp**（vendored 原生运行时）：**MIT** 许可，上游 `https://github.com/ggml-org/llama.cpp`。
  版本 **0.4.7**，三平台各一套，随仓库分发于 `backend/native/win-x64/`、
  `backend/native/linux-x64/`、`backend/native/osx-arm64/`；二进制在**构建期**从
  `JamePeng/llama-cpp-python` 0.4.7 的 wheel 中抽取，`llama-cpp-python` 本身**不是运行期依赖**。
  出处（wheel 文件名、URL、sha256、逐文件摘要）见 `backend/native/VENDOR.md`。
- Playwright 驱动：Apache-2.0；其驱动的浏览器（Microsoft Edge）由用户系统安装，
  EchoDesk 不重新分发浏览器本体。

<!-- sbom:python-detail -->
## 五、全部 Python 发行包明细

| 包名 | 版本 | 许可证 |
| --- | --- | --- |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.16.0 | MIT |
| certifi | 2026.10.23 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| colorama | 0.4.6 | BSD License |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| duckduckgo_search | 8.1.1 | MIT License |
| et_xmlfile | 2.0.0 | MIT |
| filelock | 4.2.8 | MIT |
| fsspec | 2026.9.0 | BSD-3-Clause |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| hf-xet | 1.8.0 | Apache-2.0 |
| httpcore2 | 2.16.0 | BSD-3-Clause |
| httpx2 | 2.16.0 | BSD-3-Clause |
| huggingface_hub | 2.4.0 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |
| lxml | 6.1.3 | BSD-3-Clause |
| openpyxl | 3.1.5 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pillow | 13.0.0 | MIT-CMU |
| playwright | 1.63.0 | Apache-2.0 |
| primp | 2.0.1 | MIT License |
| pycparser | 3.12 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic_core | 2.46.5 | MIT |
| pyee | 13.0.1 | MIT |
| pypdf | 6.21.4 | BSD-3-Clause |
| python-docx | 1.2.0 | MIT |
| python-pptx | 1.0.2 | MIT |
| pywin32 | 312 | PSF |
| PyYAML | 6.0.3 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| truststore | 0.10.5 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT |
| xlrd | 2.0.2 | BSD |
| xlsxwriter | 3.3.1 | BSD-2-Clause |

---

生成方式：`python tools/gen_licenses.py`（读取本机已安装发行包的元数据）。
复核建议：正式发布前用 `pip-licenses --format=markdown` 或 Syft 生成 SBOM 再核对一次。
注意：本脚本读的是**本机环境**，若开发环境里残留已废弃的包（例如为了对照而没卸掉的
`pymupdf`），它会出现在清单里。发布前请在干净的 venv（只装 backend/requirements.txt）
里重新生成，或手工删除这些行。
