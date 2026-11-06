// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/Loc.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/Loc.cs。类型与成员说明见下方注释。
// 界面本地化（移植自 frontend/Services/Localization.cs，任务书 任务三/任务六）。
//
// 与 WPF 版的**关键差异**（迁移时必须知道的取舍）：
//   1. WPF 版用附加属性 + 可视化树遍历（Loc.Key / Loc.Apply）刷文案；Avalonia 版
//      用编译期绑定 + `LanguageChanged` 事件重建界面文本。原因：Avalonia 的附加属性
//      遍历在编译绑定下不必要，事件驱动重建更直接（原型阶段选它，见 records）。
//   2. **语言包搜索顺序多了一层"内嵌资源"**：仓库里只有一份语言包
//      （frontend/Resources/Lang/*.json），Avalonia 工程通过 csproj 的链接把它编进程序集，
//      于是"单一来源"而不是复制一份。磁盘目录（用户目录 / 程序目录）优先级更高，
//      翻译者可以不改代码就试新文案。
//   3. 占位符仍是 `{0}` / `{0:0.#}`，与后端 Python 的 `_t()` 保持一致。
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using Avalonia.Platform;

namespace EchoDesk.Avalonia.Services;

public static class Loc
{
    /// <summary>源语言（文案缺失时的最终回退，也是翻译的基准）。</summary>
    public const string SourceLocale = "zh-Hans";

    /// <summary>回退链：当前语言 → en → zh-Hans。</summary>
    private static readonly string[] FallbackChain = { "en", "zh-Hans" };

    private static readonly Dictionary<string, string> LocaleNames = new(StringComparer.OrdinalIgnoreCase)
    {
        ["zh-Hans"] = "简体中文",
        ["zh-Hant"] = "繁體中文",
        ["en"] = "English",
    };

    private static readonly object Gate = new();
    private static readonly Dictionary<string, Dictionary<string, string>> Packs = new(StringComparer.OrdinalIgnoreCase);

    public static string Locale { get; private set; } = SourceLocale;
    public static string Requested { get; private set; } = "auto";

    /// <summary>语言切换后触发：界面订阅它重建自己的文案（Avalonia 版不用附加属性遍历）。</summary>
    public static event EventHandler? LanguageChanged;

    public static string DisplayName(string locale) =>
        LocaleNames.TryGetValue(locale, out string? name) ? name : locale;

