using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace EchoDesk.Services;

/// <summary>
/// 前端本地界面状态（<c>ui_state.json</c>，放在程序目录下）。
///
/// 记录窗口大小/位置/最大化、字号、主题，以及"已拒绝创建快捷方式"等一次性询问结果，
/// 这样用户拒绝过的询问不会每次启动都重复弹出。
/// </summary>
public sealed class AppSettings
{
    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
    };

    /// <summary>用户已拒绝创建启动快捷方式（不再重复询问）。</summary>
    public bool ShortcutPromptDeclined { get; set; }

    /// <summary>是否已完成首次启动引导（只展示一次）。</summary>
    public bool FirstRunDone { get; set; }

    // ---------- 窗口状态记忆 ----------
    public double WindowWidth { get; set; }
    public double WindowHeight { get; set; }
    public double WindowLeft { get; set; } = double.NaN;
    public double WindowTop { get; set; } = double.NaN;
    public bool WindowMaximized { get; set; }

    // ---------- 界面偏好 ----------
    public double FontSize { get; set; } = 14;
    public bool DarkMode { get; set; } = true;

    private static string FilePath
    {
        get
        {
            string dir = ShortcutService.ProgramDirectory;
            return Path.Combine(dir, "ui_state.json");
        }
    }

    /// <summary>读取本地界面状态；文件缺失或损坏时返回默认值。</summary>
    public static AppSettings Load()
    {
        try
        {
            string path = FilePath;
            if (!File.Exists(path))
                return new AppSettings();
            string json = File.ReadAllText(path);
            return JsonSerializer.Deserialize<AppSettings>(json, Options) ?? new AppSettings();
        }
        catch
        {
            return new AppSettings();  // 状态文件损坏不应影响启动
        }
    }

    /// <summary>写回本地界面状态（失败静默，不打断使用）。</summary>
    public void Save()
    {
        try
        {
            File.WriteAllText(FilePath, JsonSerializer.Serialize(this, Options));
        }
        catch
        {
            // 只读目录等场景下忽略
        }
    }
}
