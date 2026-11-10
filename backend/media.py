# -*- coding: utf-8 -*-
"""
[EN]
Media path policy for the multimodal (image, audio) interfaces.

Transport decision (v0.2.6 Part 2): binary payloads never travel through the JSON
line protocol. The interface stage writes an uploaded image or a recording into a
well-known temporary directory under the user data root, and only the absolute
path travels over stdin/stdout. This module is the single authority for

* where those temporary directories live (``<user data>/temp/images``,
  ``<user data>/temp/audio`` and ``<user data>/temp/audio/tts``);
* which absolute paths the backend is willing to read back (containment,
  existence, extension, size and magic-byte checks);
* how a PCM recording is wrapped into a canonical RIFF/WAVE container.

Every rejection is expressed as :class:`MediaError` with a language-pack key, so
the dispatcher can localize the message without re-implementing the policy.

[ZH]
多模态（图片、音频）接口的媒体路径策略。

传输决策（v0.2.6 Part 2）：二进制内容绝不经过 JSON 行协议。界面层把上传的图片或
录音写入用户数据根目录下的约定临时目录，协议上只传递绝对路径。本模块是以下事项
的唯一权威：

* 临时目录的位置（``<用户数据>/temp/images``、``<用户数据>/temp/audio`` 与
  ``<用户数据>/temp/audio/tts``）；
* 后端愿意回读哪些绝对路径（目录包含关系、存在性、扩展名、大小与魔数校验）；
* 如何把 PCM 录音封装为规范的 RIFF/WAVE 容器。

所有拒绝均以 :class:`MediaError` 表达，并携带语言包键，使分发层无需重复实现策略
即可本地化提示。
"""
from __future__ import annotations

import os
import struct
import time
from typing import Iterable, Optional

from config import user_data_root

#: [EN] Image formats accepted by the vision path. / [ZH] 视觉链路接受的图片格式。
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

#: [EN] Audio containers accepted by the transcription path. / [ZH] 转写链路接受的音频容器。
AUDIO_EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus")

#: [EN] Upper bound for one image (20 MiB). Larger screenshots are almost always a
#: [EN] mistake, and the image is decoded into the model context.
#: [ZH] 单张图片上限（20 MiB）。更大的截图几乎总是误操作，且图片会被解码进模型上下文。
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: [EN] Upper bound for one recording (100 MiB, roughly 50 minutes of 16 kHz mono PCM).
#: [ZH] 单段录音上限（100 MiB，约等于 16 kHz 单声道 PCM 的 50 分钟）。
MAX_AUDIO_BYTES = 100 * 1024 * 1024

#: [EN] Default retention for temporary media files, in days. Cleanup only removes
#: [EN] files this module itself created (see :func:`prune_temp_media`).
#: [ZH] 临时媒体文件的默认保留天数。清理只删除本模块自己创建的文件（见
#: [ZH] :func:`prune_temp_media`）。
DEFAULT_RETENTION_DAYS = 7

#: [EN] Magic-byte signatures per image extension. The extension alone is not
#: [EN] trusted: a renamed arbitrary file must not be handed to the decoder.
#: [ZH] 每种图片扩展名对应的魔数签名。不单独信任扩展名：被改名的任意文件不得交给
#: [ZH] 解码器。
_IMAGE_MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".webp": (b"RIFF",),
    ".bmp": (b"BM",),
}


class MediaError(Exception):
    """
    [EN]
    A rejected media operation.

    ``code`` is the stable machine-readable reason, ``key`` is the backend
    language-pack key and ``kwargs`` are the placeholders of that key. The
    dispatcher localizes with ``_t(key, **kwargs)``.

    [ZH]
    被拒绝的媒体操作。

    ``code`` 是稳定的机器可读原因，``key`` 是后端语言包键，``kwargs`` 是该键的
    占位符。分发层使用 ``_t(key, **kwargs)`` 本地化。
    """

    def __init__(self, code: str, key: str, **kwargs: object) -> None:
        super().__init__(code)
        self.code = str(code)
        self.key = str(key)
        self.kwargs = dict(kwargs)

    def localize(self, translate) -> str:
        """
        [EN] Render this error through the supplied translation function.
        [ZH] 使用给定的翻译函数渲染该错误。
        """
        try:
            return str(translate(self.key, **self.kwargs))
        except Exception:  # noqa: BLE001 - [EN] localization must never mask the original error
                           # [ZH] 本地化失败不得掩盖原始错误
            return self.code


