// 界面设置（移植自 frontend/Services/AppSettings.cs，任务书 任务三 的"用户数据目录按平台惯例"）。
//
// WPF 版把 ui_state.json 写在"程序目录的外层"（修复七：便携版不希望用户数据跟着程序目录跑）。
// 跨平台版改为交给 UserPaths.DataRoot 决定（Windows %APPDATA%、macOS Application Support、
// Linux XDG），并保留 ECHODESK_DATA 覆盖 —— 自检/测试就靠它把设置写到临时目录里。
//
// 只存"界面自己"的状态：语言、主题、字号、emoji 偏好、窗口尺寸。
// 模型、检索、Agent 这些**业务**配置留在后端的 config.json，前端不重复存一份，
// 避免两边不一致（WPF 版也是这个分工）。
using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace EchoDesk.Avalonia.Services;

public sealed class AppSettings
{
    /// <summary>语言："auto" 或具体标签（zh-Hans / zh-Hant / en）。</summary>
    [JsonPropertyName("language")]
    public string Language { get; set; } = "auto";

    /// <summary>主题："auto" / "light" / "dark"。</summary>
    [JsonPropertyName("theme")]
    public string Theme { get; set; } = "auto";

    /// <summary>聊天区字号（px，界面上的 A- / A+ 也改它）。</summary>
    [JsonPropertyName("font_size")]
    public double FontSize { get; set; } = 14;

    /// <summary>emoji 偏好："auto" / "always" / "never"。</summary>
    [JsonPropertyName("emoji")]
    public string EmojiPreference { get; set; } = "auto";

    [JsonPropertyName("window_width")]
    public double WindowWidth { get; set; } = 1080;

    [JsonPropertyName("window_height")]
    public double WindowHeight { get; set; } = 760;

    /// <summary>界面语言首选（原型阶段不落盘，仅内存）；null 表示"还没写成功"。</summary>
    [JsonIgnore]
    public string? LastLoadError { get; private set; }

    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    /// <summary>读设置。文件不存在/损坏都返回默认值并记下原因（不让界面起不来）。</summary>
    public static AppSettings Load()
    {
        var settings = new AppSettings();
        string path = UserPaths.UiStateFile;
        try
        {
            if (!File.Exists(path))
                return settings;
            string text = File.ReadAllText(path);
            if (string.IsNullOrWhiteSpace(text))
                return settings;
            AppSettings? parsed = JsonSerializer.Deserialize<AppSettings>(text);
            if (parsed is null)
                return settings;
            parsed.Clamp();
            return parsed;
        }
        catch (Exception ex)
        {
            settings.LastLoadError = ex.Message;
            return settings;
        }
    }

    /// <summary>写设置。失败不抛（只读目录、权限不足等）—— 界面不该因为存不了偏好而崩。</summary>
    public bool Save()
    {
        try
        {
            Clamp();
            UserPaths.EnsureDirectory(UserPaths.DataRoot);
            string json = JsonSerializer.Serialize(this, Options);
            File.WriteAllText(UserPaths.UiStateFile, json, new System.Text.UTF8Encoding(false));
            return true;
        }
        catch (Exception ex)
        {
            LastLoadError = ex.Message;
            return false;
        }
    }

    /// <summary>把越界的值拉回可用范围（手改 JSON 写坏了也不至于让界面没法用）。</summary>
    private void Clamp()
    {
        if (double.IsNaN(FontSize) || FontSize < 10 || FontSize > 28)
            FontSize = 14;
        if (double.IsNaN(WindowWidth) || WindowWidth < 720)
            WindowWidth = 1080;
        if (double.IsNaN(WindowHeight) || WindowHeight < 520)
            WindowHeight = 760;
        Theme = (Theme ?? "auto").Trim().ToLowerInvariant() switch
        {
            "light" => "light",
            "dark" => "dark",
            _ => "auto",
        };
        EmojiPreference = (EmojiPreference ?? "auto").Trim().ToLowerInvariant() switch
        {
            "always" or "on" or "true" => "always",
            "never" or "off" or "false" => "never",
            _ => "auto",
        };
        if (string.IsNullOrWhiteSpace(Language))
            Language = "auto";
    }
}
