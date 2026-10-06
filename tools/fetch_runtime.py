#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预热 `tools/pack_backend.py` 需要的 standalone Python 运行时缓存（弱网下的第一步）。

为什么单独拆一个脚本：本机链路实测只有 40–70 KB/s 且会中断，一个 70 MB 的解释器
要十几分钟。打包和下载**必须能分开跑** —— 否则打包脚本一失败（比如某个 wheel 上游
就是坏的），已经下好的运行时缓存也跟着被误判为"没进展"。这个脚本只做一件事：
把要用的运行时归档下到 `artifacts/backend-cache/`，可反复重跑，已缓存则秒退。

用法：

    python tools/fetch_runtime.py                # 三平台全下
    python tools/fetch_runtime.py --platform osx-arm64
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pack_backend as pb  # noqa: E402 - 必须先把 tools/ 放进 sys.path


def main() -> int:
    parser = argparse.ArgumentParser(description="预热 standalone Python 运行时缓存")
    parser.add_argument("--platform", action="append", default=[],
                        help="目标 RID（可重复），默认三平台全部")
    parser.add_argument("--keep-symbols", action="store_true",
                        help="取未剥离调试符号的归档（默认取 install_only_stripped，与打包脚本一致）")
    args = parser.parse_args()

    rids = args.platform or list(pb.PLATFORMS)
    unknown = [rid for rid in rids if rid not in pb.PLATFORMS]
    if unknown:
        raise SystemExit(f"未知平台：{'、'.join(unknown)}（可用：{'、'.join(pb.PLATFORMS)}）")

    version = pb.py_minor()
    pb.log(f"查询 python-build-standalone Release（目标 CPython {version}）…")
    release = pb.fetch_release_info()
    pb.log(f"  release {release.get('tag_name')}（{len(release.get('assets', []))} 个资产）")

    failed: list[str] = []
    for rid in rids:
        spec_target = pb.PLATFORMS[rid]
        asset = pb.pick_asset(release, spec_target["triple"], version, keep_symbols=args.keep_symbols)
        dest = os.path.join(pb.CACHE_ROOT, asset["name"])
        pb.log("")
        pb.log(f"===== {rid}：{asset['name']}（{asset['size'] / 1048576:.1f} MB）=====")
        try:
            pb.download(asset["browser_download_url"], dest)
        except SystemExit as error:
            # 单个平台失败不该中断其它平台 —— 弱网下"三个里成一个"也是净进展。
            pb.log(f"**失败**：{error}")
            failed.append(rid)
    if failed:
        pb.log(f"以下平台未取到运行时（其余成功）：{'、'.join(failed)}")
        return 1
    pb.log("运行时缓存已就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
