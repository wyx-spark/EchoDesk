// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/CrashLog.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/CrashLog.cs。类型与成员说明见下方注释。
// [EN] Front-end crash log and global exception fallback (v0.2.2 batch 3, aligned with
// [EN] App.Log and global exception handling in WPF).
// [ZH] 前端崩溃日志与全局异常兜底（v0.2.2 第 3 批，对齐 WPF 的 App.Log + 全局异常处理）。
//
// [EN] The WPF version has two related capabilities; the cross-platform version previously
// [EN] had neither (difference list B5 "entire block missing"):
// [ZH] WPF 版有两条相关能力，跨平台版此前一条都没有（差异清单 B5“整块缺失”）：
// [EN]   ① **Global unhandled exception dialog**: an unexpected exception on the UI thread
// [EN]      does not make the process disappear silently; instead a window appears saying
// [EN]      "something went wrong and where the log is";
// [ZH]   ① **全局未处理异常提示框** —— 界面线程上的意外异常不会让进程静默消失，
// [ZH]      而是弹一个“出错了 + 日志在哪”的窗口；
// [EN]   ② **Front-end crash log**: exceptions are written to frontend_crash.log, so a user
// [EN]      reporting a problem can give the concrete reason.
// [ZH]   ② **前端崩溃日志** —— 异常写进 frontend_crash.log，用户报障时能给出具体原因。
//
// [EN] Why it must be installed explicitly rather than relying on default behavior:
// [EN] Avalonia lets UI-thread exceptions terminate the process by default, so the user sees
// [EN] "the program flashes and disappears" with neither a prompt nor a log; this is exactly
// [EN] the hardest class of bug report to diagnose.
// [ZH] 为什么必须显式安装而不是靠默认行为：Avalonia 的 UI 线程异常默认会让进程退出，
// [ZH] 用户看到的是“程序一闪就没了”，既没有提示也没有日志 —— 这正是最难排查的一类报障。
using System;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Media;
using Avalonia.Threading;

namespace EchoDesk.Avalonia.Services;

public static class CrashLog
{
    /// <summary>
    /// [EN] Reason for the last write failure (used by self-checks and the status line).
    /// [ZH] 没写成功时的原因（自检与状态行用）。
    /// </summary>
    public static string? LastError { get; private set; }

    /// <summary>
    /// [EN] Crash log path (under the user data directory, alongside the backend's
    /// [EN] backend_crash.log).
    /// [ZH] 崩溃日志路径（用户数据目录下，与后端的 backend_crash.log 并列）。
    /// </summary>
    public static string FilePath => Path.Combine(UserPaths.DataRoot, "frontend_crash.log");

    /// <summary>
    /// [EN] Whether the log has already been installed (to avoid hooking twice).
    /// [ZH] 日志是否已经安装过（避免重复挂钩子）。
    /// </summary>
    private static bool _installed;

    /// <summary>
    /// [EN] Installs the global exception hooks. **Idempotent**: repeated calls hook only once.
    /// [EN]
    /// [EN] All three paths are hooked; missing one would drop a class of crashes:
    /// [EN]   · UI thread exceptions (Dispatcher) -- thrown inside event handlers;
    /// [EN]   · background task exceptions (TaskScheduler) -- `async void` and Tasks that are
    /// [EN]     never awaited;
    /// [EN]   · the process fallback (AppDomain) -- whatever the two above did not catch.
    /// [ZH] 安装全局异常钩子。**幂等**：重复调用只挂一次。
    /// [ZH]
    /// [ZH] 三条路径都挂上，缺一条就会漏掉一类崩溃：
    /// [ZH]   · UI 线程异常（Dispatcher）—— 事件处理器里抛出的；
    /// [ZH]   · 后台任务异常（TaskScheduler）—— `async void` 与未 await 的 Task；
    /// [ZH]   · 进程兜底（AppDomain）—— 上面两条都没接住的。
    /// </summary>
    public static void Install()
    {
        if (_installed)
            return;
        _installed = true;

        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            Append("AppDomain", e.ExceptionObject as Exception
                                ?? new Exception(e.ExceptionObject?.ToString() ?? "未知异常"));

        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            Append("TaskScheduler", e.Exception);
            e.SetObserved();   // [EN] Marks the exception as observed so that .NET does not
                               // [EN] take the process down again.
                               // [ZH] 标记已观察，避免 .NET 再把进程带走
        };

        Dispatcher.UIThread.UnhandledException += (_, e) =>
        {
            Append("Dispatcher", e.Exception);
            // [EN] UI-thread exceptions terminate the process by default. Here the exception
            // [EN] is marked as handled and the program keeps running: one failing handler
            // [EN] should not lose the whole conversation (the user can keep working and
            // [EN] send the log).
            // [ZH] 界面线程的异常默认会终止进程。这里标记已处理并让程序继续跑：
            // [ZH] 一个处理器出错不该让整个对话丢失（用户能继续操作并把日志发出来）。
            e.Handled = true;
        };
    }

    /// <summary>
    /// [EN] Writes one log entry (append mode). On failure it only records the reason and
    /// [EN] never throws.
    /// [ZH] 写一条日志（追加）。失败只记原因，不再抛。
    /// </summary>
    public static void Append(string source, Exception ex)
    {
        try
        {
            UserPaths.EnsureDirectory(UserPaths.DataRoot);
            var builder = new StringBuilder();
            builder.AppendLine($"===== {DateTime.Now:yyyy-MM-dd HH:mm:ss} [{source}] =====");
            builder.AppendLine(ex.GetType().FullName);
            builder.AppendLine(ex.Message);
            builder.AppendLine(ex.StackTrace);
            if (ex.InnerException is not null)
            {
                builder.AppendLine("---- inner ----");
                builder.AppendLine(ex.InnerException.ToString());
            }
            builder.AppendLine();
            File.AppendAllText(FilePath, builder.ToString(), new UTF8Encoding(false));
        }
        catch (Exception writeError)
        {
            LastError = writeError.Message;
        }
    }

    /// <summary>
    /// [EN] Global exception dialog (aligned with the one at WPF `App.xaml.cs:50-69`).
    /// [EN]
    /// [EN] It uses an Avalonia window instead of MessageBox: Avalonia has no MessageBox, and
    /// [EN] "one more window in the taskbar" is exactly what users can screenshot and send.
    /// [EN] The window text is selectable (the log path must be copyable).
    /// [ZH] 全局异常提示框（对齐 WPF `App.xaml.cs:50-69` 的那一个）。
    /// [ZH]
    /// [ZH] 用 Avalonia 的窗口而不是 MessageBox：Avalonia 没有 MessageBox，
    /// [ZH] 而“任务栏里多一个窗口”正是用户能截图发出来的东西。窗口可选中文本（日志路径要能复制）。
    /// </summary>
    public static void ShowFatal(string title, string message)
    {
        try
        {
            var text = new SelectableTextBlock
            {
                Text = message,
                TextWrapping = TextWrapping.Wrap,
                MaxWidth = 520,
            };
            var window = new Window
            {
                Title = title,
                Width = 560,
                Height = 320,
                WindowStartupLocation = WindowStartupLocation.CenterScreen,
                Content = new Border
                {
                    Padding = new global::Avalonia.Thickness(16),
                    Child = new ScrollViewer { Content = text },
                },
            };
            window.Show();
        }
        catch (Exception)
        {
            // [EN] Even the dialog cannot be opened (no graphical environment): the log has
            // [EN] already been written, and that is as far as this goes.
            // [ZH] 连提示窗口都开不出来（没有图形环境）：日志已经写下了，行为到此为止
        }
    }
}

