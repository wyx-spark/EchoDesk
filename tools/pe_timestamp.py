#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Normalise the PE ``TimeDateStamp`` field of a Windows executable.

[EN]
Some Windows build tools still write the wall-clock build time into the PE optional header,
which makes otherwise identical outputs differ between two builds. The field is a pure
metadata value: it is not covered by the appended installation payload, and neither the
Windows loader nor Inno Setup consults it for integrity. Rewriting the four bytes in place
therefore removes the variable without rebuilding the image or discarding appended data.

[ZH]
部分 Windows 构建工具仍会把墙上时钟写入 PE 可选头中的 ``TimeDateStamp`` 字段，使两次
构建的其余内容完全相同、哈希却不同。该字段是纯元数据：它不覆盖追加的安装数据，Windows
加载器与 Inno Setup 也不会用它做完整性校验。因此原地覆写这四个字节即可消除可变项，无需
重建映像，也不会丢弃追加数据。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

from reproducible import source_date_epoch


def read_pe_timestamp(path: str) -> int:
    with open(path, "rb") as handle:
        if handle.read(2) != b"MZ":
            raise SystemExit(f"[EN] Not a PE image: {path}\n[ZH] 不是 PE 映像：{path}")
        handle.seek(0x3C)
        pe_offset = struct.unpack("<I", handle.read(4))[0]
        handle.seek(pe_offset)
        if handle.read(4) != b"PE\0\0":
            raise SystemExit(f"[EN] Missing PE signature: {path}\n[ZH] 缺少 PE 签名：{path}")
        handle.seek(pe_offset + 8)
        return struct.unpack("<I", handle.read(4))[0]


def set_pe_timestamp(path: str, epoch: int | None = None) -> tuple[int, int]:
    stamp = source_date_epoch() if epoch is None else epoch
    before = read_pe_timestamp(path)
    with open(path, "r+b") as handle:
        handle.seek(0x3C)
        pe_offset = struct.unpack("<I", handle.read(4))[0]
        handle.seek(pe_offset + 8)
        handle.write(struct.pack("<I", stamp))
    return before, stamp


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Normalise the PE TimeDateStamp field")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--epoch", type=int, default=None)
    args = parser.parse_args(argv)
    stamp = source_date_epoch() if args.epoch is None else args.epoch
    for path in args.paths:
        before, after = set_pe_timestamp(path, stamp)
        print(f"[repro] [EN] {os.path.basename(path)}: PE timestamp {before} -> {after} (UTC)"
              f" / [ZH] {os.path.basename(path)}：PE 时间戳 {before} -> {after}（UTC）",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
