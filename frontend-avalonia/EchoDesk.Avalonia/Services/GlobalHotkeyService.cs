// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/GlobalHotkeyService.cs.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/GlobalHotkeyService.cs。
//
// [EN] Global push-to-talk hotkey (v0.2.6 Part 2).
//
// [EN] Why a low-level keyboard hook instead of RegisterHotKey: push-to-talk needs
// [EN] **both** the key-down and the key-up event (recording starts on press and
// [EN] stops on release), whereas RegisterHotKey only reports activation. A
// [EN] WH_KEYBOARD_LL hook also works when the application does not have focus,
// [EN] which is the entire point of a global shortcut.
//
// [EN] Platform boundary: the hook is Windows-only (user32). On macOS and Linux no
// [EN] equivalent is available without an extra accessibility permission or a
// [EN] distribution-specific portal, so the service reports `IsSupported = false`
// [EN] and the main window falls back to an in-window key handler. This is a
// [EN] deliberate, documented degradation rather than a silent failure.
//
// [ZH] 全局按键说话快捷键（v0.2.6 Part 2）。
//
// [ZH] 采用低级键盘钩子而非 RegisterHotKey 的原因：按键说话需要**按下与松开两个**
// [ZH] 事件（按下开始录音、松开结束），而 RegisterHotKey 只报告激活。WH_KEYBOARD_LL
// [ZH] 钩子在应用失去焦点时同样有效 —— 这正是全局快捷键的意义。
//
// [ZH] 平台边界：钩子仅适用于 Windows（user32）。macOS 与 Linux 在没有额外辅助功能
// [ZH] 授权或发行版专属 portal 的情况下没有等价能力，因此本服务报告
// [ZH] `IsSupported = false`，主窗口回退到窗口内按键处理。这是有意记录在案的降级，
// [ZH] 而不是静默失败。
using System;
using System.Runtime.InteropServices;
using System.Threading;

namespace EchoDesk.Avalonia.Services;

/// <summary>
/// [EN] Global keyboard hook delivering push-to-talk press/release events.
/// [ZH] 提供按键说话按下/松开事件的全局键盘钩子。
/// </summary>
public sealed class GlobalHotkeyService : IDisposable
{
    private const int WhKeyboardLl = 13;
    private const int WmKeyDown = 0x0100;
    private const int WmKeyUp = 0x0101;
    private const int WmSysKeyDown = 0x0104;
    private const int WmSysKeyUp = 0x0105;
    private const int WmQuit = 0x0012;

    private const int VkSpace = 0x20;
    private const int VkLeftControl = 0xA2;
    private const int VkRightControl = 0xA3;
    private const int VkLeftAlt = 0xA4;
    private const int VkRightAlt = 0xA5;
    private const int VkLeftShift = 0xA0;
    private const int VkRightShift = 0xA1;

    private readonly object _gate = new();
    private Thread? _thread;
    private uint _threadId;
    private IntPtr _hook;
    private LowLevelKeyboardProc? _callback;
    private ManualResetEventSlim? _installed;
    private volatile bool _running;
    private volatile bool _pushToTalkDown;
    private int _pushToTalkVirtualKey = VkRightControl;
    private volatile bool _spotlightEnabled = true;
    private bool _started;

    /// <summary>
    /// [EN] Whether the current platform provides a global hook. When false, the
    /// [EN] caller must use an in-window shortcut instead.
    /// [ZH] 当前平台是否提供全局钩子。为 false 时调用方必须改用窗口内快捷键。
    /// </summary>
    public bool IsSupported { get; private set; }

    /// <summary>
    /// [EN] Human-readable reason when <see cref="IsSupported"/> is false.
    /// [ZH] <see cref="IsSupported"/> 为 false 时的可读原因。
    /// </summary>
    public string UnavailableReason { get; private set; } = "";

    /// <summary>
    /// [EN] User pressed the configured push-to-talk key.
    /// [ZH] 用户按下了配置的按键说话键。
    /// </summary>
    public event Action? PushToTalkPressed;

    /// <summary>
    /// [EN] User released the configured push-to-talk key.
    /// [ZH] 用户松开了配置的按键说话键。
    /// </summary>
    public event Action? PushToTalkReleased;

