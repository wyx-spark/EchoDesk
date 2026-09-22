using System.Diagnostics;
using System.IO;
using System.Reflection;

namespace EchoDesk.Services;

/// <summary>
/// 启动快捷方式管理：检测、创建与失效判断。
///
/// 通过 Windows Script Host 的 <c>WScript.Shell</c> COM 接口创建 .lnk，
/// 指向主程序本体，工作目录设为程序目录，图标使用程序目录下的 EchoDesk.ico。
/// 不依赖任何第三方包（COM 调用走反射，避免引入 NuGet）。
/// </summary>
public static class ShortcutService
{
    /// <summary>快捷方式文件名（放在程序目录内，便于用户直接双击）。</summary>
    public const string ShortcutFileName = "EchoDesk 启动.lnk";

    private const string IconFileName = "EchoDesk.ico";
    private const string Description = "EchoDesk - Local AI Desktop Assistant";

    /// <summary>程序所在目录（快捷方式与图标都放在这里）。</summary>
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

    public static string ShortcutPath => Path.Combine(ProgramDirectory, ShortcutFileName);

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
        return current.Length == 0
            || string.Equals(Path.GetFullPath(target), Path.GetFullPath(current), StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>在程序目录下创建/覆盖启动快捷方式，返回快捷方式完整路径。</summary>
    public static string Create()
    {
        string? exe = MainExecutablePath
            ?? throw new InvalidOperationException("无法取得主程序路径，无法创建快捷方式。");
        string linkPath = ShortcutPath;

        object shortcut = OpenShortcut(linkPath)
            ?? throw new InvalidOperationException("WScript.Shell 不可用，无法创建快捷方式。");
        Type type = shortcut.GetType();

        type.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { exe });
        type.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { ProgramDirectory });
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
