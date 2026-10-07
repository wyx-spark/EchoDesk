// 平台探测与能力边界（任务书 任务三 / 任务五 的前端半边）。
//
// 这个类要回答三个问题，而且是**唯一**回答它们的地方：
//   1. 我现在跑在哪个平台？（kind / RID / 架构）
//   2. 这个平台上哪些能力**不可能**有？（WPS COM 读加密文档、企业内网自动化）
//   3. 后端可执行文件在这个平台上叫什么、用户数据该放哪？
//
// 为什么单独立一个类而不是散落在各处 `if (OperatingSystem.IsWindows())`：
//   * 任务五要求"降级必须给用户明确文案 + 替代路径"，文案要能说清"为什么没有"，
//     所以需要 DisplayName 这种可读名字；
//   * 任务一要求前端与后端**对平台判断一致**（自检会比对后端 ready 行里的 platform.kind），
//     两边各写一份判断逻辑迟早会漂移，这里集中一处、后端另有一处，由自检做一致性校验。
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;

namespace EchoDesk.Avalonia.Services;

/// <summary>操作系统大类。后端 <c>backend/platform_support.py</c> 上报的 <c>kind</c> 用同一组字符串。</summary>
public enum PlatformKind
{
    Windows,
    MacOS,
    Linux,
    Unknown,
}

/// <summary>本机平台信息（进程内只探测一次，见 <see cref="Current"/>）。</summary>
public sealed class PlatformInfo
{
    /// <summary>一键取用的当前平台（探测结果进程内不变）。</summary>
    public static PlatformInfo Current { get; } = Detect();

    public PlatformKind Kind { get; private init; }

    /// <summary>与后端 <c>ready.platform.kind</c> 对齐的机器名：<c>windows</c> / <c>macos</c> / <c>linux</c> / <c>unknown</c>。</summary>
    public string KindName { get; private init; } = "unknown";

    /// <summary>给人看的平台名（界面横幅、关于对话框用）。</summary>
    public string DisplayName { get; private init; } = "";

    /// <summary>带版本号的 OS 描述（排障用；来自 <see cref="RuntimeInformation.OSDescription"/>）。</summary>
    public string OsDescription { get; private init; } = "";

    /// <summary>CPU 架构（X64 / Arm64 / …）。</summary>
    public string Architecture { get; private init; } = "";

    /// <summary>.NET 的 RID（win-x64 / linux-x64 / osx-arm64 …），发布脚本与 CI 矩阵按它切片。</summary>
    public string RuntimeIdentifier { get; private init; } = "";

    /// <summary>后端可执行文件名（Windows 带 .exe，其余平台不带）。</summary>
    public string BackendExecutableName { get; private init; } = "EchoDeskBackend";

    /// <summary>纯 Python 后端的入口脚本名（未打包时用解释器直接跑它）。</summary>
    public string BackendScriptName { get; private init; } = "main.py";

    public bool IsWindows => Kind == PlatformKind.Windows;

    /// <summary>WPS COM 读加密文档：**只有 Windows 可能有**（COM + 本机 WPS 安装）。</summary>
    public bool SupportsWpsCom => IsWindows;

    /// <summary>企业内网自动化（Playwright/Edge）：非 Windows 上不提供（任务五的降级边界）。</summary>
    public bool SupportsAutomation => IsWindows;

    // 这里原有 `UserDataDirectoryName => IsWindows ? "EchoDesk" : "EchoDesk"` —— 两个分支一模一样，
    // 而且**全仓库没有任何调用方**（v0.2.2 审计 `git grep` 确认只有它自己的定义行）。
    // 它的注释还写着"Windows 用 %APPDATA%"，而代码里根本没有这条路径。
    // 死代码 + 错注释的组合最容易被后来者当真，因此直接删掉：
    // 用户数据目录由 UserPaths.DataRoot 负责（那个才是唯一实现）。

    /// <summary>
    /// 能力列表：给界面横幅用 —— 哪些能力在本平台**不可用**及替代做法。
    ///
    /// 文案**全部走语言包**（`main.platform.cap.*`）：此前这 6 段中文写死在本文件里，
    /// 英文界面下整条降级横幅都是中文（硬编码清查清单 §5.1）。键值与后端
    /// `platform_support.capabilities()` 的口径一致，只是本文件负责前端的"分组展示"文案。
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

    /// <summary>探测当前平台。不做任何 IO，也不依赖图形环境（无显示器的 CI 上必须能跑）。</summary>
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
            // 不假装认识：未知平台按"能力最少"处理（两个 Windows 专属能力都标不可用），
            // 但**不阻止启动** —— 协议层是纯 stdio，理论上任何平台都能跑起来。
            kind = PlatformKind.Unknown;
            kindName = "unknown";
            // 文案走语言包（硬编码清查清单 §5.1）；这里在启动早期调用，语言包已由 App 初始化
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

    /// <summary>拼 RID。刻意不调 <c>RuntimeInformation.RuntimeIdentifier</c>：它在自包含
    /// 发布与框架依赖发布下返回不同值（后者带发行版后缀，如 <c>linux-mint.21-x64</c>），
    /// 而发布脚本需要的是**稳定的三平台切片名**。</summary>
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
    /// 找一个可用的 Python 解释器（未打包时用解释器直接跑 <c>backend/main.py</c>）。
    /// 返回完整命令行参数的 (可执行文件, 前置参数) —— Windows 上优先 <c>py -3</c>。
    /// 找不到返回 null，调用方据此给出"未找到后端"的明确报错。
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
            // macOS/Linux：python3 优先，且必须用绝对路径查找结果的裸名（PATH 查找）。
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

    /// <summary>裸名是否能在 PATH 上找到（跨平台的 which/where，不启动进程）。</summary>
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
                    // PATH 上可能只写了 "python" 而实际文件是 python.exe
                    foreach (string ext in new[] { ".exe", ".cmd", ".bat" })
                    {
                        if (File.Exists(full + ext))
                            return true;
                    }
                }
            }
            catch (ArgumentException)
            {
                // PATH 里有非法字符的目录项：跳过而不是崩掉
            }
        }
        return false;
    }

    /// <summary>一行摘要（日志/自检/关于对话框都用它，保证同一份口径）。</summary>
    public string Summary() => $"{DisplayName} / {Architecture} / {RuntimeIdentifier} / .NET {Environment.Version}";
}
