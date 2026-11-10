// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/MediaService.cs.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/MediaService.cs。
//
// [EN] Frontend half of the v0.2.6 Part 2 media protocol.
//
// [EN] Transport rule: binary payloads never cross the JSON line protocol. This
// [EN] service stages a picked image inside the backend-provided
// [EN] `temp/images` directory and only the absolute path is sent to `image_chat`
// [EN] or `transcribe_audio`. The directories come from the backend's
// [EN] `media_state` response, keeping a single authority for the user data
// [EN] layout (the same rule as `UserPaths`).
//
// [EN] Playback stays on the interface side: the backend only produces a file
// [EN] below `temp/audio/tts` and this service hands it to the platform player.
//
// [ZH] v0.2.6 Part 2 媒体协议的前端半边。
//
// [ZH] 传输规则：二进制负载绝不经过 JSON 行协议。本服务把用户选择的图片暂存到
// [ZH] 后端提供的 `temp/images` 目录，仅把绝对路径发送给 `image_chat` 或
// [ZH] `transcribe_audio`。目录来自后端的 `media_state` 响应，使用户数据布局保持
// [ZH] 单一权威（与 `UserPaths` 同一规则）。
//
// [ZH] 播放留在界面侧：后端只在 `temp/audio/tts` 下产出文件，本服务把它交给平台
// [ZH] 播放器。
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading.Tasks;

namespace EchoDesk.Avalonia.Services;

/// <summary>
/// [EN] Snapshot of the backend media capabilities and staging directories.
/// [ZH] 后端媒体能力与暂存目录的快照。
/// </summary>
public sealed class MediaState
{
    /// <summary>[EN] Whether `media_state` was fetched successfully. / [ZH] 是否成功取回 `media_state`。</summary>
    public bool Loaded { get; set; }

    /// <summary>[EN] Directory for staged images. / [ZH] 图片暂存目录。</summary>
    public string ImagesDirectory { get; set; } =
        Path.Combine(UserPaths.DataRoot, "temp", "images");

    /// <summary>[EN] Directory for staged recordings. / [ZH] 录音暂存目录。</summary>
    public string AudioDirectory { get; set; } =
        Path.Combine(UserPaths.DataRoot, "temp", "audio");

    /// <summary>[EN] Directory for synthesized speech. / [ZH] 语音合成输出目录。</summary>
    public string TtsDirectory { get; set; } =
        Path.Combine(UserPaths.DataRoot, "temp", "audio", "tts");

    /// <summary>[EN] Whether image chat is enabled and its model is present. / [ZH] 图片对话是否已启用且模型存在。</summary>
    public bool VisionAvailable { get; set; }

    /// <summary>[EN] Localized reason when vision is unavailable. / [ZH] 视觉不可用时的已本地化原因。</summary>
    public string VisionReason { get; set; } = "";

    /// <summary>[EN] Whether transcription is available. / [ZH] 转写是否可用。</summary>
    public bool SttAvailable { get; set; }

    /// <summary>[EN] Localized reason when transcription is unavailable. / [ZH] 转写不可用时的已本地化原因。</summary>
    public string SttReason { get; set; } = "";

    /// <summary>[EN] Whether a speech engine exists on this machine. / [ZH] 本机是否存在语音引擎。</summary>
    public bool TtsEngineAvailable { get; set; }

    /// <summary>[EN] Whether speech output is enabled. / [ZH] 是否启用语音朗读。</summary>
    public bool TtsEnabled { get; set; }

    /// <summary>[EN] Whether completed answers are read out automatically. / [ZH] 是否自动朗读完成的回答。</summary>
    public bool TtsAutoRead { get; set; }

    /// <summary>[EN] Selected voice name (empty = engine default). / [ZH] 已选音色名（空 = 引擎默认）。</summary>
    public string TtsVoice { get; set; } = "";

    /// <summary>[EN] Selected rate step (-10..10). / [ZH] 已选语速档位（-10..10）。</summary>
    public int TtsRate { get; set; }

    /// <summary>[EN] Available voice names. / [ZH] 可用音色名。</summary>
    public List<string> Voices { get; } = new();

