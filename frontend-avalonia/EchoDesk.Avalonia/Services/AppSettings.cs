// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/AppSettings.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/AppSettings.cs。类型与成员说明见下方注释。
// [EN] UI settings (ported from frontend/Services/AppSettings.cs; development task 3 of the
// [EN] design specification: "the user data directory follows platform conventions").
// [ZH] 界面设置（移植自 frontend/Services/AppSettings.cs；设计规范 研发任务 3 的“用户数据目录按平台惯例”）。
//
// [EN] The WPF version wrote ui_state.json "outside the program directory" (fix 7: the portable
// [EN] build must not keep user data alongside the program directory).
// [ZH] WPF 版把 ui_state.json 写在“程序目录的外层”（修复七：便携版不希望用户数据跟着程序目录跑）。
// [EN] The cross-platform version defers to UserPaths.DataRoot and keeps the ECHODESK_DATA
// [EN] override; self-checks and tests rely on it to write settings into a temporary directory.
// [ZH] 跨平台版改为交给 UserPaths.DataRoot 决定，并保留 ECHODESK_DATA 覆盖 ——
// [ZH] 自检/测试就靠它把设置写到临时目录里。
//
// [EN] **On Windows this is not %APPDATA%** (v0.2.2 corrected a wrong comment): the release uses
// [EN] a portable layout, and the program directory is called `app`, so the user data root is its
// [EN] parent directory (outside the release directory); only in the fallback branch where "the
// [EN] program directory is not called app" is Windows the program directory itself. macOS falls
// [EN] back to Application Support and Linux to XDG -- see the comment on UserPaths.DataRoot for
// [EN] the full decision order.
// [ZH] **Windows 上不是 %APPDATA%**（v0.2.2 修正了一处写错的注释）：发布物是便携布局，
// [ZH] 程序目录叫 `app` → 用户数据根 = 它的父目录（发布目录外层）；只有“程序目录不叫 app”
// [ZH] 的兜底分支上，Windows 才是程序目录本身。macOS 回退到 Application Support、
// [ZH] Linux 回退到 XDG —— 完整判定顺序见 UserPaths.DataRoot 的注释。
//
// [EN] Only UI state is stored: language, theme, font size, emoji preference, and window size.
// [EN] Business configuration such as models, retrieval, and Agent stays in the backend's
// [EN] config.json, so the front end does not keep a duplicate that could drift out of sync (the
// [EN] WPF version uses the same division of labor).
// [ZH] 只存“界面自己”的状态：语言、主题、字号、emoji 偏好、窗口尺寸。
// [ZH] 模型、检索、Agent 这些**业务**配置留在后端的 config.json，前端不重复存一份，
// [ZH] 避免两边不一致（WPF 版也是这个分工）。
using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace EchoDesk.Avalonia.Services;

public sealed class AppSettings
{
    /// <summary>
    /// [EN] UI language: "auto" or a specific tag (zh-Hans / zh-Hant / en).
    /// [ZH] 界面语言："auto" 或具体标签（zh-Hans / zh-Hant / en）。
    /// </summary>
    [JsonPropertyName("language")]
    public string Language { get; set; } = "auto";

    /// <summary>
    /// [EN] Theme: "auto" / "light" / "dark".
    /// [ZH] 主题："auto" / "light" / "dark"。
    /// </summary>
    [JsonPropertyName("theme")]
    public string Theme { get; set; } = "auto";

    /// <summary>
    /// [EN] Chat font size in px; the A- / A+ controls in the UI also change it.
    /// [ZH] 聊天区字号（px，界面上的 A- / A+ 也改它）。
    /// </summary>
    [JsonPropertyName("font_size")]
    public double FontSize { get; set; } = 14;

    /// <summary>
    /// [EN] Emoji preference: "auto" / "always" / "never".
    /// [ZH] emoji 偏好："auto" / "always" / "never"。
    /// </summary>
    [JsonPropertyName("emoji")]
    public string EmojiPreference { get; set; } = "auto";

    /// <summary>
    /// [EN] Whether to ask for confirmation before commands with side effects (/clear,
    /// [EN] /clearcache) (v0.2.1 cross-platform-2 development task 1, the General page).
    /// [EN] Enabled by default: prevents data loss from typing /clear as /clearcache by mistake
    /// [EN] (same default as the WPF version).
    /// [ZH] 执行有副作用的指令（/clear、/clearcache）前是否二次确认（v0.2.1 跨平台-2 研发任务 1，常规页）。
    /// [ZH] 默认开启：避免把 /clear 误输入成 /clearcache 造成数据损失（WPF 版同一默认值）。
    /// </summary>
    [JsonPropertyName("confirm_destructive")]
    public bool ConfirmDestructiveCommands { get; set; } = true;

