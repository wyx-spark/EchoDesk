// [EN] EchoDesk WPF frontend module: frontend/Services/Localization.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Services/Localization.cs。类型与成员说明见下方注释。
using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;

namespace EchoDesk.Services;

/// <summary>
/// 前端多语言（任务书 任务三）。
///
/// 设计要点：
/// <list type="bullet">
/// <item><b>语言包位置</b>：<c>lang/&lt;locale&gt;.json</c>（发布目录根下），
/// 找不到时回退到程序集的 <c>Resources/Lang/</c>。用户可以自己往 <c>lang/</c> 里丢
/// 一个新语言包，不必重新编译（社区贡献语言包的路径）。</item>
/// <item><b>回退链</b>（任务书 3.4）：当前语言 → en → zh-Hans → 键名本身。
/// 返回键名本身是刻意的：缺键时界面上会显眼地出现键名，而不是空白。</item>
/// <item><b>按需加载</b>（任务书 3.9）：只把当前语言与回退链上的语言读进内存。</item>
/// <item><b>即时刷新</b>：<see cref="Apply"/> 按 XAML 里声明的
/// <c>Loc.Key</c> / <c>Loc.HelpKey</c> / <c>Loc.TipKey</c> 附加属性回填文本；
/// 切换语言时对所有已打开窗口重跑一遍即可，无需重启。</item>
/// </list>
///
/// 注意：<b>不要把动态状态文本</b>（如"已就绪"、上下文占用）写成 <c>Loc.Key</c>，
/// 那会被下一次 <see cref="Apply"/> 覆盖；这类文本用 <see cref="T(string)"/> 在代码里取，
/// 需要给屏幕阅读器补充说明时用 <c>Loc.HelpKey</c>（不能用 AutomationProperties.Name，
/// 它会覆盖真实可读文本，v0.2.0 的排查经验）。
/// </summary>
public sealed class Loc
{
    /// <summary>源语言：所有键的权威来源，也是最终回退目标。</summary>
    public const string SourceLocale = "zh-Hans";

    /// <summary>全局回退顺序：先英文，再中文。</summary>
    private static readonly string[] FallbackChain = { "en", "zh-Hans" };

    /// <summary>语言展示名（语言名一律用该语言自己的写法）。</summary>
    private static readonly Dictionary<string, string> LocaleNames = new(StringComparer.OrdinalIgnoreCase)
    {
        ["zh-Hans"] = "简体中文",
        ["zh-Hant"] = "繁體中文",
        ["en"] = "English",
        ["ja"] = "日本語",
        ["ko"] = "한국어",
        ["de"] = "Deutsch",
        ["fr"] = "Français",
        ["es"] = "Español",
        ["pt-BR"] = "Português (Brasil)",
        ["ru"] = "Русский",
        ["ar"] = "العربية",
    };

    private static readonly object Gate = new();
    private static readonly Dictionary<string, Dictionary<string, string>> Packs = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>当前生效的语言。</summary>
    public static string Locale { get; private set; } = SourceLocale;

    /// <summary>用户请求的语言（可能是 "auto"）。</summary>
    public static string Requested { get; private set; } = "auto";

    /// <summary>语言切换后触发；各窗口据此重跑 <see cref="Apply"/>。</summary>
    public static event EventHandler? LanguageChanged;

    /// <summary>支持的界面语言展示名（供设置窗口的下拉框）。</summary>
    public static string DisplayName(string locale) =>
        LocaleNames.TryGetValue(locale, out string? name) ? name : locale;

    // ---------------------------------------------------------------
    // 语言包位置与加载
    // ---------------------------------------------------------------

    /// <summary>语言包搜索目录（按优先级）：程序目录 <c>lang/</c>、程序集目录 <c>lang/</c> 与 <c>Resources/Lang/</c>。</summary>
    public static string LangDirectory => LangDirectories().FirstOrDefault(Directory.Exists) ?? LangDirectories().First();

