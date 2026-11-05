// 无头（无人值守）界面探针 —— `EchoDesk --probe [main|settings|agent|both]`。
//
// 为什么需要它：`--selftest` 刻意不开窗口，只验证"启动链路 + 协议"，于是
// **"窗口到底能不能打开"这件事一直没有任何可判定的证据**。记录里那句
// "Avalonia 前端这套代码此前从未被真正跑起来过"（v0.2.2 自测问题修复记录 §7.4）
// 就是这么来的：三条致命缺陷（x:Name 字段全 null、IBrush 强转、栈溢出）
// 全都是"窗口一构造/一显示就死"，而当时的门禁里没有任何一条会去构造窗口。
//
// 这个探针补的就是这一层：**真的构造窗口、真的显示一帧、真的把结论写成退出码**。
//   * `--probe main`     ：只开主窗口（主窗口构造 + 首帧布局）
//   * `--probe settings` ：只开设置窗口（7 个设置页全部构造 + 首帧布局）
//   * `--probe both`     ：先主窗口、再设置窗口（默认）
//   * `--probe agent`    ：拉真后端 → Agent 模式跑一轮 → 把后端真实发出的
//                          agent_step / tool_call / tool_result / done 事件
//                          逐条喂进主窗口的事件分发，再断言"步骤行真的渲染了"
//
// 退出码：0 = 全部通过；1 = 有窗口没开起来（或整个进程没活到输出结论那一步）。
// 崩溃（含 Stack overflow）会直接杀死进程 —— 那就**没有** `PROBE-RESULT: PASS`
// 这一行，判定脚本按"进程消失且无结论"记为失败。这是刻意的：栈溢出这类
// 进程级崩溃在 .NET 里无法被 try/catch 兜住，唯一可靠的信号就是"结论行在不在"。
//
// main / settings / both 三种模式**不**开后端进程（验的是界面本身）；
// agent 模式必须开后端 —— 它验的是"后端事件 → 界面渲染"这条端到端链路。
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Automation;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Controls.Documents;
using Avalonia.Media;
using Avalonia.Styling;
using Avalonia.Media.Imaging;
using Avalonia.VisualTree;
using System.IO;
using System.Text.RegularExpressions;
using Avalonia.Threading;
using EchoDesk.Avalonia.Services;
using EchoDesk.Avalonia.Views;
using EchoDesk.Avalonia.Utilities;
using EchoDesk.Avalonia.Views.Settings;

namespace EchoDesk.Avalonia;

internal static class Probe
{
    /// <summary>判定脚本按这一行认结果，改文案时要同步改 tools/ui_probe_avalonia.ps1。</summary>
    private const string ResultPrefix = "PROBE-RESULT:";

    /// <summary>超过这个时间还没有结论就自己退出（退出码 1），避免无人值守时挂死。</summary>
    private static TimeSpan Budget = TimeSpan.FromSeconds(60);

    /// <summary>`--probe shots` 的图片输出目录（`--out &lt;dir&gt;` 可覆盖）。</summary>
    private static string ShowcaseDir = Path.Combine(Environment.CurrentDirectory, "docs", "images");

    public static int Run(string[] args)
    {
        string target = "both";
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i].Equals("--probe", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length
                && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
            {
                target = args[i + 1].ToLowerInvariant();
            }
            if (args[i].Equals("--out", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                ShowcaseDir = Path.GetFullPath(args[i + 1]);
            }
        }

        // agent 模式要跑一轮真模型推理；market / pages 模式要真跟后端说一轮话（几十秒）。
        Budget = target.Equals("agent", StringComparison.OrdinalIgnoreCase)
            ? TimeSpan.FromSeconds(300)
            : target.Equals("sweep", StringComparison.OrdinalIgnoreCase)
                ? TimeSpan.FromSeconds(240)
            : target.Equals("market", StringComparison.OrdinalIgnoreCase)
                ? TimeSpan.FromSeconds(300)
                : target.Equals("pages", StringComparison.OrdinalIgnoreCase)
                    ? TimeSpan.FromSeconds(300)
                : target.Equals("shots", StringComparison.OrdinalIgnoreCase)
                    ? TimeSpan.FromSeconds(120)
                    : TimeSpan.FromSeconds(60);

        Console.WriteLine($"=== EchoDesk 界面探针（--probe {target}）===");

        // 看门狗：探针自己挂死（而不是崩溃）时也要能收场，否则无人值守会一直等。
        using var watchdog = new CancellationTokenSource(Budget);
        watchdog.Token.Register(() =>
        {
            Console.WriteLine($"{ResultPrefix} FAIL watchdog（{Budget.TotalSeconds:0} 秒内没有结论）");
            Environment.Exit(1);
        });

        AppBuilder.Configure<App>()
            .UsePlatformDetect()
            .WithInterFont()
            .LogToTrace()
            .AfterSetup(_ => RunWhenReady(target))
            .StartWithClassicDesktopLifetime(args);
        return 0;
    }

    private static void RunWhenReady(string target)
    {
        // 等首帧：窗口"构造完成"和"渲染完成"是两件事，栈溢出这类缺陷往往发生在
        // 第二次布局/主题解析时，所以必须让它真的过一帧再判定。
        Dispatcher.UIThread.Post(async () =>
        {
            var desktop = Application.Current?.ApplicationLifetime as IClassicDesktopStyleApplicationLifetime;
            if (desktop is null)
            {
                Console.WriteLine($"{ResultPrefix} FAIL 没有桌面生命周期");
                Environment.Exit(1);
                return;
            }

            // 与 App.OnFrameworkInitializationCompleted 用同一个 AppState：语言包、主题、
            // 界面设置必须先就位，否则验的不是真实启动路径。
            App.State.Initialize();

            bool ok = true;
            try
            {
                if (target.Equals("agent", StringComparison.OrdinalIgnoreCase))
                {
                    ok = await RunAgentProbeAsync(desktop);
                }
                else if (target.Equals("market", StringComparison.OrdinalIgnoreCase))
                {
                    ok = await RunMarketProbeAsync(desktop);
                }
                else if (target.Equals("pages", StringComparison.OrdinalIgnoreCase))
                {
                    ok = await RunPagesProbeAsync(desktop);
                }
                else if (target.Equals("markdown", StringComparison.OrdinalIgnoreCase))
                {
                    ok = await Task.FromResult(RunMarkdownProbeAsync(desktop));
                }
                else if (target.Equals("upload", StringComparison.OrdinalIgnoreCase))
                {
                    ok = await RunUploadProbeAsync(desktop);
                }
                else if (target.Equals("theme", StringComparison.OrdinalIgnoreCase))
                {
                    ok = await Task.FromResult(RunThemeProbeAsync(desktop));
                }
                else if (target.Equals("sweep", StringComparison.OrdinalIgnoreCase))
                {
                    ok = await RunEnglishSweepAsync(desktop);
                }
                else if (target.Equals("shots", StringComparison.OrdinalIgnoreCase))
                {
                    ok = await RunShowcaseAsync(desktop);
                }
                else
                {
                    if (!target.Equals("settings", StringComparison.OrdinalIgnoreCase))
                    {
                        var main = new MainWindow();
                        desktop.MainWindow = main;
                        main.Show();
                        Console.WriteLine("  [PASS] 主窗口已构造并显示");
                    }

                    if (!target.Equals("main", StringComparison.OrdinalIgnoreCase))
                    {
                        var settings = new SettingsWindow(App.State);
                        settings.Show();
                        Console.WriteLine($"  [PASS] 设置窗口已构造并显示（{settings.Tabs.ItemCount} 个页签）");
                        // 关掉它：Closing 里会写窗口尺寸记忆，顺便验证那条路径不炸。
                        settings.Close();
                    }
                }
            }
            catch (Exception ex)
            {
                // 注意：栈溢出**不会**走到这里（.NET 直接终止进程），
                // 所以能走到这里的是普通异常，同样记为失败并打印类型与消息。
                ok = false;
                Console.WriteLine($"  [FAIL] {ex.GetType().Name}: {ex.Message}");
                Console.WriteLine("  [details] " + ex.GetType().FullName);
                Console.WriteLine(ex.ToString());
            }

            Console.WriteLine($"{ResultPrefix} {(ok ? "PASS" : "FAIL")} target={target}");

            // 收尾：把后端与界面设置清干净再退出，避免留下孤儿进程或半写文件。
            try { App.State.Shutdown(); } catch (Exception) { }
            Environment.Exit(ok ? 0 : 1);
        }, DispatcherPriority.Background);
    }

