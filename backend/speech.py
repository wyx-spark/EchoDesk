# -*- coding: utf-8 -*-
"""
[EN]
Offline speech services: recording, Whisper transcription, and text-to-speech.

Dependency evaluation (v0.2.6 Part 2), and why no Python package was added:

* **Recording** uses ``winmm`` through ``ctypes`` on Windows and the platform
  capture tools (``arecord`` on Linux, ``ffmpeg`` on macOS) elsewhere. A Python
  audio package such as ``sounddevice`` would bundle PortAudio for every platform
  (roughly 2–3 MB per release tree) and still require an external capture stack on
  some Linux distributions; the platform backends are already present on the
  target systems.
* **Transcription** invokes a local ``whisper.cpp`` executable with a GGUF model.
  The Python bindings (``faster-whisper``) would add ``ctranslate2``, tokenizers
  and their transitive tree — measured at roughly 90–140 MB per platform — which
  contradicts the release-size envelope recorded in ``PROJECT.md``. External
  executables keep the release unchanged and remain fully offline.
* **Text-to-speech** uses the operating system voice engine: SAPI through the
  already locked ``pywin32`` on Windows, ``say`` on macOS, and ``espeak-ng`` on
  Linux. ``pyttsx3`` is a thin wrapper around exactly these engines and would add
  no capability.

Consequently ``backend/requirements.lock`` is intentionally unchanged by this
round; every dependency this module uses is either already locked or an
operating-system component. When a component is absent the action fails with a
localized, actionable message instead of invoking any network service.

[ZH]
离线语音服务：录音、Whisper 转写与语音合成。

依赖评估（v0.2.6 Part 2）与"未新增 Python 包"的理由：

* **录音**在 Windows 上通过 ``ctypes`` 调用 ``winmm``，其余平台调用系统采集工具
  （Linux 的 ``arecord``、macOS 的 ``ffmpeg``）。引入 ``sounddevice`` 之类的
  Python 音频包会为每个平台捆绑 PortAudio（每个发布树约 2–3 MB），并且在部分
  Linux 发行版上仍需外部采集栈；目标系统已经具备平台后端。
* **转写**调用本地 ``whisper.cpp`` 可执行文件与 GGUF 模型。改用 Python 绑定
  （``faster-whisper``）会引入 ``ctranslate2``、tokenizers 及其传递依赖树 ——
  实测每平台约 90–140 MB —— 与 ``PROJECT.md`` 记录的发布体积上限冲突。外部
  可执行文件不改变发布物体积，且完全离线。
* **语音合成**使用操作系统语音引擎：Windows 经已锁定的 ``pywin32`` 调用 SAPI，
  macOS 使用 ``say``，Linux 使用 ``espeak-ng``。``pyttsx3`` 正是这些引擎的薄封装，
  不会带来任何额外能力。

因此本轮刻意不修改 ``backend/requirements.lock``；本模块使用的每个依赖要么已锁定，
要么属于操作系统组件。组件缺失时动作以已本地化、可操作的消息失败，绝不调用网络服务。
"""
from __future__ import annotations

import ctypes
import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from i18n import t as _t
from media import (audio_dir, ensure_media_dirs, read_wav_duration, tts_dir,
                   write_wav)

log = logging.getLogger("EchoDesk.Speech")

#: [EN] Recording sample rate. 16 kHz mono 16-bit is what every Whisper variant
#: [EN] consumes natively, so no resampling is needed afterwards.
#: [ZH] 录音采样率。16 kHz 单声道 16 位是各种 Whisper 变体的原生输入，后续无需重采样。
SAMPLE_RATE = 16000

#: [EN] Hard upper bound for one push-to-talk recording (seconds).
#: [ZH] 单次按键录音的硬性上限（秒）。
MAX_RECORD_SECONDS = 120

#: [EN] Maximum number of characters submitted to the speech engine in one call.
#: [ZH] 单次提交给语音合成引擎的最大字符数。
MAX_TTS_CHARS = 4000


class SpeechError(Exception):
    """
    [EN]
    A failed speech operation with a language-pack key; analogous to
    :class:`media.MediaError`.

    [ZH]
    带语言包键的语音操作失败，与 :class:`media.MediaError` 同构。
    """

    def __init__(self, code: str, key: str, **kwargs: object) -> None:
        super().__init__(code)
        self.code = str(code)
        self.key = str(key)
        self.kwargs = dict(kwargs)

    def localize(self, translate) -> str:
        """[EN] Render through the supplied translator. / [ZH] 通过给定翻译函数渲染。"""
        try:
            return str(translate(self.key, **self.kwargs))
        except Exception:  # noqa: BLE001 - [EN] never mask the original reason
                           # [ZH] 绝不掩盖原始原因
            return self.code