    /// <summary>列出实际可用的语言（内置 + 用户外置语言包）。</summary>
    public static List<string> AvailableLocales()
    {
        var found = new List<string>();
        foreach (string dir in LangDirectories())
        {
            try
            {
                if (!Directory.Exists(dir))
                    continue;
                foreach (string file in Directory.GetFiles(dir, "*.json"))
                {
                    string name = Path.GetFileNameWithoutExtension(file);
                    // 模块分片（<locale>.<模块>.json）不是独立语言，跳过
                    int dot = name.IndexOf('.');
                    if (dot > 0)
                        name = name[..dot];
                    if (!found.Contains(name, StringComparer.OrdinalIgnoreCase))
                        found.Add(name);
                }
            }
            catch
            {
                // 目录不可读时忽略
            }
        }
        // 源语言与英文排前面，其余按名称排序（稳定、可预期）
        var ordered = new List<string>();
        foreach (string preferred in new[] { SourceLocale, "en" })
        {
            string? hit = found.FirstOrDefault(x => string.Equals(x, preferred, StringComparison.OrdinalIgnoreCase));
            if (hit is not null)
                ordered.Add(hit);
        }
        ordered.AddRange(found.Where(x => !ordered.Contains(x, StringComparer.OrdinalIgnoreCase))
                              .OrderBy(x => x, StringComparer.OrdinalIgnoreCase));
        return ordered;
    }

    private static Dictionary<string, string> LoadPack(string locale)
    {
        lock (Gate)
        {
            if (Packs.TryGetValue(locale, out Dictionary<string, string>? cached))
                return cached;
            var flat = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (string dir in LangDirectories())
            {
                // 主文件 + 模块分片：<locale>.json 与 <locale>.<模块>.json（任务书 3.5
                // 要求"语言包按模块拆分文件，便于多人协作"）。模块分片里的同名键
                // 覆盖主文件，翻译者可以只维护自己负责的那一块。
                var files = new List<string>();
                string main = Path.Combine(dir, locale + ".json");
                if (File.Exists(main))
                    files.Add(main);
                try
                {
                    if (Directory.Exists(dir))
                        files.AddRange(Directory.GetFiles(dir, locale + ".*.json").OrderBy(x => x, StringComparer.Ordinal));
                }
                catch
                {
                    // 目录不可读时忽略
                }
                if (files.Count == 0)
                    continue;
                foreach (string path in files)
                {
                    try
                    {
                        using JsonDocument doc = JsonDocument.Parse(File.ReadAllText(path));
                        Flatten(doc.RootElement, "", flat);
                    }
                    catch
                    {
                        // 语言包损坏不应让程序起不来：跳过这个文件
                    }
                }
                break;
            }
            Packs[locale] = flat;
            return flat;
        }
    }

    /// <summary>语言包搜索目录（按优先级），去重但保持顺序。</summary>
    private static IEnumerable<string> LangDirectories()
    {
        var seen = new List<string>();
        foreach (string dir in new[]
                 {
                     // 修复七：用户数据目录（便携版外层）优先，用户往这里丢语言包即可生效
                     Path.Combine(ShortcutService.UserDataDirectory, "lang", "ui"),
                     // 界面语言包放在 lang\ui\：后端语言包在 lang\，文件名同为 <locale>.json，
                     // 放同一目录会互相覆盖（v0.2.1 开发中实际踩到过这个坑）。
                     Path.Combine(ShortcutService.ProgramDirectory, "lang", "ui"),
                     Path.Combine(AppContext.BaseDirectory, "lang", "ui"),
                     Path.Combine(AppContext.BaseDirectory, "Resources", "Lang"),
                 })
        {
            if (seen.Contains(dir, StringComparer.OrdinalIgnoreCase))
                continue;
            seen.Add(dir);
            yield return dir;
        }
    }

    private static void Flatten(JsonElement node, string prefix, Dictionary<string, string> output)
    {
        switch (node.ValueKind)
        {
            case JsonValueKind.Object:
                // 带注释的单条写法：{"text": "...", "comment": "..."}
                if (node.TryGetProperty("text", out JsonElement textEl) && textEl.ValueKind == JsonValueKind.String)
                {
                    if (prefix.Length > 0)
                        output[prefix] = textEl.GetString() ?? "";
                    return;
                }
                foreach (JsonProperty property in node.EnumerateObject())
                {
                    if (property.Name.StartsWith('_') || property.Name.StartsWith("//") || property.Name.StartsWith('$'))
                        continue;   // 注释键
                    string key = prefix.Length > 0 ? prefix + "." + property.Name : property.Name;
                    Flatten(property.Value, key, output);
                }
                break;
            case JsonValueKind.String:
                if (prefix.Length > 0)
                    output[prefix] = node.GetString() ?? "";
                break;
        }
    }

    // ---------------------------------------------------------------
    // 语言切换
    // ---------------------------------------------------------------

    /// <summary>首次启动时初始化：setting 为 "auto" 或具体语言标签。</summary>
    public static void Initialize(string? setting)
    {
        SetLocale(setting, persist: false, notify: false);
    }

