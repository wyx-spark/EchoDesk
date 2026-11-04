# models 目录

> 本文是 [README.md](README.md) 的中文参考译本。如两者有歧义，**以英文版为准**。

模型文件（如 `Qwen3-0.6B-Q6_K.gguf`）放置于此目录。

程序启动时若检测不到本地模型，会自动从 Hugging Face 下载并加载：
- 通过 `config.json` 的 `hf_endpoint` 配置镜像，默认 `https://hf-mirror.com`（国内可达）
- `model_repo` 默认 `unsloth/Qwen3-0.6B-GGUF`
- 下载前会先在仓库中做模糊匹配，解析出真实存在的文件名（如 `Qwen3-0.6B-Q6_K.gguf`），
  并把匹配结果同步回 `config.json` 的 `model_path` / `models.*.path`，下次启动直接使用

也可以用 `/model <名称>` 命令查看、切换或手动下载模型；或自行把模型文件放入此目录后
修改 `config.json` 中的 `model_path` 指向它。

## 程序实际从哪个目录加载模型

`deploy/models/` 是**构建期**的模型来源，不是运行中的程序读取的那个目录。
把两者混为一谈，正是"模型明明放进去了却不生效"最常见的成因：

| 目录 | 谁在用 | 怎么解析 |
| --- | --- | --- |
| `deploy/models/` | `tools/build_release.ps1 -IncludeModels` | 仅构建期：这里的 `.gguf` 会被复制进发布物的 `app/backend/models/` |
| 后端工作目录下的 `models/` | 运行中的后端 | `config.json` 的 `model_hub.models_dir`（默认 `models`）按**后端自己的工作目录**解析成绝对路径 |

前端启动后端时，会把后端所在目录设为它的工作目录，所以实际位置随安装方式而变：

| EchoDesk 是怎么装的 | 后端工作目录 | `.gguf` 放这里 |
| --- | --- | --- |
| Windows 安装程序 / 便携版 zip | `<发布目录>\app\backend\` | `<发布目录>\app\backend\models\` |
| macOS / Linux 压缩包 | `<发布目录>/app/backend/app/` | `<发布目录>/app/backend/app/models/` |
| 源码检出（在 `backend/` 里跑后端） | `backend/` | `backend/models/` |

不必靠猜：没有可用模型时，程序给出的引导里会直接印出解析后的模型文件夹
（"模型文件夹：…"）；`config.json` 的 `model_hub.models_dir` 也可以把它指到别处。

## 三平台命令

三平台的发布物目录结构一致，差别只在复制命令与路径分隔符：

```
# Windows (PowerShell)
Copy-Item model.gguf "<release directory>\app\backend\models\"

# macOS / Linux
cp model.gguf "<release directory>/app/backend/models/"
```

源码检出下把目标目录换成 `backend/models/`（Windows 上是 `backend\models\`）。

两点平台差异值得单独说明：

- **Windows**：安装程序的默认安装目录就是**安装程序自己所在的文件夹**
  （`tools/installer.iss` 的 `GetDefaultDir`），所以"发布目录"就是你放
  `EchoDesk-<版本>-Setup.exe` 的那个文件夹。安装程序**不含模型**：它排除 `*.gguf`，
  需要首次启动时下载，或由你自己放进去。
- **macOS / Linux**：发布物是压缩包、没有安装程序，解压到任意可写目录即可；
  解压后若可执行位丢失，用户手册给出安装、日常使用与无图形环境下的替代做法。

模型市场搜索结果的磁盘缓存，是本文唯一不在发布目录内的、与模型相关的路径：
`model_hub.search_cache_dir` 留空时，它落在用户数据目录下的 `cache/model_hub`，
而用户数据目录按平台分别是 `~/Library/Application Support/EchoDesk`（macOS）、
`$XDG_DATA_HOME/EchoDesk` 或 `~/.local/share/EchoDesk`（Linux），
Windows 上则是便携版的外层目录，或 `ECHODESK_DATA` 指定的位置。