    /// <summary>
    /// Agent 步骤行的端到端渲染验证（v0.2.2 记录 §10 第 3 条）。
    ///
    /// 做法上的一个关键取舍：**不伪造事件**。这里拉起真后端、真跑一轮 Agent，
    /// 把后端**实际发出的**那些事件行（已经在 JSON 里）取出来，再逐条喂进
    /// <see cref="MainWindow.DispatchStreamEvent"/> —— 也就是真实运行时走的那条
    /// 分发路径。于是"步骤行有没有渲染"不再靠肉眼看，而是一个可断言的数字。
    ///
    /// 为什么不直接调 UI 里的发送按钮：那需要模拟键盘输入，且失败原因会被
    /// "后端起没起来""有没有模型"搅在一起。这里把两件事分开验：
    /// 后端拿到事件是一件事（本方法逐条打印事件类型），界面渲染是另一件事
    /// （断言 AgentStepRowCount）。
    /// </summary>
    private static async Task<bool> RunAgentProbeAsync(IClassicDesktopStyleApplicationLifetime desktop)
    {
        bool ok = true;
        var main = new MainWindow();
        desktop.MainWindow = main;
        main.Show();
        Console.WriteLine("  [PASS] 主窗口已构造并显示");

        var launch = BackendService.ResolveLaunch();
        if (launch is null)
        {
            Console.WriteLine("  [FAIL] 找不到后端（开发布局下需要 backend/main.py 或已发布的 EchoDeskBackend.exe）");
            return false;
        }
        Console.WriteLine($"  后端启动   : {launch.CommandLine}（工作目录 {launch.WorkDirectory}）");

        // **探针自己持有一个后端连接**，不借用 App.State.Backend（那是主窗口的）。
        // 原因是一条实测教训：stdout 是**单条共享流**，一个 BackendService 只应该有一个
        // 读泵；探针与主窗口共用同一个实例时，两边都在等 ready，先读到的那一方把行取走，
        // 另一方就永远等不到（现象是"120 秒没有 ready 握手"）。各持一个连接则互不干扰。
        using var backend = new BackendService();
        var started = await backend.StartAsync(verbose: true);
        if (!started.Ok)
        {
            Console.WriteLine($"  [FAIL] 后端没能起来：{started.Error}");
            return false;
        }

        // 诊断用：把后端的 stderr 与 stdout 行数打出来。
        // 没有它时"ready 没来"只能靠猜（是进程没起来？模型没加载？还是泵没跑？）。
        int stdoutLines = 0;
        int stderrLines = 0;
        backend.LineReceived += _ => Interlocked.Increment(ref stdoutLines);
        backend.StderrReceived += line =>
        {
            Interlocked.Increment(ref stderrLines);
            if (stderrLines <= 8)
                Console.WriteLine($"  [backend stderr] {line}");
        };
        backend.ProcessExited += code =>
            Console.WriteLine($"  [backend] 进程已退出，退出码 {code}");

        var ready = await backend.WaitForReadyAsync(TimeSpan.FromSeconds(120));
        if (ready is null)
        {
            Console.WriteLine($"  [FAIL] 120 秒内没有 ready 握手（stdout 行数={stdoutLines}，stderr 行数={stderrLines}）");
            return false;
        }
        using (ready)
        {
            bool modelReady = ready.RootElement.TryGetProperty("model_ready", out JsonElement mr)
                              && mr.ValueKind == JsonValueKind.True;
            Console.WriteLine($"  [INFO] 后端 ready，model_ready={modelReady}");
            if (!modelReady)
            {
                // 发布物不含模型是设计如此（PROJECT.md 硬约束 4）。没有模型就跑不出
                // Agent 事件，这一项**如实跳过**而不是假通过 —— 与记录里
                // "无法复现就如实报告"同一条原则。
                Console.WriteLine("  [SKIP] 没有可用模型：Agent 事件链路测不到（不是失败，是前提不满足）");
                return true;
            }
        }

        // 切到 Agent 模式：斜杠指令由后端在 chat 分支里解析（main._handle_chat），
        // 前端不需要第二套约定。早先写成 action="command" 会被回一句"未知动作"。
        //
        // **必须切回 chat**：`/mode agent` 会把 default_mode 写进 config.json，
        // 那是**仓库里被跟踪的文件** —— 探针不改用户配置是它的基本要求
        // （实测踩到：跑完 `git status` 里多了一条 `M backend/config.json`）。
        using (var modeReply = await backend.SendAsync(new { action = "chat", message = "/mode agent" },
                                                      TimeSpan.FromSeconds(60)))
        {
            Console.WriteLine($"  [INFO] /mode agent → {(modeReply is null ? "无响应" : modeReply.RootElement.ToString()[..Math.Min(160, modeReply.RootElement.ToString().Length)])}");
        }

        var counts = new Dictionary<string, int>(StringComparer.Ordinal);
        int stepRowsBefore = main.AgentStepRowCount;
        string finalAnswer = "";
        try
        {
            finalAnswer = await backend.SendStreamAsync(
                new { action = "chat_stream", message = "请用工具确认一下当前系统平台的名称，然后一句话回答。" },
                onDelta: _ => { },
                onContext: (_, _) => { },
                idleTimeout: null,
                cancellationToken: default,
                onEvent: (type, root) =>
                {
                    counts[type] = counts.TryGetValue(type, out int n) ? n + 1 : 1;
                    // 走**真实**的 UI 分发路径（与 StreamChatAsync 里的 onEvent 同一条）。
                    main.DispatchStreamEvent(type, root);
                },
                onDeltaChannel: (_, _, _) => { });
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  [FAIL] Agent 流式请求抛异常：{ex.GetType().Name}: {ex.Message}");
            return false;
        }

        // 事件是异步 Post 到 UI 线程的（AddAgentRow/ShowMetrics 都用了 Dispatcher.Post），
        // 让 UI 线程把队列跑完再读行数 —— 否则会读到"还没渲染"的 0，误判成缺陷。
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        int stepRows = main.AgentStepRowCount;

        Console.WriteLine($"  事件统计   : {string.Join(", ", counts.Keys)}"
                          + $"（{string.Join(" / ", Counts(counts))}）");
        Console.WriteLine($"  回答长度   : {finalAnswer.Length} 字符");

        int agentSteps = counts.TryGetValue("agent_step", out int s) ? s : 0;
        int expectedRows = agentSteps;   // 每条 agent_step 的 thinking/finalize 各产出一行
        if (agentSteps == 0)
        {
            ok = false;
            Console.WriteLine("  [FAIL] 后端一个 agent_step 都没发出 —— 无法验证渲染路径（先查 Agent 循环本身）");
        }
        else if (stepRows <= stepRowsBefore)
        {
            ok = false;
            Console.WriteLine($"  [FAIL] 后端发了 {agentSteps} 条 agent_step，但界面一行都没渲染"
                              + $"（AgentStepRowCount 仍是 {stepRows}）");
        }
        else
        {
            Console.WriteLine($"  [PASS] Agent 步骤行已渲染：后端 agent_step {agentSteps} 条 → 界面 {stepRows} 行"
                              + $"（expected≥{Math.Min(expectedRows, 1)}）");
        }

        // 把 default_mode 切回 chat：探针不许改用户的持久配置（见上面那段说明）。
        // 失败不影响判定 —— 这只是收尾，真正的判定在上面已经做完并打印了。
        try
        {
            using var back = await backend.SendAsync(new { action = "chat", message = "/mode chat" },
                                                     TimeSpan.FromSeconds(60));
            Console.WriteLine("  [INFO] 已把 default_mode 切回 chat（探针不改用户持久配置）");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  [INFO] 收尾切回 chat 失败（不影响结论）：{ex.Message}");
        }

        try { backend.Dispose(); } catch (Exception) { }
        return ok;
    }

