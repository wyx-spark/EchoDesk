// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/PlatformInfo.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/PlatformInfo.cs。类型与成员说明见下方注释。
// [EN] Platform detection and capability boundaries (the front-end half of development
// [EN] tasks 3 and 5 in the design specification).
// [ZH] 平台探测与能力边界（设计规范 研发任务 3 / 研发任务 5 的前端半边）。
//
// [EN] This class answers three questions, and it is the **only** place that answers them:
// [ZH] 这个类要回答三个问题，而且是**唯一**回答它们的地方：
// [EN]   1. Which platform am I running on? (kind / RID / architecture)
// [ZH]   1. 我现在跑在哪个平台？（kind / RID / 架构）
// [EN]   2. Which capabilities are **impossible** on this platform? (WPS COM reading
// [EN]      encrypted documents, enterprise intranet automation)
// [ZH]   2. 这个平台上哪些能力**不可能**有？（WPS COM 读加密文档、企业内网自动化）
// [EN]   3. What is the backend executable called on this platform, and where should user
// [EN]      data go?
// [ZH]   3. 后端可执行文件在这个平台上叫什么、用户数据该放哪？
//
// [EN] Why a separate class instead of scattering `if (OperatingSystem.IsWindows())`
// [EN] everywhere:
// [ZH] 为什么单独立一个类而不是散落在各处 `if (OperatingSystem.IsWindows())`：
// [EN]   * Development task 5 requires that "degradation must give the user clear wording
// [EN]     and an alternative path", and the wording must explain "why it is unavailable",
// [EN]     so a readable name such as DisplayName is needed;
// [ZH]   * 研发任务 5 要求“降级必须给用户明确文案 + 替代路径”，文案要能说清“为什么没有”，
// [ZH]     所以需要 DisplayName 这种可读名字；
// [EN]   * Development task 1 requires the front end and backend to **agree on platform
// [EN]     detection** (the self-check compares platform.kind in the backend ready line);
// [EN]     separate copies of the logic on both sides would eventually drift, so it is
// [EN]     centralized here and in one other place in the backend, with the self-check
// [EN]     verifying consistency.
// [ZH]   * 研发任务 1 要求前端与后端**对平台判断一致**（自检会比对后端 ready 行里的 platform.kind），
// [ZH]     两边各写一份判断逻辑迟早会漂移，这里集中一处、后端另有一处，由自检做一致性校验。
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;

namespace EchoDesk.Avalonia.Services;

/// <summary>
/// [EN] Operating system family. The <c>kind</c> reported by the backend
/// [EN] <c>backend/platform_support.py</c> uses the same set of strings.
/// [ZH] 操作系统大类。后端 <c>backend/platform_support.py</c> 上报的 <c>kind</c> 用同一组字符串。
/// </summary>
public enum PlatformKind
{
    Windows,
    MacOS,
    Linux,
    Unknown,
}

/// <summary>
/// [EN] Platform information for this machine (probed only once per process; see
/// [EN] <see cref="Current"/>).
/// [ZH] 本机平台信息（进程内只探测一次，见 <see cref="Current"/>）。
/// </summary>
public sealed class PlatformInfo
{
    /// <summary>
    /// [EN] The current platform, ready to use (the probe result does not change within a
    /// [EN] process).
    /// [ZH] 一键取用的当前平台（探测结果进程内不变）。
    /// </summary>
    public static PlatformInfo Current { get; } = Detect();

    public PlatformKind Kind { get; private init; }

    /// <summary>
    /// [EN] Machine name aligned with the backend's <c>ready.platform.kind</c>:
    /// [EN] <c>windows</c> / <c>macos</c> / <c>linux</c> / <c>unknown</c>.
    /// [ZH] <c>windows</c> / <c>macos</c> / <c>linux</c> / <c>unknown</c> 为与后端
    /// [ZH] <c>ready.platform.kind</c> 对齐的机器名。
    /// </summary>
    public string KindName { get; private init; } = "unknown";

    /// <summary>
    /// [EN] Human-readable platform name (used by the UI banner and the About dialog).
    /// [ZH] 给人看的平台名（界面横幅、关于对话框用）。
    /// </summary>
    public string DisplayName { get; private init; } = "";

    /// <summary>
    /// [EN] OS description including the version (for troubleshooting; from
    /// [EN] <see cref="RuntimeInformation.OSDescription"/>).
    /// [ZH] 带版本号的 OS 描述（排障用；来自 <see cref="RuntimeInformation.OSDescription"/>）。
    /// </summary>
    public string OsDescription { get; private init; } = "";