    /// <summary>把系统区域归一化成语言包名（zh-CN → zh-Hans、zh-TW → zh-Hant、en-US → en）。</summary>
    public static string NormalizeLocale(string? tag)
    {
        if (string.IsNullOrWhiteSpace(tag))
            return SourceLocale;
        string text = tag.Trim().Replace('_', '-');
        foreach (string known in LocaleNames.Keys)
        {
            if (string.Equals(known, text, StringComparison.OrdinalIgnoreCase))
                return known;
        }
        string[] parts = text.Split('-');
        string lang = parts[0].ToLowerInvariant();
        string script = parts.Length > 1 && parts[1].Length == 4 ? parts[1].ToLowerInvariant() : "";
        string region = parts.Length > 1 ? parts[^1].ToLowerInvariant() : "";
        return lang switch
        {
            "zh" => (script == "hant" || region is "tw" or "hk" or "mo") ? "zh-Hant" : "zh-Hans",
            "pt" => region == "br" ? "pt-BR" : "pt",
            "en" or "ja" or "ko" or "de" or "fr" or "es" or "ru" or "ar" => lang,
            _ => text,
        };
    }

    /// <summary>按系统区域自动选择语言（任务书 3.4 自动检测）。</summary>
    public static string DetectSystemLocale() => NormalizeLocale(CultureInfo.CurrentUICulture.Name);

    /// <summary>
    /// 切换语言。返回实际生效的语言（请求的语言没有语言包时按回退链取第一个可用的）。
    /// </summary>
    /// <param name="setting">"auto" / 具体语言标签 / null（= auto）。</param>
    /// <param name="persist">是否写入 ui_state.json。</param>
    /// <param name="notify">是否触发 <see cref="LanguageChanged"/>。</param>
    public static string SetLocale(string? setting, bool persist = true, bool notify = true)
    {
        string requested = string.IsNullOrWhiteSpace(setting) ? "auto" : setting.Trim();
        string want = requested.Equals("auto", StringComparison.OrdinalIgnoreCase)
            ? DetectSystemLocale()
            : NormalizeLocale(requested);

        lock (Gate)
        {
            Packs.Clear();
            string effective = "";
            foreach (string locale in new[] { want }.Concat(FallbackChain.Where(x => x != want)))
            {
                Dictionary<string, string> pack = LoadPack(locale);
                if (pack.Count > 0 && effective.Length == 0)
                    effective = locale;
            }
            if (effective.Length == 0)
                effective = SourceLocale;   // 一个语言包都读不到时仍可工作（界面显示键名）
            Locale = effective;
            Requested = requested;
        }

        if (persist)
        {
            AppSettings settings = AppSettings.Load();
            settings.Language = requested;
            settings.Save();
        }
        if (notify)
            LanguageChanged?.Invoke(null, EventArgs.Empty);
        return Locale;
    }

    /// <summary>重新读取语言包（翻译者改完文件不必重启）。</summary>
    public static string Reload() => SetLocale(Requested, persist: false);

    /// <summary>
    /// 不改语言、只重跑一遍文本回填。
    /// 用于"文本内容本身没变，但渲染策略变了"的场景 —— 目前是 emoji 降级开关（修复六）。
    /// </summary>
    public static void Reapply() => LanguageChanged?.Invoke(null, EventArgs.Empty);

    // ---------------------------------------------------------------
    // 取文案
    // ---------------------------------------------------------------

    /// <summary>取一条文案：当前语言 → en → zh-Hans → 键名本身。</summary>
    public static string T(string key)
    {
        if (string.IsNullOrEmpty(key))
            return "";
        foreach (string locale in new[] { Locale }.Concat(FallbackChain.Where(x => x != Locale)))
        {
            if (LoadPack(locale).TryGetValue(key, out string? value) && !string.IsNullOrEmpty(value))
                return Emoji.Prefer(value);   // 修复六：不支持 emoji 的环境降级为 ASCII 标记
        }
        return key;
    }

    /// <summary>取一条文案并替换 <c>{0}</c> 之类的占位符；缺失时用 fallback。</summary>
    public static string T(string key, string fallback, params object?[] args)
    {
        string template = T(key);
        if (template == key)                     // 没找到键
            template = fallback;
        if (args.Length == 0)
            return template;
        try
        {
            return string.Format(CultureInfo.CurrentCulture, template, args);
        }
        catch (FormatException)
        {
            return template;
        }
    }

