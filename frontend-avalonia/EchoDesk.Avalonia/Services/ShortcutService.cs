// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/ShortcutService.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/ShortcutService.cs。类型与成员说明见下方注释。
// [EN] Startup shortcut (v0.2.2, batch 3; aligned with Services/ShortcutService.cs in WPF).
// [EN]
// [EN] **Why the WPF implementation cannot be copied**: the WPF version creates `.lnk` files
// [EN] through the `WScript.Shell` COM object, whereas the cross-platform project explicitly
// [EN] disables COM interop (`BuiltInComInteropSupport=false`, see the header of
// [EN] EchoDesk.Avalonia.csproj) and deliberately avoids P/Invoke into shell32.
// [EN] This code therefore creates a launcher following the **conventions of each platform**:
// [EN]
// [EN]   Windows  → the `EchoDesk` start-up `.cmd` in the program directory (double-click to
// [EN]              start; the user may send it to the desktop)
// [EN]   macOS    → `~/Desktop/EchoDesk.command`
// [EN]   Linux    → `~/.local/share/applications/echodesk.desktop`
// [EN]
// [EN] This is not "one feature fewer": the three platforms each have their own convention for
// [EN] a startup entry point, and forcing `.lnk` onto them would be wrong (on Linux, users
// [EN] would not even recognize a `.lnk` file). The difference is recorded in the difference
// [EN] list §F4 as "platform implementation difference, equivalent behavior".
// [EN]
// [EN] Two behaviors shared with the WPF version:
// [EN]   1. **Ask only once**: after the user declines, `ShortcutPromptDeclined` is written so
// [EN]      that the prompt never appears again.
// [EN]   2. **Broken launchers must be detectable**: when the executable a launcher points to
// [EN]      is gone, prompt to recreate it.
// [ZH] 启动快捷方式（v0.2.2 第 3 批，对齐 WPF 的 Services/ShortcutService.cs）。
// [ZH]
// [ZH] **为什么不能照抄 WPF 的实现**：WPF 版用 `WScript.Shell` COM 创建 `.lnk`，
// [ZH] 而跨平台工程显式关闭了 COM 互操作（`BuiltInComInteropSupport=false`，见
// [ZH] EchoDesk.Avalonia.csproj 头部说明），也刻意不 P/Invoke 到 shell32。
// [ZH] 所以这里按**各平台的常规做法**创建一个启动器：
// [ZH]
// [ZH]   Windows  → 程序目录下的 `EchoDesk 启动.cmd`（双击即启动，可自行发送到桌面）
// [ZH]   macOS    → `~/Desktop/EchoDesk.command`
// [ZH]   Linux    → `~/.local/share/applications/echodesk.desktop`
// [ZH]
// [ZH] 这不是“少做一个功能”：三平台各有各的“启动入口”惯例，硬套 `.lnk` 反而是错的
// [ZH] （Linux 上用户根本认不出 `.lnk`）。差异已在差异清单 §F4 登记为“平台实现差异，
// [ZH] 行为等价”。
// [ZH]
// [ZH] 与 WPF 版同口径的两条行为：
// [ZH]   ① **只问一次**：用户拒绝后写 `ShortcutPromptDeclined`，不再反复弹；
// [ZH]   ② **失效要能发现**：已存在的启动器指向的可执行文件不见了 → 提示重建。
using System;
using System.IO;
using System.Runtime.InteropServices;

namespace EchoDesk.Avalonia.Services;

public static class ShortcutService
{
    /// <summary>
    /// [EN] File name of the launcher on this platform.
    /// [ZH] 本平台启动器的文件名。
    /// </summary>
    public static string FileName => PlatformInfo.Current.Kind switch
    {
        PlatformKind.MacOS => "EchoDesk.command",
        PlatformKind.Linux => "echodesk.desktop",
        _ => "EchoDesk 启动.cmd",
    };

    /// <summary>
    /// [EN] Full path of the launcher (the program directory on Windows/macOS, the
    /// [EN] applications directory on Linux).
    /// [ZH] 启动器的完整路径（Windows/macOS 放程序目录，Linux 放 applications 目录）。
    /// </summary>
    public static string ShortcutPath
    {
        get
        {
            if (PlatformInfo.Current.Kind == PlatformKind.Linux)
            {
                string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                string dir = string.IsNullOrEmpty(home)
                    ? UserPaths.ProgramDirectory
                    : Path.Combine(home, ".local", "share", "applications");
                return Path.Combine(dir, FileName);
            }
            if (PlatformInfo.Current.Kind == PlatformKind.MacOS)
            {
                string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                if (!string.IsNullOrEmpty(home))
                {
                    string desktop = Path.Combine(home, "Desktop");
                    if (Directory.Exists(desktop))
                        return Path.Combine(desktop, FileName);
                }
            }
            return Path.Combine(UserPaths.ProgramDirectory, FileName);
        }
    }