def _data_root() -> str:
    """[EN] Absolute user data root. / [ZH] 用户数据根目录的绝对路径。"""
    try:
        return os.path.abspath(user_data_root())
    except Exception:  # noqa: BLE001 - [EN] a broken environment still needs a usable directory
                       # [ZH] 环境异常时仍需给出可用目录
        return os.path.abspath(os.getcwd())


def media_root() -> str:
    """[EN] ``<user data>/temp``. / [ZH] ``<用户数据>/temp``。"""
    return os.path.join(_data_root(), "temp")


def images_dir() -> str:
    """[EN] Image staging directory. / [ZH] 图片暂存目录。"""
    return os.path.join(media_root(), "images")


def audio_dir() -> str:
    """[EN] Audio staging directory. / [ZH] 音频暂存目录。"""
    return os.path.join(media_root(), "audio")


def tts_dir() -> str:
    """[EN] Generated speech output directory. / [ZH] 语音合成输出目录。"""
    return os.path.join(audio_dir(), "tts")


def ensure_media_dirs() -> dict:
    """
    [EN]
    Create the three staging directories and return their absolute paths.

    Directory creation is idempotent. A failure is deliberately not raised here:
    the caller (the action handler) reports it for the directory that is actually
    needed, with the operating-system reason.

    [ZH]
    创建三个暂存目录并返回其绝对路径。

    目录创建是幂等的。此处刻意不抛出失败：由调用方（动作处理器）针对真正需要的
    目录报告失败，并附上操作系统给出的原因。
    """
    paths = {"images": images_dir(), "audio": audio_dir(), "tts": tts_dir()}
    for path in paths.values():
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            # [EN] The write attempt performed by the caller produces the definitive error.
            # [ZH] 最终错误由调用方的实际写入失败给出。
            pass
    return paths


def _is_under(path: str, root: str) -> bool:
    """
    [EN]
    Whether ``path`` is inside ``root`` after resolving symbolic links.

    ``commonpath`` is used instead of a string prefix so that ``temp/images-old``
    is not mistaken for a child of ``temp/images``. A path that cannot be compared
    (for example due to different drives on Windows) is rejected.

    [ZH]
    解析符号链接后，``path`` 是否位于 ``root`` 内。

    使用 ``commonpath`` 而不是字符串前缀，避免把 ``temp/images-old`` 误判为
    ``temp/images`` 的子项。无法比较的路径（例如 Windows 上分属不同驱动器）一律拒绝。
    """
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_path, real_root]) == real_root
    except Exception:  # noqa: BLE001 - [EN] any comparison failure is treated as "outside"
                       # [ZH] 任何比较失败都按"不在目录内"处理
        return False


