#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 PyPI 取一个**指定平台**的 wheel，并校验它到底是不是好的。

为什么需要这个脚本（而不是直接 `pip download`）：

1. **弱网 + 无续传**：本机到 GitHub / PyPI CDN 的链路实测 40–70 KB/s 且会中断，
   `pip download` 一旦断了就是整轮重来。这里统一走 curl `-C -` 续传，
   与 `tools/pack_backend.py` 的下载策略保持一致。
2. **校验"上游给的文件本身是坏的"**：实测 `llama_cpp_python-0.3.36` 的 macOS arm64
   wheel 在 PyPI 上就是损坏的（103 个成员里 46 个 `.dylib` 解压报
   `zlib.error: invalid block type`），而**字节数与官方 digest 都对得上** ——
   也就是说"下完了"不等于"能装"。`--verify` 会把每个成员都解压一遍，
   在打包前就把这种问题挡住，而不是等 pip install 报错。
3. **记录可复现的取件方式**：取件结果与 sha256 会打印出来，可以直接写进
   `artifacts/backend-cache/` 的说明与记录文档。

用法：

    python tools/fetch_wheel.py llama-cpp-python==0.3.35 --platform macosx_11_0_arm64 --verify
    python tools/fetch_wheel.py playwright==1.63.0 --platform macosx_11_0_arm64
    python tools/fetch_wheel.py pydantic --platform manylinux2014_x86_64 --dest artifacts/wheels
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEST = os.path.join(ROOT, "artifacts", "wheels")
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
PYPI_JSON_LATEST = "https://pypi.org/pypi/{name}/json"
PY_MINOR = f"{sys.version_info.major}.{sys.version_info.minor}"
#: 目标解释器标签，例如 cp313（与 tools/pack_backend.py 的 --python-version 保持一致）。
PY_TAG = f"cp{PY_MINOR.replace('.', '')}"
#: 可接受的 Python 标签：cp313（编译扩展）与 py3 / py2.py3（纯 Python）。
ABI_TAGS = (PY_TAG, "py3", "py2.py3")


def log(message: str) -> None:
    print(f"[wheel] {message}", flush=True)


def http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "EchoDesk-fetch-wheel"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_spec(spec: str) -> tuple[str, str]:
    """`name==version` → (name, version)；只给名字时返回空版本（取最新）。"""
    if "==" in spec:
        name, version = spec.split("==", 1)
        return name.strip(), version.strip()
    return spec.strip(), ""


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pick_candidate(candidates: list[dict], platform_tag: str, source: str) -> dict:
    """从候选里挑出「目标平台 + 目标 Python ABI」的那一个 wheel。

    候选统一成 `{"filename", "url", "sha256"}` —— PyPI 与 simple 索引两条来源都归一到这个形状，
    筛选逻辑只写一遍。
    """
    matched: list[dict] = []
    for item in candidates:
        name = item["filename"]
        if not name.endswith(".whl"):
            continue
        stem = name[: -len(".whl")]
        parts = stem.split("-")
        if len(parts) < 5:
            continue
        py_tag, abi_tag, plat_tag = parts[-3], parts[-2], parts[-1]
        if platform_tag not in plat_tag.split("."):
            continue
        if not any(tag in py_tag.split(".") for tag in ABI_TAGS):
            continue
        # 编译扩展必须匹配本项目的 cp313；纯 Python（abi=none）才允许 py3。
        if abi_tag != "none" and PY_TAG not in py_tag.split("."):
            continue
        matched.append(item)
    if not matched:
        available = sorted({i["filename"] for i in candidates if i["filename"].endswith(".whl")})
        raise SystemExit(
            f"没找到 platform={platform_tag} 且 Python={PY_MINOR} 的 wheel（来源：{source}）。\n"
            "  该来源可用的 wheel 有：\n    " + "\n    ".join(available or ["（一个都没有）"])
        )
    # 纯 Python wheel 优先（ABI 与平台无关，最省心），其次取文件名最短的。
    matched.sort(key=lambda i: (0 if "-none-" in i["filename"] else 1, len(i["filename"])))
    return matched[0]


def from_pypi(name: str, version: str, platform_tag: str) -> dict:
    """从 PyPI 的 JSON API 取候选。**注意：PyPI 上很多包只有源码包**（实测
    `llama-cpp-python` 每个版本只有 `.tar.gz`，全部 wheel 都托管在作者自己的
    GitHub Releases 索引里）—— 这种情况下必须用 `--index`。"""
    url = PYPI_JSON.format(name=name, version=version) if version else PYPI_JSON_LATEST.format(name=name)
    source = f"PyPI {name}{'==' + version if version else '（最新）'}"
    log(f"查询 {source}")
    payload = http_json(url)
    if not version:
        version = payload["info"]["version"]
        log(f"  最新版本 {version}")
    files = payload.get("urls") or []
    candidates = [
        {"filename": item["filename"], "url": item["url"], "sha256": (item.get("digests") or {}).get("sha256", "")}
        for item in files
    ]
    return pick_candidate(candidates, platform_tag, source)


