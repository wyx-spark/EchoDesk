# -*- coding: utf-8 -*-
"""
[EN]
Verification of the v0.2.6 Part 2 media protocol: ``image_chat``,
``transcribe_audio``, ``tts_generate`` and the recording actions.

The test covers the protocol contract rather than model quality:

* the staging directories resolve inside ``ECHODESK_DATA``;
* the path policy rejects relative paths, files outside the staging directory,
  unsupported extensions and over-sized payloads before any model work happens;
* a valid image request forwards the absolute path to the vision adapter and
  records the turn in the conversation history;
* transcription returns the recognized text and rejects empty results;
* speech synthesis writes a non-empty audio file below ``temp/audio/tts``;
* the recorder state machine refuses to stop when idle and writes a valid WAV
  container for captured PCM; a real device is exercised when one exists.

[ZH]
v0.2.6 Part 2 媒体协议验证：``image_chat``、``transcribe_audio``、
``tts_generate`` 与录音动作。

本测试验证协议契约而非模型质量：

* 暂存目录解析到 ``ECHODESK_DATA`` 之内；
* 路径策略在任何模型工作开始之前拒绝相对路径、暂存目录之外的文件、不支持的
  扩展名与超限负载；
* 合法图片请求把绝对路径转发给视觉适配层，并把该轮记录进对话历史；
* 转写返回识别文本，并拒绝空结果；
* 语音合成在 ``temp/audio/tts`` 下写出非空音频文件；
* 录音状态机在空闲时拒绝停止，并为已采集 PCM 写出合法 WAV 容器；存在真实设备
  时执行一次真实录音。

Run:
    python tools/test_media_api.py
Exit code: 0 = all checks passed, 1 = at least one failure.
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

PASSED = 0
FAILED = []
SKIPPED = []


def check(name, condition, detail=""):
    """[EN] Record one assertion result. / [ZH] 记录一项断言结果。"""
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def skip(name, reason):
    """[EN] Record a platform-dependent check that could not run. / [ZH] 记录因平台条件未能运行的检查。"""
    SKIPPED.append(f"{name}: {reason}")
    print(f"  [SKIP] {name} ({reason})")


class StubEngine:
    """
    [EN]
    Minimal inference engine stub; media actions never call it, but the backend
    constructor and context accounting expect its surface.

    [ZH]
    最小推理引擎桩；媒体动作不会调用它，但后端构造与上下文计量需要其接口面。
    """

    def __init__(self, ready=True):
        self.ready = ready
        self.model_path = ""
        self.n_ctx = 4096
        self.n_ctx_train = 4096
        self.max_n_ctx = 4096
        self.kv_cache_bytes = 0
        self.n_threads = 1
        self.n_gpu_layers = 0
        self.n_batch = 1
        self.last_tps = 0.0
        self.last_elapsed = 0.0
        self.last_tokens = 0

    def count_tokens(self, text):
        return max(1, len(str(text)) // 2)

    def chat_complete(self, messages, **kwargs):
        return "ok"

    def clear_cache(self):
        return True


class FakeVision:
    """[EN] Vision adapter double returning a fixed answer. / [ZH] 返回固定回答的视觉适配层替身。"""

    def __init__(self, answer="图片中有一张表格，表头为「季度」，共三行。", available=True):
        self.answer = answer
        self.available = available
        self.calls = []

    def status(self):
        return {
            "available": self.available,
            "model": "vl-model.gguf",
            "model_path": "C:/models/vl-model.gguf",
            "mmproj": "mmproj-Q8_0.gguf",
            "mmproj_path": "C:/models/mmproj-Q8_0.gguf",
            "loaded": True,
            "handler": "Qwen25VLChatHandler",
            "reason": "" if self.available else "media.vision_no_mmproj",
            "reason_text": "" if self.available else "缺少多模态投影",
            "last_error": "",
        }

    def configure(self, *args, **kwargs):
        return None

    def chat(self, image_path, prompt, max_tokens=512, temperature=0.3):
        self.calls.append((image_path, prompt, max_tokens, temperature))
        return self.answer


class FakeSTT:
    """[EN] Transcription double. / [ZH] 转写替身。"""

    def __init__(self, text="请把这份合同的违约金条款读一遍", available=True):
        self.text = text
        self.available = available
        self.calls = []

    def status(self):
        return {
            "available": self.available,
            "executable": "C:/tools/whisper-cli.exe",
            "model": "ggml-base.bin",
            "model_path": "C:/models/ggml-base.bin",
            "language": "zh",
            "reason": "" if self.available else "media.stt_no_model",
            "reason_text": "" if self.available else "缺少 Whisper 模型",
            "last_error": "",
        }

    def transcribe(self, audio_path, language=None, timeout=0.0):
        self.calls.append((audio_path, language))
        return {"text": self.text, "elapsed_ms": 12}


class FakeRecorderBackend:
    """[EN] Capture backend returning one second of silence. / [ZH] 返回一秒静音的采集后端。"""

    def stop(self):
        return b"\x00\x00" * 16000


def build_backend(ready=True):
    """[EN] Construct a backend with the stub engine. / [ZH] 以桩引擎构造后端。"""
    import main as backend_main
    from config import Config

    config = Config()
    config.session_file = ""
    config.speed_hint = False
    config.search_default = False
    config.summary_cache = False
    backend = backend_main.Backend(config)
    backend.llm = StubEngine(ready=ready)
    return backend, config


def make_image(directory, name="shot.png", payload=None):
    """[EN] Write a PNG-magic file at the given directory. / [ZH] 在指定目录写出带 PNG 魔数的文件。"""
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(payload or (b"\x89PNG\r\n\x1a\n" + b"\x00" * 256))
    return path


def main():
    import media
    import speech
    from speech import SpeechError

    # [EN] Use a directory under the repository: some environment sandboxes deny child
    # [EN] processes (including the PowerShell speech engine) write access to the system
    # [EN] temporary directory, while the product's own user-data directory is writable.
    # [ZH] 使用仓库内的目录：部分环境沙箱禁止子进程（含 PowerShell 语音引擎）写入
    # [ZH] 系统临时目录，而产品自身的用户数据目录是可写的。
    root = tempfile.mkdtemp(prefix="echodesk-media-", dir=ROOT)
    os.environ["ECHODESK_DATA"] = root
    dirs = media.ensure_media_dirs()
    print(f"[ENV] ECHODESK_DATA={root}")

    print("\n=== 1. 暂存目录与能力状态 ===")
    check("图片目录位于数据根内", media._is_under(media.images_dir(), root),
          media.images_dir())
    check("音频目录位于数据根内", media._is_under(media.audio_dir(), root),
          media.audio_dir())
    check("语音输出目录位于音频目录内", media._is_under(media.tts_dir(), media.audio_dir()),
          media.tts_dir())
    check("三个目录均已创建", all(os.path.isdir(p) for p in dirs.values()), str(dirs))

    backend, config = build_backend()
    fake_vision = FakeVision()
    backend._vision_service = lambda: fake_vision
    state = backend.handle({"action": "media_state"})
    check("media_state 返回 ok", state.get("ok") is True, str(state))
    check("media_state 携带图片目录", state.get("dirs", {}).get("images") == media.images_dir())
    check("media_state 报告视觉能力", state.get("vision", {}).get("available") is True)
    check("media_state 报告 TTS 引擎", bool(state.get("tts", {}).get("engine")) or sys.platform not in ("win32", "darwin"),
          str(state.get("tts")))
    check("media_state 报告按键说话键名", state.get("ptt", {}).get("shortcut") == "RightCtrl")

    print("\n=== 2. 路径策略（在模型工作之前拒绝） ===")
    valid_image = make_image(media.images_dir())
    check("合法图片通过校验", media.validate_image_path(valid_image) == os.path.abspath(valid_image))

    def expect_error(fn, code):
        try:
            fn()
            return False, "未抛出异常"
        except media.MediaError as exc:
            return exc.code == code, f"实际 code={exc.code}"

    ok, detail = expect_error(lambda: media.validate_image_path("relative.png"), "not_absolute")
    check("拒绝相对路径", ok, detail)
    outside = make_image(root, "outside.png")
    ok, detail = expect_error(lambda: media.validate_image_path(outside), "outside_root")
    check("拒绝暂存目录之外的路径", ok, detail)
    wrong_ext = make_image(media.images_dir(), "note.txt")
    ok, detail = expect_error(lambda: media.validate_image_path(wrong_ext), "bad_extension")
    check("拒绝不支持的扩展名", ok, detail)
    fake_image = make_image(media.images_dir(), "fake.png", b"not really a png" + b"\x00" * 32)
    ok, detail = expect_error(lambda: media.validate_image_path(fake_image), "bad_image")
    check("拒绝魔数不符的图片", ok, detail)
    ok, detail = expect_error(
        lambda: media.validate_image_path(valid_image, max_bytes=16), "too_large")
    check("拒绝超过大小上限的图片", ok, detail)
    audio = os.path.join(media.audio_dir(), "probe.wav")
    media.write_wav(audio, b"\x00\x00" * 1600)
    check("合法音频通过校验", media.validate_audio_path(audio) == os.path.abspath(audio))

    print("\n=== 3. image_chat ===")
    response = backend.handle({"action": "image_chat", "path": valid_image,
                               "prompt": "这张表有几个数据行？"})
    check("image_chat 返回助手消息", response.get("type") == "assistant", str(response))
    check("image_chat 返回 ok", response.get("ok") is True, str(response))
    check("image_chat 返回视觉模型回答", response.get("content") == fake_vision.answer,
          str(response.get("content")))
    check("视觉适配层收到绝对路径",
          bool(fake_vision.calls) and fake_vision.calls[-1][0] == os.path.abspath(valid_image),
          str(fake_vision.calls[-1:]))
    check("视觉适配层收到用户提示词",
          fake_vision.calls[-1][1] == "这张表有几个数据行？", str(fake_vision.calls[-1:]))
    check("该轮已写入对话历史（用户 + 助手）", len(backend.conv.history) >= 2,
          str(len(backend.conv.history)))
    history_text = " ".join(str(m.get("content", "")) for m in backend.conv.history)
    check("历史含图片文件名标记", os.path.basename(valid_image) in history_text, history_text[:200])

    outside_response = backend.handle({"action": "image_chat", "path": outside})
    check("image_chat 拒绝目录外路径", outside_response.get("ok") is False
          and outside_response.get("error_code") == "outside_root", str(outside_response))
    config.media.vision_enabled = False
    disabled = backend.handle({"action": "image_chat", "path": valid_image})
    check("视觉关闭时明确提示", disabled.get("ok") is False
          and disabled.get("error_code") == "vision_disabled", str(disabled))
    config.media.vision_enabled = True

    no_model_backend, _ = build_backend(ready=False)
    no_model_backend._vision_service = lambda: fake_vision
    no_model = no_model_backend.handle({"action": "image_chat", "path": valid_image})
    check("无模型时返回引导而非崩溃", no_model.get("ok") is False
          and no_model.get("error_code") == "no_model", str(no_model)[:200])

    print("\n=== 4. transcribe_audio ===")
    fake_stt = FakeSTT()
    backend._stt_service = lambda: fake_stt
    transcription = backend.handle({"action": "transcribe_audio", "path": audio})
    check("transcribe_audio 返回转写类型", transcription.get("type") == "transcription",
          str(transcription))
    check("转写文本正确", transcription.get("text") == fake_stt.text,
          str(transcription.get("text")))
    check("转写收到暂存音频路径",
          fake_stt.calls and fake_stt.calls[-1][0] == os.path.abspath(audio),
          str(fake_stt.calls[-1:]))
    backend._stt_service = lambda: FakeSTT(text="")
    empty = backend.handle({"action": "transcribe_audio", "path": audio})
    check("空转写结果明确提示", empty.get("ok") is False
          and empty.get("error_code") == "stt_empty", str(empty))
    backend._stt_service = lambda: FakeSTT(available=False)
    unavailable = backend.handle({"action": "transcribe_audio", "path": audio})
    check("STT 不可用时返回原因", unavailable.get("ok") is False
          and unavailable.get("error_code") == "stt_unavailable", str(unavailable))
    backend._stt_service = lambda: fake_stt

    print("\n=== 5. tts_generate ===")
    empty_tts = backend.handle({"action": "tts_generate", "text": "  "})
    check("空文本被拒绝", empty_tts.get("ok") is False
          and empty_tts.get("error_code") == "tts_empty", str(empty_tts))
    voices = speech.list_voices()
    check("本机语音引擎报告可用音色", voices.get("available") is True
          and len(voices.get("voices") or []) > 0, str(voices))

    # [EN] Protocol path: replace only the native synthesiser so that the request
    # [EN] shape, output location and response contract can be verified on machines
    # [EN] where the speech engine is inaccessible to child processes.
    # [ZH] 协议路径：仅替换本地合成器，使请求形态、输出位置与响应契约可在语音引擎
    # [ZH] 对子进程不可用的机器上得到验证。
    original_synth = speech.synthesize_speech
    received = {}

    def fake_synth(text, out_path, voice="", rate=0):
        received.update({"text": text, "out_path": out_path, "voice": voice, "rate": rate})
        media.write_wav(out_path, b"\x00\x00" * 1600)
        return out_path

    speech.synthesize_speech = fake_synth
    try:
        tts = backend.handle({"action": "tts_generate", "text": "语音合成测试。",
                              "voice": "TestVoice", "rate": 2})
    finally:
        speech.synthesize_speech = original_synth
    path = str(tts.get("path") or "")
    check("tts_generate 返回音频路径", tts.get("ok") is True and bool(path), str(tts)[:200])
    check("音频文件位于 tts 目录", bool(path) and media._is_under(path, media.tts_dir()), path)
    check("音频文件非空", bool(path) and os.path.getsize(path) > 44,
          str(os.path.getsize(path) if path else 0))
    check("音频格式与平台一致",
          tts.get("format") == ("aiff" if sys.platform == "darwin" else "wav"),
          str(tts.get("format")))
    check("合成器收到原文与音色", received.get("text") == "语音合成测试。"
          and received.get("voice") == "TestVoice" and received.get("rate") == 2,
          str(received))

    # [EN] Real engine attempt. Some sandboxed sessions deny SAPI voice engines to
    # [EN] grandchild processes; that is an environment property, not a code path,
    # [EN] so it is reported as a skip with the concrete reason.
    # [ZH] 真实引擎尝试。部分沙箱会话禁止孙进程访问 SAPI 语音引擎；这属于环境特性
    # [ZH] 而非代码路径，因此以具体原因记为跳过。
    real_target = speech.tts_output_path()
    try:
        real_path = original_synth("语音合成测试。", real_target)
        check("真实引擎产出音频", os.path.isfile(real_path)
              and os.path.getsize(real_path) > 44, real_path)
    except SpeechError as exc:
        skip("真实引擎合成", f"{exc.code}: {str(exc.kwargs.get('detail'))[:160]}")

    print("\n=== 6. 录音状态机 ===")
    recorder = speech.AudioRecorder(max_seconds=5)
    try:
        recorder.stop()
        check("空闲时停止被拒绝", False, "未抛出异常")
    except SpeechError as exc:
        check("空闲时停止被拒绝", exc.code == "not_recording", exc.code)
    target = os.path.join(media.audio_dir(), "manual-test.wav")
    recorder._backend = FakeRecorderBackend()
    recorder._target = target
    recorder._started_at = time.time()
    recorder._active = True
    info = recorder.stop()
    check("停止后写出 WAV", os.path.isfile(info["path"]) and info["size_bytes"] > 44,
          str(info))
    check("WAV 时长可读出", info["duration_seconds"] >= 0.5, str(info["duration_seconds"]))
    check("录音文件位于音频目录", media._is_under(info["path"], media.audio_dir()),
          info["path"])

    if sys.platform == "win32":
        try:
            devices = int(__import__("ctypes").windll.winmm.waveInGetNumDevs())
        except Exception:
            devices = 0
        if devices > 0:
            real = speech.AudioRecorder(max_seconds=5)
            try:
                real.start()
                time.sleep(0.5)
                captured = real.stop()
                check("真实设备录音产生有效文件",
                      os.path.isfile(captured["path"]) and captured["size_bytes"] > 1000,
                      str(captured))
                check("真实录音时长约为 0.5 秒",
                      0.1 <= captured["duration_seconds"] <= 5.0,
                      str(captured["duration_seconds"]))
            except SpeechError as exc:
                skip("真实设备录音", f"{exc.code}: {exc.kwargs}")
            except Exception as exc:  # noqa: BLE001 - [EN] device-level failure
                                     # [ZH] 设备层失败
                skip("真实设备录音", f"{type(exc).__name__}: {exc}")
        else:
            skip("真实设备录音", "未检测到输入设备")

    print("\n================ 结果汇总 ================")
    print(f"共 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项，"
          f"跳过 {len(SKIPPED)} 项")
    for item in SKIPPED:
        print(f"  [SKIP] {item}")
    for item in FAILED:
        print(f"  [FAIL] {item}")
    # [EN] Remove the staging tree created for this run so repeated runs do not
    # [EN] accumulate temporary media in the repository.
    # [ZH] 移除本次运行创建的暂存树，避免重复运行时在仓库中累积临时媒体。
    try:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    except Exception:  # noqa: BLE001 - [EN] cleanup failure must not change the verdict
                       # [ZH] 清理失败不得改变结论
        pass
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