    /// <summary>
    /// [EN] Settings window size (development task 1 requires "window state memory"; stored
    /// [EN] separately from the main window, so the two do not affect each other).
    /// [EN] The default matches Width=780 / Height=660 in the WPF SettingsWindow.xaml: Avalonia's
    /// [EN] Window.Width/Height are **client area** sizes, while WPF's include the window border,
    /// [EN] so this value is 764x621 after subtracting the non-client area (16 horizontal,
    /// [EN] 39 vertical).
    /// [ZH] 设置窗口尺寸（研发任务 1 要求“窗口状态记忆”；与主窗口分开存，互不影响）。
    /// [ZH] 默认值与 WPF 版 SettingsWindow.xaml 的 Width=780 / Height=660 对齐：
    /// [ZH] Avalonia 的 Window.Width/Height 是**客户区**尺寸，WPF 的是含边框的整窗尺寸，
    /// [ZH] 所以这里取 WPF 整窗减去非客户区（横向 16、纵向 39）后的 764x621。
    /// </summary>
    [JsonPropertyName("settings_window_width")]
    public double SettingsWindowWidth { get; set; } = 844;
    [JsonPropertyName("settings_window_height")]
    public double SettingsWindowHeight { get; set; } = 581;

    /// <summary>
    /// [EN] Main window size. Also aligned with the WPF 860x620 whole window, i.e. client area
    /// [EN] 844x581.
    /// [ZH] 主窗口尺寸。同样对齐 WPF 的 860x620 整窗，即客户区 844x581。
    /// </summary>
    [JsonPropertyName("window_width")]
    public double WindowWidth { get; set; } = 844;

    [JsonPropertyName("window_height")]
    public double WindowHeight { get; set; } = 581;

    // [EN] ---------- v0.2.2 batch 3: window position and one-time prompt memory ----------
    // [ZH] ---------- v0.2.2 第 3 批：窗口位置与一次性询问的记忆 ----------
    //
    // [EN] These five fields have the **same names and meanings** as in the WPF version
    // [EN] (WindowLeft / WindowTop / WindowMaximized / FirstRunDone / ShortcutPromptDeclined in the
    // [EN] WPF AppSettings). The key names go through the "read both spellings, write both"
    // [EN] compatibility layer; see the comments on Load / Save.
    // [ZH] 这五个字段与 WPF 版**同名同义**（WPF 侧 AppSettings 的 WindowLeft / WindowTop /
    // [ZH] WindowMaximized / FirstRunDone / ShortcutPromptDeclined）。
    // [ZH] 键名走 "两种写法都读、两种都写" 的兼容层，见 Load / Save 的说明。

    /// <summary>
    /// [EN] Left edge of the main window (NaN = not recorded yet; use the system default position).
    /// [ZH] 主窗口左边缘（NaN = 还没记过，按系统默认位置）。
    /// </summary>
    public double WindowLeft { get; set; } = double.NaN;

    /// <summary>
    /// [EN] Top edge of the main window (NaN = not recorded yet).
    /// [ZH] 主窗口上边缘（NaN = 还没记过）。
    /// </summary>
    public double WindowTop { get; set; } = double.NaN;

    /// <summary>
    /// [EN] Whether the window was maximized when the app last exited.
    /// [ZH] 上次退出时是否最大化。
    /// </summary>
    public bool WindowMaximized { get; set; }

    /// <summary>
    /// [EN] Whether the first-run introduction has been completed (shown only once).
    /// [ZH] 是否已完成首次启动引导（只展示一次）。
    /// </summary>
    public bool FirstRunDone { get; set; }

    /// <summary>
    /// [EN] The user has declined the startup shortcut prompt (do not ask again).
    /// [ZH] 用户已拒绝创建启动快捷方式（不再重复询问）。
    /// </summary>
    public bool ShortcutPromptDeclined { get; set; }

