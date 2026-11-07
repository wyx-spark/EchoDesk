#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打「模型补充包」并核对模型来源（离线用户用）。

**为什么需要这个工具**：安装程序与三平台发布包都**不含模型**（首次启动询问是否下载），
离线环境靠"模型补充包"解决 —— 发布说明模板里就写着这一条。而打这个包要做四件事，
每件都容易漏：找模型、**核对它确实是官方那一份**、选格式、写校验清单。
一次性脚本做一次就散了，下次换模型档位又要重来。

两条命令：

    python tools/pack_model_addon.py --verify-only   # 只核对本地模型（默认）
    python tools/pack_model_addon.py --pack          # 核对后打包

**核对用什么是"官方那一份"**：仓库里**没有**记录模型文件的 sha256（`git grep` 确认过），
但 `backend/model_hub.py` 下载时自带 `verify_sha256`，比对的是 HuggingFace 仓库 API 返回的
LFS `sha256` 字段 —— 也就是说**项目自己认定的正确哈希就是这个值**。本工具用同一个端点、
同一个 UA（镜像站会拒绝 urllib 的默认 UA，实测：curl 200 / urllib HTTPError）。

**格式与依据**（实测得出，证据在 `artifacts/compress-probe.json`）：GGUF 的权重已经是量化过的，
熵很高，zip/gzip 收益都只有约 1.8%，且两者相差不到 500 字节；因此**只出 zip 一种** ——
zip 三平台都能解，再出一份 tar.gz 要多占约 1.5 GB 而只换来"格式习惯"。
用 `--measure` 可以在自己的机器上重新量一遍再决定。

产出（`publish/archive/models/`，**独立目录**，不放进任何版本目录）：

    EchoDesk-models-<模型名>.zip    模型文件放在 zip 根部（可直接解压进 models 目录）
    SHA256SUMS.txt                  包的 sha256
    manifest.json                   每个包：模型名/量化/来源仓库/大小/sha256/打包时间