def from_index(index_url: str, name: str, platform_tag: str, version: str = "") -> dict:
    """从 PEP 503 simple 索引取候选（例如 llama-cpp-python 官方的 wheel 索引）。

    索引页里的链接可能是相对路径，也可能是**绝对 URL**（abetlen 的索引写的就是
    `https://github.com/.../releases/download/v0.3.35/xxx.whl`），两种都要认。

    simple 索引不像 PyPI JSON 那样能按版本查询，所以版本靠**文件名里那一段**筛
    （`包名-版本-...`）。给了版本就只在该版本的文件里挑，避免"要 0.3.35 却拿到 0.3.36"。
    """
    root = index_url if index_url.endswith("/") else index_url + "/"
    page = root + name.replace("_", "-") + "/"
    source = f"索引 {page}"
    log(f"查询 {source}")
    html = http_text(page)
    candidates: list[dict] = []
    for link, fragment in re.findall(r'href="([^"#]+)(?:#([^"]*))?"', html):
        filename = link.rstrip("/").split("/")[-1]
        if not filename.endswith(".whl"):
            continue
        if version and f"-{version}-" not in filename:
            continue
        if link.startswith("http://") or link.startswith("https://"):
            url = link
        else:
            url = urllib.parse.urljoin(page, link)
        digest = ""
        match = re.search(r"sha256=([0-9a-f]{64})", fragment or "")
        if match:
            digest = match.group(1)
        candidates.append({"filename": filename, "url": url, "sha256": digest})
    if version and not candidates:
        raise SystemExit(
            f"索引 {page} 里没有 {name}=={version} 的任何 wheel。\n"
            "  说明这个版本没有发布 wheel（实测 llama-cpp-python 的 0.3.31 就没有 macOS arm64 的）。"
        )
    return pick_candidate(candidates, platform_tag, source)


def http_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "EchoDesk-fetch-wheel"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def download(url: str, dest: str) -> None:
    """curl 续传下载；下完才改名，所以"文件存在即完整"。"""
    if os.path.isfile(dest):
        log(f"已存在：{os.path.relpath(dest, ROOT)}（{os.path.getsize(dest) / 1048576:.1f} MB）")
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    curl = shutil.which("curl")
    if not curl:
        raise SystemExit("需要 curl（Windows 10+ 自带）来支持断点续传。")
    if os.path.isfile(part):
        log(f"  发现片段 {os.path.getsize(part) / 1048576:.1f} MB，续传")
    log(f"下载：{url}")
    completed = subprocess.run(
        [curl, "-L", "--fail", "--retry", "8", "--retry-delay", "3", "--retry-all-errors",
         "--progress-bar", "-C", "-", "-o", part, url],
        cwd=ROOT,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"下载失败（curl 退出码 {completed.returncode}）。\n"
            f"  片段保留在 {os.path.relpath(part, ROOT)}，重跑本脚本会自动续传。"
        )
    os.replace(part, dest)


def verify_wheel(path: str) -> tuple[int, int, list[str]]:
    """把 wheel 里每个成员都读一遍，返回 (成员数, 坏成员数, 坏成员样例)。

    `zipfile.testzip()` 只做 CRC 校验，对"压缩流本身坏了"的情况不够敏感，
    这里逐个 `read()` 才能真正触发 zlib 解压错误。
    """
    bad: list[str] = []
    total = 0
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            total += 1
            try:
                with zf.open(info) as handle:
                    while handle.read(1024 * 1024):
                        pass
            except Exception as error:  # noqa: BLE001 - 任何解压异常都算坏
                bad.append(f"{info.filename}（{type(error).__name__}: {error}）")
    return total, len(bad), bad[:5]


def quarantine(path: str) -> str:
    """把校验失败的 wheel 挪进 `corrupt/` 子目录留证，而不是直接删掉。

    留证很重要：这些坏包的**字节数与上游 digest 完全一致**，也就是"看起来完全正常、
    装上才炸"。保留它们（连同记录里的 sha256）才能向别人证明"不是我们下载坏了"。
    """
    target_dir = os.path.join(os.path.dirname(path), "corrupt")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(path))
    if os.path.isfile(target):
        os.remove(target)
    shutil.move(path, target)
    return target


