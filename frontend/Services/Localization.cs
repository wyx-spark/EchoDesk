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
/// [EN] Front end localization (the design specification, development task 3).
///
/// [EN] Design points:
/// <list type="bullet">
/// <item><b>Language pack location</b>: <c>lang/&lt;locale&gt;.json</c> (at the root of the
/// release directory); when it is not found, fall back to <c>Resources/Lang/</c> inside the
/// assembly. Users can drop a new language pack into <c>lang/</c> themselves without
/// recompiling (this is the path for community-contributed language packs).</item>
/// <item><b>Fallback chain</b> (the design specification 3.4): current language, then en,
/// then zh-Hans, then the key itself. Returning the key itself is deliberate: a missing key
/// shows up conspicuously in the UI instead of a blank.</item>
/// <item><b>On-demand loading</b> (the design specification 3.9): only the current language
/// and the languages on the fallback chain are read into memory.</item>
/// <item><b>Immediate refresh</b>: <see cref="Apply"/> writes text back according to the
/// <c>Loc.Key</c> / <c>Loc.HelpKey</c> / <c>Loc.TipKey</c> attached properties declared in
/// XAML; switching languages only requires re-running it for every open window, with no
/// restart.</item>
/// </list>
///
/// [EN] Note: <b>do not</b> put dynamic status text (such as "Ready" or context usage) into
/// <c>Loc.Key</c>, because the next <see cref="Apply"/> would overwrite it; get such text in
/// code through <see cref="T(string)"/>. When screen readers need extra description, use
/// <c>Loc.HelpKey</c> (not AutomationProperties.Name, which would override the real readable
/// text, a lesson from the v0.2.0 investigation).
/// [ZH] 前端多语言（设计规范 研发任务 3）。
///
/// [ZH] 设计要点：
/// [ZH] 语言包位置：<c>lang/&lt;locale&gt;.json</c>（发布目录根下），找不到时回退到程序集的
/// [ZH] <c>Resources/Lang/</c>。用户可以自己往 <c>lang/</c> 里丢一个新语言包，不必重新编译
/// [ZH] （社区贡献语言包的路径）。
/// [ZH] 回退链（设计规范 3.4）：当前语言 → en → zh-Hans → 键名本身。返回键名本身是刻意的：
/// [ZH] 缺键时界面上会显眼地出现键名，而不是空白。
/// [ZH] 按需加载（设计规范 3.9）：只把当前语言与回退链上的语言读进内存。
/// [ZH] 即时刷新：<see cref="Apply"/> 按 XAML 里声明的
/// [ZH] <c>Loc.Key</c> / <c>Loc.HelpKey</c> / <c>Loc.TipKey</c> 附加属性回填文本；
/// [ZH] 切换语言时对所有已打开窗口重跑一遍即可，无需重启。
///
/// [ZH] 注意：<b>不要把动态状态文本</b>（如"已就绪"、上下文占用）写成 <c>Loc.Key</c>，
/// [ZH] 那会被下一次 <see cref="Apply"/> 覆盖；这类文本用 <see cref="T(string)"/> 在代码里取，
/// [ZH] 需要给屏幕阅读器补充说明时用 <c>Loc.HelpKey</c>（不能用 AutomationProperties.Name，
/// [ZH] 它会覆盖真实可读文本，v0.2.0 的排查经验）。
/// </summary>
public sealed class Loc
{
    /// <summary>
    /// [EN] Source language: the authoritative source of every key, and also the final
    /// [EN] fallback target.
    /// [ZH] 源语言：所有键的权威来源，也是最终回退目标。
    /// </summary>
    public const string SourceLocale = "zh-Hans";

    /// <summary>
    /// [EN] Global fallback order: English first, then Chinese.
    /// [ZH] 全局回退顺序：先英文，再中文。
    /// </summary>
    private static readonly string[] FallbackChain = { "en", "zh-Hans" };

    /// <summary>
    /// [EN] Display names of the languages (a language name always uses that language's own
    /// [EN] spelling).
    /// [ZH] 语言展示名（语言名一律用该语言自己的写法）。
    /// </summary>
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

    /// <summary>
    /// [EN] The language currently in effect.
    /// [ZH] 当前生效的语言。
    /// </summary>
    public static string Locale { get; private set; } = SourceLocale;

    /// <summary>
    /// [EN] The language requested by the user (may be "auto").
    /// [ZH] 用户请求的语言（可能是 "auto"）。
    /// </summary>
    public static string Requested { get; private set; } = "auto";