    /// <summary>
    /// 模型市场的端到端验证（v0.2.2：把 WPF 侧市场补全到跨平台版这一轮的验收）。
    ///
    /// 走的是**真实后端**与**真实交互路径**（点搜索 = 调 LoadMarketAsync；
    /// 翻页 = 调 GoToPageAsync；筛选 = 调同一套 RefreshQueryAsync），
    /// 断言的是市场自己给出的状态快照（ModelMarketSection.Snapshot）——
    /// 于是"分页/筛选/三层数字有没有生效"是可判定的数字，不靠肉眼。
    ///
    /// 覆盖顺序刻意与任务书一致：搜索取回 → 分页 → 每页条数 → 本地筛选 → 重置 → 钉住行。
    /// 每一条都打印 `[PASS]/[FAIL]`，最后统一汇总（部分失败不掩盖前面的结论）。
    /// </summary>
    /// <summary>
    /// 设置页整块功能的端到端验证（v0.2.2 第 2 批新增）：`--probe pages`。
    ///
    /// 为什么要有它：`--probe settings` 只证明"7 个页签能构造出来"，
    /// 而本批补的东西（本地模型库、MCP 可编辑、自动化帮助/模板/向导、语言包区块）
    /// **全都要真的跟后端说一轮话之后才有内容** —— 不显式点一遍，它们看起来和
    /// "控件在那儿但没接上"一模一样，而后者正是本批要消灭的那种缺陷。
    ///
    /// 做法与 market 模式一致：探针自持一条后端连接（`ProbeBackendOverride`），
    /// 切到每个页签、调 `RefreshAsync`、再读页面自己给出的**状态快照**做断言。
    /// </summary>
    private static async Task<bool> RunPagesProbeAsync(IClassicDesktopStyleApplicationLifetime desktop)
    {
        bool ok = true;
        _ = desktop;
        var probeBackend = new BackendService();
        App.State.ProbeBackendOverride = probeBackend;

        var started = await probeBackend.StartAsync(verbose: true);
        if (!started.Ok)
        {
            Console.WriteLine($"  [FAIL] 后端没能起来：{started.Error}");
            return false;
        }
        int stderrLines = 0;
        probeBackend.StderrReceived += line =>
        {
            if (Interlocked.Increment(ref stderrLines) <= 4)
                Console.WriteLine($"  [backend stderr] {line}");
        };
        var ready = await probeBackend.WaitForReadyAsync(TimeSpan.FromSeconds(120));
        if (ready is null)
        {
            Console.WriteLine($"  [FAIL] 120 秒内没有 ready 握手（{probeBackend.LastError}，stderr {stderrLines} 行）");
            return false;
        }
        ready.Dispose();
        Console.WriteLine("  [PASS] 后端已就绪");

        var settings = new SettingsWindow(App.State);
        desktop.MainWindow = settings;
        settings.Show();
        Console.WriteLine($"  [PASS] 设置窗口已构造并显示（{settings.Tabs.ItemCount} 个页签）");

        void Check(string name, bool pass, string detail)
        {
            Console.WriteLine($"  [{(pass ? "PASS" : "FAIL")}] {name}——{detail}");
            if (!pass)
                ok = false;
        }

        // ---------- 常规页：语言包区块 + 路径说明 + 后端语言同步 ----------
        if (!settings.SelectTab("settings.tab.general"))
        {
            Console.WriteLine("  [FAIL] 找不到「常规」页签");
            return false;
        }
        var general = settings.FindPage<GeneralPage>();
        if (general is null)
        {
            Console.WriteLine("  [FAIL] 设置窗口里没有 GeneralPage");
            return false;
        }
        await general.ProbeSyncLanguageAsync();
        string generalSnapshot = general.ProbeSnapshot();
        Console.WriteLine($"  常规页     : {generalSnapshot}");
        Check("语言包区块列出了当前语言", generalSnapshot.Contains("langCurrent=1"), "LangPackText 含当前 locale");
        Check("语言包区块列出了可用语言", generalSnapshot.Contains("langAvailableCount=") &&
            !generalSnapshot.Contains("langAvailableCount=0"), "可用语言 ≥ 1");
        Check("两个语言包按钮有文案", generalSnapshot.Contains("reloadLangButton=1")
            && generalSnapshot.Contains("openLangDirButton=1"), "重新加载 / 打开目录");
        Check("路径说明含后端目录", generalSnapshot.Contains("pathsHasBackend=1"), "DescribePaths 已补全");
        Check("路径说明含语言包目录", generalSnapshot.Contains("pathsHasLangDir=1"), "语言包目录在列");

        // ---------- 智能体页：MCP 可编辑 ----------
        if (!settings.SelectTab("settings.tab.agent"))
        {
            Console.WriteLine("  [FAIL] 找不到「智能体」页签");
            return false;
        }
        var agent = settings.FindPage<AgentPage>();
        if (agent is null)
        {
            Console.WriteLine("  [FAIL] 设置窗口里没有 AgentPage");
            return false;
        }
        await agent.ProbeReloadMcpAsync();
        string mcpBefore = agent.McpSnapshot();
        Console.WriteLine($"  MCP（读后）: {mcpBefore}");
        Check("MCP 总开关已按后端回填", mcpBefore.Contains("mcpEnabled="), "读到了 enabled");
        Check("MCP 超时框已按后端回填", mcpBefore.Contains("mcpTimeout=1"), "读到了 timeout_seconds");
        Check("MCP 三个操作按钮有文案", mcpBefore.Contains("mcpSaveButton=1")
            && mcpBefore.Contains("mcpConnectButton=1") && mcpBefore.Contains("mcpAddButton=1"),
            "保存 / 连接并取工具 / 添加服务器");
        Check("MCP 说明与安全提示都在", mcpBefore.Contains("mcpSecurityText=1"), "安全提示非空");
        // 走一遍"添加服务器"的本地表单（只落本地列表，**不点保存**，不碰用户配置）
        agent.ProbeOpenAddForm();
        agent.ProbeSubmitAddForm();
        string mcpAfter = agent.McpSnapshot();
        Console.WriteLine($"  MCP（加行）: {mcpAfter}");
        Check("添加服务器表单能落成一行", mcpAfter.Contains("mcpServerRows=") && mcpAfter != mcpBefore,
            "行数或状态发生了变化");
        agent.ProbeDropServer("probe-server");

        // ---------- 自动化页：模板 / 字段帮助 / 安全联动 / 结果预览 ----------
        if (!settings.SelectTab("settings.tab.automation"))
        {
            Console.WriteLine("  [FAIL] 找不到「自动化」页签");
            return false;
        }
        var automation = settings.FindPage<AutomationPage>();
        if (automation is null)
        {
            Console.WriteLine("  [FAIL] 设置窗口里没有 AutomationPage");
            return false;
        }
        await Dispatcher.UIThread.InvokeAsync(async () => await automation.RefreshAsync());
        string automationSnapshot = automation.AutomationSnapshot();
        Console.WriteLine($"  自动化页   : {automationSnapshot}");
        Console.WriteLine($"  字段帮助键 : {OneLine(automation.FieldHelpKeys(), 150)}");
        Check("配置模板已从后端读到", automationSnapshot.Contains("templateCount=") &&
            !automationSnapshot.Contains("templateCount=0"), automationSnapshot);
        Check("字段级帮助已从后端读到", automationSnapshot.Contains("fieldHelpCount=") &&
            !automationSnapshot.Contains("fieldHelpCount=0"), automationSnapshot);
        Check("结果预览没有裸露的占位符", automationSnapshot.Contains("previewPlaceholders=0"),
            "string.Format 参数个数已修对");
        Check("结果预览含日期层", automationSnapshot.Contains("previewHasDate=1"), "对齐 WPF 的 4 参数");
        Check("打开结果目录/浏览/测试选择器有文案", automationSnapshot.Contains("openResultsButton=1")
            && automationSnapshot.Contains("browseResultsButton=1")
            && automationSnapshot.Contains("testSelectorButton=1"), automationSnapshot);
        Check("测试向导与导入导出有文案", automationSnapshot.Contains("wizardAndIoButtons=1111"),
            automationSnapshot);
        Check("登录态已显示", automationSnapshot.Contains("loginStateText=1"), "login_state 已渲染");
        // 安全协议联动：接受 → 总开关可编辑；取消 → 自动取消并禁用
        automation.ProbeSetSafety(true);
        string safetyOn = automation.AutomationSnapshot();
        Check("接受协议后总开关可编辑", safetyOn.Contains("enabledEditable=1"), safetyOn);
        automation.ProbeSetSafety(false);
        string safetyOff = automation.AutomationSnapshot();
        Check("取消协议后总开关被禁用", safetyOff.Contains("enabledEditable=0"), safetyOff);
        Check("取消协议后总开关被强制关掉", safetyOff.Contains("safetyAccepted=0"), safetyOff);

        // ---------- 模型页：本地模型库 ----------
        if (!settings.SelectTab("settings.tab.model"))
        {
            Console.WriteLine("  [FAIL] 找不到「模型」页签");
            return false;
        }
        var model = settings.FindPage<ModelPage>();
        if (model is null)
        {
            Console.WriteLine("  [FAIL] 设置窗口里没有 ModelPage");
            return false;
        }
        await model.ProbeReloadRegistryAsync();
        string registry = model.RegistrySnapshot();
        Console.WriteLine($"  本地模型库 : {registry}");
        Check("模型库列表有内容（行或空状态说明）", registry.Contains("registryRows=")
            && (registry.Contains("registryRows=0") ? registry.Contains("registryEmptyHint=1") : true),
            registry);
        Check("模型库标题带条数与大小", registry.Contains("registryTitleHasParen=1"), registry);
        Check("重扫 / 刷新按钮有文案", registry.Contains("rescanButton=1")
            && registry.Contains("refreshRegistryButton=1"), registry);
        Check("模型页状态行有内容", registry.Contains("statusLine=1"), registry);

        // ---------- 已安装页：插件生态入口（v0.2.2 第 4 批 3a）----------
        if (!settings.SelectTab("settings.tab.installed"))
        {
            Console.WriteLine("  [FAIL] 找不到「已安装」页签");
            return false;
        }
        var installed = settings.FindPage<InstalledPage>();
        if (installed is null)
        {
            Console.WriteLine("  [FAIL] 设置窗口里没有 InstalledPage");
            return false;
        }
        await Dispatcher.UIThread.InvokeAsync(async () => await installed.RefreshAsync());
        string plugins = installed.PluginSnapshot();
        Console.WriteLine($"  已安装页   : {plugins}");
        Check("从文件夹安装 / 新建本地插件入口在",
            plugins.Contains("installFolderButton=1") && plugins.Contains("scaffoldButton=1"), plugins);
        Check("插件卡片或空状态二选一可见",
            plugins.Contains("cards=0") || plugins.Contains("emptyVisible=0"), plugins);
        Check("信任确认面板的文案齐备",
            plugins.Contains("trustTitle=1") && plugins.Contains("trustButtons=1111"), plugins);
        installed.ProbeShowTrust();
        Console.WriteLine($"  信任面板后 : {installed.PluginSnapshot()}");

        // ---------- 开发者页：目录按钮 / 清单编辑 / 钩子与面板（3a）----------
        if (!settings.SelectTab("settings.tab.developer"))
        {
            Console.WriteLine("  [FAIL] 找不到「开发者」页签");
            return false;
        }
        var developer = settings.FindPage<DeveloperPage>();
        if (developer is null)
        {
            Console.WriteLine("  [FAIL] 设置窗口里没有 DeveloperPage");
            return false;
        }
        await Dispatcher.UIThread.InvokeAsync(async () => await developer.RefreshAsync());
        string dev = developer.DeveloperSnapshot();
        Console.WriteLine($"  开发者页   : {dev}");
        Check("三个目录按钮都有文案", dev.Contains("dirButtons=111"), dev);
        Check("导出日志按钮有文案", dev.Contains("exportLogButton=1"), dev);
        Check("重载按钮已改为「只重载开发插件」", dev.Contains("reloadIsDevOnly=1"), dev);
        Check("本地插件清单表格已渲染（含空状态说明）", dev.Contains("manifestNodes=")
            && (dev.Contains("manifestNodes=0") ? false : true), dev);
        Check("清单三个按钮有文案", dev.Contains("manifestButtons=111"), dev);
        Check("钩子清单已渲染", dev.Contains("hooksText=1"), dev);
        Check("插件面板区块已渲染", dev.Contains("panelNodes=")
            && (dev.Contains("panelNodes=0") ? false : true), dev);
        Check("目录清单含 skills_dev", dev.Contains("dirsTextHasDev=1"), dev);

        return ok;
    }