def fetch_one(args: argparse.Namespace, name: str, version: str) -> tuple[int, str]:
    """取一个 wheel 并可选校验。返回 (退出码, 保存路径)。

    退出码：0 成功；2 下到了但**上游文件本身是坏的**；其它为真失败。
    """
    if args.index:
        wheel = from_index(args.index, name, args.platform, version)
    else:
        wheel = from_pypi(name, version, args.platform)
    filename = wheel["filename"]
    log(f"选中：{filename}")

    dest = os.path.join(args.dest if os.path.isabs(args.dest) else os.path.join(ROOT, args.dest), filename)
    download(wheel["url"], dest)

    actual = sha256_of(dest)
    official = wheel.get("sha256", "")
    log(f"  sha256 {actual}")
    if official:
        log(f"  官方   {official}　{'一致' if official == actual else '**不一致**'}")
        if official != actual:
            raise SystemExit("sha256 与索引记录不一致：下载被篡改或损坏，请删掉文件重下。")
    else:
        log("  索引未提供 sha256（simple 索引通常会带 #sha256= 片段，这个源没带）")
    if args.expect_sha256 and args.expect_sha256 != actual:
        raise SystemExit(f"sha256 与 --expect-sha256 不符（期望 {args.expect_sha256}）。")

    if args.verify:
        total, bad_count, samples = verify_wheel(dest)
        if bad_count:
            log(f"**完整性校验失败**：{total} 个成员里 {bad_count} 个损坏")
            for item in samples:
                log(f"    · {item}")
            moved = quarantine(dest)
            log(f"  已留证到 {os.path.relpath(moved, ROOT)}")
            log("  字节数与上游 digest 都对得上，说明**上游发布的文件本身就是坏的** —— 换版本或换来源。")
            return 2, dest
        log(f"完整性校验通过：{total} 个成员全部可解压")

    log(f"完成：{os.path.relpath(dest, ROOT)}")
    return 0, dest


def probe_versions(args: argparse.Namespace, name: str, versions: list[str]) -> int:
    """按给定顺序（通常从新到旧）逐个版本试，取**第一个通过完整性校验**的。

    实测动机：llama-cpp-python 的 macOS arm64 wheel 在 0.3.35 与 0.3.36 上**都是坏的**
    （同一个 `lib/*.dylib` 批量损坏），而这类"上游某个平台的构建坏了"的问题，
    逐个版本手工试很费时间 —— 让脚本从新到旧自动退版本，一次跑完。
    """
    log(f"探测 {name} 在 {args.platform} 上的可用版本：{' → '.join(versions)}")
    for version in versions:
        log("")
        log(f"----- 试 {name}=={version} -----")
        try:
            code, _ = fetch_one(args, name, version)
        except SystemExit as error:
            log(f"  取件失败，继续下一个：{error}")
            continue
        if code == 0:
            log("")
            log(f"**选定 {name}=={version}**（完整性校验通过）")
            return 0
    log("")
    log(f"全部候选版本在这个平台上都不可用（{len(versions)} 个）。")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="取指定平台的 wheel 并校验完整性")
    parser.add_argument("spec", help="包名或 包名==版本，例如 playwright==1.63.0")
    parser.add_argument("--platform", required=True,
                        help="目标平台标签，例如 macosx_11_0_arm64 / manylinux2014_x86_64 / win_amd64")
    parser.add_argument("--dest", default=DEFAULT_DEST, help="保存目录（默认 artifacts/wheels）")
    parser.add_argument("--verify", action="store_true",
                        help="逐成员解压校验（较慢，但能查出「下完却不能装」的上游坏包）")
    parser.add_argument("--expect-sha256", default="", help="期望的 sha256；不符即失败")
    parser.add_argument("--index", default="",
                        help="改用 PEP 503 simple 索引取件（例如 llama-cpp-python 官方的 "
                             "https://abetlen.github.io/llama-cpp-python/whl/cpu）；PyPI 上没有 wheel 的包必须用它")
    parser.add_argument("--probe-versions", default="",
                        help="逗号分隔的版本列表，从新到旧逐个试，取第一个完整性校验通过的"
                             "（隐含 --verify）。用于「上游某个平台的构建坏了」这种情况")
    args = parser.parse_args()

    name, version = parse_spec(args.spec)
    if args.probe_versions:
        args.verify = True
        versions = [v.strip() for v in args.probe_versions.split(",") if v.strip()]
        return probe_versions(args, name, versions)
    code, _ = fetch_one(args, name, version)
    return code


if __name__ == "__main__":
    sys.exit(main())