# =====================================================================
# [EN] Text-to-speech
# [ZH] 语音合成
# =====================================================================

def _windows_voice_names() -> List[str]:
    """
    [EN]
    Enumerate SAPI voices. Returns an empty list when the component is absent,
    which lets the caller distinguish "no voice engine" from "no voices".

    [ZH]
    枚举 SAPI 音色。组件缺失时返回空列表，使调用方能区分"没有语音引擎"与
    "没有可用音色"。
    """
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            tokens = voice.GetVoices()
            names = [str(tokens.Item(i).GetDescription()) for i in range(tokens.Count)]
            # [EN] Release the COM references before uninitializing: leaving them alive
            # [EN] makes pywin32 print an IUnknown release warning on every call.
            # [ZH] 反初始化之前释放 COM 引用：保留存活会让 pywin32 在每次调用时打印
            # [ZH] IUnknown 释放警告。
            del tokens
            del voice
            return names
        finally:
            pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001 - [EN] absence is a normal degraded state
                       # [ZH] 缺失属于正常降级状态
        return []


def _macos_voice_names() -> List[str]:
    """[EN] Enumerate ``say`` voices. / [ZH] 枚举 ``say`` 音色。"""
    try:
        completed = subprocess.run(["say", "-v", "?"], capture_output=True,
                                   text=True, timeout=10)
        names: List[str] = []
        for line in completed.stdout.splitlines():
            parts = line.split()
            if parts:
                names.append(parts[0])
        return names
    except Exception:  # noqa: BLE001
        return []


def _linux_voice_names() -> List[str]:
    """[EN] Enumerate ``espeak-ng`` voices. / [ZH] 枚举 ``espeak-ng`` 音色。"""
    executable = shutil.which("espeak-ng") or shutil.which("espeak")
    if not executable:
        return []
    try:
        completed = subprocess.run([executable, "--voices"], capture_output=True,
                                   text=True, timeout=10)
        names: List[str] = []
        for line in completed.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4:
                names.append(parts[3])
        return names
    except Exception:  # noqa: BLE001
        return []


def list_voices() -> Dict[str, Any]:
    """
    [EN]
    Return the voices available on this platform plus the active engine name.

    The shape is stable so that ``media_state`` and the settings interface can
    render it without platform branching on the client side.

    [ZH]
    返回本平台可用音色与当前引擎名。

    返回结构保持稳定，使 ``media_state`` 与设置界面无需在客户端按平台分支渲染。
    """
    if sys.platform == "win32":
        engine, voices = "sapi", _windows_voice_names()
    elif sys.platform == "darwin":
        engine, voices = "say", _macos_voice_names()
    else:
        executable = shutil.which("espeak-ng") or shutil.which("espeak")
        engine, voices = ("espeak" if executable else ""), _linux_voice_names()
    return {"engine": engine, "available": bool(engine), "voices": voices}


def synthesize_speech(text: str, out_path: str, voice: str = "",
                      rate: int = 0) -> str:
    """
    [EN]
    Render ``text`` into a WAVE (or AIFF on macOS) file and return its path.

    ``rate`` follows the SAPI convention: 0 is normal, positive values are faster
    and negative values slower, within ``-10 .. 10``. macOS takes a words-per-minute
    value derived from the same scale, and ``espeak-ng`` a words-per-minute value
    of 80 + 12 per step.

    [ZH]
    把 ``text`` 渲染为 WAVE（macOS 为 AIFF）文件并返回路径。

    ``rate`` 沿用 SAPI 约定：0 为正常，正数更快、负数更慢，取值范围 ``-10 .. 10``。
    macOS 使用由同一刻度换算的每分钟词数，``espeak-ng`` 使用 80 + 每档 12 的每分钟词数。
    """
    body = str(text or "").strip()
    if not body:
        raise SpeechError("empty_text", "media.tts_empty_text")
    if len(body) > MAX_TTS_CHARS:
        raise SpeechError("too_long", "media.tts_text_too_long", limit=MAX_TTS_CHARS)
    target = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    step = max(-10, min(10, int(rate or 0)))
    if sys.platform == "win32":
        return _synthesize_windows(body, target, voice, step)
    if sys.platform == "darwin":
        return _synthesize_macos(body, target, voice, step)
    return _synthesize_linux(body, target, voice, step)