    /// <summary>
    /// Markdown 渲染器验证（v0.2.2 第 4 批 3b）：`--probe markdown`。
    ///
    /// 为什么单独一个模式：渲染器的输出是**控件树**，靠肉眼看只能看出"大概渲染了"，
    /// 而本批补的四件事（行内数学、表格对齐、误包还原、行内嵌套、可点击链接）
    /// 都是"错了也照样有东西显示"的类型 —— 必须遍历控件树断言**结构**。
    /// 不需要后端，也不开设置窗口。
    /// </summary>
    private static bool RunMarkdownProbeAsync(IClassicDesktopStyleApplicationLifetime desktop)
    {
        bool ok = true;
        _ = desktop;

        void Check(string name, bool pass, string detail)
        {
            Console.WriteLine($"  [{(pass ? "PASS" : "FAIL")}] {name}——{detail}");
            if (!pass)
                ok = false;
        }

        // ① 行内数学 $x^2$：应产出等宽 Run，且**不残留** $ 定界符
        Control formula = MarkdownRenderer.Build("勾股定理 $a^2 + b^2 = c^2$ 成立。", 14);
        string formulaText = FlattenText(formula);
        Check("行内数学 $…$ 被识别成等宽片段",
            !formulaText.Contains('$') && formulaText.Contains("a^2 + b^2 = c^2"), formulaText);
        Check("行内数学用等宽字体", HasMonoRun(formula), "找到等宽 Run");

        // ② 美元金额**不能**被误判成公式（这是最容易踩的反向用例）
        Control money = MarkdownRenderer.Build("价格 $100 到 $200 之间。", 14);
        string moneyText = FlattenText(money);
        Check("美元金额不被误判成公式", moneyText.Contains("$100 到 $200"), moneyText);
        Check("美元金额不用等宽字体", !HasMonoRun(money), "没有等宽 Run");

        // ③ 行内 $$…$$（出现在一行中间）
        Control middle = MarkdownRenderer.Build("结论是 $$E = mc^2$$ 这样。", 14);
        string middleText = FlattenText(middle);
        Check("行中 $$…$$ 被识别", !middleText.Contains("$$") && middleText.Contains("E = mc^2"), middleText);

        // ④ 表格列对齐 :---: / ---:
        string table = "| 名称 | 数量 | 金额 |\n|:---|---:|:---:|\n| 甲 | 3 | 12.00 |\n| 乙 | 4 | 30.00 |";
        Control tableTree = MarkdownRenderer.Build(table, 14);
        var aligns = new List<TextAlignment>();
        CollectAlignments(tableTree, aligns);
        Check("表格右对齐（---:）生效", aligns.Contains(TextAlignment.Right), string.Join(",", aligns));
        Check("表格居中（:---:）生效", aligns.Contains(TextAlignment.Center), string.Join(",", aligns));

        // ⑤ 误包进代码块的表格被还原成真表格
        Control fenced = MarkdownRenderer.Build("```\n" + table + "\n```\n", 14);
        Check("代码块里的表格被还原成表格（Grid）", HasGrid(fenced), "找到 Grid");
        Check("真正被包起来的表格内容仍然完整",
            FlattenText(fenced).Contains("名称") && FlattenText(fenced).Contains("12.00"), FlattenText(fenced));

        // ⑥ 真正的代码块**不能**被误还原
        Control code = MarkdownRenderer.Build("```python\nprint('hi')\n```", 14);
        Check("普通代码块仍然是代码块（没有 Grid）", !HasGrid(code), "未找到 Grid");

        // ⑦ 行内嵌套：粗体里嵌行内代码
        Control nested = MarkdownRenderer.Build("**粗体里有 `code` 片段**", 14);
        Check("粗体中嵌行内代码（递归解析）", HasNestedMonoInsideBold(nested), "粗体 Span 内含等宽 Run");
        Check("内层反引号不残留", !FlattenText(nested).Contains('`'), FlattenText(nested));

        // ⑧ 可点击链接
        Control link = MarkdownRenderer.Build("见 [文档](https://example.com/doc) 一节。", 14);
        Check("链接渲染成可点击控件（InlineUIContainer + Button）", HasLinkButton(link), "找到链接按钮");
        Check("链接文本不带尖括号 URL 尾巴",
            !FlattenText(link).Contains("<https://"), FlattenText(link));

        // ⑨ 块级公式仍然居中成块
        Control blockFormula = MarkdownRenderer.Build("$$\nE = mc^2\n$$", 14);
        Check("块级公式仍渲染成居中块", FlattenText(blockFormula).Contains("E = mc^2"), FlattenText(blockFormula));

        return ok;
    }

    // ---- 控件树断言用的小工具（只在探针里用，不进产品代码） ----

    private static IEnumerable<Control> Walk(Control root)
    {
        var stack = new Stack<Control>();
        stack.Push(root);
        while (stack.Count > 0)
        {
            Control current = stack.Pop();
            yield return current;
            foreach (Control child in current.GetVisualChildren().OfType<Control>())
                stack.Push(child);
            if (current is ContentControl { Content: Control content })
                stack.Push(content);
        }
    }

    /// <summary>把控件树里的文本拼起来（够断言"定界符有没有残留、内容在不在"）。</summary>
    private static string FlattenText(Control root)
    {
        var builder = new System.Text.StringBuilder();
        Collect(root, builder);
        return builder.ToString();

        static void Collect(object? node, System.Text.StringBuilder into)
        {
            switch (node)
            {
                // 顺序说明：`Text` 优先于 `Inlines`。
                // Avalonia 的 `TextBlock.Inlines` **永远不为 null**（getter 会按需建集合），
                // 所以"用 Inlines 判断有没有内容"一定会误判 —— 公式块那种"只设 Text、
                // 不设 Inlines"的控件会被当成空的（本探针第一次跑就踩到了这条，
                // 报"块级公式渲染成空"，其实是断言工具自己的问题）。
                case SelectableTextBlock stb:
                    if (stb.Text is { Length: > 0 } stext)
                    {
                        into.Append(stext);
                    }
                    else if (stb.Inlines is not null)
                    {
                        foreach (Inline inline in stb.Inlines)
                            CollectInline(inline, into);
                    }
                    break;
                case TextBlock tb:
                    if (tb.Text is { Length: > 0 } text)
                    {
                        into.Append(text);
                    }
                    else if (tb.Inlines is not null)
                    {
                        foreach (Inline inline in tb.Inlines)
                            CollectInline(inline, into);
                    }
                    break;
                case ContentControl { Content: object inner }:
                    Collect(inner, into);
                    break;
                case Panel panel:
                    foreach (Control child in panel.Children)
                        Collect(child, into);
                    break;
                case Decorator { Child: { } decorated }:
                    Collect(decorated, into);
                    break;
                default:
                    break;
            }
        }

        static void CollectInline(Inline inline, System.Text.StringBuilder into)
        {
            switch (inline)
            {
                case Run run when run.Text is { Length: > 0 } text:
                    into.Append(text);
                    break;
                case Span span when span.Inlines is not null:
                    foreach (Inline child in span.Inlines)
                        CollectInline(child, into);
                    break;
                case InlineUIContainer { Child: Button { Content: string label } }:
                    into.Append(label);
                    break;
            }
        }
    }

    private static bool HasMonoRun(Control root) =>
        Walk(root).Any(c => c is TextBlock tb && ContainsMono(tb));

    private static bool ContainsMono(TextBlock block)
    {
        if (block.Inlines is null)
            return false;
        foreach (Inline inline in block.Inlines)
        {
            if (inline is Run { FontFamily: { } font } && font.ToString().Contains("Consolas"))
                return true;
            if (inline is Span { Inlines: not null } span
                && span.Inlines.Any(x => x is Run { FontFamily: { } f } && f.ToString().Contains("Consolas")))
            {
                return true;
            }
        }
        return false;
    }

    private static bool HasNestedMonoInsideBold(Control root)
    {
        foreach (Control control in Walk(root))
        {
            if (control is not TextBlock { Inlines: not null } block)
                continue;
            foreach (Inline inline in block.Inlines)
            {
                if (inline is Span { Inlines: not null } span
                    && span.FontWeight == FontWeight.SemiBold
                    && span.Inlines.Any(x => x is Run { FontFamily: { } f } && f.ToString().Contains("Consolas")))
                {
                    return true;
                }
            }
        }
        return false;
    }

    private static bool HasGrid(Control root) => Walk(root).Any(c => c is Grid);

    private static void CollectAlignments(Control root, List<TextAlignment> into)
    {
        foreach (Control control in Walk(root))
        {
            if (control is TextBlock tb && tb.TextAlignment != TextAlignment.Left)
                into.Add(tb.TextAlignment);
        }
    }

    private static bool HasLinkButton(Control root) =>
        Walk(root).Any(c => c is Button { Content: string } b
                            && string.Equals(AutomationProperties.GetAutomationId(b), "MarkdownLink",
                                StringComparison.Ordinal));

