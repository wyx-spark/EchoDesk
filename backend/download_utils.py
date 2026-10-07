"""模型文件检查、下载（支持 Hugging Face 镜像）与仓库内匹配查找。"""
import difflib
import os
import re
import sys
from functools import partial
from typing import Callable, Iterable, Optional, Tuple

from huggingface_hub import HfApi, hf_hub_download

from i18n import t as _t
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


#: 量化代码（``Q4_K_M`` / ``IQ4_XS`` / ``BF16`` …）。用于判断"提示里是否已经点名了量化"。
_QUANT_RE = re.compile(r"(?:iq|q)\d+(?:_[a-z0-9]+)*|bf16|f16|f32", re.IGNORECASE)

#: 没有点名量化时优先选用的通用档位（历史行为）。
_PREFERRED_QUANT = "q4_k_m"


def _stem(path: str) -> str:
    """取文件名主干并小写，且**幂等**：``a/b/Qwen3.5-0.8B-Q6_K.gguf`` → ``qwen3.5-0.8b-q6_k``。

    这里刻意只切掉末尾的 ``.gguf``，而不是用 ``os.path.splitext``：后者按**最后一个点**
    切，而模型名里本来就有点（版本号 ``Qwen3.5``）—— ``splitext("qwen3.5-0.8b-q6_k")``
    得到的是 ``("qwen3.5-0", ".8b-q6_k")``，量化代码会被切掉，重复调用两次的结果就不一样了
    （写测试时真踩到：先取过主干再交给 :func:`quant_of`，于是"提示里点名了 Q6_K"被判成
    "没点名"，通用档位加分又活了回来）。
    """
    name = os.path.basename(str(path or "")).lower()
    return name[: -len(".gguf")] if name.endswith(".gguf") else name


def quant_of(name: str) -> str:
    """取出名字里的量化代码（取最后一个，如 ``qwen3-0.6b-q6_k`` → ``q6_k``）；没有则空串。

    带不带 ``.gguf`` 都可以（见 :func:`_stem` 的幂等说明）。
    """
    matches = _QUANT_RE.findall(_stem(name))
    return matches[-1].lower() if matches else ""


def rank_model_files(files: Iterable[str], hint: str) -> str:
    """在候选文件名里挑出与 hint 最匹配的一个（**纯函数**，不联网，便于单测）。

    评分规则与踩过的坑（v0.2.1 默认模型切换）：

    * 用 ``difflib`` 比较"去掉扩展名、小写"的主干相似度；
    * **只有当 hint 自己没有点名量化时**，才给含 ``q4_k_m`` 的候选加分 ——
      这个加分最早是为了"用户只说模型名时给个通用档位"，但它是无条件加的，
      于是任何仓库里只要有 Q4_K_M，用户的显式选择（例如配了 Q6_K）就会被顶掉：
      实测 hint=``Qwen3.5-0.8B-Q6_K.gguf`` 对 ``bartowski/Qwen_Qwen3.5-0.8B-GGUF``
      解析成 ``Qwen_Qwen3.5-0.8B-Q4_K_M.gguf``（552.8 MB）、对
      ``lmstudio-community/Qwen3.5-0.8B-GGUF`` 解析成 ``Qwen3.5-0.8B-Q4_K_M.gguf``
      （503.1 MB）—— 配了 Q6_K 却下到 Q4_K_M，而且没有任何提示。
      现在"提示里写了量化"就等于"用户已经选好了"，加分不再参与。
    """
    candidates = [str(f) for f in files if str(f)]
    if not candidates:
        raise ValueError("候选文件列表为空")

    hint_stem = _stem(hint)
    prefer_generic = not quant_of(hint)

    def score(path: str) -> float:
        stem = _stem(path)
        value = difflib.SequenceMatcher(None, hint_stem, stem).ratio()
        if prefer_generic and _PREFERRED_QUANT in stem:
            value += 0.3
        return value

    return max(candidates, key=score)


def find_best_model(repo_id: str, hint: str, hf_endpoint: Optional[str] = None) -> Tuple[str, Optional[int]]:
    """在仓库中查找与 hint 匹配度最高的 .gguf 文件，返回 (文件名, 大小字节)。"""
    if hf_endpoint:
        os.environ["HF_ENDPOINT"] = hf_endpoint
    api = HfApi()
    files = [f for f in api.list_repo_files(repo_id) if f.lower().endswith(".gguf")]
    if not files:
        raise RuntimeError(f"仓库 {repo_id} 中没有 .gguf 文件")

    best = rank_model_files(files, hint)
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