def _synthesize_windows(text: str, target: str, voice: str, step: int) -> str:
    """
    [EN]
    Windows speech synthesis.

    Primary path: ``System.Speech`` through a short generated PowerShell script
    executed with ``-File``. The script is written as UTF-8 with a BOM next to the
    output file and contains no externally supplied quoting: paths and text are
    embedded as single-quoted PowerShell literals with embedded quotes doubled.
    ``-EncodedCommand`` was measured to fail on a stock Windows PowerShell 5.1
    installation with "no voices are installed or available to the current
    security settings", while the same script run with ``-File`` succeeds, so the
    script-file form is used. The SAPI ``SpFileStream`` COM path is kept as a
    fallback because a few customised installations replace ``System.Speech``.

    [ZH]
    Windows 语音合成。

    主路径：通过 ``-File`` 执行一段生成的 PowerShell 脚本并调用
    ``System.Speech``。脚本以带 BOM 的 UTF-8 写在输出文件同目录，不含任何来自
    外部的引号：路径与文本以单引号字面量内嵌，内部单引号双写。实测
    ``-EncodedCommand`` 在标准 Windows PowerShell 5.1 上会报"系统上未安装语音，
    或没有当前安全设置可用的语音"，而同一脚本用 ``-File`` 可正常执行，故采用
    脚本文件形式。SAPI ``SpFileStream`` COM 路径作为回退保留，因为少数定制安装
    会替换 ``System.Speech``。
    """
    quote = lambda value: str(value).replace("'", "''")  # noqa: E731 - [EN] local literal helper
                                                        # [ZH] 局部字面量转义助手
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -AssemblyName System.Speech",
        "$player = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        f"$player.Rate = {step}",
    ]
    if voice:
        lines.append(f"$player.SelectVoice('{quote(voice)}')")
    lines.extend((
        f"$player.SetOutputToWaveFile('{quote(target)}')",
        f"$player.Speak('{quote(text)}')",
        "$player.Dispose()",
    ))
    script_path = target + ".ps1"
    try:
        with open(script_path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
            handle.write("\n".join(lines))
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            raise SpeechError("no_engine", "media.tts_unavailable", detail="powershell")
        completed = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", script_path],
            capture_output=True, text=True, timeout=180,
        )
        if completed.returncode == 0 and os.path.isfile(target) \
                and os.path.getsize(target) > 44:
            return target
        log.warning("System.Speech 合成失败：%s", (completed.stderr or "")[:300])
    except SpeechError:
        raise
    except Exception as exc:  # noqa: BLE001 - [EN] fall through to the COM path
                             # [ZH] 退回到 COM 路径
        log.warning("System.Speech 合成异常：%s", exc)
    finally:
        try:
            if os.path.isfile(script_path):
                os.remove(script_path)
        except OSError:
            pass
    return _synthesize_windows_com(text, target, voice, step)


def _synthesize_windows_com(text: str, target: str, voice: str, step: int) -> str:
    """[EN] SAPI ``SpFileStream`` fallback. / [ZH] SAPI ``SpFileStream`` 回退路径。"""
    try:
        import pythoncom
        import win32com.client
    except Exception as exc:  # noqa: BLE001 - [EN] report the missing component
                             # [ZH] 上报缺失组件
        raise SpeechError("no_engine", "media.tts_unavailable", detail=str(exc)) from exc
    pythoncom.CoInitialize()
    try:
        engine = win32com.client.Dispatch("SAPI.SpVoice")
        tokens = engine.GetVoices()
        if voice:
            for index in range(tokens.Count):
                token = tokens.Item(index)
                if str(token.GetDescription()) == voice:
                    engine.Voice = token
                    break
        engine.Rate = step
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(target, 3, False)
        engine.AudioOutputStream = stream
        try:
            engine.Speak(text, 0)
        finally:
            stream.Close()
        if not os.path.isfile(target) or os.path.getsize(target) <= 44:
            raise SpeechError("synth_failed", "media.tts_failed", detail="empty output")
        return target
    except SpeechError:
        raise
    except Exception as exc:  # noqa: BLE001 - [EN] the engine rejected the request
                             # [ZH] 引擎拒绝了该请求
        raise SpeechError("synth_failed", "media.tts_failed", detail=str(exc)) from exc
    finally:
        pythoncom.CoUninitialize()


def _synthesize_macos(text: str, target: str, voice: str, step: int) -> str:
    """[EN] ``say`` rendering. / [ZH] ``say`` 渲染。"""
    executable = shutil.which("say")
    if not executable:
        raise SpeechError("no_engine", "media.tts_unavailable", detail="say")
    words_per_minute = max(90, min(360, 180 + step * 15))
    command = [executable, "-r", str(words_per_minute), "-o", target]
    if voice:
        command.extend(["-v", voice])
    command.extend(["--", text])
    return _run_synthesis(command, target)


