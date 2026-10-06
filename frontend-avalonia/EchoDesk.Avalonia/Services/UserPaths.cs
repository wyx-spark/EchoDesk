// 各平台的数据/日志目录约定（任务书 任务三 的"用户数据目录按平台惯例"）。
//
// **与 WPF 版的口径（v0.2.2 修正了一处写错的注释）**：
// WPF 版走 `ShortcutService.UserDataDirectory`，判定顺序是 `ECHODESK_DATA` → 程序目录名是
// `app` 取父目录 → 程序目录本身；**它不用 `%APPDATA%`**（全仓库 `frontend/` 里没有任何
// `APPDATA` 相关代码，此前这里的注释写成"%APPDATA%\EchoDesk"是错的）。
// 跨平台版多了一段"非便携布局时按平台惯例回退"：macOS 用 Application Support、Linux 用 XDG。
// 两版在**发布物布局下结果一致**（都落在发布目录外层），差别只在"程序目录不叫 app"的兜底。
//
// 全部收口在这里，界面与后端各自只认这些方法，避免到处拼路径。
// 后端对应实现是 `backend/config.py` 的 `user_data_root()`，两处必须给出同一个答案
// （v0.2.2 修掉的正是"standalone 布局下两处不一致"的缺陷）。
using System;
using System.IO;

namespace EchoDesk.Avalonia.Services;

public static class UserPaths
{
    /// <summary>程序目录（可执行文件所在目录；打包布局里 config/models 都在它附近）。</summary>
    public static string ProgramDirectory => AppContext.BaseDirectory;

    /// <summary>
    /// 用户数据根目录。判定顺序（**与后端 config.user_data_root()、WPF ShortcutService.UserDataDirectory 三方一致**，
    /// 不一致会让"界面上传的结果文件"和"后端写的结果文件"落到两个地方）：
    ///   1. 环境变量 <c>ECHODESK_DATA</c>（便携/企业受管盘/测试）；
    ///   2. 程序目录名是 <c>app</c> → 取它上一级（发布物三层布局：<c>EchoDesk-v0.2.1\app\</c>）；
    ///   3. Windows → 程序目录（保持 WPF 版已发布行为不变，不制造回归）；
    ///      macOS → <c>~/Library/Application Support/EchoDesk</c>；
    ///      Linux → <c>$XDG_DATA_HOME/EchoDesk</c> 或 <c>~/.local/share/EchoDesk</c>
    ///      （非 Windows 的程序目录常在 /usr 下、不可写，必须走平台惯例）。
    /// </summary>
    public static string DataRoot
    {
        get
        {
            string? custom = Environment.GetEnvironmentVariable("ECHODESK_DATA");
            if (!string.IsNullOrWhiteSpace(custom))
                return custom!;

            string program = ProgramDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string? parent = Path.GetDirectoryName(program);
            if (parent is not null
                && string.Equals(Path.GetFileName(program), "app", StringComparison.OrdinalIgnoreCase))
                return parent;

            if (PlatformInfo.Current.IsWindows)
                return ProgramDirectory;

            string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            if (PlatformInfo.Current.Kind == PlatformKind.MacOS)
            {
                if (!string.IsNullOrEmpty(home))
                    return Path.Combine(home, "Library", "Application Support", "EchoDesk");
            }
            else
            {
                string? xdg = Environment.GetEnvironmentVariable("XDG_DATA_HOME");
                if (!string.IsNullOrWhiteSpace(xdg))
                    return Path.Combine(xdg!, "EchoDesk");
                if (!string.IsNullOrEmpty(home))
                    return Path.Combine(home, ".local", "share", "EchoDesk");
            }
            // 兜底：连 home 都取不到（容器/服务账户）时退回程序目录，
            // 至少保证界面设置能存下来，而不是每次启动都丢。
            return ProgramDirectory;
        }
    }

    /// <summary>界面设置（字号、主题、语言）存放文件。</summary>
    public static string UiStateFile => Path.Combine(DataRoot, "ui_state.json");

    /// <summary>界面语言包目录（可被 <c>ECHODESK_LANG_DIR</c> 覆盖，方便翻译者现场试）。</summary>
    public static string LangDirectory
    {
        get
        {
            string? custom = Environment.GetEnvironmentVariable("ECHODESK_LANG_DIR");
            if (!string.IsNullOrWhiteSpace(custom))
                return custom!;
            return Path.Combine(DataRoot, "lang", "ui");
        }
    }

    /// <summary>确保目录存在并返回它（供需要写文件的调用方使用）。</summary>
    public static string EnsureDirectory(string path)
    {
        try
        {
            Directory.CreateDirectory(path);
        }
        catch (Exception)
        {
            // 只读文件系统/权限不足：调用方写文件时会再报错，这里不吞掉信息而是让写入方处理
        }
        return path;
    }
}