    /// <summary>
    /// [EN] User pressed Ctrl+Alt+Space (reserved for the spotlight entry point).
    /// [ZH] 用户按下 Ctrl+Alt+Space（预留给极速呼出入口）。
    /// </summary>
    public event Action? SpotlightRequested;

    /// <summary>
    /// [EN] Start the hook and associate it with a configured key name.
    ///
    /// [ZH] 启动钩子并关联配置的键名。
    /// </summary>
    /// <param name="shortcut">
    /// [EN] Key name from the backend configuration (`RightCtrl`, `LeftCtrl`, `RightAlt`, `F8`, ...).
    /// [ZH] 后端配置中的键名（`RightCtrl`、`LeftCtrl`、`RightAlt`、`F8` 等）。
    /// </param>
    public void Start(string shortcut)
    {
        if (_started)
            return;
        _started = true;
        _pushToTalkVirtualKey = ParseVirtualKey(shortcut);
        if (!OperatingSystem.IsWindows())
        {
            IsSupported = false;
            UnavailableReason = OperatingSystem.IsMacOS()
                ? "macOS 全局快捷键需要辅助功能授权，当前版本仅在应用窗口内响应"
                : "Linux 全局快捷键需要发行版 portal，当前版本仅在应用窗口内响应";
            return;
        }
        _installed = new ManualResetEventSlim(false);
        _thread = new Thread(HookLoop)
        {
            IsBackground = true,
            Name = "EchoDesk.GlobalHotkey",
        };
        _thread.SetApartmentState(ApartmentState.STA);
        _thread.Start();
        // [EN] Wait briefly for the hook installation result so callers can decide
        // [EN] immediately whether the in-window fallback is required.
        // [ZH] 短暂等待钩子安装结果，使调用方可以立即决定是否需要窗口内回退。
        _installed.Wait(TimeSpan.FromSeconds(2));
    }

    /// <summary>
    /// [EN] Enable or disable the spotlight chord independently of push-to-talk.
    /// [ZH] 独立于按键说话开关极速呼出组合键。
    /// </summary>
    public void SetSpotlightEnabled(bool enabled) => _spotlightEnabled = enabled;

    private void HookLoop()
    {
        _threadId = Native.GetCurrentThreadId();
        _callback = HookCallback;
        try
        {
            _hook = Native.SetWindowsHookEx(WhKeyboardLl, _callback,
                Native.GetModuleHandle(null), 0);
        }
        catch (Exception)
        {
            _hook = IntPtr.Zero;
        }
        if (_hook == IntPtr.Zero)
        {
            UnavailableReason = "无法安装全局键盘钩子（可能被安全软件拦截）";
            IsSupported = false;
            _installed?.Set();
            return;
        }
        IsSupported = true;
        _running = true;
        _installed?.Set();
        try
        {
            while (_running && Native.GetMessage(out Native.Msg message, IntPtr.Zero, 0, 0) > 0)
            {
                Native.TranslateMessage(ref message);
                Native.DispatchMessage(ref message);
            }
        }
        finally
        {
            if (_hook != IntPtr.Zero)
            {
                Native.UnhookWindowsHookEx(_hook);
                _hook = IntPtr.Zero;
            }
        }
    }

    private IntPtr HookCallback(int code, IntPtr wParam, IntPtr lParam)
    {
        try
        {
            if (code >= 0)
            {
                int message = wParam.ToInt32();
                Native.KbdLlHookStruct data =
                    Marshal.PtrToStructure<Native.KbdLlHookStruct>(lParam);
                int key = unchecked((int)data.VirtualKeyCode);
                bool isDown = message is WmKeyDown or WmSysKeyDown;
                bool isUp = message is WmKeyUp or WmSysKeyUp;
                if (key == _pushToTalkVirtualKey)
                {
                    if (isDown && !_pushToTalkDown)
                    {
                        _pushToTalkDown = true;
                        PushToTalkPressed?.Invoke();
                    }
                    else if (isUp && _pushToTalkDown)
                    {
                        _pushToTalkDown = false;
                        PushToTalkReleased?.Invoke();
                    }
                }
                else if (isDown && _spotlightEnabled && key == VkSpace
                         && (IsDown(VkLeftControl) || IsDown(VkRightControl))
                         && (IsDown(VkLeftAlt) || IsDown(VkRightAlt)))
                {
                    SpotlightRequested?.Invoke();
                }
            }
        }
        catch (Exception)
        {
            // [EN] A hook callback must never throw: Windows removes a hook that
            // [EN] times out or faults, which would silently disable the shortcut.
            // [ZH] 钩子回调绝不抛出：Windows 会移除超时或故障的钩子，导致快捷键静默失效。
        }
        return Native.CallNextHookEx(_hook, code, wParam, lParam);
    }

