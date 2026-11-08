// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/AppState.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/AppState.cs。类型与成员说明见下方注释。
// [EN] In-process shared state (corresponds to the static fields in the WPF
// [EN] App.xaml.cs plus the shared backend instance in SettingsWindow).
// [ZH] 进程内共享状态（对应 WPF 版的 App.xaml.cs 静态字段 + SettingsWindow 共享的后端实例）。
//
// [EN] Why exactly one BackendService must be shared: stdout is one shared stream, and
// [EN] giving each of the two windows its own backend would create two processes, two
// [EN] session sets, and two copies of model memory (the WPF version added a request
// [EN] gate and a single instance specifically for this).
// [ZH] 为什么必须共享一个 BackendService：stdout 是一条共享流，两个窗口各起一个后端会
// [ZH] 变成两个进程、两套会话与两份模型内存（WPF 版为此专门加了请求闸门与单实例）。
using System;
using Avalonia;
using Avalonia.Styling;

namespace EchoDesk.Avalonia.Services;

public sealed class AppState
{
    /// <summary>
    /// [EN] The backend service (unique for the whole process).
    /// [ZH] 后端服务（全进程唯一）。
    /// </summary>
    public BackendService Backend { get; } = new();

    /// <summary>
    /// [EN] Probe only: temporarily replaces the backend connection used by the UI with
    /// [EN] **the one owned by the probe itself**.
    /// [EN]
    /// [EN] Why it is needed: stdout is **a single shared stream**, and one
    /// [EN] <see cref="BackendService"/> should have exactly one read pump; when the UI
    /// [EN] (main window / settings page) and the probe both wait for the ready
    /// [EN] handshake, whichever reads a line first takes it away, and the other side
    /// [EN] waits forever (observed behavior: "the backend exited before finishing
    /// [EN] startup", with two python processes visible in stderr). The probe keeps its
    /// [EN] own connection and points the UI at it, so both sides use the same
    /// [EN] connection and the same read pump, and the problem disappears at the root.
    /// [EN]
    /// [EN] **Assigned only inside the probe** (Probe.cs); it stays null in normal runs,
    /// [EN] so behavior is exactly as before.
    /// [ZH] 探针专用：临时把界面用的后端连接换成**探针自己持有的那一条**。
    /// [ZH]
    /// [ZH] 为什么需要它：stdout 是**单条共享流**，一个 <see cref="BackendService"/> 只应有
    /// [ZH] 一个读泵；界面（主窗口 / 设置页）与探针同时等 ready 握手时，先读到行的会把它取走，
    /// [ZH] 另一方永远等不到（实测现象：“后端未完成启动即退出”，stderr 里能看到两个 python 进程）。
    /// [ZH] 探针自持连接并把界面指过来，两边就用同一条连接、同一个读泵，问题从根上消失。
    /// [ZH]
    /// [ZH] **只在探针里赋值**（Probe.cs），正常运行时保持 null，行为与以前完全一致。
    /// </summary>
    public BackendService? ProbeBackendOverride { get; set; }

    /// <summary>
    /// [EN] The backend connection that all parts of the UI should use (the probe
    /// [EN] prefers its own connection).
    /// [ZH] 界面各处应当使用的后端连接（探针优先用自己的那条）。
    /// </summary>
    public BackendService ActiveBackend => ProbeBackendOverride ?? Backend;

    /// <summary>
    /// [EN] UI settings (language/theme/font size/emoji).
    /// [ZH] 界面设置（语言/主题/字号/emoji）。
    /// </summary>
    public AppSettings Settings { get; private set; } = new();

    public PlatformInfo Platform => PlatformInfo.Current;

    /// <summary>
    /// [EN] Whether initialization has completed (prevents UI construction from
    /// [EN] happening before settings are loaded).
    /// [ZH] 是否已经完成初始化（避免界面构造早于设置加载）。
    /// </summary>
    public bool Initialized { get; private set; }

    /// <summary>
    /// [EN] Raised after appearance settings change (font size/theme); the UI
    /// [EN] subscribes and refreshes.
    /// [ZH] 设置变更（字号/主题）后触发，界面订阅它刷新。
    /// </summary>
    public event EventHandler? AppearanceChanged;