    /// <summary>
    /// [EN] Raised after a language switch; each window re-runs <see cref="Apply"/> for it.
    /// [ZH] 语言切换后触发；各窗口据此重跑 <see cref="Apply"/>。
    /// </summary>
    public static event EventHandler? LanguageChanged;

    /// <summary>
    /// [EN] Display names of the supported UI languages (for the drop-down in the settings
    /// [EN] window).
    /// [ZH] 支持的界面语言展示名（供设置窗口的下拉框）。
    /// </summary>
    public static string DisplayName(string locale) =>
        LocaleNames.TryGetValue(locale, out string? name) ? name : locale;

    // ---------------------------------------------------------------
    // [EN] Language pack location and loading
    // [ZH] 语言包位置与加载
    // ---------------------------------------------------------------

    /// <summary>
    /// [EN] Language pack search directories, in priority order: the program directory's
    /// [EN] <c>lang/</c>, the assembly directory's <c>lang/</c>, and <c>Resources/Lang/</c>.
    /// [ZH] 语言包搜索目录（按优先级）：程序目录 <c>lang/</c>、程序集目录 <c>lang/</c> 与
    /// [ZH] <c>Resources/Lang/</c>。
    /// </summary>
    public static string LangDirectory => LangDirectories().FirstOrDefault(Directory.Exists) ?? LangDirectories().First();

