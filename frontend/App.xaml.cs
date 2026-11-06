// [EN] WPF application entry point: startup, localization initialization, and
// [EN] frontend crash logging.
// [ZH] WPF 应用入口：启动、语言初始化与前端崩溃日志。
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Threading;

namespace EchoDesk
{
    public partial class App : Application
    {
        private static readonly object LogLock = new();
        private static int _unhandledCount;

        /// <summary>
        /// [EN] Frontend crash log path (frontend_crash.log in the program
        /// directory). It is parallel to the backend crash log and can be
        /// provided together when reporting an issue.
        /// [ZH] 前端崩溃日志路径（程序目录下的 frontend_crash.log）。与后端崩溃日志
        /// 并列，便于用户反馈问题时一并提供。
        /// </summary>
        public static string CrashLogPath =>
            Path.Combine(AppContext.BaseDirectory, "frontend_crash.log");

        /// <summary>
        /// [EN] Safety limit for consecutive unhandled exceptions. Once exceeded,
        /// exceptions are no longer intercepted so the process can terminate.
        /// [ZH] 连续未处理异常的安全上限。超过后不再拦截，以避免程序带病运行。
        /// </summary>
        private const int MaxHandledUnhandled = 5;

        protected override void OnStartup(StartupEventArgs e)
        {
            // [EN] Before global handling was added, an exception during window
            // [EN] construction could terminate the process without any
            // [EN] diagnostic trace. Exceptions are now logged and the
            // [EN] application is kept usable where possible.
            // [ZH] 在加入全局异常处理之前，窗口构造期间的异常可能直接终止进程，且不留下
            // [ZH] 任何诊断线索。现在会记录异常，并在可能的情况下保持程序可用。
            DispatcherUnhandledException += OnDispatcherUnhandledException;
            AppDomain.CurrentDomain.UnhandledException += (_, args) =>
                Log("AppDomain.UnhandledException", args.ExceptionObject as Exception);
            TaskScheduler.UnobservedTaskException += (_, args) =>
            {
                Log("TaskScheduler.UnobservedTaskException", args.Exception);
                args.SetObserved();
            };
            // [EN] Localization must be initialized before any window is created;
            // [EN] otherwise MainWindow would read the default language.
            // [ZH] 语言必须在任何窗口创建之前初始化，否则 MainWindow 读取到的仍是
            // [ZH] 默认语言。
            try
            {
                EchoDesk.Services.Loc.Initialize(Services.AppSettings.Load().Language);
            }
            catch (Exception ex)
            {
                // [EN] A damaged language pack must not prevent startup.
                // [ZH] 语言包损坏不应阻止启动。
                Log("App.Loc.Initialize", ex);
            }
            base.OnStartup(e);
        }

        /// <summary>
        /// [EN] Handle an unhandled UI-thread exception: log it, inform the user,
        /// and keep the application running when possible.
        /// [ZH] 处理 UI 线程未处理异常：记录日志、提示用户，并在可能时保持程序运行。
        /// </summary>
        private void OnDispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
        {
            Log("DispatcherUnhandledException", e.Exception);
            if (++_unhandledCount > MaxHandledUnhandled)
            {
                // [EN] Repeated failures indicate that application state is no
                // [EN] longer trustworthy; allow the system to terminate the process.
                // [ZH] 反复失败表明应用状态已不可信，交由系统终止进程。
                MessageBox.Show(
                    $"EchoDesk 连续发生 {_unhandledCount} 次内部错误，为避免数据异常将退出。\n" +
                    $"错误日志：{CrashLogPath}",
                    "EchoDesk 内部错误", MessageBoxButton.OK, MessageBoxImage.Error);
                // [EN] Do not set Handled, allowing the process to terminate.
                // [ZH] 不设置 Handled，使进程终止。
                return;
            }

            e.Handled = true;
            MessageBox.Show(
                "界面发生了一次内部错误，已记录日志并尝试继续运行。\n" +
                "若功能异常，请重启 EchoDesk 并把日志提供给开发者。\n\n" +
                $"错误：{e.Exception?.Message}\n日志：{CrashLogPath}",
                "EchoDesk 内部错误", MessageBoxButton.OK, MessageBoxImage.Warning);
        }

        /// <summary>
        /// [EN] Append an exception, including inner exceptions and stack traces,
        /// to the frontend crash log.
        /// [ZH] 将异常（包括内部异常与堆栈）追加写入前端崩溃日志。
        /// </summary>
        internal static void Log(string tag, Exception? ex)
        {
            try
            {
                var sb = new StringBuilder();
                sb.AppendLine($"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {tag}");
                Exception? current = ex;
                int depth = 0;
                while (current is not null && depth < 6)
                {
                    sb.AppendLine($"  {current.GetType().FullName}: {current.Message}");
                    if (!string.IsNullOrWhiteSpace(current.StackTrace))
                        sb.AppendLine(current.StackTrace);
                    current = current.InnerException;
                    depth++;
                    if (current is not null)
                        // [EN] Separate inner-exception details for readability.
                        // [ZH] 以分隔线标出内部异常，便于阅读。
                        sb.AppendLine("  --- 内部异常 ---");
                }
                sb.AppendLine();
                lock (LogLock)
                    File.AppendAllText(CrashLogPath, sb.ToString(), new UTF8Encoding(false));
            }
            catch
            {
                // [EN] Logging failure must not throw again, which would recurse.
                // [ZH] 日志写入失败不得再次抛出异常，否则会递归。
            }
        }
    }
}