    private static bool IsDown(int virtualKey) => (Native.GetAsyncKeyState(virtualKey) & 0x8000) != 0;

    /// <summary>
    /// [EN] Map a configuration key name to a Windows virtual-key code.
    /// [ZH] 把配置键名映射为 Windows 虚拟键码。
    /// </summary>
    public static int ParseVirtualKey(string? name)
    {
        string value = (name ?? "").Trim();
        if (value.Length == 0)
            return VkRightControl;
        if (value.StartsWith("F", StringComparison.OrdinalIgnoreCase)
            && int.TryParse(value.AsSpan(1), out int functionKey)
            && functionKey is >= 1 and <= 12)
        {
            return 0x70 + functionKey - 1;
        }
        return value.ToLowerInvariant() switch
        {
            "rightctrl" or "rctrl" => VkRightControl,
            "leftctrl" or "lctrl" => VkLeftControl,
            "rightalt" or "ralt" => VkRightAlt,
            "leftalt" or "lalt" => VkLeftAlt,
            "rightshift" or "rshift" => VkRightShift,
            "leftshift" or "lshift" => VkLeftShift,
            "space" => VkSpace,
            _ => VkRightControl,
        };
    }

    /// <summary>
    /// [EN] Remove the hook and stop the message loop, keeping the service reusable
    /// (the settings page restarts it after the push-to-talk switch changes).
    /// [ZH] 移除钩子并停止消息循环，同时保持服务可复用（设置页在按键说话开关变化后
    /// 重启它）。
    /// </summary>
    public void Stop()
    {
        if (!_started)
            return;
        _started = false;
        _running = false;
        _pushToTalkDown = false;
        if (_thread is not null && _threadId != 0)
        {
            Native.PostThreadMessage(_threadId, WmQuit, IntPtr.Zero, IntPtr.Zero);
            _thread.Join(TimeSpan.FromSeconds(2));
        }
        _thread = null;
        _callback = null;
        _installed?.Dispose();
        _installed = null;
        IsSupported = false;
    }

    /// <summary>
    /// [EN] Remove the hook and stop the message loop.
    /// [ZH] 移除钩子并停止消息循环。
    /// </summary>
    public void Dispose() => Stop();

    private delegate IntPtr LowLevelKeyboardProc(int code, IntPtr wParam, IntPtr lParam);

    private static class Native
    {
        [StructLayout(LayoutKind.Sequential)]
        internal struct KbdLlHookStruct
        {
            internal uint VirtualKeyCode;
            internal uint ScanCode;
            internal uint Flags;
            internal uint Time;
            internal IntPtr ExtraInfo;
        }

        [StructLayout(LayoutKind.Sequential)]
        internal struct Msg
        {
            internal IntPtr Hwnd;
            internal uint Message;
            internal IntPtr WParam;
            internal IntPtr LParam;
            internal uint Time;
            internal int PointX;
            internal int PointY;
        }

        [DllImport("user32.dll", SetLastError = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern IntPtr SetWindowsHookEx(int hook, LowLevelKeyboardProc callback,
            IntPtr module, uint threadId);

        [DllImport("user32.dll", SetLastError = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern bool UnhookWindowsHookEx(IntPtr hook);

        [DllImport("user32.dll")]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr wParam,
            IntPtr lParam);

        [DllImport("user32.dll")]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern short GetAsyncKeyState(int virtualKey);

        [DllImport("user32.dll")]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern int GetMessage(out Msg message, IntPtr window, uint filterMin,
            uint filterMax);

        [DllImport("user32.dll")]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern bool TranslateMessage(ref Msg message);

        [DllImport("user32.dll")]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern IntPtr DispatchMessage(ref Msg message);

        [DllImport("user32.dll")]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern bool PostThreadMessage(uint threadId, uint message, IntPtr wParam,
            IntPtr lParam);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern IntPtr GetModuleHandle(string? moduleName);

        [DllImport("kernel32.dll")]
        internal static extern uint GetCurrentThreadId();
    }
}
