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
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Threading;
using EchoDesk.Avalonia.Services;
using EchoDesk.Avalonia.Views;
using EchoDesk.Avalonia.Views.Settings;

namespace EchoDesk.Avalonia;

internal static class Probe
{
    /// <summary>判定脚本按这一行认结果，改文案时要同步改 tools/ui_probe_avalonia.ps1。</summary>
    private const string ResultPrefix = "PROBE-RESULT:";

    /// <summary>超过这个时间还没有结论就自己退出（退出码 1），避免无人值守时挂死。</summary>
    private static TimeSpan Budget = TimeSpan.FromSeconds(60);

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
        }

        // agent 模式要跑一轮真模型推理；market 模式要跑一次远端搜索（几十秒）。
        Budget = target.Equals("agent", StringComparison.OrdinalIgnoreCase)
            ? TimeSpan.FromSeconds(300)
            : target.Equals("market", StringComparison.OrdinalIgnoreCase)
                ? TimeSpan.FromSeconds(300)
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

    private static IEnumerable<string> Counts(Dictionary<string, int> counts)
    {
        foreach (var pair in counts)
            yield return $"{pair.Key}={pair.Value}";
    }
}
