"""模型文件检查、下载（支持 Hugging Face 镜像）与仓库内匹配查找。"""
import difflib
import os
import sys
from functools import partial
from typing import Callable, Optional, Tuple

from huggingface_hub import HfApi, hf_hub_download
from tqdm import tqdm


class _ProgressTqdm(tqdm):
    """把 huggingface_hub 内部下载进度转发给 on_progress 回调（供流式上报）。"""

    def __init__(self, *args, on_progress: Optional[Callable[[int, int], None]] = None, **kwargs):
        self._on_progress = on_progress
        super().__init__(*args, **kwargs)

    def update(self, n: int = 1) -> Optional[bool]:
        result = super().update(n)
        if self._on_progress is not None and self.total:
            try:
                self._on_progress(int(self.n), int(self.total))
            except Exception:
                pass
        return result



def check_model(model_name: str, local_dir: str = "./models") -> Optional[str]:
    """若本地已存在模型文件则返回绝对路径，否则返回 None。

    精确名匹配失败时，会在 models 目录下回退查找任意 ``*.gguf``，
    以兼容文件名含点号/连字符差异的情况。
    """
    os.makedirs(local_dir, exist_ok=True)
    exact = os.path.join(local_dir, model_name)
    if os.path.exists(exact):
        return os.path.abspath(exact)

    for entry in os.listdir(local_dir):
        if entry.lower().endswith(".gguf"):
            print(f"ℹ️ 未找到 {model_name}，改用 {entry}", file=sys.stderr)
            return os.path.abspath(os.path.join(local_dir, entry))
    return None


def find_best_model(repo_id: str, hint: str, hf_endpoint: Optional[str] = None) -> Tuple[str, Optional[int]]:
    """在仓库中查找与 hint 匹配度最高的 .gguf 文件，返回 (文件名, 大小字节)。"""
    if hf_endpoint:
        os.environ["HF_ENDPOINT"] = hf_endpoint
    api = HfApi()
    files = [f for f in api.list_repo_files(repo_id) if f.lower().endswith(".gguf")]
    if not files:
        raise RuntimeError(f"仓库 {repo_id} 中没有 .gguf 文件")

    def score(path: str) -> float:
        base = os.path.splitext(os.path.basename(path))[0].lower()
        s = difflib.SequenceMatcher(None, hint.lower(), base).ratio()
        if "q4_k_m" in base:
            s += 0.3  # 优先选择通用的 Q4_K_M 量化
        return s

    best = max(files, key=score)
    size = None
    try:
        infos = api.get_paths_info(repo_id, [best])
        if infos:
            size = infos[0].size
    except Exception:
        pass
    return best, size


def download_model(
    model_name: str,
    repo_id: str,
    local_dir: str = "./models",
    hf_endpoint: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """从 Hugging Face（可配镜像）下载模型，返回本地绝对路径。

    on_progress: 可选回调 (已下载字节, 总字节)，下载过程中被反复调用（供流式进度展示）。
    """
    if hf_endpoint:
        os.environ["HF_ENDPOINT"] = hf_endpoint
    print(f"📥 正在下载模型 '{model_name}' ...", file=sys.stderr)
    try:
        downloaded = hf_hub_download(
            repo_id=repo_id,
            filename=model_name,
            local_dir=local_dir,
            tqdm_class=partial(_ProgressTqdm, on_progress=on_progress) if on_progress else None,
        )
        print(f"✅ 下载完成: {downloaded}", file=sys.stderr)
        return os.path.abspath(downloaded)
    except Exception as e:
        raise RuntimeError(f"模型下载失败: {e}") from e
