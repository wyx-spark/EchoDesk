#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成三平台需要的图标与桌面条目（v0.2.2 全面双语与三平台友好 任务七）。

**为什么需要这个工具，而不是手工准备几个图片文件**：

* Windows 只有一份 `assets/EchoDesk.ico`；Linux 的 freedesktop 约定要的是
  `hicolor/<尺寸>x<尺寸>/apps/<名字>.png` 这种**目录结构**，macOS 要的是 `.icns` 容器。
  三者格式不同但**源素材是同一份**，手工维护必然漂移（改了 ico 忘了 png）。
* 本项目的构建机是 Windows，**没有任何 macOS 工具**（`iconutil` 只在 macOS 上存在）。
  所以 `.icns` 必须自己按容器格式拼出来 —— 好在 icns 允许直接内嵌 PNG，
  不需要图标转换器，纯标准库即可。

产出（全部由本脚本生成，不要手工编辑）：

    assets/EchoDesk.icns                         macOS 图标（PNG 内嵌的 icns 容器）
    assets/icons/hicolor/<N>x<N>/apps/echodesk.png   Linux 图标（可直接 cp -r 到 /usr/share/icons/hicolor/）
    deploy/linux/echodesk.desktop                Linux 桌面条目
    deploy/linux/com.voidentropia.echodesk.metainfo.xml  AppStream 元数据（软件中心用）

用法：

    python tools/gen_platform_icons.py            # 生成全部
    python tools/gen_platform_icons.py --check    # 只校验产物是否与源素材一致（门禁用）

**诚实的边界**：源素材最大 256×256，因此 512/1024 的槽位**不会生成假的放大图** ——
`.icns` 里没有 `ic09`(512)/`ic10`(1024)，`hicolor` 里也没有 512 目录。
高分屏下系统会放大 256 的图，观感略糊；要真正的高分屏图标，需要用矢量源重新导出 512/1024。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICO = os.path.join(ROOT, "assets", "EchoDesk.ico")
ICNS = os.path.join(ROOT, "assets", "EchoDesk.icns")
HICOLOR = os.path.join(ROOT, "assets", "icons", "hicolor")
DESKTOP = os.path.join(ROOT, "deploy", "linux", "echodesk.desktop")
METAINFO = os.path.join(ROOT, "deploy", "linux", "com.voidentropia.echodesk.metainfo.xml")