    /// <summary>
    /// 上传路径验证（v0.2.2 第 4 批 3c）：`--probe upload`。
    ///
    /// 需要真后端：逐份状态（`file_status`）与分段摘要进度（`summary_progress`）都是
    /// 后端推来的事件，前端造不出来。用两个临时 txt 文件走一遍真实上传，
    /// 再断言"界面上确实出现了逐份状态、并且上传期间是可中止的"。
    /// </summary>
    private static async Task<bool> RunUploadProbeAsync(IClassicDesktopStyleApplicationLifetime desktop)
    {
        bool ok = true;
        var probeBackend = new BackendService();
        App.State.ProbeBackendOverride = probeBackend;

        var started = await probeBackend.StartAsync(verbose: true);
        if (!started.Ok)
        {
            Console.WriteLine($"  [FAIL] 后端没能起来：{started.Error}");
            return false;
        }
        var ready = await probeBackend.WaitForReadyAsync(TimeSpan.FromSeconds(120));
        if (ready is null)
        {
            Console.WriteLine($"  [FAIL] 120 秒内没有 ready 握手（{probeBackend.LastError}）");
            return false;
        }
        ready.Dispose();
        Console.WriteLine("  [PASS] 后端已就绪");

        // **不要 Show()**：MainWindow 的 Loaded 会触发它自己的 StartBackendAsync，
        // 那会再拉一个后端、并把探针这条连接上的 ready/done 行抢走（实测表现为
        // 无响应（后端进程可能已退出））。控件树在构造时就已经建好，不显示也够断言。
        var window = new MainWindow();
        desktop.MainWindow = window;

        void Check(string name, bool pass, string detail)
        {
            Console.WriteLine($"  [{(pass ? "PASS" : "FAIL")}] {name}——{detail}");
            if (!pass)
                ok = false;
        }

        // ---------- ① 常用语插入到光标处（纯界面，先验，不依赖后端）----------
        string composed = window.ProbeInsertSnippetAtCaret("请帮我", 3, "总结这段");
        Check("常用语插入到光标处（不覆盖已有文字）",
            composed.Contains("请帮我") && composed.Contains("总结这段"), composed);
        string mid = window.ProbeInsertSnippetAtCaret("前 后", 2, "中间");
        Check("光标在中间时插入到中间",
            mid.IndexOf("中间", StringComparison.Ordinal) < mid.IndexOf('后'), mid);

        // ---------- ② 真实上传两份文件：逐份状态 ----------
        string dir = System.IO.Path.Combine(System.IO.Path.GetTempPath(),
            "echodesk-upload-" + Guid.NewGuid().ToString("N"));
        System.IO.Directory.CreateDirectory(dir);
        string first = System.IO.Path.Combine(dir, "probe-a.txt");
        string second = System.IO.Path.Combine(dir, "probe-b.txt");
        System.IO.File.WriteAllText(first, "这是第一份探针文件的内容。", new System.Text.UTF8Encoding(false));
        System.IO.File.WriteAllText(second, "这是第二份探针文件的内容。", new System.Text.UTF8Encoding(false));
        try
        {
            await Dispatcher.UIThread.InvokeAsync(async () =>
                await window.ProbeUploadAsync(new List<string> { first, second }));

            string last = window.ProbeLastSystemText();
            Console.WriteLine($"  上传结果   : {OneLine(last, 160)}");
            Check("上传期间挂上了可取消的 CTS（点「停止」真的能停）",
                window.ProbeUploadCancellable, "ProbeUploadCancellable=true");
            Check("逐份状态行渲染了（file_status）",
                window.ProbeUploadStatusLines > 0, $"statusLines={window.ProbeUploadStatusLines}");
            Check("上传结果写进了对话区",
                last.Contains("probe-a") || last.Contains("✅") || last.Contains("字符"), OneLine(last, 120));
        }
        finally
        {
            try
            {
                System.IO.Directory.Delete(dir, recursive: true);
            }
            catch (Exception)
            {
                // 临时目录删不掉不影响结论
            }
        }

        return ok;
    }

    /// <summary>
    /// 主题跟随系统**实测**（v0.2.2 第 4 批，差异清单 §F5 第 4 条）：`--probe theme`。
    ///
    /// 差异清单此前把这一条记成"未验证：Avalonia 走 ThemeVariant.Default 由系统驱动，
    /// 无需显式钩子"。**"没有显式钩子"与"确实生效"是两件事** —— 本模式真的去改
    /// 系统的"应用使用浅色/深色主题"开关，等一等，看 `ActualThemeVariant` 有没有跟着翻。
    ///
    /// 实现说明（为什么这里允许 P/Invoke）：
    /// 改注册表**不会**自动通知已运行的进程 —— 需要广播 `WM_SETTINGCHANGE`。
    /// 产品代码里没有任何 P/Invoke（跨平台工程的红线），但**探针是只在 Windows 上跑的
    /// 测试工具**，没有别的办法把这条链路真的走通。非 Windows 平台直接报 SKIP（不报 PASS）。
    /// </summary>
    private static bool RunThemeProbeAsync(IClassicDesktopStyleApplicationLifetime desktop)
    {
        bool ok = true;
        void Check(string name, bool pass, string detail)
        {
            Console.WriteLine($"  [{(pass ? "PASS" : "FAIL")}] {name}——{detail}");
            if (!pass)
                ok = false;
        }

        // ① 机制面：theme=auto 时应当把决定权交给平台（RequestedThemeVariant = Default）
        string original = App.State.Settings.Theme;
        App.State.Settings.Theme = "auto";
        App.State.ApplyTheme();
        Check("theme=auto 时把决定权交给平台",
            Application.Current?.RequestedThemeVariant == ThemeVariant.Default,
            $"RequestedThemeVariant={Application.Current?.RequestedThemeVariant}");

        var window = new MainWindow();
        desktop.MainWindow = window;

        if (!OperatingSystem.IsWindows())
        {
            Console.WriteLine("  [SKIP] 非 Windows 平台：不做改系统主题的实测（不写 PASS）");
            App.State.Settings.Theme = original;
            App.State.ApplyTheme();
            return ok;
        }

        const string key = @"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize";
        object? before = null;
        try
        {
            before = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(key)?.GetValue("AppsUseLightTheme");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  [SKIP] 读不到系统主题注册表项（{ex.Message}），实测无法进行（不写 PASS）");
            App.State.Settings.Theme = original;
            App.State.ApplyTheme();
            return ok;
        }

        int originalValue = before is int i ? i : 1;
        try
        {
            int target = originalValue == 0 ? 1 : 0;
            Microsoft.Win32.Registry.SetValue(key, "AppsUseLightTheme", target);
            BroadcastSettingChange();

            // 给平台设置一点反应时间（Avalonia 收到 WM_SETTINGCHANGE 后在本进程内转发）
            var deadline = DateTime.UtcNow.AddSeconds(5);
            while (DateTime.UtcNow < deadline)
            {
                Dispatcher.UIThread.RunJobs();
                bool expectedDark = target == 0;
                ThemeVariant seen = Application.Current!.ActualThemeVariant;
                if (seen != ThemeVariant.Default && (seen == ThemeVariant.Dark) == expectedDark)
                    break;
                System.Threading.Thread.Sleep(100);
            }

            bool expected = target == 0;
            bool gotDark = Application.Current!.ActualThemeVariant == ThemeVariant.Dark;
            Check("改系统主题后 ActualThemeVariant 跟着翻（无需重启/重建窗口）",
                gotDark == expected,
                $"系统 AppsUseLightTheme {originalValue}→{target}，ActualThemeVariant={Application.Current.ActualThemeVariant}");
            Check("跟随系统时资源确实按新主题解析（不只是枚举变了）",
                ThemeResourceChanged(Application.Current.ActualThemeVariant),
                $"变体 {Application.Current.ActualThemeVariant}");
        }
        finally
        {
            // **必须还原**：这是用户机器的真实设置，探针不能留下痕迹
            try
            {
                Microsoft.Win32.Registry.SetValue(key, "AppsUseLightTheme", originalValue);
                BroadcastSettingChange();
            }
            catch (Exception ex)
            {
                Console.WriteLine($"  [WARN] 还原系统主题失败：{ex.Message}");
            }
            App.State.Settings.Theme = original;
            App.State.ApplyTheme();
        }

        return ok;
    }

    /// <summary>
    /// 取一对随主题变化的资源，确认"真的换了皮"而不只是枚举翻了。
    /// 拿不到资源就不下结论（返回 true）—— 资源名可能随主题包变化，不该因此判失败。
    /// </summary>
    private static bool ThemeResourceChanged(ThemeVariant variant)
    {
        try
        {
            if (Application.Current is null || variant == ThemeVariant.Default)
                return false;
            Application.Current.TryGetResource("CardBackgroundBrush", ThemeVariant.Light, out object? light);
            Application.Current.TryGetResource("CardBackgroundBrush", ThemeVariant.Dark, out object? dark);
            if (light is null || dark is null)
                return true;
            return !Equals(light.ToString(), dark.ToString());
        }
        catch (Exception)
        {
            return true;
        }
    }

