# EchoDesk 第三方组件与许可证清单

本文件列出 EchoDesk 分发时随附的第三方组件及其许可证，用于开源合规审查。
扫描到的 Python 发行包共 132 个（含传递依赖）。

## 一、后端直接依赖

| 组件 | 版本 | 许可证 | 说明 |
| --- | --- | --- | --- |
| huggingface_hub | 1.27.0 | Apache-2.0 |  |
| playwright | 1.63.0 | Apache-2.0 |  |
| pydantic | 2.13.4 | MIT |  |
| pyinstaller | 6.22.1 | GNU General Public License v2 (GPLv2) |  |
| pymupdf | 1.28.2 | Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License |  |
| pywin32 | 312 | PSF |  |
| pyxtxt | 0.3.5 | MIT License |  |
| schedule | 1.2.2 | MIT License |  |
| tqdm | 4.70.0 | MPL-2.0 AND MIT |  |

## 二、高风险许可证提示

- **extract-msg 0.56.1** — GPL
  - GPL-3.0；由 pyxtxt 解析邮件类文档时可能被引入，若被打进发布物则整体分发需遵循 GPL
- **pcodedmp 1.2.6** — GPL
  - GPL-3.0；extract-msg 的依赖
- **pygame 2.6.1** — LGPL
  - LGPL；pyxtxt 依赖链引入
- **pyinstaller 6.22.1** — GNU General Public License v2 (GPLv2)
  - GPLv2 + 官方特别例外：明确允许用 PyInstaller 打包并分发专有程序，打包产物本身不受 GPL 约束（构建工具，不随产物以库形式使用）
- **pymupdf 1.28.2** — Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License
  - AGPL-3.0 或 Artifex 商业许可二选一；由 pyxtxt 解析 PDF 时引入，随发布物分发会产生 AGPL 义务（对外分发需提供对应源码，或购买商业许可）
- **pynput 1.8.2** — LGPLv3
  - LGPL-3.0；pyxtxt 依赖链引入
- **python-bidi 0.6.11** — GNU Library or Lesser General Public License (LGPL)
  - LGPL；文本方向处理依赖
- **RTFDE 0.1.2.2** — GNU Lesser General Public License v3 (LGPLv3)
  - LGPL-3.0；extract-msg 的依赖

### 合规结论与建议

- **自用 / 组织内部使用**：不构成「对外分发」，上述著佐权义务不触发，当前依赖可直接使用。
- **对外分发（含免费公开）**：需要处理 pyxtxt 依赖链中的 AGPL/GPL 组件。可选做法：
  1. 随发布物一并提供对应组件的完整源码（AGPL/GPL 允许，但需注明修改与获取方式）；
  2. 或替换文件解析实现，绕开这些组件——PDF 用 `pypdf`/`pdfminer.six`（BSD/MIT）、
     Word 用 `python-docx`（MIT）、Excel 用 `openpyxl`（MIT）、PPTX 用 `python-pptx`（MIT）、
     TXT/CSV 直接读取，这几类均为宽松许可，可闭源分发；
  3. 或购买 PyMuPDF 商业许可（Artifex）以解除 AGPL 约束（仅解决 PDF 一路）。
- **PyInstaller 无需担心**：其 GPLv2 带官方例外，明确允许打包分发专有程序。
- 发布物体积优化顺带也能降低该风险：若用 `--exclude-module` 排除未被用到的
  解析分支（如邮件/RTF），可减少随包分发的 GPL 组件数量。

## 三、前端组件

- 自包含 .NET 运行时与 WPF 程序集：MIT 许可（Microsoft .NET）。
- 未引用任何第三方 NuGet 包（JSON 通信使用内置 System.Text.Json），
  因此前端侧无额外开源合规负担。

## 四、随包分发的原生库

- llama.cpp（llama-cpp-python 内嵌的 llama.dll / ggml 系列）：MIT 许可。
- Playwright 驱动：Apache-2.0；其驱动的浏览器（Microsoft Edge）由用户系统安装，
  EchoDesk 不重新分发浏览器本体。

## 五、全部 Python 发行包明细

