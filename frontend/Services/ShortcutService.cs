// [EN] EchoDesk WPF frontend module: frontend/Services/ShortcutService.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Services/ShortcutService.cs。类型与成员说明见下方注释。
using System.Diagnostics;
using System.IO;
using System.Reflection;

namespace EchoDesk.Services;

/// <summary>
/// 启动快捷方式与目录布局管理（任务书修复七）。
///
/// 发布物采用"便携版三层结构"：
/// <code>
/// EchoDesk-v0.2.1/
/// ├── Start-EchoDesk-v0.2.1.lnk      ← 快捷方式在外层
/// ├── ui_state.json / snippets.json / results/   ← 用户数据在外层
/// └── app/                      ← 程序本体（exe、dll、lang、backend/）
///     └── EchoDesk.exe
/// </code>
///
/// 于是需要区分两个目录：
/// <list type="bullet">
/// <item><see cref="ProgramDirectory"/>：exe 所在目录（<c>app\</c>），放程序文件；</item>
/// <item><see cref="UserDataDirectory"/>：外层目录，放快捷方式、用户数据与results。</item>
/// </list>
///
/// 快捷方式通过 Windows Script Host 的 <c>WScript.Shell</c> COM 接口创建，
/// 不依赖任何第三方包（COM 调用走反射，避免引入 NuGet）。
/// </summary>
public static class ShortcutService
{
    private const string IconFileName = "EchoDesk.ico";
    private const string Description = "EchoDesk - Local AI Desktop Assistant";

    /// <summary>便携版布局中程序本体所在子目录名。</summary>
    private const string AppFolderName = "app";

    /// <summary>用户可用环境变量指定数据目录（企业环境把数据放到受管盘时用）。</summary>
    public const string DataDirectoryEnvironmentVariable = "ECHODESK_DATA";

    /// <summary>启动快捷方式文件名：<c>Start-EchoDesk-v0.2.1.lnk</c>（带版本号，多个版本可共存）。</summary>
    public static string ShortcutFileName
    {
        get
        {
            Version? version = typeof(ShortcutService).Assembly.GetName().Version;
            return version is null
                ? "Start-EchoDesk.lnk"
                : $"Start-EchoDesk-v{version.Major}.{version.Minor}.{version.Build}.lnk";
        }
    }

    /// <summary>程序所在目录（exe 所在目录；便携版布局下即 <c>app\</c>）。</summary>
    public static string ProgramDirectory
    {
        get
        {
            string? exe = MainExecutablePath;
            return exe is null
                ? AppContext.BaseDirectory
                : Path.GetDirectoryName(exe) ?? AppContext.BaseDirectory;
        }
    }

    /// <summary>
    /// 用户数据目录：快捷方式、<c>ui_state.json</c>、<c>snippets.json</c> 与
    /// 「results」都放这里（便携版布局下是外层目录）。
    ///
    /// 判定顺序（不依赖"当前工作目录"，避免用户从别处启动时把数据写到奇怪的位置）：
    /// 1. 环境变量 <c>ECHODESK_DATA</c>；
    /// 2. 程序目录名为 <c>app</c> 且存在父目录可写 → 父目录（便携版布局）；
    /// 3. 退化为程序目录（单层部署 / 开发时 <c>bin\Debug\</c>）。
    /// </summary>
    public static string UserDataDirectory
    {
        get
        {
            string program = ProgramDirectory;
            string? overrideDir = Environment.GetEnvironmentVariable(DataDirectoryEnvironmentVariable);
            if (!string.IsNullOrWhiteSpace(overrideDir))
            {
                try
                {
                    string full = Path.GetFullPath(overrideDir.Trim());
                    Directory.CreateDirectory(full);
                    return full;
                }
                catch
                {
                    // 环境变量指向不可写位置时忽略，继续走自动判定
                }
            }
            string? parent = Path.GetDirectoryName(program.TrimEnd(Path.DirectorySeparatorChar));
            if (parent is not null
                && string.Equals(Path.GetFileName(program.TrimEnd(Path.DirectorySeparatorChar)),
                                 AppFolderName, StringComparison.OrdinalIgnoreCase)
                && IsWritable(parent))
            {
                return parent;
            }
            return program;
        }
    }

    /// <summary>是否处于"程序本体在 app\ 子目录"的便携版布局。</summary>
    public static bool IsPortableLayout
        => !string.Equals(ProgramDirectory, UserDataDirectory, StringComparison.OrdinalIgnoreCase);