    /// <summary>
    /// [EN] CPU architecture (X64 / Arm64 / …).
    /// [ZH] CPU 架构（X64 / Arm64 / …）。
    /// </summary>
    public string Architecture { get; private init; } = "";

    /// <summary>
    /// [EN] The .NET RID (win-x64 / linux-x64 / osx-arm64 …); release scripts and the CI
    /// [EN] matrix slice by it.
    /// [ZH] .NET 的 RID（win-x64 / linux-x64 / osx-arm64 …），发布脚本与 CI 矩阵按它切片。
    /// </summary>
    public string RuntimeIdentifier { get; private init; } = "";

    /// <summary>
    /// [EN] Backend executable file name (with .exe on Windows, without it elsewhere).
    /// [ZH] 后端可执行文件名（Windows 带 .exe，其余平台不带）。
    /// </summary>
    public string BackendExecutableName { get; private init; } = "EchoDeskBackend";

    /// <summary>
    /// [EN] Entry script name of the pure-Python backend (when unpackaged, the interpreter runs
    /// [EN] it directly).
    /// [ZH] 纯 Python 后端的入口脚本名（未打包时用解释器直接跑它）。
    /// </summary>
    public string BackendScriptName { get; private init; } = "main.py";

    public bool IsWindows => Kind == PlatformKind.Windows;

    /// <summary>
    /// [EN] WPS COM reading of encrypted documents: **only possible on Windows** (COM plus a
    /// [EN] local WPS installation).
    /// [ZH] WPS COM 读加密文档：**只有 Windows 可能有**（COM + 本机 WPS 安装）。
    /// </summary>
    public bool SupportsWpsCom => IsWindows;

    /// <summary>
    /// [EN] Enterprise intranet automation (Playwright/Edge): not provided on non-Windows
    /// [EN] platforms (the degradation boundary of development task 5).
    /// [ZH] 企业内网自动化（Playwright/Edge）：非 Windows 上不提供（研发任务 5 的降级边界）。
    /// </summary>
    public bool SupportsAutomation => IsWindows;

    // [EN] Previously this file had
    // [EN] `UserDataDirectoryName => IsWindows ? "EchoDesk" : "EchoDesk"`; both branches
    // [EN] were identical, and **nothing in the whole repository called it** (the v0.2.2
    // [EN] audit with `git grep` confirmed only its own definition line).
    // [EN] Its comment also claimed "Windows uses %APPDATA%", while no such path exists in the
    // [EN] code. A combination of dead code and a wrong comment is easily taken seriously by
    // [EN] later readers, so it was deleted outright: the user data directory is owned by
    // [EN] UserPaths.DataRoot (the only implementation).
    // [ZH] 这里原有 `UserDataDirectoryName => IsWindows ? "EchoDesk" : "EchoDesk"` —— 两个分支一模一样，
    // [ZH] 而且**全仓库没有任何调用方**（v0.2.2 审计 `git grep` 确认只有它自己的定义行）。
    // [ZH] 它的注释还写着“Windows 用 %APPDATA%”，而代码里根本没有这条路径。
    // [ZH] 死代码 + 错注释的组合最容易被后来者当真，因此直接删掉：
    // [ZH] 用户数据目录由 UserPaths.DataRoot 负责（那个才是唯一实现）。

