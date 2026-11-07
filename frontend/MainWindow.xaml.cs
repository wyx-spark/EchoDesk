// [EN] EchoDesk WPF frontend module: frontend/MainWindow.xaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/MainWindow.xaml.cs。类型与成员说明见下方注释。
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using EchoDesk.Services;
using EchoDesk.Utilities;

namespace EchoDesk;

public partial class MainWindow : Window
{
    /// <summary>退出前的感谢语（界面语言可切换，因此用属性而不是 const）。</summary>
    private static string ThanksMessage => Loc.T("main.thanks");

    private BackendService? _backend;
    private bool _isProcessing;
    private bool _stopRequested;
    private bool _stopMode;          // 发送按钮当前处于"停止"模式（流式生成中）
    private bool _commandLike;       // 聊天输入框出现指令格式（"/"+英文字母）
    private bool _commandRowOpen;    // 指令输入行是否展开
    private double _fontSize = 14;
    private TextBox? _paletteSource;
    private DispatcherTimer? _exitTimer;
    private int _exitCountdown;
    private readonly SnippetManager _snippets = new();
    // 本地界面状态（窗口大小/位置/字号/主题、已拒绝的询问），存于程序目录 ui_state.json
    private readonly AppSettings _settings = AppSettings.Load();
    // 下载/预热等实时进度在对话框中的"活"消息（内容块），复用更新避免刷屏
    private TextBox? _liveStatusContent;

    // 任务十二：最后一条用户消息（Alt+↑ 复制回输入框用）。
    // 只存一条字符串而不是整份消息列表：这个功能唯一需要的就是"上一条提问"。
    private string _lastUserMessage = "";

    // 流式 Markdown 实时渲染：节流计时器，边生成边把累积文本渲染进 RichTextBox
    private DispatcherTimer? _mdTimer;
    private RichTextBox? _mdTarget;
    private string _mdPending = "";
    // ---------- 流式节流（v0.2.1 优化已知问题-2 任务五） ----------
    // 后端逐 token 发，前端**每个 delta 都重排一次界面**才是卡顿的主因：
    // RichTextBox 重建 FlowDocument 与 ScrollToBottom 触发整页测量，几百个
    // delta 就是几百次全量布局。这里把"滚动到底"与"思考区重渲染"也纳入节流，
    // 只保留文本累积（Append，O(1)）是每 delta 都做的。
    private DispatcherTimer? _streamTickTimer;   // 50ms 合并一次滚动与思考区渲染
    private bool _scrollPending;
    private string? _thinkPending;               // 思考区待渲染文本（null = 无待办）
    private TextBlock? _thinkPendingCount;       // 与之配套的字数标签
    // ---------- 运行指标（任务七） ----------
    private DispatcherTimer? _metricsTimer;      // 生成中每 500ms 刷新一次指标显示
    private readonly Dictionary<string, string> _lastStats = new();
    private string _metricsState = "idle";       // idle / waiting / running / done / failed / stopped / hidden
    //: 生成开始的本地时刻（秒表用）。后端心跳每 1.5 秒才来一次，
    //: 秒表若只靠心跳会一顿一顿地跳；token 数/速度仍只认后端上报。
    private DateTime? _metricsStartUtc;
    //: 思考区的渲染目标（流式期间由节流器统一重渲染，见 ScheduleThinkRender）
    private RichTextBox? _thinkBackend;
    // 底部通知条计时器（下载完成/自动化完成/错误提示）
    private DispatcherTimer? _toastTimer;
    // 修复四：最近一次能力提示清单（点状态区的提示按钮时逐条展示）
    private List<string> _capabilityWarnings = new();
    // 设置窗口（单实例：再次点击时激活已有窗口）
    private SettingsWindow? _settingsWindow;

    // 聊天输入框中的指令前缀（"/" + 连续英文字母，允许只有一个 "/"）
    private static readonly Regex CommandPrefixRegex = new(@"^/[A-Za-z]*(?: [A-Za-z]+)*", RegexOptions.Compiled);

    // 指令数据（/help 第一行展示 /help 自身；Format 为参数格式，Description 用于完整帮助）
    // 说明（v0.2.1 任务三）：这张表是**后端不可用时的本地回退**。正常情况下
    // 指令面板的数据来自后端的 {"action":"commands"}，因此技能指令会自动出现在
    // 候选列表里，且命令说明随界面语言切换（任务书 2.6）。
    //
    // v0.2.2 硬编码清查 §4.1 A-3（第 3 批）：回退表原先整表写死中文（23 行 / 56 条字面量），
    // 后端未就绪或降级运行时英文界面会整屏显示中文。现在**只保留结构**（命令名 + 是否破坏性），
    // 文案从语言包 `cmd.items.<cmd>.{name,usage,desc}` 取 —— 那批键本轮已复制进前端语言包
    // （`frontend/Resources/Lang/*.main.json` 的 `cmd` 段），与 Avalonia 的
    // `Services/CommandCatalog.cs` 用**同一套键、同一套组装口径**，避免两个前端再分叉。
    private static readonly (string Command, bool Destructive)[] FallbackSpec =
    {
        ("/help", false),
        ("/mode", false),
        ("/agent", false),
        ("/mcp", false),
        ("/task", false),
        ("/preset", false),
        ("/compact", false),
        ("/file", false),
        ("/summarize", false),
        ("/search", false),
        ("/model", false),
        ("/clearcache", false),
        ("/prompt", false),
        ("/temp", false),
        ("/darkmode", false),
        ("/stop", false),
        ("/snippet", false),
        ("/status", false),
        ("/clear", false),
        ("/undo", false),
        ("/save", false),
        ("/about", false),
        ("/exit", false),
    };

    /// <summary>按当前语言现算回退指令表（字段口径与 <see cref="ApplyCommandsFromBackend"/> 一致）。</summary>
    private static List<CommandItem> BuildFallbackCommands()
    {
        var items = new List<CommandItem>();
        foreach ((string command, bool destructive) in FallbackSpec)
        {
            string desc = Loc.T($"cmd.items.{command}.desc");
            string usage = Loc.T($"cmd.items.{command}.usage");
            // 语言包里没有这条指令（极端情况：包被裁剪）→ 不伪造文案，直接不列出
            if (desc.StartsWith("cmd.items.", StringComparison.Ordinal)
                || usage.StartsWith("cmd.items.", StringComparison.Ordinal))
                continue;
            string format = usage.StartsWith(command, StringComparison.OrdinalIgnoreCase)
                ? usage[command.Length..].Trim()
                : usage.Trim();
            string shortText = desc.Split('\n')[0];
            if (shortText.Length > 40)
                shortText = shortText[..40] + "…";
            items.Add(new CommandItem(command, format, desc, shortText, format.Length > 0, destructive));
        }
        return items;
    }

    /// <summary>当前生效的指令表（后端返回的本地化列表，或本地回退）。</summary>
    private static List<CommandItem> Commands = BuildFallbackCommands();

    /// <summary>指令表是否已被后端数据替换（没换过时，切语言要重建回退表）。</summary>
    private static bool _commandsFromBackend;

    /// <summary>把后端 {"action":"commands"} 返回的本地化指令元数据换成当前指令表。</summary>
    internal static void ApplyCommandsFromBackend(JsonElement array)
    {
        if (array.ValueKind != JsonValueKind.Array)
            return;
        var items = new List<CommandItem>();
        foreach (JsonElement element in array.EnumerateArray())
        {
            string name = element.TryGetProperty("name", out JsonElement n) ? n.GetString() ?? "" : "";
            if (name.Length == 0)
                continue;
            string usage = element.TryGetProperty("usage", out JsonElement u) ? u.GetString() ?? name : name;
            string desc = element.TryGetProperty("desc", out JsonElement d) ? d.GetString() ?? "" : "";
            string label = element.TryGetProperty("label", out JsonElement l) ? l.GetString() ?? "" : "";
            bool destructive = element.TryGetProperty("destructive", out JsonElement x)
                               && x.ValueKind == JsonValueKind.True;
            string source = element.TryGetProperty("source", out JsonElement s) ? s.GetString() ?? "" : "";
            // 用法去掉命令名，只留参数格式（后端 usage 形如 "/mode <编号|缩写|全称>"）
            string format = usage.StartsWith(name, StringComparison.OrdinalIgnoreCase)
                ? usage[name.Length..].Trim()
                : usage.Trim();
            string shortText = desc.Split('\n')[0];
            if (shortText.Length > 40)
                shortText = shortText[..40] + "…";
            if (source == "skill" && label.Length > 0)
                shortText = $"🧩 {label}";
            items.Add(new CommandItem(name, format, desc, shortText, format.Length > 0, destructive));
        }
        if (items.Count > 0)
        {
            Commands = items;
            _commandsFromBackend = true;
        }
    }

    public MainWindow()
    {
        InitializeComponent();
        AllowDrop = true; // 允许文件拖拽
        // 多语言（任务三）：先按当前语言回填 XAML 里声明的 Loc.Key，再订阅切换事件
        Loc.Apply(this);
        Title = Loc.T("main.title", "EchoDesk {0} — 由{1}出品", AppVersion, LocalizedStudio());
        VersionText.Text = AppVersion;
        Loc.LanguageChanged += (_, _) => ApplyLanguage();
        Loaded += MainWindow_Loaded;
    }

    /// <summary>
    /// [EN] Current application version (for example <c>v0.2.3-alpha.1</c>).
    ///
    /// [EN] v0.2.3 changed two things. First, the property reads InformationalVersion in
    /// preference to AssemblyVersion: InformationalVersion allows a prerelease suffix such as
    /// `-alpha.1`, whereas AssemblyVersion must be four purely numeric fields under Windows rules,
    /// so reading it would display this preview build as v0.2.3. Second, the hard-coded fallback
    /// literal was removed, because it was a second source of truth that expired on every version
    /// bump. Build metadata (<c>+&lt;commit&gt;</c>) is never shown in the interface.
    ///
    /// [ZH] 当前程序版本（如 <c>v0.2.3-alpha.1</c>）。
    ///
    /// [ZH] v0.2.3 改了两处：**优先读 InformationalVersion**（它允许 `-alpha.1` 这类预发布
    /// 后缀，而 AssemblyVersion 按 Windows 规则只能是四段纯数字，读它会把预览版显示成 v0.2.3），
    /// 同时去掉了原来兜底的硬编码字面量（那是个会在升版本后过期的第二处真相）。
    /// 构建元数据（<c>+&lt;commit&gt;</c>）不进界面。
    /// </summary>
    internal static string AppVersion
    {
        get
        {
            Assembly assembly = typeof(MainWindow).Assembly;
            string? informational = assembly
                .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
            if (!string.IsNullOrWhiteSpace(informational))
            {
                int plus = informational.IndexOf('+');
                string text = (plus >= 0 ? informational[..plus] : informational).Trim();
                if (text.Length > 0)
                    return text.StartsWith('v') ? text : "v" + text;
            }
            Version? version = assembly.GetName().Version;
            return version is null ? "v0.0.0" : $"v{version.Major}.{version.Minor}.{version.Build}";
        }
    }

    /// <summary>
    /// [EN] The project's Releases page. After WPF moved into LTS in v0.2.3, both LTS notices point
    /// here. It uses <c>releases/latest</c> instead of a hard-coded version number so the buttons
    /// keep working when a new version ships.
    ///
    /// [ZH] 项目 Release 页面（v0.2.3：WPF 转入 LTS 之后，两条 LTS 提示的按钮都指向这里）。
    /// 用 <c>releases/latest</c> 而不是写死某个版本号：新版本发布后按钮不需要改。
    /// </summary>
    internal const string ReleasesUrl = "https://github.com/wyx-spark/EchoDesk/releases/latest";

    /// <summary>
    /// [EN] Project home page, shown on the About page and identical to the address in the backend
    /// key <c>about.repo</c>.
    ///
    /// [ZH] 项目主页（「关于」页展示用，与后端 about.repo 的地址一致）。
    /// </summary>
    internal const string ProjectUrl = "https://github.com/wyx-spark/EchoDesk";

