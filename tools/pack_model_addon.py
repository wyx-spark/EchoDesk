#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打「模型补充包」并核对模型来源（离线用户用）。

[EN] Pack the offline model add-on and verify that every local model is the
official upstream file. The release tree never ships model weights; offline
users obtain them from the GitHub Releases tag ``models-qwen-v1``.
[ZH] 打「模型补充包」并核对模型来源（离线用户用）。发布物一律不含模型权重；
离线用户从 GitHub Releases 的 ``models-qwen-v1`` 标签获取模型包。

[EN] Why this tool exists: the installer and the three platform archives ship
no model, the first launch asks whether to download one, and air-gapped users
need a pack. Building that pack has four error-prone steps — locate the model,
prove it is the official file, choose a format, write checksums — so the logic
lives here instead of a one-off script.
[ZH] 本工具存在的原因：安装程序与三平台发布包都不含模型，首次启动询问是否下载，
离线环境靠“模型补充包”解决。而打这个包要做四件事，每件都容易漏：找模型、
核对它确实是官方那一份、选格式、写校验清单。一次性脚本做一次就散了。

[EN] Modes:

    python tools/pack_model_addon.py --catalog       # print the catalog offline
    python tools/pack_model_addon.py --local-only    # local audit, no network
    python tools/pack_model_addon.py --verify-only   # local + upstream check
    python tools/pack_model_addon.py --plan          # upstream sizes only
    python tools/pack_model_addon.py --pack          # verify, then pack

[ZH] 模式：

    python tools/pack_model_addon.py --catalog       # 离线打印模型清单
    python tools/pack_model_addon.py --local-only    # 只清点本地，不联网
    python tools/pack_model_addon.py --verify-only   # 本地 + 上游核对
    python tools/pack_model_addon.py --plan          # 只解析上游体积
    python tools/pack_model_addon.py --pack          # 核对后打包

[EN] Upstream identity: the repository does not record model sha256 values
(``git grep`` confirms it), but ``backend/model_hub.py`` verifies downloads
against the Hugging Face API LFS ``sha256`` field. This tool uses the same
endpoint and the same User-Agent (mirrors reject urllib's default UA).
[ZH] 核对用什么是“官方那一份”：仓库里没有记录模型文件的 sha256（`git grep`
确认过），但 `backend/model_hub.py` 下载时自带 `verify_sha256`，比对的是
HuggingFace 仓库 API 返回的 LFS `sha256` 字段 —— 项目自己认定的正确哈希就是
这个值。本工具用同一个端点、同一个 UA（镜像站会拒绝 urllib 的默认 UA）。

[EN] Format: GGUF weights are already quantized and have high entropy; zip and
gzip both save only about 1.8% and differ by less than 500 bytes, so only zip is
produced. Evidence is in ``artifacts/compress-probe.json``; ``--measure``
re-measures it on the current machine.
[ZH] 格式：GGUF 权重已经量化过，熵很高，zip/gzip 收益都只有约 1.8%，两者相差
不到 500 字节；因此只出 zip 一种。证据在 `artifacts/compress-probe.json`；
用 `--measure` 可以在自己的机器上重新量一遍。

[EN] Safety rules for this tool: a single file larger than 2 GiB is skipped with
a WARNING and never aborts the run; a missing local model prints the manual
download pointer instead of raising. No mode here downloads or uploads weights.
[ZH] 本工具的安全规则：单个文件大于 2 GiB 时输出 WARNING 并跳过，绝不中断；
本地缺少模型时打印手动下载指引，而不是抛错。任何模式都不会下载或上传模型权重。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile

import reproducible as repro

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "backend", "config.json")
TARGET = os.path.join(ROOT, "publish", "archive", "models")
PROBE = os.path.join(ROOT, "artifacts", "compress-probe")

#: [EN] Endpoint order: the mirror first (it is the default `hf_endpoint`).
#: [ZH] 端点顺序：镜像站在前（`backend/config.json` 的 `hf_endpoint` 就是它）。
ENDPOINTS = ("https://hf-mirror.com", "https://huggingface.co")
TIMEOUT = 40

#: [EN] Must match `_headers()` in `backend/model_hub.py`: mirrors reject the
#: default urllib User-Agent.
#: [ZH] **必须与 `backend/model_hub.py` 的 `_headers()` 一致**：镜像站会拒绝
#: urllib 默认 UA。
UA = {"User-Agent": "EchoDesk/0.2.0 (+local desktop assistant)", "Accept": "application/json"}

#: [EN] Local model locations, checked in this order.
#: [ZH] 本地模型文件的候选位置（按此顺序找；与 `build_release.ps1 -IncludeModels` 一致）。
LOCAL_SEARCH = ("backend/models", "models", "deploy/models")

#: [EN] Evaluation and probe folders also hold historical copies.
#: [ZH] 评测/探针目录里也常有一份（历史上大档就放在这里）。
EXTRA_SEARCH = ("artifacts/eval-models", "artifacts/probe-models")

