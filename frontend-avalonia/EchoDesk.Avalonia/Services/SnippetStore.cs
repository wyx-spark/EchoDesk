// 常用语（snippets.json）—— 与 WPF 版 frontend/Utilities/SnippetManager.cs 同一份数据格式。
//
// 跨平台版的差别只在**路径**：WPF 版用 ShortcutService.UserDataDirectory，这里走
// UserPaths.DataRoot —— 两者在发布物布局下结果相同（都是发布目录外层），
// 且都与后端 `config.user_data_root()` 的口径一致，否则用户会在两个地方各看到一份"常用语"。
// （v0.2.2 修正了此处把 Windows 写成 `%APPDATA%` 的注释：代码里没有这条路径。）
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace EchoDesk.Avalonia.Services;

/// <summary>一条用户自定义的常用语。</summary>
public sealed class Snippet
{
    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("text")]
    public string Text { get; set; } = "";
}

/// <summary>常用语的读写。文件不存在时给空列表，损坏时给空列表并记原因（不让界面起不来）。</summary>
public sealed class SnippetStore
{
    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    private readonly string _path;

    public SnippetStore()
    {
        _path = Path.Combine(UserPaths.DataRoot, "snippets.json");
        Load();
    }

    public List<Snippet> Items { get; private set; } = new();

    /// <summary>最近一次读写失败的原因（界面可据此提示"存不下来"，而不是假装保存成功）。</summary>
    public string LastError { get; private set; } = "";

    public void Load()
    {
        LastError = "";
        try
        {
            if (!File.Exists(_path))
            {
                Items = new List<Snippet>();
                return;
            }
            string text = File.ReadAllText(_path);
            Items = string.IsNullOrWhiteSpace(text)
                ? new List<Snippet>()
                : JsonSerializer.Deserialize<List<Snippet>>(text) ?? new List<Snippet>();
        }
        catch (Exception ex)
        {
            LastError = ex.Message;
            Items = new List<Snippet>();
        }
    }

    private bool Save()
    {
        try
        {
            UserPaths.EnsureDirectory(UserPaths.DataRoot);
            File.WriteAllText(_path, JsonSerializer.Serialize(Items, Options),
                new System.Text.UTF8Encoding(false));
            return true;
        }
        catch (Exception ex)
        {
            // 保存失败不影响本次会话使用，但必须让调用方知道（界面据此提示）。
            LastError = ex.Message;
            return false;
        }
    }

    /// <summary>新增；名称已存在时返回 false（与 WPF 版同语义，界面按"重名"提示）。</summary>
    public bool Add(string name, string text)
    {
        if (string.IsNullOrWhiteSpace(name))
            return false;
        if (Items.Any(s => string.Equals(s.Name, name, StringComparison.Ordinal)))
            return false;
        Items.Add(new Snippet { Name = name, Text = text });
        Save();
        return true;
    }

    public bool Remove(string name)
    {
        Snippet? snippet = Items.FirstOrDefault(s => string.Equals(s.Name, name, StringComparison.Ordinal));
        if (snippet is null)
            return false;
        Items.Remove(snippet);
        Save();
        return true;
    }
}
