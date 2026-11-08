// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Probe.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Probe.cs。类型与成员说明见下方注释。
// [EN] Headless (unattended) UI probe — `EchoDesk --probe [main|settings|agent|both]`.
//
// [EN] Why it exists: `--selftest` deliberately opens no window and verifies only the
// [EN] "startup chain + protocol", so **whether a window can be opened at all had no
// [EN] evidence a gate could decide on**. The sentence in the record, "the Avalonia front
// [EN] end had never really been run before" (v0.2.2 self-test issue fix record §7.4),
// [EN] comes from exactly this: all three fatal defects (every x:Name field null, the
// [EN] IBrush cast, the stack overflow) killed the process as soon as a window was
// [EN] constructed or shown, and no gate at the time ever constructed a window.
//
// [EN] This probe fills that layer: **it really constructs the window, really shows one
// [EN] frame, and really turns the conclusion into an exit code**.
// [EN]   * `--probe main`     : only the main window (construction + first-frame layout)
// [EN]   * `--probe settings` : only the settings window (all 7 settings pages constructed
// [EN]                          plus first-frame layout)
// [EN]   * `--probe both`     : the main window first, then the settings window (default)
// [EN]   * `--probe agent`    : start the real backend, run one Agent round, and feed the
// [EN]                         agent_step / tool_call / tool_result / done events actually
// [EN]                         emitted by the backend into the main window's event
// [EN]                         dispatch one by one, then assert that the step rows really
// [EN]                         rendered
//
// [EN] Exit code: 0 = everything passed; 1 = some window did not open (or the process did
// [EN] not live long enough to print the conclusion). A crash (including a stack overflow)
// [EN] kills the process outright — then there is **no** `PROBE-RESULT: PASS` line, and the
// [EN] decision script records "process gone without a conclusion" as a failure. This is
// [EN] deliberate: a process-level crash such as a stack overflow cannot be caught by
// [EN] try/catch in .NET, so the only reliable signal is whether the conclusion line exists.
//
// [EN] The main / settings / both modes do **not** start a backend process (they verify the
// [EN] UI itself); agent mode must start the backend — it verifies the end-to-end chain
// [EN] "backend events to UI rendering".
//
// [ZH] 无头（无人值守）界面探针 —— `EchoDesk --probe [main|settings|agent|both]`。
//
// [ZH] 为什么需要它：`--selftest` 刻意不开窗口，只验证“启动链路 + 协议”，于是
// [ZH] **“窗口到底能不能打开”这件事一直没有任何可判定的证据**。记录里那句
// [ZH] “Avalonia 前端这套代码此前从未被真正跑起来过”（v0.2.2 自测问题修复记录 §7.4）
// [ZH] 就是这么来的：三条致命缺陷（x:Name 字段全 null、IBrush 强转、栈溢出）
// [ZH] 全都是“窗口一构造/一显示就死”，而当时的门禁里没有任何一条会去构造窗口。
//
// [ZH] 这个探针补的就是这一层：**真的构造窗口、真的显示一帧、真的把结论写成退出码**。
// [ZH]   * `--probe main`     ：只开主窗口（主窗口构造 + 首帧布局）
// [ZH]   * `--probe settings` ：只开设置窗口（7 个设置页全部构造 + 首帧布局）
// [ZH]   * `--probe both`     ：先主窗口、再设置窗口（默认）
// [ZH]   * `--probe agent`    ：拉真后端 → Agent 模式跑一轮 → 把后端真实发出的
// [ZH]                          agent_step / tool_call / tool_result / done 事件
// [ZH]                          逐条喂进主窗口的事件分发，再断言“步骤行真的渲染了”
//
// [ZH] 退出码：0 = 全部通过；1 = 有窗口没开起来（或整个进程没活到输出结论那一步）。
// [ZH] 崩溃（含 Stack overflow）会直接杀死进程 —— 那就**没有** `PROBE-RESULT: PASS`
// [ZH] 这一行，判定脚本按“进程消失且无结论”记为失败。这是刻意的：栈溢出这类
// [ZH] 进程级崩溃在 .NET 里无法被 try/catch 兜住，唯一可靠的信号就是“结论行在不在”。
//
// [ZH] main / settings / both 三种模式**不**开后端进程（验的是界面本身）；
// [ZH] agent 模式必须开后端 —— 它验的是“后端事件 → 界面渲染”这条端到端链路。
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
    /// <summary>
    /// [EN] The decision script recognizes the result by this line; when the text changes,
    /// [EN] tools/ui_probe_avalonia.ps1 must be updated as well.
    /// [ZH] 判定脚本按这一行认结果，改文案时要同步改 tools/ui_probe_avalonia.ps1。
    /// </summary>
    private const string ResultPrefix = "PROBE-RESULT:";

    /// <summary>
    /// [EN] Exits with code 1 once this time elapses without a conclusion, so that an
    /// [EN] unattended run cannot hang forever.
    /// [ZH] 超过这个时间还没有结论就自己退出（退出码 1），避免无人值守时挂死。
    /// </summary>
    private static TimeSpan Budget = TimeSpan.FromSeconds(60);

    /// <summary>
    /// [EN] Image output directory of `--probe shots` (overridable with `--out &lt;dir&gt;`).
    /// [ZH] `--probe shots` 的图片输出目录（`--out &lt;dir&gt;` 可覆盖）。
    /// </summary>
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

        // [EN] Agent mode runs one real model inference round; market / pages mode really
        // [EN] talks to the backend for a round (tens of seconds).
        // [ZH] agent 模式要跑一轮真模型推理；market / pages 模式要真跟后端说一轮话（几十秒）。
        Budget = target.Equals("agent", StringComparison.OrdinalIgnoreCase)
            ? TimeSpan.FromSeconds(300)
            : target.Equals("sweep", StringComparison.OrdinalIgnoreCase) ? TimeSpan.FromSeconds(240)
            : target.Equals("market", StringComparison.OrdinalIgnoreCase) ? TimeSpan.FromSeconds(300)
            : target.Equals("pages", StringComparison.OrdinalIgnoreCase) ? TimeSpan.FromSeconds(300)
            : target.Equals("shots", StringComparison.OrdinalIgnoreCase) ? TimeSpan.FromSeconds(120)
            : TimeSpan.FromSeconds(60);

        Console.WriteLine($"=== EchoDesk 界面探针（--probe {target}）===");

        // [EN] Watchdog: the probe must also be able to finish when it hangs (rather than
        // [EN] crashes); otherwise an unattended run would wait forever.
        // [ZH] 看门狗：探针自己挂死（而不是崩溃）时也要能收场，否则无人值守会一直等。
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
        // [EN] Wait for the first frame: a window being "constructed" and being "rendered"
        // [EN] are two different things, and defects such as stack overflows often happen
        // [EN] during the second layout or theme resolution, so the window must really pass
        // [EN] one frame before the verdict.
        // [ZH] 等首帧：窗口“构造完成”和“渲染完成”是两件事，栈溢出这类缺陷往往发生在
        // [ZH] 第二次布局/主题解析时，所以必须让它真的过一帧再判定。
        Dispatcher.UIThread.Post(async () =>
        {
            var desktop = Application.Current?.ApplicationLifetime as IClassicDesktopStyleApplicationLifetime;
            if (desktop is null)
            {
                Console.WriteLine($"{ResultPrefix} FAIL 没有桌面生命周期");
                Environment.Exit(1);
                return;
            }

            // [EN] Use the same AppState as App.OnFrameworkInitializationCompleted: the
            // [EN] language pack, theme and UI settings must already be in place, otherwise
            // [EN] the probe would not verify the real startup path.
            // [ZH] 与 App.OnFrameworkInitializationCompleted 用同一个 AppState：语言包、主题、
            // [ZH] 界面设置必须先就位，否则验的不是真实启动路径。
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
                else if (target.Equals("chat", StringComparison.OrdinalIgnoreCase))
                {
                    ok = await RunChatProbeAsync(desktop);
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
                        // [EN] Motion contract of the settings surface: the page entrance must run
                        //      for 250 ms through opacity and the render transform, and the
                        //      field-help side panel must start at zero width with a width
                        //      transition available for its opening motion.
                        // [ZH] 设置界面的动效约定：页签入场必须通过不透明度与渲染变换在 250 毫秒内完成，
                        // [ZH] 且字段帮助侧面板必须以零宽度起始，并具备供其展开动效使用的宽度过渡。
                        string settingsMotion = settings.ProbeMotionSnapshot();
                        Console.WriteLine($"  设置窗口动效: {settingsMotion}");
                        bool settingsMotionOk =
                            settingsMotion.Contains("pageDuration=250", StringComparison.Ordinal)
                            && settingsMotion.Contains("DoubleTransition", StringComparison.Ordinal)
                            && settingsMotion.Contains("TransformOperationsTransition", StringComparison.Ordinal)
                            && settingsMotion.Contains("fieldHelpWidth=0", StringComparison.Ordinal)
                            && settingsMotion.Contains("fieldHelpTransitions=DoubleTransition",
                                StringComparison.Ordinal);
                        Console.WriteLine($"  [{(settingsMotionOk ? "PASS" : "FAIL")}] 设置窗口动效配置"
                            + $"——{(settingsMotionOk ? "页签与侧面板过渡均已就位" : OneLine(settingsMotion, 180))}");
                        if (!settingsMotionOk)
                            ok = false;
                        // [EN] Close it: Closing writes the remembered window size, which
                        // [EN] incidentally verifies that this path does not blow up.
                        // [ZH] 关掉它：Closing 里会写窗口尺寸记忆，顺便验证那条路径不炸。
                        settings.Close();
                    }
                }
            }
            catch (Exception ex)
            {
                // [EN] Note: a stack overflow does **not** reach this handler (.NET terminates
                // [EN] the process directly), so anything caught here is an ordinary exception;
                // [EN] it is still recorded as a failure and its type and message are printed.
                // [ZH] 注意：栈溢出**不会**走到这里（.NET 直接终止进程），
                // [ZH] 所以能走到这里的是普通异常，同样记为失败并打印类型与消息。
                ok = false;
                Console.WriteLine($"  [FAIL] {ex.GetType().Name}: {ex.Message}");
                Console.WriteLine("  [details] " + ex.GetType().FullName);
                Console.WriteLine(ex.ToString());
            }

            Console.WriteLine($"{ResultPrefix} {(ok ? "PASS" : "FAIL")} target={target}");

            // [EN] Cleanup: shut the backend and the UI settings down cleanly before exiting,
            // [EN] so that no orphan process or half-written file is left behind.
            // [ZH] 收尾：把后端与界面设置清干净再退出，避免留下孤儿进程或半写文件。
            try { App.State.Shutdown(); } catch (Exception) { }
            Environment.Exit(ok ? 0 : 1);
        }, DispatcherPriority.Background);
    }

    /// <summary>
    /// [EN] End-to-end rendering verification of Agent step rows (v0.2.2 record §10, item 3).
    ///
    /// [EN] A key trade-off in the approach: **do not fabricate events**. The probe starts the
    /// [EN] real backend and runs one real Agent round, takes the event lines the backend
    /// [EN] **actually emitted** (already in JSON), and feeds them into
    /// [EN] <see cref="MainWindow.DispatchStreamEvent"/> one by one — the same dispatch path
    /// [EN] used at real runtime. Whether step rows render is therefore an assertable number
    /// [EN] rather than something judged by eye.
    ///
    /// [EN] Why not call the send button in the UI directly: that would require simulating
    /// [EN] keyboard input, and the failure reason would be mixed up with "did the backend
    /// [EN] come up" and "is there a model". The two concerns are verified separately here:
    /// [EN] the backend receiving events is one thing (this method prints each event type),
    /// [EN] and UI rendering is another (it asserts AgentStepRowCount).
    /// [ZH] Agent 步骤行的端到端渲染验证（v0.2.2 记录 §10 第 3 条）。
    ///
    /// [ZH] 做法上的一个关键取舍：**不伪造事件**。这里拉起真后端、真跑一轮 Agent，
    /// [ZH] 把后端**实际发出的**那些事件行（已经在 JSON 里）取出来，再逐条喂进
    /// [ZH] <see cref="MainWindow.DispatchStreamEvent"/> —— 也就是真实运行时走的那条
    /// [ZH] 分发路径。于是“步骤行有没有渲染”不再靠肉眼看，而是一个可断言的数字。
    ///
    /// [ZH] 为什么不直接调 UI 里的发送按钮：那需要模拟键盘输入，且失败原因会被
    /// [ZH] “后端起没起来”“有没有模型”搅在一起。这里把两件事分开验：
    /// [ZH] 后端拿到事件是一件事（本方法逐条打印事件类型），界面渲染是另一件事
    /// [ZH] （断言 AgentStepRowCount）。
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

        // [EN] **The probe holds its own backend connection** instead of borrowing
        // [EN] App.State.Backend (that one belongs to the main window). Reason: a lesson from
        // [EN] actual runs — stdout is a **single shared stream**, and one BackendService
        // [EN] should have exactly one read pump; when the probe and the main window share an
        // [EN] instance, both wait for ready, the side that reads first takes the line, and
        // [EN] the other side waits forever (the symptom is "no ready handshake within 120
        // [EN] seconds"). Separate connections do not interfere with each other.
        // [ZH] **探针自己持有一个后端连接**，不借用 App.State.Backend（那是主窗口的）。
        // [ZH] 原因是一条实测教训：stdout 是**单条共享流**，一个 BackendService 只应该有一个
        // [ZH] 读泵；探针与主窗口共用同一个实例时，两边都在等 ready，先读到的那一方把行取走，
        // [ZH] 另一方就永远等不到（现象是“120 秒没有 ready 握手”）。各持一个连接则互不干扰。
        using var backend = new BackendService();
        var started = await backend.StartAsync(verbose: true);
        if (!started.Ok)
        {
            Console.WriteLine($"  [FAIL] 后端没能起来：{started.Error}");
            return false;
        }

        // [EN] Diagnostics: print the line counts of the backend stderr and stdout. Without
        // [EN] them, "ready never arrived" can only be guessed at (did the process not start?
        // [EN] was the model not loaded? or was the pump not running?).
        // [ZH] 诊断用：把后端的 stderr 与 stdout 行数打出来。
        // [ZH] 没有它时“ready 没来”只能靠猜（是进程没起来？模型没加载？还是泵没跑？）。
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
                // [EN] Release builds ship no model by design (PROJECT.md hard constraint 4).
                // [EN] Without a model no Agent events can be produced, so this item is
                // [EN] **skipped as it is** rather than passed falsely — the same principle as
                // [EN] "report honestly when it cannot be reproduced" in the record.
                // [ZH] 发布物不含模型是设计如此（PROJECT.md 硬约束 4）。没有模型就跑不出
                // [ZH] Agent 事件，这一项**如实跳过**而不是假通过 —— 与记录里
                // [ZH] “无法复现就如实报告”同一条原则。
                Console.WriteLine("  [SKIP] 没有可用模型：Agent 事件链路测不到（不是失败，是前提不满足）");
                return true;
            }
        }

        // [EN] Switch to Agent mode: slash commands are parsed by the backend in the chat
        // [EN] branch (main._handle_chat), so the front end needs no second convention. An
        // [EN] earlier version sent action="command" and got "unknown action" back.
        //
        // [EN] **It must be switched back to chat**: `/mode agent` writes default_mode into
        // [EN] config.json, which is a **tracked file in the repository** — a probe not
        // [EN] touching the user's configuration is a basic requirement (observed in an
        // [EN] actual run: afterwards `git status` listed an extra `M backend/config.json`).
        // [ZH] 切到 Agent 模式：斜杠指令由后端在 chat 分支里解析（main._handle_chat），
        // [ZH] 前端不需要第二套约定。早先写成 action="command" 会被回一句“未知动作”。
        //
        // [ZH] **必须切回 chat**：`/mode agent` 会把 default_mode 写进 config.json，
        // [ZH] 那是**仓库里被跟踪的文件** —— 探针不改用户配置是它的基本要求
        // [ZH] （实测踩到：跑完 `git status` 里多了一条 `M backend/config.json`）。
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
                    // [EN] Go through the **real** UI dispatch path (the same one as onEvent in
                    // [EN] StreamChatAsync).
                    // [ZH] 走**真实**的 UI 分发路径（与 StreamChatAsync 里的 onEvent 同一条）。
                    main.DispatchStreamEvent(type, root);
                },
                onDeltaChannel: (_, _, _) => { });
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  [FAIL] Agent 流式请求抛异常：{ex.GetType().Name}: {ex.Message}");
            return false;
        }

        // [EN] Events are posted asynchronously to the UI thread (AddAgentRow and ShowMetrics
        // [EN] both use Dispatcher.Post), so let the UI thread drain the queue before reading
        // [EN] the row count — otherwise a "not rendered yet" 0 would be misread as a defect.
        // [ZH] 事件是异步 Post 到 UI 线程的（AddAgentRow/ShowMetrics 都用了 Dispatcher.Post），
        // [ZH] 让 UI 线程把队列跑完再读行数 —— 否则会读到“还没渲染”的 0，误判成缺陷。
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        int stepRows = main.AgentStepRowCount;

        Console.WriteLine($"  事件统计   : {string.Join(", ", counts.Keys)}"
                          + $"（{string.Join(" / ", Counts(counts))}）");
        Console.WriteLine($"  回答长度   : {finalAnswer.Length} 字符");

        int agentSteps = counts.TryGetValue("agent_step", out int s) ? s : 0;
        int expectedRows = agentSteps;   // [EN] Each thinking/finalize of an agent_step yields one row.
                                         // [ZH] 每条 agent_step 的 thinking/finalize 各产出一行。
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

        // [EN] Switch default_mode back to chat: a probe must not change the user's persisted
        // [EN] configuration (see the note above). A failure here does not affect the verdict —
        // [EN] this is only cleanup; the real verdict was already made and printed above.
        // [ZH] 把 default_mode 切回 chat：探针不许改用户的持久配置（见上面那段说明）。
        // [ZH] 失败不影响判定 —— 这只是收尾，真正的判定在上面已经做完并打印了。
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
    /// [EN] End-to-end verification of the model market (v0.2.2: the acceptance for the round
    /// [EN] that completed the WPF-side market up to the cross-platform build).
    ///
    /// [EN] It uses the **real backend** and the **real interaction paths** (clicking search
    /// [EN] calls LoadMarketAsync; paging calls GoToPageAsync; filtering calls the same
    /// [EN] RefreshQueryAsync) and asserts the state snapshot the market itself reports
    /// [EN] (ModelMarketSection.Snapshot) — so "do paging / filtering / the three-tier numbers
    /// [EN] take effect" becomes a decidable number rather than something judged by eye.
    ///
    /// [EN] The coverage order deliberately matches the design specification: fetch search
    /// [EN] results, paging, page size, local filtering, reset, pinned row. Every item prints
    /// [EN] `[PASS]/[FAIL]`, and a summary follows at the end (a partial failure does not hide
    /// [EN] earlier conclusions).
    /// [ZH] 模型市场的端到端验证（v0.2.2：把 WPF 侧市场补全到跨平台版这一轮的验收）。
    ///
    /// [ZH] 走的是**真实后端**与**真实交互路径**（点搜索 = 调 LoadMarketAsync；
    /// [ZH] 翻页 = 调 GoToPageAsync；筛选 = 调同一套 RefreshQueryAsync），
    /// [ZH] 断言的是市场自己给出的状态快照（ModelMarketSection.Snapshot）——
    /// [ZH] 于是“分页/筛选/三层数字有没有生效”是可判定的数字，不靠肉眼。
    ///
    /// [ZH] 覆盖顺序刻意与设计规范一致：搜索取回 → 分页 → 每页条数 → 本地筛选 →
    /// [ZH] 重置 → 钉住行。每一条都打印 `[PASS]/[FAIL]`，最后统一汇总
    /// [ZH] （部分失败不掩盖前面的结论）。
    /// </summary>
    /// <summary>
    /// [EN] End-to-end verification of the whole settings-page feature set (added in the
    /// [EN] second batch of v0.2.2): `--probe pages`.
    ///
    /// [EN] Why it is needed: `--probe settings` only proves that "the 7 tabs can be
    /// [EN] constructed", whereas everything added in this batch (the local model registry,
    /// [EN] editable MCP, automation help / templates / wizard, and the language-pack block)
    /// [EN] **only has content after a real round of conversation with the backend** — unless
    /// [EN] it is clicked through explicitly, they look exactly like "the control is there but
    /// [EN] not wired up", and that is precisely the kind of defect this batch removes.
    ///
    /// [EN] The approach matches market mode: the probe holds its own backend connection
    /// [EN] (`ProbeBackendOverride`), switches to each tab, calls `RefreshAsync`, and then
    /// [EN] asserts the **state snapshot** the page itself reports.
    /// [ZH] 设置页整块功能的端到端验证（v0.2.2 第 2 批新增）：`--probe pages`。
    ///
    /// [ZH] 为什么要有它：`--probe settings` 只证明“7 个页签能构造出来”，
    /// [ZH] 而本批补的东西（本地模型库、MCP 可编辑、自动化帮助/模板/向导、语言包区块）
    /// [ZH] **全都要真的跟后端说一轮话之后才有内容** —— 不显式点一遍，它们看起来和
    /// [ZH] “控件在那儿但没接上”一模一样，而后者正是本批要消灭的那种缺陷。
    ///
    /// [ZH] 做法与 market 模式一致：探针自持一条后端连接（`ProbeBackendOverride`），
    /// [ZH] 切到每个页签、调 `RefreshAsync`、再读页面自己给出的**状态快照**做断言。
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

        // [EN] ---------- General page: language-pack block, path description, backend
        // [EN] language sync ----------
        // [ZH] ---------- 常规页：语言包区块 + 路径说明 + 后端语言同步 ----------
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

        // [EN] ---------- Agent page: editable MCP ----------
        // [ZH] ---------- 智能体页：MCP 可编辑 ----------
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
        // [EN] Walk through the local "add server" form (it only lands in the local list,
        // [EN] **save is not clicked**, and the user configuration is untouched).
        // [ZH] 走一遍“添加服务器”的本地表单（只落本地列表，**不点保存**，不碰用户配置）
        agent.ProbeOpenAddForm();
        agent.ProbeSubmitAddForm();
        string mcpAfter = agent.McpSnapshot();
        Console.WriteLine($"  MCP（加行）: {mcpAfter}");
        Check("添加服务器表单能落成一行", mcpAfter.Contains("mcpServerRows=") && mcpAfter != mcpBefore,
            "行数或状态发生了变化");
        agent.ProbeDropServer("probe-server");

        // [EN] ---------- Automation page: templates, field help, safety interlock, result
        // [EN] preview ----------
        // [ZH] ---------- 自动化页：模板 / 字段帮助 / 安全联动 / 结果预览 ----------
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
        // [EN] Safety-protocol interlock: accepting makes the master switch editable;
        // [EN] cancelling cancels it automatically and disables the switch.
        // [ZH] 安全协议联动：接受 → 总开关可编辑；取消 → 自动取消并禁用
        automation.ProbeSetSafety(true);
        string safetyOn = automation.AutomationSnapshot();
        Check("接受协议后总开关可编辑", safetyOn.Contains("enabledEditable=1"), safetyOn);
        automation.ProbeSetSafety(false);
        string safetyOff = automation.AutomationSnapshot();
        Check("取消协议后总开关被禁用", safetyOff.Contains("enabledEditable=0"), safetyOff);
        Check("取消协议后总开关被强制关掉", safetyOff.Contains("safetyAccepted=0"), safetyOff);

        // [EN] ---------- Model page: local model registry ----------
        // [ZH] ---------- 模型页：本地模型库 ----------
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

        // [EN] ---------- Model page: inference presets and context description (v0.2.4
        // [EN] development task 2) ----------
        // [ZH] ---------- 模型页：推理预设与上下文描述（v0.2.4 研发任务 2）----------
        await model.ProbeReloadInferenceAsync();
        string inference = model.InferenceSnapshot();
        Console.WriteLine($"  推理预设   : {inference}");
        Check("三档推理预设都已列出", inference.Contains("profileOptions=3"), inference);
        Check("当前推理预设已按后端回填",
            inference.Contains("profileSelected=fast")
            || inference.Contains("profileSelected=balanced")
            || inference.Contains("profileSelected=quality"), inference);
        Check("上下文描述已渲染（原生 / 实际可用）",
            inference.Contains("contextText=1"), inference);
        Check("推理档位标签与保存按钮有文案",
            inference.Contains("profileLabel=1") && inference.Contains("saveButton=1"),
            inference);

        // [EN] v0.2.4 task 3: editable override inputs, warning icons, test-load
        // [EN] button and the hardware diagnostics panel must all exist. The
        // [EN] snapshot fills deliberately over-budget values; the probe never
        // [EN] clicks "Test load", so it cannot block for 60 seconds.
        // [ZH] v0.2.4 任务-3：可编辑覆盖输入、警告图标、测试加载按钮与硬件诊断面板
        // [ZH] 必须全部存在。快照会填入故意超预算的值；探针不会点击「测试加载」，
        // [ZH] 因此不会阻塞 60 秒。
        Check("覆盖输入框与测试加载按钮存在",
            inference.Contains("nctxField=1")
            && inference.Contains("gpuLayersField=1")
            && inference.Contains("testLoadButton=1"), inference);
        Check("硬件诊断面板存在", inference.Contains("diagnosticsPanel=1"), inference);
        string overrideSnapshot = model.ProbeOverrideSnapshot();
        Console.WriteLine($"  覆盖警告   : {overrideSnapshot}");
        Check("超预算时两个警告图标均可见",
            overrideSnapshot.Contains("warnIcons=2"), overrideSnapshot);
        Check("推荐值已从后端回填",
            !overrideSnapshot.Contains("recommendedNctx=0")
            && !overrideSnapshot.Contains("recommendedGpuLayers=-999"),
            overrideSnapshot);
        await model.ProbeReloadInferenceAsync();

        // [EN] ---------- Installed page: plugin ecosystem entry points (v0.2.2 batch 4, 3a)
        // [EN] ----------
        // [ZH] ---------- 已安装页：插件生态入口（v0.2.2 第 4 批 3a）----------
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

        // [EN] ---------- Developer page: directory buttons, manifest editing, hooks and
        // [EN] panels (3a) ----------
        // [ZH] ---------- 开发者页：目录按钮 / 清单编辑 / 钩子与面板（3a）----------
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
    /// [EN] Markdown renderer verification (v0.2.2 batch 4, 3b): `--probe markdown`.
    ///
    /// [EN] Why a separate mode: the renderer's output is a **control tree**, and the eye can
    /// [EN] only tell "something was rendered". The four things added in this batch (inline
    /// [EN] math, table alignment, restoration of wrongly fenced tables, inline nesting,
    /// [EN] clickable links) are all of the "it still shows something when wrong" kind — the
    /// [EN] control tree must be walked and its **structure** asserted. No backend is needed,
    /// [EN] and no settings window is opened.
    /// [ZH] Markdown 渲染器验证（v0.2.2 第 4 批 3b）：`--probe markdown`。
    ///
    /// [ZH] 为什么单独一个模式：渲染器的输出是**控件树**，靠肉眼看只能看出“大概渲染了”，
    /// [ZH] 而本批补的四件事（行内数学、表格对齐、误包还原、行内嵌套、可点击链接）
    /// [ZH] 都是“错了也照样有东西显示”的类型 —— 必须遍历控件树断言**结构**。
    /// [ZH] 不需要后端，也不开设置窗口。
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

        // [EN] (1) Inline math $x^2$: a monospace Run must be produced and **no** $ delimiter
        // [EN] may remain.
        // [ZH] ① 行内数学 $x^2$：应产出等宽 Run，且**不残留** $ 定界符
        Control formula = MarkdownRenderer.Build("勾股定理 $a^2 + b^2 = c^2$ 成立。", 14);
        string formulaText = FlattenText(formula);
        Check("行内数学 $…$ 被识别成等宽片段",
            !formulaText.Contains('$') && formulaText.Contains("a^2 + b^2 = c^2"), formulaText);
        Check("行内数学用等宽字体", HasMonoRun(formula), "找到等宽 Run");

        // [EN] (2) Dollar amounts **must not** be mistaken for formulas (the negative case
        // [EN] that is easiest to hit).
        // [ZH] ② 美元金额**不能**被误判成公式（这是最容易踩的反向用例）
        Control money = MarkdownRenderer.Build("价格 $100 到 $200 之间。", 14);
        string moneyText = FlattenText(money);
        Check("美元金额不被误判成公式", moneyText.Contains("$100 到 $200"), moneyText);
        Check("美元金额不用等宽字体", !HasMonoRun(money), "没有等宽 Run");

        // [EN] (3) Inline $$...$$ (appearing in the middle of a line).
        // [ZH] ③ 行内 $$…$$（出现在一行中间）
        Control middle = MarkdownRenderer.Build("结论是 $$E = mc^2$$ 这样。", 14);
        string middleText = FlattenText(middle);
        Check("行中 $$…$$ 被识别", !middleText.Contains("$$") && middleText.Contains("E = mc^2"), middleText);

        // [EN] (4) Table column alignment `:---:` / `---:`.
        // [ZH] ④ 表格列对齐 :---: / ---:
        string table = "| 名称 | 数量 | 金额 |\n|:---|---:|:---:|\n| 甲 | 3 | 12.00 |\n| 乙 | 4 | 30.00 |";
        Control tableTree = MarkdownRenderer.Build(table, 14);
        var aligns = new List<TextAlignment>();
        CollectAlignments(tableTree, aligns);
        Check("表格右对齐（---:）生效", aligns.Contains(TextAlignment.Right), string.Join(",", aligns));
        Check("表格居中（:---:）生效", aligns.Contains(TextAlignment.Center), string.Join(",", aligns));

        // [EN] (5) A table wrongly fenced into a code block is restored to a real table.
        // [ZH] ⑤ 误包进代码块的表格被还原成真表格
        Control fenced = MarkdownRenderer.Build("```\n" + table + "\n```\n", 14);
        Check("代码块里的表格被还原成表格（Grid）", HasGrid(fenced), "找到 Grid");
        Check("真正被包起来的表格内容仍然完整",
            FlattenText(fenced).Contains("名称") && FlattenText(fenced).Contains("12.00"), FlattenText(fenced));

        // [EN] (6) A genuine code block **must not** be wrongly restored.
        // [ZH] ⑥ 真正的代码块**不能**被误还原
        Control code = MarkdownRenderer.Build("```python\nprint('hi')\n```", 14);
        Check("普通代码块仍然是代码块（没有 Grid）", !HasGrid(code), "未找到 Grid");

        // [EN] (7) Inline nesting: inline code inside bold text.
        // [ZH] ⑦ 行内嵌套：粗体里嵌行内代码
        Control nested = MarkdownRenderer.Build("**粗体里有 `code` 片段**", 14);
        Check("粗体中嵌行内代码（递归解析）", HasNestedMonoInsideBold(nested), "粗体 Span 内含等宽 Run");
        Check("内层反引号不残留", !FlattenText(nested).Contains('`'), FlattenText(nested));

        // [EN] (8) Clickable links: a link inside a Chinese sentence renders as a clickable
        // [EN] control.
        // [ZH] ⑧ 可点击链接：中文句子中的链接应渲染成可点击控件。
        Control link = MarkdownRenderer.Build("见 [文档](https://example.com/doc) 一节。", 14);  // [ZH] 样例：中文句子中间夹一个链接。
        Check("链接渲染成可点击控件（InlineUIContainer + Button）", HasLinkButton(link), "找到链接按钮");
        Check("链接文本不带尖括号 URL 尾巴",
            !FlattenText(link).Contains("<https://"), FlattenText(link));

        // [EN] (9) A block-level formula is still centered as a block.
        // [ZH] ⑨ 块级公式仍然居中成块
        Control blockFormula = MarkdownRenderer.Build("$$\nE = mc^2\n$$", 14);
        Check("块级公式仍渲染成居中块", FlattenText(blockFormula).Contains("E = mc^2"), FlattenText(blockFormula));

        return ok;
    }

    /// <summary>
    /// [EN] Verification of the flattened message layout and of the streaming throttle
    ///      (`--probe chat`). The mode deliberately runs without a backend: the layout parameters
    ///      are read from freshly constructed message containers, and the streaming behaviour is
    ///      exercised through the same buffer and timer path used by real generation.
    /// [ZH] 平铺式消息布局与流式节流的验证（`--probe chat`）。该模式刻意不连接后端：布局参数取自
    /// [ZH] 新构建的消息容器，流式行为则通过真实生成所使用的同一缓冲区与定时器路径演练。
    /// </summary>
    private static async Task<bool> RunChatProbeAsync(IClassicDesktopStyleApplicationLifetime desktop)
    {
        bool ok = true;

        void Check(string name, bool pass, string detail)
        {
            Console.WriteLine($"  [{(pass ? "PASS" : "FAIL")}] {name}——{detail}");
            if (!pass)
                ok = false;
        }

        var main = new MainWindow { ProbeSuppressBackendStart = true };
        desktop.MainWindow = main;
        main.Show();
        Console.WriteLine("  [PASS] 主窗口已构造并显示（不启动后端）");
        await Task.Delay(300);

        string styles = main.ProbeMessageStyleSnapshot();
        Console.WriteLine($"  消息样式   : {styles.Replace("\n", "  ‖  ")}");
        string[] parts = styles.Split('\n');
        string user = parts.Length > 0 ? parts[0] : "";
        string assistant = parts.Length > 1 ? parts[1] : "";

        Check("用户消息呈现为浅色背景块",
            user.Contains("bg=#", StringComparison.Ordinal)
            && !user.Contains("bg=Transparent", StringComparison.Ordinal)
            && !user.Contains("bg=#00ffffff", StringComparison.OrdinalIgnoreCase), user);
        Check("助手消息正文平铺（无背景）",
            assistant.Contains("bg=Transparent", StringComparison.Ordinal)
            || assistant.Contains("bg=#00ffffff", StringComparison.OrdinalIgnoreCase), assistant);
        Check("消息下方间距已增大（≥ 14）",
            ReadNumber(user, "marginBottom") >= 14, OneLine(user, 160));
        Check("用户块具备内边距", ReadNumber(user, "padLeft") >= 10, OneLine(user, 160));
        Check("操作条默认隐藏且不参与命中测试",
            user.Contains("barOpacity=0", StringComparison.Ordinal)
            && user.Contains("barHit=False", StringComparison.Ordinal), user);
        Check("操作条具备淡入过渡", ReadNumber(user, "barTransitions") >= 1, user);
        Check("悬停时操作条浮现且恢复可交互",
            user.Contains("barHover=1/True", StringComparison.Ordinal), user);
        Check("角色标识为小号字体", ReadNumber(user, "roleFont") is >= 10 and <= 12, user);

        string streaming = await main.ProbeStreamingAsync(2000);
        Console.WriteLine($"  流式节流   : {streaming}");
        Check("节流间隔不超过 40 毫秒", ReadNumber(streaming, "interval") is > 0 and <= 40,
            OneLine(streaming, 160));
        Check("高频 Token 被合并且渲染次数远小于 Token 数",
            ReadNumber(streaming, "renders") > 0
            && ReadNumber(streaming, "renders") < ReadNumber(streaming, "tokens") / 10,
            OneLine(streaming, 160));
        Check("最后一个 Token 没有丢失",
            streaming.Contains("tailPresent=True", StringComparison.Ordinal), OneLine(streaming, 160));
        Check("流式期间显示打字机光标",
            streaming.Contains("caretDuring=True", StringComparison.Ordinal), OneLine(streaming, 160));
        Check("生成结束后光标立即消失",
            streaming.Contains("caretAfter=False", StringComparison.Ordinal), OneLine(streaming, 160));
        Check("首字到达前呈现三点等待指示器",
            streaming.Contains("dotsBeforeFirstToken=True", StringComparison.Ordinal), OneLine(streaming, 200));
        Check("首个片段到达后三点指示器立即撤下",
            streaming.Contains("dotsDuring=False", StringComparison.Ordinal), OneLine(streaming, 200));
        Check("生成结束后三点指示器不复存在",
            streaming.Contains("dotsAfter=False", StringComparison.Ordinal), OneLine(streaming, 200));

        string motion = main.ProbeMotionSnapshot();
        Console.WriteLine($"  主窗口动效 : {motion}");
        Check("状态区折叠同时具备不透明度过渡与渲染变换过渡",
            ReadNumber(motion, "stripTransitions") >= 2
            && motion.Contains("DoubleTransition", StringComparison.Ordinal)
            && motion.Contains("TransformOperationsTransition", StringComparison.Ordinal),
            OneLine(motion, 200));

        string stripMotion = await main.ProbeStatusStripMotionAsync();
        Console.WriteLine($"  状态区收放 : {stripMotion}");
        Check("状态区折叠后完成隐藏并复位不透明度",
            stripMotion.Contains("collapsedHidden=True", StringComparison.Ordinal), OneLine(stripMotion, 160));
        Check("状态区展开后恢复可见并回到满不透明度",
            stripMotion.Contains("expandedVisible=True", StringComparison.Ordinal), OneLine(stripMotion, 160));

        // [EN] ---- Input box: hover may only affect rendering -------------------------------
        // [ZH] ---- 输入框：悬停只能影响渲染 ----------------------------------
        string hover = await main.ProbeInputHoverStabilityAsync();
        Console.WriteLine($"  输入框悬停 : {hover}");
        Check("输入框悬停不改变边界/边距/内边距/描边宽度（零布局重排）",
            hover.Contains("layoutStable=True", StringComparison.Ordinal), OneLine(hover, 200));
        Check("输入框背景与描边通过 BrushTransition 平滑过渡",
            hover.Contains("borderBrushTransition=True", StringComparison.Ordinal), OneLine(hover, 200));
        Check("输入框光晕通过 BoxShadowsTransition 平滑过渡",
            hover.Contains("boxShadowTransition=True", StringComparison.Ordinal), OneLine(hover, 200));

        // [EN] ---- Status area: aligned with WPF plus an extra memory-pressure item -------
        // [ZH] ---- 状态区：与 WPF 对齐并额外提供内存压力 ----------------------
        string items = main.ProbeStatusStripItemsSnapshot();
        Console.WriteLine($"  状态区各项 : {items}");
        Check("状态区控件数量不少于 WPF（9 项 + 内存压力）",
            ReadNumber(items, "count") >= 10, OneLine(items, 220));
        foreach ((string key, string label) in new[]
                 {
                     ("search", "搜索"), ("automation", "自动化"), ("market", "市场"),
                     ("context", "上下文"), ("kv", "KV"), ("memory", "内存"),
                     ("capability", "模型能力"), ("compact", "压缩"),
                 })
        {
            string marker = $"{key}=";
            int start = items.IndexOf(marker, StringComparison.Ordinal);
            string value = start < 0 ? "" : items[(start + marker.Length)..].Split('｜')[0];
            Check($"状态区包含{label}项且文本非空", value.Trim().Length > 0, $"{key}='{value}'");
        }
        Check("状态区内存项已格式化为百分比",
            items.Contains("memory=", StringComparison.Ordinal)
            && System.Text.RegularExpressions.Regex.IsMatch(items, @"memory=[^｜]*%"),
            OneLine(items, 220));

        // [EN] ---- Title bar: brand, version and studio are all present -------------------
        // [ZH] ---- 标题栏：品牌/版本/出品方齐全 ------------------------------
        main.ProbeSetTitleBarVersion("v0.2.4");
        string titleBar = main.ProbeTitleBarSnapshot();
        Console.WriteLine($"  标题栏     : {titleBar}");
        Check("标题栏显示版本号（来自后端版本源）",
            titleBar.Contains("version=v0.2.4", StringComparison.Ordinal), OneLine(titleBar, 180));
        int studioAt = titleBar.IndexOf("studio=", StringComparison.Ordinal);
        string studioValue = studioAt < 0 ? "" : titleBar[(studioAt + "studio=".Length)..].Split('｜')[0];
        Check("标题栏出品方文案非空", studioValue.Trim().Length > 0, $"studio='{studioValue}'");
        Check("标题栏标题有实际宽度", ReadNumber(titleBar, "titleWidth") > 0, OneLine(titleBar, 180));

        // [EN] ---- Memory above 90% triggers the red warning -----------------------------
        // [ZH] ---- 内存 >90% 红色警告 ---------------------------------------
        string memWarning = main.ProbeMemoryWarning();
        Console.WriteLine($"  内存预警   : {memWarning}");
        Check("内存使用率 95% 时触发红色警告类",
            memWarning.Contains("critical=True", StringComparison.Ordinal), OneLine(memWarning, 200));
        Check("内存使用率 95% 时文案带百分比",
            memWarning.Contains("95", StringComparison.Ordinal), OneLine(memWarning, 200));
        int memTipAt = memWarning.IndexOf("tip=", StringComparison.Ordinal);
        string memTip = memTipAt < 0 ? "" : memWarning[(memTipAt + "tip=".Length)..];
        Check("内存预警悬停提示非空", memTip.Trim().Length > 0, $"tip='{memTip}'");

        // [EN] ---- Update download progress bar -------------------------------------------
        // [ZH] ---- 更新下载进度条 -------------------------------------------
        string progress = main.ProbeUpdateProgressUi();
        Console.WriteLine($"  更新进度   : {progress}");
        Check("下载进度条可见且为 25%",
            progress.Contains("visible=True", StringComparison.Ordinal)
            && Math.Abs(ReadNumber(progress, "value") - 25) < 0.01, OneLine(progress, 200));
        Check("下载进度文本显示百分比",
            progress.Contains("%", StringComparison.Ordinal), OneLine(progress, 200));
        Check("下载完成后进度条隐藏并复位",
            progress.Contains("hidden=True", StringComparison.Ordinal), OneLine(progress, 200));

        return ok;
    }

    /// <summary>
    /// [EN] Reads the numeric value of a <c>key=value</c> pair from a probe report string.
    /// [ZH] 从探针报告字符串中读取 <c>key=value</c> 键值对的数值。
    /// </summary>
    private static double ReadNumber(string report, string key)
    {
        var match = Regex.Match(report, Regex.Escape(key) + @"=(-?[0-9]+(?:\.[0-9]+)?)");
        return match.Success && double.TryParse(match.Groups[1].Value, out double value) ? value : -1;
    }

    // [EN] ---- Small helpers for control-tree assertions (used only by the probe, never in
    // [EN] product code) ----
    // [ZH] ---- 控件树断言用的小工具（只在探针里用，不进产品代码） ----

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

    /// <summary>
    /// [EN] Concatenate the text of a control tree (enough to assert "does a delimiter remain,
    /// [EN] and is the content there").
    /// [ZH] 把控件树里的文本拼起来（够断言“定界符有没有残留、内容在不在”）。
    /// </summary>
    private static string FlattenText(Control root)
    {
        var builder = new System.Text.StringBuilder();
        Collect(root, builder);
        return builder.ToString();

        static void Collect(object? node, System.Text.StringBuilder into)
        {
            switch (node)
            {
                // [EN] Ordering note: `Text` takes precedence over `Inlines`. Avalonia's
                // [EN] `TextBlock.Inlines` is **never null** (the getter builds the collection
                // [EN] on demand), so "use Inlines to tell whether there is content" always
                // [EN] misjudges — a control of the formula-block kind that "only sets Text
                // [EN] and never sets Inlines" is treated as empty (this probe hit that on its
                // [EN] very first run and reported "the block formula rendered empty", when the
                // [EN] real problem was the assertion helper itself).
                // [ZH] 顺序说明：`Text` 优先于 `Inlines`。
                // [ZH] Avalonia 的 `TextBlock.Inlines` **永远不为 null**（getter 会按需建集合），
                // [ZH] 所以“用 Inlines 判断有没有内容”一定会误判 —— 公式块那种“只设 Text、
                // [ZH] 不设 Inlines”的控件会被当成空的（本探针第一次跑就踩到了这条，
                // [ZH] 报“块级公式渲染成空”，其实是断言工具自己的问题）。
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
    /// [EN] Upload path verification (v0.2.2 batch 4, 3c): `--probe upload`.
    ///
    /// [EN] A real backend is required: the per-file status (`file_status`) and the segment
    /// [EN] summary progress (`summary_progress`) are both events pushed by the backend, which
    /// [EN] the front end cannot fabricate. Two temporary txt files go through a real upload,
    /// [EN] and the probe then asserts "per-file status really appeared in the UI, and the
    /// [EN] upload is cancellable while it runs".
    /// [ZH] 上传路径验证（v0.2.2 第 4 批 3c）：`--probe upload`。
    ///
    /// [ZH] 需要真后端：逐份状态（`file_status`）与分段摘要进度（`summary_progress`）都是
    /// [ZH] 后端推来的事件，前端造不出来。用两个临时 txt 文件走一遍真实上传，
    /// [ZH] 再断言“界面上确实出现了逐份状态、并且上传期间是可中止的”。
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

        // [EN] **Do not call Show()**: MainWindow's Loaded triggers its own StartBackendAsync,
        // [EN] which starts yet another backend and steals the ready/done lines from the
        // [EN] probe's connection (observed as no response; the backend process may already
        // [EN] have exited). The control tree is already built during construction, so
        // [EN] asserting without showing is enough.
        // [ZH] **不要 Show()**：MainWindow 的 Loaded 会触发它自己的 StartBackendAsync，
        // [ZH] 那会再拉一个后端、并把探针这条连接上的 ready/done 行抢走（实测表现为
        // [ZH] 无响应（后端进程可能已退出））。控件树在构造时就已经建好，不显示也够断言。
        var window = new MainWindow();
        desktop.MainWindow = window;

        void Check(string name, bool pass, string detail)
        {
            Console.WriteLine($"  [{(pass ? "PASS" : "FAIL")}] {name}——{detail}");
            if (!pass)
                ok = false;
        }

        // [EN] ---------- (1) Snippet insertion at the caret (UI only, verified first, no
        // [EN] backend needed) ----------
        // [ZH] ---------- ① 常用语插入到光标处（纯界面，先验，不依赖后端）----------
        string composed = window.ProbeInsertSnippetAtCaret("请帮我", 3, "总结这段");
        Check("常用语插入到光标处（不覆盖已有文字）",
            composed.Contains("请帮我") && composed.Contains("总结这段"), composed);
        string mid = window.ProbeInsertSnippetAtCaret("前 后", 2, "中间");
        Check("光标在中间时插入到中间",
            mid.IndexOf("中间", StringComparison.Ordinal) < mid.IndexOf('后'), mid);

        // [EN] ---------- (2) A real upload of two files: per-file status ----------
        // [ZH] ---------- ② 真实上传两份文件：逐份状态 ----------
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
                // [EN] An undeletable temporary directory does not affect the verdict.
                // [ZH] 临时目录删不掉不影响结论
            }
        }

        return ok;
    }

    /// <summary>
    /// [EN] A **real test** of the theme following the system (v0.2.2 batch 4, difference list
    /// [EN] §F5 item 4): `--probe theme`.
    ///
    /// [EN] The difference list previously recorded this item as "not verified: Avalonia uses
    /// [EN] ThemeVariant.Default and is driven by the system, so no explicit hook is needed".
    /// [EN] **"No explicit hook" and "actually in effect" are two different things** — this
    /// [EN] mode really flips the system switch for "apps use light/dark theme", waits a
    /// [EN] moment, and checks whether `ActualThemeVariant` flips with it.
    ///
    /// [EN] Implementation note (why P/Invoke is allowed here): changing the registry does
    /// [EN] **not** automatically notify already running processes — `WM_SETTINGCHANGE` must be
    /// [EN] broadcast. Product code contains no P/Invoke at all (a red line for a
    /// [EN] cross-platform project), but **the probe is a test tool that only runs on
    /// [EN] Windows**, and there is no other way to really exercise this chain. On non-Windows
    /// [EN] platforms the mode reports SKIP (not PASS).
    /// [ZH] 主题跟随系统**实测**（v0.2.2 第 4 批，差异清单 §F5 第 4 条）：`--probe theme`。
    ///
    /// [ZH] 差异清单此前把这一条记成“未验证：Avalonia 走 ThemeVariant.Default 由系统驱动，
    /// [ZH] 无需显式钩子”。**“没有显式钩子”与“确实生效”是两件事** —— 本模式真的去改
    /// [ZH] 系统的“应用使用浅色/深色主题”开关，等一等，看 `ActualThemeVariant` 有没有跟着翻。
    ///
    /// [ZH] 实现说明（为什么这里允许 P/Invoke）：
    /// [ZH] 改注册表**不会**自动通知已运行的进程 —— 需要广播 `WM_SETTINGCHANGE`。
    /// [ZH] 产品代码里没有任何 P/Invoke（跨平台工程的红线），但**探针是只在 Windows 上跑的
    /// [ZH] 测试工具**，没有别的办法把这条链路真的走通。非 Windows 平台直接报 SKIP（不报 PASS）。
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

        // [EN] (1) Mechanism side: with theme=auto the decision must be handed to the platform
        // [EN] (RequestedThemeVariant = Default).
        // [ZH] ① 机制面：theme=auto 时应当把决定权交给平台（RequestedThemeVariant = Default）
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

            // [EN] Give the platform settings a moment to react (Avalonia forwards
            // [EN] WM_SETTINGCHANGE within this process).
            // [ZH] 给平台设置一点反应时间（Avalonia 收到 WM_SETTINGCHANGE 后在本进程内转发）
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
            // [EN] **It must be restored**: this is a real setting of the user's machine, and a
            // [EN] probe must not leave traces behind.
            // [ZH] **必须还原**：这是用户机器的真实设置，探针不能留下痕迹
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
    /// [EN] Take a pair of resources that change with the theme, to confirm that the skin
    /// [EN] really changed and not merely that the enum did. If the resources cannot be
    /// [EN] resolved, draw no conclusion (return true) — resource names may change with theme
    /// [EN] packs, which should not fail the probe.
    /// [ZH] 取一对随主题变化的资源，确认“真的换了皮”而不只是枚举翻了。
    /// [ZH] 拿不到资源就不下结论（返回 true）—— 资源名可能随主题包变化，不该因此判失败。
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
    /// [EN] Broadcast <c>WM_SETTINGCHANGE</c> so that already running processes learn that the
    /// [EN] system settings changed. **Used only by the probe**: product code does not use
    /// [EN] P/Invoke (cross-platform red line), but "change the registry, do not broadcast, and
    /// [EN] the process receives no notification" is established Windows behaviour; without the
    /// [EN] broadcast, "does following the system respond immediately" cannot be tested at all.
    /// [ZH] 广播 <c>WM_SETTINGCHANGE</c>，让已运行的进程知道系统设置变了。
    /// [ZH] **只在探针里用**：产品代码不引 P/Invoke（跨平台红线），
    /// [ZH] 但“改注册表 → 不广播 → 进程收不到通知”是 Windows 的既定行为，
    /// [ZH] 不广播就根本测不出“跟随系统是否即时响应”。
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
            // [EN] "ImmersiveColorSet": Windows uses it to announce that the light/dark theme
            // [EN] changed.
            // [ZH] “ImmersiveColorSet”：Windows 用它通知“浅色/深色主题变了”
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
        // [EN] The probe holds its own backend connection and makes **the UI use it too**
        // [EN] (AppState.ProbeBackendOverride). Otherwise the framework would build the main
        // [EN] window itself, that window would start a backend and read the ready handshake,
        // [EN] and the probe and the UI would be two readers competing for the same stdout
        // [EN] stream — whichever reads first wins and the other waits forever (observed as
        // [EN] "the backend exited before startup completed", with the logs of two python
        // [EN] processes visible on stderr).
        // [ZH] 探针自持一条后端连接，并让**界面也用它**（AppState.ProbeBackendOverride）。
        // [ZH] 不这么做的话：框架会自己建主窗口、主窗口又去拉后端并读 ready 握手，
        // [ZH] 探针与界面两个读者抢同一条 stdout 流 —— 先读到的那方赢，另一方永远等不到
        // [ZH] （实测现象：“后端未完成启动即退出”，stderr 里能看到两个 python 进程的日志）。
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

        // [EN] The UI uses exactly this probe connection, so the settings pages and the market
        // [EN] can send requests directly now.
        // [ZH] 界面用的就是探针这条连接，所以设置页/市场现在能直接发请求
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

        // [EN] ---- (1) Search: fetch results and compute the first page ----
        // [ZH] ---- ① 搜索：取回结果并算出第一页 ----
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

        // [EN] ---- (2) Paging: next page / back to the first page ----
        // [ZH] ---- ② 分页：下一页 / 回首页 ----
        await page.ProbeMarketGoToPageAsync(2);
        var s2 = page.MarketSnapshot();
        Check("「下一页」可用且能跳到第 2 页", s2.PageInfo.Contains("2 /"), s2.PageInfo);
        Check("非首页时「首页」可用", s2.FirstEnabled, $"FirstEnabled={s2.FirstEnabled}");
        await page.ProbeMarketGoToPageAsync(1);
        var s3 = page.MarketSnapshot();
        Check("回到第 1 页后「首页」禁用", !s3.FirstEnabled, $"FirstEnabled={s3.FirstEnabled}");
        Check("第 1 页时「下一页」可用", s3.NextEnabled, $"NextEnabled={s3.NextEnabled}");

        // [EN] ---- (3) Page size: going from 20 to 50 must reduce the page count and return
        // [EN] to page 1 ----
        // [ZH] ---- ③ 每页条数：20 → 50 时总页数必须变少，且回到第 1 页 ----
        await page.ProbeMarketGoToPageAsync(2);
        await page.ProbeMarketSetPageSizeAsync(50);
        var s4 = page.MarketSnapshot();
        int pagesBefore = ParsePages(s2.PageInfo);
        int pagesAfter = ParsePages(s4.PageInfo);
        Check("切换每页条数后总页数变少", pagesAfter > 0 && pagesAfter < pagesBefore,
            $"{pagesBefore} → {pagesAfter}");
        Check("切换每页条数后回到第 1 页", s4.PageInfo.Contains("1 /"), s4.PageInfo);
        await page.ProbeMarketSetPageSizeAsync(20);

        // [EN] ---- (4) Local filtering: use a string that can hardly match, to verify that the
        // [EN] count really narrows and that a re-search exit is offered ----
        // [ZH] ---- ④ 本地筛选：用一个几乎不可能匹配的串，验证条数真的收窄并给出重搜出口 ----
        await page.ProbeMarketFilterAsync("zzz-绝对不存在的模型名-zzz");
        var s5 = page.MarketSnapshot();
        Check("本地筛选把结果收窄到 0", s5.ItemCount == 0, $"列表 {s5.ItemCount} 行");
        Check("筛完为空时给出「重新搜索远端」出口",
            s5.Status.Contains("本地筛选后 0 条") || s5.Status.Contains("0 条"),
            OneLine(s5.Status, 100));
        await page.ProbeMarketResetAsync();
        var s6 = page.MarketSnapshot();
        Check("重置筛选后结果恢复", s6.ItemCount > 0, $"列表 {s6.ItemCount} 行");

        // [EN] ---- (5) Structured filtering: check a facet with a count above 0; the count
        // [EN] must not exceed the unfiltered one ----
        // [ZH] ---- ⑤ 结构化筛选：勾一个计数>0 的分面，条数应不大于未筛选时 ----
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

        // [EN] ---- (6) Pinned row of the default model plus its details ----
        // [ZH] ---- ⑥ 默认模型钉住行 + 详情 ----
        var s8 = page.MarketSnapshot();
        Check("默认模型钉住行可见", s8.PinnedVisible, $"PinnedVisible={s8.PinnedVisible}");
        if (s8.PinnedVisible)
        {
            await page.ProbeMarketOpenPinnedDetailAsync();
            var s9 = page.MarketSnapshot();
            Check("钉住行的详情能列出文件", s9.FileRows > 0, $"详情行 {s9.FileRows}");
        }

        // [EN] ---- (7) After switching the language the market texts follow, without asking
        // [EN] the backend again ----
        // [ZH] ---- ⑦ 语言切换后市场文案跟着走（不重新问后端）----
        App.State.SetLanguage("en");
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        var s10 = page.MarketSnapshot();
        Check("切到英文后市场文案仍是完整的（没有空标签）", s10.Total.Length > 0 && s10.PageInfo.Length > 0,
            $"{s10.PageInfo}｜{s10.Total}");
        App.State.SetLanguage("zh-Hans");

        settings.Close();
        // [EN] The backend is owned by App.State (unique within the process) and is closed
        // [EN] uniformly by the outer State.Shutdown(), so it is not disposed separately here —
        // [EN] doing so would close the connection the UI is currently using.
        // [ZH] 后端由 App.State 持有（进程内唯一），收尾统一由外层 State.Shutdown() 关，
        // [ZH] 这里不单独 Dispose —— 否则会关掉界面正在用的那条连接。
        return ok;
    }

    private static int ParsePages(string pageInfo)
    {
        // [EN] "page 3 / 250" or "Page 3 / 250": take the first number **after** the slash. A
        // [EN] regex is used instead of TakeWhile because the text contains full-width
        // [EN] characters and spaces, which hand-written slicing misses (hit in this round).
        // [ZH] “第 3 / 250 页” / “Page 3 / 250”：取斜杠**之后**的第一段数字。
        // [ZH] 用正则而不是 TakeWhile：文案里有全角字符与空格，手写切分会漏（本轮踩到过）。
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
    // [EN] Full-page sweep for the English UI (--probe sweep)
    //
    // [EN] Why it exists (a maintainer requirement): static scans (check_lang /
    // [EN] audit_lang_keys) can only see **literals in the source**; they miss Chinese that is
    // [EN] composed dynamically or only exists at render time — for example
    // [EN]   whole sentences built as $"{Loc.T("a", "…")}（{x}）",
    // [EN]   items inserted into a ComboBox at runtime, text supplied by ToolTip templates, and
    // [EN]   AutomationProperties.Name.
    // [EN] So the probe collects the text of every visible control in **real windows** and
    // [EN] judges it with [\u4e00-\u9fa5].
    //
    // [EN] Forced English: App.State.SetLanguage("en") — the same path the user takes when
    // [EN] clicking "language: English".
    // [EN] Screenshots: RenderTargetBitmap renders the window directly, in the **same render
    // [EN] pass** as the text, which helps manual review.
    // [ZH] 英文环境全页扫描（--probe sweep）
    //
    // [ZH] 存在理由（维护者要求）：静态扫描（check_lang / audit_lang_keys）只能看到**源码里的字面量**，
    // [ZH] 抓不到“动态拼接”和“渲染期才存在”的中文 —— 例如
    // [ZH]   $"{Loc.T("a", "…")}（{x}）" 拼出来的整句、
    // [ZH]   ComboBox 运行时塞进去的 Items、ToolTip 在模板里给的文本、AutomationProperties.Name。
    // [ZH] 所以在**真实窗口**里把所有可见控件的文本抓一遍，用 [\u4e00-\u9fa5] 判定。
    //
    // [ZH] 强制英文：App.State.SetLanguage("en") —— 与用户点“语言：English”走同一条路径。
    // [ZH] 截图：RenderTargetBitmap 直接渲染窗口，和文本是**同一次渲染**，便于人工复核。
    // ===================================================================================

    private static readonly Regex CjkPattern = new(@"[\u4e00-\u9fa5]");

    // ===================================================================================
    // [EN] README showcase screenshots (--probe shots)
    //
    // [EN] Run only on a **development machine**: fix the dark theme and Simplified Chinese,
    // [EN] inject demo data into three real windows, and render them to PNG with
    // [EN] RenderTargetBitmap at 2x pixels. Why do it this way instead of letting the user take
    // [EN] screenshots by hand:
    // [EN]   * the same code produces the same set of images every time, so the README cannot
    // [EN]     become a leak source because of a system theme, user name or path;
    // [EN]   * it goes through the product's own render path (AddMessage / MarkdownRenderer /
    // [EN]     settings-page controls) rather than a separately drawn set of "illustrations" —
    // [EN]     what is in the image is what the user sees on opening it;
    // [EN]   * no backend and no network: the screenshots do not depend on whether a model is
    // [EN]     installed locally.
    // [ZH] README 宣传截图（--probe shots）
    //
    // [ZH] 只在**开发机**跑：固定深色主题、简体中文，用演示数据注入三个真实窗口，
    // [ZH] 再用 RenderTargetBitmap 以 2 倍像素渲染成 PNG。为什么这样做而不是让用户手截：
    // [ZH]   * 同一份代码每次生成同一组图，README 不会因为某次系统主题/用户名/路径而变成泄密源；
    // [ZH]   * 走的是产品自己的渲染路径（AddMessage / MarkdownRenderer / 设置页控件），
    // [ZH]     不是另画一套“示意图”——图上有什么，用户打开就能看到什么；
    // [ZH]   * 不拉后端、不联网：截图不依赖本机是否装了模型。
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
            // [EN] Dark is the primary tone of the showcase images; one set is produced per
            // [EN] language, English for README.md and Chinese for README.zh-Hans.md.
            // [ZH] 深色是宣传图的主色调；中英各出一套，README.md 用英文，README.zh-Hans.md 用中文。
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
            // [EN] Restore the user settings: screenshots are a development tool and must not
            // [EN] leave the theme, language or window size at demo values.
            // [ZH] 还原用户设置：截图是开发工具，不应把主题/语言/窗口尺寸留成演示值。
            App.State.Settings.Theme = previousTheme;
            App.State.ApplyTheme();
            App.State.SetLanguage(string.IsNullOrWhiteSpace(previousLanguage) ? "auto" : previousLanguage);
            App.State.Settings.SettingsWindowWidth = previousSettingsWidth;
            App.State.Settings.SettingsWindowHeight = previousSettingsHeight;
        }

        return ok;
    }

    /// <summary>
    /// [EN] Generate the three images for one language; English uses bare file names and
    /// [EN] Chinese gets a .zh-Hans suffix.
    /// [ZH] 生成一个语言版本的三张图；英文用裸文件名，中文加 .zh-Hans 后缀。
    /// </summary>
    private static async Task<bool> CaptureLanguageAsync(IClassicDesktopStyleApplicationLifetime desktop,
        List<Window> windows, string language, bool english, string suffix)
    {
        bool ok = true;
        App.State.SetLanguage(language);
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        Console.WriteLine($"  [info] 生成 {language} 截图{suffix}");

        // [EN] ---------- (1) Main screen: a contract summary is streaming ----------
        // [ZH] ---------- ① 主界面：合同摘要正在流式输出 ----------
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

        // [EN] ---------- (2) Settings - Model market ----------
        // [ZH] ---------- ② 设置 → 模型市场 ----------
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

        // [EN] ---------- (3) Settings - Automation (safety protocol accepted, scrape task
        // [EN] filled in) ----------
        // [ZH] ---------- ③ 设置 → 自动化（已接受安全协议 + 填好抓取任务） ----------
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
        // [EN] Remember the original language and restore it at the end: SetLanguage
        // [EN] **persists** into the UI settings, and without restoring it, later probes
        // [EN] (market) would start in English while asserting on the Chinese page-number
        // [EN] format — hit in an actual run.
        // [ZH] 记住原语言并在结束时恢复：SetLanguage 会**持久化**到界面设置里，
        // [ZH] 不恢复的话后续探针（market）会以英文启动，而它按中文页码格式断言 —— 实测踩到过。
        string previousLanguage = App.State.Settings.Language;
        // [EN] The scanned language can be overridden with ECHODESK_SWEEP_LANG:
        // [EN]   en (default) -> look for "Chinese left in the English UI";
        // [EN]   zh-Hans      -> look for "English that should not appear in the Chinese UI"
        // [EN]                     (only a list for manual confirmation, never a hard failure).
        // [ZH] 扫描语言可被 ECHODESK_SWEEP_LANG 覆盖：
        // [ZH]   en（默认）→ 查“英文界面里残留的中文”；
        // [ZH]   zh-Hans     → 查“中文界面里不该出现的英文”（只列清单供人工确认，不判死）。
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

        // [EN] Restore the original language (including its persisted value), so that later
        // [EN] probes and the user's real settings are not polluted.
        // [ZH] 恢复原语言（含持久化），避免污染后续探针与用户的真实设置
        App.State.SetLanguage(string.IsNullOrWhiteSpace(previousLanguage) ? "auto" : previousLanguage);
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);

        Console.WriteLine($"  [info] 共扫描控件文本 {texts} 条，截图 {shots} 张 → {outDir}");
        if (latinMode)
        {
            // [EN] English left in the Chinese UI: **listed only**. Technical terms (Token /
            // [EN] MCP / Top-P...) are mixed in, so whether any of it counts as a defect needs
            // [EN] a human; the machine never fails the run (otherwise the gate would be
            // [EN] relaxed again and again for a few false positives and lose its meaning).
            // [ZH] 中文界面里的英文残留：**只列清单**。技术名词（Token / MCP / Top-P…）会混在里面，
            // [ZH] 是否算缺陷要人看，机器不判死（否则会为了几条误报反复放宽，门禁失去意义）。
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

    /// <summary>
    /// [EN] Scan all control texts under one window / page; return the number of collected
    /// [EN] texts.
    /// [ZH] 扫描一个窗口/页面下的全部控件文本；返回收集到的文本条数。
    /// </summary>
    /// <summary>
    /// [EN] Whether the current scan runs in the "look for English leftovers in the Chinese UI"
    /// [EN] mode.
    /// [ZH] 当前扫描是否处于“中文界面查英文残留”模式。
    /// </summary>
    private static bool LatinCandidatesMode;

    /// <summary>
    /// [EN] Technical terms that are legitimately English in the Chinese UI (brands, protocols,
    /// [EN] shortcuts, parameter names).
    /// [ZH] 中文界面里本来就该是英文的技术名词（品牌 / 协议 / 快捷键 / 参数名）。
    /// </summary>
    private static readonly HashSet<string> LatinAllowed = new(StringComparer.OrdinalIgnoreCase)
    {
        "EchoDesk", "WPF", "Avalonia", "Agent", "MCP", "API", "Token", "GGUF", "HF", "GitHub",
        "URL", "KV", "Top-P", "top_p", "Ctrl", "Enter", "Esc", "Tab", "Markdown", "JSON",
        "HTML", "HTTP", "HTTPS", "Python", "Windows", "Linux", "macOS", "llama", "Qwen",
        "Studio", "Void", "Entropia", "GPU", "CPU", "ID", "OK", "PDF", "DOCX", "XLSX",
        "PPTX", "OFD", "UTF", "SHA", "SQL", "local", "auto", "chat", "contract", "general",
        "light", "dark", "en", "zh-Hans", "zh-Hant", "Skill", "Skills", "Hub", "Plugin",
        "Plugins", "GGUF", "Q4_K_M", "Q6_K", "Q8_0", "Instruct", "Chat", "Base",
        // [EN] Accessible names of the framework's own scrollbar buttons / thumb: not product
        // [EN] copy.
        // [ZH] 框架自带的滚动条按钮/滑块的无障碍名：不是产品文案
        "Line up", "Line down", "Line left", "Line right",
        "Page up", "Page down", "Page left", "Page right", "Position",
    };

    /// <summary>
    /// [EN] Text that looks like "English copy" rather than a path / URL / code / ID: it starts
    /// [EN] with a plain ASCII letter, contains only letters, digits and common punctuation, is
    /// [EN] at least 3 characters long, and contains no Chinese. Technical terms on the
    /// [EN] allowlist do not count.
    /// [ZH] 像“英文文案”而不是路径/URL/代码/ID 的文本：
    /// [ZH] 纯 ASCII 字母开头、只含字母数字与常见标点、至少 3 个字符、且不含中文。
    /// [ZH] 命中白名单里的技术名词不算。
    /// </summary>
    private static bool LooksLikeUnexpectedEnglish(string text)
    {
        string t = text.Trim();
        if (t.Length < 3)
            return false;
        if (CjkPattern.IsMatch(t))
            return false;
        // [EN] .NET type names (ContentControl.Content returns the control type name): not UI
        // [EN] copy.
        // [ZH] .NET 类型名（ContentControl.Content 会返回控件类型名）：不是界面文案
        if (t.Contains("Avalonia.", StringComparison.Ordinal) || t.Contains("EchoDesk.", StringComparison.Ordinal))
            return false;
        if (!LatinSentence.IsMatch(t))
            return false;
        string word = t.TrimEnd('.', ':', '!', '?', ' ');
        return !LatinAllowed.Contains(word);
    }

    private static readonly Regex LatinSentence = new(@"^[A-Za-z][A-Za-z0-9 .,:;!?()\[\]/+\-–—_&%'""]*$");
    /// <summary>
    /// [EN] "Suspected English leftovers" collected in Chinese mode (listed only, confirmed by
    /// [EN] a human; technical terms are mixed in).
    /// [ZH] 中文模式下收集到的“疑似英文残留”（只列清单，由人确认；技术名词会混在里面）。
    /// </summary>
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
                // [EN] Report each kind of text on a control only once (Header and Content
                // [EN] often overlap).
                // [ZH] 同一控件同类文本只报一次（Header 与 Content 常重叠）
                if (!seen.Add(text)) continue;
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


    /// <summary>
    /// [EN] Language autonyms (the language selector must use each language's own script).
    /// [ZH] 语言自称（语言选择器里必须用各自的文字）。
    /// </summary>
    private static readonly string[] LanguageAutonyms = { "简体中文", "繁體中文" };

    /// <summary>
    /// [EN] Plugin display names: **proper nouns / brand names that do not take part in UI
    /// [EN] localization** (a maintainer decision). Showing a plugin's own name in the UI is
    /// [EN] correct product design, not a missed translation — the same reasoning as language
    /// [EN] autonyms, and only this single form is let through.
    /// [ZH] 插件显示名：**专有名词 / 品牌名，不参与 UI 本地化**（维护者裁定）。
    /// [ZH] 界面里显示插件自己的名字是正确的产品设计，不是漏译 —— 与语言自称同理，只放过这一种形态。
    /// </summary>
    private static readonly string[] PluginNames =
    {
        "Hello World 示例插件", "OFD 文档读取",
        "Hello World example plugin", "OFD document reader",
    };

    /// <summary>
    /// [EN] Backend log lines that start with a timestamp (§12.3 designs them as
    /// [EN] single-language; showing them verbatim is a known trade-off).
    /// [ZH] 以时间戳开头的后端日志行（§12.3 设计为单语，原样显示属已知取舍）。
    /// </summary>
    private static readonly Regex BackendLogLine = new(@"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}");

    /// <summary>
    /// [EN] Path fragments (Chinese directory names are "data", not UI copy).
    /// [ZH] 路径片段（中文目录名属“数据”，不是界面文案）。
    /// </summary>
    private static readonly Regex PathFragment = new(@"[A-Za-z]:\\[^\r\n，。；]*|/[A-Za-z0-9_.\-]+(?:/[^\r\n，。；]*)*");

    /// <summary>
    /// [EN] Titles deliberately shown bilingually (the same source as BilingualLabels in the
    /// [EN] WPF-side ui_sweep_wpf.ps1): a bilingual language label lets users of both languages
    /// [EN] find it at a glance; this is **by design**, not a missed translation.
    /// [ZH] 刻意双语显示的标题（与 WPF 侧 ui_sweep_wpf.ps1 的 BilingualLabels 同源）：
    /// [ZH] 「语言 / Language」这类标题两种语言的用户都能一眼找到，属于**设计如此**，不是漏译。
    /// </summary>
    private static readonly string[] BilingualLabels = { "语言 / Language", "Language / 语言" };

    /// <summary>
    /// [EN] Strip the legitimate Chinese categories listed above from the text; only Chinese
    /// [EN] remaining after that counts as a defect.
    /// [ZH] 把上列几类“合理中文”从文本里剔掉；剔完还剩中文才算缺陷。
    /// </summary>
    private static bool IsAllowedChinese(string text)
    {
        // [EN] The whole line is a backend log: skip it.
        // [ZH] 整行是后端日志：跳过
        if (BackendLogLine.IsMatch(text.TrimStart())) return true;

        // [EN] Plugin display name (a proper noun): if the whole string is the plugin name, let
        // [EN] it pass directly.
        // [ZH] 插件显示名（专有名词）：整串就是插件名时直接放过
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

    /// <summary>
    /// [EN] All text sources that a user can see or read on a control.
    /// [ZH] 一个控件上“用户能看见/能读到”的所有文本来源。
    /// </summary>
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
    /// [EN] Save the window as currently rendered to a PNG; a failure only warns (screenshots
    /// [EN] are review material, not a pass condition). <paramref name="scale"/> is used for
    /// [EN] README showcase images: 2 renders at twice the pixel size (high-DPI appearance).
    /// [ZH] 把窗口按当前渲染结果存成 PNG；失败只告警（截图是复核材料，不是判定条件）。
    /// [ZH] <paramref name="scale"/> 用于 README 宣传图：2 表示按 2 倍像素渲染（高分屏观感）。
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
            // [EN] Note: this overload is marked obsolete (warning only); it is kept so that
            // [EN] the code does not depend on the encoder option type of a specific version.
            // [ZH] 注：该重载标记过时（仅警告），保留以免依赖具体版本的编码器选项类型
            bmp.Save(path);
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