#: [EN] A single file above 2 GiB is not worth packing: skip it with a WARNING.
#: [ZH] 单个文件超过 2 GiB 就不值得打包：输出 WARNING 并跳过。
MAX_MODEL_BYTES = 2 * 1024 * 1024 * 1024

#: [EN] Independent Release tag that hosts every offline model archive.
#: [ZH] 托管全部离线模型包的独立 Release 标签。
MODELS_RELEASE_TAG = "models-qwen-v1"

#: [EN] Exact user-facing hint required by the offline delivery policy.
#: [ZH] 离线交付策略要求的用户提示原文。
MISSING_MODEL_HINT_ZH = "请前往 GitHub Releases 的 models-qwen-v1 标签手动下载离线模型包"
MISSING_MODEL_HINT_EN = (
    "Please download the offline model package manually from the GitHub Releases "
    f"tag `{MODELS_RELEASE_TAG}`."
)

#: [EN] Q4_K_M is mandatory; Q6_K and Q8_0 are best-effort extras.
#: [ZH] Q4_K_M 必须包含；Q6_K 与 Q8_0 为尽力尝试的附加量化。
QUANT_PRIORITY = ("Q4_K_M", "Q6_K", "Q8_0")

#: [EN] Unsloth GGUF catalog used for the offline add-on. Each entry may list
#: fallback repositories because Unsloth does not publish every size as GGUF.
#: [ZH] 离线补充包使用的 Unsloth GGUF 清单。Unsloth 并未为每个尺寸都发布 GGUF，
#: 因此每个条目允许列出回退仓库。
UNSLOTH_CATALOG = (
    {
        "name": "Qwen3-0.6B",
        "repos": ("unsloth/Qwen3-0.6B-GGUF",),
    },
    {
        "name": "Qwen3-1.7B",
        "repos": ("unsloth/Qwen3-1.7B-GGUF",),
    },
    {
        # [EN] Unsloth publishes this size only as a fine-tuning checkpoint, not
        # [EN] as GGUF; the official Qwen GGUF conversion carries identical weights.
        # [ZH] Unsloth 该尺寸只发布微调检查点、未发布 GGUF；官方 Qwen 的 GGUF 转换
        # [ZH] 携带同一份权重，因此作为回退来源。
        "name": "Qwen2.5-1.5B",
        "repos": (
            "unsloth/Qwen2.5-1.5B-Instruct-GGUF",
            "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        ),
    },
    {
        "name": "Qwen2.5-Coder-1.5B",
        "repos": ("unsloth/Qwen2.5-Coder-1.5B-Instruct-GGUF",),
    },
    {
        "name": "Llama-3.2-1B",
        "repos": ("unsloth/Llama-3.2-1B-Instruct-GGUF",),
    },
    {
        "name": "SmolLM2-135M",
        "repos": ("unsloth/SmolLM2-135M-Instruct-GGUF",),
    },
    {
        "name": "SmolLM2-1.7B",
        "repos": ("unsloth/SmolLM2-1.7B-Instruct-GGUF",),
    },
    {
        "name": "gemma-3-270m",
        "repos": ("unsloth/gemma-3-270m-it-GGUF",),
    },
    {
        "name": "gemma-3-1b",
        "repos": ("unsloth/gemma-3-1b-it-GGUF",),
    },
    {
        "name": "gemma-2-2b",
        "repos": ("unsloth/gemma-2-2b-it-GGUF",),
    },
    {
        # [EN] The Unsloth Qwen3-VL repository currently publishes mmproj-F16 /
        # [EN] BF16 / F32 only; mmproj-Q8_0.gguf remains the required binding and
        # [EN] is resolved from the ggml-org fallback when available.
        # [ZH] Unsloth 的 Qwen3-VL 仓库目前只提供 mmproj-F16 / BF16 / F32；
        # [ZH] mmproj-Q8_0.gguf 仍是必须绑定的首选，缺货时从 ggml-org 回退仓库解析。
        "name": "Qwen3-VL-2B-Instruct",
        "repos": ("unsloth/Qwen3-VL-2B-Instruct-GGUF",),
        "mmproj": {
            "candidates": ("mmproj-Q8_0.gguf", "mmproj-F16.gguf", "mmproj-BF16.gguf"),
            "repos": (
                "unsloth/Qwen3-VL-2B-Instruct-GGUF",
                "ggml-org/Qwen3-VL-2B-Instruct-GGUF",
            ),
        },
    },
    {
        # [EN] Same projection-layer rule as the Instruct variant above.
        # [ZH] 投影层规则与上面的 Instruct 变体一致。
        "name": "Qwen3-VL-2B-Thinking",
        "repos": ("unsloth/Qwen3-VL-2B-Thinking-GGUF",),
        "mmproj": {
            "candidates": ("mmproj-Q8_0.gguf", "mmproj-F16.gguf", "mmproj-BF16.gguf"),
            "repos": (
                "unsloth/Qwen3-VL-2B-Thinking-GGUF",
                "ggml-org/Qwen3-VL-2B-Thinking-GGUF",
            ),
        },
    },
    {
        # [EN] Qwen3.5 is natively multimodal, but Unsloth's GGUF release still
        # [EN] publishes a standalone mmproj-F16.gguf projection layer; it is
        # [EN] therefore bound here rather than assumed to be embedded.
        # [ZH] Qwen3.5 是原生多模态，但 Unsloth 的 GGUF 发布仍单独提供
        # [ZH] mmproj-F16.gguf 投影层；因此在此显式绑定，不假设投影层已内嵌。
        "name": "Qwen3.5-0.8B",
        "repos": ("unsloth/Qwen3.5-0.8B-GGUF",),
        "mmproj": {
            "candidates": ("mmproj-F16.gguf", "mmproj-BF16.gguf"),
        },
    },
    {
        # [EN] Qwen3.5-2B follows the same projection-layer layout as 0.8B.
        # [ZH] Qwen3.5-2B 的投影层布局与 0.8B 相同。
        "name": "Qwen3.5-2B",
        "repos": ("unsloth/Qwen3.5-2B-GGUF",),
        "mmproj": {
            "candidates": ("mmproj-F16.gguf", "mmproj-BF16.gguf"),
        },
    },
)

#: [EN] Cache of Hugging Face metadata per repository to avoid repeated calls.
#: [ZH] 按仓库缓存 Hugging Face 元数据，避免重复请求。
_METADATA_CACHE: dict[str, tuple[dict, str]] = {}


def human(size: int) -> str:
    return f"{size / 1048576:.2f} MB"


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_of_stream(handle) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1 << 22), b""):
        digest.update(block)
    return digest.hexdigest()