def _synthesize_linux(text: str, target: str, voice: str, step: int) -> str:
    """[EN] ``espeak-ng`` rendering. / [ZH] ``espeak-ng`` 渲染。"""
    executable = shutil.which("espeak-ng") or shutil.which("espeak")
    if not executable:
        raise SpeechError("no_engine", "media.tts_unavailable", detail="espeak-ng")
    words_per_minute = max(80, min(450, 160 + step * 12))
    command = [executable, "-s", str(words_per_minute), "-w", target]
    if voice:
        command.extend(["-v", voice])
    command.append(text)
    return _run_synthesis(command, target)


def _run_synthesis(command: List[str], target: str) -> str:
    """[EN] Execute a synthesis command and validate the produced file. / [ZH] 执行合成命令并校验产物。"""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        raise SpeechError("synth_failed", "media.tts_failed", detail=str(exc)) from exc
    if completed.returncode != 0 or not os.path.isfile(target):
        detail = (completed.stderr or completed.stdout or "").strip()[:400]
        raise SpeechError("synth_failed", "media.tts_failed", detail=detail)
    return target


def tts_output_path() -> str:
    """
    [EN]
    Unique output path inside ``temp/audio/tts``. macOS ``say`` always writes an
    AIFF container, so the extension follows the platform instead of renaming the
    container and misleading the player.

    [ZH]
    ``temp/audio/tts`` 内的唯一输出路径。macOS 的 ``say`` 始终写出 AIFF 容器，
    因此扩展名随平台变化，避免给容器改名而误导播放器。
    """
    ensure_media_dirs()
    extension = ".aiff" if sys.platform == "darwin" else ".wav"
    return os.path.join(tts_dir(), f"tts-{int(time.time() * 1000)}{extension}")


# =====================================================================
# [EN] Whisper transcription
# [ZH] Whisper 转写
# =====================================================================

#: [EN] Executable names probed on ``PATH``; whisper.cpp releases use the first two.
#: [ZH] 在 ``PATH`` 上探测的可执行文件名；whisper.cpp 发布物使用前两个。
_WHISPER_EXECUTABLES = ("whisper-cli", "whisper-cli.exe", "whisper", "main.exe", "main")

#: [EN] Timestamp prefixes such as ``[00:00:00.000 --> 00:00:02.000]``.
#: [ZH] 形如 ``[00:00:00.000 --> 00:00:02.000]`` 的时间戳前缀。
_TIMESTAMP = re.compile(r"^\s*\[[\d:.\s\-→>]+\]\s*")

#: [EN] Non-speech markers emitted by whisper.cpp. / [ZH] whisper.cpp 输出的非语音标记。
_NON_SPEECH = re.compile(r"^\s*[\[(](?:blank_audio|music|silence|inaudible)[\])]\s*$",
                         re.IGNORECASE)


def find_whisper_executable(explicit: str = "") -> str:
    """
    [EN]
    Resolve the ``whisper.cpp`` executable: explicit configuration first, then
    ``PATH``. Returns an empty string when nothing is found.

    [ZH]
    解析 ``whisper.cpp`` 可执行文件：显式配置优先，其次 ``PATH``。未找到时返回空串。
    """
    if explicit:
        candidate = os.path.abspath(os.path.expanduser(explicit))
        if os.path.isfile(candidate):
            return candidate
    for name in _WHISPER_EXECUTABLES:
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)
    return ""


def find_whisper_model(explicit: str = "", models_dir: str = "") -> str:
    """
    [EN]
    Resolve the Whisper GGML model: explicit configuration first, then the model
    directory. Preference order is ``ggml-*.bin``, ``whisper*.bin`` and any other
    ``*.bin`` whose name contains ``whisper``.

    [ZH]
    解析 Whisper GGML 模型：显式配置优先，其次模型目录。优先顺序为
    ``ggml-*.bin``、``whisper*.bin``，以及名称包含 ``whisper`` 的其它 ``*.bin``。
    """
    if explicit:
        candidate = os.path.abspath(os.path.expanduser(explicit))
        if os.path.isfile(candidate):
            return candidate
    if not models_dir or not os.path.isdir(models_dir):
        return ""
    patterns = ("ggml-*.bin", "whisper*.bin", "ggml*.bin")
    for pattern in patterns:
        candidates = sorted(glob.glob(os.path.join(models_dir, pattern)))
        if candidates:
            return os.path.abspath(candidates[0])
    for candidate in sorted(glob.glob(os.path.join(models_dir, "*.bin"))):
        if "whisper" in os.path.basename(candidate).lower():
            return os.path.abspath(candidate)
    return ""


