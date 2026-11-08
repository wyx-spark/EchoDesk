// [EN] EchoDesk WPF frontend module: frontend/Utilities/SnippetManager.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Utilities/SnippetManager.cs。类型与成员说明见下方注释。
using System.IO;
using System.Text.Json;
using EchoDesk.Services;

namespace EchoDesk.Utilities;

/// <summary>
/// [EN] A user-defined snippet.
/// [ZH] 一条用户自定义的常用语。
/// </summary>
public sealed class Snippet
{
    public string Name { get; set; } = "";
    public string Text { get; set; } = "";
}

/// <summary>
/// [EN] Manages user-defined snippets (<c>snippets.json</c>; after fix seven the file lives in
/// [EN] the user data directory, which is the outer folder in the portable layout, separate
/// [EN] from the program files in <c>app\</c>).
/// [ZH] 管理用户自定义常用语（<c>snippets.json</c>，修复七后存放在用户数据目录，
/// [ZH] 便携版布局下即外层目录，与 <c>app\</c> 里的程序文件分开）。
/// </summary>
public sealed class SnippetManager
{
    private readonly string _path;
    public List<Snippet> Items { get; private set; } = new();

    public SnippetManager()
    {
        _path = Path.Combine(ShortcutService.UserDataDirectory, "snippets.json");
        Load();
    }

    private void Load()
    {
        try
        {
            if (File.Exists(_path))
                Items = JsonSerializer.Deserialize<List<Snippet>>(File.ReadAllText(_path)) ?? new();
        }
        catch
        {
            Items = new();
        }
    }

    private void Save()
    {
        try
        {
            File.WriteAllText(_path, JsonSerializer.Serialize(Items, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch
        {
            // [EN] A failed save does not affect use (the change applies to this session only).
            // [ZH] 保存失败不影响使用（仅本次会话生效）
        }
    }

    /// <summary>
    /// [EN] Adds a snippet; returns false when the name already exists.
    /// [ZH] 新增常用语；名称已存在时返回 false。
    /// </summary>
    public bool Add(string name, string text)
    {
        if (Items.Any(s => s.Name == name))
            return false;
        Items.Add(new Snippet { Name = name, Text = text });
        Save();
        return true;
    }

    public bool Remove(string name)
    {
        var snippet = Items.FirstOrDefault(s => s.Name == name);
        if (snippet is null)
            return false;
        Items.Remove(snippet);
        Save();
        return true;
    }
}