def quant_of(filename: str) -> str:
    """[EN] Extract the quantization code from a GGUF filename.
    [ZH] 从 GGUF 文件名中提取量化代码。"""
    match = re.search(r"(Q\d+_K(?:_[SML])?|Q\d+_\d|Q\d+_K)", filename, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _quant_pattern(quant: str) -> re.Pattern[str]:
    """[EN] Match a quantization token as a whole token, so Q6_K never matches
    [EN] Q6_K_XL and Q4_K_M never matches Q4_K_S.
    [ZH] 把量化代码当作完整词元匹配，避免 Q6_K 命中 Q6_K_XL、Q4_K_M 命中 Q4_K_S。"""
    return re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(quant)}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )


def _norm(text: str) -> str:
    """[EN] Normalize a model name for order-independent containment checks.
    [ZH] 归一化模型名，用于与文件名做忽略大小写的包含判断。"""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _name_present(filename: str, name: str) -> bool:
    """[EN] Check that a model name appears in a filename as a whole token
    [EN] block. This prevents ``Qwen2.5-1.5B`` from matching a
    [EN] ``Qwen2.5-Coder-1.5B`` file.
    [ZH] 检查模型名是否以完整词块出现在文件名中，避免
    [ZH] ``Qwen2.5-1.5B`` 误命中 ``Qwen2.5-Coder-1.5B`` 文件。"""
    low = filename.lower()
    target = name.lower()
    for match in re.finditer(re.escape(target), low):
        before = low[match.start() - 1] if match.start() > 0 else ""
        after = low[match.end()] if match.end() < len(low) else ""
        if before and before not in "-_./ ":
            continue
        if after and after not in "-_./ ":
            continue
        return True
    return False


def _entry_size(entry: dict) -> int:
    """[EN] Read a sibling size from the API, preferring the LFS record.
    [ZH] 读取 API 返回的文件大小，优先使用 LFS 记录。"""
    lfs = entry.get("lfs") or {}
    return int(lfs.get("size") or entry.get("size") or 0)


def _entry_sha256(entry: dict) -> str:
    """[EN] Read the upstream LFS sha256, or an empty string when absent.
    [ZH] 读取上游 LFS sha256；缺失时返回空字符串。"""
    return str((entry.get("lfs") or {}).get("sha256") or "")


def _search_roots() -> list[str]:
    """[EN] Resolve every local model directory against the repository root.
    [ZH] 把每个本地模型目录解析为相对仓库根目录的绝对路径。"""
    roots = [os.path.join(ROOT, base.replace("/", os.sep)) for base in LOCAL_SEARCH]
    roots += [os.path.join(ROOT, extra.replace("/", os.sep)) for extra in EXTRA_SEARCH]
    return roots


def tiers() -> list[dict]:
    """从 `backend/config.json` 读模型档位 —— 不在本工具里硬编码模型名。"""
    with io.open(CONFIG, encoding="utf-8") as handle:
        config = json.load(handle)
    out: list[dict] = []
    for name, spec in (config.get("models") or {}).items():
        filename = os.path.basename(str(spec.get("path") or ""))
        if filename.lower().endswith(".gguf"):
            out.append({"tier": name, "filename": filename,
                        "repo": str(spec.get("repo") or config.get("model_repo") or "")})
    return out