    public static List<string> AvailableLocales() => new(LocaleNames.Keys);

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
        if (lang == "zh")
        {
            if (script == "hant" || region is "tw" or "hk" or "mo")
                return "zh-Hant";
            return "zh-Hans";
        }
        if (lang == "en")
            return "en";
        return lang;
    }

    public static string DetectSystemLocale() => NormalizeLocale(CultureInfo.CurrentUICulture.Name);

    /// <summary>首次启动时初始化：setting 为 "auto" 或具体语言标签。</summary>
    public static void Initialize(string? setting) => SetLocale(setting, persist: false, notify: false);

    /// <summary>切换语言并尝试落盘（persist=false 用于启动期读设置，避免写出自己刚读的值）。</summary>
    public static string SetLocale(string? setting, bool persist = true, bool notify = true)
    {
        string want;
        bool auto = string.IsNullOrWhiteSpace(setting)
                    || string.Equals(setting, "auto", StringComparison.OrdinalIgnoreCase);
        want = auto ? DetectSystemLocale() : NormalizeLocale(setting);

        Requested = auto ? "auto" : want;
        // 语言包不存在时逐级回退，最终落到源语言：**不抛异常**，界面至少是中文而不是空白
        foreach (string locale in new[] { want }.Concat(FallbackChain))
        {
            if (locale == want || !string.IsNullOrEmpty(locale))
            {
                if (LoadPack(locale).Count > 0)
                {
                    Locale = locale;
                    break;
                }
            }
        }
        if (persist && notify)
        {
            try
            {
                AppSettings settings = AppSettings.Load();
                settings.Language = auto ? "auto" : want;
                settings.Save();
            }
            catch (Exception)
            {
                // 设置写不进去不该阻断语言切换本身
            }
        }
        if (notify)
            LanguageChanged?.Invoke(null, EventArgs.Empty);
        return Locale;
    }

    /// <summary>重载语言包（翻译者改完磁盘上的 JSON 后调用，不必重启）。</summary>
    public static string Reload()
    {
        lock (Gate)
            Packs.Clear();
        return SetLocale(Requested, persist: false);
    }

    /// <summary>取一条文案（缺失时返回 key 本身，便于发现漏翻）。</summary>
    public static string T(string key)
    {
        foreach (string locale in new[] { Locale }.Concat(FallbackChain))
        {
            if (LoadPack(locale).TryGetValue(key, out string? value) && !string.IsNullOrEmpty(value))
                return Emoji.Prefer(value);
        }
        return key;
    }

    /// <summary>取一条文案并替换 <c>{0}</c> 之类的占位符；缺失时用 fallback。</summary>
    public static string T(string key, string fallback, params object?[] args)
    {
        string template = T(key);
        if (template == key)
            template = fallback;
        if (args.Length == 0)
            return template;
        try
        {
            return string.Format(CultureInfo.CurrentCulture, template, args);
        }
        catch (FormatException)
        {
            // 占位符写错（翻译常见错误）：返回原模板而不是崩界面
            return template;
        }
    }

    public static bool Has(string key) => T(key) != key;

    private static Dictionary<string, string> LoadPack(string locale)
    {
        lock (Gate)
        {
            if (Packs.TryGetValue(locale, out Dictionary<string, string>? cached))
                return cached;
            var flat = new Dictionary<string, string>(StringComparer.Ordinal);

            // ① 磁盘目录（用户目录优先，其次程序目录）——同名键后读的覆盖先读的
            foreach (string dir in LangDirectories())
            {
                var files = new List<string>();
                string main = Path.Combine(dir, locale + ".json");
                if (File.Exists(main))
                    files.Add(main);
                try
                {
                    if (Directory.Exists(dir))
                        files.AddRange(Directory.GetFiles(dir, locale + ".*.json"));
                }
                catch (Exception)
                {
                    // 目录不可读：忽略该来源
                }
                if (files.Count == 0)
                    continue;
                files.Sort(StringComparer.Ordinal);
                foreach (string path in files)
                    LoadInto(flat, () => File.ReadAllText(path));
                break;   // 找到目录即不再往低优先级目录找（避免版本混装）
            }

            // ② 内嵌在程序集里的语言包（仓库里唯一那份，保证发布后不会缺包）
            foreach (string name in new[] { locale + ".json", locale + ".main.json", locale + ".settings.json" })
            {
                string uri = "avares://EchoDesk/Assets/Lang/" + name;
                LoadInto(flat, () =>
                {
                    try
                    {
                        using Stream stream = AssetLoader.Open(new Uri(uri));
                        using var reader = new StreamReader(stream);
                        return reader.ReadToEnd();
                    }
                    catch (Exception)
                    {
                        return "";   // 没有这个分片：正常情况（只有 zh-Hans 有分片也是允许的）
                    }
                });
            }

            Packs[locale] = flat;
            return flat;
        }
    }

    /// <summary>读一个 JSON 文本并拍平进目标字典（解析失败只跳过这一个文件，不让程序起不来）。</summary>
    private static void LoadInto(Dictionary<string, string> flat, Func<string> read)
    {
        try
        {
            string text = read();
            if (string.IsNullOrWhiteSpace(text))
                return;
            using JsonDocument doc = JsonDocument.Parse(text);
            Flatten(doc.RootElement, "", flat);
        }
        catch (Exception)
        {
            // 语言包损坏：跳过
        }
    }

    /// <summary>语言包搜索目录（按优先级）。</summary>
    private static IEnumerable<string> LangDirectories()
    {
        var seen = new List<string>();
        foreach (string dir in new[]
                 {
                     UserPaths.LangDirectory,                                        // 用户数据目录（翻译者现场试）
                     Path.Combine(UserPaths.ProgramDirectory, "lang", "ui"),         // 发布布局
                     Path.Combine(UserPaths.ProgramDirectory, "Resources", "Lang"),  // 开发布局
                 })
        {
            if (seen.Contains(dir, StringComparer.OrdinalIgnoreCase))
                continue;
            seen.Add(dir);
            yield return dir;
        }
    }

    /// <summary>
    /// 嵌套对象拍平成点号键（<c>{"main":{"status":{"ready":"…"}}}</c> → <c>main.status.ready</c>）。
    /// 三种写法都支持：纯字符串、<c>{"text": "…"}</c>（带注释的单条）、已写好的点号键。
    /// **点号键优先**：语言包里同时存在嵌套副本与扁平键时，扁平键是运行时真正读取的那份
    /// （v0.2.1 在这上面踩过坑：改了嵌套副本却没生效），所以这里让扁平键覆盖嵌套值。
    /// </summary>
    private static void Flatten(JsonElement node, string prefix, Dictionary<string, string> output)
    {
        if (node.ValueKind != JsonValueKind.Object)
        {
            if (node.ValueKind == JsonValueKind.String && prefix.Length > 0)
                output[prefix] = node.GetString() ?? "";
            return;
        }

        if (node.TryGetProperty("text", out JsonElement textEl) && textEl.ValueKind == JsonValueKind.String)
        {
            if (prefix.Length > 0)
                output[prefix] = textEl.GetString() ?? "";
            return;
        }

        // 先收嵌套子节点，再收本层字面量键，保证"扁平键赢"
        var literals = new List<(string Key, string Value)>();
        foreach (JsonProperty property in node.EnumerateObject())
        {
            if (property.Name.StartsWith('_') || property.Name.StartsWith("//") || property.Name.StartsWith('$'))
                continue;   // 注释键
            if (property.Name.Contains('.') && property.Value.ValueKind == JsonValueKind.String)
            {
                literals.Add((property.Name, property.Value.GetString() ?? ""));
                continue;
            }
            string key = prefix.Length > 0 ? prefix + "." + property.Name : property.Name;
            Flatten(property.Value, key, output);
        }
        foreach ((string key, string value) in literals)
        {
            if (prefix.Length > 0 && !key.StartsWith(prefix + ".", StringComparison.Ordinal))
                continue;   // 点号键写的是别的模块：由那个模块的父节点负责
            output[key] = value;
        }
    }

    /// <summary>测试/自检用：直接查询某个语言包里是否有某个键（不触发回退链）。</summary>
    public static bool HasInPack(string locale, string key) => LoadPack(locale).ContainsKey(key);

    /// <summary>测试/自检用：某个语言包的键总数。</summary>
    public static int PackKeyCount(string locale) => LoadPack(locale).Count;

    /// <summary>测试/自检用：清空缓存（配合 Reload 验证磁盘改动生效）。</summary>
    public static void ResetCache()
    {
        lock (Gate)
            Packs.Clear();
    }
}
