// 进程内共享状态（对应 WPF 版的 App.xaml.cs 静态字段 + SettingsWindow 共享的后端实例）。
//
// 为什么必须共享一个 BackendService：stdout 是一条共享流，两个窗口各起一个后端会
// 变成两个进程、两套会话与两份模型内存（WPF 版为此专门加了请求闸门与单实例）。
using System;
using Avalonia;
using Avalonia.Styling;

namespace EchoDesk.Avalonia.Services;

public sealed class AppState
{
    /// <summary>后端服务（全进程唯一）。</summary>
    public BackendService Backend { get; } = new();

    /// <summary>界面设置（语言/主题/字号/emoji）。</summary>
    public AppSettings Settings { get; private set; } = new();

    public PlatformInfo Platform => PlatformInfo.Current;

    /// <summary>是否已经完成初始化（避免界面构造早于设置加载）。</summary>
    public bool Initialized { get; private set; }

    /// <summary>设置变更（字号/主题）后触发，界面订阅它刷新。</summary>
    public event EventHandler? AppearanceChanged;

    /// <summary>启动时初始化：读设置、配 emoji、定语言、应用主题。</summary>
    public void Initialize()
    {
        if (Initialized)
            return;
        Settings = AppSettings.Load();
        Emoji.Configure(Settings.EmojiPreference);
        Loc.Initialize(Settings.Language);
        ApplyTheme();
        Initialized = true;
    }

    /// <summary>应用主题（auto 交给系统/Avalonia 的 Default）。</summary>
    public void ApplyTheme()
    {
        if (Application.Current is null)
            return;
        Application.Current.RequestedThemeVariant = Settings.Theme switch
        {
            "light" => ThemeVariant.Light,
            "dark" => ThemeVariant.Dark,
            _ => ThemeVariant.Default,
        };
    }

    /// <summary>切换主题并落盘。</summary>
    public void SetTheme(string theme)
    {
        Settings.Theme = theme;
        Settings.Save();
        ApplyTheme();
        AppearanceChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>切换字号并落盘（A- / A+ / Ctrl+滚轮都走这里）。</summary>
    public void SetFontSize(double size)
    {
        Settings.FontSize = Math.Clamp(size, 10, 28);
        Settings.Save();
        AppearanceChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>切换语言并落盘。</summary>
    public void SetLanguage(string language)
    {
        Settings.Language = language;
        Settings.Save();
        Loc.SetLocale(language, persist: false);
        AppearanceChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>切换 emoji 偏好并落盘。</summary>
    public void SetEmojiPreference(string preference)
    {
        Settings.EmojiPreference = preference;
        Settings.Save();
        Emoji.Configure(preference);
        AppearanceChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>退出前收尾：把界面设置写回（窗口尺寸等）。</summary>
    public void Shutdown()
    {
        try
        {
            Settings.Save();
        }
        catch (Exception)
        {
        }
        Backend.Dispose();
    }
}