    /// <summary>是否包含某个键（避免把键名显示到界面上）。</summary>
    public static bool Has(string key) => T(key) != key;

    // ---------------------------------------------------------------
    // XAML 附加属性 + 应用
    // ---------------------------------------------------------------

    /// <summary>元素文本对应的语言包键（Text / Content / Header 自动判断）。</summary>
    public static readonly DependencyProperty KeyProperty = DependencyProperty.RegisterAttached(
        "Key", typeof(string), typeof(Loc), new PropertyMetadata(null));

    /// <summary>元素的 ToolTip 对应的语言包键。</summary>
    public static readonly DependencyProperty TipKeyProperty = DependencyProperty.RegisterAttached(
        "TipKey", typeof(string), typeof(Loc), new PropertyMetadata(null));

    /// <summary>屏幕阅读器说明（AutomationProperties.HelpText）对应的语言包键。</summary>
    public static readonly DependencyProperty HelpKeyProperty = DependencyProperty.RegisterAttached(
        "HelpKey", typeof(string), typeof(Loc), new PropertyMetadata(null));

    public static string? GetKey(DependencyObject o) => (string?)o.GetValue(KeyProperty);
    public static void SetKey(DependencyObject o, string? value) => o.SetValue(KeyProperty, value);
    public static string? GetTipKey(DependencyObject o) => (string?)o.GetValue(TipKeyProperty);
    public static void SetTipKey(DependencyObject o, string? value) => o.SetValue(TipKeyProperty, value);
    public static string? GetHelpKey(DependencyObject o) => (string?)o.GetValue(HelpKeyProperty);
    public static void SetHelpKey(DependencyObject o, string? value) => o.SetValue(HelpKeyProperty, value);

    /// <summary>
    /// 递归把语言包文本写回控件树。切换语言后对每个已打开的窗口调用一次即可。
    /// 同时遍历逻辑树与可视化树：前者覆盖尚未渲染的元素（如折叠的 Tab），
    /// 后者覆盖已实例化的模板内容（如弹出的 Popup）。
    /// </summary>
    public static void Apply(DependencyObject? root)
    {
        if (root is null)
            return;
        var seen = new HashSet<DependencyObject>(ReferenceEqualityComparer.Instance);
        Walk(root, seen);
    }

    private static void Walk(DependencyObject node, HashSet<DependencyObject> seen)
    {
        if (!seen.Add(node))
            return;
        ApplyNode(node);

        if (node is Visual or System.Windows.Media.Media3D.Visual3D)
        {
            int count = VisualTreeHelper.GetChildrenCount(node);
            for (int i = 0; i < count; i++)
                Walk(VisualTreeHelper.GetChild(node, i), seen);
        }
        foreach (object? child in LogicalTreeHelper.GetChildren(node))
        {
            if (child is DependencyObject dep)
                Walk(dep, seen);
        }
    }

    private static void ApplyNode(DependencyObject node)
    {
        string? tipKey = GetTipKey(node);
        if (tipKey is not null)
            SetToolTip(node, T(tipKey));

        string? helpKey = GetHelpKey(node);
        if (helpKey is not null)
            AutomationProperties.SetHelpText(node, T(helpKey));

        string? key = GetKey(node);
        if (key is null)
            return;
        string text = T(key);

        switch (node)
        {
            case Window window:
                window.Title = text;
                break;
            case TextBlock textBlock:
                textBlock.Text = text;
                break;
            case HeaderedContentControl headeredContent:   // GroupBox / TabItem / Expander
                headeredContent.Header = text;
                break;
            case HeaderedItemsControl headeredItems:       // MenuItem
                headeredItems.Header = text;
                break;
            case ContentControl contentControl when contentControl.Content is string or null:
                contentControl.Content = text;
                break;
            case System.Windows.Controls.Primitives.RangeBase:
                break;   // Slider/ProgressBar 没有文本
        }
    }

    private static void SetToolTip(DependencyObject node, string text)
    {
        if (node is FrameworkElement element)
            element.ToolTip = text;
    }
    /// <summary>引用相等比较器：控件树里可能有相等但不同的对象，按引用去重避免重复遍历。</summary>
    private sealed class ReferenceEqualityComparer : IEqualityComparer<DependencyObject>
    {
        public static readonly ReferenceEqualityComparer Instance = new();
        public bool Equals(DependencyObject? x, DependencyObject? y) => ReferenceEquals(x, y);
        public int GetHashCode(DependencyObject obj) => System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(obj);
    }
}