    /// <summary>
    /// [EN] Open an http(s) link in the system default browser.
    ///
    /// [EN] Only http and https are accepted: if another button reuses this entry point later, a
    /// value such as <c>file:</c> from an external source still cannot turn one click into local
    /// code execution.
    ///
    /// [ZH] 用系统默认浏览器打开一条 http(s) 链接。
    ///
    /// [ZH] 只放行 http / https：这个入口将来若被其它按钮复用，也不会因为传入
    /// <c>file:</c> 之类的外部输入而变成"点一下执行本地程序"。
    /// </summary>
    internal static void OpenExternalLink(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out Uri? uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
            return;
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = uri.AbsoluteUri,
                UseShellExecute = true,
            });
        }
        catch (Exception ex)
        {
            App.Log("MainWindow.OpenExternalLink", ex);
        }
    }

    /// <summary>
    /// [EN] The "Download the Avalonia edition" button on the LTS notice bar: it opens the latest
    /// Releases page and never downloads or installs anything inside the application.
    ///
    /// [ZH] LTS 提示条上的「下载 Avalonia 版本」：跳到 Releases 最新版，不在程序内下载。
    /// </summary>
    private void LtsDownloadButton_Click(object sender, RoutedEventArgs e) => OpenExternalLink(ReleasesUrl);

    private const string StudioName = "Void Entropia Studio";

    /// <summary>
    /// 当前语言下的出品方**简称**（任务书「工作室名称敲定」）。
    ///
    /// 为什么不能直接用 <see cref="StudioName"/> 或后端 `ready` 里的 `studio` 字段：
    /// 那两个都是英文标识（法律主体、GitHub 账号、构建元数据一律保持英文），
    /// 直接显示会让中文界面出现英文名。显示值一律走语言包，
    /// 语言包缺失时才回退到英文常量。
    /// </summary>
    private static string LocalizedStudio(string? fallback = null) =>
        Loc.T("main.studioName", fallback ?? StudioName);

    /// <summary>
    /// 首次出现用的**完整形式**——历史上这里是「熵寂工作室（Void-Entropia-Studio）」这种
    /// 中英并存写法，用意是"第一次就交代中英对应关系"。
    ///
    /// **2026-10-07 总策划裁定七改为全中文**：版权声明一律不再中英混合，
    /// 所以中文语言包里的完整形式与简称现在**同为「熵寂工作室」**。
    /// 英文语言包对应地只留 "Void Entropia Studio"（原先那份带中文括注的同样去掉，
    /// 否则英文界面里会冒出中文，两个语言包各自内部自洽）。
    /// 这个函数保留：它仍表示"规范署名位"的取值入口（`/about` 用它），
    /// 日后若中文侧需要更完整的中文表达（例如加年份），只改语言包即可。
    /// </summary>
    private static string LocalizedStudioFull(string? fallback = null) =>
        Loc.T("main.studioNameFull", fallback ?? StudioName);

    /// <summary>切换语言后重跑一遍文本回填，并刷新动态状态文本（无需重启）。</summary>
    internal void ApplyLanguage()
    {
        Loc.Apply(this);
        Title = Loc.T("main.title", "EchoDesk {0} — 由{1}出品", AppVersion, LocalizedStudio());
        if (!_isProcessing)
            StatusText.Text = Loc.T("main.status.ready");
        // 回退指令表的文案来自语言包，切语言时要重建（后端数据不在时它才是当前表）。
        // 不清空已选中的候选列表：重建只换文案，命令名与顺序不变。
        if (!_commandsFromBackend)
            Commands = BuildFallbackCommands();
        ApplyStatusStripState();
        CommandButtonText.Text = _commandRowOpen
            ? Loc.T("main.command.close") : Loc.T("main.command.open");
        SnippetButtonText.Text = SnippetPalette.IsOpen
            ? Loc.T("main.snippet.close") : Loc.T("main.snippet.open");
        if (_backend is not null)
            _ = RefreshStatusBarAsync();
    }

    // ---------- 生命周期 ----------

    private async void MainWindow_Loaded(object sender, RoutedEventArgs e)
    {
        RestoreWindowState();
        // 主题与字号来自上次会话（ui_state.json）
        // 修复二：这里原来只在"配置为浅色"时调 ApplyTheme，导致配置为深色时
        // 启动界面仍是 App.xaml 合并进去的浅色字典，深色永远出不来。
        // 现在无条件交给 ThemeService 应用一次（含"跟随系统"的解析）。
        ThemeService.Apply(ThemeService.Parse(_settings.Theme), notify: false);
        ThemeService.HookSystemChanges();
        ThemeService.Changed += OnThemeChanged;
        // 修复六：按本机字体与用户偏好决定是否输出 emoji 图标
        Emoji.Configure(_settings.EmojiMode);
        _fontSize = Math.Clamp(_settings.FontSize, 10, 24);
        UpdateAllMessageFontSize();
        ApplyStatusStripState();   // 优化五：按上次的选择展开/折叠轻量状态区

        _backend = new BackendService();
        // 后端连接完成前禁用全部控件，并提示连接状态
        SetControlsEnabled(false);
        StatusText.Text = Loc.T("main.status.starting");
        AddMessage("system", Loc.T("main.connecting", "⏳ 正在连接后端，请稍候..."));
        // 实时显示后端起模型下载等 stderr 进度
        _backend.StderrReceived += Backend_StderrReceived;
        // v0.2.2 第 4 批：宿主的**结构化**进度事件（后端在处理非流式请求期间主动发来）。
        // 渲染成"活消息"——同一条系统气泡就地更新，而不是像 stderr 那样只在状态栏闪一下。
        // WPF 侧同样要接：这条协议扩展是给两个前端共用的，只改一个会让另一个漏掉进度。
        _backend.HostProgressReceived += Backend_HostProgressReceived;
        // 后端异常退出时在对话框提示，避免用户只看上方状态栏
        _backend.ProcessExited += Backend_ProcessExited;
        try
        {
            // 等待后端完成模型加载 + 会话恢复 + 预热后就绪
            await _backend.StartAsync();

            // 见下方 SyncBackendLanguageOnStartup 的说明：后端"就绪"和"要下载模型"
            // 是两条不同路径，语言同步两条都要做。
            await SyncBackendLanguageOnStartup();
        }
        catch (Exception ex)
        {
            StatusText.Text = Loc.T("main.status.startFailed", "后端启动失败");
            AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message));
            // 发布构建不含模型时，后端会先回 need_download（StartAsync 因此抛错），
            // 但进程是活的、后续请求照常可用 —— 语言同步**必须**在这条路径上补做，
            // 否则会出现"英文界面 + 后端讲中文"（Agent 工具说明、模型数据源、插件名）。
            await SyncBackendLanguageOnStartup();
            SetControlsEnabled(false); // 保持禁用
            return;
        }

        // 任务六：模型状态一变（下载完成/热加载）就自动切换界面，无需重启前端
        _backend.ModelReadyChanged += Backend_ModelReadyChanged;
        _backend.StderrReceived -= Backend_StderrReceived;
        // 任务一/任务六：无模型时**不再**禁用控件、也不再退出 ——
        // 文件解析、设置读写、模型市场、会话历史都要能用，只有推理走引导。
        SetControlsEnabled(true);
        if (_backend.ModelReady)
            StatusText.Text = Loc.T("main.status.ready");
        else
            EnterNoModelMode();
        Title = Loc.T("main.title", "EchoDesk {0} — 由{1}出品", _backend.Version, LocalizedStudio(_backend.Studio));
        string welcome = Loc.T("main.welcome", "✨ 欢迎使用 EchoDesk {0} — 由{1}出品",
                               _backend.Version, LocalizedStudioFull(_backend.Studio));
        if (_backend.RestoredCount > 0)
            welcome += "\n" + Loc.T("main.welcomeRestored", "📂 已自动恢复上次会话（{0} 轮）",
                                     _backend.RestoredCount);
        AddMessage("system", welcome);
        // 展示恢复的历史消息，让用户能查看之前的对话内容
        foreach (var (role, content, reasoning) in _backend.RestoredHistory)
        {
            if (role == "user")
                AddMessage("user", content);
            else if (role == "assistant")
                AddMessage("assistant", content, reasoning);
            else
                AddMessage("system", content);
        }
        MessageScrollViewer.ScrollToBottom();
        InputBox.Focus();
        _ = UpdateContextAsync(); // 显示初始上下文占用
        _ = RefreshStatusBarAsync(); // 状态栏：模型 / 模式 / 搜索状态
        _ = RefreshCommandsAsync();  // 任务三：指令面板数据源改为后端（含技能指令、随语言切换）
        _ = CheckUpdateAsync();      // 任务五 方案 A：只检查与提示，不自动下载替换
        ShowFirstRunGuideIfNeeded();
        CheckStartupShortcut();   // 启动时检测程序目录下的启动快捷方式
    }

    /// <summary>
    /// 从后端取本地化的指令元数据（含技能注册的指令），刷新指令面板。
    /// 内置的那份静态表只在后端不可用时兜底，因此换语言或装插件后指令列表自动跟着变。
    /// </summary>
    private async Task RefreshCommandsAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "commands" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("commands", out JsonElement commands))
                ApplyCommandsFromBackend(commands);
        }
        catch
        {
            // 后端忙碌/异常时保留现有指令表
        }
    }

    private string _updateUrl = "";

    /// <summary>
    /// 检查新版本（任务书 任务五 · 方案 A：只检查与提示）。
    /// 发现新版本时状态区出现一个可点条目，点击用系统浏览器打开 Releases 页面；
    /// 全程不下载、不替换文件，失败也不影响使用（只记一条日志）。
    /// </summary>
    private async Task CheckUpdateAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "update_check" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("update", out JsonElement update))
                return;
            string status = update.TryGetProperty("status", out JsonElement st) ? st.GetString() ?? "" : "";
            if (!string.Equals(status, "available", StringComparison.Ordinal))
                return;
            _updateUrl = update.TryGetProperty("url", out JsonElement url) ? url.GetString() ?? "" : "";
            string latest = update.TryGetProperty("latest", out JsonElement l) ? l.GetString() ?? "" : "";
            if (StripUpdateButton is null || _updateUrl.Length == 0)
                return;
            StripUpdateText.Text = Loc.T("main.update.notice", "🎉 发现新版本 {0}（当前 {1}）", latest, AppVersion);
            StripUpdateButton.ToolTip = Loc.T("main.update.tip", "点击打开下载页面") + "\n" + _updateUrl;
            StripUpdateButton.Visibility = Visibility.Visible;
        }
        catch (Exception ex)
        {
            App.Log("MainWindow.CheckUpdate", ex);
        }
    }

    private void StripUpdateButton_Click(object sender, RoutedEventArgs e)
    {
        if (_updateUrl.Length == 0)
            return;
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = _updateUrl,
                UseShellExecute = true,
            });
        }
        catch (Exception ex)
        {
            Toast(Loc.T("main.update.checkFailed", "检查更新失败：{0}", ex.Message), error: true);
        }
    }

    // ---------- 窗口状态记忆（大小/位置/最大化，兼容多显示器与高 DPI） ----------

    /// <summary>恢复上次的窗口大小、位置与最大化状态；位置已不可见时回退居中。</summary>
    private void RestoreWindowState()
    {
        if (_settings.WindowWidth >= 400 && _settings.WindowHeight >= 320)
        {
            Width = _settings.WindowWidth;
            Height = _settings.WindowHeight;
        }
        if (!double.IsNaN(_settings.WindowLeft) && !double.IsNaN(_settings.WindowTop)
            && IsRectVisible(_settings.WindowLeft, _settings.WindowTop, Width, Height))
        {
            WindowStartupLocation = WindowStartupLocation.Manual;
            Left = _settings.WindowLeft;
            Top = _settings.WindowTop;
        }
        if (_settings.WindowMaximized)
            WindowState = WindowState.Maximized;
    }

    /// <summary>窗口矩形是否与虚拟屏幕（含所有显示器）相交，避免还原到已拔掉的显示器上。</summary>
    private static bool IsRectVisible(double left, double top, double width, double height)
    {
        var virtualScreen = new Rect(
            SystemParameters.VirtualScreenLeft,
            SystemParameters.VirtualScreenTop,
            SystemParameters.VirtualScreenWidth,
            SystemParameters.VirtualScreenHeight);
        var window = new Rect(left, top, width, height);
        // 至少露出一部分标题栏才认为可见
        return virtualScreen.IntersectsWith(window)
            && window.Left + window.Width > virtualScreen.Left + 40
            && window.Top + window.Height > virtualScreen.Top + 20;
    }

    /// <summary>关闭时保存窗口状态（最大化时记录还原后的尺寸，下次还原才不会撑满屏）。</summary>
    private void SaveWindowState()
    {
        Rect bounds = WindowState == WindowState.Normal
            ? new Rect(Left, Top, Width, Height)
            : RestoreBounds;
        if (bounds.Width >= 400 && bounds.Height >= 320)
        {
            _settings.WindowWidth = bounds.Width;
            _settings.WindowHeight = bounds.Height;
            _settings.WindowLeft = bounds.Left;
            _settings.WindowTop = bounds.Top;
        }
        _settings.WindowMaximized = WindowState == WindowState.Maximized;
        _settings.FontSize = _fontSize;
        // 主题由 ThemeService 负责持久化（修复二）：这里不再回写 DarkMode，
        // 否则会把"跟随系统"覆盖成固定值。
        _settings.Theme = ThemeService.Serialize(ThemeService.Selected);
        _settings.Save();
    }

    // ---------- 状态栏（顶部关键状态 + 输入框下方轻量状态区） ----------

    /// <summary>向后端查询 /status 与自动化/市场状态并刷新状态栏。</summary>
    private async Task RefreshStatusBarAsync()
    {
        if (_backend is null)
            return;
        try
        {
            // 任务三：改读机器可读字段（model/mode/search/kv_cache_text），
            // 不再解析 /status 的中文文本——否则界面换成英文后状态栏会失效。
            string json = await _backend.SendRequestAsync(new { action = "context" });
            using JsonDocument doc = JsonDocument.Parse(json);
            ApplyStatusFields(doc.RootElement);
        }
        catch
        {
            // 后端忙碌/异常时忽略，后续交互会再次刷新
            return;
        }
        await RefreshAutomationStatusAsync();
        await RefreshMarketStatusAsync();
    }

    /// <summary>刷新状态区的自动化状态段（优化五：图标 + 简短短语）。</summary>
    private async Task RefreshAutomationStatusAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "automation_status" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("automation", out JsonElement st))
                return;
            bool running = st.TryGetProperty("running", out JsonElement r) && r.ValueKind == JsonValueKind.True;
            bool enabled = st.TryGetProperty("enabled", out JsonElement en) && en.ValueKind == JsonValueKind.True;
            SetAutomationStatus(running, enabled);
        }
        catch
        {
            // 忽略
        }
    }

    /// <summary>刷新状态区的技能市场连接段。</summary>
    private async Task RefreshMarketStatusAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "market_status" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("market_status", out JsonElement st))
                return;
            bool enabled = st.TryGetProperty("enabled", out JsonElement en) && en.ValueKind == JsonValueKind.True;
            string state = st.TryGetProperty("state", out JsonElement s) ? s.GetString() ?? "" : "";
            SetMarketStatus(!enabled
                ? Loc.T("settings.market.state.disabled")
                : state.Length > 0 ? TranslateMarketState(state) : Loc.T("settings.market.state.ready"));
        }
        catch
        {
            // 忽略
        }
    }

    /// <summary>按后端返回的字段刷新顶部关键状态与状态区（与界面语言无关）。</summary>
    private void ApplyStatusFields(JsonElement root)
    {
        // 任务六：无模型时状态栏必须明确显示"无模型"，而不是留空或显示一个并不存在
        // 的模型名（config 里仍有默认文件名，但那个文件并没有被加载）。
        bool modelReady = !(root.TryGetProperty("model_ready", out JsonElement readyEl)
                            && readyEl.ValueKind == JsonValueKind.False);
        if (!modelReady)
        {
            ModelText.Text = Loc.T("main.model.noModel", "🧠 模型: 无模型");
            ModelChip.ToolTip = Loc.T("main.model.noModelTip",
                "当前没有可用的本地模型。点击打开「设置 → 模型」下载或加载模型。");
        }
        else if (root.TryGetProperty("model", out JsonElement modelEl) && modelEl.ValueKind == JsonValueKind.String)
        {
            string model = modelEl.GetString() ?? "";
            if (model.Length > 0)
            {
                ModelText.Text = Loc.T("main.model.chip", "🧠 模型: {0}", Shorten(model));
                ModelChip.ToolTip = Loc.T("main.model.tip", "当前模型：{0}", model);
            }
        }
        if (root.TryGetProperty("mode", out JsonElement modeEl) && modeEl.ValueKind == JsonValueKind.String)
        {
            string mode = modeEl.GetString() ?? "";
            if (mode.Length > 0)
                SetModeStatus(mode);
        }
        if (root.TryGetProperty("search", out JsonElement searchEl)
            && searchEl.ValueKind is JsonValueKind.True or JsonValueKind.False)
        {
            SetSearchStatus(searchEl.GetBoolean());
        }
        if (root.TryGetProperty("kv_cache_text", out JsonElement kvEl) && kvEl.ValueKind == JsonValueKind.String)
            SetKvCacheStatus(kvEl.GetString() ?? "");
        // 修复四（方向七）：模型能力与实测速度也进状态区
        if (root.TryGetProperty("capability", out JsonElement capEl) && capEl.ValueKind == JsonValueKind.Object)
            SetCapabilityStatus(capEl);
    }

    /// <summary>
    /// 状态区：模型能力（修复四 方向七）。
    ///
    /// 显示"参数量 · 量化 · 可用窗口 · 实测速度"四项——这是用户判断
    /// "这个模型能不能干这件事"所需要的最小信息集。四项都缺时保持占位文本，
    /// 不显示空白（空白会让人以为界面坏了）。
    /// </summary>
    private void SetCapabilityStatus(JsonElement capability)
    {
        string parameters = GetStringField(capability, "parameters_text");
        string quantization = GetStringField(capability, "quantization");
        int usable = GetIntField(capability, "usable_context");
        double tps = GetDoubleField(capability, "last_tps");

        var parts = new List<string>();
        if (parameters.Length > 0)
            parts.Add(parameters);
        if (quantization.Length > 0)
            parts.Add(quantization);
        if (usable > 0)
            parts.Add($"{usable / 1024}k");
        if (tps > 0)
            parts.Add(Loc.T("main.strip.capabilityTps", "{0:0.#} 字/秒", tps));
        if (parts.Count == 0)
            return;
        StripCapabilityText.Text = Loc.T("main.strip.capability", "🧠 {0}", string.Join(" · ", parts));

        var tips = new List<string>
        {
            Loc.T("main.strip.capabilityDetail", "参数量 {0} · 量化 {1} · 可用窗口 {2} tokens",
                parameters.Length > 0 ? parameters : Loc.T("main.value.unknown", "未知"),
                quantization.Length > 0 ? quantization : Loc.T("main.value.unknown", "未知"),
                usable > 0 ? usable.ToString() : Loc.T("main.value.unknown", "未知")),
        };
        if (tps > 0)
            tips.Add(Loc.T("main.strip.capabilitySpeed", "最近一次生成速度 {0:0.#} 字/秒", tps));
        tips.Add(Loc.T("main.strip.capabilityClick", "点击打开「设置 → 模型」查看完整能力与推荐配置"));
        StripCapabilityButton.ToolTip = string.Join("\n", tips);

        // 能力提示按钮：有告警才出现（文档过长 / 显存不足 / 速度过低）
        List<string> warnings = GetStringListField(capability, "warnings");
        _capabilityWarnings = warnings;
        StripWarnButton.Visibility = warnings.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        if (warnings.Count > 0)
        {
            StripWarnText.Text = Loc.T("main.strip.warn", "⚠️ 能力提示 {0}", warnings.Count);
            StripWarnButton.ToolTip = string.Join("\n", warnings.Select(w => "• " + w));
        }
    }

    private static string GetStringField(JsonElement node, string name)
        => node.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? "" : "";

    private static int GetIntField(JsonElement node, string name)
        => node.TryGetProperty(name, out JsonElement el)
           && el.ValueKind == JsonValueKind.Number && el.TryGetInt32(out int value) ? value : 0;

    private static double GetDoubleField(JsonElement node, string name)
        => node.TryGetProperty(name, out JsonElement el)
           && el.ValueKind == JsonValueKind.Number && el.TryGetDouble(out double value) ? value : 0;

    private static List<string> GetStringListField(JsonElement node, string name)
    {
        var list = new List<string>();
        if (node.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in el.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.String)
                    list.Add(item.GetString() ?? "");
            }
        }
        return list.Where(x => x.Length > 0).ToList();
    }

    /// <summary>更新顶部"模式"胶囊（/mode 切换后调用）。</summary>
    private void SetModeStatus(string mode)
    {
        ModeText.Text = Loc.T("main.mode.chip", "🛠️ 模式: {0}", mode);
        ModeChip.ToolTip = Loc.T("main.mode.tip", "当前对话模式：{0}", mode);
    }

    /// <summary>状态区：搜索开关（优化五）。</summary>
    private void SetSearchStatus(bool on)
    {
        string state = on ? Loc.T("main.strip.on") : Loc.T("main.strip.off");
        SearchText.Text = Loc.T("main.strip.search", "🔍 搜索：{0}", state);
        StripSearchButton.ToolTip = on
            ? Loc.T("main.strip.searchOnTip", "联网搜索已开启：提问前会先搜索网络参考信息。点击关闭")
            : Loc.T("main.strip.searchOffTip", "联网搜索已关闭。点击开启（/search on）");
    }

    /// <summary>状态区：KV Cache 估算（优化五）。</summary>
    private void SetKvCacheStatus(string text)
    {
        if (string.IsNullOrWhiteSpace(text) || text == "---")
            return;
        StripKvText.Text = Loc.T("main.strip.kv", "💾 KV: {0}", text);
        StripKvButton.ToolTip = Loc.T("main.strip.kvValueTip",
            "KV Cache 估算约 {0}（fp16，随上下文窗口线性增长）\n点击打开「设置 → 模型」", text);
    }

    /// <summary>状态栏模型名过长时截断显示。</summary>
    private static string Shorten(string name)
    {
        string trimmed = name.EndsWith(".gguf", StringComparison.OrdinalIgnoreCase) ? name[..^5] : name;
        return trimmed.Length <= 26 ? trimmed : trimmed[..25] + "…";
    }

    /// <summary>刷新状态区的自动化状态段（优化五：图标 + 简短短语）。</summary>
    private void SetAutomationStatus(bool running, bool enabled = true)
    {
        string state = !enabled ? Loc.T("main.automation.disabled", "未启用")
            : running ? Loc.T("main.automation.running", "运行中")
            : Loc.T("main.strip.off");
        AutoText.Text = Loc.T("main.strip.automation", "⚙️ 自动化：{0}", state);
        StripAutomationButton.ToolTip = running
            ? Loc.T("main.strip.autoRunningTip", "自动化正在运行（浏览器已打开）。点击打开「设置 → 自动化」查看日志或停止")
            : enabled
                ? Loc.T("main.strip.autoIdleTip", "自动化已启用，当前空闲。点击打开「设置 → 自动化」")
                : Loc.T("main.strip.autoDisabledTip", "自动化未启用（需先勾选安全协议）。点击打开「设置 → 自动化」");
    }

    /// <summary>把后端返回的市场状态串（中文常量）映射成当前界面语言的文案。</summary>
    private static string TranslateMarketState(string state) => state switch
    {
        "已禁用" => Loc.T("settings.market.state.disabled"),
        "未同步" => Loc.T("settings.market.state.unsynced"),
        "已就绪" => Loc.T("settings.market.state.ready"),
        "缓存已过期" => Loc.T("settings.market.state.stale"),
        "已同步（当前无匹配技能）" => Loc.T("settings.market.state.syncedEmpty", "已同步（当前无匹配技能）"),
        _ => state,
    };

    /// <summary>刷新状态区的技能市场连接段（优化五）。</summary>
    private void SetMarketStatus(string text)
    {
        MarketText.Text = Loc.T("main.strip.market", "🌐 市场：{0}", text);
        StripMarketButton.ToolTip = Loc.T("main.strip.marketValueTip",
            "技能市场：{0}\n点击打开「设置 → 技能市场」", text);
    }

    // ---------- 轻量状态区：点击跳转 / 折叠（优化五） ----------

    private void ModelChip_Click(object sender, RoutedEventArgs e) => OpenSettings(tab: "模型");

    private void ModeChip_Click(object sender, RoutedEventArgs e) => OpenSettings(tab: "常规");

    /// <summary>搜索项直接切换开关（搜索没有独立的设置页，切换即"对应设置"）。</summary>
    private async void StripSearchButton_Click(object sender, RoutedEventArgs e)
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "chat", message = "/search toggle" });
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
            HandleChatResponse(json);
            Toast(content.Length > 0
                ? content
                : Loc.T("main.strip.searchToggled", "已切换联网搜索"));
        }
        catch (Exception ex)
        {
            Toast(Loc.T("main.strip.searchToggleFailed", "切换搜索失败：{0}", ex.Message), error: true);
        }
    }

    private void StripAutomationButton_Click(object sender, RoutedEventArgs e) => OpenSettings(tab: "自动化");

    private void StripMarketButton_Click(object sender, RoutedEventArgs e) => OpenSettings(tab: "技能市场");

    private void StripContextButton_Click(object sender, RoutedEventArgs e) => _ = UpdateContextAsync();

    private void StripKvButton_Click(object sender, RoutedEventArgs e) => OpenSettings(tab: "模型");

    // ---------- 修复四：能力提示与手动压缩上下文 ----------

    private void StripCapabilityButton_Click(object sender, RoutedEventArgs e) => OpenSettings(tab: "模型");

    /// <summary>能力提示按钮：把完整告警逐条讲清楚（状态区只放得下一个数字）。</summary>
    private void StripWarnButton_Click(object sender, RoutedEventArgs e)
    {
        if (_capabilityWarnings.Count == 0)
            return;
        MessageBox.Show(this,
            string.Join("\n\n", _capabilityWarnings.Select((w, i) => $"{i + 1}. {w}")),
            Loc.T("main.strip.warnTitle", "模型能力提示"),
            MessageBoxButton.OK, MessageBoxImage.Information);
    }

    /// <summary>
    /// 手动压缩上下文（修复四 基础要求）。
    ///
    /// 走 `context_compress` 动作而不是让用户自己等自动触发：准备上传大文件、
    /// 或者上下文已经 70% 时，用户需要一个"立刻腾地方"的按钮。
    /// </summary>
    private async void StripCompactButton_Click(object sender, RoutedEventArgs e)
    {
        if (_backend is null)
            return;
        StripCompactButton.IsEnabled = false;
        StripCompactText.Text = Loc.T("main.strip.compacting", "🗜 压缩中…");
        try
        {
            string json = await _backend.SendRequestAsync(
                new { action = "context_compress", force = true },
                idleTimeout: BackendService.HeavyIdleTimeout);
            using JsonDocument doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("context", out JsonElement context))
                ApplyStatusFields(context);
            string message = Loc.T("main.strip.compactNone", "上下文无需压缩");
            if (doc.RootElement.TryGetProperty("compress", out JsonElement compress)
                && compress.ValueKind == JsonValueKind.Object)
            {
                bool compressed = compress.TryGetProperty("compressed", out JsonElement c)
                                  && c.ValueKind == JsonValueKind.True;
                if (compressed)
                {
                    int dropped = GetIntField(compress, "dropped");
                    int before = GetIntField(compress, "used_before");
                    int after = GetIntField(compress, "used_after");
                    message = after > 0
                        ? Loc.T("main.strip.compactDone", "已压缩：移出 {0} 条（{1} → {2} tokens）",
                                dropped, before, after)
                        : Loc.T("main.strip.compactDoneShort", "已压缩：移出 {0} 条较早消息", dropped);
                }
            }
            AddMessage("system", Loc.T("main.strip.compactResult", "🗜 {0}", message));
            Toast(message);
        }
        catch (BackendBusyException)
        {
            Toast(Loc.T("main.error.busy"), error: true);
        }
        catch (Exception ex)
        {
            App.Log("MainWindow.StripCompact", ex);
            Toast(Loc.T("main.strip.compactFailed", "❌ 压缩上下文失败：{0}", ex.Message), error: true);
        }
        finally
        {
            StripCompactText.Text = Loc.T("main.strip.compact", "🗜 压缩上下文");
            StripCompactButton.IsEnabled = true;
        }
    }

    /// <summary>折叠/展开轻量状态区（状态记在 ui_state.json，下次启动沿用）。</summary>
    private void StripToggleButton_Click(object sender, RoutedEventArgs e)
    {
        _settings.StatusStripCollapsed = !_settings.StatusStripCollapsed;
        _settings.Save();
        ApplyStatusStripState();
    }

    /// <summary>按 ui_state.json 应用状态区的折叠状态。</summary>
    private void ApplyStatusStripState()
    {
        bool collapsed = _settings.StatusStripCollapsed;
        StatusStripItems.Visibility = collapsed ? Visibility.Collapsed : Visibility.Visible;
        StripToggleText.Text = collapsed
            ? Loc.T("main.strip.toggleExpand", "▸")
            : Loc.T("main.strip.toggleCollapse", "▾");
        StripToggleButton.ToolTip = collapsed
            ? Loc.T("main.strip.expandTip", "展开状态区（搜索 / 自动化 / 市场 / 上下文 / KV Cache）")
            : Loc.T("main.strip.collapseTip", "折叠状态区");
        AutomationProperties.SetName(StripToggleButton, collapsed
            ? Loc.T("main.strip.expandTip", "展开状态区")
            : Loc.T("main.strip.collapseTip", "折叠状态区"));
    }

    // ---------- 首次启动引导 ----------

    /// <summary>首次启动时展示欢迎与上手引导（模型下载、快捷方式、常用操作）。</summary>
    private void ShowFirstRunGuideIfNeeded()
    {
        if (_settings.FirstRunDone)
            return;
        _settings.FirstRunDone = true;
        _settings.Save();
        AddMessage("system", Loc.T("main.guide.firstRun",
            "🎉 首次使用引导\n" +
            "1️⃣ 模型：首次启动若本地缺少模型，会询问是否下载；也可手动把 .gguf 放到 models/ 目录。\n" +
            "2️⃣ 启动方式：可在程序目录创建「EchoDesk 启动」快捷方式，双击即可运行（已按你的选择处理）。\n" +
            "3️⃣ 长文档：上传合同等长文件时，超出上下文窗口会自动分段摘要后再回答，不会截断丢信息。\n" +
            "4️⃣ 常用指令：输入 /help 查看全部指令；输入 / 会弹出指令列表。\n" +
            "5️⃣ 复制内容：每条消息右下角有「复制 / Markdown / 代码」按钮，也可拖选文本后按 Ctrl+C。\n" +
            "6️⃣ 快捷键：Ctrl+Enter 发送 · Esc 停止 · Ctrl+L 清空 · Ctrl+O 上传 · Ctrl+Shift+C 复制全部 · Ctrl+滚轮 调字号"));
        Toast(Loc.T("main.guide.toast", "🎉 欢迎首次使用 EchoDesk，已显示上手引导"));
    }

    /// <summary>
    /// 进入"无模型"界面状态（v0.2.1 综合优化 任务六）。
    ///
    /// 关键点：**不禁用任何控件、不退出**。文件解析、设置读写、模型市场、会话历史
    /// 都不依赖模型，必须照常可用；只有推理会返回引导。旧行为在这里会弹一个
    /// "下载否则退出"的二选一，用户拒绝后整个应用不可用 —— 那正是本轮要拆掉的墙角。
    /// 引导里会给出模型文件夹的**真实路径**，并提示可以点模型状态条直接进模型市场。
    /// </summary>
    private void EnterNoModelMode()
    {
        StatusText.Text = Loc.T("main.status.noModel", "无模型（仍可使用文件解析与设置）");
        ModelText.Text = Loc.T("main.model.noModel", "🧠 模型: 无模型");
        ModelChip.ToolTip = Loc.T("main.model.noModelTip",
            "当前没有可用的本地模型。点击打开「设置 → 模型」下载或加载模型。");
        string guide = _backend?.ModelGuide ?? "";
        if (guide.Length == 0)
        {
            guide = Loc.T("main.noModel.guideFallback",
                "⚠️ 当前没有可用的本地模型，推理不可用（文件解析、设置、模型市场仍可正常使用）。\n" +
                "可以点顶部的模型状态条打开「设置 → 模型 → 模型市场」下载，或把 .gguf 文件放进：\n{0}",
                _backend?.ModelsDir ?? "");
        }
        AddMessage("system", guide);
        Toast(Loc.T("main.noModel.toast",
            "⚠️ 未检测到本地模型：下载或手动放入后会自动加载，无需重启"));
    }

    /// <summary>
    /// 模型可用状态变化时的界面切换（任务六）：下载完成/热加载成功后自动转为正常状态，
    /// 模型被删除或加载失败则回到"无模型"引导 —— 全程不需要重启前端。
    /// </summary>

    /// <summary>
    /// 启动后把界面语言推给后端（v0.2.2 UI 英文扫描）。
    ///
    /// 为什么要单独一个方法：`StartAsync()` 有两条路径 —— 正常就绪，以及**发布构建不含模型**时的
    /// `need_download`（后者会抛异常）。两条路径下后端进程都是活的、都会按自己的 locale 回文案，
    /// 所以语言同步必须在**两条路径上都执行**；原先写在 try 里，`need_download` 时被跳过，
    /// 于是打包版出现"界面英文、后端中文"（Agent 工具说明 / 模型数据源 / 插件名）。
    ///
    /// 失败只记调试日志：语言同步不该挡住启动。
    /// </summary>
    private async Task SyncBackendLanguageOnStartup()
    {
        try
        {
            string langJson = await _backend.SendRequestAsync(
                new { action = "set_language", locale = Loc.Locale });
            using System.Text.Json.JsonDocument langDoc = System.Text.Json.JsonDocument.Parse(langJson);
            if (langDoc.RootElement.TryGetProperty("commands", out System.Text.Json.JsonElement langCommands))
                ApplyCommandsFromBackend(langCommands);
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine("启动时同步后端语言失败：" + ex.Message);
        }
    }

    private void Backend_ModelReadyChanged(bool ready)
    {
        try
        {
            Dispatcher.Invoke(() =>
            {
                if (ready)
                {
                    StatusText.Text = Loc.T("main.status.ready");
                    AddMessage("system", Loc.T("main.model.hotLoaded",
                        "✅ 模型已就绪，现在可以正常对话了。"));
                    Toast(Loc.T("main.model.hotLoaded", "✅ 模型已就绪，现在可以正常对话了。"));
                }
                else
                {
                    EnterNoModelMode();
                }
                _ = RefreshStatusBarAsync();
            });
        }
        catch (Exception ex)
        {
            App.Log("MainWindow.Backend_ModelReadyChanged", ex);
        }
    }

    /// <summary>
    /// 启动时检测外层目录下的启动快捷方式：不存在则询问是否创建；
    /// 已失效（移动文件夹/改名后目标丢失或工作目录不对）则提示重新创建；
    /// 用户拒绝后记录状态不再重复询问。
    /// </summary>
    private void CheckStartupShortcut()
    {
        try
        {
            if (ShortcutService.IsValid())
                return;

            if (ShortcutService.Exists())
            {
                var recreate = MessageBox.Show(this,
                    Loc.T("main.shortcut.broken",
                        "检测到启动快捷方式已失效：\n{0}\n\n" +
                        "（通常是因为程序文件夹被移动或改名）\n\n是否重新创建？",
                        ShortcutService.ShortcutPath),
                    Loc.T("main.shortcut.brokenTitle", "快捷方式已失效"),
                    MessageBoxButton.YesNo,
                    MessageBoxImage.Warning);
                if (recreate == MessageBoxResult.Yes)
                    ReportShortcutCreated();
                return;
            }

            if (_settings.ShortcutPromptDeclined)
                return;  // 用户之前已拒绝，不再询问

            var create = MessageBox.Show(this,
                Loc.T("main.shortcut.missing",
                    "未在程序文件夹内检测到启动快捷方式。\n\n" +
                    "是否创建「{0}」？\n" +
                    "创建后双击它即可启动 EchoDesk，无需分辨哪个可执行文件是入口。",
                    ShortcutService.ShortcutFileName),
                Loc.T("main.shortcut.missingTitle", "创建启动快捷方式"),
                MessageBoxButton.YesNo,
                MessageBoxImage.Question);
            if (create == MessageBoxResult.Yes)
            {
                ReportShortcutCreated();
            }
            else
            {
                _settings.ShortcutPromptDeclined = true;
                _settings.Save();
                AddMessage("system", Loc.T("main.shortcut.skipped",
                    "ℹ️ 已跳过创建启动快捷方式（后续不再询问，可手动创建）。"));
            }
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.shortcut.failed", "⚠️ 快捷方式处理失败: {0}", ex.Message));
        }
    }

    private void ReportShortcutCreated()
    {
        try
        {
            string path = ShortcutService.Create();
            AddMessage("system", Loc.T("main.shortcut.created", "✅ 已创建启动快捷方式：{0}", path));
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.shortcut.createFailed", "❌ 创建快捷方式失败: {0}", ex.Message));
        }
    }

    /// <summary>后端 stderr 进度（下载/预热等）：完整内容进对话框，状态栏只显示简化状态。</summary>
    private void Backend_StderrReceived(string line)
    {
        // 只展示下载/加载/预热相关进度，避免无关日志刷屏
        if (line.Contains("正在下载") || line.Contains("下载") || line.Contains("加载模型") || line.Contains("预热"))
        {
            Dispatcher.Invoke(() =>
            {
                UpdateLiveStatus(line.Trim());
                StatusText.Text = SimplifyStatus(line.Trim());
            });
        }
    }

    /// <summary>
    /// 宿主的结构化进度（v0.2.2 第 4 批）：后端在处理非流式请求期间主动发来，
    /// 已经是一条干净的进度文本（不需要像 stderr 那样先做关键字过滤）。
    /// 复用同一套 `UpdateLiveStatus` / `SimplifyStatus`，行为与 stderr 那条路一致。
    /// </summary>
    private void Backend_HostProgressReceived(string text)
    {
        if (string.IsNullOrWhiteSpace(text))
            return;
        Dispatcher.Invoke(() =>
        {
            UpdateLiveStatus(text.Trim());
            StatusText.Text = SimplifyStatus(text.Trim());
        });
    }

    /// <summary>把完整进度写进对话框：复用同一条"活"消息，避免每条百分比都新增一条。</summary>
    private void UpdateLiveStatus(string text)
    {
        if (_liveStatusContent is null)
        {
            var panel = NewMessagePanel("system", text, out FrameworkElement block);
            _liveStatusContent = (TextBox)block;
            MessagePanel.Children.Add(panel);
        }
        else
        {
            _liveStatusContent.Text = "ℹ️ " + text;
        }
        MessageScrollViewer.ScrollToBottom();
    }

    /// <summary>把详细进度行简化为状态栏短文本。</summary>
    private static string SimplifyStatus(string line)
    {
        if (line.Contains("预热")) return Loc.T("main.status.preheating", "正在预热模型……");
        if (line.Contains("下载")) return Loc.T("main.status.downloadingModel", "正在下载模型……");
        if (line.Contains("加载")) return Loc.T("main.status.loadingModel", "正在加载模型……");
        return line;
    }

    /// <summary>后端进程异常退出时，在对话框中显著提示（后台线程触发，需调度回 UI）。</summary>
    private void Backend_ProcessExited(int exitCode)
    {
        Dispatcher.Invoke(() =>
        {
            StatusText.Text = Loc.T("main.status.exited", "后端已退出");
            AddMessage("system", Loc.T("main.error.backendCrashed",
                "⚠️ 后端进程意外退出（退出码 {0}）。请检查程序所在目录的 backend_crash.log，或重启 EchoDesk。",
                exitCode));
        });
    }

    /// <summary>统一启用/禁用输入区域所有控件（连接后端期间禁用）。</summary>
    private void SetControlsEnabled(bool enabled)
    {
        SendButton.IsEnabled = enabled;
        UploadButton.IsEnabled = enabled;
        SnippetButton.IsEnabled = enabled;
        CommandButton.IsEnabled = enabled;
        ContextButton.IsEnabled = enabled;
        InputBox.IsEnabled = enabled;
        CommandInputBox.IsEnabled = enabled;
    }

    protected override void OnClosed(EventArgs e)
    {
        _exitTimer?.Stop();
        _toastTimer?.Stop();
        SaveWindowState();
        _backend?.Dispose();
        base.OnClosed(e);
    }

    // ---------- 快捷键（窗口级） ----------

    /// <summary>
    /// 快捷键：Ctrl+Enter 发送 · Esc 停止生成 · Ctrl+L 清空 · Ctrl+O 上传 ·
    /// Ctrl+Shift+C 复制全部 · Ctrl+加/减/0 调字号 · Ctrl+T 深浅色。
    /// （换行由输入框的 Enter / Shift+Enter 直接处理，无需拦截）
    /// </summary>
    protected override void OnPreviewKeyDown(KeyEventArgs e)
    {
        bool ctrl = Keyboard.Modifiers == ModifierKeys.Control;
        bool ctrlShift = Keyboard.Modifiers == (ModifierKeys.Control | ModifierKeys.Shift);

        if (ctrlShift && e.Key == Key.C)
        {
            e.Handled = true;
            CopyAllMessages();
            return;
        }
        if (ctrl)
        {
            switch (e.Key)
            {
                case Key.L:
                    e.Handled = true;
                    _ = RunLocalCommandAsync("/clear");
                    return;
                case Key.O:
                    e.Handled = true;
                    UploadButton_Click(this, new RoutedEventArgs());
                    return;
                case Key.T:
                    e.Handled = true;
                    HandleDarkMode("/darkmode");
                    return;
                case Key.OemComma:   // Ctrl+, 打开设置
                    e.Handled = true;
                    OpenSettings();
                    return;
                case Key.M:          // Ctrl+M 打开设置并切到技能市场
                    e.Handled = true;
                    OpenSettings(openMarket: true);
                    return;
                case Key.OemPlus:
                case Key.Add:
                    e.Handled = true;
                    AdjustFontSize(1);
                    return;
                case Key.OemMinus:
                case Key.Subtract:
                    e.Handled = true;
                    AdjustFontSize(-1);
                    return;
                case Key.D0:
                case Key.NumPad0:
                    e.Handled = true;
                    AdjustFontSize(14 - _fontSize); // 恢复默认字号
                    return;
            }
        }
        if (e.Key == Key.Escape)
        {
            if (CommandPalette.IsOpen || SnippetPalette.IsOpen)
            {
                CloseAllPalettes();
                e.Handled = true;
                return;
            }
            if (_stopMode)
            {
                e.Handled = true;
                _ = TriggerStopAsync();
            }
            return;
        }
        base.OnPreviewKeyDown(e);
    }

    // ---------- 设置窗口 ----------

    /// <summary>
    /// 打开设置窗口（单实例），主题与字号变化即时同步回主窗口。
    /// 构造与显示一律在主线程同步完成且不做任何后端等待，因此窗口「打开即见」；
    /// 万一构造异常（历史缺陷：FontSizeSlider 回调空引用），也只记录日志并提示，
    /// 不再让整个进程被终止。
    /// </summary>
    private void OpenSettings(bool openMarket = false, string? tab = null)
    {
        if (_backend is null)
            return;
        string target = tab ?? (openMarket ? "技能市场" : "");
        if (_settingsWindow is { IsLoaded: true })
        {
            _settingsWindow.Activate();
            if (target.Length > 0)
                _settingsWindow.ShowTab(target, refreshMarket: target == "技能市场");
            return;
        }
        try
        {
            var window = new SettingsWindow(
                _backend, _settings, _fontSize,
                onThemeChanged: theme => ApplyTheme(theme),
                onFontSizeChanged: size => AdjustFontSize(size - _fontSize));
            _settingsWindow = window;
            window.Closed += (_, _) =>
            {
                _settingsWindow = null;
                _ = RefreshStatusBarAsync();  // 设置可能改了模式/自动化状态
            };
            window.Owner = this;
            window.Show();
        }
        catch (Exception ex)
        {
            _settingsWindow = null;
            App.Log("MainWindow.OpenSettings", ex);
            AddMessage("system", Loc.T("main.error.settingsOpenFailed", "❌ 打开设置窗口失败：{0}", ex.Message));
            Toast(Loc.T("main.error.settingsOpenFailedToast", "打开设置窗口失败：{0}", ex.Message));
            return;
        }
        if (target.Length > 0)
            _settingsWindow.ShowTab(target, refreshMarket: target == "技能市场");
    }

    private void SettingsButton_Click(object sender, RoutedEventArgs e) => OpenSettings();

    // ---------- 危险指令确认（修复二） ----------

    /// <summary>
    /// 有副作用且不可逆的指令。执行前会二次确认（可在设置「常规」中关闭）。
    /// <c>/exit</c> 不在其中：它本身已有 10 秒可中断的倒计时与感谢提示，等价于内置确认。
    /// </summary>
    private static readonly HashSet<string> DestructiveCommands =
        new(StringComparer.OrdinalIgnoreCase) { "/clear", "/clearcache" };

    /// <summary>判断一串输入是否以危险指令开头。</summary>
    private static string? DestructiveCommandOf(string command)
    {
        string head = command.Trim();
        int space = head.IndexOf(' ');
        if (space > 0)
            head = head[..space];
        return DestructiveCommands.Contains(head) ? head : null;
    }

    /// <summary>危险指令执行前的确认对话框；返回 false 表示用户取消。</summary>
    private bool ConfirmDestructive(string command)
    {
        string? key = DestructiveCommandOf(command);
        if (key is null || !_settings.ConfirmDestructiveCommands)
            return true;
        string detail = key.ToLowerInvariant() switch
        {
            "/clearcache" => Loc.T("main.dialog.cacheText"),
            "/clear" => Loc.T("main.dialog.clearText"),
            _ => "",
        };
        MessageBoxResult result = MessageBox.Show(
            this,
            Loc.T("main.dialog.destructiveText",
                  "确认执行 {0} ？\n\n{1}\n\n（可在「设置 → 常规」中关闭该确认）", key, detail),
            Loc.T("main.dialog.destructiveTitle"), MessageBoxButton.YesNo,
            MessageBoxImage.Warning, MessageBoxResult.No);
        return result == MessageBoxResult.Yes;
    }

    /// <summary>执行指令输入框之外的本地指令（含前端自行处理的 /snippet、/darkmode）。</summary>
    private async Task RunLocalCommandAsync(string command)
    {
        if (_isProcessing || _backend is null || command.Length == 0)
            return;
        if (!ConfirmDestructive(command))
            return;
        SetProcessing(true);
        AddMessage("user", command);
        try
        {
            if (command.StartsWith("/snippet", StringComparison.OrdinalIgnoreCase))
                HandleSnippetCommand(command);
            else if (command.StartsWith("/darkmode", StringComparison.OrdinalIgnoreCase))
                HandleDarkMode(command);
            else
                HandleChatResponse(await _backend.SendRequestAsync(new { action = "chat", message = command }));
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message));
        }
        finally
        {
            SetProcessing(false);
        }
    }

    // ---------- 发送消息（对话输入栏） ----------

    private async void SendButton_Click(object sender, RoutedEventArgs e)
    {
        CloseAllPalettes();
        if (_stopMode)
        {
            await TriggerStopAsync();
            return;
        }
        await SendMessageAsync();
    }

    private async void InputBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (HandlePaletteKey(e))
        {
            e.Handled = true;
            return;
        }
        // Alt+↑（任务十二）：把上一条用户消息复制回输入框。放在 Ctrl+Enter 之前：
        // 两者的 Key 不同，但先判"带修饰键的方向键"能避免将来加别的方向键快捷键时互相偷键。
        if (e.Key == Key.Up && Keyboard.Modifiers == ModifierKeys.Alt)
        {
            e.Handled = true;
            RecallLastUserMessage();
            return;
        }
        // Ctrl+Enter 发送；其余（Enter / Shift+Enter）均插入换行
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.Control)
        {
            e.Handled = true;
            await SendMessageAsync();
        }
    }

    /// <summary>
    /// 任务十二：把上一条用户消息复制回输入框（Alt+↑）。
    ///
    /// 与 Avalonia 版同一语义（frontend-avalonia 的 RecallLastUserMessage）：
    /// 只找 role == "user"、覆盖输入框内容、用状态栏说明结果而不弹确认框。
    /// 记录 §8.4 要求实现前确认与既有快捷键不冲突：输入框上现有的绑定只有
    /// Ctrl+Enter（发送）与指令面板的 ↑/↓/Tab/Enter/Esc（面板打开时优先吃掉），
    /// Alt+↑ 在两者之外。
    /// </summary>
    private void RecallLastUserMessage()
    {
        // 用**显式记录的字段**而不是从可视树回读：消息面板里没有角色标记
        // （NewMessagePanel 只放时间与内容块），从可视树反推角色既脆又容易在
        // 主题/语言重渲染后失效。这里只在 AddMessage 时记一行文本，零额外状态机。
        string text = _lastUserMessage;
        if (text.Length == 0)
        {
            StatusText.Text = Loc.T("main.recall.none", "还没有可以复制的提问");
            return;
        }
        InputBox.Text = text;
        InputBox.CaretIndex = text.Length;
        InputBox.Focus();
        StatusText.Text = Loc.T("main.recall.lastUser", "↩️ 已复制上一条提问（Alt+↑）");
    }

    private void InputBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        CloseCommandPalette(); // 指令选择栏仅由指令输入框触发
        UpdateCommandUi(InputBox.Text);
    }

    private void InputBox_GotKeyboardFocus(object sender, KeyboardFocusChangedEventArgs e) => CloseAllPalettes();

    private async Task SendMessageAsync()
    {
        if (_isProcessing || _backend is null)
            return;
        string message = InputBox.Text.Trim();
        if (message.Length == 0)
            return;
        if (!ConfirmDestructive(message))
            return;   // 用户取消：保留输入框内容不动

        InputBox.Text = "";
        SetProcessing(true);
        AddMessage("user", message);

        try
        {
            if (message.Equals("/help", StringComparison.OrdinalIgnoreCase))
                ShowHelpLocally();
            else if (message.Equals("/stop", StringComparison.OrdinalIgnoreCase))
                await TriggerStopAsync();
            else if (message.StartsWith("/snippet", StringComparison.OrdinalIgnoreCase))
                HandleSnippetCommand(message);
            else if (message.StartsWith("/darkmode", StringComparison.OrdinalIgnoreCase))
                HandleDarkMode(message); // 主题为前端职责，本地处理
            else if (message.StartsWith("/"))
                HandleChatResponse(await _backend.SendRequestAsync(new { action = "chat", message }));
            else
                await SendStreamAsync(message); // 普通对话走流式输出
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message));
        }
        finally
        {
            SetProcessing(false);
        }
    }

    // ---------- 流式对话输出 ----------

    private async Task SendStreamAsync(string message)
    {
        // 从一开始就使用 RichTextBox，流式过程中即按 Markdown 实时渲染
        var panel = NewMessagePanel("assistant", "", out FrameworkElement contentBlock);
        panel.Children.Remove(contentBlock);
        var rich = CreateRichContent();
        panel.Children.Insert(1, rich);
        MessagePanel.Children.Add(panel);

        var sb = new StringBuilder("🤖 ");
        // 思考内容单独累积（v0.2.1 模型思考内容分离 任务四）：回答区只放最终答案
        var thinking = new StringBuilder();
        Expander? thinkRegion = null;
        TextBlock? thinkCount = null;
        RichTextBox? thinkBody = null;
        bool thinkAutoExpanded = false;
        bool answerStarted = false;
        _mdTarget = rich;
        // 自动化标识：界面探针（tools\ui_probe.ps1）靠它找到"本次回答正文"与思考区，
        // 断言回答区里没有思考标签、折叠区可展开可收起。手工创建的元素不会自动获得
        // x:Name 那样的 AutomationId，必须显式设置。
        AutomationProperties.SetAutomationId(rich, "AnswerBody");
        RenderInto(rich, sb.ToString()); // 初始只显示图标
        _stopRequested = false;
        _stopMode = true;
        SendButton.Content = "⏹";
        SendButton.ToolTip = Loc.T("main.button.stopTip", "停止生成 (Esc)");
        SendButton.IsEnabled = true; // 流式期间按钮变为"停止"，必须可点击

        // 思考区按需创建：模型没有思考内容时界面上不会出现一个空折叠区（任务四要求）
        void EnsureThinkRegion()
        {
            if (thinkRegion is not null)
                return;
            (thinkRegion, thinkCount, thinkBody) = CreateReasoningRegion();
            thinkRegion.IsExpanded = true;
            panel.Children.Insert(1, thinkRegion);
            answerStarted = false;
            _thinkBackend = thinkBody;   // 供节流器统一重渲染（任务五）
        }

        void RefreshThinkRegion()
        {
            if (thinkRegion is null)
                return;
            if (thinkCount is not null)
                thinkCount.Text = Loc.T("main.reasoning.count", "· {0} 字", thinking.Length);
            if (thinkBody is not null)
                thinkBody.Document = MarkdownRenderer.Build(Emoji.Prefer(thinking.ToString()));
        }

        // Agent 步骤区（能力扩展-1 任务二）：agent_step / tool_call / tool_result 三类事件
        // 各渲染成**独立的行**，而不是把"调用了什么"拼进回答正文 ——
        // 拼进正文会被复制、会与答案混在一起，用户也就没法一眼看清模型到底做了什么。
        // 区块按需创建：普通对话（没有这三类事件）不会多出一块空面板。
        Border? agentRegion = null;
        StackPanel? agentRows = null;

        void EnsureAgentRegion()
        {
            if (agentRows is not null)
                return;
            (agentRegion, agentRows) = CreateAgentTraceRegion();
            // 插在回答面板**之前**：事件先发生、答案后产出，阅读顺序才是自然的
            int at = MessagePanel.Children.IndexOf(panel);
            MessagePanel.Children.Insert(at < 0 ? MessagePanel.Children.Count : at, agentRegion);
        }

        void AddAgentRow(string text, string brushKey, bool strong = false)
        {
            if (string.IsNullOrEmpty(text))
                return;
            EnsureAgentRegion();
            if (agentRows is null)
                return;
            AppendAgentTraceRow(agentRows, text, brushKey, strong);
            ScheduleScrollToBottom();
        }

        // 运行指标（任务七）：请求开始时重置，此后由心跳与 done 驱动
        ResetStreamStats();
        _metricsStartUtc = DateTime.UtcNow;
        _metricsState = "waiting";
        StartMetricsTimer();

        try
        {
            string full = await _backend!.SendStreamRequestAsync(
                new { action = "chat_stream", message },
                delta =>
                {
                    sb.Append(delta);
                    ScheduleMarkdownRender(sb.ToString()); // 节流实时渲染
                    ScheduleScrollToBottom();              // 滚动同样节流（任务五）
                },
                onContext: (_, _) => { },   // 上下文显示改由 UpdateContextFromResponse(root) 统一处理，见下方 onEvent
                onEvent: (type, root) =>
                {
                    // 心跳（任务五）：模型还在算但暂时没有新 token。
                    // 刷新指标并让状态文案说明"在处理输入"，避免界面看起来像卡死。
                    if (type == "progress")
                    {
                        if (root.TryGetProperty("stats", out JsonElement beat))
                        {
                            bool waiting = root.TryGetProperty("waiting_first_token", out JsonElement w)
                                           && w.ValueKind == JsonValueKind.True;
                            ApplyStreamStats(beat, waiting ? "waiting" : "running");
                        }
                        StatusText.Text = _metricsState == "waiting"
                            ? Loc.T("main.status.prefill", "正在处理输入…")
                            : Loc.T("main.status.generating", "正在生成…");
                        return;
                    }
                    if (type == "error")
                    {
                        ApplyStreamStats(default, "failed");
                        return;
                    }
                    // ---- Agent 步骤行（能力扩展-1 任务二）----
                    // 三类事件 = 独立的三行；下面每条分支只负责"把这一行写清楚"，
                    // 不做任何状态机 —— 后端的 AgentLoop 已经是权威的步骤来源。
                    if (type == "agent_step")
                    {
                        string phase = GetStringField(root, "phase");
                        int stepIndex = GetIntField(root, "index");
                        int maxSteps = GetIntField(root, "max_steps");
                        if (phase == "finalize")
                        {
                            // 步数用完后的"收敛"补一轮：用户要知道为什么突然出结论了
                            AddAgentRow(Loc.T("main.agent.finalize", "🧩 正在汇总结论"), "SubTextBrush");
                        }
                        else if (phase == "thinking")
                        {
                            AddAgentRow(maxSteps > 0
                                    ? Loc.T("main.agent.step", "📍 第 {0}/{1} 步", stepIndex, maxSteps)
                                    : Loc.T("main.agent.stepNoMax", "📍 第 {0} 步", stepIndex),
                                "SubTextBrush");
                        }
                        else if (phase == "thought")
                        {
                            // 任务十三（自测问题修复-1）后端把每一步的思考内容分离出来后，
                            // 随 phase="thought" 事件发过来 —— 但这里此前**没有渲染它**：
                            // 于是"Agent 的思考过程"只在最终那条 done.reasoning 的折叠区里看得到，
                            // 逐步的思考是**发了却没人画**（本轮逐项比对两个前端时发现的）。
                            // 有思考才画一行，没有就不画，免得每步多一行空的。
                            // 截断到 200 字符，与 tool_result 的观察结果同一口径。
                            string thought = GetStringField(root, "reasoning");
                            if (thought.Trim().Length > 0)
                                AddAgentRow(Loc.T("main.agent.thought", "💭 {0}", OneLineText(thought, 200)),
                                    "SubTextBrush");
                        }
                        // phase == "answer"：模型已决定直接作答，答案马上就出现在回答区，
                        // 这里不再多打一行（步骤区只讲"用了什么工具"）
                        return;
                    }
                    if (type == "tool_call")
                    {
                        string tool = GetStringField(root, "tool");
                        string why = GetStringField(root, "why");
                        string args = root.TryGetProperty("args", out JsonElement argsEl)
                            ? OneLineText(argsEl.GetRawText(), 120) : "";
                        var line = new StringBuilder(Loc.T("main.agent.call", "🔧 调用 {0}", tool));
                        if (args.Length > 0)
                            line.Append(' ').Append(Loc.T("main.agent.args", "参数 {0}", args));
                        if (why.Length > 0)
                            line.Append(' ').Append(Loc.T("main.agent.why", "· 理由：{0}", why));
                        AddAgentRow(line.ToString(), "InfoBrush");
                        return;
                    }
                    if (type == "tool_result")
                    {
                        string tool = GetStringField(root, "tool");
                        string observation = GetStringField(root, "observation");
                        int elapsedMs = GetIntField(root, "elapsed_ms");
                        bool toolOk = root.TryGetProperty("ok", out JsonElement okEl)
                                      && okEl.ValueKind == JsonValueKind.True;
                        if (!toolOk && IsAgentDeniedObservation(observation))
                        {
                            // 权限不足单独一档、且加重字重：这不是"工具坏了"，而是
                            // "用户没授权" —— 他要做的就是 /agent allow <工具>
                            AddAgentRow(Loc.T("main.agent.denied",
                                "🔒 {0} 未启用（用 /agent allow {1} 授权后可用）", tool, tool),
                                "WarningBrush", strong: true);
                        }
                        else if (!toolOk)
                        {
                            AddAgentRow(Loc.T("main.agent.failed", "❌ {0} 失败：{1}",
                                tool, OneLineText(observation, 200)), "ErrorBrush");
                        }
                        else
                        {
                            AddAgentRow(Loc.T("main.agent.result", "↩ {0} 返回：{1}",
                                    tool, OneLineText(observation, 200))
                                + " " + Loc.T("main.agent.elapsed", "（{0} ms）", elapsedMs),
                                "SuccessBrush");
                        }
                        return;
                    }
                    if (type != "done")
                        return;
                    // done 行带权威的运行指标（任务七）：以它为准（心跳期间的值是中间态）
                    if (root.TryGetProperty("stats", out JsonElement finalStats))
                    {
                        bool stopped = root.TryGetProperty("stopped", out JsonElement st)
                                       && st.ValueKind == JsonValueKind.True;
                        ApplyStreamStats(finalStats, stopped ? "stopped" : "done");
                    }
                    // done 行带权威的思考全文：以它为准重建思考区（流式期间可能只拿到一段）
                    string finalThinking = root.TryGetProperty("reasoning", out JsonElement rs)
                        ? rs.GetString() ?? "" : "";
                    if (finalThinking.Length > 0)
                    {
                        EnsureThinkRegion();
                        thinking.Clear();
                        thinking.Append(finalThinking);
                        RefreshThinkRegion();
                    }
                    // 思考把长度上限吃光、答案还没开始：展开思考区并说明，别留一个空回答区
                    if (root.TryGetProperty("reasoning_unclosed", out JsonElement unclosed)
                        && unclosed.ValueKind == JsonValueKind.True)
                    {
                        EnsureThinkRegion();
                        if (thinkRegion is not null)
                            thinkRegion.IsExpanded = true;
                        if (StripAssistantIcon(sb.ToString()).Trim().Length == 0)
                            sb.Append(Loc.T("main.reasoning.noAnswer",
                                "\n\n⚠️ 模型只输出了思考内容，没有给出最终答案（通常是长度上限用完了："
                                + "用 /preset quality 切到高质量档、或调大 config.json 的 max_tokens，"
                                + "也可以把问题问得更具体）"));
                    }
                    // Agent 轮的汇总（能力扩展-1 任务二）：步数 / 工具 / 总耗时，以及"步数用完"。
                    // 判据是"真的跑过 Agent"（steps>0 或步骤区已存在）——
                    // 否则普通对话也会被塞进一行"共 0 步"，那是噪声。
                    if (root.TryGetProperty("agent", out JsonElement agentEl))
                    {
                        int runSteps = GetIntField(agentEl, "steps");
                        if (runSteps > 0 || agentRegion is not null)
                        {
                            if (agentEl.TryGetProperty("hit_max_steps", out JsonElement hitMaxEl)
                                && hitMaxEl.ValueKind == JsonValueKind.True)
                            {
                                AddAgentRow(Loc.T("main.agent.hitMax",
                                    "⚠️ 已达步数上限，改为直接给结论"), "WarningBrush");
                            }
                            string summary = root.TryGetProperty("stopped", out JsonElement stoppedEl)
                                             && stoppedEl.ValueKind == JsonValueKind.True
                                ? Loc.T("main.agent.summaryStopped", "⏹ 已中止：{0} 步 · {1} ms",
                                    runSteps, GetIntField(agentEl, "elapsed_ms"))
                                : Loc.T("main.agent.summary",
                                    "✅ 完成：{0} 步 · {1} ms", runSteps, GetIntField(agentEl, "elapsed_ms"));
                            List<string> usedTools = GetStringListField(agentEl, "tools");
                            if (usedTools.Count > 0)
                                summary += " · " + Loc.T("main.agent.toolsUsed",
                                    "工具：{0}", string.Join(", ", usedTools));
                            AddAgentRow(summary, "SubTextBrush");
                        }
                    }
                },
                onDeltaChannel: (channel, text, reset) =>
                {
                    if (channel == "reasoning")
                    {
                        EnsureThinkRegion();
                        if (!thinkAutoExpanded && thinkRegion is not null)
                        {
                            // 正在思考：先展开，让用户看得见模型的推理在实时推进
                            thinkRegion.IsExpanded = true;
                            thinkAutoExpanded = true;
                        }
                        thinking.Append(text);
                        // 思考增量按节流重渲染（任务五）：思考往往比答案还长，
                        // 每个增量都重建一次 FlowDocument 是纯 CPU 下最明显的卡顿源。
                        ScheduleThinkRender(thinking.ToString(), thinkCount);
                        ScheduleScrollToBottom();
                        return;
                    }
                    if (reset)
                    {
                        // 作废回答区：此前发出去的"回答"其实都是思考（模型的起始标签
                        // 写在提示词里，只有结束标签可见，如 DeepSeek-R1）。
                        // 把它搬进思考区，回答区从头开始。
                        EnsureThinkRegion();
                        string moved = sb.ToString();
                        thinking.Append(StripAssistantIcon(moved));
                        RefreshThinkRegion();
                        sb.Clear();
                        sb.Append("🤖 ");
                        answerStarted = false;
                        return;
                    }
                    if (!answerStarted)
                    {
                        answerStarted = true;
                        // 答案开始：思考区自动收起（任务四"回答完成后自动收起"，比"完成后"
                        // 更早一步；用户手动展开过就不再打扰 —— 这里只在切换那一次收）
                        if (thinkRegion is not null)
                            thinkRegion.IsExpanded = false;
                    }
                    sb.Append(text);
                    ScheduleMarkdownRender(sb.ToString());
                    ScheduleScrollToBottom();
                });
            // 以 done 行的权威回答为准重建一次（思考区的孤立标签改判会改变回答区内容）
            if (full.Length > 0)
            {
                sb.Clear();
                sb.Append("🤖 ").Append(full);
            }
            if (_stopRequested && full.Length == 0)
                sb.Append(Loc.T("main.message.stopped", "\n⏹（已停止生成）"));
            RefreshThinkRegion();
            FinalizeMarkdownRender(sb.ToString()); // 结束：完整渲染一次
            AddMessageActions(panel, sb.ToString(), sb.ToString()); // 生成结束后提供复制操作条
        }
        finally
        {
            StopMarkdownTimer();
            StopStreamTickTimer();
            StopMetricsTimer();
            // 结束态保留最终数值（任务七：生成完成后保留、下次请求开始才重置）；
            // 中止的轮次标注"已中止"，不清零已产生的指标。
            if (_metricsState is "running" or "waiting")
                _metricsState = _stopRequested ? "stopped" : "done";
            RefreshMetricsDisplay();
            _stopMode = false;
            SendButton.Content = "➤";
            SendButton.ToolTip = Loc.T("main.button.sendTip", "发送 (Ctrl+Enter)");
            SendButton.IsEnabled = true;
        }
    }

    /// <summary>
    /// 创建"思考过程"折叠区（v0.2.1 模型思考内容分离 任务四）。
    ///
    /// 为什么用 WPF 原生 <see cref="Expander"/> 而不是自绘折叠按钮：标题栏自带
    /// 展开/收起按钮、键盘操作、读屏语义与 Toggle 自动化接口（UI 探针能直接驱动），
    /// 手写一套等价物只有坏处。视觉区分靠三件事：标题是副色小字、正文整块放在
    /// 浅底边框里、正文用斜体副色 —— 和回答区（主题正文色、正常字号、无边框）
    /// 一眼就能分开。
    ///
    /// 返回 (折叠区, 字数 TextBlock, 正文 RichTextBox)：流式期间需要就地更新后两者。
    /// </summary>
    private (Expander Root, TextBlock Count, RichTextBox Body) CreateReasoningRegion()
    {
        var title = new TextBlock
        {
            Text = Loc.T("main.reasoning.title", "🧠 思考过程"),
            FontSize = Math.Max(9, _fontSize - 2),
        };
        ThemeService.Bind(title, TextBlock.ForegroundProperty, "SubTextBrush");
        // 挂语言键：切换语言时 Loc.Apply 会把它重新翻译（动态创建的元素同样在遍历范围内）
        Loc.SetKey(title, "main.reasoning.title");

        var count = new TextBlock
        {
            Margin = new Thickness(6, 0, 0, 0),
            FontSize = Math.Max(9, _fontSize - 3),
            VerticalAlignment = VerticalAlignment.Center,
        };
        ThemeService.Bind(count, TextBlock.ForegroundProperty, "SubTextBrush");

        var header = new StackPanel { Orientation = Orientation.Horizontal };
        header.Children.Add(title);
        header.Children.Add(count);

        var body = CreateRichContent();
        // Tag 刻意不是 "content"：复制操作条与"复制全部对话"靠 Tag=="content" 找正文，
        // 让它们继续只拿答案；思考内容仍可用鼠标拖选复制（RichTextBox 可选）。
        body.Tag = "reasoning";
        body.FontStyle = FontStyles.Italic;
        ThemeService.Bind(body, TextBox.ForegroundProperty, "SubTextBrush");

        var border = new Border
        {
            Margin = new Thickness(0, 2, 0, 6),
            Padding = new Thickness(8, 6, 8, 6),
            CornerRadius = new CornerRadius(4),
            BorderThickness = new Thickness(1),
        };
        ThemeService.Bind(border, Border.BackgroundProperty, "CodeBackground");
        ThemeService.Bind(border, Border.BorderBrushProperty, "MessageBorderBrush");
        border.Child = body;

        var region = new Expander
        {
            Header = header,
            Content = border,
            Margin = new Thickness(0),
            Tag = "reasoning",
            IsExpanded = false,   // 默认收起（任务四）：历史回放与生成结束都保持收起
        };
        Loc.SetTipKey(region, "main.reasoning.tip");
        // 界面探针用：按这个 AutomationId 找折叠区，再用 TogglePattern 验展开/收起
        AutomationProperties.SetAutomationId(region, "ReasoningRegion");
        return (region, count, body);
    }

    /// <summary>思考区的字号跟随全局字号（比正文略小：它是辅助信息，不该抢答案的视线）。</summary>
    private void ScaleReasoningRegion(Expander region)
    {
        if (region.Header is StackPanel head)
        {
            foreach (TextBlock label in head.Children.OfType<TextBlock>())
                label.FontSize = Math.Max(9, _fontSize - 2);
        }
        if (region.Content is Border border && border.Child is RichTextBox body)
            body.FontSize = Math.Max(9, _fontSize - 1);
    }

    /// <summary>去掉回答累积串开头的助手图标（"🤖 "）：判断"有没有答案"与搬运文本时都要用它。</summary>
    private static string StripAssistantIcon(string text)
        => text.StartsWith("🤖 ", StringComparison.Ordinal) ? text[3..] : text;

    // ---------- Agent 步骤区（能力扩展-1 任务二） ----------

    /// <summary>
    /// 创建 Agent 步骤区：标题 + 逐行状态（调用了什么工具、参数、返回、耗时）。
    ///
    /// 为什么是**逐行 TextBlock**、而不是像多文件上传那样把若干行拼进一条 TextBox：
    /// 这里必须逐行着色 —— 「🔒 未启用」要和正常调用一眼分开，那正是用户该去
    /// <c>/agent allow</c> 的信号；拼成一段文本就只能整块一个颜色，信号被稀释掉。
    /// 视觉与思考区（<see cref="CreateReasoningRegion"/>）一致：浅底 + 边框 + 副色小字，
    /// 与回答正文一眼可分。
    ///
    /// 返回 (根元素, 步骤行容器)：流式期间往后者追加行。
    /// </summary>
    private (Border Root, StackPanel Rows) CreateAgentTraceRegion()
    {
        var title = new TextBlock
        {
            Text = Loc.T("main.agent.title", "🛠️ Agent 步骤"),
            FontSize = Math.Max(9, _fontSize - 2),
        };
        ThemeService.Bind(title, TextBlock.ForegroundProperty, "SubTextBrush");
        // 挂语言键：切语言时 Loc.Apply 会把它重新翻译（动态创建的元素同样在遍历范围内）
        Loc.SetKey(title, "main.agent.title");

        var rows = new StackPanel();
        var body = new StackPanel();
        body.Children.Add(title);
        body.Children.Add(rows);

        var border = new Border
        {
            Margin = new Thickness(0, 2, 0, 4),
            Padding = new Thickness(8, 6, 8, 6),
            CornerRadius = new CornerRadius(4),
            BorderThickness = new Thickness(1),
            // Tag 刻意不是 "content"：复制操作条与"复制全部对话"靠 Tag=="content" 找正文，
            // 步骤行属于**过程信息**，不该混进复制出来的回答里（与思考区同样的取舍）。
            Tag = "agent-trace",
            Child = body,
        };
        ThemeService.Bind(border, Border.BackgroundProperty, "CodeBackground");
        ThemeService.Bind(border, Border.BorderBrushProperty, "MessageBorderBrush");
        Loc.SetTipKey(border, "main.agent.tip");
        // 动态创建的元素要等下一次 Loc.Apply 才会被回填，所以首屏自己先写一次提示
        border.ToolTip = Loc.T("main.agent.tip",
            "模型自己决定的每一步：调用哪个工具、参数是什么、返回了什么、耗时多久。按 Esc 可随时停止。");
        // 界面探针用：按这个 AutomationId 找 Agent 步骤区
        AutomationProperties.SetAutomationId(border, "AgentTrace");
        return (border, rows);
    }

    /// <summary>往 Agent 步骤区追加一行（放入容器并返回，便于调用方后续微调）。</summary>
    private TextBlock AppendAgentTraceRow(StackPanel rows, string text, string brushKey, bool strong)
    {
        var row = new TextBlock
        {
            // 与消息文本一致地做 emoji 降级（本机没有 emoji 字体时换成纯文本标记）
            Text = Emoji.Prefer(text),
            FontSize = Math.Max(9, _fontSize - 2),
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 1, 0, 1),
            Tag = "agent-trace",
            FontWeight = strong ? FontWeights.SemiBold : FontWeights.Normal,
        };
        ThemeService.Bind(row, TextBlock.ForegroundProperty, brushKey);
        rows.Children.Add(row);
        return row;
    }

    /// <summary>Agent 步骤区的字号跟随全局字号（与思考区同样处理，Ctrl+滚轮调字号时才不会落下它）。</summary>
    private void ScaleAgentTrace(Border region)
    {
        if (region.Child is not StackPanel body)
            return;
        double size = Math.Max(9, _fontSize - 2);
        foreach (object child in body.Children)
        {
            if (child is TextBlock label)                 // 标题
                label.FontSize = size;
            else if (child is StackPanel rows)            // 步骤行
                foreach (TextBlock row in rows.Children.OfType<TextBlock>())
                    row.FontSize = size;
        }
    }

    /// <summary>
    /// 工具返回「没权限」而不是「执行失败」的判据。
    ///
    /// 后端的措辞是固定的两句中文（<c>agent_tools.ToolRegistry.run</c>：敏感工具未授权时
    /// 「…当前未启用…」，本版不提供的保护类工具「…当前不可用」），而它就是模型看到的
    /// 原始观察结果、不随界面语言变 —— 所以这里按原文匹配是稳的，
    /// 反而不能按"界面语言下的失败文案"去匹配。
    /// </summary>
    private static bool IsAgentDeniedObservation(string observation)
        => observation.Contains(AgentDeniedMark, StringComparison.Ordinal)
           // 兼容旧串：v0.2.2 之前后端那两句是写死中文的（本地化后仍可能来自旧版后端）
           || observation.Contains("未启用", StringComparison.Ordinal)
           || observation.Contains("当前不可用", StringComparison.Ordinal);

    /// <summary>
    /// 后端"被拒绝"观察串的**语言无关标记**（v0.2.2 硬编码清查第 6 批）。
    ///
    /// 原先只能按中文原文匹配（见上面注释），但后端那两句在本地化后**不再固定是中文**，
    /// 按原文匹配在英文界面下必然失效。所以改成：后端在观察串前面加这个 ASCII 标记，
    /// 前端据此判定，显示前把它剥掉（见 <see cref="OneLineText"/>）。
    /// </summary>
    private const string AgentDeniedMark = "[!DENIED] ";

    /// <summary>把多行文本压成一行并截断：步骤行的参数与工具返回常是多行/很长的 JSON。</summary>
    private static string OneLineText(string text, int max)
    {
        if (string.IsNullOrEmpty(text))
            return "";
        string flat = text.Replace('\r', ' ').Replace('\n', ' ').Trim();
        // 剥掉"被拒绝"标记：它只给判定用，不该显示给用户
        if (flat.StartsWith(AgentDeniedMark, StringComparison.Ordinal))
            flat = flat[AgentDeniedMark.Length..].Trim();
        while (flat.Contains("  ", StringComparison.Ordinal))
            flat = flat.Replace("  ", " ", StringComparison.Ordinal);
        return flat.Length <= max ? flat : flat[..max] + "…";
    }

    // ---------- 流式 Markdown 实时渲染（节流） ----------

    private void ScheduleMarkdownRender(string text)
    {
        _mdPending = text;
        if (_mdTimer is { IsEnabled: true })
            return; // 已排定，下一次触发读取最新累积文本
        _mdTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(60) };
        _mdTimer.Tick += (_, _) =>
        {
            _mdTimer!.Stop();
            RenderInto(_mdTarget, _mdPending);
        };
        _mdTimer.Start();
    }

    private void FinalizeMarkdownRender(string text)
    {
        StopMarkdownTimer();
        RenderInto(_mdTarget, text);
    }

    private void StopMarkdownTimer()
    {
        if (_mdTimer != null)
        {
            _mdTimer.Stop();
            _mdTimer = null;
        }
    }

    // ---------- 流式合并更新（任务五）与运行指标（任务七） ----------

    /// <summary>请求把消息区滚到底；同一个节流周期内多次请求只执行一次。</summary>
    private void ScheduleScrollToBottom()
    {
        _scrollPending = true;
        EnsureStreamTickTimer();
    }

    /// <summary>请求稍后重渲染思考区（流式期间思考增量很密，逐条重渲染会卡）。</summary>
    private void ScheduleThinkRender(string text, TextBlock? countLabel)
    {
        _thinkPending = text;
        _thinkPendingCount = countLabel;
        EnsureStreamTickTimer();
    }

    /// <summary>
    /// 50ms 一次的合并更新：把"滚动到底"与"思考区重渲染"从"每 delta 一次"
    /// 降到"每 50ms 一次"。区间取值理由见 <c>stream_throttle_ms</c> 注释：
    /// 60 帧下 50ms 约等于每 3 帧一次，人眼已看不出延迟，而布局次数少一个数量级。
    /// </summary>
    private void EnsureStreamTickTimer()
    {
        if (_streamTickTimer is { IsEnabled: true })
            return;
        int interval = _settings.StreamThrottleMs > 0 ? _settings.StreamThrottleMs : 50;
        _streamTickTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(interval) };
        _streamTickTimer.Tick += (_, _) => FlushStreamTick();
        _streamTickTimer.Start();
    }

    private void FlushStreamTick()
    {
        if (_thinkPending != null)
        {
            string text = _thinkPending;
            _thinkPending = null;
            if (_thinkBackend != null)
                _thinkBackend.Document = MarkdownRenderer.Build(Emoji.Prefer(text));
            if (_thinkPendingCount != null)
                _thinkPendingCount.Text = Loc.T("main.reasoning.count", "· {0} 字", text.Length);
            _thinkPendingCount = null;
        }
        if (_scrollPending)
        {
            _scrollPending = false;
            MessageScrollViewer.ScrollToBottom();
        }
        if (!_scrollPending && _thinkPending == null && _streamTickTimer != null)
        {
            _streamTickTimer.Stop();
            _streamTickTimer = null;
        }
    }

    private void StopStreamTickTimer()
    {
        if (_streamTickTimer != null)
        {
            _streamTickTimer.Stop();
            _streamTickTimer = null;
        }
        _scrollPending = false;
        _thinkPending = null;
        _thinkPendingCount = null;
    }

    // ---------- 运行指标（任务七） ----------

    /// <summary>
    /// 记录后端上报的运行指标并刷新状态区。
    ///
    /// 指标**全部来自后端实测**（<c>done.stats</c> / 心跳 <c>progress.stats</c>），
    /// 前端只做格式化，不做估算 —— 任务书明确要求"不能仅靠前端估算"。
    /// 无模型时（后端报 model_ready=false）整个指标块隐藏，不显示 0 或占位符。
    /// </summary>
    private void ApplyStreamStats(JsonElement stats, string state)
    {
        foreach (string key in new[]
                 {
                     "first_token_ms", "elapsed_ms", "output_chars", "tokens",
                     "tokens_estimated", "tps", "prompt_chars", "prefill_done",
                 })
        {
            if (stats.TryGetProperty(key, out JsonElement value))
                _lastStats[key] = value.ValueKind switch
                {
                    JsonValueKind.True => "true",
                    JsonValueKind.False => "false",
                    JsonValueKind.Number => value.ToString(),
                    _ => value.GetString() ?? "",
                };
        }
        _metricsState = state;
        RefreshMetricsDisplay();
    }

    /// <summary>下一次请求开始时重置指标（任务七："下次请求开始时重置"）。</summary>
    private void ResetStreamStats()
    {
        _lastStats.Clear();
        // 无模型时不显示任何指标（任务七："无模型时隐藏无意义的值，不显示 0 或占位符"）
        _metricsState = _backend is null || !_backend.ModelReady ? "hidden" : "idle";
        RefreshMetricsDisplay();
    }

    private static double StatNumber(IReadOnlyDictionary<string, string> stats, string key)
    {
        return stats.TryGetValue(key, out string? raw)
            && double.TryParse(raw, System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out double value)
            ? value
            : -1;
    }

    /// <summary>把指标渲染到状态区；无意义的项直接不显示（不写 0、不写占位符）。</summary>
    private void RefreshMetricsDisplay()
    {
        if (MetricsText == null)
            return;
        // 无模型：整块隐藏（后端会报 model_ready=false，此处由调用方清空）
        if (_metricsState == "hidden" || _lastStats.Count == 0)
        {
            MetricsText.Visibility = Visibility.Collapsed;
            return;
        }
        var parts = new List<string>();
        // 模型名取自后端上报（BackendService.LoadedModel，随 model_state/model_ready 更新）
        string model = _backend?.LoadedModel ?? "";
        if (model.Length > 0)
            parts.Add($"🧩 {model}");

        if (_metricsState == "waiting")
        {
            // 首 token 到达前：显示"等待首 token"而不是 0.0 秒（任务书明确要求）
            parts.Add(Loc.T("main.metrics.waitingFirstToken", "⏳ 等待首 token"));
            double waited = StatNumber(_lastStats, "elapsed_ms");
            if (waited >= 0)
                parts.Add(Loc.T("main.metrics.elapsed", "⏱️ {0}", FormatSeconds(waited / 1000.0)));
        }
        else
        {
            double ttft = StatNumber(_lastStats, "first_token_ms");
            if (ttft >= 0)
                parts.Add(Loc.T("main.metrics.ttft", "⚡ 首 token {0}", FormatSeconds(ttft / 1000.0)));
            double tps = StatNumber(_lastStats, "tps");
            if (tps > 0)
                parts.Add(Loc.T("main.metrics.tps", "🚀 {0:0.#} token/s", tps));
            double tokens = StatNumber(_lastStats, "tokens");
            if (tokens > 0)
            {
                bool estimated = _lastStats.TryGetValue("tokens_estimated", out string? est)
                                 && est == "true";
                // 估算值必须标注（任务书要求），用 ≈ 与提示文本两种方式同时表达
                string label = estimated
                    ? Loc.T("main.metrics.outputTokensEstimated", "≈{0} tokens", (int)tokens)
                    : Loc.T("main.metrics.outputTokens", "{0} tokens", (int)tokens);
                parts.Add("📤 " + label);
            }
            else if (_metricsState == "running")
            {
                double chars = StatNumber(_lastStats, "output_chars");
                if (chars > 0)
                    parts.Add(Loc.T("main.metrics.chars", "📤 {0} 字", (int)chars));
            }
            double prompt = StatNumber(_lastStats, "prompt_chars");
            if (prompt > 0 && _metricsState != "done")
                parts.Add(Loc.T("main.metrics.promptChars", "📥 {0} 字", (int)prompt));
            double elapsed = StatNumber(_lastStats, "elapsed_ms");
            if (elapsed >= 0)
                parts.Add(Loc.T("main.metrics.elapsed", "⏱️ {0}", FormatSeconds(elapsed / 1000.0)));
            if (_metricsState == "stopped")
                parts.Add(Loc.T("main.metrics.stopped", "⏹ 已中止"));
            else if (_metricsState == "failed")
                parts.Add(Loc.T("main.metrics.failed", "⚠️ 失败"));
        }
        MetricsText.Text = string.Join("  ·  ", parts);
        MetricsText.ToolTip = Loc.T("main.metrics.tip",
            "本次生成的运行指标（由后端实测上报）：首 token 延迟、输出速度、token 数、总耗时。"
            + "带 ≈ 的数字为估算值。");
        MetricsText.Visibility = parts.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    private static string FormatSeconds(double seconds)
        => seconds >= 10 ? $"{seconds:0.0}s" : $"{seconds:0.00}s";

    private void StartMetricsTimer()
    {
        _metricsTimer ??= new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
        _metricsTimer.Tick -= MetricsTimerTick;
        _metricsTimer.Tick += MetricsTimerTick;
        _metricsTimer.Start();
    }

    private void MetricsTimerTick(object? sender, EventArgs e)
    {
        // 生成中：本地累计"已用时"（后端心跳每 1.5 秒才来一次，秒表要自己走），
        // token/s 等仍需后端上报，前端不估算。
        if (_metricsState is "running" or "waiting")
        {
            if (_metricsStartUtc != null)
            {
                double ms = (DateTime.UtcNow - _metricsStartUtc.Value).TotalMilliseconds;
                _lastStats["elapsed_ms"] = ((int)ms).ToString();
            }
            RefreshMetricsDisplay();
        }
        else
        {
            _metricsTimer?.Stop();
        }
    }

    private void StopMetricsTimer()
    {
        _metricsTimer?.Stop();
        _metricsStartUtc = null;
    }

    private static void RenderInto(RichTextBox? target, string text)
    {
        if (target is null)
            return;
        target.Document = MarkdownRenderer.Build(text);
    }

    // ---------- 停止生成 ----------

    private async Task TriggerStopAsync()
    {
        if (_backend is null)
            return;
        _stopRequested = true;
        _stopMode = false;
        SendButton.IsEnabled = false;
        StatusText.Text = Loc.T("main.status.stopping", "正在停止...");
        try
        {
            // fire-and-forget：不读取响应，避免与流式读取竞争 stdout
            await _backend.SendFireAndForgetAsync(new { action = "stop" });
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.error.stopFailed", "❌ 停止请求失败: {0}", ex.Message));
        }
    }

    // ---------- 发送指令（指令输入栏） ----------

    private async void CommandInputBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (HandlePaletteKey(e))
        {
            e.Handled = true;
            return;
        }
        if (e.Key == Key.Escape)
        {
            e.Handled = true;
            HideCommandInput();
            return;
        }
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.None)
        {
            e.Handled = true;
            string command = CommandInputBox.Text.Trim();
            if (command.Length > 0)
            {
                if (!ConfirmDestructive(command))
                    return;   // 取消执行时保留输入，便于用户修改
                CommandInputBox.Text = "";
                await SendCommandAsync(command);
            }
        }
    }

    private void CommandInputBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        // 防御：XAML 解析期若该框带初值会先于 CommandPlaceholder 创建而触发本回调
        if (CommandPlaceholder is null)
            return;
        CommandPlaceholder.Visibility = CommandInputBox.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
        UpdateCommandPalette(CommandInputBox);
    }

    private async Task SendCommandAsync(string command)
    {
        if (_isProcessing || _backend is null)
            return;
        SetProcessing(true);
        AddMessage("user", command);
        try
        {
            if (command.Equals("/help", StringComparison.OrdinalIgnoreCase))
                ShowHelpLocally();
            else if (command.Equals("/stop", StringComparison.OrdinalIgnoreCase))
                await TriggerStopAsync();
            else if (command.StartsWith("/snippet", StringComparison.OrdinalIgnoreCase))
                HandleSnippetCommand(command);
            else if (command.StartsWith("/darkmode", StringComparison.OrdinalIgnoreCase))
                HandleDarkMode(command);
            else
                HandleChatResponse(await _backend.SendRequestAsync(new { action = "chat", message = command }));
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message));
        }
        finally
        {
            SetProcessing(false);
            HideCommandInput(); // 指令执行完毕后收起指令输入行
        }
    }

    // ---------- 指令选择栏 ----------

    /// <summary>
    /// 过滤并**排序**指令候选（修复二）。
    ///
    /// 此前只做前缀过滤、保持声明顺序，导致输入 <c>/clear</c> 时首项是 <c>/clearcache</c>，
    /// 直接按 Enter 会执行错误且有副作用的指令。排序规则：
    /// <list type="number">
    /// <item>与输入**完全一致**的命令排最前；</item>
    /// <item>其余按命令名长度升序（长度越短表示已输入前缀占该命令的比例越高、越具体）；</item>
    /// <item>最后按指令表的原有顺序，保证结果稳定可预期。</item>
    /// </list>
    /// </summary>
    internal static List<CommandItem> RankCommands(string text)
    {
        string query = text.Trim();
        string key = query.Length > 1 ? query[1..] : "";
        return Commands
            .Select((command, index) => (Command: command, Index: index))
            .Where(x => query.Length > 0
                        && query.StartsWith('/')
                        && (key.Length == 0
                            || x.Command.Command.StartsWith("/" + key, StringComparison.OrdinalIgnoreCase)))
            .OrderByDescending(x => string.Equals(x.Command.Command, query, StringComparison.OrdinalIgnoreCase))
            .ThenBy(x => x.Command.Command.Length)
            .ThenBy(x => x.Index)
            .Select(x => x.Command)
            .ToList();
    }

    private void UpdateCommandPalette(TextBox source)
    {
        string text = source.Text;
        if (text.Length > 0 && text.StartsWith('/'))
        {
            List<CommandItem> filtered = RankCommands(text);
            if (filtered.Count > 0)
            {
                _paletteSource = source;
                CommandPalette.PlacementTarget = source;
                CommandList.ItemsSource = filtered;
                CommandList.SelectedIndex = 0;
                CommandPalette.IsOpen = true;
                return;
            }
        }
        CloseCommandPalette();
    }

    private bool HandlePaletteKey(KeyEventArgs e)
    {
        if (!CommandPalette.IsOpen)
            return false;
        switch (e.Key)
        {
            case Key.Down:
                MoveCommandSelection(1);
                return true;
            case Key.Up:
                MoveCommandSelection(-1);
                return true;
            case Key.Tab:
                if (CommandList.SelectedItem is CommandItem tabItem)
                    ExecuteCommandItem(tabItem, completeOnly: true);
                return true;
            case Key.Escape:
                CloseCommandPalette();
                return true;
            case Key.Enter:
                if (CommandList.SelectedItem is not CommandItem item)
                    return false; // 无选中项 → 回落为正常发送
                string typed = (_paletteSource?.Text ?? "").Trim();
                bool exact = string.Equals(item.Command, typed, StringComparison.OrdinalIgnoreCase);
                // 若候选不唯一且首项并非精确匹配，先补全而不执行：
                // 匹配项多时（如 /s）应让用户看清完整指令与参数格式后再决定，
                // 而不是盲目执行列表里的第一项。
                if (!exact && CommandList.Items.Count > 1)
                {
                    ExecuteCommandItem(item, completeOnly: true);
                    Toast(Loc.T("main.command.completed",
                        "已补全为 {0}，确认无误后再按 Enter 执行", item.Command));
                    return true;
                }
                ExecuteCommandItem(item);
                return true;
        }
        return false;
    }

    private void ExecuteCommandItem(CommandItem item, bool completeOnly = false)
    {
        CloseCommandPalette();
        var target = _paletteSource ?? CommandInputBox;

        // 需要参数或 Tab 补全 → 只填入"指令+空格"，让用户继续输入参数
        if (item.NeedsArgs || completeOnly)
        {
            target.Text = item.Command + " ";
            target.CaretIndex = target.Text.Length;
            target.Focus();
            return;
        }
        // 无需参数 → 直接执行
        if (target == InputBox)
        {
            InputBox.Text = item.Command;
            _ = SendMessageAsync();   // 确认环节在 SendMessageAsync 内统一处理
        }
        else
        {
            // 危险指令在此确认：取消时保留输入框内容，便于用户修改
            if (!ConfirmDestructive(item.Command))
                return;
            CommandInputBox.Text = "";
            _ = SendCommandAsync(item.Command);
        }
    }

    private void MoveCommandSelection(int delta)
    {
        if (CommandList.Items.Count == 0)
            return;
        int index = (CommandList.SelectedIndex + delta + CommandList.Items.Count) % CommandList.Items.Count;
        CommandList.SelectedIndex = index;
        CommandList.ScrollIntoView(CommandList.SelectedItem);
    }

    private void CloseCommandPalette() => CommandPalette.IsOpen = false;

    private void CloseAllPalettes()
    {
        CommandPalette.IsOpen = false;
        SnippetPalette.IsOpen = false;
        UpdateSnippetButtonText();
    }

    private void CommandList_PreviewMouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (CommandList.SelectedItem is not CommandItem item || e.OriginalSource is not DependencyObject source)
            return;
        // 仅当点击落在某个列表项上时才执行
        if (ItemsControl.ContainerFromElement(CommandList, source) is ListBoxItem container && container.IsSelected)
            ExecuteCommandItem(item);
    }

    /// <summary>聊天输入框出现 "/"+英文字母 时：只显示底部提示与按钮文字，不改动加减号图标。</summary>
    private void UpdateCommandUi(string text)
    {
        _commandLike = text.Length >= 1 && text.StartsWith('/') && (text.Length == 1 || IsAsciiLetter(text[1]));
        CommandBarHint.Visibility = _commandLike ? Visibility.Visible : Visibility.Collapsed;
        UpdateCommandButtonText();
    }

    /// <summary>命令按钮右侧文字：识别到指令→"转移到命令输入框"；否则按开合状态显示"打开/关闭命令输入框"。</summary>
    private void UpdateCommandButtonText()
    {
        CommandButtonText.Text = _commandLike
            ? Loc.T("main.command.move", "转移到命令输入框")
            : (_commandRowOpen ? Loc.T("main.command.close") : Loc.T("main.command.open"));
    }

    // ---------- 指令输入行（默认隐藏） ----------

    private void CommandButton_Click(object sender, RoutedEventArgs e)
    {
        CloseAllPalettes();
        if (_commandRowOpen)
        {
            HideCommandInput();
            return;
        }

        // 把聊天输入框中的指令前缀（"/" 及后面的连续英文字母，允许只有单个 "/"）转移进指令输入框
        // 不加尾随空格（仅 Tab 补全时才补空格，避免用户删多余空格）
        string text = InputBox.Text;
        Match match = CommandPrefixRegex.Match(text);
        if (match.Success && match.Value.Length > 0)
        {
            string token = match.Value.TrimEnd();
            InputBox.Text = text[match.Length..];
            CommandInputBox.Text = token;
            InputBox.CaretIndex = InputBox.Text.Length;
        }
        ShowCommandInput();
    }

    private void ShowCommandInput()
    {
        _commandRowOpen = true;
        CommandInputRow.Visibility = Visibility.Visible;
        CommandIconRotate.Angle = 45; // 加号旋转 45° 变叉号
        UpdateCommandButtonText();
        CommandInputBox.Focus();
        CommandInputBox.CaretIndex = CommandInputBox.Text.Length;
    }

    private void HideCommandInput()
    {
        _commandRowOpen = false;
        CommandInputRow.Visibility = Visibility.Collapsed;
        CommandIconRotate.Angle = 0; // 恢复加号
        UpdateCommandButtonText();
        CloseAllPalettes();
        InputBox.Focus();
    }

    // ---------- 上下文占用（工具栏右侧） ----------

    /// <summary>从响应中读取附带的上文占用并刷新显示（/clear、/undo 后实时减少）。</summary>
    private void UpdateContextFromResponse(JsonElement root)
    {
        if (root.TryGetProperty("used", out JsonElement usedEl) && root.TryGetProperty("limit", out JsonElement limitEl)
            && usedEl.ValueKind == JsonValueKind.Number && limitEl.ValueKind == JsonValueKind.Number)
        {
            int used = usedEl.GetInt32();
            int limit = limitEl.GetInt32();
            // 任务十（自测问题修复-1）：显示值改用**上一轮真正发出去的量**。
            //
            // 后端并列给了两个口径（backend/main.py 的 _attach_context）：
            //   used              = 系统提示 + 历史（持久占用的底数）
            //   used_last_request = 上一轮实际发出去的全部内容（含文件正文/检索片段/搜索结果）
            // 旧显示只取 used，于是"占了 355 / 6553（5.4%）"而真实那一次发出去的是 1462 tokens ——
            // 用户说的"远未到 1/3"就是这么来的。这里两者取**较大值**：底数和上一轮都可以是
            // 当前占用的大头，取大不会漏报；后端没给这个字段（旧版）时自然退化为 used。
            int lastRequest = root.TryGetProperty("used_last_request", out JsonElement lrEl)
                              && lrEl.ValueKind == JsonValueKind.Number
                ? lrEl.GetInt32() : 0;
            int shown = Math.Max(used, lastRequest);
            ContextText.Text = FormatContext(shown, limit);
            // 优化五：轻量状态区显示占用百分比（图标 + 简短短语）
            int percent = limit > 0 ? (int)Math.Round(shown * 100.0 / limit) : 0;
            StripContextText.Text = Loc.T("main.context.strip", "📊 上下文 {0}%", Math.Clamp(percent, 0, 100));
            // 提示里把两个口径都写出来：用户才知道"这一轮压了多满"与"底数是多少"的差别
            StripContextButton.ToolTip = Loc.T("main.strip.contextTipTwo",
                "上下文占用 {0:N0} / {1:N0} tokens（{2}%）\n其中常驻底数 {3:N0}，上一轮实际发出 {4:N0}\n点击刷新",
                shown, limit, percent, used, lastRequest);
        }
        if (root.TryGetProperty("kv_cache_text", out JsonElement kv) && kv.ValueKind == JsonValueKind.String)
            SetKvCacheStatus(kv.GetString() ?? "");
        if (root.TryGetProperty("search", out JsonElement se) && se.ValueKind is JsonValueKind.True or JsonValueKind.False)
            SetSearchStatus(se.GetBoolean());
    }

    /// <summary>数字格式化：>=1000 显示 k，>=1000000 显示 m。</summary>
    private static string FormatTokens(int n) =>
        n >= 1_000_000 ? $"{n / 1_000_000.0:0.#}m"
        : n >= 1000 ? $"{n / 1000.0:0.#}k"
        : n.ToString();

    private static string FormatContext(int used, int limit) =>
        limit > 0
            ? Loc.T("main.context.button", "上下文：{0}/{1}", FormatTokens(used), FormatTokens(limit))
            : Loc.T("main.context.button", "上下文：{0}/{1}",
                    Loc.T("main.context.unknown"), Loc.T("main.context.unknown"));

    /// <summary>向后端请求当前上下文占用并更新工具栏显示。</summary>
    private async Task UpdateContextAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "context" });
            using JsonDocument doc = JsonDocument.Parse(json);
            UpdateContextFromResponse(doc.RootElement);
        }
        catch
        {
            // 后端忙碌/异常时忽略，下次交换再更新
        }
    }

    private async void ContextButton_Click(object sender, RoutedEventArgs e) => await UpdateContextAsync();

    private static bool IsAsciiLetter(char c) =>
        (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');

    // ---------- 常用语（常用语模板选择框） ----------

    private void SnippetButton_Click(object sender, RoutedEventArgs e)
    {
        CloseCommandPalette();
        if (SnippetPalette.IsOpen)
        {
            CloseSnippetPalette();
            return;
        }
        SnippetList.ItemsSource = _snippets.Items;
        if (_snippets.Items.Count == 0)
        {
            AddMessage("system", Loc.T("main.snippet.emptyHint",
                "📋 暂无常用语，可先打开命令输入框输入 /snippet add 名称 文本 新增"));
            return;
        }
        // 显示在输入区域上方：命令输入行可见时锚定命令行，否则锚定输入区域容器
        SnippetPalette.PlacementTarget = _commandRowOpen ? (UIElement)CommandInputRow : (UIElement)InputAreaBorder;
        SnippetPalette.IsOpen = true;
        UpdateSnippetButtonText();
    }

    private void CloseSnippetPalette()
    {
        SnippetPalette.IsOpen = false;
        UpdateSnippetButtonText();
    }

    /// <summary>常用语按钮右侧文字：按选择框开合状态显示"打开/关闭常用语选择框"。</summary>
    private void UpdateSnippetButtonText()
    {
        SnippetButtonText.Text = SnippetPalette.IsOpen
            ? Loc.T("main.snippet.close") : Loc.T("main.snippet.open");
    }

    private void SnippetList_PreviewMouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (SnippetList.SelectedItem is not Snippet snippet || e.OriginalSource is not DependencyObject source)
            return;
        if (ItemsControl.ContainerFromElement(SnippetList, source) is ListBoxItem container && container.IsSelected)
        {
            CloseSnippetPalette();
            int index = InputBox.CaretIndex;
            InputBox.Text = InputBox.Text.Insert(index, snippet.Text);
            InputBox.CaretIndex = index + snippet.Text.Length;
            InputBox.Focus();
        }
    }

    private void HandleSnippetCommand(string message)
    {
        var parts = message.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length < 2)
        {
            AddMessage("system", Loc.T("main.snippet.usage",
                "用法: /snippet list | add 名称 文本 | remove 名称"));
            return;
        }
        switch (parts[1].ToLowerInvariant())
        {
            case "list":
                if (_snippets.Items.Count == 0)
                {
                    AddMessage("system", Loc.T("main.snippet.emptyShort",
                        "📋 暂无常用语，可用 /snippet add 名称 文本 新增"));
                    return;
                }
                var sb = new StringBuilder(Loc.T("main.snippet.listHeader", "📋 常用语：\n"));
                foreach (var s in _snippets.Items)
                    sb.AppendLine(Loc.T("main.snippet.listItem", "🔹 {0}：{1}", s.Name, s.Text));
                AddMessage("system", sb.ToString().TrimEnd());
                break;
            case "add":
                if (parts.Length < 4)
                {
                    AddMessage("system", Loc.T("main.snippet.usageAdd", "用法: /snippet add 名称 文本..."));
                    return;
                }
                string name = parts[2];
                string text = string.Join(' ', parts.Skip(3));
                AddMessage("system", _snippets.Add(name, text)
                    ? Loc.T("main.snippet.added", "✅ 已新增常用语「{0}」", name)
                    : Loc.T("main.snippet.exists", "❌ 常用语「{0}」已存在（可先 remove 再添加）", name));
                break;
            case "remove":
                if (parts.Length < 3)
                {
                    AddMessage("system", Loc.T("main.snippet.usageRemove", "用法: /snippet remove 名称"));
                    return;
                }
                AddMessage("system", _snippets.Remove(parts[2])
                    ? Loc.T("main.snippet.removed", "🗑️ 已删除常用语「{0}」", parts[2])
                    : Loc.T("main.snippet.notFound", "❌ 未找到常用语「{0}」", parts[2]));
                break;
            default:
                AddMessage("system", Loc.T("main.snippet.usage",
                    "用法: /snippet list | add 名称 文本 | remove 名称"));
                break;
        }
    }

    // ---------- 帮助 ----------

    /// <summary>后端不可用时的本地 /help 回退渲染（文案同样走语言包）。</summary>
    private void ShowHelpLocally()
    {
        var sb = new StringBuilder();
        sb.AppendLine(Loc.T("main.help.header", "📖 EchoDesk {0} 完整指令介绍 — 由{1}出品",
                            _backend?.Version ?? AppVersion,
                            LocalizedStudio(_backend?.Studio)));
        sb.AppendLine();
        foreach (CommandItem c in Commands)
        {
            string usage = c.Format.Length > 0 ? $"{c.Command} {c.Format}" : c.Command;
            sb.AppendLine($"🔹 {usage}");
            sb.AppendLine($"   {c.Description}");
            sb.AppendLine();
        }
        AddMessage("system", sb.ToString().TrimEnd());
    }

    private void HandleChatResponse(string responseJson)
    {
        using JsonDocument doc = JsonDocument.Parse(responseJson);
        JsonElement root = doc.RootElement;
        string type = root.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "system" : "system";
        string content = root.TryGetProperty("content", out JsonElement c)
            ? c.GetString() ?? Loc.T("main.error.noResponse", "无响应")
            : Loc.T("main.error.noResponse", "无响应");
        string action = root.TryGetProperty("action", out JsonElement a) ? a.GetString() ?? "" : "";

        // /exit：先致谢并倒计时 10 秒，然后自动关闭
        if (action == "exit")
        {
            _backend?.Stop();
            BeginExitCountdown(ThanksMessage);
            return;
        }

        if (type == "assistant")
        {
            // 非流式回复同样可能带思考内容（v0.2.1 模型思考内容分离 任务五：
            // 流式与非流式两条路径都要正确区分展示）
            string reasoning = root.TryGetProperty("reasoning", out JsonElement rs)
                ? rs.GetString() ?? "" : "";
            AddMessage("assistant", content, reasoning);
        }
        else
        {
            AddMessage("system", content);
        }

        // 任务三：模式与搜索状态改读响应里的字段，不再靠中文字符串匹配
        // （否则把界面切成英文后这两个状态就永远不更新了）
        ApplyStatusFields(root);

        // /model 需要下载：征得用户同意后发送下载动作
        if (action == "confirm_model" && root.TryGetProperty("model", out JsonElement modelEl))
            _ = ConfirmModelDownloadAsync(modelEl.GetString() ?? "");

        // 响应附带的上下文占用：/clear、/undo 后实时减少
        UpdateContextFromResponse(root);
    }

    private async Task ConfirmModelDownloadAsync(string modelName)
    {
        var result = MessageBox.Show(
            this,
            Loc.T("main.dialog.modelDownloadText",
                "{0}\n\n是否现在下载？下载需要联网且可能耗时较长。",
                Loc.T("main.model.downloadQuestion", "确定要下载模型 '{0}' 吗？", modelName)),
            Loc.T("main.dialog.modelDownloadTitle"),
            MessageBoxButton.YesNo,
            MessageBoxImage.Question);
        if (result != MessageBoxResult.Yes)
        {
            AddMessage("system", Loc.T("main.model.cancelledUser", "已取消下载"));
            return;
        }

        AddMessage("system", Loc.T("main.model.downloadingNamed",
            "⏳ 正在下载并加载模型 '{0}'，请稍候...", modelName));
        StatusText.Text = Loc.T("main.status.downloadingModel", "正在下载模型……");
        try
        {
            // 流式下载：完整进度进对话框，状态栏只显示简化状态
            string final = await _backend!.SendStreamRequestAsync(
                new { action = "model_download", name = modelName },
                delta =>
                {
                    UpdateLiveStatus(delta);
                    StatusText.Text = SimplifyStatus(delta);
                });
            AddMessage("system", final);
            Toast(Loc.T("main.model.downloadDone", "✅ 模型下载完成：{0}", modelName));
            _ = RefreshStatusBarAsync();
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.model.operationFailed", "❌ 模型操作失败: {0}", ex.Message));
            Toast(Loc.T("main.model.downloadFailed", "❌ 模型下载失败：{0}", ex.Message), error: true);
            StatusText.Text = Loc.T("main.status.ready");
        }
        StatusText.Text = Loc.T("main.status.ready");
    }

    // ---------- 退出倒计时 ----------

    private void BeginExitCountdown(string thanksMessage)
    {
        AddMessage("system", thanksMessage);
        StatusText.Text = Loc.T("main.status.exiting", "正在退出...");
        SetControlsEnabled(false);
        CloseAllPalettes();

        _exitCountdown = 10;
        StatusText.Text = Loc.T("main.status.exitCountdown", "程序将在 {0} 秒后自动关闭", _exitCountdown);
        _exitTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _exitTimer.Tick += (_, _) =>
        {
            _exitCountdown--;
            if (_exitCountdown <= 0)
            {
                _exitTimer.Stop();
                Application.Current.Shutdown();
            }
            else
            {
                StatusText.Text = Loc.T("main.status.exitCountdown", "程序将在 {0} 秒后自动关闭", _exitCountdown);
            }
        };
        _exitTimer.Start();
    }

    // ---------- 文件上传（多文件：v0.2.1 优化已知问题-2 任务八） ----------

    private async void UploadButton_Click(object sender, RoutedEventArgs e)
    {
        CloseAllPalettes();
        var dialog = new Microsoft.Win32.OpenFileDialog
        {
            Title = Loc.T("main.upload.chooseTitle", "选择要上传的文件"),
            // 多选（任务八）：Ctrl / Shift 多选或框选；对话框与拖拽两条路径行为一致
            Multiselect = true,
            Filter = Loc.T("main.upload.filter", "支持的文件")
                     + " (*.doc;*.docx;*.xls;*.xlsx;*.ppt;*.pptx;*.txt;*.pdf)|*.doc;*.docx;*.xls;*.xlsx;*.ppt;*.pptx;*.txt;*.pdf|"
                     + Loc.T("main.upload.filterAll", "所有文件") + " (*.*)|*.*",
        };
        if (dialog.ShowDialog() == true)
            await UploadFilesAsync(dialog.FileNames);
    }

    private async void MainWindow_Drop(object sender, DragEventArgs e)
    {
        if (_backend is null || !e.Data.GetDataPresent(DataFormats.FileDrop))
            return;
        // 多文件拖拽（任务八）：取**全部**路径，不再只取 [0]。
        // 拖入非文件内容（文本片段、图片位图）时 FileDrop 不存在，上面已经返回，
        // 行为与单文件时代一致：忽略而不是报错。
        if (e.Data.GetData(DataFormats.FileDrop) is not string[] paths || paths.Length == 0)
            return;
        await UploadFilesAsync(paths);
    }

    /// <summary>
    /// 上传一个或多个文件（任务八）。
    ///
    /// 与单文件版的差别：**逐份显示状态**（成功 / 失败 / 进度），
    /// 单个失败不影响其他，全部完成后统一提示"已加载 N 个文件"。
    /// 数量与总量超限时由后端告知（``sys.files_too_many`` / ``sys.files_partial``），
    /// 前端只负责如实显示，不自己算一套规则（两边算法不一致会更难排查）。
    /// </summary>
    private async Task UploadFilesAsync(IEnumerable<string> paths)
    {
        if (_backend is null)
            return;
        var list = paths.Where(p => !string.IsNullOrWhiteSpace(p)).ToList();
        if (list.Count == 0)
            return;
        if (list.Count == 1)
        {
            await UploadFilesAsyncCore(list);
            return;
        }
        // 文件过多时先问一次（任务书："文件过多或总大小过大时提示用户并询问是否继续"）
        long totalBytes = 0;
        foreach (string path in list)
        {
            try { totalBytes += new System.IO.FileInfo(path).Length; }
            catch { /* 取不到大小就不计入总量 */ }
        }
        const int maxCount = 10;
        const long maxBytes = 60L * 1024 * 1024;
        bool tooMany = list.Count > maxCount;
        bool tooLarge = totalBytes > maxBytes;
        if (tooMany || tooLarge)
        {
            string reason = tooMany
                ? Loc.T("main.upload.confirmCount", "一次选择了 {0} 个文件（上限 {1} 个）", list.Count, maxCount)
                : Loc.T("main.upload.confirmSize", "所选文件共 {0}（上限 {1}）",
                        FormatBytes(totalBytes), FormatBytes(maxBytes));
            MessageBoxResult answer = MessageBox.Show(
                Loc.T("main.upload.confirmContinue",
                      "{0}。\n\n继续会只处理前 {1} 个文件，其余忽略。是否继续？", reason, maxCount),
                Loc.T("main.upload.confirmTitle", "文件较多"),
                MessageBoxButton.YesNo, MessageBoxImage.Question);
            if (answer != MessageBoxResult.Yes)
                return;
            list = list.Take(maxCount).ToList();
        }
        await UploadFilesAsyncCore(list);
    }

    private static string FormatBytes(long bytes)
        => bytes >= 1048576 ? $"{bytes / 1048576.0:0.#} MB" : $"{bytes / 1024.0:0.#} KB";

    private async Task UploadFilesAsyncCore(List<string> paths)
    {
        AddMessage("system", Loc.T("main.upload.uploadingMany",
            "📥 正在上传 {0} 个文件…", paths.Count));

        // 逐份状态就地更新同一条消息（8 个文件不会把对话框刷满）
        var statusPanel = NewMessagePanel("system",
            Loc.T("main.summary.preparing", "📝 正在解析文档并准备分段摘要…"),
            out FrameworkElement statusContent);
        var statusTarget = statusContent as TextBox;
        MessagePanel.Children.Add(statusPanel);
        MessageScrollViewer.ScrollToBottom();

        var lines = new List<string>();
        int ok = 0, failed = 0;
        void RenderStatus()
        {
            if (statusTarget is null)
                return;
            statusTarget.Text = Emoji.Prefer(string.Join("\n", lines));
            MessageScrollViewer.ScrollToBottom();
        }

        bool previousStopMode = _stopMode;
        _stopRequested = false;
        _stopMode = true;
        SendButton.Content = "⏹";
        SendButton.ToolTip = Loc.T("main.button.stopSummaryTip", "中止分段摘要 (Esc)");
        SendButton.IsEnabled = true;
        try
        {
            string final = await _backend.SendStreamRequestAsync(
                new { action = "file_stream", files = paths },
                _ => { },
                onContext: (_, _) => { },   // 上下文显示改由 UpdateContextFromResponse(root) 统一处理，见下方 onEvent
                onEvent: (type, root) =>
                {
                    // 逐份状态行（任务八）：file_status 只在多文件路径产出
                    if (type == "file_status")
                    {
                        string name = root.TryGetProperty("name", out JsonElement n)
                            ? n.GetString() ?? "" : "";
                        string phase = root.TryGetProperty("phase", out JsonElement p)
                            ? p.GetString() ?? "" : "";
                        int index = root.TryGetProperty("index", out JsonElement ix) ? ix.GetInt32() : 0;
                        int total = root.TryGetProperty("total", out JsonElement tl) ? tl.GetInt32() : 0;
                        if (phase == "start")
                        {
                            lines.Add(Loc.T("main.upload.fileStart", "⏳ [{0}/{1}] {2} …",
                                index, total, name));
                        }
                        else
                        {
                            bool okFlag = root.TryGetProperty("ok", out JsonElement okEl)
                                          && okEl.ValueKind == JsonValueKind.True;
                            if (okFlag)
                            {
                                ok++;
                                int chars = root.TryGetProperty("chars", out JsonElement ch)
                                    ? ch.GetInt32() : 0;
                                lines.Add(Loc.T("main.upload.fileOk", "✅ [{0}/{1}] {2}（{3} 字符）",
                                    index, total, name, chars));
                            }
                            else
                            {
                                failed++;
                                lines.Add(Loc.T("main.upload.fileFailed", "❌ [{0}/{1}] {2} 解析失败",
                                    index, total, name));
                            }
                        }
                        RenderStatus();
                        return;
                    }
                    if (type != "summary_progress" || statusTarget is null)
                        return;
                    string text = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                    if (text.Length == 0)
                        return;
                    // 摘要进度：替换最后一行（当前文件的那一条），不追加新行
                    string prefix = "";
                    if (root.TryGetProperty("file_index", out JsonElement fi)
                        && root.TryGetProperty("file_total", out JsonElement ft))
                    {
                        prefix = $"[{fi.GetInt32()}/{ft.GetInt32()}] ";
                    }
                    if (lines.Count > 0 && lines[^1].StartsWith("⏳", StringComparison.Ordinal))
                        lines[^1] = prefix + text;
                    else
                        lines.Add(prefix + text);
                    RenderStatus();
                });
            if (final.Length > 0)
                AddMessage("system", final);
            else if (paths.Count > 1)
                AddMessage("system", Loc.T("main.upload.doneMany",
                    "✅ 已加载 {0} 个文件（失败 {1} 个）", ok, failed));
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.upload.failed", "❌ 上传失败: {0}", ex.Message));
        }
        finally
        {
            _stopMode = previousStopMode;
            SendButton.Content = "➤";
            SendButton.ToolTip = Loc.T("main.button.sendTip", "发送 (Ctrl+Enter)");
            SendButton.IsEnabled = true;
        }
    }

    // ---------- 主题切换 ----------

    /// <summary>
    /// <c>/darkmode [on|off|auto]</c>：切换浅色 / 深色 / 跟随系统（修复二）。
    /// 无参数时在浅色与深色之间翻转（保持老用户的操作习惯）。
    /// </summary>
    private void HandleDarkMode(string message)
    {
        string arg = message["/darkmode".Length..].Trim().ToLowerInvariant();
        AppTheme target = arg switch
        {
            "on" or "dark" => AppTheme.Dark,
            "off" or "light" => AppTheme.Light,
            "auto" or "system" => AppTheme.System,
            // 无参数时切换：跟随系统状态下先落到"与当前相反"的固定主题
            _ => ThemeService.IsDark ? AppTheme.Light : AppTheme.Dark,
        };
        ApplyTheme(target);
        AddMessage("system", target switch
        {
            AppTheme.Dark => Loc.T("main.theme.dark", "🌙 已切换深色模式"),
            AppTheme.Light => Loc.T("main.theme.light", "☀️ 已切换浅色模式"),
            _ => Loc.T("main.theme.system", "🖥️ 已切换为跟随系统主题"),
        });
    }

    /// <summary>应用主题并持久化（修复二：主题状态只有 ThemeService 一个来源）。</summary>
    private void ApplyTheme(AppTheme theme)
    {
        ThemeService.Apply(theme);
        _settings.Theme = ThemeService.Serialize(theme);
        _settings.Save();   // 主题即时生效并记住选择
    }

    /// <summary>
    /// 主题变化后的界面刷新。用 <c>DynamicResource</c> 绑定的控件会自行更新，
    /// 这里只处理一类必须代码干预的内容：已渲染的 Markdown 文档
    /// （FlowDocument 的画刷是在构建时写进 Run/Paragraph 的）。
    /// </summary>
    private void OnThemeChanged(AppTheme theme)
    {
        _settings.Theme = ThemeService.Serialize(ThemeService.Selected);
        _settings.Save();
        foreach (var child in MessagePanel.Children)
        {
            if (child is Panel panel)
                MarkdownRenderer.RebindTheme(panel);
        }
        _settingsWindow?.ApplyThemeResources();
    }

    // ---------- 字号调节（Ctrl+滚轮 / Ctrl+加号减号） ----------

    private void MessageScrollViewer_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (Keyboard.Modifiers == ModifierKeys.Control)
        {
            AdjustFontSize(e.Delta > 0 ? 0.5 : -0.5);
            e.Handled = true;
        }
    }

    /// <summary>调整全局字号并同步到所有消息控件（纯文本 / Markdown / 时间戳以外的内容）。</summary>
    private void AdjustFontSize(double delta)
    {
        _fontSize = Math.Clamp(_fontSize + delta, 10, 24);
        UpdateAllMessageFontSize();
        StatusText.Text = Loc.T("main.status.fontSize", "字号 {0}", _fontSize.ToString("0.#"));
        _settings.FontSize = _fontSize;
        _settings.Save();
    }

    private void UpdateAllMessageFontSize()
    {
        foreach (var child in MessagePanel.Children)
        {
            // Agent 步骤区是消息区里的独立区块（不是消息面板，所以不在内层循环里）：
            // 它没有 Tag="content"，必须单独跟上字号。
            if (child is Border traceBorder && Equals(traceBorder.Tag, "agent-trace"))
            {
                ScaleAgentTrace(traceBorder);
                continue;
            }
            if (child is not Panel panel)
                continue;
            foreach (var inner in panel.Children)
            {
                if (inner is not FrameworkElement fe)
                    continue;
                // 思考折叠区（Tag="reasoning"）里的正文与标题也跟随字号
                if (fe is Expander region && Equals(region.Tag, "reasoning"))
                {
                    ScaleReasoningRegion(region);
                    continue;
                }
                if (!Equals(fe.Tag, "content"))
                    continue;
                switch (fe)
                {
                    case TextBox box:          // 用户/系统消息（纯文本，可选中复制）
                        box.FontSize = _fontSize;
                        break;
                    case RichTextBox rich:     // 助手消息（Markdown；未显式设字号的 Run 会继承）
                        rich.FontSize = _fontSize;
                        break;
                    case TextBlock text:
                        text.FontSize = _fontSize;
                        break;
                }
            }
        }
    }

    // ---------- UI 辅助 ----------

    private void SetProcessing(bool processing)
    {
        _isProcessing = processing;
        SendButton.IsEnabled = !processing;
        StatusText.Text = processing
            ? Loc.T("main.status.processing", "处理中")
            : Loc.T("main.status.ready");
    }

    /// <summary>创建可选中/可复制的纯文本内容控件（只读 TextBox：支持拖选与 Ctrl+C）。</summary>
    /// <param name="text">初始文本。</param>
    /// <param name="brushKey">前景色对应的色板键（修复二：传键而不是画刷，切主题才能跟随）。</param>
    private TextBox CreatePlainContent(string text, string brushKey)
    {
        var box = new TextBox
        {
            Text = text,
            IsReadOnly = true,
            IsReadOnlyCaretVisible = false,
            BorderThickness = new Thickness(0),
            Background = Brushes.Transparent,
            Padding = new Thickness(0),
            Margin = new Thickness(0),
            TextWrapping = TextWrapping.Wrap,
            // 禁用内部滚动：让滚轮事件冒泡给外层消息列表，滚动体验与整页一致
            VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            FontSize = _fontSize,
            Tag = "content",
            Cursor = Cursors.IBeam,
            ToolTip = Loc.T("main.message.selectTip"),
        };
        ThemeService.Bind(box, TextBox.ForegroundProperty, brushKey);
        return box;
    }

    /// <summary>创建助手消息用的只读 RichTextBox（Markdown 渲染 + 可选中复制）。</summary>
    private RichTextBox CreateRichContent()
    {
        return new RichTextBox
        {
            IsReadOnly = true,
            BorderThickness = new Thickness(0),
            Background = Brushes.Transparent,
            Padding = new Thickness(0),
            Margin = new Thickness(0),
            FontSize = _fontSize,
            Tag = "content",
            IsDocumentEnabled = true,
            VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
    }

    /// <summary>创建消息面板：右上角时间戳 + 内容（Tag="content" 便于统一调字号）+ 复制操作条。</summary>
    private StackPanel NewMessagePanel(string role, string text, out FrameworkElement content)
    {
        (string icon, string brushKey) = role switch
        {
            "user" => ("👤 ", "UserBrush"),
            "assistant" => ("🤖 ", "AssistantBrush"),
            _ => ("ℹ️ ", "SystemBrush"),
        };

        var time = new TextBlock
        {
            Text = DateTime.Now.ToString("HH:mm:ss"),
            FontSize = 10,
            HorizontalAlignment = HorizontalAlignment.Right,
        };
        // 用资源引用而不是 TryFindResource 取值快照：否则切主题后旧消息保持旧色
        ThemeService.Bind(time, TextBlock.ForegroundProperty, "SubTextBrush");
        content = CreatePlainContent(icon + text, brushKey);
        var panel = new StackPanel { Margin = new Thickness(5) };
        panel.Children.Add(time);
        panel.Children.Add(content);
        return panel;
    }

    /// <summary>在消息面板底部追加复制操作条（复制 / Markdown / 代码 / 全选）。</summary>
    private void AddMessageActions(StackPanel panel, string plainText, string? markdown = null)
    {
        var bar = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Margin = new Thickness(0, 2, 0, 0),
        };

        string plain = plainText;
        bar.Children.Add(MakeActionButton("📋 " + Loc.T("main.message.copy", "复制"),
            Loc.T("main.message.copyPlainTip", "复制为纯文本"),
            () => CopyToClipboard(plain, Loc.T("main.message.copiedPlain", "已复制为纯文本"))));

        if (markdown is not null)
        {
            string md = markdown;
            bar.Children.Add(MakeActionButton("⧉ " + Loc.T("main.message.copyMarkdown", "Markdown"),
                Loc.T("main.message.copyMarkdownTip", "复制原始 Markdown 源码"),
                () => CopyToClipboard(md, Loc.T("main.message.copiedMarkdown", "已复制 Markdown 源码"))));

            string code = MarkdownHelper.ExtractCodeBlocks(md);
            if (code.Length > 0)
                bar.Children.Add(MakeActionButton("</> " + Loc.T("main.message.copyCode", "代码"),
                    Loc.T("main.message.copyCodeTip", "复制全部代码块"),
                    () => CopyToClipboard(code, Loc.T("main.message.copiedCode", "已复制代码块"))));
        }

        var target = panel.Children.OfType<FrameworkElement>().FirstOrDefault(e => Equals(e.Tag, "content"));
        bar.Children.Add(MakeActionButton(Loc.T("main.message.selectAll", "全选"),
            Loc.T("main.message.selectAllTip", "选中本条消息全部文本"), () => SelectAll(target)));
        panel.Children.Add(bar);
    }

    private static Button MakeActionButton(string caption, string tooltip, Action onClick)
    {
        var button = new Button
        {
            Content = caption,
            FontSize = 10,
            Padding = new Thickness(6, 1, 6, 1),
            Margin = new Thickness(4, 0, 0, 0),
            MinWidth = 0,
            Cursor = Cursors.Hand,
            ToolTip = tooltip,
            Style = Application.Current.TryFindResource("RoundButton") as Style,
        };
        button.Click += (_, _) => onClick();
        return button;
    }

    /// <summary>把文本写入剪贴板（剪贴板被占用时重试，失败给出提示）。</summary>
    private void CopyToClipboard(string text, string successMessage)
    {
        if (string.IsNullOrEmpty(text))
        {
            Toast(Loc.T("main.message.nothingToCopy", "⚠️ 没有可复制的内容"));
            return;
        }
        for (int attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                Clipboard.SetText(text);
                Toast(Loc.T("main.toast.copiedCount", "✅ {0}（{1} 字符）", successMessage, text.Length));
                return;
            }
            catch (Exception)
            {
                System.Threading.Thread.Sleep(60);  // 剪贴板被其他程序短暂占用
            }
        }
        Toast(Loc.T("main.error.clipboardBusy", "❌ 复制失败：剪贴板被其他程序占用，请重试"));
    }

    /// <summary>取出消息控件中的纯文本（TextBox 取 Text，RichTextBox 取文档文本）。</summary>
    private static string ReadContentText(FrameworkElement? element) => element switch
    {
        TextBox box => box.Text,
        RichTextBox rich => new TextRange(rich.Document.ContentStart, rich.Document.ContentEnd).Text.TrimEnd('\r', '\n'),
        _ => "",
    };

    private static void SelectAll(FrameworkElement? element)
    {
        switch (element)
        {
            case TextBox box:
                box.Focus();
                box.SelectAll();
                break;
            case RichTextBox rich:
                rich.Focus();
                rich.SelectAll();
                break;
        }
    }

    /// <summary>把全部对话复制为纯文本（快捷键 Ctrl+Shift+C）。</summary>
    private void CopyAllMessages()
    {
        var sb = new StringBuilder();
        foreach (var child in MessagePanel.Children)
        {
            if (child is not StackPanel panel)
                continue;
            var content = panel.Children.OfType<FrameworkElement>().FirstOrDefault(e => Equals(e.Tag, "content"));
            string text = ReadContentText(content);
            if (text.Length > 0)
                sb.AppendLine(text).AppendLine();
        }
        CopyToClipboard(sb.ToString().TrimEnd(), Loc.T("main.message.copiedAll", "已复制全部对话"));
    }

    /// <summary>底部通知条：短暂显示后自动隐藏（下载完成/自动化完成/错误提示等）。</summary>
    private void Toast(string message, bool error = false)
    {
        // 修复六：emoji 不可用时统一降级为 ASCII 标记，避免出现方块
        ToastText.Text = Emoji.Prefer(message);
        ThemeService.Bind(ToastText, TextBlock.ForegroundProperty, error ? "ErrorBrush" : "AccentBrush");
        ToastBorder.Visibility = Visibility.Visible;
        HintText.Visibility = Visibility.Collapsed;
        _toastTimer ??= new DispatcherTimer { Interval = TimeSpan.FromSeconds(6) };
        _toastTimer.Stop();
        _toastTimer.Tick += (_, _) =>
        {
            _toastTimer!.Stop();
            ToastBorder.Visibility = Visibility.Collapsed;
            HintText.Visibility = Visibility.Visible;
        };
        _toastTimer.Start();
    }

    private void AddMessage(string role, string content, string reasoning = "")
    {
        // 任务十二：记下最后一条用户消息，供 Alt+↑ 复制回输入框。
        // 只记**用户**的：复制助手回复没有意义，"再问一次"才是用户会反复做的事。
        if (role == "user" && content.Trim().Length > 0)
            _lastUserMessage = content;
        // 修复六：消息内容里的 emoji 也走同一套降级（后端返回的 system 消息同样带上它）
        content = Emoji.Prefer(content);
        var panel = NewMessagePanel(role, role == "assistant" ? "" : content, out FrameworkElement contentBlock);
        if (role == "assistant")
        {
            // 思考内容折叠展示（v0.2.1 模型思考内容分离 任务四/五）：历史回放与非流式
            // 回复都走这里，默认收起；没有思考内容时**不创建**折叠区（不留空占位）。
            if (!string.IsNullOrWhiteSpace(reasoning))
            {
                var region = CreateReasoningRegion();
                region.Body.Document = MarkdownRenderer.Build(Emoji.Prefer(reasoning.Trim()));
                region.Count.Text = Loc.T("main.reasoning.count", "· {0} 字", reasoning.Trim().Length);
                panel.Children.Insert(1, region.Root);
            }
            ReplaceWithMarkdown(panel, contentBlock, content); // 助手消息渲染为 Markdown
        }
        AddMessageActions(panel, content, role == "assistant" ? content : null);
        MessagePanel.Children.Add(panel);
        MessageScrollViewer.ScrollToBottom();
    }

    /// <summary>把消息面板中的纯文本块替换为渲染好的 Markdown（只读 RichTextBox）。</summary>
    private void ReplaceWithMarkdown(StackPanel panel, FrameworkElement contentBlock, string markdown)
    {
        // 按原位置替换：消息里可能先插了思考折叠区（索引 1），正文必须排在它后面，
        // 否则答案会跑到思考上面去（任务四要求"回答区域只显示最终答案"且顺序自然）。
        int index = panel.Children.IndexOf(contentBlock);
        if (index < 0)
            index = panel.Children.Count;
        panel.Children.RemoveAt(index);
        var richText = CreateRichContent();
        richText.Document = MarkdownRenderer.Build(markdown);
        panel.Children.Insert(Math.Min(index, panel.Children.Count), richText);
    }
}

/// <summary>指令选择栏中的一条指令：Command 指令名，Format 参数格式，Description 完整介绍（/help），Short 精简介绍（指令栏）。</summary>
public sealed record CommandItem(string Command, string Format, string Description, string Short, bool NeedsArgs,
                                 bool Destructive = false)
{
    /// <summary>
    /// 指令列表里展示的完整用法（命令名 + 参数格式）。
    /// 修复二：明确显示参数格式，避免把 /clear 与 /clearcache 之类的相近指令看混。
    /// </summary>
    public string Usage => Format.Length > 0
        ? Loc.T("main.help.usage", "用法：{0}", $"{Command} {Format}")
        : $"{Loc.T("main.help.usage", "用法：{0}", Command)} {Loc.T("main.help.noArgs", "（无参数）")}";

    /// <summary>有副作用且不可逆（/clear、/clearcache）：执行前二次确认（修复二）。</summary>
    public bool HasSideEffects => Destructive;
}