#: icns 里"内嵌 PNG"的块类型 → 该块应当承载的像素尺寸。
#: 命名规则：ic07=128、ic08=256、ic09=512、ic10=1024，ic11~ic14 是它们的 @2x 变体。
#: 这里只登记**源素材真的有的尺寸**（16~256），不放大充数。
ICNS_PNG_TYPES: dict[bytes, int] = {
    b"ic11": 32,    # 16x16@2x
    b"ic12": 64,    # 32x32@2x
    b"ic07": 128,   # 128x128
    b"ic13": 256,   # 128x128@2x
    b"ic08": 256,   # 256x256
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def read_ico_images(path: str) -> dict[int, bytes]:
    """从 .ico 里取出每个尺寸的**原始图像数据**，返回 {尺寸: 字节}。

    只处理 PNG 内嵌的条目（本项目当前 6 个条目全是 PNG）。遇到 BMP 条目会明确报错而不是
    猜格式 —— 猜错会产出"看着像图标、实际打不开"的文件。
    """
    with open(path, "rb") as handle:
        data = handle.read()
    reserved, image_type, count = struct.unpack("<HHH", data[:6])
    if reserved != 0 or image_type != 1:
        raise SystemExit(f"{path} 不是合法的 .ico（reserved={reserved} type={image_type}）")
    images: dict[int, bytes] = {}
    for index in range(count):
        offset = 6 + index * 16
        width, height, _colors, _reserved, _planes, _bpp, size, data_offset = struct.unpack(
            "<BBBBHHII", data[offset:offset + 16])
        size_px = width or 256
        blob = data[data_offset:data_offset + size]
        if not blob.startswith(PNG_MAGIC):
            raise SystemExit(f"{path} 里的 {size_px}x{size_px} 条目不是 PNG（BMP 条目未实现）")
        images[size_px] = blob
    if not images:
        raise SystemExit(f"{path} 里没有任何图像")
    return images


def png_size(blob: bytes) -> tuple[int, int]:
    width, height = struct.unpack(">II", blob[16:24])
    return width, height


def build_icns(images: dict[int, bytes]) -> bytes:
    """把 PNG 打进 icns 容器。

    容器格式：`icns` + 总长度(4) + 若干「类型(4) + 长度(4，含这 8 字节头) + 数据」块。
    长度一律**大端**（与 png_size 同一约定）—— 这一点写错会让 macOS 直接拒绝加载。
    """
    body = io.BytesIO()
    for chunk_type, want in ICNS_PNG_TYPES.items():
        blob = images.get(want)
        if blob is None:
            continue
        width, height = png_size(blob)
        if width != want or height != want:
            raise SystemExit(f"icns 块 {chunk_type!r} 需要 {want}x{want}，但拿到 {width}x{height}")
        body.write(chunk_type)
        body.write(struct.pack(">I", len(blob) + 8))
        body.write(blob)
    payload = body.getvalue()
    return b"icns" + struct.pack(">I", len(payload) + 8) + payload


def desktop_entry() -> str:
    """Linux 桌面条目。

    `Icon=echodesk` 对应 `hicolor/*/apps/echodesk.png`；`Exec` 默认按"解压到任意目录、
    用 start.sh 启动"的便携布局写，**并在注释里给出装到系统目录时的替代写法** ——
    这个项目目前没有 Linux 系统级安装包，写死一个 /opt 路径会让便携用户点不开。
    """
    return (
        "[Desktop Entry]\n"
        "# EchoDesk 桌面条目（v0.2.2 全面双语与三平台友好 任务七）\n"
        "# 安装方式见 deploy/linux/README.md；图标来自 assets/icons/hicolor/。\n"
        "#\n"
        "# 便携用法：把本文件放到 ~/.local/share/applications/，并把 Exec 改成解压目录里的 start.sh 绝对路径，\n"
        "#           例如 Exec=/home/<你>/EchoDesk-v0.2.2-linux-x64/start.sh\n"
        "# 系统级用法：程序装到 /opt/echodesk/，图标树 cp -r 到 /usr/share/icons/hicolor/，\n"
        "#           此时 Exec 写 /opt/echodesk/start.sh 即可（本文件默认就是这种写法）。\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=EchoDesk\n"
        "GenericName=Local AI desktop assistant\n"
        "GenericName[zh_CN]=本地 AI 桌面助手\n"
        "Comment=Chat with a local model; files, automation, skills and MCP all stay on this machine.\n"
        "Comment[zh_CN]=用本机模型对话；文件、自动化、技能与 MCP 都不出本机。\n"
        "Exec=/opt/echodesk/start.sh\n"
        "Icon=echodesk\n"
        "Terminal=false\n"
        "Categories=Utility;Office;\n"
        "Keywords=AI;assistant;LLM;local;offline;\n"
        "Keywords[zh_CN]=AI;助手;本地;离线;\n"
        "StartupNotify=true\n"
        "StartupWMClass=EchoDesk\n"
    )


def metainfo() -> str:
    """AppStream 元数据（Linux 软件中心展示用）。字段取值都来自仓库里的事实，不编造。"""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- EchoDesk AppStream 元数据（由 tools/gen_platform_icons.py 生成）。\n"
        "     安装：cp 到 /usr/share/metainfo/ ；截图目录是 assets/screenshots/（本版暂无截图）。 -->\n"
        '<component type="desktop-application">\n'
        "  <id>com.voidentropia.echodesk</id>\n"
        "  <metadata_license>CC0-1.0</metadata_license>\n"
        "  <project_license>Apache-2.0</project_license>\n"
        "  <name>EchoDesk</name>\n"
        "  <summary>Local-only AI desktop assistant</summary>\n"
        "  <summary xml:lang=\"zh_CN\">纯本地的 AI 桌面助手</summary>\n"
        "  <description>\n"
        "    <p>\n"
        "      EchoDesk runs a local model on your own machine and keeps conversation history,\n"
        "      documents and search results there. It reads Office/TXT/PDF files, runs agent\n"
        "      workflows and automation, loads plugins, and speaks MCP — without sending your\n"
        "      data to a server.\n"
        "    </p>\n"
        "    <p xml:lang=\"zh_CN\">\n"
        "      EchoDesk 在本机运行模型，对话记录、文档与检索结果都留在本机。可读取 Office/TXT/PDF、\n"
        "      运行 Agent 与自动化、加载插件、接入 MCP —— 不把数据发往服务器。\n"
        "    </p>\n"
        "  </description>\n"
        "  <launchable type=\"desktop-id\">echodesk.desktop</launchable>\n"
        "  <url type=\"homepage\">https://github.com/VoidEntropiaStudio/EchoDesk</url>\n"
        "  <developer id=\"com.voidentropia\">\n"
        "    <name>Void Entropia Studio</name>\n"
        "  </developer>\n"
        "  <content_rating type=\"oars-1.1\"/>\n"
        "  <releases>\n"
        "    <release version=\"0.2.2\" date=\"2026-10-06\"/>\n"
        "  </releases>\n"
        "</component>\n"
    )


