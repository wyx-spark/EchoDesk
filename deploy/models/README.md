# models 目录

模型文件（如 `Qwen3.5-0.8B-Q4_K_M.gguf`）放置于此目录。

程序启动时若检测不到本地模型，会自动从 Hugging Face 下载并加载：
- 通过 `config.json` 的 `hf_endpoint` 配置镜像，默认 `https://hf-mirror.com`（国内可达）
- `model_repo` 默认 `unsloth/Qwen3.5-0.8B-GGUF`
- 下载前会先在仓库中做模糊匹配，解析出真实存在的文件名（如 `Qwen3.5-0.8B-Q4_K_M.gguf`），
  并把匹配结果同步回 `config.json` 的 `model_path` / `models.*.path`，下次启动直接使用

也可以用 `/model <名称>` 命令查看、切换或手动下载模型；或自行把模型文件放入此目录后
修改 `config.json` 中的 `model_path` 指向它。
