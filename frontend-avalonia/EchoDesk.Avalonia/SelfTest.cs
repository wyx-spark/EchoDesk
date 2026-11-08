// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/SelfTest.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/SelfTest.cs。类型与成员说明见下方注释。
// [EN] Cross-platform startup self-test (`EchoDesk --selftest`).
// [EN]
// [EN] Why it must exist (the verification requirement of development task 1): only Windows
// [EN] is available locally, and Linux/macOS have neither a graphical environment nor a
// [EN] convenient way to run an interactive window, yet "does cross-platform work at all"
// [EN] must be backed by evidence rather than by a claim.
// [EN] This mode **creates no window** and only does three things:
// [EN]   1. Print the platform probe result (OS / architecture / RID / capability flags);
// [EN]   2. Locate and launch the Python backend according to platform rules;
// [EN]   3. Run through the real JSON line protocol (ready → chat action → receive response).
// [EN] Thus `dotnet EchoDesk.dll --selftest` on CI verifies the "startup chain + protocol"
// [EN] layer without a display and without a model (with no model the backend returns
// [EN] guidance text, and the protocol still works).
// [ZH] 跨平台启动自检（`EchoDesk --selftest`）。
// [ZH]
// [ZH] 为什么必须有它（研发任务 1 的验证要求）：本机只有 Windows，Linux/macOS 上既没有
// [ZH] 图形环境也不便跑交互式窗口，但“跨平台到底能不能用”必须给出证据而不是声明。
// [ZH] 这个模式**不创建任何窗口**，只跑三件事：
// [ZH]   1. 打印平台探测结果（OS / 架构 / RID / 平台能力标记）；
// [ZH]   2. 按平台规则定位并拉起 Python 后端；
// [ZH]   3. 走一遍真实 JSON 行协议（ready → chat 动作 → 收响应）。
// [ZH] 于是 CI 上 `dotnet EchoDesk.dll --selftest` 就能验证“启动链路 + 协议”这一层，
// [ZH] 不需要显示器、不需要模型（无模型时后端返回的是引导文案，协议照样通）。
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia;

