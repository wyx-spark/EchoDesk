// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Program.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Program.cs。类型与成员说明见下方注释。
// [EN] EchoDesk cross-platform front end (Avalonia) — program entry point.
// [EN]
// [EN] Difference from the WPF version (the one most easily overlooked during migration):
// [EN] WPF opens the window automatically through StartupUri in App.xaml, whereas in
// [EN] Avalonia 12 the AppBuilder is built explicitly by Program.Main.
// [EN] A **parameterless BuildAvaloniaApp()** is kept here: both the Avalonia designer and
// [EN] the headless tests locate this method by reflection, and without it UI tests cannot
// [EN] run in unattended environments.
// [ZH] EchoDesk 跨平台前端（Avalonia）—— 程序入口。
// [ZH]
// [ZH] 与 WPF 版的差别（迁移时最容易被忽略的一条）：WPF 用 App.xaml 的 StartupUri
// [ZH] 自动开窗，Avalonia 12 里由 Program.Main 显式构建 AppBuilder。
// [ZH] 这里保留一个**无参数的 BuildAvaloniaApp()**：Avalonia 的设计器与
// [ZH] headless 测试都靠反射找这个方法，缺了它就没法在无人值守环境里跑 UI 测试。
using System;
using Avalonia;

namespace EchoDesk.Avalonia;

internal static class Program
{
    // [EN] Avalonia build entry point: both the designer and the test host depend on this
    // [EN] name and signature.
    // [ZH] Avalonia 的构建入口：设计器与测试宿主都依赖这个名字与签名。
    public static AppBuilder BuildAvaloniaApp()
        => AppBuilder.Configure<App>()
            .UsePlatformDetect()
            .WithInterFont()
            .LogToTrace();

    [STAThread]
    public static int Main(string[] args)
    {
        // [EN] Supports --selftest: no window is opened; it runs the "cross-platform startup
        // [EN] chain" checks once and returns an exit code.
        // [EN] Why it is needed: of the three major platforms only Windows can run a graphical
        // [EN] UI natively; on Linux/macOS at least "Avalonia starts + the backend launches +
        // [EN] the protocol works" must be verifiable, otherwise the only evidence for
        // [EN] cross-platform support would be "it compiles" (see the verification section of
        // [EN] records).
        // [ZH] 支持 --selftest：不开窗口，只跑一遍“跨平台启动链路”的检查并返回退出码。
        // [ZH] 为什么需要它：三大平台里只有 Windows 能在本机跑图形界面，Linux/macOS 上
        // [ZH] 至少要能验证“Avalonia 能启动 + 后端能拉起 + 协议能通”，
        // [ZH] 否则跨平台就只剩“能编译”这一条证据（见 records 的验证一节）。
        if (Array.Exists(args, a => a.Equals("--selftest", StringComparison.OrdinalIgnoreCase)))
        {
            return SelfTest.RunAsync(args).GetAwaiter().GetResult();
        }

        // [EN] Supports --probe: actually constructs and shows the window, answering "can the
        // [EN] UI open at all" with an exit code.
        // [EN] Division of labor with --selftest: selftest verifies the protocol (no window),
        // [EN] probe verifies the UI (no backend).
        // [EN] See the notes at the top of Probe.cs and tools/ui_probe_avalonia.ps1.
        // [ZH] 支持 --probe：真的构造并显示窗口，用退出码回答“界面到底能不能开”。
        // [ZH] 与 --selftest 的分工：selftest 验协议（不开窗口），probe 验界面（不拉后端）。
        // [ZH] 见 Probe.cs 顶部说明与 tools/ui_probe_avalonia.ps1。
        if (Array.Exists(args, a => a.Equals("--probe", StringComparison.OrdinalIgnoreCase)))
        {
            return Probe.Run(args);
        }
        BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);
        return 0;
    }
}
