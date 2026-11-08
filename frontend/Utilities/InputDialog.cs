// [EN] EchoDesk WPF frontend module: frontend/Utilities/InputDialog.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Utilities/InputDialog.cs。类型与成员说明见下方注释。
using System.Windows;
using System.Windows.Controls;
using EchoDesk.Services;

namespace EchoDesk.Utilities;

/// <summary>
/// [EN] Minimal single-line input dialog (WPF has no built-in InputBox).
/// [EN]
/// [EN] It is used only for one-off input such as naming a template, so the window is
/// [EN] deliberately built in code and no third-party control library is introduced (a global
/// [EN] requirement of the design specification: do not increase the package size for a small
/// [EN] feature).
/// [EN]
/// [EN] Localization: button text comes from `main.dialog.ok` / `main.dialog.cancel`;
/// [EN] the window title and prompt are supplied by the caller (the caller resolves
/// [EN] them via Loc.T), because this type is only a generic container and should not
/// [EN] decide the text ownership for the caller.
/// [ZH] 极简单行输入对话框（WPF 没有内置的 InputBox）。
/// [ZH]
/// [ZH] 只用于「给模板起个名字」这类一次性输入，因此刻意做成代码构建的窗口，
/// [ZH] 不引入任何第三方控件库（设计规范全局要求：不为一个小功能增加打包体积）。
/// [ZH]
/// [ZH] 多语言：按钮文案走 `main.dialog.ok` / `main.dialog.cancel`；
/// [ZH] 窗口标题与提示语由调用方传入（调用方负责用 Loc.T 取好），
/// [ZH] 因为这里只是一个通用容器，不该替调用方决定文案归属。
/// </summary>
public static class InputDialog
{
    /// <summary>
    /// [EN] Shows the input box; returns null when the user cancels or the input is empty.
    /// [ZH] 弹出输入框；用户取消或输入为空时返回 null。
    /// </summary>
    public static string? Show(Window? owner, string title, string prompt, string defaultValue = "")
    {
        var window = new Window
        {
            Title = title,
            Width = 420,
            SizeToContent = SizeToContent.Height,
            WindowStartupLocation = owner is null ? WindowStartupLocation.CenterScreen : WindowStartupLocation.CenterOwner,
            ResizeMode = ResizeMode.NoResize,
            ShowInTaskbar = false,
        };
        // [EN] Theme and fonts: a window built in code does not automatically inherit the
        // [EN] implicit Window styles and must bind them explicitly; otherwise the dialog has a
        // [EN] white background in the dark theme (fix two).
        // [ZH] 主题与字体：代码构建的窗口不会自动继承 Window 的隐式样式，
        // [ZH] 必须显式绑定，否则深色主题下这里会是一块白底（修复二）。
        window.SetResourceReference(Window.BackgroundProperty, "WindowBackground");
        window.SetResourceReference(Window.ForegroundProperty, "TextForeground");
        window.SetResourceReference(Window.FontFamilyProperty, "UiFontFamily");
        if (owner is not null)
            window.Owner = owner;

        var root = new StackPanel { Margin = new Thickness(16) };
        root.Children.Add(new TextBlock
        {
            Text = prompt,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 8),
        });
        var box = new TextBox { Text = defaultValue, Padding = new Thickness(6, 4, 6, 4) };
        root.Children.Add(box);

        var buttons = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Margin = new Thickness(0, 14, 0, 0),
        };
        string? result = null;
        var ok = new Button
        {
            Content = Loc.T("main.dialog.ok", "确定"),
            Padding = new Thickness(16, 5, 16, 5),
            IsDefault = true,
        };
        ok.Click += (_, _) =>
        {
            result = box.Text;
            window.DialogResult = true;
        };
        var cancel = new Button
        {
            Content = Loc.T("main.dialog.cancel", "取消"),
            Padding = new Thickness(16, 5, 16, 5),
            Margin = new Thickness(8, 0, 0, 0),
            IsCancel = true,
        };
        buttons.Children.Add(ok);
        buttons.Children.Add(cancel);
        root.Children.Add(buttons);

        window.Content = root;
        box.Focus();
        box.SelectAll();
        bool? closed = window.ShowDialog();
        return closed == true && !string.IsNullOrWhiteSpace(result) ? result!.Trim() : null;
    }
}
