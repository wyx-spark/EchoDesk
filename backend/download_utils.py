"""
[EN]
Model file detection, download (with Hugging Face mirror support), and in-repository
matching.

[ZH]
模型文件检查、下载（支持 Hugging Face 镜像）以及仓库内的匹配查找。
"""
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
    """
    [EN]
    Forwards huggingface_hub's internal download progress to the ``on_progress``
    callback for streaming reporting.

    [ZH]
    将 huggingface_hub 内部的下载进度转发给 ``on_progress`` 回调（用于流式上报）。
    """

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
    """
    [EN]
    Return the absolute path if the model file already exists locally, otherwise
    None.

    If exact-name matching fails, the function falls back to any ``*.gguf`` file in
    the models directory, which tolerates differences in dots and hyphens within
    file names.

    [ZH]
    若本地已存在模型文件则返回其绝对路径，否则返回 None。

    精确文件名匹配失败时，会在 models 目录下回退查找任意 ``*.gguf`` 文件，以兼容
    文件名中点号与连字符存在差异的情况。
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


#: [EN] Quantization codes (``Q4_K_M`` / ``IQ4_XS`` / ``BF16`` ...). Used to detect
#: [EN] whether the hint already names a quantization.
#: [ZH] 量化代码（``Q4_K_M`` / ``IQ4_XS`` / ``BF16`` 等）。用于判断提示中是否已明确
#: [ZH] 指定量化档位。
_QUANT_RE = re.compile(r"(?:iq|q)\d+(?:_[a-z0-9]+)*|bf16|f16|f32", re.IGNORECASE)

#: [EN] Generic quantization tier preferred when the hint does not name one
#: [EN] (historical behavior).
#: [ZH] 未指定量化时优先选用的通用档位（历史行为）。
_PREFERRED_QUANT = "q4_k_m"


def _stem(path: str) -> str:
    """
    [EN]
    Return the lowercase file-name stem and remain idempotent:
    ``a/b/Qwen3.5-0.8B-Q6_K.gguf`` -> ``qwen3.5-0.8b-q6_k``.

    Only the trailing ``.gguf`` is stripped here, deliberately rather than using
    ``os.path.splitext``. The latter splits at the last dot, and model names
    legitimately contain dots (version numbers such as ``Qwen3.5``):
    ``splitext("qwen3.5-0.8b-q6_k")`` yields ``("qwen3.5-0", ".8b-q6_k")``, which
    drops the quantization code and makes repeated calls non-idempotent. A test
    exposed this regression: the stem was computed once and then passed to
    :func:`quant_of`, so "the hint names Q6_K" was misclassified as "no
    quantization named" and the generic-tier scoring bonus applied again.

    [ZH]
    取文件名主干并转为小写，且保持幂等：
    ``a/b/Qwen3.5-0.8B-Q6_K.gguf`` → ``qwen3.5-0.8b-q6_k``。

    此处刻意只截掉末尾的 ``.gguf``，而不使用 ``os.path.splitext``：后者按最后一个
    点切分，而模型名本身就可能含点（版本号 ``Qwen3.5``）。
    ``splitext("qwen3.5-0.8b-q6_k")`` 得到 ``("qwen3.5-0", ".8b-q6_k")``，会切掉
    量化代码，导致重复调用的结果不一致。测试已暴露该回归：先取过主干再传给
    :func:`quant_of`，于是"提示中已指定 Q6_K"被判定为"未指定量化"，通用档位加分
    再次生效。
    """
    name = os.path.basename(str(path or "")).lower()
    return name[: -len(".gguf")] if name.endswith(".gguf") else name


def quant_of(name: str) -> str:
    """
    [EN]
    Return the quantization code contained in the name (the last match, e.g.
    ``qwen3-0.6b-q6_k`` -> ``q6_k``); return an empty string if none is present.

    The name may include or omit the ``.gguf`` suffix (see the idempotency note in
    :func:`_stem`).

    [ZH]
    取出名称中的量化代码（取最后一个匹配，如 ``qwen3-0.6b-q6_k`` → ``q6_k``）；
    不存在时返回空串。

    名称可包含或省略 ``.gguf`` 后缀（参见 :func:`_stem` 的幂等性说明）。
    """
    matches = _QUANT_RE.findall(_stem(name))
    return matches[-1].lower() if matches else ""


def rank_model_files(files: Iterable[str], hint: str) -> str:
    """
    [EN]
    Select the candidate file name that best matches ``hint`` (a pure function: no
    network access, so it is directly unit-testable).

    Scoring rules and the regression behind them (v0.2.1 default-model switching):

    * ``difflib`` compares the lowercased stems with the extension removed.
    * Candidates containing ``q4_k_m`` receive a bonus only when the hint itself does
      not name a quantization. This bonus originally existed to provide a generic
      tier when the user supplied only a model name, but it was applied
      unconditionally, so any repository containing Q4_K_M would override an explicit
      user choice (for example a configured Q6_K). Measured behavior:
      hint=``Qwen3.5-0.8B-Q6_K.gguf`` resolved to
      ``Qwen_Qwen3.5-0.8B-Q4_K_M.gguf`` (552.8 MB) on
      ``bartowski/Qwen_Qwen3.5-0.8B-GGUF`` and to ``Qwen3.5-0.8B-Q4_K_M.gguf``
      (503.1 MB) on ``lmstudio-community/Qwen3.5-0.8B-GGUF``, silently downloading
      Q4_K_M while Q6_K was configured. A quantization named in the hint now means
      the user has already made the selection, and the bonus does not participate.

    [ZH]
    在候选文件名中选出与 hint 最匹配的一个（纯函数，不联网，便于单元测试）。

    评分规则及其背后的回归问题（v0.2.1 默认模型切换）：

    * 使用 ``difflib`` 比较"去掉扩展名并转为小写"后的主干相似度；
    * 仅当 hint 本身未指定量化时，才对含 ``q4_k_m`` 的候选加分。该加分最初用于
      "用户只给出模型名时提供一个通用档位"，但它是无条件应用的，因此只要仓库中存在
      Q4_K_M，用户的显式选择（例如配置了 Q6_K）就会被覆盖。实测：hint 为
      ``Qwen3.5-0.8B-Q6_K.gguf`` 时，在 ``bartowski/Qwen_Qwen3.5-0.8B-GGUF`` 仓库
      中解析为 ``Qwen_Qwen3.5-0.8B-Q4_K_M.gguf``（552.8 MB），在
      ``lmstudio-community/Qwen3.5-0.8B-GGUF`` 仓库中解析为
      ``Qwen3.5-0.8B-Q4_K_M.gguf``（503.1 MB）——配置的是 Q6_K 却下载了 Q4_K_M，
      且没有任何提示。现在"提示中写明了量化"即等同于"用户已完成选择"，该加分不再
      参与。
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
    """
    [EN]
    Find the best-matching .gguf file in the repository and return
    ``(file name, size in bytes)``.

    [ZH]
    在仓库中查找与 hint 匹配度最高的 .gguf 文件，返回 ``(文件名, 大小字节)``。
    """
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
    """
    [EN]
    Download a model from Hugging Face (a mirror endpoint may be configured) and
    return the local absolute path.

    on_progress: optional callback (bytes downloaded, total bytes), invoked
    repeatedly during the download to support streaming progress display.

    [ZH]
    从 Hugging Face（可配置镜像）下载模型，返回本地绝对路径。

    on_progress: 可选回调 (已下载字节, 总字节)，下载过程中会被反复调用，用于流式
    进度展示。
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
