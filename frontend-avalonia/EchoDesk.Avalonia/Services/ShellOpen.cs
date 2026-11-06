// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/ShellOpen.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/ShellOpen.cs。类型与成员说明见下方注释。
// [EN] Open a directory or link with the system default application.
//
// This helper centralizes the three places that open a directory in the system
// file manager: the language-pack directory, the automation results directory,
// and the model directory. Duplicating `Process.Start(UseShellExecute)` would
// drift across platforms.
//
// P/Invoke is deliberately avoided. `UseShellExecute = true` delegates to the
// platform shell: Windows Explorer, macOS Finder, or Linux xdg-open.
// [ZH] 使用系统默认程序打开目录或链接。
//
// 该辅助类统一了三处需要在系统文件管理器中打开目录的位置：语言包目录、自动化结果
// 目录与模型目录。重复的 `Process.Start(UseShellExecute)` 会在不同平台间产生差异。
//
// 有意不使用 P/Invoke。`UseShellExecute = true` 将操作交给平台 shell：Windows
// 资源管理器、macOS Finder 或 Linux xdg-open。
using System;
using System.Diagnostics;
using System.IO;

namespace EchoDesk.Avalonia.Services;

public static class ShellOpen
{
    /// <summary>
    /// [EN] Open a directory, creating it first when missing. A user who opens a
    /// not-yet-created directory usually wants to be taken there. Failures return
    /// false instead of throwing; the caller displays the message.
    /// [ZH] 打开目录；不存在时先创建。用户打开尚未创建的目录时，通常希望直接前往
    /// 该位置。失败返回 false 而不抛出异常；由调用方显示提示。
    /// </summary>
    public static bool TryOpenDirectory(string? dir)
    {
        if (string.IsNullOrWhiteSpace(dir))
            return false;
        try
        {
            Directory.CreateDirectory(dir);
            Process.Start(new ProcessStartInfo { FileName = dir, UseShellExecute = true });
            return true;
        }
        catch (Exception)
        {
            // [EN] Insufficient permissions, no default file manager, or an
            // [EN] invalid path: the caller turns false into a user-facing notice.
            // [ZH] 权限不足、缺少默认文件管理器或路径非法：调用方将 false 转换为
            // [ZH] 用户提示。
            return false;
        }
    }
}
