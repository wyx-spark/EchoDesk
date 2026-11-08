// [EN] EchoDesk WPF frontend module: frontend/Services/ThemeService.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Services/ThemeService.cs。类型与成员说明见下方注释。
using System.Windows;
using Microsoft.Win32;

namespace EchoDesk.Services;

/// <summary>
/// [EN] UI theme: follow system / light / dark (the design specification, fix two).
/// [ZH] 界面主题：跟随系统 / 浅色 / 深色（设计规范 修复二）。
/// </summary>
public enum AppTheme
{
    /// <summary>
    /// [EN] Follows the Windows "app mode" setting and switches automatically when the system
    /// [EN] does.
    /// [ZH] 跟随 Windows 的"应用模式"设置，系统切换时自动跟随。
    /// </summary>
    System,
    Light,
    Dark,
}

/// <summary>
/// [EN] Theme service (fix two).
///
/// [EN] There were two problems before the fix, both solved here:
/// [EN] 1. <c>MainWindow_Loaded</c> called <c>ApplyTheme</c> only when "configured as light",
/// [EN] so with a dark configuration the startup UI still used the light dictionary merged in
/// [EN] App.xaml, and dark could never appear;
/// [EN] 2. There was no "follow system" option and no single source of theme state (windows
/// [EN] each kept their own <c>_settings.DarkMode</c>).
///
/// [EN] Today the current theme is centralized in this class: every switch goes through
/// [EN] <see cref="Apply"/>, which replaces the theme dictionary in
/// [EN] <c>Application.Current.Resources.MergedDictionaries</c> and broadcasts
/// [EN] <see cref="Changed"/> so that windows refresh the content they must rebuild in
/// [EN] code (brushes inside Markdown documents, brushes of dynamically created
/// [EN] controls, and so on).
/// [ZH] 主题服务（修复二）。
///
/// [ZH] 修复前的问题有两个，都在这里解决：
/// [ZH] 1. <c>MainWindow_Loaded</c> 只在"配置为浅色"时才调 <c>ApplyTheme</c>，
/// [ZH]    于是配置为深色时启动界面仍是 App.xaml 里合并的浅色字典 —— 深色永远出不来；
/// [ZH] 2. 没有"跟随系统"选项，也没有主题状态单一来源（窗口各自记
/// [ZH]    <c>_settings.DarkMode</c>）。
///
/// [ZH] 现在的做法：把"当前主题"收敛到本类，任何切换都走 <see cref="Apply"/>，
/// [ZH] 由它替换 <c>Application.Current.Resources.MergedDictionaries</c> 中的主题字典，
/// [ZH] 并广播 <see cref="Changed"/> 让各窗口刷新那些必须用代码重建的内容
/// [ZH] （Markdown 文档里的画刷、动态生成控件的画刷等）。
/// </summary>
public static class ThemeService
{
    /// <summary>
    /// [EN] Source prefix of the theme dictionary in App.xaml (dictionaries are matched by it
    /// [EN] when swapping).
    /// [ZH] 主题字典在 App.xaml 中的来源前缀（换字典时按它匹配）。
    /// </summary>
    private const string ThemeSourceMarker = "Resources/Theme.";

    private static bool _applied;

    /// <summary>
    /// [EN] Raised after the theme changes (a switch or a system theme change); the parameter
    /// [EN] is the theme actually in effect.
    /// [ZH] 主题发生变化（切换或系统主题变化）后触发；参数为生效后的实际主题。
    /// </summary>
    public static event Action<AppTheme>? Changed;

    /// <summary>
    /// [EN] The theme chosen by the user (may be "follow system").
    /// [ZH] 用户选择的主题（可能是"跟随系统"）。
    /// </summary>
    public static AppTheme Selected { get; private set; } = AppTheme.System;

    /// <summary>
    /// [EN] The theme actually in effect ("follow system" resolves to Light or Dark).
    /// [ZH] 当前实际生效的主题（"跟随系统"会解析成 Light 或 Dark）。
    /// </summary>
    public static AppTheme Effective { get; private set; } = AppTheme.Light;

