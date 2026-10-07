// 用系统默认程序打开目录 / 链接（v0.2.2 第 2 批新增）。
//
// **为什么要有这个文件**：本批要补的三处（常规页的语言包目录、自动化页的结果目录、
// 模型页的模型目录）都要"用系统文件管理器打开一个目录"。三份复制粘贴的
// `Process.Start(UseShellExecute)` 会在"非 Windows 上该用什么参数"这类问题上慢慢分叉，
// 所以收口在这里。
//
// 刻意**不用 P/Invoke**（explorer.exe / open / xdg-open）：Avalonia 工程明确不引 Windows
// 专属能力（见 EchoDesk.Avalonia.csproj 头部说明），而 `UseShellExecute = true`
// 正是各平台"交给 shell 处理"的标准做法 —— Windows 资源管理器、macOS Finder、
// Linux xdg-open 都由它代劳。
using System;
using System.Diagnostics;
using System.IO;

namespace EchoDesk.Avalonia.Services;

public static class ShellOpen
{
    /// <summary>
    /// 打开目录（不存在时先创建——"目录还没有"时用户点按钮的意图通常是"带我去那儿"，
    /// 而不是"告诉我它不存在"）。失败只返回 false，不抛：调用方负责显示提示。
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
            // 权限不足 / 没有默认文件管理器 / 路径非法：调用方把 false 变成一句提示
            return false;
        }
    }
}