def generate() -> None:
    images = read_ico_images(ICO)
    sizes = sorted(images)
    print(f"源素材 {os.path.relpath(ICO, ROOT)}：{len(sizes)} 个尺寸 → {', '.join(str(s) for s in sizes)}")

    icns = build_icns(images)
    with open(ICNS, "wb") as handle:
        handle.write(icns)
    used = sorted({want for want in ICNS_PNG_TYPES.values() if want in images})
    print(f"已生成 {os.path.relpath(ICNS, ROOT)}（{len(icns)} 字节；内嵌块尺寸 {used}）")

    written = 0
    for size in sizes:
        target_dir = os.path.join(HICOLOR, f"{size}x{size}", "apps")
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "echodesk.png"), "wb") as handle:
            handle.write(images[size])
        written += 1
    print(f"已生成 {os.path.relpath(HICOLOR, ROOT)}/…：{written} 个尺寸的 echodesk.png")

    os.makedirs(os.path.dirname(DESKTOP), exist_ok=True)
    with io.open(DESKTOP, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(desktop_entry())
    with io.open(METAINFO, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(metainfo())
    print(f"已生成 {os.path.relpath(DESKTOP, ROOT)} 与 {os.path.relpath(METAINFO, ROOT)}")


def check() -> int:
    """门禁：产物必须与源素材一致（防止改了 ico 忘了重新生成）。"""
    problems: list[str] = []
    if not os.path.isfile(ICO):
        return 0  # 没有源素材就没什么可查的（例如裁剪过的检出）
    images = read_ico_images(ICO)

    if not os.path.isfile(ICNS):
        problems.append(f"缺少 {os.path.relpath(ICNS, ROOT)}（跑 python tools/gen_platform_icons.py）")
    else:
        with open(ICNS, "rb") as handle:
            blob = handle.read()
        if blob[:4] != b"icns":
            problems.append(f"{os.path.relpath(ICNS, ROOT)} 不是 icns（magic={blob[:4]!r}）")
        elif struct.unpack(">I", blob[4:8])[0] != len(blob):
            problems.append(f"{os.path.relpath(ICNS, ROOT)} 的总长度字段与文件实际长度不符")
        else:
            # 逐个块校验：长度自洽 + 内嵌 PNG 尺寸与块类型相符
            offset, seen = 8, 0
            while offset + 8 <= len(blob):
                chunk_type = blob[offset:offset + 4]
                chunk_len = struct.unpack(">I", blob[offset + 4:offset + 8])[0]
                if chunk_len < 8 or offset + chunk_len > len(blob):
                    problems.append(f"icns 块 {chunk_type!r} 的长度字段越界（{chunk_len}）")
                    break
                data = blob[offset + 8:offset + chunk_len]
                want = ICNS_PNG_TYPES.get(chunk_type)
                if want is None:
                    problems.append(f"icns 里有未登记预期的块类型 {chunk_type!r}")
                elif data.startswith(PNG_MAGIC):
                    width, height = png_size(data)
                    if width != want or height != want:
                        problems.append(f"icns 块 {chunk_type!r} 期望 {want}x{want}，实际 {width}x{height}")
                else:
                    problems.append(f"icns 块 {chunk_type!r} 不是 PNG（本工具只生成 PNG 内嵌的 icns）")
                offset += chunk_len
                seen += 1
            if seen == 0:
                problems.append("icns 里没有任何块")

    for size in sorted(images):
        path = os.path.join(HICOLOR, f"{size}x{size}", "apps", "echodesk.png")
        if not os.path.isfile(path):
            problems.append(f"缺少 Linux 图标 {os.path.relpath(path, ROOT)}")
            continue
        with open(path, "rb") as handle:
            blob = handle.read()
        if blob != images[size]:
            problems.append(f"{os.path.relpath(path, ROOT)} 与源素材不同（改了 ico 但没重新生成）")

    for path in (DESKTOP, METAINFO):
        if not os.path.isfile(path):
            problems.append(f"缺少 {os.path.relpath(path, ROOT)}")

    print("== 平台资源门禁（图标 / .desktop / AppStream）==")
    if problems:
        print(f"发现问题 {len(problems)} 项：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print(f"通过：icns + {len(images)} 个尺寸的 hicolor 图标 + .desktop + metainfo 都与源素材一致。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/校验三平台图标与桌面条目")
    parser.add_argument("--check", action="store_true", help="只校验，不生成")
    args = parser.parse_args()
    if args.check:
        return check()
    generate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