    /// <summary>
    /// [EN] Whether the "lightweight status area" of the main window is collapsed (v0.2.1
    /// [EN] cross-platform-2 development task 2, the second migrated module).
    /// [EN] Same name and meaning as in the WPF version: the collapsed state must persist across
    /// [EN] restarts, otherwise it has to be collapsed manually on every launch.
    /// [ZH] 主窗口“轻量状态区”是否已折叠（v0.2.1 跨平台-2 研发任务 2 第二个迁移模块）。
    /// [ZH] 与 WPF 版同名同语义：折叠状态要能在下次启动沿用，否则每次开窗都要手动收一次。
    /// </summary>
    [JsonPropertyName("status_strip_collapsed")]
    public bool StatusStripCollapsed { get; set; }

    /// <summary>
    /// [EN] Preferred UI language (not persisted during prototyping, memory only); null means
    /// [EN] "not written successfully yet".
    /// [ZH] 界面语言首选（原型阶段不落盘，仅内存）；null 表示“还没写成功”。
    /// </summary>
    [JsonIgnore]
    public string? LastLoadError { get; private set; }

    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    // =================================================================
    // [EN] Field compatibility (v0.2.2 batch 3): PascalCase <-> snake_case
    // [ZH] 字段兼容（v0.2.2 第 3 批）：PascalCase ↔ snake_case
    // =================================================================
    //
    // [EN] The two front ends **share the same ui_state.json** (both under UserPaths.DataRoot),
    // [EN] but historically they used different key names: WPF used C# property names
    // [EN] (`WindowWidth` / `FontSize` / `EmojiMode` / `DarkMode`), while Avalonia used snake_case
    // [EN] (`window_width` / `font_size` / `emoji`). The result was that **switching front ends
    // [EN] lost preferences**: a font size tuned in WPF reverted to the default in the
    // [EN] cross-platform build -- that is the item recorded as B7 in the differences list.
    //
    // [EN] Approach: **read both key spellings (snake_case first) and write both**. Do not
    // [EN] "unify on one", because each spelling is read by one front end and changing either
    // [EN] would blind the other; the cost of one extra alias key (about 20 more lines in the
    // [EN] file) is far smaller than "preferences mysteriously disappearing".
    // [ZH] 两个前端**共用同一份 ui_state.json**（都在 UserPaths.DataRoot 下），但历史上键名
    // [ZH] 各写各的：WPF 用 C# 属性名（`WindowWidth` / `FontSize` / `EmojiMode` / `DarkMode`），
    // [ZH] Avalonia 用 snake_case（`window_width` / `font_size` / `emoji`）。
    // [ZH] 结果是**换一个前端就丢一次偏好**：用 WPF 调好的字号，切到跨平台版变回默认值 ——
    // [ZH] 差异清单 B7 记的就是这一条。
    //
    // [ZH] 处理方式：**两种键名都读（snake_case 优先），两种都写**。
    // [ZH] 不做“统一成一种”，因为两种写法各有一个前端在读，改任何一边都会让另一边失明；
    // [ZH] 多写一个别名键的代价（文件大 20 行）远小于“用户偏好莫名其妙丢失”。

    /// <summary>
    /// [EN] For each field, the (Avalonia key name, WPF key name). Both places must read and
    /// [EN] write; see Load / Save.
    /// [ZH] 每个字段的（Avalonia 键名, WPF 键名）。两处都要读、两处都要写，见 Load / Save。
    /// </summary>
    public static IReadOnlyList<(string Snake, string Pascal)> FieldAliases { get; } = new[]
    {
        ("language", "Language"),
        ("theme", "Theme"),
        ("font_size", "FontSize"),
        ("emoji", "EmojiMode"),
        ("confirm_destructive", "ConfirmDestructiveCommands"),
        ("window_width", "WindowWidth"),
        ("window_height", "WindowHeight"),
        ("window_left", "WindowLeft"),
        ("window_top", "WindowTop"),
        ("window_maximized", "WindowMaximized"),
        ("first_run_done", "FirstRunDone"),
        ("shortcut_prompt_declined", "ShortcutPromptDeclined"),
        ("status_strip_collapsed", "StatusStripCollapsed"),
        ("settings_window_width", "SettingsWindowWidth"),
        ("settings_window_height", "SettingsWindowHeight"),
    };

