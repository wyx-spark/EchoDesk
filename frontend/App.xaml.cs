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
        /// 前端崩溃日志路径（程序目录\frontend_crash.log）。
        /// 与后端的 backend_crash.log 并列，便于用户反馈问题时一并提供。
        /// </summary>
        public static string CrashLogPath =>
            Path.Combine(AppContext.BaseDirectory, "frontend_crash.log");

        /// <summary>连续未处理异常的安全上限：超过后不再拦截，避免程序带病运行。</summary>
        private const int MaxHandledUnhandled = 5;

        protected override void OnStartup(StartupEventArgs e)
        {            // 说明（v0.2.0 修复一）：此前前端没有任何全局异常处理，设置窗口构造中的
            // NullReferenceException 会直接终止进程——用户看到的是「界面无响应，只能从
            // 任务管理器结束」，且不留任何线索。这里记录异常并尽量让程序继续可用。
            DispatcherUnhandledException += OnDispatcherUnhandledException;
            AppDomain.CurrentDomain.UnhandledException += (_, args) =>
                Log("AppDomain.UnhandledException", args.ExceptionObject as Exception);
            TaskScheduler.UnobservedTaskException += (_, args) =>
            {
                Log("TaskScheduler.UnobservedTaskException", args.Exception);
                args.SetObserved();
            };
            base.OnStartup(e);
        }

        /// <summary>UI 线程未处理异常：写日志 + 提示用户，并让程序继续运行（不静默退出）。</summary>
        private void OnDispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
        {
            Log("DispatcherUnhandledException", e.Exception);
            if (++_unhandledCount > MaxHandledUnhandled)
            {
                // 反复出错说明状态已不可信，交回系统终止（仍然留了日志）
                MessageBox.Show(
                    $"EchoDesk 连续发生 {_unhandledCount} 次内部错误，为避免数据异常将退出。\n" +
                    $"错误日志：{CrashLogPath}",
                    "EchoDesk 内部错误", MessageBoxButton.OK, MessageBoxImage.Error);
                return;  // 不设置 Handled → 进程终止
            }

            e.Handled = true;
            MessageBox.Show(
                "界面发生了一次内部错误，已记录日志并尝试继续运行。\n" +
                "若功能异常，请重启 EchoDesk 并把日志提供给开发者。\n\n" +
                $"错误：{e.Exception?.Message}\n日志：{CrashLogPath}",
                "EchoDesk 内部错误", MessageBoxButton.OK, MessageBoxImage.Warning);
        }

        /// <summary>把异常（含内部异常与堆栈）追加写入前端崩溃日志。</summary>
        internal static void Log(string tag, Exception? ex)
        {            try
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
                        sb.AppendLine("  --- 内部异常 ---");
                }
                sb.AppendLine();
                lock (LogLock)
                    File.AppendAllText(CrashLogPath, sb.ToString(), new UTF8Encoding(false));
            }
            catch
            {
                // 日志失败不能再抛异常（否则递归）
            }
        }
    }
}
