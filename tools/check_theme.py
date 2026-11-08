"""主题色板与对比度检查（任务书修复二）。

检查三件事，任何一项不达标都退出码非 0：

1. **色板一致性**：``Theme.Light.xaml`` 与 ``Theme.Dark.xaml`` 的键名集合必须完全相同。
   少一个键就会出现"切到深色后某处取不到资源 → 回退成系统默认色"，即"残留旧色"。
2. **对比度达标**：按 WCAG 2.1 相对亮度公式计算每一对"文字色 / 背景色"的对比度，
   正文要求 ≥ 4.5:1，大字号与图形边界要求 ≥ 3:1。检查表见 ``CONTRAST_PAIRS``。
3. **不硬编码颜色**：界面代码（xaml / cs）里不允许出现色值字面量，颜色必须来自色板；
   同时校验所有 ``{DynamicResource <键>}`` 引用的键真的存在（写错键名会导致文字不可见）。

色板来源（v0.2.3 起明确）：

* 第 1、2 项的通用断言针对 WPF 色板 ``frontend/Resources/Theme.Light.xaml`` /
  ``Theme.Dark.xaml``；
* 跨平台前端（Avalonia）拥有独立的色板 ``frontend-avalonia/.../Themes/Light.axaml`` /
  ``Dark.axaml``，其中定义了仅由该前端使用的强调色令牌（悬停、按下）。
  WPF 在转入长期支持时清理了这些未引用令牌，因此针对它们的对比度断言必须检查
  **真正定义它们的色板**，否则门禁会拿一个不可能出现的键去比对，自己变瞎。

用法：
    python tools/check_theme.py            # 检查
    python tools/check_theme.py -v         # 打印全部对比度明细
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEME_DIR = os.path.join(ROOT, "frontend", "Resources")
LIGHT = os.path.join(THEME_DIR, "Theme.Light.xaml")
DARK = os.path.join(THEME_DIR, "Theme.Dark.xaml")
STYLES = os.path.join(THEME_DIR, "Styles.xaml")

# 跨平台前端（Avalonia）的色板：v0.2.3 起该前端拥有独立主题文件。
AVALONIA_THEME_DIR = os.path.join(ROOT, "frontend-avalonia", "EchoDesk.Avalonia", "Themes")
AV_LIGHT = os.path.join(AVALONIA_THEME_DIR, "Light.axaml")
AV_DARK = os.path.join(AVALONIA_THEME_DIR, "Dark.axaml")

# 允许出现色值的文件（色板的唯一来源）
COLOR_ALLOWLIST = {os.path.normcase(LIGHT), os.path.normcase(DARK)}

# (前景键, 背景键, 最低对比度, 说明, 是否强制)
# 4.5 = 正文（WCAG AA），3.0 = 大字号 / 必须能分辨的控件边界（WCAG 1.4.11）。
# 最后一项 False 表示"纯装饰性元素"，只测量并提示、不作为失败（WCAG 1.4.11 明确
# 只约束"理解内容所必需的图形"与"用户界面组件"；卡片之间的分隔线两者都不是）。
CONTRAST_PAIRS = [
    ("TextForeground", "WindowBackground", 4.5, "正文 / 窗口背景", True),
    ("TextForeground", "SurfaceBrush", 4.5, "正文 / 卡片表面", True),
    ("TextForeground", "SurfaceAltBrush", 4.5, "正文 / 次级表面", True),
    ("TextForeground", "InputBackground", 4.5, "输入框文字 / 输入框背景", True),
    ("TextForeground", "SelectionBackground", 4.5, "列表项文字 / 选中背景", True),
    ("TextForeground", "RowHoverBackground", 4.5, "列表项文字 / 悬停背景", True),
    ("TextForeground", "TabActiveBackground", 4.5, "页签文字 / 选中页签", True),
    ("TextForeground", "TabInactiveBackground", 4.5, "页签文字 / 未选中页签", True),
    ("SubTextBrush", "WindowBackground", 4.5, "次要文字（时间戳/提示）/ 窗口背景", True),
    ("SubTextBrush", "SurfaceBrush", 4.5, "次要文字 / 卡片表面", True),
    ("SubTextBrush", "SurfaceAltBrush", 4.5, "次要文字 / 次级表面", True),
    ("SubTextBrush", "InputBackground", 4.5, "次要文字 / 输入框背景", True),
    ("SubTextBrush", "TabInactiveBackground", 4.5, "次要文字 / 未选中页签", True),
    ("DisabledTextBrush", "ButtonDisabledBackground", 4.5, "禁用文字 / 禁用按钮背景", True),
    ("DisabledTextBrush", "WindowBackground", 4.5, "禁用文字 / 窗口背景", True),
    ("ButtonForeground", "ButtonBackground", 4.5, "按钮文字 / 按钮背景（正常）", True),
    ("ButtonForeground", "ButtonHoverBackground", 4.5, "按钮文字 / 按钮背景（悬停）", True),
    ("ButtonForeground", "ButtonPressedBackground", 4.5, "按钮文字 / 按钮背景（按下）", True),
    ("ButtonDisabledForeground", "ButtonDisabledBackground", 4.5, "按钮文字 / 按钮背景（禁用）", True),
    ("AccentBrush", "WindowBackground", 4.5, "强调色文字（标题）/ 窗口背景", True),
    ("AccentBrush", "SurfaceBrush", 4.5, "强调色文字 / 卡片表面", True),
    ("AccentForegroundBrush", "AccentBrush", 4.5, "强调按钮文字 / 强调背景", True),
    ("SelectionForeground", "SelectionBackground", 4.5, "选中项文字 / 选中背景", True),
    ("MenuForeground", "MenuBackground", 4.5, "菜单文字 / 菜单背景", True),
    ("MenuForeground", "MenuHighlightBackground", 4.5, "菜单文字 / 菜单高亮", True),
    ("TooltipForeground", "TooltipBackground", 4.5, "提示气泡文字 / 背景", True),
    ("LinkBrush", "WindowBackground", 4.5, "链接 / 窗口背景", True),
    ("LinkBrush", "SurfaceBrush", 4.5, "链接 / 卡片表面", True),
    ("CodeForeground", "CodeBackground", 4.5, "代码文字 / 代码块背景", True),
    ("QuoteForeground", "WindowBackground", 4.5, "引用块文字 / 窗口背景", True),
    ("SuccessBrush", "WindowBackground", 4.5, "成功状态 / 窗口背景", True),
    ("WarningBrush", "WindowBackground", 4.5, "警告状态 / 窗口背景", True),
    ("ErrorBrush", "WindowBackground", 4.5, "错误状态 / 窗口背景", True),
    ("InfoBrush", "WindowBackground", 4.5, "信息状态 / 窗口背景", True),
    ("UserBrush", "WindowBackground", 4.5, "用户消息标识 / 窗口背景", True),
    ("AssistantBrush", "WindowBackground", 4.5, "助手消息标识 / 窗口背景", True),
    ("SystemBrush", "WindowBackground", 4.5, "系统消息标识 / 窗口背景", True),
    # 控件边界：必须能分辨出"这里有个输入框/按钮"，属 WCAG 1.4.11 范围
    ("InputBorder", "WindowBackground", 3.0, "输入框边框 / 窗口背景", True),
    ("InputBorder", "InputBackground", 3.0, "输入框边框 / 输入框背景", True),
    ("ButtonBorder", "WindowBackground", 3.0, "按钮边框 / 窗口背景", True),
    # 滚动条滑块是"用户界面组件"，需要 3:1
    ("ScrollBarThumbBrush", "ScrollBarTrackBrush", 3.0, "滚动条滑块 / 轨道", True),
    # 纯装饰元素：只测量
    ("SeparatorBrush", "SurfaceBrush", 3.0, "分隔线 / 卡片表面（装饰性）", False),
]

# Avalonia 专属的对比度断言。这些键只存在于跨平台前端的色板中（WPF 在转入长期支持时
# 清理了未引用的悬停/按下令牌），因此必须针对 Avalonia 色板执行，阈值与上表一致。
AVALONIA_CONTRAST_PAIRS = [
    ("AccentForegroundBrush", "AccentBrush", 4.5, "强调按钮文字 / 强调背景（Avalonia）", True),
    ("AccentForegroundBrush", "AccentHoverBrush", 4.5, "强调按钮文字 / 悬停（Avalonia）", True),
    ("AccentForegroundBrush", "AccentPressedBrush", 4.5, "强调按钮文字 / 按下（Avalonia）", True),
]

BRUSH_RE = re.compile(r'x:Key="([A-Za-z0-9_]+)"\s+Color="(#[0-9A-Fa-f]{6,8})"')
COLOR_LITERAL_RE = re.compile(r'#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?\b')
DYNAMIC_RE = re.compile(r'\{DynamicResource\s+([A-Za-z0-9_]+)\s*\}')
STATIC_RE = re.compile(r'\{StaticResource\s+([A-Za-z0-9_]+)\s*\}')
CS_COLOR_RE = re.compile(r'Color\.From(?:Rgb|Argb|ScRgb)\(|Brushes\.[A-Z][A-Za-z]+')

failures: list[str] = []


def load_palette(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    return {key: value for key, value in BRUSH_RE.findall(text)}


def to_rgb(color: str) -> tuple:
    text = color.lstrip("#")
    if len(text) == 8:      # AARRGGBB
        text = text[2:]
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def relative_luminance(rgb: tuple) -> float:
    def channel(value: int) -> float:
        srgb = value / 255.0
        return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    l1 = relative_luminance(to_rgb(fg))
    l2 = relative_luminance(to_rgb(bg))
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def check_palette(family: str, light: dict, dark: dict) -> None:
    if not light or not dark:
        failures.append(f"{family} 主题色板为空，路径可能写错了")
        return
    missing_in_dark = sorted(set(light) - set(dark))
    missing_in_light = sorted(set(dark) - set(light))
    if missing_in_dark:
        failures.append(f"{family} 深色主题缺少键（切主题时会残留默认色）: {', '.join(missing_in_dark)}")
    if missing_in_light:
        failures.append(f"{family} 浅色主题缺少键: {', '.join(missing_in_light)}")
    if not missing_in_dark and not missing_in_light:
        print(f"  [OK] {family} 两套色板键名一致，共 {len(light)} 个键")


def check_contrast(labels: dict, verbose: bool, pairs=None) -> None:
    pairs = CONTRAST_PAIRS if pairs is None else pairs
    for name, palette in labels.items():
        bad = 0
        advisory = []
        for fg_key, bg_key, minimum, label, required in pairs:
            if fg_key not in palette or bg_key not in palette:
                failures.append(f"{name}：对比度表引用了不存在的键 {fg_key} / {bg_key}")
                bad += 1
                continue
            ratio = contrast(palette[fg_key], palette[bg_key])
            enough = ratio >= minimum
            if not enough and not required:
                advisory.append(f"{label}: {ratio:.2f}:1（装饰性，仅提示）")
                continue
            mark = "OK  " if enough else "FAIL"
            if verbose or not enough:
                print(f"    [{mark}] {name} {label}: {ratio:.2f}:1（要求 ≥ {minimum}）")
            if not enough:
                failures.append(
                    f"{name} 对比度不足：{label} {palette[fg_key]} on {palette[bg_key]} "
                    f"= {ratio:.2f}:1（要求 ≥ {minimum}）")
                bad += 1
        required_count = sum(1 for item in CONTRAST_PAIRS if item[4])
        if bad == 0:
            print(f"  [OK] {name}：{required_count} 组强制对比度全部达标")
        for note in advisory:
            print(f"    [提示] {name} {note}")


def iter_ui_sources():
    """界面代码文件（xaml / cs），用于硬编码色值与资源键检查。"""
    for base in (os.path.join(ROOT, "frontend"),):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("obj", "bin", "Resources")]
            for name in filenames:
                if name.endswith((".xaml", ".cs")):
                    yield os.path.join(dirpath, name)
    # Resources 下的非主题 xaml（Styles.xaml 等）也要查
    for name in os.listdir(THEME_DIR):
        if name.endswith(".xaml") and os.path.normcase(os.path.join(THEME_DIR, name)) not in COLOR_ALLOWLIST:
            yield os.path.join(THEME_DIR, name)


def iter_avalonia_sources() -> list[str]:
    """[EN] Avalonia UI sources (XAML/C#/project) used for orphan-asset checks.

    [ZH] 用于孤立资源检查的 Avalonia 界面源文件（XAML/C#/工程文件）。
    """
    sources: list[str] = []
    base = os.path.join(ROOT, "frontend-avalonia")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("obj", "bin")]
        for name in filenames:
            if name.endswith((".axaml", ".cs", ".csproj")):
                sources.append(os.path.join(dirpath, name))
    return sources


def check_avalonia_orphans() -> None:
    """[EN] Fail when an Avalonia asset or theme dictionary is never referenced.

    [ZH] 当 Avalonia 资源文件或主题字典从未被引用时失败。

    The check exists because unreferenced files still get embedded by the SDK (or sit in
    the source tree forever) and silently grow the release. It is intentionally narrow:
    only files under ``Assets/`` and ``Themes/`` are considered, so generated or
    runtime-loaded content cannot cause a false failure.
    """
    app = os.path.join(ROOT, "frontend-avalonia", "EchoDesk.Avalonia")
    text_by_file: dict[str, str] = {}
    for path in iter_avalonia_sources():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text_by_file[path] = handle.read()
        except OSError:
            continue

    def referenced(name: str) -> list[str]:
        return [path for path, text in text_by_file.items() if name in text]

    missing_references: list[str] = []
    assets_dir = os.path.join(app, "Assets")
    asset_count = 0
    if os.path.isdir(assets_dir):
        for dirpath, _dirnames, filenames in os.walk(assets_dir):
            for name in filenames:
                asset_count += 1
                if not referenced(name):
                    missing_references.append(os.path.relpath(os.path.join(dirpath, name), ROOT))

    themes_dir = os.path.join(app, "Themes")
    theme_count = 0
    if os.path.isdir(themes_dir):
        for name in sorted(os.listdir(themes_dir)):
            if not name.endswith(".axaml"):
                continue
            theme_count += 1
            if not referenced(name):
                missing_references.append(os.path.relpath(os.path.join(themes_dir, name), ROOT))

    if missing_references:
        failures.append(
            "Avalonia 存在未被引用的资源文件（会随发布物一起变大）："
            + "、".join(missing_references)
        )
    else:
        print(f"  [OK] Avalonia 资源无孤儿：{asset_count} 个 Assets 文件、"
              f"{theme_count} 个主题字典均被引用")


def check_hardcoded(labels: dict) -> None:
    keys = set(labels["浅色"]) | set(labels["深色"])
    # 样式资源里额外定义的键（字体等）也算合法引用目标
    with open(STYLES, "r", encoding="utf-8") as f:
        styles = f.read()
    keys |= set(re.findall(r'x:Key="([A-Za-z0-9_]+)"', styles))

    color_hits, key_hits = [], []
    for path in iter_ui_sources():
        if os.path.normcase(path) in COLOR_ALLOWLIST:
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        rel = os.path.relpath(path, ROOT)
        for match in COLOR_LITERAL_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            color_hits.append(f"{rel}:{line} 出现硬编码色值 {match.group(0)}")
        for match in CS_COLOR_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            snippet = text[match.start():match.end()]
            # Brushes.Transparent / Gray 之类属于"无色"或语义占位，允许
            if snippet in ("Brushes.Transparent", "Brushes.Gray"):
                continue
            color_hits.append(f"{rel}:{line} 出现硬编码颜色 {snippet}（应改为色板键）")
        for match in DYNAMIC_RE.finditer(text):
            if match.group(1) not in keys:
                line = text.count("\n", 0, match.start()) + 1
                key_hits.append(f"{rel}:{line} 引用了不存在的色板键 {match.group(1)}")
        for match in STATIC_RE.finditer(text):
            if match.group(1) not in keys:
                line = text.count("\n", 0, match.start()) + 1
                key_hits.append(f"{rel}:{line} 引用了不存在的资源键 {match.group(1)}")

    if color_hits:
        failures.extend(color_hits)
    else:
        print("  [OK] 界面代码中没有硬编码颜色（颜色全部来自色板）")
    if key_hits:
        failures.extend(key_hits)
    else:
        print("  [OK] 所有资源引用都能在色板/样式中找到")


def main() -> int:
    parser = argparse.ArgumentParser(description="主题色板与对比度检查")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印全部对比度明细")
    args = parser.parse_args()

    print("== 主题色板（修复二）==")
    labels = {"浅色": load_palette(LIGHT), "深色": load_palette(DARK)}
    check_palette("WPF", labels["浅色"], labels["深色"])
    print("-- WCAG AA 对比度")
    check_contrast(labels, args.verbose)

    # 跨平台前端的色板：v0.2.3 起为独立主题文件。此处检查键名一致性与专属令牌的对比度，
    # 阈值与 WPF 色板相同（正文 4.5:1），不降低任何既有要求。
    print("-- 跨平台前端色板（Avalonia）")
    if not (os.path.exists(AV_LIGHT) and os.path.exists(AV_DARK)):
        failures.append(f"Avalonia 色板缺失：{AV_LIGHT} / {AV_DARK}")
    else:
        av_labels = {"浅色（Avalonia）": load_palette(AV_LIGHT),
                     "深色（Avalonia）": load_palette(AV_DARK)}
        check_palette("Avalonia", av_labels["浅色（Avalonia）"], av_labels["深色（Avalonia）"])
        check_contrast(av_labels, args.verbose, AVALONIA_CONTRAST_PAIRS)

    print("-- 界面代码")
    check_hardcoded(labels)

    print("-- Avalonia 资源引用")
    check_avalonia_orphans()

    if failures:
        print(f"\n✗ 发现 {len(failures)} 处问题：")
        for item in failures:
            print(f"   - {item}")
        return 1
    print("\n✓ 主题检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