    /// <summary>
    /// [EN] Read settings. If the file is missing or corrupt, return the defaults and record the
    /// [EN] reason (the UI must still start).
    /// [ZH] 读设置。文件不存在/损坏都返回默认值并记下原因（不让界面起不来）。
    /// </summary>
    public static AppSettings Load()
    {
        var settings = new AppSettings();
        string path = UserPaths.UiStateFile;
        try
        {
            if (!File.Exists(path))
                return settings;
            string text = File.ReadAllText(path);
            if (string.IsNullOrWhiteSpace(text))
                return settings;
            using JsonDocument doc = JsonDocument.Parse(text);
            JsonElement root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return settings;

            settings.Language = ReadString(root, "language", "Language", settings.Language);
            settings.Theme = ReadString(root, "theme", "Theme", settings.Theme);
            settings.FontSize = ReadDouble(root, "font_size", "FontSize", settings.FontSize);
            settings.EmojiPreference = ReadString(root, "emoji", "EmojiMode", settings.EmojiPreference);
            settings.ConfirmDestructiveCommands = ReadBool(root, "confirm_destructive",
                "ConfirmDestructiveCommands", settings.ConfirmDestructiveCommands);
            settings.WindowWidth = ReadDouble(root, "window_width", "WindowWidth", settings.WindowWidth);
            settings.WindowHeight = ReadDouble(root, "window_height", "WindowHeight", settings.WindowHeight);
            settings.WindowLeft = ReadDouble(root, "window_left", "WindowLeft", settings.WindowLeft);
            settings.WindowTop = ReadDouble(root, "window_top", "WindowTop", settings.WindowTop);
            settings.WindowMaximized = ReadBool(root, "window_maximized", "WindowMaximized", false);
            settings.FirstRunDone = ReadBool(root, "first_run_done", "FirstRunDone", false);
            settings.ShortcutPromptDeclined = ReadBool(root, "shortcut_prompt_declined",
                "ShortcutPromptDeclined", false);
            settings.StatusStripCollapsed = ReadBool(root, "status_strip_collapsed",
                "StatusStripCollapsed", false);
            settings.SettingsWindowWidth = ReadDouble(root, "settings_window_width",
                "SettingsWindowWidth", settings.SettingsWindowWidth);
            settings.SettingsWindowHeight = ReadDouble(root, "settings_window_height",
                "SettingsWindowHeight", settings.SettingsWindowHeight);
            settings.MigrateLegacyTheme(root);
            settings.MigrateLegacyWindowSizes();
            settings.Clamp();
            return settings;
        }
        catch (Exception ex)
        {
            settings.LastLoadError = ex.Message;
            return settings;
        }
    }

    /// <summary>
    /// [EN] Migrate the `DarkMode` boolean from v0.2.1 and earlier to the three-state `theme`
    /// [EN] (same criterion as WPF's `MigrateLegacyTheme`).
    ///
    /// [EN] Migrate only when the file has no theme/Theme field -- if the user has chosen a theme
    /// [EN] in the new version, the new field wins. Older versions wrote `dark` by default;
    /// [EN] treating that unconditionally as a preference would flip every existing user's UI from
    /// [EN] light to dark. Discarding it outright, however, would lose the setting of users who
    /// [EN] did manually switch to dark, so an existing legacy field is treated as a preference.
    /// [ZH] 迁移 v0.2.1 及更早版本的 `DarkMode` 布尔开关到三态 `theme`
    /// [ZH] （与 WPF 的 `MigrateLegacyTheme` 同一判据）。
    ///
    /// [ZH] 只在“文件里没有 theme/Theme 字段”时迁移 —— 用户在新版本里选过主题就以新字段为准。
    /// [ZH] 旧版本默认写 `dark`，若一律当成用户偏好会把所有老用户的界面从浅色翻成深色，
    /// [ZH] 但反过来直接丢弃会让“确实手动切过深色”的用户设置丢失，故按“旧字段存在即视为偏好”处理。
    /// </summary>
    private void MigrateLegacyTheme(JsonElement root)
    {
        if (root.TryGetProperty("theme", out _) || root.TryGetProperty("Theme", out _))
            return;
        foreach (string name in new[] { "dark_mode", "DarkMode" })
        {
            if (root.TryGetProperty(name, out JsonElement legacy)
                && legacy.ValueKind is JsonValueKind.True or JsonValueKind.False)
            {
                Theme = legacy.GetBoolean() ? "dark" : "light";
                return;
            }
        }
    }