    /// <summary>[EN] Whether the push-to-talk shortcut is configured. / [ZH] 是否配置了按键说话快捷键。</summary>
    public bool PttEnabled { get; set; } = true;

    /// <summary>[EN] Configured push-to-talk key name. / [ZH] 配置的按键说话键名。</summary>
    public string PttShortcut { get; set; } = "RightCtrl";
}

/// <summary>
/// [EN] Media protocol client: media state, image staging and audio playback.
/// [ZH] 媒体协议客户端：媒体状态、图片暂存与音频播放。
/// </summary>
public sealed class MediaService
{
    private readonly object _gate = new();

    /// <summary>[EN] Latest media state snapshot. / [ZH] 最新的媒体状态快照。</summary>
    public MediaState State { get; } = new();

    /// <summary>
    /// [EN] Fetch `media_state` from the backend and update <see cref="State"/>.
    /// Failures leave the previous snapshot untouched: a media feature must never
    /// keep the main window from becoming usable.
    ///
    /// [ZH] 从后端取回 `media_state` 并更新 <see cref="State"/>。失败时保留上一份
    /// 快照：媒体功能绝不能让主窗口无法使用。
    /// </summary>
    public async Task<MediaState> LoadStateAsync(BackendService backend,
        System.Threading.CancellationToken cancellationToken = default)
    {
        try
        {
            using JsonDocument? doc = await backend.SendAsync(
                new { action = "media_state" },
                idleTimeout: TimeSpan.FromSeconds(30),
                cancellationToken: cancellationToken);
            if (doc is null)
                return State;
            JsonElement root = doc.RootElement;
            lock (_gate)
            {
                if (root.TryGetProperty("dirs", out JsonElement dirs))
                {
                    State.ImagesDirectory = ReadString(dirs, "images", State.ImagesDirectory);
                    State.AudioDirectory = ReadString(dirs, "audio", State.AudioDirectory);
                    State.TtsDirectory = ReadString(dirs, "tts", State.TtsDirectory);
                }
                if (root.TryGetProperty("vision", out JsonElement vision))
                {
                    State.VisionAvailable = ReadBool(vision, "available", false);
                    State.VisionReason = ReadString(vision, "reason_text", "");
                }
                if (root.TryGetProperty("stt", out JsonElement stt))
                {
                    State.SttAvailable = ReadBool(stt, "available", false);
                    State.SttReason = ReadString(stt, "reason_text", "");
                }
                if (root.TryGetProperty("tts", out JsonElement tts))
                {
                    State.TtsEngineAvailable = ReadBool(tts, "available", false);
                    State.TtsEnabled = ReadBool(tts, "enabled", true);
                    State.TtsAutoRead = ReadBool(tts, "auto_read", false);
                    State.TtsVoice = ReadString(tts, "voice", "");
                    State.TtsRate = ReadInt(tts, "rate", 0);
                    State.Voices.Clear();
                    if (tts.TryGetProperty("voices", out JsonElement voices)
                        && voices.ValueKind == JsonValueKind.Array)
                    {
                        foreach (JsonElement item in voices.EnumerateArray())
                        {
                            string? name = item.GetString();
                            if (!string.IsNullOrWhiteSpace(name))
                                State.Voices.Add(name!);
                        }
                    }
                }
                if (root.TryGetProperty("ptt", out JsonElement ptt))
                {
                    State.PttEnabled = ReadBool(ptt, "enabled", true);
                    State.PttShortcut = ReadString(ptt, "shortcut", "RightCtrl");
                }
                State.Loaded = true;
            }
        }
        catch (Exception)
        {
            // [EN] A media state failure is not fatal: every media action reports
            // [EN] its own localized error when it is actually used.
            // [ZH] 媒体状态获取失败不是致命错误：每个媒体动作在真正使用时都会报告
            // [ZH] 各自的已本地化错误。
        }
        return State;
    }

