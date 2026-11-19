// [EN] EchoDesk Avalonia frontend module: Services/WindowChrome.cs. Native window-chrome
// [EN] adaptation shared by the main window and the settings window.
// [ZH] EchoDesk 前端模块：Services/WindowChrome.cs。主窗口与设置窗口共用的原生窗口边框适配。
using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;

namespace EchoDesk.Avalonia.Services;

/// <summary>
/// [EN] Applies the platform-native window chrome that the custom (borderless) caption
/// imitates.
///
/// * macOS: the window is extended into the title bar area and the system chrome is
///   preferred, which restores the native "traffic light" buttons at the top left. The
///   self-drawn caption buttons are hidden and the title text is shifted right so that it
///   never sits underneath the native buttons.
/// * Windows: the custom caption is kept, and the native corner rounding / drop shadow are
///   requested through the transparency-level hint (Mica on Windows 11, Acrylic as a
///   fallback). On systems that do not support either, Avalonia falls back to opaque
///   composition and the window still works exactly as before.
/// * Linux: unchanged; no platform-specific hint is known to be safe.
///
/// The whole method is best-effort: a platform refusing a hint must never prevent the
/// window from opening.
///
/// [ZH] 为自绘（无边框）标题栏补上各平台的原生窗口质感。
///
/// * macOS：把窗口扩展到标题栏区域并优先使用系统边框，从而恢复左上角原生的"红绿灯"
///   按钮。自绘的标题栏按钮隐藏，标题文字右移，避免被原生按钮遮挡。
/// * Windows：保留自绘标题栏，并通过透明级别提示请求原生圆角与投影（Windows 11 用
///   Mica，退化时用 Acrylic）。两者都不支持的系统上 Avallonia 会退回不透明合成，窗口
///   行为与之前完全一致。
/// * Linux：保持现状；没有已知安全的平台专用提示。
///
/// 整个方法是尽力而为：某个平台拒绝某个提示，绝不能阻止窗口打开。
/// </summary>
public static class WindowChrome
{
    /// <summary>
    /// [EN] Applies the platform chrome to <paramref name="window"/>.
    /// [ZH] 把平台窗口质感应用到 <paramref name="window"/>。
    /// </summary>
    /// <param name="window">[EN] Target window. [ZH] 目标窗口。</param>
    /// <param name="titleText">[EN] Caption title, shifted on macOS. [ZH] 标题文本，macOS 下右移。</param>
    /// <param name="captionButtons">[EN] Self-drawn caption buttons, hidden on macOS.
    /// [ZH] 自绘标题栏按钮，macOS 下隐藏。</param>
    public static void Apply(Window window, Control? titleText = null,
                             params Control?[] captionButtons)
    {
        try
        {
            if (OperatingSystem.IsMacOS())
            {
                // [EN] Avalonia 12 removed the separate chrome-hint enum; extending the
                // [EN] client area is what makes the platform draw its own window buttons
                // [EN] (the macOS traffic lights).
                // [ZH] Avalonia 12 已移除独立的 chrome hint 枚举；扩展客户区本身就会让平台
                // [ZH] 绘制自己的窗口按钮（即 macOS 红绿灯）。
                window.ExtendClientAreaToDecorationsHint = true;
                window.ExtendClientAreaTitleBarHeightHint = 44;
                foreach (Control? button in captionButtons)
                {
                    if (button is not null)
                        button.IsVisible = false;
                }
                if (titleText is not null)
                    titleText.Margin = new Thickness(72, 0, 0, 0);
            }
            else if (OperatingSystem.IsWindows())
            {
                window.TransparencyLevelHint = new[]
                {
                    WindowTransparencyLevel.Mica,
                    WindowTransparencyLevel.AcrylicBlur,
                };
            }
        }
        catch (Exception)
        {
            // [EN] A refused hint is not a failure: keep the previous appearance.
            // [ZH] 提示被拒绝不算失败：保持原有外观。
        }
    }
}
