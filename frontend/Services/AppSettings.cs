// [EN] EchoDesk WPF frontend module: frontend/Services/AppSettings.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Services/AppSettings.cs。类型与成员说明见下方注释。
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace EchoDesk.Services;

/// <summary>
/// [EN] Local frontend UI state stored in <c>ui_state.json</c>.
///
/// Records window size, position, maximized state, font size, theme, and
/// one-time decisions such as declining shortcut creation, so declined prompts
/// are not shown again on every start.
///
/// [ZH] 前端本地界面状态，保存在 <c>ui_state.json</c> 中。
///
/// [ZH] 记录窗口大小、位置、最大化状态、字号、主题，以及拒绝创建快捷方式等一次性询问结果，
/// [ZH] 使已拒绝的询问不会在每次启动时重复出现。
/// </summary>
public sealed class AppSettings
{
    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        // [EN] The legacy DarkMode field is cleared after migration and no
        // [EN] longer written back; other fields are serialized normally.
        // [ZH] 旧字段 DarkMode 迁移后置空，不再写回文件；其他字段照常序列化。
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    /// <summary>
    /// [EN] The user declined startup-shortcut creation; do not ask again.
    /// [ZH] 用户已拒绝创建启动快捷方式，不再重复询问。
    /// </summary>
    public bool ShortcutPromptDeclined { get; set; }

    /// <summary>
    /// [EN] Whether the first-run guide has been completed; it is shown once.
    /// [ZH] 是否已完成首次启动引导；该引导仅展示一次。
    /// </summary>
    public bool FirstRunDone { get; set; }

    // [EN] ---------- Window state ----------
    // [ZH] ---------- 窗口状态记忆 ----------
    public double WindowWidth { get; set; }
    public double WindowHeight { get; set; }
    public double WindowLeft { get; set; } = double.NaN;
    public double WindowTop { get; set; } = double.NaN;
    public bool WindowMaximized { get; set; }

    // [EN] ---------- Interface preferences ----------
    // [ZH] ---------- 界面偏好 ----------
    public double FontSize { get; set; } = 14;

    /// <summary>
    /// [EN] Theme: <c>auto</c> (follow system), <c>light</c>, or <c>dark</c>.
    /// [ZH] 主题：<c>auto</c>（跟随系统）、<c>light</c> 或 <c>dark</c>。
    /// </summary>
    public string Theme { get; set; } = ThemeService.Serialize(AppTheme.System);

    /// <summary>
    /// [EN] Legacy dark-mode switch retained only for reading old files. Users
    /// who previously wrote <c>DarkMode</c> have expressed a preference that
    /// must be migrated. New versions use <see cref="Theme"/> and never write
    /// this field.
    /// [ZH] 旧版深色开关，仅用于读取旧文件。此前写入 <c>DarkMode</c> 的用户已经表达
    /// [ZH] 过偏好，迁移时不能丢失。新版本以 <see cref="Theme"/> 为准，不再写回本字段。
    /// </summary>
    public bool? DarkMode { get; set; }

    /// <summary>
    /// [EN] Emoji output policy: <c>auto</c> (based on available fonts),
    /// <c>always</c>, or <c>never</c>.
    /// [ZH] emoji 输出策略：<c>auto</c>（按本机字体自动判断）、<c>always</c> 或
    /// <c>never</c>。
    /// </summary>
    public string EmojiMode { get; set; } = "auto";

    /// <summary>
    /// [EN] Interface language: <c>auto</c> follows the system locale; explicit
    /// tags such as <c>zh-Hans</c> or <c>en</c> are also accepted.
    /// [ZH] 界面语言：<c>auto</c> 表示跟随系统区域设置；也可使用
    /// [ZH] <c>zh-Hans</c>、<c>en</c> 等明确语言标签。
    /// </summary>
    public string Language { get; set; } = "auto";

    /// <summary>
    /// [EN] Developer mode: enables verbose logging and hot reload, and is
    /// activated automatically when plugins exist under skills_dev.
    /// [ZH] 开发者模式：启用详细日志与热重载；检测到 skills_dev 中存在插件时自动生效。
    /// </summary>
    public bool DeveloperMode { get; set; }

    /// <summary>
    /// [EN] Whether to confirm commands with irreversible side effects, such as
    /// <c>/clear</c> and <c>/clearcache</c>. Enabled by default to prevent
    /// accidental data loss.
    /// [ZH] 执行具有不可逆副作用的指令（如 <c>/clear</c>、<c>/clearcache</c>）前是否
    /// [ZH] 二次确认。默认开启，以避免误操作造成数据丢失。
    /// </summary>
    public bool ConfirmDestructiveCommands { get; set; } = true;