| 包名 | 版本 | 许可证 |
| --- | --- | --- |
| altgraph | 0.17.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.14.2 | MIT |
| beautifulsoup4 | 4.15.0 | MIT License |
| blinker | 1.9.0 | MIT License |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| chardet | 7.6.0 | 0BSD |
| charset-normalizer | 3.4.9 | MIT |
| click | 8.4.2 | BSD-3-Clause |
| colorama | 0.4.6 | BSD License |
| colorclass | 2.2.2 | MIT |
| compressed-rtf | 1.0.7 | MIT |
| comtypes | 1.4.16 | MIT |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| cssselect | 1.5.0 | BSD-3-Clause |
| defusedxml | 0.7.1 | PSFL |
| diskcache | 5.6.3 | Apache 2.0 |
| duckduckgo_search | 8.1.1 | MIT License |
| easygui | 0.98.3 | BSD |
| easyocr | 1.7.2 | Apache License 2.0 |
| ebcdic | 2.0.1 | BSD-2-Clause |
| EbookLib | 0.20 | GNU Affero General Public License |
| et_xmlfile | 2.0.0 | MIT |
| extract-msg | 0.56.1 | GPL |
| filelock | 3.32.3 | MIT |
| Flask | 3.1.3 | BSD-3-Clause |
| fsspec | 2026.7.0 | BSD-3-Clause |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| hf-xet | 1.6.0 | Apache-2.0 |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| huggingface_hub | 1.27.0 | Apache-2.0 |
| idna | 3.18 | BSD-3-Clause |
| ImageIO | 2.37.4 | BSD-2-Clause |
| itsdangerous | 2.2.0 | BSD License |
| jieba | 0.42.1 | MIT |
| Jinja2 | 3.1.6 | BSD License |
| lark | 1.3.1 | MIT |
| lazy-loader | 0.5 | BSD-3-Clause |
| linkify-it-py | 2.1.0 | MIT |
| llama_cpp_python | 0.3.35 | MIT |
| llvmlite | 0.49.0 | BSD-2-Clause AND Apache-2.0 WITH LLVM-exception |
| lxml | 6.1.1 | BSD-3-Clause |
| lxml_html_clean | 0.4.5 | BSD-3-Clause |
| Markdown | 3.10.3 | BSD-3-Clause |
| markdown-it-py | 4.2.0 | MIT License |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| mdit-py-plugins | 0.6.1 | MIT License |
| mdurl | 0.1.2 | MIT License |
| more-itertools | 11.1.0 | MIT |
| mpmath | 1.3.0 | BSD |
| msoffcrypto-tool | 6.0.0 | MIT |
| networkx | 3.6.1 | BSD-3-Clause |
| ninja | 1.13.0 | Apache Software License |
| numba | 0.67.0 | BSD |
| numpy | 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| odfpy | 1.4.1 | Apache Software License |
| olefile | 0.47 | BSD |
| oletools | 0.60.2 | BSD |
| ollama | 0.6.2 | MIT |
| openai-whisper | 20250625 | MIT |
| opencv-python-headless | 5.0.0.93 | Apache 2.0 |
| openpyxl | 3.1.5 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| Panda3D | 1.10.16 | Modified BSD License |
| pcodedmp | 1.2.6 | GPL |
| pefile | 2024.8.26 | MIT |
| pillow | 12.3.0 | MIT-CMU |
| pip | 26.2.1 | MIT |
| platformdirs | 4.11.3 | MIT |
| playwright | 1.63.0 | Apache-2.0 |
| primp | 1.3.1 | MIT License |
| pyclipper | 1.4.0 | MIT |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| pydantic_core | 2.46.4 | MIT |
| pyee | 13.0.1 | MIT |
| pygame | 2.6.1 | LGPL |
| Pygments | 2.21.0 | BSD-2-Clause |
| pyinstaller | 6.22.1 | GNU General Public License v2 (GPLv2) |
| pyinstaller-hooks-contrib | 2026.6 | Apache Software License |
| pylatexenc | 2.11 | MIT |
| pymupdf | 1.28.2 | Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License |
| pynput | 1.8.2 | LGPLv3 |
| PyOpenGL | 3.1.10 | BSD License |
| PyOpenGL-accelerate | 3.1.10 | BSD License |
| pyparsing | 3.3.2 | MIT |
| pyperclip | 1.11.0 | BSD |
| pypiwin32 | 223 | UNKNOWN |
| python-bidi | 0.6.11 | GNU Library or Lesser General Public License (LGPL) |
| python-docx | 1.2.0 | MIT |
| python-magic-bin | 0.4.14 | MIT |
| python-pptx | 1.0.2 | MIT |
| pyttsx3 | 2.99 | MPL-2.0 |
| pywin32 | 312 | PSF |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause |
| pyxtxt | 0.3.5 | MIT License |
| PyYAML | 6.0.3 | MIT |
| readability-lxml | 0.8.4.1 | Apache License 2.0 |
| red-black-tree-mod | 1.22 | MIT |
| regex | 2026.7.19 | Apache-2.0 AND CNRI-Python |
| requests | 2.34.2 | Apache-2.0 |
| resemble | 1.9.0 | Apache Software License |
| rich | 15.0.0 | MIT |
| RTFDE | 0.1.2.2 | GNU Lesser General Public License v3 (LGPLv3) |
| schedule | 1.2.2 | MIT License |
| scikit-image | 0.26.0 | BSD License |
| scipy | 1.18.0 | BSD License |
| setuptools | 84.0.0 | MIT |
| shapely | 2.1.2 | BSD 3-Clause |
| six | 1.17.0 | MIT |
| soupsieve | 2.9.2 | MIT |
| striprtf | 0.0.32 | BSD-3-Clause |
| sympy | 1.14.0 | BSD |
| textual | 8.2.8 | MIT |
| tifffile | 2026.8.16 | BSD-3-Clause |
| tiktoken | 0.13.0 | MIT License Copyright (c) 2022 OpenAI, Shantanu Jain Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE. |
| torch | 2.13.0 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT |
| torchvision | 0.28.0 | BSD |
| tqdm | 4.70.0 | MPL-2.0 AND MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tzdata | 2026.3 | Apache-2.0 |
| tzlocal | 5.4.4 | MIT |
| uc-micro-py | 2.0.0 | MIT |
| urllib3 | 2.7.0 | MIT |
| Werkzeug | 3.1.8 | BSD-3-Clause |
| win_unicode_console | 0.5 | MIT |
| xlrd | 2.0.2 | BSD |
| xlsxwriter | 3.2.9 | BSD-2-Clause |

---

生成方式：`python tools/gen_licenses.py`（读取本机已安装发行包的元数据）。
复核建议：正式发布前用 `pip-licenses --format=markdown` 或 Syft 生成 SBOM 再核对一次；
若引入 AGPL 组件（如 PyMuPDF），需评估以网络服务方式提供时的源码开放义务。
