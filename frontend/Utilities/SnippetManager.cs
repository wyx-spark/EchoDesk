using System.IO;
using System.Text.Json;

namespace EchoDesk.Utilities;

/// <summary>一条用户自定义的常用语。</summary>
public sealed class Snippet
{
    public string Name { get; set; } = "";
    public string Text { get; set; } = "";
}

/// <summary>
/// 管理用户自定义常用语（snippets.json 存放在 exe 同目录）。
/// </summary>
public sealed class SnippetManager
{
    private readonly string _path;
    public List<Snippet> Items { get; private set; } = new();

    public SnippetManager()
    {
        string dir = Path.GetDirectoryName(Environment.ProcessPath) ?? ".";
        _path = Path.Combine(dir, "snippets.json");
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
            // 保存失败不影响使用（仅本次会话生效）
        }
    }

    /// <summary>新增常用语；名称已存在时返回 false。</summary>
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
