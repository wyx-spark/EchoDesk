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

    /// <summary>
    /// 探针专用：临时把界面用的后端连接换成**探针自己持有的那一条**。
    ///
    /// 为什么需要它：stdout 是**单条共享流**，一个 <see cref="BackendService"/> 只应有
    /// 一个读泵；界面（主窗口 / 设置页）与探针同时等 ready 握手时，先读到行的会把它取走，
    /// 另一方永远等不到（实测现象："后端未完成启动即退出"，stderr 里能看到两个 python 进程）。
    /// 探针自持连接并把界面指过来，两边就用同一条连接、同一个读泵，问题从根上消失。
    ///
    /// **只在探针里赋值**（Probe.cs），正常运行时保持 null，行为与以前完全一致。
    /// </summary>
    public BackendService? ProbeBackendOverride { get; set; }

    /// <summary>界面各处应当使用的后端连接（探针优先用自己的那条）。</summary>
    public BackendService ActiveBackend => ProbeBackendOverride ?? Backend;

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

    /// <summary>
    /// 切换语言并落盘。**重复设置同一个值是空操作**。
    ///
    /// 为什么必须有这个幂等闸（实测踩到的坑）：`Loc.SetLocale` 会无条件发 `LanguageChanged`，
    /// 而设置窗口订阅了它 → 重建全部文案 → 常规页重建下拉项 → 下拉项的 SelectionChanged
    /// 又回到这里。只要"同值也照做一遍"，这就是一条同步闭环：打开设置窗口时 340 圈后
    /// 栈溢出（6868 行栈），进程直接死、窗口永远打不开。闭环入口由调用方
    /// （GeneralPage 的 _applying 重入闸）堵住，这里再堵一层 —— 将来别的页面在程序内部
    /// 刷新时误触发一次 SelectionChanged，也不会再变成栈溢出。
    ///
    /// 幂等判断**刻意放在这一层而不是 Loc.SetLocale 里**：`Loc.Reload()`（重新加载语言包）
    /// 靠 SetLocale 的通知去重建界面，那种情况"语言没变但语言包变了"也必须通知，
    /// 放到最底层会把重新加载功能一起废掉。
    /// </summary>
    public void SetLanguage(string language)
    {
        string wanted = string.IsNullOrWhiteSpace(language) ? "auto" : language.Trim();
        if (string.Equals(Settings.Language, wanted, StringComparison.OrdinalIgnoreCase))
            return;
        Settings.Language = wanted;
        Settings.Save();
        Loc.SetLocale(wanted, persist: false);
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
