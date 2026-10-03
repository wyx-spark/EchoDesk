using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace EchoDesk.Services;

/// <summary>
/// 前端本地界面状态（<c>ui_state.json</c>，放在程序目录下）。
///
/// 记录窗口大小/位置/最大化、字号、主题，以及"已拒绝创建快捷方式"等一次性询问结果，
/// 这样用户拒绝过的询问不会每次启动都重复弹出。
/// </summary>
public sealed class AppSettings
{
    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        // 旧字段（DarkMode）迁移后置空，不再写回文件；其余字段照常写出
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    /// <summary>用户已拒绝创建启动快捷方式（不再重复询问）。</summary>
    public bool ShortcutPromptDeclined { get; set; }

    /// <summary>是否已完成首次启动引导（只展示一次）。</summary>
    public bool FirstRunDone { get; set; }

    // ---------- 窗口状态记忆 ----------
    public double WindowWidth { get; set; }
    public double WindowHeight { get; set; }
    public double WindowLeft { get; set; } = double.NaN;
    public double WindowTop { get; set; } = double.NaN;
    public bool WindowMaximized { get; set; }

    // ---------- 界面偏好 ----------
    public double FontSize { get; set; } = 14;

    /// <summary>
    /// 主题（修复二）：<c>auto</c>（跟随系统）/ <c>light</c> / <c>dark</c>。
    /// </summary>
    public string Theme { get; set; } = ThemeService.Serialize(AppTheme.System);

    /// <summary>
    /// 旧版深色开关（v0.2.1 及以前）。保留字段仅为了**读旧文件**：
    /// 老版本写过 <c>DarkMode</c> 的用户已经表达过偏好，迁移时不能丢。
    /// 新版本一律以 <see cref="Theme"/> 为准，本字段不再写回。
    /// </summary>
    public bool? DarkMode { get; set; }

    /// <summary>
    /// emoji 输出策略（修复六）：<c>auto</c>（按本机字体自动判断）/ <c>always</c> / <c>never</c>。
    /// </summary>
    public string EmojiMode { get; set; } = "auto";

    /// <summary>
    /// 界面语言（任务书 3.4 持久化）：<c>auto</c> 表示按系统区域自动选择，
    /// 也可以写 <c>zh-Hans</c> / <c>en</c> 等具体语言标签。
    /// </summary>
    public string Language { get; set; } = "auto";

    /// <summary>开发者模式（任务书 2B.5）：开启详细日志与热重载；检测到 skills_dev 中有插件时自动生效。</summary>
    public bool DeveloperMode { get; set; }

    /// <summary>
    /// 执行有副作用且不可逆的指令（<c>/clear</c>、<c>/clearcache</c>）前是否二次确认。
    /// 默认开启：避免「输入 /clear 却执行了 /clearcache」这类误操作造成数据损失。
    /// </summary>
    public bool ConfirmDestructiveCommands { get; set; } = true;

    /// <summary>输入框下方轻量状态区是否收起（默认展开）。</summary>
    public bool StatusStripCollapsed { get; set; }

    /// <summary>
    /// 流式输出的界面合并间隔（毫秒，v0.2.1 优化已知问题-2 任务五）。
    ///
    /// 后端逐 token 发 delta，若前端每来一个就重排一次界面（重建 Markdown 文档 +
    /// 滚动到底），几百个 delta 就是几百次全量布局 —— 这正是"生成一半卡住、
    /// 等会儿冒出来好多"的界面侧原因。按 50ms 合并一次，人眼看不出延迟，
    /// 布局次数少一个数量级。设为 0 表示不合并（每个 delta 都更新，仅用于排查）。
    /// </summary>
    public int StreamThrottleMs { get; set; } = 50;

    private static string FilePath
    {
        get
        {
            // 修复七：界面状态放在用户数据目录（便携版布局下即外层目录），
            // 与 app\ 里的程序文件分开，便于用户查找与备份。
            string dir = ShortcutService.UserDataDirectory;
            return Path.Combine(dir, "ui_state.json");
        }
    }

    /// <summary>读取本地界面状态；文件缺失或损坏时返回默认值。</summary>
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
            return new AppSettings();  // 状态文件损坏不应影响启动
        }
    }

    /// <summary>
    /// 迁移 v0.2.1 及更早版本的 <c>DarkMode</c> 布尔开关到新的三态 <see cref="Theme"/>。
    ///
    /// 只在"旧文件里没有 Theme 字段"时迁移——用户在新版本里选过主题就以新字段为准。
    /// 旧版本默认写 <c>dark</c>，若一律当成用户偏好会把所有老用户的界面从浅色翻成深色，
    /// 但反过来直接丢弃会让"确实手动切过深色"的用户设置丢失，故按"旧字段存在即视为偏好"处理
    /// （打包时不会附带 ui_state.json，因此全新安装一律走"跟随系统"）。
    /// </summary>
    private void MigrateLegacyTheme(string json)
    {
        if (json.Contains("\"Theme\"", StringComparison.Ordinal))
            return;
        if (DarkMode is bool legacy)
            Theme = ThemeService.Serialize(legacy ? AppTheme.Dark : AppTheme.Light);
    }

    /// <summary>写回本地界面状态（失败静默，不打断使用）。</summary>
    public void Save()
    {
        try
        {
            File.WriteAllText(FilePath, JsonSerializer.Serialize(this, Options));
        }
        catch
        {
            // 只读目录等场景下忽略
        }
    }
}