    /// <summary>
    /// [EN] Capability list: used by the UI banner to show which capabilities are
    /// [EN] **unavailable** on this platform and what the alternatives are.
    /// [EN]
    /// [EN] All wording **goes through the language pack** (`main.platform.cap.*`): these six
    /// [EN] Chinese strings used to be hard-coded in this file, so under an English UI the
    /// [EN] whole degradation banner was in Chinese (hard-coded inventory checklist §5.1).
    /// [EN] Keys and values match the backend's `platform_support.capabilities()`; this file
    /// [EN] only owns the front-end "grouped display" wording.
    /// [ZH] 能力列表：给界面横幅用 —— 哪些能力在本平台**不可用**及替代做法。
    /// [ZH]
    /// [ZH] 文案**全部走语言包**（`main.platform.cap.*`）：此前这 6 段中文写死在本文件里，
    /// [ZH] 英文界面下整条降级横幅都是中文（硬编码清查清单 §5.1）。键值与后端
    /// [ZH] `platform_support.capabilities()` 的口径一致，只是本文件负责前端的“分组展示”文案。
    /// </summary>
    public IReadOnlyList<(string Capability, bool Available, string Note)> Capabilities()
    {
        var list = new List<(string, bool, string)>
        {
            (Loc.T("main.platform.cap.local", "对话 / 流式输出 / 文件解析"), true,
             Loc.T("main.platform.cap.localNote", "全平台可用")),
            (Loc.T("main.platform.cap.extended", "本地检索（RAG）/ Agent 平台 / MCP / 插件"), true,
             Loc.T("main.platform.cap.extendedNote", "全平台可用，纯 Python 实现")),
        };
        string wps = Loc.T("main.platform.cap.wps", "WPS COM 读加密文档");
        string automation = Loc.T("main.platform.cap.automation", "企业内网自动化（Playwright/Edge）");
        if (IsWindows)
        {
            list.Add((wps, true, Loc.T("main.platform.cap.wpsNote", "Windows 专属：依赖本机 WPS 的 COM 接口")));
            list.Add((automation, true, Loc.T("main.platform.cap.automationNote", "Windows 专属：依赖本机 Edge")));
        }
        else
        {
            list.Add((wps, false, Loc.T("main.platform.cap.wpsDegraded",
                "仅 Windows：非 Windows 上加密文档改用通用解析器，密文会解析成乱码")));
            list.Add((automation, false, Loc.T("main.platform.cap.automationDegraded",
                "仅 Windows：本平台不提供该页面，可用 MCP/插件接入外部工具替代")));
        }
        return list;
    }

    /// <summary>
    /// [EN] Probes the current platform. It performs no IO and depends on no graphical
    /// [EN] environment (it must run on a headless CI machine).
    /// [ZH] 探测当前平台。不做任何 IO，也不依赖图形环境（无显示器的 CI 上必须能跑）。
    /// </summary>
    public static PlatformInfo Detect()
    {
        PlatformKind kind;
        string kindName;
        string display;
        string exe;

        if (OperatingSystem.IsWindows())
        {
            kind = PlatformKind.Windows;
            kindName = "windows";
            display = "Windows";
            exe = "EchoDeskBackend.exe";
        }
        else if (OperatingSystem.IsMacOS())
        {
            kind = PlatformKind.MacOS;
            kindName = "macos";
            display = "macOS";
            exe = "EchoDeskBackend";
        }
        else if (OperatingSystem.IsLinux())
        {
            kind = PlatformKind.Linux;
            kindName = "linux";
            display = "Linux";
            exe = "EchoDeskBackend";
        }
        else
        {
            // [EN] Do not pretend to recognize it: an unknown platform gets the "fewest
            // [EN] capabilities" treatment (both Windows-only capabilities are marked
            // [EN] unavailable), but startup is **not blocked**; the protocol layer is pure
            // [EN] stdio, so in theory it can run on any platform.
            // [ZH] 不假装认识：未知平台按“能力最少”处理（两个 Windows 专属能力都标不可用），
            // [ZH] 但**不阻止启动** —— 协议层是纯 stdio，理论上任何平台都能跑起来。
            kind = PlatformKind.Unknown;
            kindName = "unknown";
            // [EN] Wording goes through the language pack (hard-coded inventory checklist
            // [EN] §5.1); this runs early at startup, after App has initialized the language
            // [EN] pack.
            // [ZH] 文案走语言包（硬编码清查清单 §5.1）；这里在启动早期调用，语言包已由 App 初始化
            display = Loc.T("main.platform.unknown", "未知平台（{0}）",
                RuntimeInformation.OSDescription);
            exe = "EchoDeskBackend";
        }

        return new PlatformInfo
        {
            Kind = kind,
            KindName = kindName,
            DisplayName = display,
            OsDescription = RuntimeInformation.OSDescription,
            Architecture = RuntimeInformation.OSArchitecture.ToString(),
            RuntimeIdentifier = BuildRid(kindName, RuntimeInformation.OSArchitecture),
            BackendExecutableName = exe,
        };
    }

