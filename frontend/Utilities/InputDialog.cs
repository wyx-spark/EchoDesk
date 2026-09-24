using System.Windows;
using System.Windows.Controls;
using EchoDesk.Services;

namespace EchoDesk.Utilities;

/// <summary>
/// 极简单行输入对话框（WPF 没有内置的 InputBox）。
///
/// 只用于「给模板起个名字」这类一次性输入，因此刻意做成代码构建的窗口，
/// 不引入任何第三方控件库（任务书全局要求：不为一个小功能增加打包体积）。
///
/// 多语言：按钮文案走 `main.dialog.ok` / `main.dialog.cancel`；
/// 窗口标题与提示语由调用方传入（调用方负责用 Loc.T 取好），
/// 因为这里只是一个通用容器，不该替调用方决定文案归属。
/// </summary>
public static class InputDialog
{
    /// <summary>弹出输入框；用户取消或输入为空时返回 null。</summary>
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
        // 主题与字体：代码构建的窗口不会自动继承 Window 的隐式样式，
        // 必须显式绑定，否则深色主题下这里会是一块白底（修复二）。
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
