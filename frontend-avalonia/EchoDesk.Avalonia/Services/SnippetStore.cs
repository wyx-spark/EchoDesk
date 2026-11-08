// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/SnippetStore.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/SnippetStore.cs。类型与成员说明见下方注释。
// [EN] Snippets (snippets.json) — the same data format as
// [EN] frontend/Utilities/SnippetManager.cs in the WPF version.
// [EN]
// [EN] The only cross-platform difference is the **path**: the WPF version uses
// [EN] ShortcutService.UserDataDirectory while this one uses UserPaths.DataRoot — both yield
// [EN] the same result under the release layout (both are the outer release directory), and
// [EN] both agree with the backend's `config.user_data_root()`. Otherwise users would see one
// [EN] copy of their "snippets" in each of the two places.
// [EN] (v0.2.2 corrected the earlier comment that wrote `%APPDATA%` for Windows: that path
// [EN] does not exist in the code.)
// [ZH] 常用语（snippets.json）—— 与 WPF 版 frontend/Utilities/SnippetManager.cs 同一份数据格式。
// [ZH]
// [ZH] 跨平台版的差别只在**路径**：WPF 版用 ShortcutService.UserDataDirectory，这里走
// [ZH] UserPaths.DataRoot —— 两者在发布物布局下结果相同（都是发布目录外层），
// [ZH] 且都与后端 `config.user_data_root()` 的口径一致，否则用户会在两个地方各看到
// [ZH] 一份“常用语”。
// [ZH] （v0.2.2 修正了此处把 Windows 写成 `%APPDATA%` 的注释：代码里没有这条路径。）
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace EchoDesk.Avalonia.Services;

/// <summary>
/// [EN] A user-defined snippet.
/// [ZH] 一条用户自定义的常用语。
/// </summary>
public sealed class Snippet
{
    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("text")]
    public string Text { get; set; } = "";
}

/// <summary>
/// [EN] Reading and writing of snippets. A missing file yields an empty list; a corrupt file
/// [EN] yields an empty list plus a recorded reason (so that the UI can still start).
/// [ZH] 常用语的读写。文件不存在时给空列表，损坏时给空列表并记原因（不让界面起不来）。
/// </summary>
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

    /// <summary>
    /// [EN] Reason of the most recent read/write failure (the UI can use it to report "could
    /// [EN] not save" instead of pretending the save succeeded).
    /// [ZH] 最近一次读写失败的原因（界面可据此提示“存不下来”，而不是假装保存成功）。
    /// </summary>
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
            // [EN] A failed save does not affect this session's use, but the caller must know
            // [EN] about it (the UI reports it).
            // [ZH] 保存失败不影响本次会话使用，但必须让调用方知道（界面据此提示）。
            LastError = ex.Message;
            return false;
        }
    }

    /// <summary>
    /// [EN] Add a snippet; returns false when the name already exists (same semantics as the
    /// [EN] WPF version, where the UI reports it as a duplicate name).
    /// [ZH] 新增；名称已存在时返回 false（与 WPF 版同语义，界面按“重名”提示）。
    /// </summary>
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