    /// <summary>
    /// [EN] Whether dark mode is active (for decisions in code, such as image inversion).
    /// [ZH] 是否处于深色（供代码里判断，例如图片反色）。
    /// </summary>
    public static bool IsDark => Effective == AppTheme.Dark;

    /// <summary>
    /// [EN] Parses a persisted string into a theme; returns "follow system" when it cannot be
    /// [EN] recognized.
    /// [ZH] 把持久化的字符串解析成主题；无法识别时返回"跟随系统"。
    /// </summary>
    public static AppTheme Parse(string? value) => (value ?? "").Trim().ToLowerInvariant() switch
    {
        "dark" => AppTheme.Dark,
        "light" => AppTheme.Light,
        _ => AppTheme.System,
    };

    /// <summary>
    /// [EN] The persisted string for a theme.
    /// [ZH] 主题的持久化字符串。
    /// </summary>
    public static string Serialize(AppTheme theme) => theme switch
    {
        AppTheme.Dark => "dark",
        AppTheme.Light => "light",
        _ => "auto",
    };

    /// <summary>
    /// [EN] Determines whether the system is currently dark from the Windows "app mode"
    /// [EN] registry value.
    /// [ZH] 按 Windows 的"应用模式"注册表项判断系统当前是否为深色。
    /// </summary>
    public static AppTheme SystemTheme()
    {
        try
        {
            using RegistryKey? key = Registry.CurrentUser.OpenSubKey(
                @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
            // [EN] AppsUseLightTheme=0 means dark; a value of 1 or an absent value means light.
            // [ZH] AppsUseLightTheme=0 表示深色；值为 1 或缺省表示浅色
            object? value = key?.GetValue("AppsUseLightTheme");
            if (value is int light)
                return light == 0 ? AppTheme.Dark : AppTheme.Light;
        }
        catch
        {
            // [EN] The registry is unreadable (restricted environment): treat it as light,
            // [EN] which does not affect startup.
            // [ZH] 注册表不可读（受限环境）：按浅色处理，不影响启动
        }
        return AppTheme.Light;
    }

    /// <summary>
    /// [EN] Resolves what the "selected theme" will actually render as.
    /// [ZH] 解析"选择的主题"实际会渲染成什么。
    /// </summary>
    public static AppTheme Resolve(AppTheme selected)
        => selected == AppTheme.System ? SystemTheme() : selected;

    /// <summary>
    /// [EN] Applies a theme. It can be called once at startup, and afterwards by the settings
    /// [EN] window.
    /// [ZH] 应用主题。可在启动时调用一次，之后由设置窗口调用。
    /// </summary>
    /// <param name="selected">
    /// [EN] The theme chosen by the user (including "follow system").
    /// [ZH] 用户选择的主题（含"跟随系统"）。
    /// </param>
    /// <param name="notify">
    /// [EN] Whether to broadcast <see cref="Changed"/>.
    /// [ZH] 是否广播 <see cref="Changed"/>。
    /// </param>
    public static void Apply(AppTheme selected, bool notify = true)
    {
        AppTheme effective = Resolve(selected);
        bool same = _applied && effective == Effective && selected == Selected;
        Selected = selected;
        Effective = effective;
        _applied = true;

        SwapDictionary(effective);
        if (notify && !same)
            Changed?.Invoke(effective);
    }

    /// <summary>
    /// [EN] Subscribes to system theme changes. Windows raises UserPreferenceChanged when the
    /// [EN] user changes "app mode", but that event does not say what changed, so the registry
    /// [EN] is read again and compared.
    /// [ZH] 订阅系统主题变化。Windows 在用户改"应用模式"时发 UserPreferenceChanged，
    /// [ZH] 但该事件不区分具体改了什么，因此重新读注册表比对。
    /// </summary>
    public static void HookSystemChanges()
    {
        try
        {
            SystemEvents.UserPreferenceChanged += (_, _) =>
            {
                if (Selected != AppTheme.System)
                    return;
                AppTheme effective = SystemTheme();
                if (effective == Effective)
                    return;
                // [EN] The event may arrive on a non-UI thread; switch back to the UI thread
                // [EN] before touching the resource dictionaries.
                // [ZH] 事件可能在非 UI 线程到达，切回 UI 线程再动资源字典
                Application.Current?.Dispatcher.BeginInvoke(new Action(() => Apply(AppTheme.System)));
            };
        }
        catch
        {
            // [EN] SystemEvents throws in extremely restricted environments (no message pump
            // [EN] permission); manual switching is unaffected.
            // [ZH] SystemEvents 在极端受限的环境会抛异常（无消息泵权限），不影响手动切换
        }
    }

    /// <summary>
    /// [EN] Replaces the theme dictionary among the merged dictionaries (keeping the other
    /// [EN] dictionaries and their order).
    /// [ZH] 替换合并字典中的主题字典（保留其它字典与其顺序）。
    /// </summary>
    private static void SwapDictionary(AppTheme effective)
    {
        ResourceDictionary? resources = Application.Current?.Resources;
        if (resources is null)
            return;
        var merged = resources.MergedDictionaries;
        var theme = merged.FirstOrDefault(
            d => d.Source?.OriginalString?.Contains(ThemeSourceMarker, StringComparison.Ordinal) == true);
        int index = theme is null ? 0 : merged.IndexOf(theme);
        var replacement = new ResourceDictionary
        {
            Source = new Uri(
                $"{ThemeSourceMarker}{(effective == AppTheme.Dark ? "Dark" : "Light")}.xaml",
                UriKind.Relative),
        };
        if (index < merged.Count)
            merged[index] = replacement;
        else
            merged.Add(replacement);
    }

    /// <summary>
    /// [EN] Binds a control property to a palette key so that it follows theme switches
    /// [EN] automatically.
    ///
    /// [EN] This method must be used instead of <c>TryFindResource</c>: the latter is a **value
    /// [EN] snapshot**, so message bubbles created before a theme switch keep the old color (in
    /// [EN] testing this showed up as "light remnants after switching").
    /// [ZH] 把控件属性绑定到色板键上，切主题时自动跟随。
    ///
    /// [ZH] 必须用这个方法而不是 <c>TryFindResource</c>：后者是**取值快照**，
    /// [ZH] 主题切换后已创建的消息气泡会保留旧色（实测中表现为"切换后仍有浅色残留"）。
    /// </summary>
    public static void Bind(FrameworkElement target, DependencyProperty property, string key)
        => target.SetResourceReference(property, key);

    /// <summary>
    /// [EN] Same as above, for document content elements such as
    /// [EN] <c>Paragraph</c>/<c>Run</c>/<c>Hyperlink</c>.
    /// [ZH] 同上，用于 <c>Paragraph</c>/<c>Run</c>/<c>Hyperlink</c> 等文档内容元素。
    /// </summary>
    public static void Bind(FrameworkContentElement target, DependencyProperty property, string key)
        => target.SetResourceReference(property, key);

    /// <summary>
    /// [EN] Gets the current brush for a palette key (a value snapshot).
    ///
    /// [EN] Only for cases where the brush must be passed out as a value (for example binding a
    /// [EN] Brush property in a list item template, or comparing colors in code). **Use
    /// [EN] <see cref="Bind"/> wherever binding is possible**; a snapshot does not follow theme
    /// [EN] changes, so the receiver must rebuild it when the theme switches.
    /// [ZH] 按色板键取当前画刷（取值快照）。
    ///
    /// [ZH] 仅用于"必须把画刷作为值传出去"的场合（例如列表项模板里绑定 Brush 属性、
    /// [ZH] 或需要在代码里比较颜色）。**能绑定的场合请用 <see cref="Bind"/>** ——
    /// [ZH] 快照不会跟着主题变，取值方必须在主题切换时重建。
    /// </summary>
    public static System.Windows.Media.Brush Brush(string key)
        => Application.Current?.TryFindResource(key) as System.Windows.Media.Brush
           ?? System.Windows.Media.Brushes.Gray;
}
