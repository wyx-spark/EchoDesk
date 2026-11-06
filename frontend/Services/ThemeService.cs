// [EN] EchoDesk WPF frontend module: frontend/Services/ThemeService.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Services/ThemeService.cs。类型与成员说明见下方注释。
using System.Windows;
using Microsoft.Win32;

namespace EchoDesk.Services;

/// <summary>界面主题：跟随系统 / 浅色 / 深色（任务书修复二）。</summary>
public enum AppTheme
{
    /// <summary>跟随 Windows 的"应用模式"设置，系统切换时自动跟随。</summary>
    System,
    Light,
    Dark,
}

/// <summary>
/// 主题服务（修复二）。
///
/// 修复前的问题有两个，都在这里解决：
/// 1. <c>MainWindow_Loaded</c> 只在"配置为浅色"时才调 <c>ApplyTheme</c>，
///    于是配置为深色时启动界面仍是 App.xaml 里合并的浅色字典 —— 深色永远出不来；
/// 2. 没有"跟随系统"选项，也没有主题状态单一来源（窗口各自记 <c>_settings.DarkMode</c>）。
///
/// 现在的做法：把"当前主题"收敛到本类，任何切换都走 <see cref="Apply"/>，
/// 由它替换 <c>Application.Current.Resources.MergedDictionaries</c> 中的主题字典，
/// 并广播 <see cref="Changed"/> 让各窗口刷新那些必须用代码重建的内容
/// （Markdown 文档里的画刷、动态生成控件的画刷等）。
/// </summary>
public static class ThemeService
{
    /// <summary>主题字典在 App.xaml 中的来源前缀（换字典时按它匹配）。</summary>
    private const string ThemeSourceMarker = "Resources/Theme.";

    private static bool _applied;

    /// <summary>主题发生变化（切换或系统主题变化）后触发；参数为生效后的实际主题。</summary>
    public static event Action<AppTheme>? Changed;

    /// <summary>用户选择的主题（可能是"跟随系统"）。</summary>
    public static AppTheme Selected { get; private set; } = AppTheme.System;

    /// <summary>当前实际生效的主题（"跟随系统"会解析成 Light 或 Dark）。</summary>
    public static AppTheme Effective { get; private set; } = AppTheme.Light;

    /// <summary>是否处于深色（供代码里判断，例如图片反色）。</summary>
    public static bool IsDark => Effective == AppTheme.Dark;

    /// <summary>把持久化的字符串解析成主题；无法识别时返回"跟随系统"。</summary>
    public static AppTheme Parse(string? value) => (value ?? "").Trim().ToLowerInvariant() switch
    {
        "dark" => AppTheme.Dark,
        "light" => AppTheme.Light,
        _ => AppTheme.System,
    };

    /// <summary>主题的持久化字符串。</summary>
    public static string Serialize(AppTheme theme) => theme switch
    {
        AppTheme.Dark => "dark",
        AppTheme.Light => "light",
        _ => "auto",
    };

    /// <summary>按 Windows 的"应用模式"注册表项判断系统当前是否为深色。</summary>
    public static AppTheme SystemTheme()
    {
        try
        {
            using RegistryKey? key = Registry.CurrentUser.OpenSubKey(
                @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
            // AppsUseLightTheme=0 表示深色；值为 1 或缺省表示浅色
            object? value = key?.GetValue("AppsUseLightTheme");
            if (value is int light)
                return light == 0 ? AppTheme.Dark : AppTheme.Light;
        }
        catch
        {
            // 注册表不可读（受限环境）：按浅色处理，不影响启动
        }
        return AppTheme.Light;
    }

    /// <summary>解析"选择的主题"实际会渲染成什么。</summary>
    public static AppTheme Resolve(AppTheme selected)
        => selected == AppTheme.System ? SystemTheme() : selected;

    /// <summary>
    /// 应用主题。可在启动时调用一次，之后由设置窗口调用。
    /// </summary>
    /// <param name="selected">用户选择的主题（含"跟随系统"）。</param>
    /// <param name="notify">是否广播 <see cref="Changed"/>。</param>
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
    /// 订阅系统主题变化。Windows 在用户改"应用模式"时发 UserPreferenceChanged，
    /// 但该事件不区分具体改了什么，因此重新读注册表比对。
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
                // 事件可能在非 UI 线程到达，切回 UI 线程再动资源字典
                Application.Current?.Dispatcher.BeginInvoke(new Action(() => Apply(AppTheme.System)));
            };
        }
        catch
        {
            // SystemEvents 在极端受限的环境会抛异常（无消息泵权限），不影响手动切换
        }
    }

    /// <summary>替换合并字典中的主题字典（保留其它字典与其顺序）。</summary>
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
    /// 把控件属性绑定到色板键上，切主题时自动跟随。
    ///
    /// 必须用这个方法而不是 <c>TryFindResource</c>：后者是**取值快照**，
    /// 主题切换后已创建的消息气泡会保留旧色（实测中表现为"切换后仍有浅色残留"）。
    /// </summary>
    public static void Bind(FrameworkElement target, DependencyProperty property, string key)
        => target.SetResourceReference(property, key);

    /// <summary>同上，用于 <c>Paragraph</c>/<c>Run</c>/<c>Hyperlink</c> 等文档内容元素。</summary>
    public static void Bind(FrameworkContentElement target, DependencyProperty property, string key)
        => target.SetResourceReference(property, key);

    /// <summary>
    /// 按色板键取当前画刷（取值快照）。
    ///
    /// 仅用于"必须把画刷作为值传出去"的场合（例如列表项模板里绑定 Brush 属性、
    /// 或需要在代码里比较颜色）。**能绑定的场合请用 <see cref="Bind"/>** ——
    /// 快照不会跟着主题变，取值方必须在主题切换时重建。
    /// </summary>
    public static System.Windows.Media.Brush Brush(string key)
        => Application.Current?.TryFindResource(key) as System.Windows.Media.Brush
           ?? System.Windows.Media.Brushes.Gray;
}