def find_local(filename: str) -> str | None:
    for base in LOCAL_SEARCH + EXTRA_SEARCH:
        candidate = os.path.join(ROOT, base.replace("/", os.sep), filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def find_local_catalog(name: str, quant: str) -> str | None:
    """[EN] Locate a catalog model by fuzzy filename matching. The exact
    [EN] canonical name is tried first, then directories are scanned for a GGUF
    [EN] whose normalized stem contains the model name and whose quantization
    [EN] token matches exactly.
    [ZH] 用模糊文件名定位清单模型：先试规范文件名，再扫描目录，要求归一化后的
    [ZH] 文件名包含模型名、且量化代码按完整词元匹配。"""
    exact = find_local(f"{name}-{quant}.gguf")
    if exact:
        return exact
    pattern = _quant_pattern(quant)
    for root in _search_roots():
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for filename in names:
            lower = filename.lower()
            if not lower.endswith(".gguf") or "mmproj" in lower or "imatrix" in lower:
                continue
            if not pattern.search(filename):
                continue
            if not _name_present(os.path.splitext(filename)[0], name):
                continue
            candidate = os.path.join(root, filename)
            if os.path.isfile(candidate):
                return candidate
    return None


def find_local_mmproj(candidates: tuple[str, ...]) -> str | None:
    """[EN] Locate a projection layer by its bound candidate names.
    [ZH] 按绑定的候选名称定位投影层文件。"""
    for candidate in candidates:
        found = find_local(candidate)
        if found:
            return found
    for root in _search_roots():
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for filename in names:
            lower = filename.lower()
            if "mmproj" not in lower or not lower.endswith(".gguf"):
                continue
            for candidate in candidates:
                if lower == candidate.lower():
                    return os.path.join(root, filename)
    return None


def fetch_metadata(repo: str) -> tuple[dict, str]:
    """[EN] Fetch repository metadata from the first reachable endpoint.
    [EN] Raises LookupError with a reason instead of exiting the process.
    [ZH] 从第一个可达端点获取仓库元数据；失败时抛 LookupError 并说明原因，
    [ZH] 而不是终止整个进程。"""
    last = ""
    for endpoint in ENDPOINTS:
        try:
            request = urllib.request.Request(f"{endpoint}/api/models/{repo}?blobs=true", headers=UA)
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8")), endpoint
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                last = f"{endpoint}: HTTP {exc.code}"
            else:
                last = f"{endpoint}: {type(exc).__name__}"
    raise LookupError(f"两个端点都取不到 {repo} 的元数据（{last}）")


def metadata_for(repo: str) -> tuple[dict, str]:
    """[EN] Return cached metadata for one repository.
    [ZH] 返回单个仓库的缓存元数据。"""
    if repo not in _METADATA_CACHE:
        _METADATA_CACHE[repo] = fetch_metadata(repo)
    return _METADATA_CACHE[repo]


def select_quant_sibling(metadata: dict, quant: str) -> dict | None:
    """[EN] Select the main model file for one quantization, excluding
    [EN] projection layers and imatrix side files.
    [ZH] 为一个量化选择主模型文件，排除投影层与 imatrix 辅助文件。"""
    pattern = _quant_pattern(quant)
    for sibling in metadata.get("siblings") or []:
        filename = str(sibling.get("rfilename") or "")
        lower = filename.lower()
        if not lower.endswith(".gguf"):
            continue
        if "mmproj" in lower or "imatrix" in lower:
            continue
        if pattern.search(filename):
            return sibling
    return None


def resolve_mmproj(repos: tuple[str, ...], candidates: tuple[str, ...]) -> tuple[str, str, dict]:
    """[EN] Resolve a projection layer across all bound repositories. Exact
    [EN] candidate names win, then any Q8_0 projection, then F16, then any
    [EN] projection; this keeps mmproj-Q8_0.gguf the preferred binding while
    [EN] remaining usable when a repository ships only F16/BF16/F32.
    [ZH] 在所有绑定仓库中解析投影层：候选名称精确匹配优先，其次任意 Q8_0
    [ZH] 投影，再次 F16，最后任意投影；既保证 mmproj-Q8_0.gguf 是首选绑定，
    [ZH] 也能在仓库只提供 F16/BF16/F32 时继续工作。"""
    available: list[tuple[str, str, dict]] = []
    errors: list[str] = []
    for repo in repos:
        try:
            metadata, endpoint = metadata_for(repo)
        except LookupError as exc:
            errors.append(str(exc))
            continue
        for sibling in metadata.get("siblings") or []:
            filename = str(sibling.get("rfilename") or "")
            if "mmproj" in filename.lower() and filename.lower().endswith(".gguf"):
                available.append((repo, endpoint, sibling))
    if not available:
        raise LookupError("；".join(errors) or "未找到任何投影层文件")
    # [EN] The first candidate is the required binding; look for it across all
    # [EN] repositories, then accept any Q8_0 projection before F16 fallbacks.
    # [ZH] 第一个候选是必须绑定的目标：先跨所有仓库精确查找，再接受任意 Q8_0
    # [ZH] 投影，最后才回退到 F16。
    primary = candidates[0]
    for repo, endpoint, sibling in available:
        if os.path.basename(str(sibling.get("rfilename"))).lower() == primary.lower():
            return repo, endpoint, sibling
    for repo, endpoint, sibling in available:
        if "q8_0" in str(sibling.get("rfilename")).lower():
            return repo, endpoint, sibling
    for candidate in candidates[1:]:
        for repo, endpoint, sibling in available:
            if os.path.basename(str(sibling.get("rfilename"))).lower() == candidate.lower():
                return repo, endpoint, sibling
    for repo, endpoint, sibling in available:
        if "f16" in str(sibling.get("rfilename")).lower():
            return repo, endpoint, sibling
    return available[0]


def build_targets() -> list[dict]:
    """[EN] Build the flat list of pack targets: configured tiers first, then
    [EN] catalog quantizations, then bound projection layers. Duplicate
    [EN] (kind, model, quantization) combinations are collapsed so the same
    [EN] local file is never packed twice.
    [ZH] 构建扁平的打包目标列表：先配置档位，再清单量化，最后绑定的投影层；
    [ZH] 相同（类型、模型、量化）组合会去重，避免同一个本地文件被打包两次。"""
    targets: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(target: dict) -> None:
        key = (
            str(target["kind"]),
            _norm(str(target.get("name") or target["label"])),
            str(target.get("quant") or ""),
        )
        if key in seen:
            return
        seen.add(key)
        targets.append(target)

    for tier in tiers():
        add({
            "kind": "model",
            "source": "config",
            "label": tier["tier"],
            "name": tier["tier"],
            "filename": tier["filename"],
            "quant": quant_of(tier["filename"]),
            "repos": (tier["repo"],) if tier["repo"] else (),
        })
    for model in UNSLOTH_CATALOG:
        for quant in QUANT_PRIORITY:
            add({
                "kind": "model",
                "source": "catalog",
                "label": f"{model['name']} · {quant}",
                "name": model["name"],
                "quant": quant,
                "repos": tuple(model["repos"]),
            })
        mmproj = model.get("mmproj")
        if mmproj:
            add({
                "kind": "mmproj",
                "source": "catalog",
                "label": f"{model['name']} · mmproj",
                "name": model["name"],
                "quant": quant_of(str(mmproj["candidates"][0])),
                "candidates": tuple(mmproj["candidates"]),
                "repos": tuple(mmproj.get("repos") or model["repos"]),
            })
    return targets


def local_path_for(target: dict) -> str | None:
    """[EN] Resolve the local file for one target without any network access.
    [ZH] 不访问网络，解析单个目标的本地文件。"""
    if target["kind"] == "mmproj":
        return find_local_mmproj(target["candidates"])
    if target["source"] == "config":
        return find_local(target["filename"])
    return find_local_catalog(target["name"], target["quant"])


def remote_entry_for(target: dict) -> tuple[str, str, dict]:
    """[EN] Resolve the upstream file record for one target, trying fallback
    [EN] repositories in order. Raises LookupError when nothing matches.
    [ZH] 为单个目标解析上游文件记录，按顺序尝试回退仓库；无匹配时抛 LookupError。"""
    if target["kind"] == "mmproj":
        return resolve_mmproj(target["repos"], target["candidates"])
    last = "未配置仓库"
    for repo in target["repos"]:
        try:
            metadata, endpoint = metadata_for(repo)
        except LookupError as exc:
            last = str(exc)
            continue
        if target["source"] == "config":
            entry = next((s for s in metadata.get("siblings") or []
                          if str(s.get("rfilename")) == target["filename"]), None)
            if entry:
                return repo, endpoint, entry
            last = f"{repo} 仓库里没有 {target['filename']}"
            continue
        entry = select_quant_sibling(metadata, target["quant"])
        if entry:
            return repo, endpoint, entry
        last = f"{repo} 仓库里没有 {target['quant']} 量化"
    raise LookupError(last)


def _print_missing(label: str) -> None:
    """[EN] Print the manual-download pointer without aborting the run.
    [ZH] 打印手动下载指引，不中断脚本。"""
    print(f"  !! 本地缺少 {label}")
    print(f"     {MISSING_MODEL_HINT_ZH}")
    print(f"     {MISSING_MODEL_HINT_EN}")


def local_audit() -> int:
    """[EN] Offline inventory of local models with the 2 GiB size guard.
    [ZH] 离线清点本地模型，并执行 2 GiB 体积拦截。"""
    print(f"== 本地模型清点（不联网，单文件上限 {human(MAX_MODEL_BYTES)}）==")
    present = 0
    for target in build_targets():
        path = local_path_for(target)
        if not path:
            _print_missing(target["label"])
            continue
        size = os.path.getsize(path)
        if size > MAX_MODEL_BYTES:
            print(f"  WARNING: 跳过 {target['label']}：{human(size)} 超过 "
                  f"{human(MAX_MODEL_BYTES)} 上限")
            continue
        present += 1
        print(f"  OK  {target['label']:<28} {os.path.relpath(path, ROOT)}  {human(size)}")
    print(f"\n  本地可用 {present} 项；缺失项按上面的 models-qwen-v1 指引手动补充。")
    return 0


def print_catalog() -> int:
    """[EN] Print the configured catalog without touching the network.
    [ZH] 离线打印已配置的模型清单。"""
    print(f"== Unsloth 模型清单（{len(UNSLOTH_CATALOG)} 项）==")
    for index, model in enumerate(UNSLOTH_CATALOG, 1):
        print(f"  {index:>2}. {model['name']:<22} {' | '.join(model['repos'])}")
        print(f"      量化：{' / '.join(QUANT_PRIORITY)}"
              f"（Q4_K_M 必需，Q6_K 与 Q8_0 尽力尝试）")
        mmproj = model.get("mmproj")
        if mmproj:
            print(f"      投影层：{mmproj['candidates'][0]}"
                  f"（候选：{' / '.join(mmproj['candidates'])}）")
    print(f"\n  离线包统一托管在 Release 标签 {MODELS_RELEASE_TAG} 下；"
          f"单个文件超过 {human(MAX_MODEL_BYTES)} 将被跳过。")
    return 0


def plan() -> int:
    """[EN] Resolve upstream files and sizes without downloading any weight.
    [ZH] 只解析上游文件与体积，不下载任何权重。"""
    print(f"== 远端文件规划（不下载；单文件上限 {human(MAX_MODEL_BYTES)}）==")
    wanted = 0
    for target in build_targets():
        try:
            repo, endpoint, entry = remote_entry_for(target)
        except LookupError as exc:
            print(f"  WARNING: 跳过 {target['label']}：{exc}")
            continue
        size = _entry_size(entry)
        if size > MAX_MODEL_BYTES:
            print(f"  WARNING: 跳过 {target['label']}：{human(size)} 超过 "
                  f"{human(MAX_MODEL_BYTES)} 上限（{repo}）")
            continue
        wanted += 1
        print(f"  OK  {target['label']:<28} {size:>12} 字节  {human(size)}  "
              f"{repo} @ {endpoint}")
    print(f"\n  计划打包 {wanted} 个文件；未下载、未上传任何模型权重。")
    return 0


def verify() -> tuple[list[dict], list[str]]:
    """[EN] Verify local files against upstream size/sha256. Missing files are
    [EN] soft failures that print the manual-download pointer; only a proven
    [EN] mismatch is returned as a hard problem.
    [ZH] 用上游 size/sha256 核对本地文件。缺失属于软失败，只打印手动下载指引；
    [ZH] 只有已证实的哈希/大小不一致才作为硬问题返回。"""
    records: list[dict] = []
    problems: list[str] = []
    missing = 0
    for target in build_targets():
        print("=" * 100)
        print(f"{target['label']}  {' | '.join(target['repos']) or '（无仓库）'}")
        path = local_path_for(target)
        if not path:
            missing += 1
            _print_missing(target["label"])
            continue
        print(f"  本地：{os.path.relpath(path, ROOT)}")
        local_size = os.path.getsize(path)
        if local_size > MAX_MODEL_BYTES:
            print(f"  WARNING: 跳过：{human(local_size)} 超过 "
                  f"{human(MAX_MODEL_BYTES)} 上限")
            continue
        try:
            repo, endpoint, entry = remote_entry_for(target)
        except LookupError as exc:
            # [EN] An unreachable or incomplete upstream must not abort packing.
            # [ZH] 上游不可达或记录不完整时不得中断打包。
            print(f"  WARNING: 无法核对远端记录：{exc}")
            continue
        remote_name = str(entry.get("rfilename") or "")
        remote_size = _entry_size(entry)
        remote_sha = _entry_sha256(entry)
        if remote_size > MAX_MODEL_BYTES:
            print(f"  WARNING: 跳过：上游 {remote_name} 为 {human(remote_size)}，"
                  f"超过 {human(MAX_MODEL_BYTES)} 上限")
            continue
        print(f"  远端：{remote_name}  {remote_size} 字节  "
              f"sha256={remote_sha or '（无 LFS 字段）'}（{endpoint}）")
        local_sha = sha256_of(path)
        print(f"  本地：{local_size} 字节  sha256={local_sha}")
        ok = (remote_size == local_size) and bool(remote_sha) and (remote_sha == local_sha)
        if not ok:
            problems.append(f"{target['label']} 与 HF 记录不符（size 或 sha256）")
        print(f"  核对：{'一致 ✅' if ok else '**不一致 ❌**'}")
        if ok:
            records.append({
                **target,
                "repo": repo,
                "filename": remote_name,
                "path": os.path.relpath(path, ROOT).replace("\\", "/"),
                "local_size": local_size,
                "local_sha256": local_sha,
                "remote_size": remote_size,
                "remote_sha256": remote_sha,
                "endpoint": endpoint,
                "verified": True,
            })
    if missing:
        print("\n" + "!" * 100)
        print(f"{missing} 个文件本地缺失。{MISSING_MODEL_HINT_ZH}")
        print(MISSING_MODEL_HINT_EN)
    return records, problems


def measure(records: list[dict]) -> None:
    """重新量一遍 zip / tar.gz 的实际收益（决定格式用）。"""
    # [EN] Measuring nothing would divide by zero; report and return instead.
    # [ZH] 没有可测量记录时计算百分比会除零；先提示并返回。
    if not records:
        print("  没有可测量的已核对模型。")
        return
    os.makedirs(PROBE, exist_ok=True)
    results = []
    for record in records:
        source = os.path.join(ROOT, record["path"])
        raw = record["local_size"]
        stem = os.path.splitext(record["filename"])[0]
        zip_path = os.path.join(PROBE, f"{stem}.zip")
        tgz_path = os.path.join(PROBE, f"{stem}.tar.gz")
        started = time.time()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(source, record["filename"])
        zip_size, zip_sec = os.path.getsize(zip_path), time.time() - started
        started = time.time()
        with tarfile.open(tgz_path, "w:gz", compresslevel=6) as tf:
            info = tf.gettarinfo(source, arcname=record["filename"])
            with open(source, "rb") as handle:
                tf.addfile(info, handle)
        tgz_size, tgz_sec = os.path.getsize(tgz_path), time.time() - started
        print(f"  {record['filename']}")
        print(f"    zip    {human(zip_size)}  省 {(raw - zip_size) / raw * 100:.3f}%  {zip_sec:.1f}s")
        print(f"    tar.gz {human(tgz_size)}  省 {(raw - tgz_size) / raw * 100:.3f}%  {tgz_sec:.1f}s")
        results.append({"model": record["filename"], "raw_bytes": raw, "zip_bytes": zip_size,
                        "zip_saved_percent": round((raw - zip_size) / raw * 100, 3),
                        "targz_bytes": tgz_size,
                        "targz_saved_percent": round((raw - tgz_size) / raw * 100, 3)})
    total_raw = sum(r["raw_bytes"] for r in results)
    total_zip = sum(r["zip_bytes"] for r in results)
    total_tgz = sum(r["targz_bytes"] for r in results)
    print(f"\n  合计：原始 {human(total_raw)}｜zip {human(total_zip)}"
          f"（省 {(total_raw - total_zip) / total_raw * 100:.3f}%）"
          f"｜tar.gz {human(total_tgz)}（省 {(total_raw - total_tgz) / total_raw * 100:.3f}%）")
    print(f"  两种都出要多占 {human(total_tgz)}")
    out = os.path.join(ROOT, "artifacts", "compress-probe.json")
    with io.open(out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2)
    print(f"  实测数据：{os.path.relpath(out, ROOT)}")
    shutil.rmtree(PROBE, ignore_errors=True)


def pack(records: list[dict]) -> int:
    if not records:
        print("\n  没有可打包的本地模型。")
        print(f"  {MISSING_MODEL_HINT_ZH}")
        print(f"  {MISSING_MODEL_HINT_EN}")
        return 0
    os.makedirs(TARGET, exist_ok=True)
    entries: list[dict] = []
    for record in records:
        source = os.path.join(ROOT, record["path"])
        name = str(record.get("filename") or os.path.basename(record["path"]))
        local_size = int(record["local_size"])
        if local_size > MAX_MODEL_BYTES:
            print(f"  WARNING: 跳过 {name}：{human(local_size)} 超过 "
                  f"{human(MAX_MODEL_BYTES)} 上限")
            continue
        package = f"EchoDesk-models-{os.path.splitext(name)[0]}.zip"
        target = os.path.join(TARGET, package)
        print(f"  打包 {package}（{human(local_size)}）…")
        started = time.time()
        # [EN] Deterministic member timestamp and fixed permission bits; the model file is
        # [EN] stored at the archive root so users can extract it directly into models/.
        # [ZH] 固定成员时间戳与权限位；模型文件位于归档根部，用户可直接解压进 models/。
        repro.write_zip_files(target, [(source, name)])
        seconds = time.time() - started
        zip_size = os.path.getsize(target)
        with zipfile.ZipFile(target) as zf:
            bad = zf.testzip()
            names = zf.namelist()
            extracted = sha256_of_stream(zf.open(name)) if not bad else ""
        if bad:
            raise SystemExit(f"{package} 成员 {bad} 损坏")
        if names != [name]:
            raise SystemExit(f"{package} 成员列表异常：{names}")
        # **关键校验**：从包里读回来的哈希必须等于已与 HF 核对过的模型哈希 ——
        # 只校验 zip 自身的哈希证明不了"包里装的就是那个模型"。
        if extracted != record["local_sha256"]:
            raise SystemExit(f"{package} 读回的 sha256 与模型不一致")
        print(f"    ✓ {human(zip_size)}（省 {(local_size - zip_size) / local_size * 100:.3f}%）"
              f"  {seconds:.1f}s  成员读回校验通过")
        entries.append({
            "package": package,
            "tier": record["label"],
            "contains": name,
            "quantization": record.get("quant") or quant_of(name),
            "source_repo": record["repo"],
            "source_path_in_repo": record["filename"],
            "model_bytes": local_size,
            "model_sha256": record["local_sha256"],
            "package_bytes": zip_size,
            "package_sha256": sha256_of(target),
            "package_format": "zip（deflate）",
            "compression_saved_percent": round(
                (local_size - zip_size) / local_size * 100, 3),
            "hf_lfs_sha256": record["remote_sha256"],
            "hf_verified": True,
            "packed_at": repro.utc_iso(),
        })

    if not entries:
        print("\n  没有可打包的本地模型（全部被体积拦截或缺失）。")
        print(f"  {MISSING_MODEL_HINT_ZH}")
        return 0

    with io.open(os.path.join(TARGET, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(f"{entry['package_sha256']}  {entry['package']}\n")

    manifest = {
        "what": "EchoDesk 模型补充包（离线用户用）",
        "why": "发布物一律不含模型（首次启动询问是否下载）；离线环境用本补充包。",
        "release_tag": MODELS_RELEASE_TAG,
        "size_limit_bytes": MAX_MODEL_BYTES,
        "where_to_put": {"windows": "发布目录\\app\\backend\\models\\",
                         "macos_linux": "发布目录/app/backend/models/",
                         "source_checkout": "backend/models/"},
        "format_decision": {
            "chosen": "zip（deflate）",
            "reason": "实测 zip/gzip 收益都只有约 1.8%，两者相差不到 500 字节；"
                      "两种都出要多占约 1.5 GB 而只换来格式习惯，zip 三平台都能解",
            "evidence": "artifacts/compress-probe.json（用 --measure 可重测）",
        },
        "verification": {
            "model_sha256": "与 HuggingFace 仓库 API 的 LFS sha256 逐项核对一致"
                            "（backend/model_hub.py 的 verify_sha256 用的就是同一字段）",
            "package_sha256": "打包后从 zip 内读回成员重算，必须等于已核对的模型 sha256",
            "checked_at": repro.utc_iso(),
        },
        "packages": entries,
    }
    with io.open(os.path.join(TARGET, "manifest.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    print(f"\n  目录：{os.path.relpath(TARGET, ROOT)}")
    for entry in entries:
        print(f"    {entry['package']}  {human(entry['package_bytes'])}  "
              f"{entry['package_sha256'][:16]}…")
    print("    SHA256SUMS.txt / manifest.json")
    print(f"  合计 {human(sum(e['package_bytes'] for e in entries))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="核对模型来源并打模型补充包")
    parser.add_argument("--verify-only", action="store_true", help="只核对（默认）")
    parser.add_argument("--pack", action="store_true", help="核对后打包到 publish/archive/models/")
    parser.add_argument("--measure", action="store_true", help="重新量 zip/tar.gz 的实际压缩率")
    parser.add_argument("--catalog", action="store_true", help="离线打印 Unsloth 模型清单")
    parser.add_argument("--local-only", action="store_true", help="只清点本地文件，不联网")
    parser.add_argument("--plan", action="store_true", help="只解析远端文件与体积，不下载")
    parser.add_argument("--json", default="", help="把核对结果写成 JSON")
    args = parser.parse_args()

    if args.catalog:
        return print_catalog()
    if args.local_only:
        return local_audit()
    if args.plan:
        return plan()

    print("== 核对本地模型与 HuggingFace 记录 ==")
    records, problems = verify()
    if problems:
        print("\n** 以下文件与上游记录不一致，已跳过打包：**")
        for problem in problems:
            print(f"  - {problem}")
        print("  请删除本地文件后重新从官方仓库获取，再运行本工具。")
        return 1
    if args.json:
        with io.open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
        print(f"核对结果：{os.path.relpath(args.json, ROOT)}")
    if args.measure:
        print("\n== 实测压缩收益 ==")
        measure(records)
    if args.pack:
        print("\n== 打包 ==")
        return pack(records)
    if not args.measure:
        print("\n（只核对；加 --pack 才会打包）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
