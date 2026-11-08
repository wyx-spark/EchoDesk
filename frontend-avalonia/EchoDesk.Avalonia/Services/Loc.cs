// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/Loc.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/Loc.cs。类型与成员说明见下方注释。
// [EN] UI localization (ported from frontend/Services/Localization.cs, development
// [EN] tasks 3 and 6 of the design specification).
// [ZH] 界面本地化（移植自 frontend/Services/Localization.cs，设计规范研发任务 3/6）。
//
// [EN] **Key differences from the WPF version** (trade-offs that migration must know):
// [ZH] 与 WPF 版的**关键差异**（迁移时必须知道的取舍）：
// [EN]   1. The WPF version refreshed text through attached properties plus a visual
// [EN]      tree walk (Loc.Key / Loc.Apply); the Avalonia version uses compiled
// [EN]      bindings plus the `LanguageChanged` event to rebuild UI text. Reason: the
// [EN]      attached-property walk is unnecessary under compiled bindings, and
// [EN]      event-driven rebuilding is more direct (chosen for the prototype phase, see
// [EN]      records).
// [ZH]   1. WPF 版用附加属性 + 可视化树遍历（Loc.Key / Loc.Apply）刷文案；Avalonia 版
// [ZH]      用编译期绑定 + `LanguageChanged` 事件重建界面文本。原因：Avalonia 的附加属性
// [ZH]      遍历在编译绑定下不必要，事件驱动重建更直接（原型阶段选它，见 records）。
// [EN]   2. **The language pack search order has one extra layer, the "embedded
// [EN]      resource"**: the repository contains only one copy of the language pack
// [EN]      (frontend/Resources/Lang/*.json), and the Avalonia project compiles it into
// [EN]      the assembly via a csproj link, so there is a "single source" rather than a
// [EN]      duplicate. Disk directories (user directory / program directory) have higher
// [EN]      priority, so translators can try new text without changing code.
// [ZH]   2. **语言包搜索顺序多了一层“内嵌资源”**：仓库里只有一份语言包
// [ZH]      （frontend/Resources/Lang/*.json），Avalonia 工程通过 csproj 的链接把它编进程序集，
// [ZH]      于是“单一来源”而不是复制一份。磁盘目录（用户目录 / 程序目录）优先级更高，
// [ZH]      翻译者可以不改代码就试新文案。
// [EN]   3. Placeholders are still `{0}` / `{0:0.#}`, consistent with `_t()` in the
// [EN]      backend Python.
// [ZH]   3. 占位符仍是 `{0}` / `{0:0.#}`，与后端 Python 的 `_t()` 保持一致。
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
    /// <summary>
    /// [EN] Source language (the final fallback when text is missing, and the baseline
    /// [EN] for translation).
    /// [ZH] 源语言（文案缺失时的最终回退，也是翻译的基准）。
    /// </summary>
    public const string SourceLocale = "zh-Hans";

    /// <summary>
    /// [EN] Fallback chain: current language -> en -> zh-Hans.
    /// [ZH] 回退链：当前语言 → en → zh-Hans。
    /// </summary>
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

    /// <summary>
    /// [EN] Triggered after a language switch: the UI subscribes to rebuild its own text
    /// [EN] (the Avalonia version does not use attached-property walks).
    /// [ZH] 语言切换后触发：界面订阅它重建自己的文案（Avalonia 版不用附加属性遍历）。
    /// </summary>
    public static event EventHandler? LanguageChanged;

    public static string DisplayName(string locale) =>
        LocaleNames.TryGetValue(locale, out string? name) ? name : locale;

    public static List<string> AvailableLocales() => new(LocaleNames.Keys);

    /// <summary>
    /// [EN] Normalizes a system locale into a language pack name (zh-CN -> zh-Hans,
    /// [EN] zh-TW -> zh-Hant, en-US -> en).
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

    /// <summary>
    /// [EN] Initialization on first launch: setting is "auto" or a concrete language tag.
    /// [ZH] 首次启动时初始化：setting 为 "auto" 或具体语言标签。
    /// </summary>
    public static void Initialize(string? setting) => SetLocale(setting, persist: false, notify: false);

    /// <summary>
    /// [EN] Switches the language and tries to persist it (persist=false is used while
    /// [EN] reading settings at startup, to avoid writing back the value just read).
    /// [ZH] 切换语言并尝试落盘（persist=false 用于启动期读设置，避免写出自己刚读的值）。
    /// </summary>
    public static string SetLocale(string? setting, bool persist = true, bool notify = true)
    {
        string want;
        bool auto = string.IsNullOrWhiteSpace(setting)
                    || string.Equals(setting, "auto", StringComparison.OrdinalIgnoreCase);
        want = auto ? DetectSystemLocale() : NormalizeLocale(setting);

        Requested = auto ? "auto" : want;
        // [EN] When a language pack does not exist, fall back level by level and land on
        // [EN] the source language: **does not throw**, so the UI shows at least Chinese
        // [EN] rather than blank.
        // [ZH] 语言包不存在时逐级回退，最终落到源语言：**不抛异常**，界面至少是中文而不是空白。
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
                // [EN] Failing to write settings should not block the language switch
                // [EN] itself.
                // [ZH] 设置写不进去不该阻断语言切换本身。
            }
        }
        if (notify)
            LanguageChanged?.Invoke(null, EventArgs.Empty);
        return Locale;
    }

    /// <summary>
    /// [EN] Reloads the language packs (called after a translator edits the JSON on
    /// [EN] disk; no restart is required).
    /// [ZH] 重载语言包（翻译者改完磁盘上的 JSON 后调用，不必重启）。
    /// </summary>
    public static string Reload()
    {
        lock (Gate)
            Packs.Clear();
        return SetLocale(Requested, persist: false);
    }

    /// <summary>
    /// [EN] Gets one text entry (returns the key itself when missing, which makes
    /// [EN] untranslated entries easy to spot).
    /// [ZH] 取一条文案（缺失时返回 key 本身，便于发现漏翻）。
    /// </summary>
    public static string T(string key)
    {
        foreach (string locale in new[] { Locale }.Concat(FallbackChain))
        {
            if (LoadPack(locale).TryGetValue(key, out string? value) && !string.IsNullOrEmpty(value))
                return Emoji.Prefer(value);
        }
        return key;
    }

    /// <summary>
    /// [EN] Gets one text entry and replaces placeholders such as <c>{0}</c>; uses
    /// [EN] fallback when missing.
    /// [ZH] 取一条文案并替换 <c>{0}</c> 之类的占位符；缺失时用 fallback。
    /// </summary>
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
            // [EN] Placeholder typo (a common translation error): return the original
            // [EN] template instead of crashing the UI.
            // [ZH] 占位符写错（翻译常见错误）：返回原模板而不是崩界面。
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

            // [EN] 1) Disk directories (user directory first, then program directory) --
            // [EN] for keys with the same name, later reads override earlier ones.
            // [ZH] ① 磁盘目录（用户目录优先，其次程序目录）——同名键后读的覆盖先读的。
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
                    // [EN] Directory unreadable: ignore this source.
                    // [ZH] 目录不可读：忽略该来源。
                }
                if (files.Count == 0)
                    continue;
                files.Sort(StringComparer.Ordinal);
                foreach (string path in files)
                    LoadInto(flat, () => File.ReadAllText(path));
                // [EN] Once a directory is found, stop looking at lower-priority
                // [EN] directories (avoids mixing versions).
                // [ZH] 找到目录即不再往低优先级目录找（避免版本混装）。
                break;
            }

            // [EN] 2) Language packs embedded in the assembly (the only copy in the
            // [EN] repository, guaranteeing that no pack is missing after release).
            // [ZH] ② 内嵌在程序集里的语言包（仓库里唯一那份，保证发布后不会缺包）。
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
                        // [EN] This fragment does not exist: a normal case (it is also
                        // [EN] allowed that only zh-Hans has fragments).
                        // [ZH] 没有这个分片：正常情况（只有 zh-Hans 有分片也是允许的）。
                        return "";
                    }
                });
            }

            Packs[locale] = flat;
            return flat;
        }
    }

    /// <summary>
    /// [EN] Reads one JSON text and flattens it into the target dictionary (a parse
    /// [EN] failure skips only this file and does not prevent startup).
    /// [ZH] 读一个 JSON 文本并拍平进目标字典（解析失败只跳过这一个文件，不让程序起不来）。
    /// </summary>
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
            // [EN] Corrupted language pack: skip.
            // [ZH] 语言包损坏：跳过。
        }
    }

    /// <summary>
    /// [EN] Language pack search directories (in priority order).
    /// [ZH] 语言包搜索目录（按优先级）。
    /// </summary>
    private static IEnumerable<string> LangDirectories()
    {
        var seen = new List<string>();
        foreach (string dir in new[]
                 {
                     // [EN] User data directory (translators try new text here).
                     // [ZH] 用户数据目录（翻译者现场试）。
                     UserPaths.LangDirectory,
                     // [EN] Release layout.
                     // [ZH] 发布布局。
                     Path.Combine(UserPaths.ProgramDirectory, "lang", "ui"),
                     // [EN] Development layout.
                     // [ZH] 开发布局。
                     Path.Combine(UserPaths.ProgramDirectory, "Resources", "Lang"),
                 })
        {
            if (seen.Contains(dir, StringComparer.OrdinalIgnoreCase))
                continue;
            seen.Add(dir);
            yield return dir;
        }
    }

    /// <summary>
    /// [EN] Flattens nested objects into dotted keys
    /// [EN] (<c>{"main":{"status":{"ready":"…"}}}</c> -> <c>main.status.ready</c>).
    /// [EN] All three forms are supported: a plain string, <c>{"text": "…"}</c> (a
    /// [EN] single entry with comments), and an already written dotted key.
    /// [EN] **Dotted keys take priority**: when a language pack contains both a nested
    /// [EN] copy and a flat key, the flat key is the one actually read at runtime
    /// [EN] (v0.2.1 tripped over this: a change to the nested copy had no effect), so
    /// [EN] the flat key overrides the nested value here.
    /// [ZH] 嵌套对象拍平成点号键（<c>{"main":{"status":{"ready":"…"}}}</c> → <c>main.status.ready</c>）。
    /// [ZH] 三种写法都支持：纯字符串、<c>{"text": "…"}</c>（带注释的单条）、已写好的点号键。
    /// [ZH] **点号键优先**：语言包里同时存在嵌套副本与扁平键时，扁平键是运行时真正读取的那份
    /// [ZH] （v0.2.1 在这上面踩过坑：改了嵌套副本却没生效），所以这里让扁平键覆盖嵌套值。
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

        // [EN] Collect nested children first, then literal keys at this level, so the
        // [EN] "flat key wins" rule holds.
        // [ZH] 先收嵌套子节点，再收本层字面量键，保证“扁平键赢”。
        var literals = new List<(string Key, string Value)>();
        foreach (JsonProperty property in node.EnumerateObject())
        {
            if (property.Name.StartsWith('_') || property.Name.StartsWith("//") || property.Name.StartsWith('$'))
                continue;   // [EN] Comment key.
                            // [ZH] 注释键。
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
                // [EN] The dotted key targets another module: the parent node of that
                // [EN] module is responsible for it.
                // [ZH] 点号键写的是别的模块：由那个模块的父节点负责。
                continue;
            output[key] = value;
        }
    }

    /// <summary>
    /// [EN] For tests/self-check: directly queries whether a key exists in a given
    /// [EN] language pack (does not trigger the fallback chain).
    /// [ZH] 测试/自检用：直接查询某个语言包里是否有某个键（不触发回退链）。
    /// </summary>
    public static bool HasInPack(string locale, string key) => LoadPack(locale).ContainsKey(key);

    /// <summary>
    /// [EN] For tests/self-check: total number of keys in a language pack.
    /// [ZH] 测试/自检用：某个语言包的键总数。
    /// </summary>
    public static int PackKeyCount(string locale) => LoadPack(locale).Count;

    /// <summary>
    /// [EN] For tests/self-check: clears the cache (used with Reload to verify that disk
    /// [EN] changes take effect).
    /// [ZH] 测试/自检用：清空缓存（配合 Reload 验证磁盘改动生效）。
    /// </summary>
    public static void ResetCache()
    {
        lock (Gate)
            Packs.Clear();
    }
}