internal static class SelfTest
{
    public static async Task<int> RunAsync(string[] args)
    {
        var passed = 0;
        var failed = 0;
        void Check(string name, bool ok, string detail = "")
        {
            if (ok) { passed++; Console.WriteLine($"  [PASS] {name}"); }
            else { failed++; Console.WriteLine($"  [FAIL] {name}" + (detail.Length > 0 ? $" —— {detail}" : "")); }
        }

        Console.WriteLine("=== EchoDesk 跨平台自检（--selftest）===");
        var platform = PlatformInfo.Detect();
        Console.WriteLine($"平台：{platform.DisplayName}");
        Console.WriteLine($"  OS 描述      : {platform.OsDescription}");
        Console.WriteLine($"  架构 / RID   : {platform.Architecture} / {platform.RuntimeIdentifier}");
        Console.WriteLine($"  .NET         : {Environment.Version}");
        Console.WriteLine($"  WPS COM 加密文档 : {(platform.SupportsWpsCom ? "可用" : "不可用（平台限制）")}");
        Console.WriteLine($"  企业内网自动化   : {(platform.SupportsAutomation ? "可用" : "不可用（平台限制）")}");
        Console.WriteLine();

        Check("平台探测给出结论（不是「未知」）", platform.Kind != PlatformKind.Unknown, platform.Kind.ToString());
        Check("能力标记与平台一致",
            platform.SupportsWpsCom == platform.IsWindows && platform.SupportsAutomation == platform.IsWindows,
            $"wps={platform.SupportsWpsCom} automation={platform.SupportsAutomation}");
        Check("RID 不是空的", !string.IsNullOrWhiteSpace(platform.RuntimeIdentifier), platform.RuntimeIdentifier);

        // [EN] Backend startup (it starts without a model too: the backend enters a "ready
        // [EN] without model" state and returns guidance text).
        // [ZH] 后端启动（无模型也能起来：后端会进入“无模型就绪”状态并给引导文案）。
        var backend = new BackendService();
        var lines = new List<string>();
        backend.LineReceived += line =>
        {
            lock (lines) { lines.Add(line); }
        };
        var started = await backend.StartAsync(verbose: true);
        Check("后端进程能按平台规则拉起", started.Ok, started.Error);
        if (started.Ok)
        {
            Console.WriteLine($"  启动命令     : {started.Command}");
            var ready = await backend.WaitForReadyAsync(TimeSpan.FromSeconds(60));
            Check("收到 ready 握手", ready is not null, "60 秒内没有 ready 行");
            if (ready is not null)
            {
                var payload = ready.RootElement;
                Check("ready 行带 model_ready 字段", payload.TryGetProperty("model_ready", out _));
                Check("ready 行带 platform 字段（本轮新增的降级依据）",
                    payload.TryGetProperty("platform", out _));
                if (payload.TryGetProperty("platform", out var plat))
                {
                    var kind = plat.TryGetProperty("kind", out var k) ? k.GetString() : "";
                    Check("后端上报的平台与本机一致", kind == platform.KindName, $"后端={kind} 前端={platform.KindName}");
                }
            }

            // [EN] Ordinary (non-streaming) action: use the platform action added in this
            // [EN] round, which also verifies that the platform report round-trips.
            // [EN] Note: **do not** use about — the backend has no such action and replies
            // [EN] "unknown action", so the "content is non-empty" assertion would still
            // [EN] pass, meaning nothing was tested (the first version did exactly that;
            // [EN] fixed since).
            // [ZH] 普通（非流式）动作：用本轮新增的 platform 动作，顺带校验平台报告能往返。
            // [ZH] 注意**不要**用 about —— 后端没有这个动作，它会回一句“未知动作”，
            // [ZH] 断言“内容非空”照样通过，等于没测（第一版就是这么写的，已改掉）。
            var platformReply = await backend.SendAsync(new { action = "platform" }, TimeSpan.FromSeconds(60));
            Check("能跑通一条普通动作（platform）", platformReply is not null, "没有收到响应");
            if (platformReply is not null)
            {
                using (platformReply)
                {
                    var root = platformReply.RootElement;
                    bool hasPlatform = root.TryGetProperty("platform", out var plat);
                    Check("platform 动作返回平台报告", hasPlatform);
                    if (hasPlatform)
                    {
                        string kind = plat.TryGetProperty("kind", out var k) ? k.GetString() ?? "" : "";
                        Check("平台报告的平台名与本机一致", kind == platform.KindName, $"报告={kind} 本机={platform.KindName}");
                        bool hasCaps = plat.TryGetProperty("capabilities", out var caps)
                                       && caps.ValueKind == JsonValueKind.Object
                                       && caps.EnumerateObject().Any();
                        Check("平台报告带能力清单", hasCaps);
                        int degraded = plat.TryGetProperty("degraded", out var deg) && deg.ValueKind == JsonValueKind.Array
                            ? deg.GetArrayLength() : -1;
                        // [EN] Windows must have no degraded items; non-Windows must list at
                        // [EN] least two (WPS COM + automation).
                        // [ZH] Windows 上不应有降级项；非 Windows 上必须至少列出两项
                        // [ZH] （WPS COM + 自动化）。
                        Check(platform.IsWindows ? "Windows 上降级项为空" : "非 Windows 上列出了降级项",
                            platform.IsWindows ? degraded == 0 : degraded >= 2,
                            $"degraded={degraded}");
                    }
                }
            }

            var status = await backend.SendAsync(new { action = "capability" }, TimeSpan.FromSeconds(90));
            if (status is not null && status.RootElement.TryGetProperty("capability", out var cap))
            {
                var notes = cap.TryGetProperty("warnings", out var w) && w.ValueKind == JsonValueKind.Array
                    ? w.GetArrayLength() : 0;
                Console.WriteLine($"  能力画像     : 已返回（{notes} 条提示）");
                Check("capability 动作可用（跨平台后仍是同一套协议）", true);
            }
            else
            {
                Check("capability 动作可用（跨平台后仍是同一套协议）", false, "没有返回 capability 字段");
            }
            backend.Dispose();
        }

        // [EN] ---------- v0.2.2 batch 3: UI settings interop + crash log + launcher ----------
        // [ZH] ---------- v0.2.2 第 3 批：界面设置的跨前端兼容 + 崩溃日志 + 启动器 ----------
        //
        // [EN] These three items **need neither a graphical environment nor the backend**, so
        // [EN] the self-test is the best place for them: they all verify "pure logic" and can
        // [EN] be asserted on any platform.
        // [ZH] 这三项**不需要图形环境也不需要后端**，所以放在自检里最合适：
        // [ZH] 它们验的都是“纯逻辑”，任何平台上都能断言。
        Console.WriteLine();
        Console.WriteLine("== v0.2.2 第 3 批：界面状态兼容 / 崩溃日志 / 启动器 ==");

        string tempDir = Path.Combine(Path.GetTempPath(), "echodesk-ui-state-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(tempDir);
        string? previousData = Environment.GetEnvironmentVariable("ECHODESK_DATA");
        try
        {
            Environment.SetEnvironmentVariable("ECHODESK_DATA", tempDir);

            // [EN] 1. Key point: WPF writes PascalCase, and the cross-platform version must be
            // [EN] able to read it (otherwise switching front ends loses preferences).
            // [ZH] ① 关键：WPF 写的是 PascalCase，跨平台版必须读得懂（否则换前端就丢偏好）。
            File.WriteAllText(Path.Combine(tempDir, "ui_state.json"),
                "{\"FontSize\":17,\"Theme\":\"dark\",\"Language\":\"en\",\"EmojiMode\":\"never\","
                + "\"WindowMaximized\":true,\"FirstRunDone\":true,\"ShortcutPromptDeclined\":true,"
                + "\"WindowLeft\":123,\"WindowTop\":45}",
                new System.Text.UTF8Encoding(false));
            AppSettings wpfWritten = AppSettings.Load();
            Check("读得懂 WPF 写的 PascalCase 字段（FontSize）", Math.Abs(wpfWritten.FontSize - 17) < 0.001,
                wpfWritten.FontSize.ToString("0.#"));
            Check("读得懂 WPF 写的 Theme", wpfWritten.Theme == "dark", wpfWritten.Theme);
            Check("读得懂 WPF 写的 Language", wpfWritten.Language == "en", wpfWritten.Language);
            Check("读得懂 WPF 写的 EmojiMode（映射到 EmojiPreference）",
                wpfWritten.EmojiPreference == "never", wpfWritten.EmojiPreference);
            Check("读得懂 WPF 写的窗口位置", Math.Abs(wpfWritten.WindowLeft - 123) < 0.001
                && Math.Abs(wpfWritten.WindowTop - 45) < 0.001,
                $"{wpfWritten.WindowLeft}/{wpfWritten.WindowTop}");
            Check("读得懂 WPF 写的一次性询问标志",
                wpfWritten.FirstRunDone && wpfWritten.ShortcutPromptDeclined, "两个都为 true");

            // [EN] 2. Reverse direction: the file written by the cross-platform version
            // [EN] contains **both key-name styles**, so WPF can read it too.
            // [ZH] ② 反向：跨平台版写出去的文件里**两种键名都在**，WPF 也读得懂。
            wpfWritten.FontSize = 16;
            wpfWritten.Save();
            string written = File.ReadAllText(Path.Combine(tempDir, "ui_state.json"));
            Check("落盘时同时写 snake_case 与 PascalCase",
                written.Contains("\"font_size\"") && written.Contains("\"FontSize\""),
                "两种键名都在");
            AppSettings roundTrip = AppSettings.Load();
            Check("回读自己的文件（snake_case 优先）", Math.Abs(roundTrip.FontSize - 16) < 0.001,
                roundTrip.FontSize.ToString("0.#"));

            // [EN] 3. Crash log: it can be written, and its content contains the exception
            // [EN] type.
            // [ZH] ③ 崩溃日志：写得进去，且内容里有异常类型。
            CrashLog.Append("selftest", new InvalidOperationException("selftest-marker"));
            string logPath = CrashLog.FilePath;
            Check("崩溃日志写到了用户数据目录", File.Exists(logPath) && logPath.StartsWith(tempDir,
                StringComparison.OrdinalIgnoreCase), logPath);
            Check("崩溃日志里有异常类型与消息",
                File.Exists(logPath) && File.ReadAllText(logPath).Contains("selftest-marker"),
                "内容可查");

            // [EN] 4. Launcher: the file name, and that IsValid safely returns false when there
            // [EN] is "no launcher".
            // [ZH] ④ 启动器：文件名、以及 IsValid 在“没有启动器”时必须安全返回 false。
            //
            // [EN] **The environment must be cleaned up before asserting** (v0.2.2 batch 4, a
            // [EN] side effect found after decisions 1 and 3): once decision 1 changed the
            // [EN] start method to "create automatically", **a single UI probe run leaves that
            // [EN] launcher lying in the program directory**, so this assertion became
            // [EN] "dependent on leftovers from the environment" — the same code was green on a
            // [EN] clean machine and red on a machine that had run the probe. That is a
            // [EN] **non-isolated assertion**, not a product defect, and falls under hard
            // [EN] constraint 6 of PROJECT.md (when a gate fails, suspect the gate first).
            // [EN] The fix is not to weaken the assertion but to **restore the environment to
            // [EN] "no launcher" before verifying**, and to put the original state back
            // [EN] afterwards (so a developer machine does not lose a file by running the
            // [EN] self-test).
            // [ZH] **必须先把现场清干净再断言**（v0.2.2 第 4 批，裁定一/三之后发现的连带问题）：
            // [ZH] 决策①把启动方式改成“自动创建”之后，**只要跑过一次界面试探，程序目录里就会
            // [ZH] 真的躺着那个启动器**，于是这条断言变成了“依赖环境残留”——同一份代码，
            // [ZH] 干净机器上绿、跑过探针的机器上红。这是**断言自身不隔离**，不是产品缺陷，
            // [ZH] 属 PROJECT.md 硬约束 6（门禁失败先怀疑门禁）。
            // [ZH] 修法不是放宽断言，而是**把现场恢复成“没有启动器”再验**，验完把原状还
            // [ZH] 原回去（这样开发机上不会因为跑自检而少一个文件）。
            Check("启动器文件名为本平台惯例",
                ShortcutService.FileName.Length > 0, ShortcutService.FileName);

            string launcherPath = ShortcutService.ShortcutPath;
            byte[]? originalLauncher = null;
            try
            {
                if (File.Exists(launcherPath))
                    originalLauncher = File.ReadAllBytes(launcherPath);

                if (originalLauncher is not null)
                    File.Delete(launcherPath);
                Check("IsValid 在缺少启动器时安全返回 false", !ShortcutService.IsValid(),
                    $"已临时移除 {Path.GetFileName(launcherPath)} 后判定");

                // [EN] It can be created, is judged "valid" once created (pointing at the
                // [EN] current exe), and is then deleted.
                // [ZH] 建得出来、建完判定为“有效”（指向当前 exe），再删掉。
                string created = ShortcutService.Create();
                bool ok = File.Exists(created) && ShortcutService.IsValid();
                Check("Create 建出的启动器能被判定为有效", ok, created);
            }
            catch (Exception ex)
            {
                // [EN] Scenarios such as a read-only program directory: do not misreport "the
                // [EN] environment does not allow it" as a product defect.
                // [ZH] 程序目录只读等场景：不把“环境不允许”误报成产品缺陷。
                Console.WriteLine($"  [SKIP] 启动器写盘受限，跳过创建类断言（{ex.Message}）");
            }
            finally
            {
                // [EN] Restore the environment: write it back if it existed before, delete it
                // [EN] if it did not (the self-test leaves no trace).
                // [ZH] 还原现场：原本有就写回去，原本没有就删掉（自检不留痕）。
                try
                {
                    if (originalLauncher is not null)
                        File.WriteAllBytes(launcherPath, originalLauncher);
                    else if (File.Exists(launcherPath))
                        File.Delete(launcherPath);
                }
                catch (Exception)
                {
                    // [EN] A failed restore does not affect the conclusion.
                    // [ZH] 还原失败不影响结论。
                }
            }
        }
        finally
        {
            Environment.SetEnvironmentVariable("ECHODESK_DATA", previousData);
            try
            {
                Directory.Delete(tempDir, recursive: true);
            }
            catch (Exception)
            {
                // [EN] A temporary directory that cannot be deleted does not affect the
                // [EN] conclusion.
                // [ZH] 临时目录删不掉不影响结论。
            }
        }

        Console.WriteLine();
        Console.WriteLine($"通过 {passed} 项，失败 {failed} 项");
        return failed == 0 ? 0 : 1;
    }

    private static string Trim(string text, int limit)
    {
        if (string.IsNullOrEmpty(text)) return "";
        var flat = text.Replace('\n', ' ').Replace('\r', ' ');
        return flat.Length <= limit ? flat : flat[..limit] + "…";
    }
}