    /// <summary>
    /// [EN] Lists the languages actually available (built-in plus user-provided external
    /// [EN] language packs).
    /// [ZH] 列出实际可用的语言（内置 + 用户外置语言包）。
    /// </summary>
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
                    // [EN] Module shards (<locale>.<module>.json) are not standalone
                    // [EN] languages; skip them.
                    // [ZH] 模块分片（<locale>.<模块>.json）不是独立语言，跳过
                    int dot = name.IndexOf('.');
                    if (dot > 0)
                        name = name[..dot];
                    if (!found.Contains(name, StringComparer.OrdinalIgnoreCase))
                        found.Add(name);
                }
            }
            catch
            {
                // [EN] Ignore the directory when it cannot be read.
                // [ZH] 目录不可读时忽略
            }
        }
        // [EN] The source language and English come first; the rest are sorted by name (stable
        // [EN] and predictable).
        // [ZH] 源语言与英文排前面，其余按名称排序（稳定、可预期）
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
                // [EN] Main file plus module shards: <locale>.json and <locale>.<module>.json
                // [EN] (the design specification 3.5 requires "language packs split into files
                // [EN] by module for easier collaboration"). A key with the same name in a
                // [EN] module shard overrides the main file, so a translator can maintain only
                // [EN] the part they own.
                // [ZH] 主文件 + 模块分片：<locale>.json 与 <locale>.<模块>.json（设计规范 3.5
                // [ZH] 要求"语言包按模块拆分文件，便于多人协作"）。模块分片里的同名键
                // [ZH] 覆盖主文件，翻译者可以只维护自己负责的那一块。
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
                    // [EN] Ignore the directory when it cannot be read.
                    // [ZH] 目录不可读时忽略
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
                        // [EN] A corrupted language pack must not keep the application from
                        // [EN] starting: skip this file.
                        // [ZH] 语言包损坏不应让程序起不来：跳过这个文件
                    }
                }
                break;
            }
            Packs[locale] = flat;
            return flat;
        }
    }

    /// <summary>
    /// [EN] Language pack search directories (in priority order), de-duplicated while keeping
    /// [EN] the order.
    /// [ZH] 语言包搜索目录（按优先级），去重但保持顺序。
    /// </summary>
    private static IEnumerable<string> LangDirectories()
    {
        var seen = new List<string>();
        foreach (string dir in new[]
                 {
                     // [EN] Fix seven: the user data directory (the outer level of the portable
                     // [EN] build) comes first; a language pack dropped here takes effect.
                     // [ZH] 修复七：用户数据目录（便携版外层）优先，用户往这里丢语言包即可生效
                     Path.Combine(ShortcutService.UserDataDirectory, "lang", "ui"),
                     // [EN] UI language packs live in lang\ui\: backend language packs live in
                     // [EN] lang\ and use the same <locale>.json file names, so putting both in
                     // [EN] one directory would overwrite each other (this was actually hit
                     // [EN] during v0.2.1 development).
                     // [ZH] 界面语言包放在 lang\ui\：后端语言包在 lang\，文件名同为 <locale>.json，
                     // [ZH] 放同一目录会互相覆盖（v0.2.1 开发中实际踩到过这个坑）。
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
                // [EN] Single-entry form with a comment:
                // [EN] {"text": "...", "comment": "..."}
                // [ZH] 带注释的单条写法：{"text": "...", "comment": "..."}
                if (node.TryGetProperty("text", out JsonElement textEl) && textEl.ValueKind == JsonValueKind.String)
                {
                    if (prefix.Length > 0)
                        output[prefix] = textEl.GetString() ?? "";
                    return;
                }
                foreach (JsonProperty property in node.EnumerateObject())
                {
                    if (property.Name.StartsWith('_') || property.Name.StartsWith("//") || property.Name.StartsWith('$'))
                        continue;   // [EN] Comment key.
                                    // [ZH] 注释键
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
    // [EN] Language switching
    // [ZH] 语言切换
    // ---------------------------------------------------------------

    /// <summary>
    /// [EN] Initializes on first start: `setting` is "auto" or a concrete language tag.
    /// [ZH] 首次启动时初始化：setting 为 "auto" 或具体语言标签。
    /// </summary>
    public static void Initialize(string? setting)
    {
        SetLocale(setting, persist: false, notify: false);
    }

    /// <summary>
    /// [EN] Normalizes a system locale to a language pack name (zh-CN to zh-Hans, zh-TW to
    /// [EN] zh-Hant, en-US to en).
    /// [ZH] 把系统区域归一化成语言包名（zh-CN → zh-Hans、zh-TW → zh-Hant、en-US → en）。
    /// </summary>
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

    /// <summary>
    /// [EN] Selects the language automatically from the system locale (the design specification
    /// [EN] 3.4, automatic detection).
    /// [ZH] 按系统区域自动选择语言（设计规范 3.4 自动检测）。
    /// </summary>
    public static string DetectSystemLocale() => NormalizeLocale(CultureInfo.CurrentUICulture.Name);

    /// <summary>
    /// [EN] Switches the language. Returns the language actually in effect (when the requested
    /// [EN] language has no language pack, the first available one on the fallback chain is
    /// [EN] used).
    /// [ZH] 切换语言。返回实际生效的语言（请求的语言没有语言包时按回退链取第一个可用的）。
    /// </summary>
    /// <param name="setting">
    /// [EN] "auto", a concrete language tag, or null (= auto).
    /// [ZH] "auto" / 具体语言标签 / null（= auto）。
    /// </param>
    /// <param name="persist">
    /// [EN] Whether to write ui_state.json.
    /// [ZH] 是否写入 ui_state.json。
    /// </param>
    /// <param name="notify">
    /// [EN] Whether to raise <see cref="LanguageChanged"/>.
    /// [ZH] 是否触发 <see cref="LanguageChanged"/>。
    /// </param>
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
                // [EN] Still works when no language pack can be read (the UI shows the keys).
                // [ZH] 一个语言包都读不到时仍可工作（界面显示键名）
                effective = SourceLocale;
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

    /// <summary>
    /// [EN] Re-reads the language packs (translators do not have to restart after editing the
    /// [EN] files).
    /// [ZH] 重新读取语言包（翻译者改完文件不必重启）。
    /// </summary>
    public static string Reload() => SetLocale(Requested, persist: false);

    /// <summary>
    /// [EN] Does not change the language but re-runs the text backfill once. It is used when
    /// [EN] "the text content itself did not change but the rendering strategy did"; currently
    /// [EN] that is the emoji fallback switch (fix six).
    /// [ZH] 不改语言、只重跑一遍文本回填。
    /// [ZH] 用于"文本内容本身没变，但渲染策略变了"的场景 —— 目前是 emoji 降级开关（修复六）。
    /// </summary>
    public static void Reapply() => LanguageChanged?.Invoke(null, EventArgs.Empty);

    // ---------------------------------------------------------------
    // [EN] Getting messages
    // [ZH] 取文案
    // ---------------------------------------------------------------

    /// <summary>
    /// [EN] Gets one message: current language, then en, then zh-Hans, then the key itself.
    /// [ZH] 取一条文案：当前语言 → en → zh-Hans → 键名本身。
    /// </summary>
    public static string T(string key)
    {
        if (string.IsNullOrEmpty(key))
            return "";
        foreach (string locale in new[] { Locale }.Concat(FallbackChain.Where(x => x != Locale)))
        {
            if (LoadPack(locale).TryGetValue(key, out string? value) && !string.IsNullOrEmpty(value))
                // [EN] Fix six: environments without emoji support fall back to ASCII markers.
                // [ZH] 修复六：不支持 emoji 的环境降级为 ASCII 标记
                return Emoji.Prefer(value);
        }
        return key;
    }

    /// <summary>
    /// [EN] Gets one message and replaces placeholders such as <c>{0}</c>; when the message is
    /// [EN] missing, fallback is used.
    /// [ZH] 取一条文案并替换 <c>{0}</c> 之类的占位符；缺失时用 fallback。
    /// </summary>
    public static string T(string key, string fallback, params object?[] args)
    {
        string template = T(key);
        if (template == key)                     // [EN] Key not found.
                                                 // [ZH] 没找到键
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

    /// <summary>
    /// [EN] Whether a key exists (to avoid showing the key name in the UI).
    /// [ZH] 是否包含某个键（避免把键名显示到界面上）。
    /// </summary>
    public static bool Has(string key) => T(key) != key;

    // ---------------------------------------------------------------
    // [EN] XAML attached properties + application
    // [ZH] XAML 附加属性 + 应用
    // ---------------------------------------------------------------

    /// <summary>
    /// [EN] Language pack key for the element text (Text / Content / Header is detected
    /// [EN] automatically).
    /// [ZH] 元素文本对应的语言包键（Text / Content / Header 自动判断）。
    /// </summary>
    public static readonly DependencyProperty KeyProperty = DependencyProperty.RegisterAttached(
        "Key", typeof(string), typeof(Loc), new PropertyMetadata(null));

    /// <summary>
    /// [EN] Language pack key for the element's ToolTip.
    /// [ZH] 元素的 ToolTip 对应的语言包键。
    /// </summary>
    public static readonly DependencyProperty TipKeyProperty = DependencyProperty.RegisterAttached(
        "TipKey", typeof(string), typeof(Loc), new PropertyMetadata(null));

    /// <summary>
    /// [EN] Language pack key for the screen reader description
    /// [EN] (AutomationProperties.HelpText).
    /// [ZH] 屏幕阅读器说明（AutomationProperties.HelpText）对应的语言包键。
    /// </summary>
    public static readonly DependencyProperty HelpKeyProperty = DependencyProperty.RegisterAttached(
        "HelpKey", typeof(string), typeof(Loc), new PropertyMetadata(null));

    public static string? GetKey(DependencyObject o) => (string?)o.GetValue(KeyProperty);
    public static void SetKey(DependencyObject o, string? value) => o.SetValue(KeyProperty, value);
    public static string? GetTipKey(DependencyObject o) => (string?)o.GetValue(TipKeyProperty);
    public static void SetTipKey(DependencyObject o, string? value) => o.SetValue(TipKeyProperty, value);
    public static string? GetHelpKey(DependencyObject o) => (string?)o.GetValue(HelpKeyProperty);
    public static void SetHelpKey(DependencyObject o, string? value) => o.SetValue(HelpKeyProperty, value);

    /// <summary>
    /// [EN] Recursively writes language pack text back into the control tree. After a language
    /// [EN] switch, calling it once for every open window is enough.
    /// [EN] Both the logical tree and the visual tree are walked: the former covers elements
    /// [EN] that are not rendered yet (such as a collapsed Tab), and the latter covers template
    /// [EN] content that has already been instantiated (such as an open Popup).
    /// [ZH] 递归把语言包文本写回控件树。切换语言后对每个已打开的窗口调用一次即可。
    /// [ZH] 同时遍历逻辑树与可视化树：前者覆盖尚未渲染的元素（如折叠的 Tab），
    /// [ZH] 后者覆盖已实例化的模板内容（如弹出的 Popup）。
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
                break;   // [EN] Slider/ProgressBar has no text.
                         // [ZH] Slider/ProgressBar 没有文本
        }
    }

    private static void SetToolTip(DependencyObject node, string text)
    {
        if (node is FrameworkElement element)
            element.ToolTip = text;
    }
    /// <summary>
    /// [EN] Reference equality comparer: the control tree may contain equal but different
    /// [EN] objects; de-duplicate by reference to avoid walking them repeatedly.
    /// [ZH] 引用相等比较器：控件树里可能有相等但不同的对象，按引用去重避免重复遍历。
    /// </summary>
    private sealed class ReferenceEqualityComparer : IEqualityComparer<DependencyObject>
    {
        public static readonly ReferenceEqualityComparer Instance = new();
        public bool Equals(DependencyObject? x, DependencyObject? y) => ReferenceEquals(x, y);
        public int GetHashCode(DependencyObject obj) => System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(obj);
    }
}