    /// <summary>
    /// [EN] Builds the RID. It deliberately does not call
    /// [EN] <c>RuntimeInformation.RuntimeIdentifier</c>: that returns different values for
    /// [EN] self-contained and framework-dependent publishing (the latter carries a
    /// [EN] distribution suffix such as <c>linux-mint.21-x64</c>), while the release scripts
    /// [EN] need a **stable three-platform slice name**.
    /// [ZH] 拼 RID。刻意不调 <c>RuntimeInformation.RuntimeIdentifier</c>：它在自包含
    /// [ZH] 发布与框架依赖发布下返回不同值（后者带发行版后缀，如 <c>linux-mint.21-x64</c>），
    /// [ZH] 而发布脚本需要的是**稳定的三平台切片名**。
    /// </summary>
    private static string BuildRid(string kindName, System.Runtime.InteropServices.Architecture arch)
    {
        string suffix = arch switch
        {
            System.Runtime.InteropServices.Architecture.X64 => "x64",
            System.Runtime.InteropServices.Architecture.X86 => "x86",
            System.Runtime.InteropServices.Architecture.Arm64 => "arm64",
            System.Runtime.InteropServices.Architecture.Arm => "arm",
            _ => arch.ToString().ToLowerInvariant(),
        };
        return kindName switch
        {
            "windows" => "win-" + suffix,
            "macos" => "osx-" + suffix,
            "linux" => "linux-" + suffix,
            _ => "unknown-" + suffix,
        };
    }

    /// <summary>
    /// [EN] Finds a usable Python interpreter (when unpackaged, the interpreter runs
    /// [EN] <c>backend/main.py</c> directly).
    /// [EN] Returns (executable, prefix arguments) for the full command line; on Windows,
    /// [EN] <c>py -3</c> is preferred.
    /// [EN] Returns null when none is found; the caller then reports "backend not found"
    /// [EN] explicitly.
    /// [ZH] 找一个可用的 Python 解释器（未打包时用解释器直接跑 <c>backend/main.py</c>）。
    /// [ZH] 返回完整命令行参数的 (可执行文件, 前置参数) —— Windows 上优先 <c>py -3</c>。
    /// [ZH] 找不到返回 null，调用方据此给出“未找到后端”的明确报错。
    /// </summary>
    public (string FileName, string[] PrefixArgs)? FindPython()
    {
        var candidates = new List<(string, string[])>();
        if (IsWindows)
        {
            candidates.Add(("py", new[] { "-3" }));
            candidates.Add(("python", Array.Empty<string>()));
            candidates.Add(("python3", Array.Empty<string>()));
        }
        else
        {
            // [EN] macOS/Linux: python3 is preferred, and the bare name from the
            // [EN] absolute-path lookup must be used (PATH lookup).
            // [ZH] macOS/Linux：python3 优先，且必须用绝对路径查找结果的裸名（PATH 查找）。
            candidates.Add(("python3", Array.Empty<string>()));
            candidates.Add(("python", Array.Empty<string>()));
        }

        foreach ((string name, string[] prefix) in candidates)
        {
            if (OnPath(name))
                return (name, prefix);
        }
        return null;
    }

    /// <summary>
    /// [EN] Whether a bare name can be found on PATH (a cross-platform which/where that does
    /// [EN] not start a process).
    /// [ZH] 裸名是否能在 PATH 上找到（跨平台的 which/where，不启动进程）。
    /// </summary>
    public static bool OnPath(string fileName)
    {
        string? path = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(path))
            return false;
        foreach (string dir in path.Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(dir))
                continue;
            try
            {
                string full = Path.Combine(dir.Trim(), fileName);
                if (File.Exists(full))
                    return true;
                if (OperatingSystem.IsWindows())
                {
                    // [EN] PATH may contain only "python" while the actual file is python.exe
                    // [ZH] PATH 上可能只写了 “python” 而实际文件是 python.exe
                    foreach (string ext in new[] { ".exe", ".cmd", ".bat" })
                    {
                        if (File.Exists(full + ext))
                            return true;
                    }
                }
            }
            catch (ArgumentException)
            {
                // [EN] A PATH entry contains invalid characters: skip it instead of crashing
                // [ZH] PATH 里有非法字符的目录项：跳过而不是崩掉
            }
        }
        return false;
    }

    /// <summary>
    /// [EN] One-line summary (used by logs, self-checks, and the About dialog so that all
    /// [EN] share one wording).
    /// [ZH] 一行摘要（日志/自检/关于对话框都用它，保证同一份口径）。
    /// </summary>
    public string Summary() => $"{DisplayName} / {Architecture} / {RuntimeIdentifier} / .NET {Environment.Version}";
}
