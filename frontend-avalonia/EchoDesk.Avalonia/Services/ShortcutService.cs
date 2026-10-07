// 启动快捷方式（v0.2.2 第 3 批，对齐 WPF 的 Services/ShortcutService.cs）。
//
// **为什么不能照抄 WPF 的实现**：WPF 版用 `WScript.Shell` COM 建 `.lnk`，
// 而跨平台工程显式关掉了 COM 互操作（`BuiltInComInteropSupport=false`，见
// EchoDesk.Avalonia.csproj 头部说明），也刻意不 P/Invoke 到 shell32。
// 所以这里按**各平台的常规做法**建一个启动器：
//
//   Windows  → 程序目录下的 `EchoDesk 启动.cmd`（双击即启动，可自行发送到桌面）
//   macOS    → `~/Desktop/EchoDesk.command`
//   Linux    → `~/.local/share/applications/echodesk.desktop`
//
// 这不是"少做一个功能"：三平台各有各的"启动入口"惯例，硬套 `.lnk` 反而是错的
// （Linux 上用户根本认不出 `.lnk`）。差异已在差异清单 §F4 登记为"平台实现差异，行为等价"。
//
// 与 WPF 版同口径的两条行为：
//   ① **只问一次**：用户拒绝后写 `ShortcutPromptDeclined`，不再反复弹；
//   ② **失效要能发现**：已存在的启动器指向的可执行文件不见了 → 提示重建。
using System;
using System.IO;
using System.Runtime.InteropServices;

namespace EchoDesk.Avalonia.Services;

public static class ShortcutService
{
    /// <summary>本平台启动器的文件名。</summary>
    public static string FileName => PlatformInfo.Current.Kind switch
    {
        PlatformKind.MacOS => "EchoDesk.command",
        PlatformKind.Linux => "echodesk.desktop",
        _ => "EchoDesk 启动.cmd",
    };

    /// <summary>启动器的完整路径（Windows/macOS 放程序目录，Linux 放 applications 目录）。</summary>
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

    /// <summary>可执行文件的真实路径（启动器要指向它）。</summary>
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

    /// <summary>启动器是否已经存在。</summary>
    public static bool Exists() => File.Exists(ShortcutPath);

    /// <summary>
    /// 启动器是否**仍然有效**：文件在，且它指向的可执行文件也还在。
    /// 用户把程序目录改名/搬家后，WPF 版的 `.lnk` 会变成死链 —— 这里用同一判据。
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

    /// <summary>创建启动器。返回它的路径；失败抛异常（调用方负责显示原因）。</summary>
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
                // .desktop 是 Linux 上唯一被桌面环境认得的"启动入口"
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
                // .command 双击即可执行，等价于 Windows 上双击快捷方式
                body = "#!/bin/sh\n"
                       + $"cd \"{UserPaths.ProgramDirectory}\" || exit 1\n"
                       + $"exec \"{exe}\"\n";
                break;
            default:
                // Windows：不写 .lnk（需要 COM），改成一个双击即启动的批处理
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

    /// <summary>给脚本加可执行位（Linux/macOS 上不加就双击不了）。</summary>
    private static void MakeExecutable(string path)
    {
        try
        {
            if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
                File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
        catch (Exception)
        {
            // 某些文件系统不支持 Unix 权限位：忽略，用户仍可 chmod +x
        }
    }
}
