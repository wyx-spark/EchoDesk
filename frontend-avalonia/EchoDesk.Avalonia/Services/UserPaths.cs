// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/UserPaths.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/UserPaths.cs。类型与成员说明见下方注释。
// [EN] Platform-specific data and log directory conventions.
//
// The WPF frontend uses `ShortcutService.UserDataDirectory`, which resolves
// `ECHODESK_DATA`, then the parent of an `app` program directory, then the
// program directory itself. It does not use `%APPDATA%`.
//
// The cross-platform frontend adds platform fallbacks for non-portable layouts:
// Application Support on macOS and XDG directories on Linux. Both frontends
// resolve to the same location in the published layout.
//
// All path construction is centralized here. The backend counterpart is
// `user_data_root()` in `backend/config.py`; both must return the same answer.
// [ZH] 各平台的数据与日志目录约定。
//
// WPF 前端使用 `ShortcutService.UserDataDirectory`，依次解析 `ECHODESK_DATA`、
// 名为 `app` 的程序目录的父目录，最后是程序目录本身。它不使用 `%APPDATA%`。
//
// 跨平台前端为非便携布局增加了平台回退：macOS 使用 Application Support，Linux
// 使用 XDG 目录。两种前端在发布布局下解析到同一位置。
//
// 所有路径构造集中于此。后端对应实现为 `backend/config.py` 的 `user_data_root()`，
// 两者必须返回一致结果。
using System;
using System.IO;

namespace EchoDesk.Avalonia.Services;

public static class UserPaths
{
    /// <summary>
    /// [EN] Program directory containing the executable. In a packaged layout,
    /// config and models are nearby.
    /// [ZH] 可执行文件所在目录。在打包布局中，config 与 models 位于其附近。
    /// </summary>
    public static string ProgramDirectory => AppContext.BaseDirectory;

    /// <summary>
    /// [EN] User data root. Resolution order, kept consistent with the backend
    /// `user_data_root()` and WPF `ShortcutService.UserDataDirectory`:
    ///   1. the <c>ECHODESK_DATA</c> environment variable;
    ///   2. if the program directory is named <c>app</c>, its parent;
    ///   3. Windows: the program directory;
    ///      macOS: <c>~/Library/Application Support/EchoDesk</c>;
    ///      Linux: <c>$XDG_DATA_HOME/EchoDesk</c> or
    ///      <c>~/.local/share/EchoDesk</c>.
    ///
    /// [ZH] 用户数据根目录。判定顺序与后端 `user_data_root()` 及 WPF
    /// `ShortcutService.UserDataDirectory` 保持一致：
    ///   1. 环境变量 <c>ECHODESK_DATA</c>；
    ///   2. 程序目录名为 <c>app</c> 时取其父目录；
    ///   3. Windows 使用程序目录；macOS 使用
    ///      <c>~/Library/Application Support/EchoDesk</c>；Linux 使用
    ///      <c>$XDG_DATA_HOME/EchoDesk</c> 或 <c>~/.local/share/EchoDesk</c>。
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
            // [EN] Fallback to the program directory when the home directory is
            // [EN] unavailable, such as in containers or service accounts, so UI
            // [EN] settings can still be persisted.
            // [ZH] 在主目录不可用时（如容器或服务账户）回退到程序目录，确保界面设置
            // [ZH] 仍能持久化。
            return ProgramDirectory;
        }
    }

    /// <summary>
    /// [EN] File storing UI settings: font size, theme, and language.
    /// [ZH] 存放界面设置（字号、主题、语言）的文件。
    /// </summary>
    public static string UiStateFile => Path.Combine(DataRoot, "ui_state.json");

    /// <summary>
    /// [EN] UI language-pack directory, overridable through
    /// <c>ECHODESK_LANG_DIR</c> for translator testing.
    ///
    /// Read-only fallback (v0.2.5 P0): when the data directory has no <c>lang/ui</c>
    /// (macOS <c>.app</c> and Linux AppImage keep their language packs as read-only
    /// packaged resources), use <c>&lt;program directory&gt;/lang/ui</c> instead.
    /// Without it the English UI silently degrades to the built-in Chinese fallback
    /// strings on those layouts. The data directory always wins when it does exist, so
    /// translators can still drop a pack there and see it immediately.
    ///
    /// [ZH] 界面语言包目录，可通过 <c>ECHODESK_LANG_DIR</c> 覆盖，便于翻译者测试。
    ///
    /// 只读回落（v0.2.5 P0）：数据目录下没有 <c>lang/ui</c> 时（macOS <c>.app</c> 与
    /// Linux AppImage 的语言包是随包的只读资源），改用
    /// <c>&lt;程序目录&gt;/lang/ui</c>。没有这条回落，这些形态下的英文界面会静默退化
    /// 成内置中文兜底文案。数据目录存在时始终优先，翻译者仍可把语言包放进去立即生效。
    /// </summary>
    public static string LangDirectory
    {
        get
        {
            string? custom = Environment.GetEnvironmentVariable("ECHODESK_LANG_DIR");
            if (!string.IsNullOrWhiteSpace(custom))
                return custom!;

            string dataDir = Path.Combine(DataRoot, "lang", "ui");
            if (Directory.Exists(dataDir))
                return dataDir;

            // [EN] Program directory fallback: in a packaged .app this is
            // [EN] Contents/MacOS; in the portable tree it is the directory that ships
            // [EN] alongside app/lang/ui.
            // [ZH] 程序目录回落：打包后的 .app 里就是 Contents/MacOS；便携版里则是
            // [ZH] 与 app/lang/ui 同级的程序目录。
            string programDir = Path.Combine(ProgramDirectory, "lang", "ui");
            if (Directory.Exists(programDir))
                return programDir;

            // [EN] Neither exists: keep the historical answer so callers that display or
            // [EN] open the path still point at the user data directory.
            // [ZH] 两者都不存在：沿用历史答案，让展示/打开该路径的调用方仍指向用户数据目录。
            return dataDir;
        }
    }

    /// <summary>
    /// [EN] Ensure a directory exists and return its path for callers that write
    /// files.
    /// [ZH] 确保目录存在并返回路径，供需要写入文件的调用方使用。
    /// </summary>
    public static string EnsureDirectory(string path)
    {
        try
        {
            Directory.CreateDirectory(path);
        }
        catch (Exception)
        {
            // [EN] Read-only file systems and permission failures are left for
            // [EN] the writing caller to report.
            // [ZH] 只读文件系统与权限失败留给写入方报告。
        }
        return path;
    }
}