def _clean_transcript(text: str) -> str:
    """
    [EN]
    Remove timestamps and non-speech markers from whisper.cpp standard output.

    [ZH]
    从 whisper.cpp 标准输出中移除时间戳与非语音标记。
    """
    lines: List[str] = []
    for raw in str(text or "").splitlines():
        line = _TIMESTAMP.sub("", raw).strip()
        if not line or _NON_SPEECH.match(line):
            continue
        lines.append(line)
    return " ".join(lines).strip()


class WhisperTranscriber:
    """
    [EN]
    Thin owner of the configured ``whisper.cpp`` executable and model.

    ``status()`` is cheap; ``transcribe()`` validates the pair, runs the process
    with a bounded timeout and returns cleaned text.

    [ZH]
    已配置 ``whisper.cpp`` 可执行文件与模型的轻量持有者。

    ``status()`` 代价很低；``transcribe()`` 校验这一对配置，在有限超时内运行进程，
    并返回清洗后的文本。
    """

    def __init__(self, executable: str = "", model: str = "",
                 models_dir: str = "", language: str = "auto") -> None:
        self.executable = find_whisper_executable(executable)
        self.model = find_whisper_model(model, models_dir)
        self.language = str(language or "auto")
        self.last_error = ""

    def status(self) -> Dict[str, Any]:
        """[EN] Machine-readable availability report. / [ZH] 机器可读可用性报告。"""
        reason = ""
        if not self.executable:
            reason = "media.stt_no_executable"
        elif not self.model:
            reason = "media.stt_no_model"
        return {
            "available": not reason,
            "executable": self.executable,
            "model": os.path.basename(self.model),
            "model_path": self.model,
            "language": self.language,
            "reason": reason,
            "reason_text": _t(reason) if reason else "",
            "last_error": self.last_error,
        }

    def transcribe(self, audio_path: str, language: Optional[str] = None,
                   timeout: float = 0.0) -> Dict[str, Any]:
        """
        [EN]
        Transcribe one audio file and return ``{"text", "elapsed_ms"}``.

        A language of ``auto`` omits the language switch so that whisper.cpp
        performs its own detection. The timeout scales with the recording length
        and is capped at ten minutes.

        [ZH]
        转写一个音频文件并返回 ``{"text", "elapsed_ms"}``。

        语言为 ``auto`` 时省略语言参数，由 whisper.cpp 自行检测。超时随录音时长
        放大，上限为十分钟。
        """
        if not self.executable:
            raise SpeechError("no_executable", "media.stt_no_executable")
        if not self.model:
            raise SpeechError("no_model", "media.stt_no_model")
        selected = str(language or self.language or "auto")
        command = [self.executable, "-m", self.model, "-f", audio_path, "-nt"]
        if selected and selected.lower() not in ("auto", "detect"):
            command.extend(["-l", selected])
        duration = read_wav_duration(audio_path)
        budget = float(timeout or 0) or max(60.0, min(600.0, duration * 3.0 + 60.0))
        started = time.time()
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=budget)
        except subprocess.TimeoutExpired as exc:
            self.last_error = f"timeout after {budget:.0f}s"
            raise SpeechError("timeout", "media.stt_timeout", seconds=f"{budget:.0f}") from exc
        except Exception as exc:  # noqa: BLE001 - [EN] process could not start
                                 # [ZH] 进程无法启动
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise SpeechError("failed", "media.stt_failed", detail=self.last_error) from exc
        elapsed_ms = int((time.time() - started) * 1000)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            self.last_error = (detail[-1] if detail else f"exit {completed.returncode}")[:400]
            # [EN] A PATH can contain an unrelated `whisper` executable (for example the
            # [EN] OpenAI Python CLI). It rejects whisper.cpp's ``-m`` flag with an argparse
            # [EN] error; reporting that as "transcription failed" hides the real cause, so
            # [EN] it is classified as "wrong executable" with actionable guidance.
            # [ZH] PATH 上可能存在无关的 `whisper` 可执行文件（例如 OpenAI Python CLI）。
            # [ZH] 它会以 argparse 错误拒绝 whisper.cpp 的 ``-m`` 参数；若按"转写失败"上报，
            # [ZH] 会掩盖真实原因，因此归类为"可执行文件不匹配"并给出可操作指引。
            joined = " ".join(detail).lower()
            if "-m" in joined and (
                    "unrecognized argument" in joined
                    or "unknown option" in joined
                    or "no such option" in joined
                    or "invalid option" in joined
                    or "unrecognized option" in joined):
                raise SpeechError("wrong_executable", "media.stt_wrong_executable",
                                  name=os.path.basename(self.executable),
                                  detail=self.last_error)
            raise SpeechError("failed", "media.stt_failed", detail=self.last_error)
        text = _clean_transcript(completed.stdout)
        if not text:
            # [EN] Some whisper.cpp builds write the transcript to stderr; try it too.
            # [ZH] 部分 whisper.cpp 构建把转写写入 stderr；一并尝试。
            text = _clean_transcript(completed.stderr)
        self.last_error = ""
        return {"text": text, "elapsed_ms": elapsed_ms}


