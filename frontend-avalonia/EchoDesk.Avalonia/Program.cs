// EchoDesk 跨平台前端（Avalonia）—— 程序入口。
//
// 与 WPF 版的差别（迁移时最容易被忽略的一条）：WPF 用 App.xaml 的 StartupUri
// 自动开窗，Avalonia 12 里由 Program.Main 显式构建 AppBuilder。
// 这里保留一个**无参数的 BuildAvaloniaApp()**：Avalonia 的设计器与
// headless 测试都靠反射找这个方法，缺了它就没法在无人值守环境里跑 UI 测试。
using System;
using Avalonia;

namespace EchoDesk.Avalonia;

internal static class Program
{
    // Avalonia 的构建入口：设计器与测试宿主都依赖这个名字与签名。
    public static AppBuilder BuildAvaloniaApp()
        => AppBuilder.Configure<App>()
            .UsePlatformDetect()
            .WithInterFont()
            .LogToTrace();

    [STAThread]
    public static int Main(string[] args)
    {
        // 支持 --selftest：不开窗口，只跑一遍"跨平台启动链路"的检查并返回退出码。
        // 为什么需要它：三大平台里只有 Windows 能在本机跑图形界面，Linux/macOS 上
        // 至少要能验证"Avalonia 能启动 + 后端能拉起 + 协议能通"，
        // 否则跨平台就只剩"能编译"这一条证据（见 records 的验证一节）。
        if (Array.Exists(args, a => a.Equals("--selftest", StringComparison.OrdinalIgnoreCase)))
        {
            return SelfTest.RunAsync(args).GetAwaiter().GetResult();
        }

        // 支持 --probe：真的构造并显示窗口，用退出码回答"界面到底能不能开"。
        // 与 --selftest 的分工：selftest 验协议（不开窗口），probe 验界面（不拉后端）。
        // 见 Probe.cs 顶部说明与 tools/ui_probe_avalonia.ps1。
        if (Array.Exists(args, a => a.Equals("--probe", StringComparison.OrdinalIgnoreCase)))
        {
            return Probe.Run(args);
        }
        BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);
        return 0;
    }
}