    /// <summary>
    /// [EN] Migrate window sizes remembered by older versions to the v0.2.2 aligned defaults.
    ///
    /// [EN] Why this is needed: the visual alignment changed the main window default from 1080x760
    /// [EN] to 844x581 (the client area of the WPF 860x620 window) and the settings window from
    /// [EN] 980x720 to 764x621. Window sizes are **remembered**, however: changing only the
    /// [EN] defaults leaves the old large sizes in existing users' ui_state.json, so the window
    /// [EN] keeps opening at 1080x760 and nearly fills the screen on displays such as 1366x768
    /// [EN] after system scaling, which users perceive as "too large to work with".
    ///
    /// [EN] Migrate only the two pairs that are **exactly the old defaults**: sizes the user
    /// [EN] dragged themselves stay untouched (those are real preferences).
    /// [ZH] 把旧版本记忆下来的窗口尺寸迁到 v0.2.2 对齐后的默认值。
    ///
    /// [ZH] 为什么需要：视觉对齐把主窗口默认从 1080x760 改成 844x581（WPF 860x620 的客户区）、
    /// [ZH] 设置窗口从 980x720 改成 764x621。但窗口尺寸是**记住**的 —— 只改默认值的话，
    /// [ZH] 老用户的 ui_state.json 里仍存着旧的大尺寸，窗口会一直按 1080x760 打开；
    /// [ZH] 在 1366x768 这类屏上再经系统缩放放大后几乎铺满，用户会觉得“太大不好操作”。
    ///
    /// [ZH] 只迁移**恰好等于旧默认值**的那两组数：用户自己拖过的尺寸保持不动（那是真实偏好）。
    /// </summary>
    private void MigrateLegacyWindowSizes()
    {
        if (Math.Abs(WindowWidth - 1080) < 0.5 && Math.Abs(WindowHeight - 760) < 0.5)
        {
            WindowWidth = 844;
            WindowHeight = 581;
        }
        // [EN] The settings window has had two generations of historical defaults: 980x720 in
        // [EN] v0.2.1 and 764x621 in the first visual-alignment version. The user asked for "the
        // [EN] settings window to be the same size as the main window", so both generations
        // [EN] migrate to 844x581; any other size the user dragged stays untouched.
        // [ZH] 设置窗口的历史默认有两代：v0.2.1 的 980x720，和视觉对齐第一版的 764x621。
        // [ZH] 用户要求“设置窗口跟主窗口同样大小”，所以这两代默认值都迁到 844x581；
        // [ZH] 用户自己拖过的其它尺寸保持不动。
        bool legacySettingsSize =
            (Math.Abs(SettingsWindowWidth - 980) < 0.5 && Math.Abs(SettingsWindowHeight - 720) < 0.5)
            || (Math.Abs(SettingsWindowWidth - 764) < 0.5 && Math.Abs(SettingsWindowHeight - 621) < 0.5);
        if (legacySettingsSize)
        {
            SettingsWindowWidth = 844;
            SettingsWindowHeight = 581;
        }
    }

    private static string ReadString(JsonElement root, string snake, string pascal, string fallback)
    {
        foreach (string name in new[] { snake, pascal })
        {
            if (root.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.String)
                return value.GetString() ?? fallback;
        }
        return fallback;
    }

    private static double ReadDouble(JsonElement root, string snake, string pascal, double fallback)
    {
        foreach (string name in new[] { snake, pascal })
        {
            if (root.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.Number
                && value.TryGetDouble(out double parsed))
            {
                return parsed;
            }
        }
        return fallback;
    }

    private static bool ReadBool(JsonElement root, string snake, string pascal, bool fallback)
    {
        foreach (string name in new[] { snake, pascal })
        {
            if (root.TryGetProperty(name, out JsonElement value)
                && value.ValueKind is JsonValueKind.True or JsonValueKind.False)
            {
                return value.GetBoolean();
            }
        }
        return fallback;
    }

    /// <summary>
    /// [EN] Write settings. Failures do not throw (read-only directory, insufficient permissions,
    /// [EN] etc.) -- the UI should not crash merely because preferences cannot be saved.
    /// [ZH] 写设置。失败不抛（只读目录、权限不足等）—— 界面不该因为存不了偏好而崩。
    /// </summary>
    public bool Save()
    {
        try
        {
            Clamp();
            UserPaths.EnsureDirectory(UserPaths.DataRoot);
            string json = SerializeWithAliases();
            File.WriteAllText(UserPaths.UiStateFile, json, new System.Text.UTF8Encoding(false));
            return true;
        }
        catch (Exception ex)
        {
            LastLoadError = ex.Message;
            return false;
        }
    }