    /// <summary>
    /// [EN] Whether the compact status strip below the input is collapsed
    /// (expanded by default).
    /// [ZH] 输入框下方的轻量状态区是否收起（默认展开）。
    /// </summary>
    public bool StatusStripCollapsed { get; set; }

    /// <summary>
    /// [EN] UI coalescing interval for streaming output, in milliseconds.
    ///
    /// The backend emits token deltas. Rebuilding the Markdown document and
    /// scrolling on every delta causes hundreds of full layouts and visible
    /// stalls. Coalescing every 50 ms is imperceptible to users and reduces
    /// layout work by an order of magnitude. A value of 0 disables coalescing
    /// and updates on every delta, for diagnostics only.
    ///
    /// [ZH] 流式输出的界面合并间隔（毫秒）。
    ///
    /// [ZH] 后端逐 token 发送 delta。若每个 delta 都重建 Markdown 文档并滚动，会造成数百次
    /// [ZH] 全量布局和明显卡顿。按 50 毫秒合并一次，人眼几乎无法察觉，同时将布局次数
    /// [ZH] 降低一个数量级。设为 0 表示不合并、每个 delta 都更新，仅用于诊断。
    /// </summary>
    public int StreamThrottleMs { get; set; } = 50;

    private static string FilePath
    {
        get
        {
            // [EN] UI state lives in the user data directory, separate from the
            // [EN] program files, so it is easy to find and back up.
            // [ZH] 界面状态存放在用户数据目录，与程序文件分开，便于查找与备份。
            string dir = ShortcutService.UserDataDirectory;
            return Path.Combine(dir, "ui_state.json");
        }
    }

    /// <summary>
    /// [EN] Load local UI state, returning defaults when the file is missing or
    /// malformed.
    /// [ZH] 读取本地界面状态；文件缺失或损坏时返回默认值。
    /// </summary>
    public static AppSettings Load()
    {
        try
        {
            string path = FilePath;
            if (!File.Exists(path))
                return new AppSettings();
            string json = File.ReadAllText(path);
            AppSettings settings = JsonSerializer.Deserialize<AppSettings>(json, Options) ?? new AppSettings();
            settings.MigrateLegacyTheme(json);
            return settings;
        }
        catch
        {
            // [EN] A damaged state file must not prevent startup.
            // [ZH] 状态文件损坏不应影响启动。
            return new AppSettings();
        }
    }

    /// <summary>
    /// [EN] Migrate the legacy <c>DarkMode</c> boolean to the tri-state
    /// <see cref="Theme"/>.
    ///
    /// Migration runs only when the old file has no Theme field. A theme chosen
    /// in a newer version takes precedence. The legacy default was <c>dark</c>,
    /// so treating every old file as an explicit preference would flip all
    /// existing users to dark mode; discarding the field would lose a real user
    /// choice. A present legacy field is therefore treated as a preference.
    /// Fresh installations have no ui_state.json and follow the system theme.
    ///
    /// [ZH] 将旧版 <c>DarkMode</c> 布尔开关迁移到三态 <see cref="Theme"/>。
    ///
    /// [ZH] 仅当旧文件没有 Theme 字段时迁移；新版本中选择的主题优先。旧版默认写入
    /// [ZH] <c>dark</c>，若一律视为用户偏好会使所有老用户变为深色；直接丢弃又会丢失真实
    /// [ZH] 的手动选择。因此旧字段存在时按偏好处理。全新安装不含 ui_state.json，默认跟随
    /// [ZH] 系统主题。
    /// </summary>
    private void MigrateLegacyTheme(string json)
    {
        if (json.Contains("\"Theme\"", StringComparison.Ordinal))
            return;
        if (DarkMode is bool legacy)
            Theme = ThemeService.Serialize(legacy ? AppTheme.Dark : AppTheme.Light);
    }

    /// <summary>
    /// [EN] Persist local UI state. Failures are silent and do not interrupt use.
    /// [ZH] 写回本地界面状态；失败时静默处理，不打断使用。
    /// </summary>
    public void Save()
    {
        try
        {
            File.WriteAllText(FilePath, JsonSerializer.Serialize(this, Options));
        }
        catch
        {
            // [EN] Ignore write failures in read-only directories.
            // [ZH] 在只读目录等场景下忽略写入失败。
        }
    }
}