    /// <summary>
    /// [EN] Real path of the executable (the launcher has to point to it).
    /// [ZH] 可执行文件的真实路径（启动器要指向它）。
    /// </summary>
    public static string ExecutablePath
    {
        get
        {
            string exe = Environment.ProcessPath ?? "";
            if (!string.IsNullOrEmpty(exe) && File.Exists(exe))
                return exe;
            string candidate = Path.Combine(UserPaths.ProgramDirectory, "EchoDesk");
            if (File.Exists(candidate))
                return candidate;
            return Path.Combine(UserPaths.ProgramDirectory, "EchoDesk.exe");
        }
    }

    /// <summary>
    /// [EN] Whether the launcher already exists.
    /// [ZH] 启动器是否已经存在。
    /// </summary>
    public static bool Exists() => File.Exists(ShortcutPath);

    /// <summary>
    /// [EN] Whether the launcher is **still valid**: the file exists, and the executable it
    /// [EN] points to still exists as well.
    /// [EN] After the user renames or moves the program directory, the WPF version's `.lnk`
    /// [EN] becomes a dead link — the same criterion is used here.
    /// [ZH] 启动器是否**仍然有效**：文件在，且它指向的可执行文件也还在。
    /// [ZH] 用户把程序目录改名/搬家后，WPF 版的 `.lnk` 会变成死链 —— 这里用同一判据。
    /// </summary>
    public static bool IsValid()
    {
        if (!Exists())
            return false;
        try
        {
            string text = File.ReadAllText(ShortcutPath);
            return text.Contains(ExecutablePath, StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception)
        {
            return false;
        }
    }

    /// <summary>
    /// [EN] Create the launcher. Returns its path; a failure throws (the caller displays the
    /// [EN] reason).
    /// [ZH] 创建启动器。返回它的路径；失败抛异常（调用方负责显示原因）。
    /// </summary>
    public static string Create()
    {
        string path = ShortcutPath;
        string? dir = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(dir))
            Directory.CreateDirectory(dir);

        string exe = ExecutablePath;
        string body;
        switch (PlatformInfo.Current.Kind)
        {
            case PlatformKind.Linux:
                // [EN] The `.desktop` file is the only "startup entry" recognized by desktop
                // [EN] environments on Linux.
                // [ZH] `.desktop` 是 Linux 上唯一被桌面环境认得的“启动入口”。
                body = "[Desktop Entry]\n"
                       + "Type=Application\n"
                       + "Name=EchoDesk\n"
                       + "Comment=Local-first AI desktop assistant\n"
                       + $"Exec=\"{exe}\"\n"
                       + $"Path=\"{UserPaths.ProgramDirectory}\"\n"
                       + "Terminal=false\n"
                       + "Categories=Utility;\n";
                break;
            case PlatformKind.MacOS:
                // [EN] A `.command` file runs on double-click, equivalent to double-clicking a
                // [EN] shortcut on Windows.
                // [ZH] `.command` 双击即可执行，等价于 Windows 上双击快捷方式。
                body = "#!/bin/sh\n"
                       + $"cd \"{UserPaths.ProgramDirectory}\" || exit 1\n"
                       + $"exec \"{exe}\"\n";
                break;
            default:
                // [EN] Windows: no `.lnk` file is written (that needs COM); a batch file that
                // [EN] starts on double-click is used instead.
                // [ZH] Windows：不写 `.lnk`（需要 COM），改成一个双击即启动的批处理。
                body = "@echo off\r\n"
                       + $"cd /d \"{UserPaths.ProgramDirectory}\"\r\n"
                       + $"start \"\" \"{exe}\"\r\n";
                break;
        }
        File.WriteAllText(path, body, new System.Text.UTF8Encoding(false));
        if (PlatformInfo.Current.Kind != PlatformKind.Windows && !OperatingSystem.IsWindows())
            MakeExecutable(path);
        return path;
    }

    /// <summary>
    /// [EN] Add the executable bit to the script (without it, double-clicking does not work on
    /// [EN] Linux/macOS).
    /// [ZH] 给脚本加可执行位（Linux/macOS 上不加就双击不了）。
    /// </summary>
    private static void MakeExecutable(string path)
    {
        try
        {
            if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
                File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
        catch (Exception)
        {
            // [EN] Some file systems do not support Unix permission bits: ignore the failure,
            // [EN] since the user can still run chmod +x.
            // [ZH] 某些文件系统不支持 Unix 权限位：忽略，用户仍可 chmod +x。
        }
    }
}