    /// <summary>
    /// [EN] Copy a picked image into the image staging directory and return the
    /// absolute destination path, or null when the extension is unsupported.
    ///
    /// The copy is deliberate: the source may live in a read-only or remote
    /// location, and the backend only accepts paths inside its staging root.
    ///
    /// [ZH] 把所选图片复制到图片暂存目录并返回目标绝对路径；扩展名不支持时返回
    /// null。
    ///
    /// 复制是刻意为之：源文件可能位于只读或远程位置，而后端只接受其暂存根目录内
    /// 的路径。
    /// </summary>
    public string? StageImage(string sourcePath)
    {
        if (string.IsNullOrWhiteSpace(sourcePath) || !File.Exists(sourcePath))
            return null;
        string extension = Path.GetExtension(sourcePath).ToLowerInvariant();
        if (extension is not (".png" or ".jpg" or ".jpeg" or ".webp" or ".bmp"))
            return null;
        string directory = State.ImagesDirectory;
        UserPaths.EnsureDirectory(directory);
        string stem = Path.GetFileNameWithoutExtension(sourcePath);
        foreach (char invalid in Path.GetInvalidFileNameChars())
            stem = stem.Replace(invalid, '_');
        if (stem.Length == 0)
            stem = "image";
        string target = Path.Combine(directory,
            $"{stem}-{DateTime.Now:yyyyMMdd-HHmmss-fff}{extension}");
        try
        {
            File.Copy(sourcePath, target, overwrite: false);
            return target;
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>
    /// [EN] Play an audio file produced by `tts_generate` with the platform player.
    /// Playback runs on a background task so the interface never blocks on audio.
    /// Returns false when the file is missing or no player is available.
    ///
    /// [ZH] 用平台播放器播放 `tts_generate` 产出的音频文件。播放放在后台任务中，
    /// 界面不会因音频而阻塞。文件缺失或没有可用播放器时返回 false。
    /// </summary>
    public bool PlayAudio(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            return false;
        _ = Task.Run(() =>
        {
            try
            {
                if (OperatingSystem.IsWindows())
                {
                    PlaySoundWindows(path);
                    return;
                }
                if (OperatingSystem.IsMacOS())
                {
                    RunProcess("afplay", path);
                    return;
                }
                if (!RunProcess("paplay", path))
                    RunProcess("aplay", path);
            }
            catch (Exception)
            {
                // [EN] Playback failure is not surfaced as a dialog: the text of the
                // [EN] answer is already visible, so audio is a convenience only.
                // [ZH] 播放失败不弹窗：回答文本已经可见，音频只是便利功能。
            }
        });
        return true;
    }

    private static void PlaySoundWindows(string path)
    {
        // [EN] winmm PlaySound with SND_FILENAME | SND_SYNC. SND_NODEFAULT prevents
        // [EN] the system beep when the file cannot be decoded.
        // [ZH] 使用 winmm 的 PlaySound，标志为 SND_FILENAME | SND_SYNC。
        // [ZH] SND_NODEFAULT 避免文件无法解码时播放系统提示音。
        const uint SndFilename = 0x00020000;
        const uint SndNoDefault = 0x00000002;
        const uint SndSync = 0x00000000;
        PlaySound(path, IntPtr.Zero, SndFilename | SndNoDefault | SndSync);
    }

    private static bool RunProcess(string executable, string argument)
    {
        try
        {
            var info = new ProcessStartInfo(executable)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            info.ArgumentList.Add(argument);
            using Process? process = Process.Start(info);
            process?.WaitForExit(120000);
            return process is not null && process.ExitCode == 0;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static string ReadString(JsonElement element, string name, string fallback)
    {
        if (element.TryGetProperty(name, out JsonElement value)
            && value.ValueKind == JsonValueKind.String)
        {
            string? text = value.GetString();
            return string.IsNullOrEmpty(text) ? fallback : text!;
        }
        return fallback;
    }

    private static bool ReadBool(JsonElement element, string name, bool fallback)
    {
        if (element.TryGetProperty(name, out JsonElement value)
            && value.ValueKind is JsonValueKind.True or JsonValueKind.False)
        {
            return value.GetBoolean();
        }
        return fallback;
    }

    private static int ReadInt(JsonElement element, string name, int fallback)
    {
        if (element.TryGetProperty(name, out JsonElement value)
            && value.ValueKind == JsonValueKind.Number
            && value.TryGetInt32(out int parsed))
        {
            return parsed;
        }
        return fallback;
    }

    [DllImport("winmm.dll", CharSet = CharSet.Unicode)]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    private static extern bool PlaySound(string sound, IntPtr module, uint flags);
}