# =====================================================================
# [EN] Audio recording
# [ZH] 音频录音
# =====================================================================

class _WindowsWaveInRecorder:
    """
    [EN]
    ``winmm`` waveIn recorder implemented with ``ctypes``.

    Three one-second buffers are queued; the polling thread copies completed
    buffers and re-queues them until recording stops. Buffers are concatenated in
    completion order, which for a single input device is capture order. No
    callback is used, therefore no window message loop is required.

    [ZH]
    使用 ``ctypes`` 实现的 ``winmm`` waveIn 录音器。

    预先排队三个一秒缓冲；轮询线程复制已完成的缓冲并重新排队，直至停止录音。
    缓冲按完成顺序拼接，对单一输入设备而言即采集顺序。不使用回调，因此不需要
    窗口消息循环。
    """

    WHDR_DONE = 0x00000001
    WAVE_MAPPER = -1
    CALLBACK_NULL = 0x00000000
    WAVE_FORMAT_PCM = 1
    BUFFER_SECONDS = 1.0
    QUEUE_DEPTH = 3

    class WAVEFORMATEX(ctypes.Structure):
        _fields_ = [
            ("wFormatTag", ctypes.c_ushort),
            ("nChannels", ctypes.c_ushort),
            ("nSamplesPerSec", ctypes.c_uint),
            ("nAvgBytesPerSec", ctypes.c_uint),
            ("nBlockAlign", ctypes.c_ushort),
            ("wBitsPerSample", ctypes.c_ushort),
            ("cbSize", ctypes.c_ushort),
        ]

    class WAVEHDR(ctypes.Structure):
        _fields_ = [
            ("lpData", ctypes.c_void_p),
            ("dwBufferLength", ctypes.c_uint),
            ("dwBytesRecorded", ctypes.c_uint),
            ("dwUser", ctypes.c_size_t),
            ("dwFlags", ctypes.c_uint),
            ("dwLoops", ctypes.c_uint),
            ("lpNext", ctypes.c_void_p),
            ("reserved", ctypes.c_size_t),
        ]

    def __init__(self, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = int(sample_rate)
        self._winmm = ctypes.windll.winmm
        self._handle = ctypes.c_void_p()
        self._headers: List[Any] = []
        self._buffers: List[Any] = []
        self._chunks: List[bytes] = []
        self._lock = threading.RLock()
        self._draining = False
        self._thread: Optional[threading.Thread] = None

    def _check(self, code: int, operation: str) -> None:
        if code != 0:
            raise SpeechError("record_failed", "media.record_unavailable",
                              detail=f"{operation}: MMRESULT {code}")

    def start(self) -> None:
        """[EN] Open the default input device and begin capturing. / [ZH] 打开默认输入设备并开始采集。"""
        fmt = self.WAVEFORMATEX(
            self.WAVE_FORMAT_PCM, 1, self.sample_rate,
            self.sample_rate * 2, 2, 16, 0,
        )
        self._check(
            self._winmm.waveInOpen(ctypes.byref(self._handle), self.WAVE_MAPPER,
                                   ctypes.byref(fmt), 0, 0, self.CALLBACK_NULL),
            "waveInOpen",
        )
        buffer_size = int(self.sample_rate * 2 * self.BUFFER_SECONDS)
        try:
            for _ in range(self.QUEUE_DEPTH):
                buffer = ctypes.create_string_buffer(buffer_size)
                header = self.WAVEHDR()
                header.lpData = ctypes.cast(buffer, ctypes.c_void_p)
                header.dwBufferLength = buffer_size
                header.dwFlags = 0
                self._check(
                    self._winmm.waveInPrepareHeader(self._handle, ctypes.byref(header),
                                                    ctypes.sizeof(header)),
                    "waveInPrepareHeader",
                )
                self._check(
                    self._winmm.waveInAddBuffer(self._handle, ctypes.byref(header),
                                                ctypes.sizeof(header)),
                    "waveInAddBuffer",
                )
                self._buffers.append(buffer)
                self._headers.append(header)
            self._check(self._winmm.waveInStart(self._handle), "waveInStart")
        except Exception:
            self._release_headers()
            self._close()
            raise
        self._thread = threading.Thread(target=self._poll, name="echodesk-record",
                                        daemon=True)
        self._thread.start()

    def _poll(self) -> None:
        while True:
            with self._lock:
                draining = self._draining
                if draining and all(h.dwFlags & self.WHDR_DONE for h in self._headers):
                    return
                for header in self._headers:
                    if header.dwFlags & self.WHDR_DONE:
                        self._chunks.append(
                            ctypes.string_at(header.lpData, header.dwBytesRecorded))
                        header.dwFlags = 0
                        if not draining:
                            self._winmm.waveInAddBuffer(
                                self._handle, ctypes.byref(header),
                                ctypes.sizeof(header))
            time.sleep(0.02)

    def stop(self) -> bytes:
        """[EN] Stop capturing and return the concatenated PCM payload. / [ZH] 停止采集并返回拼接后的 PCM 负载。"""
        with self._lock:
            self._draining = True
        self._winmm.waveInStop(self._handle)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        with self._lock:
            # [EN] Final sweep for buffers completed after the polling thread exited.
            # [ZH] 对轮询线程退出后才完成的缓冲做最终清扫。
            for header in self._headers:
                if header.dwFlags & self.WHDR_DONE:
                    self._chunks.append(
                        ctypes.string_at(header.lpData, header.dwBytesRecorded))
                    header.dwFlags = 0
            payload = b"".join(self._chunks)
            self._chunks = []
        self._winmm.waveInReset(self._handle)
        self._release_headers()
        self._close()
        return payload

    def _release_headers(self) -> None:
        for header in self._headers:
            try:
                self._winmm.waveInUnprepareHeader(self._handle, ctypes.byref(header),
                                                  ctypes.sizeof(header))
            except Exception:  # noqa: BLE001 - [EN] best-effort native cleanup
                               # [ZH] 尽力清理原生资源
                pass
        self._headers = []
        self._buffers = []

    def _close(self) -> None:
        if getattr(self._handle, "value", None):
            try:
                self._winmm.waveInClose(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = ctypes.c_void_p()


class _ProcessRecorder:
    """
    [EN]
    POSIX recorder backed by an external capture process (``arecord`` on Linux,
    ``ffmpeg`` on macOS). Stopping the process finalizes the container header.

    [ZH]
    由外部采集进程驱动的 POSIX 录音器（Linux 的 ``arecord``、macOS 的 ``ffmpeg``）。
    停止进程即完成容器头部。
    """

    def __init__(self, target: str, sample_rate: int = SAMPLE_RATE) -> None:
        self.target = target
        self.sample_rate = int(sample_rate)
        self.process: Optional[subprocess.Popen] = None

    def _command(self) -> List[str]:
        if sys.platform == "darwin":
            executable = shutil.which("ffmpeg")
            if not executable:
                raise SpeechError("no_engine", "media.record_unavailable", detail="ffmpeg")
            return [executable, "-y", "-f", "avfoundation", "-i", ":0",
                    "-ar", str(self.sample_rate), "-ac", "1", self.target]
        executable = shutil.which("arecord")
        if not executable:
            raise SpeechError("no_engine", "media.record_unavailable", detail="arecord")
        return [executable, "-q", "-f", "S16_LE", "-r", str(self.sample_rate),
                "-c", "1", "-t", "wav", self.target]

    def start(self) -> None:
        """[EN] Launch the capture process. / [ZH] 启动采集进程。"""
        self.process = subprocess.Popen(
            self._command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # [EN] A process that exits immediately means the device is unavailable.
        # [ZH] 进程立刻退出说明设备不可用。
        time.sleep(0.15)
        if self.process.poll() is not None:
            raise SpeechError("record_failed", "media.record_unavailable",
                              detail=f"capture exit {self.process.returncode}")

    def stop(self) -> bytes:
        """[EN] Terminate the capture process and return the finished file. / [ZH] 终止采集进程并返回成品文件。"""
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return b""


class AudioRecorder:
    """
    [EN]
    Push-to-talk recorder with an explicit state machine.

    Only one recording may be active. The recorder writes a WAV file below
    ``temp/audio`` when it stops; ``cancel`` discards the data without writing.
    A watchdog stops an over-long recording automatically so a missed key-release
    cannot fill the disk.

    [ZH]
    带显式状态机的按键录音器。

    同一时刻只允许一段录音。停止时在 ``temp/audio`` 下写出 WAV 文件；``cancel``
    丢弃数据且不写文件。看门狗会自动停止超长录音，避免漏掉的松键事件占满磁盘。
    """

    def __init__(self, max_seconds: int = MAX_RECORD_SECONDS,
                 sample_rate: int = SAMPLE_RATE) -> None:
        self.max_seconds = max(5, min(600, int(max_seconds or MAX_RECORD_SECONDS)))
        self.sample_rate = int(sample_rate)
        self._lock = threading.RLock()
        self._backend: Any = None
        self._target = ""
        self._started_at = 0.0
        self._watchdog: Optional[threading.Timer] = None
        self._active = False

    @property
    def active(self) -> bool:
        """[EN] Whether a recording is in progress. / [ZH] 是否正在录音。"""
        with self._lock:
            return self._active

    def start(self) -> Dict[str, Any]:
        """
        [EN]
        Begin recording and return ``{"path", "started_at", "max_seconds"}``.

        [ZH]
        开始录音并返回 ``{"path", "started_at", "max_seconds"}``。
        """
        with self._lock:
            if self._active:
                raise SpeechError("already_recording", "media.record_already_active")
            ensure_media_dirs()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = os.path.join(audio_dir(), f"rec-{stamp}-{os.getpid()}.wav")
            backend: Any
            if sys.platform == "win32":
                backend = _WindowsWaveInRecorder(self.sample_rate)
            else:
                backend = _ProcessRecorder(target, self.sample_rate)
                backend.target = target
            backend.start()
            self._backend = backend
            self._target = target
            self._started_at = time.time()
            self._active = True
            self._watchdog = threading.Timer(self.max_seconds, self._auto_stop)
            self._watchdog.daemon = True
            self._watchdog.start()
            return {"path": target, "started_at": self._started_at,
                    "max_seconds": self.max_seconds}

    def _auto_stop(self) -> None:
        """[EN] Watchdog body: stop an over-long recording. / [ZH] 看门狗主体：停止超长录音。"""
        try:
            if self.active:
                log.warning("录音超过 %s 秒，已自动停止", self.max_seconds)
                self.stop()
        except Exception:  # noqa: BLE001 - [EN] the watchdog must never raise
                           # [ZH] 看门狗绝不抛出
            log.exception("自动停止录音失败")

    def stop(self) -> Dict[str, Any]:
        """
        [EN]
        Finish recording, write the WAV file and return its metadata.

        [ZH]
        结束录音、写出 WAV 文件并返回其元数据。
        """
        with self._lock:
            if not self._active:
                raise SpeechError("not_recording", "media.record_not_active")
            backend = self._backend
            target = self._target
            started = self._started_at
            self._active = False
            self._backend = None
            watchdog = self._watchdog
            self._watchdog = None
            if watchdog is not None:
                watchdog.cancel()
        try:
            payload = backend.stop()
            if payload:
                write_wav(target, payload, sample_rate=self.sample_rate)
            # [EN] The process backend writes the container itself; the Windows backend
            # [EN] returns PCM and needs the WAV wrapper above.
            # [ZH] 进程后端自行写出容器；Windows 后端返回 PCM，需要上面的 WAV 封装。
            if not os.path.isfile(target) or os.path.getsize(target) <= 44:
                raise SpeechError("empty_recording", "media.record_empty")
            duration = read_wav_duration(target) or (time.time() - started)
            return {"path": os.path.abspath(target),
                    "duration_seconds": round(float(duration), 3),
                    "size_bytes": os.path.getsize(target)}
        except SpeechError:
            raise
        except Exception as exc:  # noqa: BLE001 - [EN] finalize failure
                                     # [ZH] 收尾失败
            raise SpeechError("stop_failed", "media.record_unavailable",
                              detail=str(exc)) from exc

    def cancel(self) -> None:
        """
        [EN]
        Abort recording and discard the captured data. Does nothing when idle.

        [ZH]
        中止录音并丢弃已采集数据。空闲时为空操作。
        """
        with self._lock:
            if not self._active:
                return
            backend = self._backend
            target = self._target
            self._active = False
            self._backend = None
            watchdog = self._watchdog
            self._watchdog = None
            if watchdog is not None:
                watchdog.cancel()
        try:
            backend.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if target and os.path.isfile(target):
                os.remove(target)
        except OSError:
            pass