def _validate_common(path: str, extensions: Iterable[str], max_bytes: int,
                     root: str, kind: str) -> str:
    """
    [EN]
    Shared validation for a staged media file, returning its resolved absolute path.

    Checks, in order: non-empty value, absolute path, existence, regular file,
    allowed extension, containment under the staging root, and size. The order is
    chosen so that a path outside the staging area is rejected before any metadata
    of the target is read.

    [ZH]
    暂存媒体文件的共同校验，返回解析后的绝对路径。

    依次检查：非空、绝对路径、存在、常规文件、扩展名允许、位于暂存根目录内、大小。
    该顺序保证目录外的路径在读取目标元数据之前即被拒绝。
    """
    raw = str(path or "").strip()
    if not raw:
        raise MediaError("empty_path", "media.error.empty_path")
    if not os.path.isabs(raw):
        raise MediaError("not_absolute", "media.error.not_absolute", path=raw)
    resolved = os.path.abspath(raw)
    if not os.path.exists(resolved):
        raise MediaError("not_found", "media.error.not_found", path=resolved)
    if not os.path.isfile(resolved):
        raise MediaError("not_file", "media.error.not_file", path=resolved)
    extension = os.path.splitext(resolved)[1].lower()
    if extension not in extensions:
        raise MediaError("bad_extension", "media.error.bad_extension",
                         path=resolved, formats=", ".join(sorted(extensions)))
    if not _is_under(resolved, root):
        raise MediaError("outside_root", "media.error.outside_root",
                         path=resolved, root=os.path.abspath(root))
    try:
        size = os.path.getsize(resolved)
    except OSError as exc:
        raise MediaError("unreadable", "media.error.unreadable",
                         path=resolved, reason=str(exc)) from exc
    if size <= 0:
        raise MediaError("empty", "media.error.empty", path=resolved)
    if size > max_bytes:
        raise MediaError("too_large", "media.error.too_large",
                         path=resolved, limit=_format_mib(max_bytes), kind=kind)
    return resolved


def _format_mib(value: int) -> str:
    """[EN] Format a byte count as MiB with one decimal. / [ZH] 把字节数格式化为一位小数的 MiB。"""
    return f"{value / (1024 * 1024):.1f} MiB"


def _check_image_magic(path: str, extension: str) -> None:
    """
    [EN]
    Verify the magic bytes of an image file.

    The check is intentionally shallow: it proves that the file is the container
    the extension claims, not that the payload is fully decodable. Full decoding is
    performed by the vision backend, whose error is reported separately.

    [ZH]
    校验图片文件的魔数。

    该校验刻意保持浅层：只证明文件确实是对应扩展名所声明的容器，不证明负载可完整
    解码。完整解码由视觉后端执行，其错误另行上报。
    """
    signatures = _IMAGE_MAGIC.get(extension, ())
    if not signatures:
        return
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError as exc:
        raise MediaError("unreadable", "media.error.unreadable",
                         path=path, reason=str(exc)) from exc
    if extension == ".webp" and head[:4] == b"RIFF":
        # [EN] A RIFF container is WebP only when bytes 8..11 are "WEBP".
        # [ZH] 仅当第 8..11 字节为 "WEBP" 时 RIFF 容器才是 WebP。
        if head[8:12] != b"WEBP":
            raise MediaError("bad_image", "media.error.bad_image", path=path)
        return
    if not any(head.startswith(signature) for signature in signatures):
        raise MediaError("bad_image", "media.error.bad_image", path=path)


def validate_image_path(path: str, max_bytes: int = MAX_IMAGE_BYTES) -> str:
    """
    [EN]
    Validate a staged image and return its absolute path.

    [ZH]
    校验暂存图片并返回其绝对路径。
    """
    resolved = _validate_common(path, IMAGE_EXTENSIONS, max_bytes, images_dir(), "image")
    _check_image_magic(resolved, os.path.splitext(resolved)[1].lower())
    return resolved


def validate_audio_path(path: str, max_bytes: int = MAX_AUDIO_BYTES) -> str:
    """
    [EN]
    Validate a staged audio file and return its absolute path.

    [ZH]
    校验暂存音频文件并返回其绝对路径。
    """
    return _validate_common(path, AUDIO_EXTENSIONS, max_bytes, audio_dir(), "audio")


def staged_image_target(source_name: str) -> str:
    """
    [EN]
    Build a unique destination path inside the image staging directory.

    The interface stage may use this to copy a picked file; the backend never
    writes images itself. The returned name preserves a sanitized stem and forces
    a ``.png`` extension, because screenshot capture always produces PNG data.

    [ZH]
    在图片暂存目录内构造唯一的目标路径。

    界面层可用于复制用户选择的文件；后端自身从不写入图片。返回值保留经清洗的
    文件名主体，并强制 ``.png`` 扩展名，因为截图捕获始终产出 PNG 数据。
    """
    stem = "".join(ch for ch in os.path.splitext(os.path.basename(str(source_name)))[0]
                   if ch.isalnum() or ch in ("-", "_", " ")).strip() or "image"
    return os.path.join(images_dir(), f"{stem}-{int(time.time() * 1000)}.png")