    /// <summary>
    /// [EN] When writing to disk, **write both key spellings** (snake_case is Avalonia's own
    /// [EN] canonical form, PascalCase is the one WPF understands). See
    /// [EN] <see cref="FieldAliases"/>.
    /// [ZH] 落盘时**同时写两套键名**（snake_case 是 Avalonia 自己的规范写法，
    /// [ZH] PascalCase 是 WPF 读得懂的那一份）。见 <see cref="FieldAliases"/> 的说明。
    /// </summary>
    private string SerializeWithAliases()
    {
        var map = new Dictionary<string, object?>
        {
            ["language"] = Language,
            ["Language"] = Language,
            ["theme"] = Theme,
            ["Theme"] = Theme,
            ["font_size"] = FontSize,
            ["FontSize"] = FontSize,
            ["emoji"] = EmojiPreference,
            ["EmojiMode"] = EmojiPreference,
            ["confirm_destructive"] = ConfirmDestructiveCommands,
            ["ConfirmDestructiveCommands"] = ConfirmDestructiveCommands,
            ["window_width"] = WindowWidth,
            ["WindowWidth"] = WindowWidth,
            ["window_height"] = WindowHeight,
            ["WindowHeight"] = WindowHeight,
            ["window_left"] = double.IsNaN(WindowLeft) ? null : WindowLeft,
            ["WindowLeft"] = double.IsNaN(WindowLeft) ? null : WindowLeft,
            ["window_top"] = double.IsNaN(WindowTop) ? null : WindowTop,
            ["WindowTop"] = double.IsNaN(WindowTop) ? null : WindowTop,
            ["window_maximized"] = WindowMaximized,
            ["WindowMaximized"] = WindowMaximized,
            ["first_run_done"] = FirstRunDone,
            ["FirstRunDone"] = FirstRunDone,
            ["shortcut_prompt_declined"] = ShortcutPromptDeclined,
            ["ShortcutPromptDeclined"] = ShortcutPromptDeclined,
            ["status_strip_collapsed"] = StatusStripCollapsed,
            ["StatusStripCollapsed"] = StatusStripCollapsed,
            ["settings_window_width"] = SettingsWindowWidth,
            ["SettingsWindowWidth"] = SettingsWindowWidth,
            ["settings_window_height"] = SettingsWindowHeight,
            ["SettingsWindowHeight"] = SettingsWindowHeight,
        };
        return JsonSerializer.Serialize(map, Options);
    }

    /// <summary>
    /// [EN] Pull out-of-range values back into the usable range (so a hand-edited JSON file does
    /// [EN] not make the UI unusable).
    /// [ZH] 把越界的值拉回可用范围（手改 JSON 写坏了也不至于让界面没法用）。
    /// </summary>
    private void Clamp()
    {
        if (double.IsNaN(FontSize) || FontSize < 10 || FontSize > 28)
            FontSize = 14;
        if (double.IsNaN(WindowWidth) || WindowWidth < 720)
            WindowWidth = 844;
        if (double.IsNaN(WindowHeight) || WindowHeight < 520)
            WindowHeight = 581;
        if (double.IsNaN(SettingsWindowWidth) || SettingsWindowWidth < 560)
            SettingsWindowWidth = 844;
        if (double.IsNaN(SettingsWindowHeight) || SettingsWindowHeight < 420)
            SettingsWindowHeight = 581;
        Theme = (Theme ?? "auto").Trim().ToLowerInvariant() switch
        {
            "light" => "light",
            "dark" => "dark",
            _ => "auto",
        };
        EmojiPreference = (EmojiPreference ?? "auto").Trim().ToLowerInvariant() switch
        {
            "always" or "on" or "true" => "always",
            "never" or "off" or "false" => "never",
            _ => "auto",
        };
        if (string.IsNullOrWhiteSpace(Language))
            Language = "auto";
        // [EN] Window position: if either coordinate is NaN, discard both (remembering only half
        // [EN] would place the window in an odd corner).
        // [ZH] 窗口位置：只要有一个是 NaN 就整体丢弃（半记半不记会把窗口放到奇怪的角落）
        if (double.IsNaN(WindowLeft) || double.IsNaN(WindowTop)
            || double.IsInfinity(WindowLeft) || double.IsInfinity(WindowTop))
        {
            WindowLeft = double.NaN;
            WindowTop = double.NaN;
        }
    }
}