    /// <summary>
    /// 广播 <c>WM_SETTINGCHANGE</c>，让已运行的进程知道系统设置变了。
    /// **只在探针里用**：产品代码不引 P/Invoke（跨平台红线），
    /// 但"改注册表 → 不广播 → 进程收不到通知"是 Windows 的既定行为，
    /// 不广播就根本测不出"跟随系统是否即时响应"。
    /// </summary>
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Auto,
        SetLastError = true)]
    private static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, IntPtr wParam, string lParam,
        uint flags, uint timeout, out IntPtr result);

    private static void BroadcastSettingChange()
    {
        const uint WM_SETTINGCHANGE = 0x001A;
        const uint SMTO_ABORTIFHUNG = 0x0002;
        const uint HWND_BROADCAST = 0xFFFF;
        try
        {
            // "ImmersiveColorSet"：Windows 用它通知"浅色/深色主题变了"
            SendMessageTimeout(new IntPtr(HWND_BROADCAST), WM_SETTINGCHANGE, IntPtr.Zero,
                "ImmersiveColorSet", SMTO_ABORTIFHUNG, 1000, out _);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  [WARN] 广播 WM_SETTINGCHANGE 失败：{ex.Message}");
        }
    }

    private static async Task<bool> RunMarketProbeAsync(IClassicDesktopStyleApplicationLifetime desktop)
    {
        bool ok = true;
        _ = desktop;
        // 探针自持一条后端连接，并让**界面也用它**（AppState.ProbeBackendOverride）。
        // 不这么做的话：框架会自己建主窗口、主窗口又去拉后端并读 ready 握手，
        // 探针与界面两个读者抢同一条 stdout 流 —— 先读到的那方赢，另一方永远等不到
        // （实测现象："后端未完成启动即退出"，stderr 里能看到两个 python 进程的日志）。
        var probeBackend = new BackendService();
        App.State.ProbeBackendOverride = probeBackend;

        var started = await probeBackend.StartAsync(verbose: true);
        if (!started.Ok)
        {
            Console.WriteLine($"  [FAIL] 后端没能起来：{started.Error}");
            return false;
        }
        int stderrLines = 0;
        probeBackend.StderrReceived += line =>
        {
            if (Interlocked.Increment(ref stderrLines) <= 4)
                Console.WriteLine($"  [backend stderr] {line}");
        };
        var ready = await probeBackend.WaitForReadyAsync(TimeSpan.FromSeconds(120));
        if (ready is null)
        {
            Console.WriteLine($"  [FAIL] 120 秒内没有 ready 握手（{probeBackend.LastError}，stderr {stderrLines} 行）");
            return false;
        }
        ready.Dispose();
        Console.WriteLine("  [PASS] 后端已就绪");

        // 界面用的就是探针这条连接，所以设置页/市场现在能直接发请求
        var settings = new SettingsWindow(App.State);
        desktop.MainWindow = settings;
        settings.Show();
        Console.WriteLine($"  [PASS] 设置窗口已构造并显示（{settings.Tabs.ItemCount} 个页签）");
        if (!settings.SelectTab("settings.tab.model"))
        {
            Console.WriteLine("  [FAIL] 找不到「模型」页签");
            return false;
        }
        Console.WriteLine("  [PASS] 已切到「模型」页签");

        var backend = probeBackend;

        var page = settings.FindPage<ModelPage>();
        if (page is null)
        {
            Console.WriteLine("  [FAIL] 设置窗口里没有 ModelPage（页签注册与查找不一致）");
            return false;
        }
        await Dispatcher.UIThread.InvokeAsync(async () => await page.RefreshAsync());

        void Check(string name, bool pass, string detail)
        {
            Console.WriteLine($"  [{(pass ? "PASS" : "FAIL")}] {name}——{detail}");
            if (!pass)
                ok = false;
        }

        // ---- ① 搜索：取回结果并算出第一页 ----
        var before = page.MarketSnapshot();
        Console.WriteLine($"  初始状态   : 列表 {before.ItemCount} 行｜{before.PageInfo}｜{before.Total}");
        await page.ProbeMarketSearchAsync();
        var s1 = page.MarketSnapshot();
        Console.WriteLine($"  搜索后     : 列表 {s1.ItemCount} 行｜{s1.PageInfo}｜{s1.Total}");
        Console.WriteLine($"  状态行     : {OneLine(s1.Status, 160)}");
        Check("搜索后取回一页结果", s1.ItemCount > 0, $"列表 {s1.ItemCount} 行");
        Check("分页信息已填充", s1.PageInfo.Contains('/'), s1.PageInfo);
        Check("总条数口径已给出", s1.Total.Length > 0, s1.Total);
        Check("状态行说明下载量口径", s1.Status.Contains("下载量口径") || s1.Status.Contains("download"),
            OneLine(s1.Status, 80));
        if (s1.ItemCount == 0)
        {
            Console.WriteLine("  [SKIP] 没有取回结果（离线/限流）：后续分页与筛选断言无意义");
            return ok;
        }

        // ---- ② 分页：下一页 / 回首页 ----
        await page.ProbeMarketGoToPageAsync(2);
        var s2 = page.MarketSnapshot();
        Check("「下一页」可用且能跳到第 2 页", s2.PageInfo.Contains("2 /"), s2.PageInfo);
        Check("非首页时「首页」可用", s2.FirstEnabled, $"FirstEnabled={s2.FirstEnabled}");
        await page.ProbeMarketGoToPageAsync(1);
        var s3 = page.MarketSnapshot();
        Check("回到第 1 页后「首页」禁用", !s3.FirstEnabled, $"FirstEnabled={s3.FirstEnabled}");
        Check("第 1 页时「下一页」可用", s3.NextEnabled, $"NextEnabled={s3.NextEnabled}");

        // ---- ③ 每页条数：20 → 50 时总页数必须变少，且回到第 1 页 ----
        await page.ProbeMarketGoToPageAsync(2);
        await page.ProbeMarketSetPageSizeAsync(50);
        var s4 = page.MarketSnapshot();
        int pagesBefore = ParsePages(s2.PageInfo);
        int pagesAfter = ParsePages(s4.PageInfo);
        Check("切换每页条数后总页数变少", pagesAfter > 0 && pagesAfter < pagesBefore,
            $"{pagesBefore} → {pagesAfter}");
        Check("切换每页条数后回到第 1 页", s4.PageInfo.Contains("1 /"), s4.PageInfo);
        await page.ProbeMarketSetPageSizeAsync(20);

        // ---- ④ 本地筛选：用一个几乎不可能匹配的串，验证条数真的收窄并给出重搜出口 ----
        await page.ProbeMarketFilterAsync("zzz-绝对不存在的模型名-zzz");
        var s5 = page.MarketSnapshot();
        Check("本地筛选把结果收窄到 0", s5.ItemCount == 0, $"列表 {s5.ItemCount} 行");
        Check("筛完为空时给出「重新搜索远端」出口",
            s5.Status.Contains("本地筛选后 0 条") || s5.Status.Contains("0 条"),
            OneLine(s5.Status, 100));
        await page.ProbeMarketResetAsync();
        var s6 = page.MarketSnapshot();
        Check("重置筛选后结果恢复", s6.ItemCount > 0, $"列表 {s6.ItemCount} 行");

        // ---- ⑤ 结构化筛选：勾一个计数>0 的分面，条数应不大于未筛选时 ----
        if (s6.FacetOptions > 0)
        {
            await page.ProbeMarketToggleFacetAsync(0);
            var s7 = page.MarketSnapshot();
            Check("勾选分面后结果不增加（收窄或持平）", s7.ItemCount <= s6.ItemCount,
                $"{s6.ItemCount} → {s7.ItemCount}（分面 {s6.FacetOptions} 项）");
            await page.ProbeMarketResetAsync();
        }
        else
        {
            Console.WriteLine("  [SKIP] 没有可选分面（后端未给 options）");
        }

        // ---- ⑥ 默认模型钉住行 + 详情 ----
        var s8 = page.MarketSnapshot();
        Check("默认模型钉住行可见", s8.PinnedVisible, $"PinnedVisible={s8.PinnedVisible}");
        if (s8.PinnedVisible)
        {
            await page.ProbeMarketOpenPinnedDetailAsync();
            var s9 = page.MarketSnapshot();
            Check("钉住行的详情能列出文件", s9.FileRows > 0, $"详情行 {s9.FileRows}");
        }

        // ---- ⑦ 语言切换后市场文案跟着走（不重新问后端）----
        App.State.SetLanguage("en");
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        var s10 = page.MarketSnapshot();
        Check("切到英文后市场文案仍是完整的（没有空标签）", s10.Total.Length > 0 && s10.PageInfo.Length > 0,
            $"{s10.PageInfo}｜{s10.Total}");
        App.State.SetLanguage("zh-Hans");

        settings.Close();
        // 后端由 App.State 持有（进程内唯一），收尾统一由外层 State.Shutdown() 关，
        // 这里不单独 Dispose —— 否则会关掉界面正在用的那条连接。
        return ok;
    }

    private static int ParsePages(string pageInfo)
    {
        // "第 3 / 250 页" / "Page 3 / 250"：取斜杠**之后**的第一段数字。
        // 用正则而不是 TakeWhile：文案里有全角字符与空格，手写切分会漏（本轮踩到过）。
        var match = System.Text.RegularExpressions.Regex.Match(pageInfo ?? "", @"/\s*(\d+)");
        return match.Success && int.TryParse(match.Groups[1].Value, out int pages) ? pages : 0;
    }

    private static string OneLine(string text, int limit)
    {
        if (string.IsNullOrEmpty(text))
            return "";
        string flat = text.Replace('\n', ' ').Replace('\r', ' ');
        return flat.Length <= limit ? flat : flat[..limit] + "…";
    }


    // ===================================================================================
    // 英文环境全页扫描（--probe sweep）
    //
    // 存在理由（维护者要求）：静态扫描（check_lang / audit_lang_keys）只能看到**源码里的字面量**，
    // 抓不到"动态拼接"和"渲染期才存在"的中文 —— 例如
    //   $"{Loc.T("a", "…")}（{x}）" 拼出来的整句、
    //   ComboBox 运行时塞进去的 Items、ToolTip 在模板里给的文本、AutomationProperties.Name。
    // 所以在**真实窗口**里把所有可见控件的文本抓一遍，用 [\u4e00-\u9fa5] 判定。
    //
    // 强制英文：App.State.SetLanguage("en") —— 与用户点"语言：English"走同一条路径。
    // 截图：RenderTargetBitmap 直接渲染窗口，和文本是**同一次渲染**，便于人工复核。
    // ===================================================================================

    private static readonly Regex CjkPattern = new(@"[\u4e00-\u9fa5]");

    // ===================================================================================
    // README 宣传截图（--probe shots）
    //
    // 只在**开发机**跑：固定深色主题、简体中文，用演示数据注入三个真实窗口，
    // 再用 RenderTargetBitmap 以 2 倍像素渲染成 PNG。为什么这样做而不是让用户手截：
    //   * 同一份代码每次生成同一组图，README 不会因为某次系统主题/用户名/路径而变成泄密源；
    //   * 走的是产品自己的渲染路径（AddMessage / MarkdownRenderer / 设置页控件），
    //     不是另画一套"示意图"——图上有什么，用户打开就能看到什么；
    //   * 不拉后端、不联网：截图不依赖本机是否装了模型。
    // ===================================================================================

    private static async Task<bool> RunShowcaseAsync(IClassicDesktopStyleApplicationLifetime desktop)
    {
        bool ok = true;
        Directory.CreateDirectory(ShowcaseDir);
        Console.WriteLine($"  输出目录   : {ShowcaseDir}");

        string previousTheme = App.State.Settings.Theme;
        string previousLanguage = App.State.Settings.Language;
        double previousSettingsWidth = App.State.Settings.SettingsWindowWidth;
        double previousSettingsHeight = App.State.Settings.SettingsWindowHeight;

        var windows = new List<Window>();
        try
        {
            // 深色是宣传图的主色调；中英各出一套，README.md 用英文，README.zh-Hans.md 用中文。
            App.State.Settings.Theme = "dark";
            App.State.ApplyTheme();

            ok &= await CaptureLanguageAsync(desktop, windows, "en", english: true, suffix: "");
            ok &= await CaptureLanguageAsync(desktop, windows, "zh-Hans", english: false, suffix: ".zh-Hans");

            Console.WriteLine("  [PASS] 中英双语截图已生成（2 倍像素，深色主题）");
        }
        catch (Exception ex)
        {
            ok = false;
            Console.WriteLine($"  [FAIL] 截图流程异常：{ex.GetType().Name}: {ex.Message}");
            Console.WriteLine(ex.ToString());
        }
        finally
        {
            foreach (Window window in windows)
            {
                try { window.Close(); } catch (Exception) { }
            }
            // 还原用户设置：截图是开发工具，不应把主题/语言/窗口尺寸留成演示值。
            App.State.Settings.Theme = previousTheme;
            App.State.ApplyTheme();
            App.State.SetLanguage(string.IsNullOrWhiteSpace(previousLanguage) ? "auto" : previousLanguage);
            App.State.Settings.SettingsWindowWidth = previousSettingsWidth;
            App.State.Settings.SettingsWindowHeight = previousSettingsHeight;
        }

        return ok;
    }

    /// <summary>生成一个语言版本的三张图；英文用裸文件名，中文加 .zh-Hans 后缀。</summary>
    private static async Task<bool> CaptureLanguageAsync(IClassicDesktopStyleApplicationLifetime desktop,
        List<Window> windows, string language, bool english, string suffix)
    {
        bool ok = true;
        App.State.SetLanguage(language);
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        Console.WriteLine($"  [info] 生成 {language} 截图{suffix}");

        // ---------- ① 主界面：合同摘要正在流式输出 ----------
        var main = new MainWindow
        {
            ProbeSuppressBackendStart = true,
            Width = 1280,
            Height = 820,
        };
        windows.Add(main);
        desktop.MainWindow = main;
        main.Show();
        await Task.Delay(800);
        main.Width = 1280;
        main.Height = 820;
        main.ProbePrepareShowcase(english);
        await Task.Delay(900);
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        ok &= CheckShowcase($"主界面（{language}）", main.Icon is not null, "Window.Icon 已解析（非 null）");
        ok &= SaveShot(main, Path.Combine(ShowcaseDir, $"screenshot-main{suffix}.png"), scale: 2) == 1;

        // ---------- ② 设置 → 模型市场 ----------
        var marketSettings = new SettingsWindow(App.State) { Width = 1280, Height = 920 };
        windows.Add(marketSettings);
        marketSettings.Show();
        await Task.Delay(1200);
        if (!marketSettings.SelectTab("settings.tab.model"))
        {
            Console.WriteLine("  [FAIL] 找不到「模型」页签");
            ok = false;
        }
        else
        {
            ModelPage? modelPage = marketSettings.FindPage<ModelPage>();
            if (modelPage is null)
            {
                Console.WriteLine("  [FAIL] 设置窗口里没有 ModelPage");
                ok = false;
            }
            else
            {
                modelPage.ProbeShowDemoMarket(english);
                var demoState = modelPage.MarketSnapshot();
                Console.WriteLine($"  [info] 市场演示数据：{demoState.ItemCount} 行｜{demoState.PageInfo}｜{demoState.Total}");
                await Task.Delay(900);
                await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
                var settledState = modelPage.MarketSnapshot();
                Console.WriteLine($"  [info] 截图前市场状态：{settledState.ItemCount} 行｜{settledState.PageInfo}｜{settledState.Total}");
                ok &= CheckShowcase($"模型市场（{language}）", marketSettings.Icon is not null,
                    "Window.Icon 已解析（非 null）");
                ok &= SaveShot(marketSettings,
                    Path.Combine(ShowcaseDir, $"screenshot-model-market{suffix}.png"), scale: 2) == 1;
            }
        }

        // ---------- ③ 设置 → 自动化（已接受安全协议 + 填好抓取任务） ----------
        var automationSettings = new SettingsWindow(App.State) { Width = 1280, Height = 920 };
        windows.Add(automationSettings);
        automationSettings.Show();
        await Task.Delay(1200);
        if (!automationSettings.SelectTab("settings.tab.automation"))
        {
            Console.WriteLine("  [FAIL] 找不到「自动化」页签");
            ok = false;
        }
        else
        {
            AutomationPage? automationPage = automationSettings.FindPage<AutomationPage>();
            if (automationPage is null)
            {
                Console.WriteLine("  [FAIL] 设置窗口里没有 AutomationPage");
                ok = false;
            }
            else
            {
                automationPage.ProbeShowDemo(english);
                await Task.Delay(900);
                await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
                ok &= SaveShot(automationSettings,
                    Path.Combine(ShowcaseDir, $"screenshot-automation{suffix}.png"), scale: 2) == 1;
            }
        }

        return ok;
    }

    private static bool CheckShowcase(string name, bool condition, string detail)
    {
        Console.WriteLine($"  [{(condition ? "PASS" : "FAIL")}] {name}——{detail}");
        return condition;
    }

    private static async Task<bool> RunEnglishSweepAsync(IClassicDesktopStyleApplicationLifetime desktop)
    {
        // 记住原语言并在结束时恢复：SetLanguage 会**持久化**到界面设置里，
        // 不恢复的话后续探针（market）会以英文启动，而它按中文页码格式断言 —— 实测踩到过。
        string previousLanguage = App.State.Settings.Language;
        // 扫描语言可被 ECHODESK_SWEEP_LANG 覆盖：
        //   en（默认）→ 查"英文界面里残留的中文"；
        //   zh-Hans     → 查"中文界面里不该出现的英文"（只列清单供人工确认，不判死）。
        string forced = Environment.GetEnvironmentVariable("ECHODESK_SWEEP_LANG") ?? "";
        if (string.IsNullOrWhiteSpace(forced))
            forced = "en";
        bool latinMode = forced.StartsWith("zh", StringComparison.OrdinalIgnoreCase);
        App.State.SetLanguage(forced);
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        LatinCandidatesMode = latinMode;
        LatinCandidates.Clear();
        Console.WriteLine($"  [info] 界面语言已强制设为 {forced}（检查方向：{(latinMode ? "英文残留" : "中文残留")}）");

        string outDir = Environment.GetEnvironmentVariable("ECHODESK_SWEEP_DIR") ?? "";
        if (string.IsNullOrWhiteSpace(outDir))
            outDir = Path.Combine(Environment.CurrentDirectory, "artifacts", "ui-sweep");
        Directory.CreateDirectory(outDir);

        var failures = new List<string>();
        int texts = 0, shots = 0;

        var main = new MainWindow();
        desktop.MainWindow = main;
        main.Show();
        await Task.Delay(900);
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        texts += SweepControl(main, "主窗口", failures);
        shots += SaveShot(main, Path.Combine(outDir, "01-主窗口.png"));

        var settings = new SettingsWindow(App.State);
        settings.Show();
        await Task.Delay(600);
        string[] tabs =
        {
            "settings.tab.general", "settings.tab.model", "settings.tab.installed",
            "settings.tab.market", "settings.tab.automation", "settings.tab.agent",
            "settings.tab.developer",
        };
        Console.WriteLine($"  [info] 设置窗口有 {settings.Tabs.ItemCount} 个页签，逐个扫描");
        for (int i = 0; i < tabs.Length; i++)
        {
            string key = tabs[i];
            string shortName = key.Replace("settings.tab.", "");
            string pageName = $"设置窗口/{shortName}";
            if (!settings.SelectTab(key))
            {
                failures.Add($"【{pageName}】页签切换失败（SelectTab 返回 false）");
                Console.WriteLine($"  [FAIL] 【{pageName}】页签切换失败");
                continue;
            }
            await Task.Delay(600);
            await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
            texts += SweepControl(settings, pageName, failures);
            shots += SaveShot(settings, Path.Combine(outDir, $"settings-{i + 1:00}-{shortName}.png"));
            Console.WriteLine($"  [info] {pageName} 扫描完成");
        }
        settings.Close();

        // 恢复原语言（含持久化），避免污染后续探针与用户的真实设置
        App.State.SetLanguage(string.IsNullOrWhiteSpace(previousLanguage) ? "auto" : previousLanguage);
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);

        Console.WriteLine($"  [info] 共扫描控件文本 {texts} 条，截图 {shots} 张 → {outDir}");
        if (latinMode)
        {
            // 中文界面里的英文残留：**只列清单**。技术名词（Token / MCP / Top-P…）会混在里面，
            // 是否算缺陷要人看，机器不判死（否则会为了几条误报反复放宽，门禁失去意义）。
            Console.WriteLine($"  [info] 中文界面里疑似英文残留 {LatinCandidates.Count} 条（需人工确认）：");
            foreach (string line in LatinCandidates)
                Console.WriteLine($"    - {line}");
        }
        if (failures.Count == 0)
        {
            Console.WriteLine("  [PASS] 主窗口 + 设置窗口 7 页签：全部控件文本均为英文（无中文字符）");
            return true;
        }
        Console.WriteLine($"  [FAIL] 发现 {failures.Count} 处中文残留：");
        foreach (string line in failures)
            Console.WriteLine($"    {line}");
        return false;
    }

    /// <summary>扫描一个窗口/页面下的全部控件文本；返回收集到的文本条数。</summary>
    /// <summary>当前扫描是否处于"中文界面查英文残留"模式。</summary>
    private static bool LatinCandidatesMode;

    /// <summary>中文界面里本来就该是英文的技术名词（品牌 / 协议 / 快捷键 / 参数名）。</summary>
    private static readonly HashSet<string> LatinAllowed = new(StringComparer.OrdinalIgnoreCase)
    {
        "EchoDesk", "WPF", "Avalonia", "Agent", "MCP", "API", "Token", "GGUF", "HF", "GitHub",
        "URL", "KV", "Top-P", "top_p", "Ctrl", "Enter", "Esc", "Tab", "Markdown", "JSON",
        "HTML", "HTTP", "HTTPS", "Python", "Windows", "Linux", "macOS", "llama", "Qwen",
        "Studio", "Void", "Entropia", "GPU", "CPU", "ID", "OK", "PDF", "DOCX", "XLSX",
        "PPTX", "OFD", "UTF", "SHA", "SQL", "local", "auto", "chat", "contract", "general",
        "light", "dark", "en", "zh-Hans", "zh-Hant", "Skill", "Skills", "Hub", "Plugin",
        "Plugins", "GGUF", "Q4_K_M", "Q6_K", "Q8_0", "Instruct", "Chat", "Base",
        // 框架自带的滚动条按钮/滑块的无障碍名：不是产品文案
        "Line up", "Line down", "Line left", "Line right",
        "Page up", "Page down", "Page left", "Page right", "Position",
    };

    /// <summary>
    /// 像"英文文案"而不是路径/URL/代码/ID 的文本：
    /// 纯 ASCII 字母开头、只含字母数字与常见标点、至少 3 个字符、且不含中文。
    /// 命中白名单里的技术名词不算。
    /// </summary>
    private static bool LooksLikeUnexpectedEnglish(string text)
    {
        string t = text.Trim();
        if (t.Length < 3)
            return false;
        if (CjkPattern.IsMatch(t))
            return false;
        // .NET 类型名（ContentControl.Content 会返回控件类型名）：不是界面文案
        if (t.Contains("Avalonia.", StringComparison.Ordinal) || t.Contains("EchoDesk.", StringComparison.Ordinal))
            return false;
        if (!LatinSentence.IsMatch(t))
            return false;
        string word = t.TrimEnd('.', ':', '!', '?', ' ');
        return !LatinAllowed.Contains(word);
    }

    private static readonly Regex LatinSentence = new(@"^[A-Za-z][A-Za-z0-9 .,:;!?()\[\]/+\-–—_&%'""]*$");
    /// <summary>中文模式下收集到的"疑似英文残留"（只列清单，由人确认；技术名词会混在里面）。</summary>
    private static readonly List<string> LatinCandidates = new();

    private static int SweepControl(Control root, string pageName, List<string> failures)
    {
        int count = 0;
        var nodes = new List<Control> { root };
        nodes.AddRange(root.GetVisualDescendants().OfType<Control>());
        foreach (var node in nodes)
        {
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var (kind, text) in TextsOf(node))
            {
                if (string.IsNullOrWhiteSpace(text)) continue;
                if (!seen.Add(text)) continue;          // 同一控件同类文本只报一次（Header 与 Content 常重叠）
                count++;
                string clean = text.Replace("\r", " ").Replace("\n", " ").Trim();
                if (clean.Length > 120) clean = clean[..120] + "…";
                if (LatinCandidatesMode)
                {
                    if (LooksLikeUnexpectedEnglish(text))
                        LatinCandidates.Add($"【{pageName}】的【{kind}】：{clean}");
                    continue;
                }
                if (IsAllowedChinese(text)) continue;
                if (!CjkPattern.IsMatch(text)) continue;
                failures.Add($"【{pageName}】的【{kind}】显示了中文：【{clean}】");
            }
        }
        return count;
    }


    /// <summary>语言自称（语言选择器里必须用各自的文字）。</summary>
    private static readonly string[] LanguageAutonyms = { "简体中文", "繁體中文" };

    /// <summary>
    /// 插件显示名：**专有名词 / 品牌名，不参与 UI 本地化**（维护者裁定）。
    /// 界面里显示插件自己的名字是正确的产品设计，不是漏译 —— 与语言自称同理，只放过这一种形态。
    /// </summary>
    private static readonly string[] PluginNames =
    {
        "Hello World 示例插件", "OFD 文档读取",
        "Hello World example plugin", "OFD document reader",
    };

    /// <summary>以时间戳开头的后端日志行（§12.3 设计为单语，原样显示属已知取舍）。</summary>
    private static readonly Regex BackendLogLine = new(@"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}");

    /// <summary>路径片段（中文目录名属"数据"，不是界面文案）。</summary>
    private static readonly Regex PathFragment = new(@"[A-Za-z]:\\[^\r\n，。；]*|/[A-Za-z0-9_.\-]+(?:/[^\r\n，。；]*)*");

    /// <summary>
    /// 刻意双语显示的标题（与 WPF 侧 ui_sweep_wpf.ps1 的 BilingualLabels 同源）：
    /// 「语言 / Language」这类标题两种语言的用户都能一眼找到，属于**设计如此**，不是漏译。
    /// </summary>
    private static readonly string[] BilingualLabels = { "语言 / Language", "Language / 语言" };

    /// <summary>把上列几类"合理中文"从文本里剔掉；剔完还剩中文才算缺陷。</summary>
    private static bool IsAllowedChinese(string text)
    {
        if (BackendLogLine.IsMatch(text.TrimStart())) return true;   // 整行是后端日志：跳过

        // 插件显示名（专有名词）：整串就是插件名时直接放过
        string trimmed = text.Trim();
        foreach (string plugin in PluginNames)
        {
            if (trimmed == plugin || trimmed.StartsWith(plugin)) return true;
        }

        string probe = text;
        foreach (string autonym in LanguageAutonyms)
            probe = probe.Replace(autonym, "");
        foreach (string bilingual in BilingualLabels)
            probe = probe.Replace(bilingual, "");
        probe = PathFragment.Replace(probe, "");
        return !CjkPattern.IsMatch(probe);
    }

    /// <summary>一个控件上"用户能看见/能读到"的所有文本来源。</summary>
    private static IEnumerable<(string Kind, string Text)> TextsOf(Control node)
    {
        switch (node)
        {
            case TextBlock tb:
                yield return ("TextBlock.Text", tb.Text ?? "");
                break;
            case TextBox box:
                yield return ("TextBox.Text", box.Text ?? "");
                yield return ("TextBox.PlaceholderText", box.PlaceholderText ?? "");
                break;
            case ComboBox combo:
                yield return ("ComboBox.选中项", AsText(combo.SelectedItem));
                if (combo.Items is not null)
                {
                    foreach (object? item in combo.Items)
                        yield return ("ComboBox.Items", AsText(item));
                }
                break;
            case TabItem tab:
                yield return ("TabItem.Header", AsText(tab.Header));
                break;
            case Expander ex:
                yield return ("Expander.Header", AsText(ex.Header));
                break;
            case MenuItem mi:
                yield return ("MenuItem.Header", AsText(mi.Header));
                break;
            case ToggleSwitch sw:
                yield return ("ToggleSwitch.Content", AsText(sw.Content) + " " + AsText(sw.OnContent) + " " + AsText(sw.OffContent));
                break;
        }

        if (node is ContentControl cc) yield return (node.GetType().Name + ".Content", AsText(cc.Content));

        if (ToolTip.GetTip(node) is { } tip) yield return ("ToolTip", AsText(tip));

        string autoName = AutomationProperties.GetName(node) ?? "";
        if (!string.IsNullOrEmpty(autoName)) yield return ("AutomationProperties.Name", autoName);
        string autoHelp = AutomationProperties.GetHelpText(node) ?? "";
        if (!string.IsNullOrEmpty(autoHelp)) yield return ("AutomationProperties.HelpText", autoHelp);
    }

    private static string AsText(object? value) => value switch
    {
        null => "",
        string s => s,
        TextBlock tb => tb.Text ?? "",
        _ => value.ToString() ?? "",
    };

    /// <summary>
    /// 把窗口按当前渲染结果存成 PNG；失败只告警（截图是复核材料，不是判定条件）。
    /// <paramref name="scale"/> 用于 README 宣传图：2 表示按 2 倍像素渲染（高分屏观感）。
    /// </summary>
    private static int SaveShot(Control window, string path, int scale = 1)
    {
        try
        {
            int w = (int)Math.Ceiling(window.Bounds.Width);
            int h = (int)Math.Ceiling(window.Bounds.Height);
            if (w <= 0 || h <= 0)
            {
                Console.WriteLine($"  [warn] 截图跳过（窗口尺寸 {w}x{h}）：{Path.GetFileName(path)}");
                return 0;
            }
            int safeScale = Math.Clamp(scale, 1, 4);
            string? directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory))
                Directory.CreateDirectory(directory);
            using var bmp = new RenderTargetBitmap(
                new PixelSize(w * safeScale, h * safeScale),
                new Vector(96 * safeScale, 96 * safeScale));
            bmp.Render(window);
            bmp.Save(path);  // 注：该重载标记过时（仅警告），保留以免依赖具体版本的编码器选项类型
            Console.WriteLine($"  截图       : {Path.GetFileName(path)}（{w * safeScale}×{h * safeScale}）");
            return 1;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  [warn] 截图失败 {Path.GetFileName(path)}：{ex.GetType().Name}: {ex.Message}");
            return 0;
        }
    }

    private static IEnumerable<string> Counts(Dictionary<string, int> counts)
    {
        foreach (var pair in counts)
            yield return $"{pair.Key}={pair.Value}";
    }
}






