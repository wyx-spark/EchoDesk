using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using EchoDesk.Avalonia.Services;
using EchoDesk.Avalonia.Views;

namespace EchoDesk.Avalonia;

public partial class App : Application
{
    /// <summary>当前进程的共享状态（后端服务、界面设置、语言）。</summary>
    public static AppState State { get; } = new();

    public override void Initialize() => AvaloniaXamlLoader.Load(this);

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            // 先把语言包与界面设置读起来：窗口构造里就要用到（标题、按钮文案、字号）。
            State.Initialize();
            desktop.MainWindow = new MainWindow();
            // 关窗时把后端子进程收干净：WPF 版靠 Process 的 Kill 兜底，
            // 跨平台版同样要显式做——Linux 上留下孤儿 python 进程会一直占着模型内存。
            desktop.ShutdownRequested += (_, _) => State.Backend.Dispose();
        }
        base.OnFrameworkInitializationCompleted();
    }
}