    /// <summary>目录是否可写（拿不到权限时按不可写处理，数据退回程序目录）。</summary>
    private static bool IsWritable(string directory)
    {
        try
        {
            if (!Directory.Exists(directory))
                return false;
            string probe = Path.Combine(directory, $".echodesk-write-{Guid.NewGuid():N}.tmp");
            using (File.Create(probe, 1, FileOptions.DeleteOnClose))
            {
            }
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>当前主程序可执行文件路径。</summary>
    public static string? MainExecutablePath
    {
        get
        {
            // 单文件发布下 Environment.ProcessPath 即入口 exe
            string? path = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(path) && File.Exists(path))
                return path;
            try
            {
                return Process.GetCurrentProcess().MainModule?.FileName;
            }
            catch
            {
                return null;
            }
        }
    }

    /// <summary>快捷方式完整路径（放在用户数据目录 / 便携版外层）。</summary>
    public static string ShortcutPath => Path.Combine(UserDataDirectory, ShortcutFileName);

    /// <summary>图标路径（随程序本体放在 <c>app\</c> 下）。</summary>
    public static string IconPath => Path.Combine(ProgramDirectory, IconFileName);

    public static bool Exists() => File.Exists(ShortcutPath);

    /// <summary>读取现有快捷方式的目标路径；不存在或读取失败时返回 null。</summary>
    public static string? ReadTarget()
    {
        if (!Exists())
            return null;
        try
        {
            object? shortcut = OpenShortcut(ShortcutPath);
            if (shortcut is null)
                return null;
            object? target = shortcut.GetType().InvokeMember(
                "TargetPath", BindingFlags.GetProperty, null, shortcut, null);
            return target as string;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>快捷方式是否存在且仍指向当前主程序（移动文件夹/改名后会失效）。</summary>
    public static bool IsValid()
    {
        if (!Exists())
            return false;
        string? target = ReadTarget();
        if (string.IsNullOrEmpty(target) || !File.Exists(target))
            return false;  // 目标已不存在 → 失效，需要重新创建
        string current = MainExecutablePath ?? "";
        if (current.Length == 0)
            return true;
        return string.Equals(Path.GetFullPath(target), Path.GetFullPath(current), StringComparison.OrdinalIgnoreCase)
               // 便携版移动位置后目标仍可用，但工作目录要跟着更新——算"需要重建"
               && HasCorrectWorkingDirectory();
    }

    /// <summary>读取快捷方式的工作目录；读取失败返回 null。</summary>
    public static string? ReadWorkingDirectory()
    {
        if (!Exists())
            return null;
        try
        {
            object? shortcut = OpenShortcut(ShortcutPath);
            if (shortcut is null)
                return null;
            object? dir = shortcut.GetType().InvokeMember(
                "WorkingDirectory", BindingFlags.GetProperty, null, shortcut, null);
            return dir as string;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>快捷方式的工作目录是否就是用户数据目录（修复七要求的"当前目录在外层"）。</summary>
    private static bool HasCorrectWorkingDirectory()
    {
        string? dir = ReadWorkingDirectory();
        if (string.IsNullOrEmpty(dir))
            return false;
        try
        {
            return string.Equals(Path.GetFullPath(dir), Path.GetFullPath(UserDataDirectory),
                                 StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// 在用户数据目录（便携版外层）创建/覆盖启动快捷方式，返回快捷方式完整路径。
    ///
    /// 工作目录固定设为用户数据目录：这样双击快捷方式启动时进程的当前目录就是外层，
    /// <c>results/</c> 等相对路径产物自然落在外面，不会埋进 <c>app\</c>。
    /// </summary>
    public static string Create()
    {
        string? exe = MainExecutablePath
            ?? throw new InvalidOperationException("无法取得主程序路径，无法创建快捷方式。");
        string linkPath = ShortcutPath;
        Directory.CreateDirectory(UserDataDirectory);

        object shortcut = OpenShortcut(linkPath)
            ?? throw new InvalidOperationException("WScript.Shell 不可用，无法创建快捷方式。");
        Type type = shortcut.GetType();

        type.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { exe });
        type.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut,
                          new object[] { UserDataDirectory });
        type.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { Description });
        type.InvokeMember("WindowStyle", BindingFlags.SetProperty, null, shortcut, new object[] { 1 });
        if (File.Exists(IconPath))
        {
            // 图标文件形如 "C:\dir\EchoDesk.ico,0"
            type.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut, new object[] { $"{IconPath},0" });
        }
        type.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
        return linkPath;
    }

    /// <summary>通过 WScript.Shell 打开（或新建）一个快捷方式对象。</summary>
    private static object? OpenShortcut(string linkPath)
    {
        Type? shellType = Type.GetTypeFromProgID("WScript.Shell");
        if (shellType is null)
            return null;
        object? shell = Activator.CreateInstance(shellType);
        if (shell is null)
            return null;
        return shellType.InvokeMember(
            "CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { linkPath });
    }
}