    /// <summary>
    /// [EN] Initializes at startup: load settings, configure emoji, determine the
    /// [EN] language, and apply the theme.
    /// [ZH] 启动时初始化：读设置、配 emoji、定语言、应用主题。
    /// </summary>
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

    /// <summary>
    /// [EN] Applies the theme (auto is delegated to the system/Avalonia Default).
    /// [ZH] 应用主题（auto 交给系统/Avalonia 的 Default）。
    /// </summary>
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

    /// <summary>
    /// [EN] Switches the theme and persists it.
    /// [ZH] 切换主题并落盘。
    /// </summary>
    public void SetTheme(string theme)
    {
        Settings.Theme = theme;
        Settings.Save();
        ApplyTheme();
        AppearanceChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>
    /// [EN] Switches the font size and persists it (A- / A+ / Ctrl+wheel all go through
    /// [EN] here).
    /// [ZH] 切换字号并落盘（A- / A+ / Ctrl+滚轮都走这里）。
    /// </summary>
    public void SetFontSize(double size)
    {
        Settings.FontSize = Math.Clamp(size, 10, 28);
        Settings.Save();
        AppearanceChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>
    /// [EN] Switches the language and persists it. **Setting the same value again is a
    /// [EN] no-op.**
    /// [EN]
    /// [EN] Why this idempotence gate is required (a pitfall hit in testing):
    /// [EN] `Loc.SetLocale` raises `LanguageChanged` unconditionally, and the settings
    /// [EN] window subscribes to it -> rebuild all text -> the general page rebuilds its
    /// [EN] drop-down -> the drop-down's SelectionChanged returns here. As long as
    /// [EN] "running even for the same value" is allowed, this is a synchronous loop:
    /// [EN] opening the settings window overflowed the stack after 340 rounds (6868
    /// [EN] stack lines), killing the process so the window never opened. The loop entry
    /// [EN] is blocked by the caller (the _applying reentrancy gate in GeneralPage), and
    /// [EN] another layer is added here -- if some other page accidentally triggers
    /// [EN] SelectionChanged during an internal refresh in the future, it will not
    /// [EN] become a stack overflow either.
    /// [EN]
    /// [EN] The idempotence check is **deliberately placed at this layer rather than
    /// [EN] inside Loc.SetLocale**: `Loc.Reload()` (reload the language pack) relies on
    /// [EN] SetLocale's notification to rebuild the UI, and in that case "the language
    /// [EN] did not change but the language pack did" must still notify; putting the
    /// [EN] check at the lowest layer would disable the reload feature as well.
    /// [ZH] 切换语言并落盘。**重复设置同一个值是空操作**。
    /// [ZH]
    /// [ZH] 为什么必须有这个幂等闸（实测踩到的坑）：`Loc.SetLocale` 会无条件发 `LanguageChanged`，
    /// [ZH] 而设置窗口订阅了它 → 重建全部文案 → 常规页重建下拉项 → 下拉项的 SelectionChanged
    /// [ZH] 又回到这里。只要“同值也照做一遍”，这就是一条同步闭环：打开设置窗口时 340 圈后
    /// [ZH] 栈溢出（6868 行栈），进程直接死、窗口永远打不开。闭环入口由调用方
    /// [ZH] （GeneralPage 的 _applying 重入闸）堵住，这里再堵一层 —— 将来别的页面在程序内部
    /// [ZH] 刷新时误触发一次 SelectionChanged，也不会再变成栈溢出。
    /// [ZH]
    /// [ZH] 幂等判断**刻意放在这一层而不是 Loc.SetLocale 里**：`Loc.Reload()`（重新加载语言包）
    /// [ZH] 靠 SetLocale 的通知去重建界面，那种情况“语言没变但语言包变了”也必须通知，
    /// [ZH] 放到最底层会把重新加载功能一起废掉。
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

    /// <summary>
    /// [EN] Switches the emoji preference and persists it.
    /// [ZH] 切换 emoji 偏好并落盘。
    /// </summary>
    public void SetEmojiPreference(string preference)
    {
        Settings.EmojiPreference = preference;
        Settings.Save();
        Emoji.Configure(preference);
        AppearanceChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>
    /// [EN] Wraps up before exit: writes the UI settings back (window size, etc.).
    /// [ZH] 退出前收尾：把界面设置写回（窗口尺寸等）。
    /// </summary>
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