def write_wav(path: str, pcm: bytes, sample_rate: int = 16000,
              channels: int = 1, bits: int = 16) -> str:
    """
    [EN]
    Write interleaved PCM samples into a canonical 44-byte-header RIFF/WAVE file.

    The header is written explicitly rather than through the ``wave`` module so
    that the recorder can stream buffers of unknown final length: the recorder
    collects PCM first and calls this function once when recording stops.

    [ZH]
    把交错 PCM 采样写入带标准 44 字节头部的 RIFF/WAVE 文件。

    头部显式写出而不使用 ``wave`` 模块，以便录音器处理最终长度未知的缓冲：录音器
    先收集 PCM，停止时调用本函数一次。
    """
    if bits != 16:
        raise ValueError("only 16-bit PCM is supported")
    block_align = max(1, channels) * (bits // 8)
    byte_rate = max(1, sample_rate) * block_align
    data = bytes(pcm)
    # [EN] RIFF chunk size covers "WAVE" plus the two sub-chunks (8 + 16 + 8 + data).
    # [ZH] RIFF 块大小覆盖 "WAVE" 与两个子块（8 + 16 + 8 + data）。
    riff_size = 4 + (8 + 16) + (8 + len(data))
    header = b"".join((
        b"RIFF", struct.pack("<I", riff_size), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                             byte_rate, block_align, bits),
        b"data", struct.pack("<I", len(data)),
    ))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(data)
    return os.path.abspath(path)


def read_wav_duration(path: str) -> float:
    """
    [EN]
    Return the duration of a PCM WAVE file in seconds, or ``0.0`` when the header
    cannot be interpreted.

    [ZH]
    返回 PCM WAVE 文件的时长（秒）；头部无法解释时返回 ``0.0``。
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(44)
            if len(header) < 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return 0.0
            byte_rate = struct.unpack("<I", header[28:32])[0]
            data_size = struct.unpack("<I", header[40:44])[0]
            return float(data_size) / byte_rate if byte_rate else 0.0
    except Exception:  # noqa: BLE001 - [EN] a malformed file simply has no duration
                       # [ZH] 损坏文件直接视为没有时长
        return 0.0


def prune_temp_media(retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """
    [EN]
    Delete staged media older than ``retention_days`` and return the number of
    files removed.

    Only regular files directly inside the staging directories are considered;
    subdirectories are left untouched. ``retention_days <= 0`` disables cleanup.

    [ZH]
    删除超过 ``retention_days`` 天的暂存媒体，并返回删除的文件数。

    只考虑暂存目录内的常规文件，子目录保持不动。``retention_days <= 0`` 表示关闭清理。
    """
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for directory in (images_dir(), audio_dir(), tts_dir()):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.remove(entry.path)
                    removed += 1
            except OSError:
                continue
    return removed


def describe_dirs() -> dict:
    """
    [EN]
    Machine-readable staging directory description for ``media_state``.

    [ZH]
    供 ``media_state`` 使用的机器可读暂存目录描述。
    """
    return {
        "root": media_root(),
        "images": images_dir(),
        "audio": audio_dir(),
        "tts": tts_dir(),
        "max_image_bytes": MAX_IMAGE_BYTES,
        "max_audio_bytes": MAX_AUDIO_BYTES,
        "image_extensions": list(IMAGE_EXTENSIONS),
        "audio_extensions": list(AUDIO_EXTENSIONS),
    }


def optional_path(value: Optional[str]) -> str:
    """[EN] Normalize an optional configured path. / [ZH] 规范化可选配置路径。"""
    return os.path.abspath(os.path.expanduser(str(value))) if value else ""
