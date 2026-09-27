# models 目录

模型文件（如 `Qwen3.5-0.8B-Q4_K_M.gguf`）放置于此目录。

程序启动时若检测不到本地模型，会自动从 Hugging Face 下载并加载：
- 通过 `config.json` 的 `hf_endpoint` 配置镜像，默认 `https://hf-mirror.com`（国内可达）
- `model_repo` 默认 `unsloth/Qwen3.5-0.8B-GGUF`
- 下载前会先在仓库中做模糊匹配，解析出真实存在的文件名（如 `Qwen3.5-0.8B-Q4_K_M.gguf`），
  并把匹配结果同步回 `config.json` 的 `model_path` / `models.*.path`，下次启动直接使用

也可以用 `/model <名称>` 命令查看、切换或手动下载模型；或自行把模型文件放入此目录后
修改 `config.json` 中的 `model_path` 指向它。

---

# models directory

Model files (such as `Qwen3.5-0.8B-Q4_K_M.gguf`) go in this directory.

If the program finds no local model at start-up, it downloads one from Hugging Face and loads it
automatically:
- The mirror is configured through `hf_endpoint` in `config.json`; the default is
  `https://hf-mirror.com` (reachable from mainland China)
- `model_repo` defaults to `unsloth/Qwen3.5-0.8B-GGUF`
- Before downloading it does a fuzzy match in the repository to resolve the file name that actually
  exists (such as `Qwen3.5-0.8B-Q4_K_M.gguf`), then writes the match back to `model_path` /
  `models.*.path` in `config.json`, so the next start uses it directly

You can also use the `/model <name>` command to list, switch or manually download models; or put a
model file into this directory yourself and point `model_path` in `config.json` at it.