**不上传**：本工具只在本机留存；是否挂到 GitHub Release 由维护者决定。
"""
from __future__ import annotations

import argparse
import gzip
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

#: 端点顺序：镜像站在前（`backend/config.json` 的 `hf_endpoint` 就是它，国内可达）
ENDPOINTS = ("https://hf-mirror.com", "https://huggingface.co")
TIMEOUT = 40

#: **必须与 `backend/model_hub.py` 的 `_headers()` 一致**：镜像站会拒绝 urllib 默认 UA。
UA = {"User-Agent": "EchoDesk/0.2.0 (+local desktop assistant)", "Accept": "application/json"}

#: 本地模型文件的候选位置（按此顺序找；与 `build_release.ps1 -IncludeModels` 的来源一致）
LOCAL_SEARCH = ("backend/models", "models", "deploy/models")


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
    for base in LOCAL_SEARCH:
        candidate = os.path.join(ROOT, base.replace("/", os.sep), filename)
        if os.path.isfile(candidate):
            return candidate
    # 评测/探针目录里也常有一份（历史上大档就放在这里）
    for extra in ("artifacts/eval-models", "artifacts/probe-models"):
        candidate = os.path.join(ROOT, extra.replace("/", os.sep), filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def fetch_metadata(repo: str) -> tuple[dict, str]:
    last = ""
    for endpoint in ENDPOINTS:
        try:
            request = urllib.request.Request(f"{endpoint}/api/models/{repo}?blobs=true", headers=UA)
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8")), endpoint
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = f"{endpoint}: {'HTTP ' + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__}"
            print(f"    {last} 不可用")
    raise SystemExit(f"两个端点都取不到 {repo} 的元数据：{last}")


def verify() -> list[dict]:
    """逐个档位核对本地模型与 HF 记录的 size/sha256。"""
    records: list[dict] = []
    problems: list[str] = []
    for tier in tiers():
        print("=" * 100)
        print(f"{tier['tier']}  {tier['repo']}/{tier['filename']}")
        path = find_local(tier["filename"])
        if not path:
            problems.append(f"{tier['tier']} 本地找不到 {tier['filename']}")
            print(f"  !! 本地找不到（找过 {'、'.join(LOCAL_SEARCH)}）")
            continue
        print(f"  本地：{os.path.relpath(path, ROOT)}")
        metadata, endpoint = fetch_metadata(tier["repo"])
        entry = next((s for s in metadata.get("siblings", [])
                      if s.get("rfilename") == tier["filename"]), None)
        if not entry:
            problems.append(f"{tier['tier']} 仓库里没有 {tier['filename']}")
            print(f"  !! {endpoint} 上该仓库没有这个文件")
            continue
        remote_size = entry.get("size")
        remote_sha = (entry.get("lfs") or {}).get("sha256") or ""
        local_size = os.path.getsize(path)
        print(f"  远端：{remote_size} 字节  sha256={remote_sha or '（无 LFS 字段）'}（{endpoint}）")
        local_sha = sha256_of(path)
        print(f"  本地：{local_size} 字节  sha256={local_sha}")
        ok = (remote_size == local_size) and bool(remote_sha) and (remote_sha == local_sha)
        if not ok:
            problems.append(f"{tier['tier']} 与 HF 记录不符（size 或 sha256）")
        print(f"  核对：{'一致 ✅' if ok else '**不一致 ❌**'}")
        records.append({**tier, "path": os.path.relpath(path, ROOT).replace("\\", "/"),
                        "local_size": local_size, "local_sha256": local_sha,
                        "remote_size": remote_size, "remote_sha256": remote_sha,
                        "endpoint": endpoint, "verified": ok})
    if problems:
        raise SystemExit("模型核对未通过：\n  - " + "\n  - ".join(problems))
    return records


def measure(records: list[dict]) -> None:
    """重新量一遍 zip / tar.gz 的实际收益（决定格式用）。"""
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
    os.makedirs(TARGET, exist_ok=True)
    entries: list[dict] = []
    for record in records:
        source = os.path.join(ROOT, record["path"])
        name = record["filename"]
        package = f"EchoDesk-models-{os.path.splitext(name)[0]}.zip"
        target = os.path.join(TARGET, package)
        print(f"  打包 {package}（{human(record['local_size'])}）…")
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
        quant = re.sub(r"^Qwen3-[\d.]+B-", "", os.path.splitext(name)[0])
        print(f"    ✓ {human(zip_size)}（省 {(record['local_size'] - zip_size) / record['local_size'] * 100:.3f}%）"
              f"  {seconds:.1f}s  成员读回校验通过")
        entries.append({
            "package": package,
            "tier": record["tier"],
            "contains": name,
            "quantization": quant,
            "source_repo": record["repo"],
            "source_path_in_repo": record["path"],
            "model_bytes": record["local_size"],
            "model_sha256": record["local_sha256"],
            "package_bytes": zip_size,
            "package_sha256": sha256_of(target),
            "package_format": "zip（deflate）",
            "compression_saved_percent": round(
                (record["local_size"] - zip_size) / record["local_size"] * 100, 3),
            "hf_lfs_sha256": record["remote_sha256"],
            "hf_verified": True,
            "packed_at": repro.utc_iso(),
        })

    with io.open(os.path.join(TARGET, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(f"{entry['package_sha256']}  {entry['package']}\n")

    manifest = {
        "what": "EchoDesk 模型补充包（离线用户用）",
        "why": "发布物一律不含模型（首次启动询问是否下载）；离线环境用本补充包。",
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
    print(f"    SHA256SUMS.txt / manifest.json")
    print(f"  合计 {human(sum(e['package_bytes'] for e in entries))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="核对模型来源并打模型补充包")
    parser.add_argument("--verify-only", action="store_true", help="只核对（默认）")
    parser.add_argument("--pack", action="store_true", help="核对后打包到 publish/archive/models/")
    parser.add_argument("--measure", action="store_true", help="重新量 zip/tar.gz 的实际压缩率")
    parser.add_argument("--json", default="", help="把核对结果写成 JSON")
    args = parser.parse_args()

    print("== 核对本地模型与 HuggingFace 记录 ==")
    records = verify()
    if args.json:
        with io.open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
        print(f"核对结果：{os.path.relpath(args.json, ROOT)}")
    if args.measure:
        print("\n== 实测压缩收益 ==")
        measure(records)
    if args.pack:
        print("\n== 打包 ==")
        pack(records)
    elif not args.measure:
        print("\n（只核对；加 --pack 才会打包）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
