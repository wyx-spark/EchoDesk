"""
[EN]
Native llama.cpp runtime used by the Reader edition. The former Python binding
(``llama-cpp-python``) is physically removed: this module talks to the shared
library shipped in ``backend/native`` directly through the vendored C ABI
declarations in :mod:`llama_capi`. It owns the model, the context, the KV cache and
the sampling chain, and exposes the small slice of behaviour the engine needs.

Why the rewrite: the previous wrapper kept a hidden long-lived native state in
Python objects, and any concurrent use of the same model (chat thread plus the
background document summarizer) could reach a C++ access violation that no Python
exception handling can intercept. Here every native call runs under the engine's
process-wide mutex, the model is loaded once, and the KV cache is cleared before
each request, so the native state can never be observed halfway through a request.

Design notes:
  * Parameters from ``inference_config.json`` are mapped one-to-one onto
    ``llama_model_params`` / ``llama_context_params`` (threads, batch, ubatch,
    mlock, mmap, FlashAttention, K/V cache quantization, GPU layers).
  * Prefill is fed in ``n_batch``-sized chunks; decode produces one token per
    ``llama_decode`` call and yields text incrementally.
  * Sampling uses llama.cpp's own sampler chain (penalties -> top_k -> top_p ->
    temperature -> distribution), so results stay comparable with upstream.
  * A chunk may split a multi-byte UTF-8 sequence; an incremental decoder keeps
    the stream valid without dropping characters.

[ZH]
Reader 版使用的原生 llama.cpp 运行期。旧的 Python 绑定（``llama-cpp-python``）已物理
移除：本模块通过 :mod:`llama_capi` 中就地内置的 C ABI 声明，直接调用 ``backend/native``
下的动态库。模型、上下文、KV Cache 与采样链都由本模块持有，并对外暴露引擎所需的那一
小部分行为。

重写原因：旧封装把长期存活的本地状态藏在 Python 对象里，聊天线程与后台文档摘要器一旦
并发使用同一个模型，就可能触发 C++ 访问违例 —— 那类崩溃无法被任何 Python 异常处理拦截。
现在所有原生调用都在引擎的进程级互斥锁内执行，模型只加载一次，且每个请求前清空
KV Cache，因此原生状态永远不会被观察到"半个请求"的中间态。

设计要点：
  * ``inference_config.json`` 中的参数一一映射到 ``llama_model_params`` /
    ``llama_context_params``（线程、批大小、ubatch、mlock、mmap、FlashAttention、
    K/V Cache 量化、GPU 层数）。
  * prefill 按 ``n_batch`` 分块喂入；decode 每次调用产生一个 token，并增量产出文本。
  * 采样使用 llama.cpp 自带的采样链（惩罚 -> top_k -> top_p -> 温度 -> 分布），
    与上游结果保持可比。
  * 一个 chunk 可能截断多字节 UTF-8 序列；增量解码器保证流始终合法且不丢字符。
"""
import codecs
import ctypes
import logging
import math
import os
import sys
import threading
import time
from typing import Dict, Iterator, List, Optional

log = logging.getLogger("echodesk.llama_native")

# [EN] Default sampling values. They are only fallbacks: the engine always passes
# [EN] the values derived from the preset / request.
# [ZH] 采样默认值，仅作兜底：引擎始终传入由预设/请求推导出的取值。
DEFAULT_TOP_K = 40
DEFAULT_REPEAT_LAST_N = 64


class NativeLlamaError(RuntimeError):
    """[EN] Raised when the native llama.cpp layer cannot fulfil a request.
    [ZH] 原生 llama.cpp 层无法完成请求时抛出。"""


class NativeRuntimeUnavailable(NativeLlamaError):
    """
    [EN]
    Raised when the native runtime cannot be brought up far enough to load a model:
    the ggml backend registry is empty, it holds no CPU backend, or a required ABI
    constant is missing. It is raised *before* ``llama_model_load_from_file``, so
    the failure carries the registration evidence (resolved library directory,
    backend count, device names) instead of surfacing later as a NULL model.

    [ZH]
    原生运行期无法启动到可以加载模型的程度时抛出：ggml 后端注册表为空、其中没有 CPU
    后端，或必需的 ABI 常量缺失。它在 ``llama_model_load_from_file`` **之前**抛出，因此
    携带注册证据（解析到的库目录、后端个数、设备名），而不是在下游退化成 NULL 模型。
    """


# [EN] Re-exported from ``llm_backend`` so the routing layer can import the type
# [EN] without importing this module: importing ``llama_native`` loads the native
# [EN] DLLs, and the vision gate must be able to refuse before any DLL is loaded.
# [ZH] 从 ``llm_backend`` 重新导出：路由层需要导入该类型，但不应为此导入本模块 ——
# [ZH] 导入 ``llama_native`` 会加载原生 DLL，而视觉闸门必须能在任何 DLL 加载之前拒绝。
from llm_backend import NativeVisionUnsupported  # noqa: E402,F401


class NativeLlamaAborted(NativeLlamaError):
    """
    [EN]
    Raised when the caller asked to stop during prefill.

    It is deliberately a distinct type rather than a plain ``RuntimeError``: an abort is
    a normal user action, so the engine can stay silent about it, while a genuine code
    failure must still surface as a diagnostic. ``isinstance(exc, NativeLlamaError)``
    keeps the two distinguishable without changing the existing error contract.

    [ZH]
    调用方在预填充期间要求停止时抛出。

    这里刻意使用独立类型而不是普通的 ``RuntimeError``：中止是正常的用户操作，引擎可以
    对它保持静默，而真正的代码故障仍必须作为诊断信息呈现。它仍继承
    ``NativeLlamaError``，因此在不改变既有错误契约的前提下两者可 ``isinstance`` 区分。
    """


#: [EN] Keeps the log callback alive for the lifetime of the process: ctypes
#: [EN] function pointers are garbage collected like any other object, and a
#: [EN] collected callback would make llama.cpp jump into freed memory. The log
#: [EN] callback is **process-global** in llama.cpp, so the install is guarded and
#: [EN] every created callback is retained in :data:`_LOG_CALLBACK_REFS`: a second
#: [EN] engine starting concurrently must never replace (and thereby free) a
#: [EN] callback that the native side may be invoking right now.
#: [ZH] 保证日志回调在整个进程生命周期内存活：ctypes 函数指针与普通对象一样会被
#: [ZH] 垃圾回收，回调被回收会让 llama.cpp 跳进已释放的内存。日志回调在 llama.cpp 里是
#: [ZH] **进程全局**的，因此安装过程加了锁，且每个创建过的回调都保留在
#: [ZH] :data:`_LOG_CALLBACK_REFS` 中：并发启动的第二个引擎绝不能替换（从而释放）
#: [ZH] 原生侧此刻可能正在调用的回调。
_LOG_CALLBACK_REF = None
_LOG_CALLBACK_REFS = []
_LOG_CALLBACK_LOCK = threading.Lock()
_LOG_VERBOSE = False

#: [EN] One-shot guard for the ggml backend registry. The registry is process
#: [EN] wide, so registering twice is redundant; more importantly every extra
#: [EN] ``ggml_backend_load_all`` scans the library directory again.
#: [ZH] ggml 后端注册的一次性守卫。注册表是进程级的，重复注册既无必要，也会让
#: [ZH] 每次 ``ggml_backend_load_all`` 重新扫描一遍库目录。
_BACKENDS_REGISTERED = False

#: [EN] Registration evidence of the first successful call, reused by every later
#: [EN] instance: resolved directory, backend/device names and the attempts made.
#: [ZH] 首次成功注册的证据，供后续实例复用：解析到的目录、后端与设备名、尝试过的路径。
_BACKEND_INFO: Dict[str, object] = {}

#: [EN] ``chat_format`` is accepted for API compatibility but the native runtime
#: [EN] always uses the template embedded in the GGUF. The warning is emitted once
#: [EN] per process so a chatty construction loop cannot flood the status strip.
#: [ZH] ``chat_format`` 仅为 API 兼容而接受，原生运行期始终使用 GGUF 内嵌模板。警告在
#: [ZH] 进程内只发一次，避免构造循环刷屏状态栏。
_CHAT_FORMAT_WARNED = False


def _warn_chat_format_once(chat_format) -> None:
    """
    [EN] Warn that ``chat_format`` is ignored, at most once per process.
    [ZH] 告警 ``chat_format`` 被忽略，每个进程最多一次。
    """
    global _CHAT_FORMAT_WARNED
    if _CHAT_FORMAT_WARNED:
        return
    _CHAT_FORMAT_WARNED = True
    log.warning(
        "chat_format=%r 被忽略：原生运行期使用 GGUF 内嵌模板"
        "（tokenizer.chat_template）；不含模板的模型应改走旧轮子", chat_format)


def _read_model_metadata(model_path: str) -> Dict[str, object]:
    """
    [EN]
    Read the GGUF metadata for ``LLMEngine.model_info()``.

    ``model_info()`` maps every ``llm.metadata`` item whose value is a scalar, so
    the native runtime only has to expose the same dictionary under llama.cpp's
    original key names (``general.*`` / ``<arch>.*`` / ``tokenizer.*``). Nothing is
    translated and no display formatting happens here.

    [ZH]
    为 ``LLMEngine.model_info()`` 读取 GGUF 元数据。

    ``model_info()`` 会把 ``llm.metadata`` 中值为标量的每一项原样映射，因此原生运行期只需
    按 llama.cpp 的原始键名（``general.*`` / ``<arch>.*`` / ``tokenizer.*``）暴露同一份
    字典。这里不做键名翻译，也不做展示层格式化。
    """
    try:
        import gguf_meta  # noqa: PLC0415 - optional, no native dependency

        raw = gguf_meta.cached_metadata(model_path)
    except Exception:  # noqa: BLE001 - metadata is diagnostic, never fatal
        return {}
    return {str(key): value for key, value in (raw or {}).items()
            if isinstance(value, (str, int, float))}


def _backend_library_dir() -> Optional[str]:
    """
    [EN]
    Directory that actually holds the loaded ``ggml`` libraries.

    A bare ``ggml_backend_load_all()`` resolves its search directory against the
    process working directory, while an embedded runtime keeps the DLLs in
    ``backend/native/<rid>`` regardless of where the process was started from.
    The directory is therefore read back from the loaded library handle instead
    of being reconstructed from configuration, so it is always the directory the
    running binaries came from.

    The explicit directory pinned by :mod:`llama_capi` (``LLAMA_CPP_LIB_PATH``)
    takes precedence when it is a directory. Why it is needed: PyInstaller scans
    modules that import ``ctypes`` and copies every shared library it can name
    into the bundle root, so a frozen build ends up with ``ggml.dll`` both in
    ``_internal/`` (the scanner's copy, which ``find_library`` happens to win)
    and in ``_internal/native/`` (where the CPU backend plugins live). Reading the
    directory back from the handle then points at the root, where no
    ``ggml-cpu-*.dll`` exists, and registration reports ``reg_count=0`` -- a real
    frozen-build failure measured in v0.2.7. The pinned directory is the one the
    runtime was vendored into, so it is authoritative.

    [ZH]
    实际持有已加载 ``ggml`` 库的目录。

    裸调用 ``ggml_backend_load_all()`` 以进程工作目录为搜索基准，而嵌入式运行期的 DLL
    固定在 ``backend/native/<rid>``，与进程启动位置无关。因此这里从已加载的库句柄反读
    目录，而不是从配置重建，保证它与正在运行的二进制始终同源。

    当 :mod:`llama_capi` 钉住的目录（``LLAMA_CPP_LIB_PATH``）存在时，以它为准。为什么需要：
    PyInstaller 会扫描导入 ``ctypes`` 的模块，把它能叫出名字的共享库再复制一份到包根，于是
    冻结构建里 ``ggml.dll`` 同时出现在 ``_internal/``（扫描副本，``find_library`` 恰好命中它）
    和 ``_internal/native/``（CPU 后端插件所在处）。此时从句柄反读目录会指向包根，那里没有
    任何 ``ggml-cpu-*.dll``，注册结果是 ``reg_count=0`` —— 这是 v0.2.7 实测到的冻结构建失败。
    钉住的目录才是运行期 vendoring 的位置，因此以它为准。
    """
    pinned = str(os.environ.get("LLAMA_CPP_LIB_PATH") or "").strip()
    if pinned and os.path.isdir(pinned):
        return pinned
    try:
        from llama_abi import _ggml  # noqa: PLC0415 - optional at import time
    except Exception:  # noqa: BLE001 - the binding may be absent, caller decides
        return None
    for attribute in ("libggml", "libggml_base"):
        library = getattr(_ggml, attribute, None)
        path = str(getattr(library, "_name", "") or "")
        if not path:
            continue
        directory = os.path.dirname(os.path.abspath(path))
        if os.path.isdir(directory):
            return directory
    return None


def _resolve_ggml_symbol(api, symbol, argtypes):
    """
    [EN]
    Return ``(callable, source_label)`` for one ``ggml`` entry point.

    The binding module only re-exports the ``llama_*`` surface, so a loader is
    looked up on the declarations first, then on the vendored ``_ggml`` module
    (which binds it at import time), and finally on the raw ``ggml`` library
    handles -- ``libggml`` owns the backend registry, ``libggml_base`` is only a
    fallback for builds that export it there. Missing everywhere yields
    ``(None, "")``; the caller decides whether that is fatal.

    [ZH]
    返回某个 ``ggml`` 入口的 ``(可调用对象, 来源标签)``。

    绑定模块只转出 ``llama_*`` 表面，因此先后在声明模块、内置 ``_ggml`` 模块（导入时已
    绑定）与原始 ``ggml`` 库句柄上查找 —— 后端注册表在 ``libggml`` 里，``libggml_base``
    只是个别构建把它导出在彼处时的兜底。三处都没有则返回 ``(None, "")``，是否致命由
    调用方决定。
    """
    loader = getattr(api, symbol, None)
    if callable(loader):
        return loader, "llama_capi"
    try:
        from llama_abi import _ggml  # noqa: PLC0415 - optional at import time
    except Exception:  # noqa: BLE001
        return None, ""
    loader = getattr(_ggml, symbol, None)
    if callable(loader):
        return loader, "llama_abi._ggml"
    for attribute in ("libggml", "libggml_base"):
        library = getattr(_ggml, attribute, None)
        if library is None:
            continue
        try:
            loader = getattr(library, symbol)
        except AttributeError:
            continue
        loader.argtypes = list(argtypes)
        loader.restype = None
        return loader, attribute
    return None, ""


def _backend_registry_count() -> Optional[int]:
    """
    [EN] Number of registered ggml backends, or ``None`` when the counter is not
    [EN] exported. Used only as evidence for the debug log and to decide whether
    [EN] the path-scoped loader must run; loading still works without it.
    [ZH] 已注册的 ggml 后端数量；计数符号未导出时返回 ``None``。它只用于 debug 日志与
    [ZH] 判断是否需要走带路径的加载器，缺少它时加载本身仍然可用。
    """
    try:
        from llama_abi import _ggml  # noqa: PLC0415 - optional at import time
    except Exception:  # noqa: BLE001
        return None
    counter = getattr(_ggml, "ggml_backend_reg_count", None)
    if callable(counter):
        try:
            return int(counter())
        except Exception:  # noqa: BLE001 - a diagnostic never breaks startup
            return None
    library = getattr(_ggml, "libggml", None)
    if library is None:
        return None
    try:
        counter = getattr(library, "ggml_backend_reg_count")
    except AttributeError:
        return None
    counter.argtypes = []
    counter.restype = ctypes.c_size_t
    try:
        return int(counter())
    except Exception:  # noqa: BLE001
        return None


def _backend_registry_names() -> List[str]:
    """
    [EN] Names of the registered ggml backend registries (e.g. ``["CPU"]``).
    [ZH] 已注册的 ggml 后端注册表名称（例如 ``["CPU"]``）。
    """
    try:
        from llama_abi import _ggml  # noqa: PLC0415 - optional at import time
    except Exception:  # noqa: BLE001
        return []
    counter = getattr(_ggml, "ggml_backend_reg_count", None)
    getter = getattr(_ggml, "ggml_backend_reg_get", None)
    namer = getattr(_ggml, "ggml_backend_reg_name", None)
    if not (callable(counter) and callable(getter) and callable(namer)):
        return []
    out = []
    try:
        total = int(counter())
    except Exception:  # noqa: BLE001 - diagnostics never break startup
        return []
    for index in range(total):
        try:
            name = namer(getter(index))
        except Exception:  # noqa: BLE001
            continue
        if name:
            out.append(name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name))
    return out


def _backend_devices() -> List[str]:
    """
    [EN]
    Names of the devices the registered backends expose (e.g. ``["CPU"]``).

    The per-registry enumeration is used first because it needs no global device
    cursor; the global ``ggml_backend_dev_count/get`` pair is the fallback for a
    build that only exposes the device-level API.

    [ZH]
    已注册后端暴露的设备名称（例如 ``["CPU"]``）。

    优先按注册表逐个枚举，因为它不依赖全局设备游标；只有按注册表枚举不可用时，才回落到
    全局 ``ggml_backend_dev_count/get`` 那一对。
    """
    try:
        from llama_abi import _ggml  # noqa: PLC0415 - optional at import time
    except Exception:  # noqa: BLE001
        return []
    namer = getattr(_ggml, "ggml_backend_dev_name", None)
    if not callable(namer):
        return []

    def _name(device):
        try:
            value = namer(device)
        except Exception:  # noqa: BLE001
            return ""
        if not value:
            return ""
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

    out = []
    counter = getattr(_ggml, "ggml_backend_reg_count", None)
    getter = getattr(_ggml, "ggml_backend_reg_get", None)
    dev_counter = getattr(_ggml, "ggml_backend_reg_dev_count", None)
    dev_getter = getattr(_ggml, "ggml_backend_reg_dev_get", None)
    if all(callable(item) for item in (counter, getter, dev_counter, dev_getter)):
        try:
            total = int(counter())
        except Exception:  # noqa: BLE001
            total = 0
        for index in range(total):
            try:
                registry = getter(index)
                device_total = int(dev_counter(registry))
            except Exception:  # noqa: BLE001
                continue
            for slot in range(device_total):
                try:
                    name = _name(dev_getter(registry, slot))
                except Exception:  # noqa: BLE001
                    continue
                if name:
                    out.append(name)
        if out:
            return out

    dev_counter = getattr(_ggml, "ggml_backend_dev_count", None)
    dev_getter = getattr(_ggml, "ggml_backend_dev_get", None)
    if callable(dev_counter) and callable(dev_getter):
        try:
            total = int(dev_counter())
        except Exception:  # noqa: BLE001
            total = 0
        for index in range(total):
            try:
                name = _name(dev_getter(index))
            except Exception:  # noqa: BLE001
                continue
            if name:
                out.append(name)
    return out


def _flash_attn_enum_value(api, enabled: bool) -> int:
    """
    [EN]
    Value of ``LLAMA_FLASH_ATTN_TYPE_ENABLED`` / ``..._DISABLED`` for this build.

    The constant is preferred because that is the historical wheel surface; the
    vendored ABI only declares the ``llama_flash_attn_type`` enum, so its member
    is the fallback. When neither exists the caller gets a loud
    :class:`NativeRuntimeUnavailable` -- silently guessing an integer here would
    leave the UI reporting a mode the native runtime is not actually using.

    [ZH]
    当前构建的 ``LLAMA_FLASH_ATTN_TYPE_ENABLED`` / ``..._DISABLED`` 取值。

    常量优先，因为那是历史轮子的对外表面；内置 ABI 只声明 ``llama_flash_attn_type``
    枚举，因此以枚举成员兜底。两者都不存在时抛出醒目的
    :class:`NativeRuntimeUnavailable` —— 在这里静默猜一个整数，会让界面报告的运行模式与
    原生运行期实际使用的不一致。
    """
    symbol = ("LLAMA_FLASH_ATTN_TYPE_ENABLED" if enabled
              else "LLAMA_FLASH_ATTN_TYPE_DISABLED")
    value = getattr(api, symbol, None)
    if value is not None:
        return int(value)
    enum = getattr(api, "llama_flash_attn_type", None)
    value = getattr(enum, symbol, None) if enum is not None else None
    if value is not None:
        return int(value)
    raise NativeRuntimeUnavailable(
        "flash attention mode %r is missing from the native ABI: neither %s nor "
        "llama_flash_attn_type.%s is exported" % (symbol, symbol, symbol))


def _register_backends(api) -> Dict[str, object]:
    """
    [EN]
    Register the ggml backends once, explicitly from the library directory.

    This revision of ``llama.cpp`` no longer registers backends inside
    ``llama_backend_init()``, and a bare ``ggml_backend_load_all()`` resolves its
    search directory against the *working directory* -- which is whatever the
    user's shortcut / installer / service happened to be, not where the DLLs live.
    The sequence is therefore:

    1. ``llama_backend_init()`` when the build still exports it;
    2. ``ggml_backend_load_all_from_path(<dir of libggml._name>)`` -- explicit
       directory, no cwd dependency, nothing hard-coded;
    3. ``ggml_backend_load_all()`` only as a fallback when step 2 registered
       nothing;
    4. a loud :class:`NativeRuntimeUnavailable` when the registry is still empty or
       holds no CPU backend -- never let the model load return NULL downstream.

    Returns the registration evidence (directory, backend/device names) that the
    instance keeps for the future ``inference_status()``.

    [ZH]
    一次性注册 ggml 后端，且显式指定库目录。

    这个 ``llama.cpp`` 修订不再在 ``llama_backend_init()`` 内注册后端；裸调用
    ``ggml_backend_load_all()`` 又以**工作目录**为搜索基准 —— 那是用户的快捷方式 / 安装器 /
    服务碰巧所在的目录，不是 DLL 所在目录。因此顺序是：

    1. 构建仍导出 ``llama_backend_init()`` 时先调用；
    2. ``ggml_backend_load_all_from_path(<libggml._name 所在目录>)`` —— 显式目录，不依赖
       cwd，也不写死路径；
    3. 只有第 2 步一个后端都没注册上时，才回落到裸 ``ggml_backend_load_all()``；
    4. 注册表仍为空或其中没有 CPU 后端时抛醒目的 :class:`NativeRuntimeUnavailable` ——
       绝不让下游拿到 NULL 模型。

    返回注册证据（目录、后端与设备名），由实例保存供将来的 ``inference_status()`` 使用。
    """
    global _BACKENDS_REGISTERED, _BACKEND_INFO
    if _BACKENDS_REGISTERED:
        return dict(_BACKEND_INFO)

    initialize = getattr(api, "llama_backend_init", None)
    if callable(initialize):
        try:
            initialize()
        except Exception as exc:  # noqa: BLE001 - registration below is the gate
            log.debug("llama_backend_init 调用失败，继续后端注册: %s", exc)
    else:
        log.debug("llama_backend_init 未导出，跳过后端初始化")

    directory = _backend_library_dir()
    attempts = []
    if directory:
        path_loader, source = _resolve_ggml_symbol(
            api, "ggml_backend_load_all_from_path", [ctypes.c_char_p])
        if callable(path_loader):
            try:
                path_loader(os.fsencode(directory))
                attempts.append("load_all_from_path(%s) via %s" % (directory, source))
            except Exception as exc:  # noqa: BLE001 - fallback below may still work
                log.debug("ggml_backend_load_all_from_path(%s) 调用失败: %s",
                          directory, exc)
        else:
            log.debug("ggml_backend_load_all_from_path 未在绑定模块或 ggml 库句柄上导出")
    else:
        log.debug("无法从 libggml._name 反推库目录，显式路径注册不可用")

    count = _backend_registry_count()
    if not count:
        loader, source = _resolve_ggml_symbol(api, "ggml_backend_load_all", [])
        if callable(loader):
            try:
                loader()
                attempts.append("load_all(cwd fallback) via %s" % source)
            except Exception as exc:  # noqa: BLE001 - the gate below reports it
                log.debug("ggml_backend_load_all（来源 %s）调用失败: %s", source, exc)
        else:
            log.debug("ggml_backend_load_all 未在绑定模块或 ggml 库句柄上导出")
        count = _backend_registry_count()

    devices = _backend_devices()
    backends = _backend_registry_names()
    has_cpu = any("CPU" in name.upper() for name in devices + backends)
    info = {
        "dir": directory,
        "reg_count": count,
        "devices": devices,
        "backends": backends,
        "has_cpu": has_cpu,
        "attempts": attempts,
    }
    log.debug("ggml 后端注册结果: %s", info)

    if not count:
        raise NativeRuntimeUnavailable(
            "no ggml backend registered from %r (reg_count=%s, devices=%s, "
            "backends=%s, attempts=%s)"
            % (directory, count, devices, backends, attempts))
    if not has_cpu:
        raise NativeRuntimeUnavailable(
            "no CPU backend among registered ggml devices from %r "
            "(reg_count=%s, devices=%s, backends=%s)"
            % (directory, count, devices, backends))

    _BACKEND_INFO = info
    _BACKENDS_REGISTERED = True
    return dict(info)


def _install_log_filter(verbose: bool) -> None:
    """
    [EN]
    Route the native log stream through Python's logging instead of stderr.

    The backend forwards every stderr line to the interface, so llama.cpp's
    verbosity (hundreds of loader lines per model load) would flood the status
    region. Messages are kept at DEBUG when not verbose and at INFO otherwise;
    warnings and errors remain visible because they explain load failures.

    [ZH]
    把原生日志流交给 Python 日志系统，而不是直接写 stderr。

    后端会把每一行 stderr 转发到界面，llama.cpp 的啰嗦输出（每次加载模型数百行）
    会淹没状态区。非 verbose 时消息压到 DEBUG，否则压到 INFO；警告与错误保持可见，
    因为它们解释了加载失败的原因。
    """
    global _LOG_CALLBACK_REF, _LOG_VERBOSE
    _LOG_VERBOSE = bool(verbose)
    with _LOG_CALLBACK_LOCK:
        if _LOG_CALLBACK_REF is not None:
            return
        api = _ffi()

        def _callback(level, text, _user_data):  # noqa: ANN001 - ctypes signature
            message = (text or b"").decode("utf-8", "replace").rstrip()
            if not message:
                return
            # [EN] The native logger is fully silent unless verbose mode is requested:
            # [EN] load failures always surface as a NULL return / raised exception with
            # [EN] an explicit message, while the loader's progress chatter would
            # [EN] otherwise flood the interface (the backend forwards stderr to the
            # [EN] status strip). Level numbers differ between llama.cpp revisions, so
            # [EN] the gate is the verbose flag rather than a numeric threshold.
            # [ZH] 除非显式开启 verbose，原生日志完全静默：加载失败一定以 NULL 返回 /
            # [ZH] 抛出带明确文案的异常呈现，而加载器的进度噪声否则会淹没界面（后端会把
            # [ZH] stderr 转发到状态栏）。不同 llama.cpp 版本的级别数值并不一致，因此
            # [ZH] 以 verbose 开关而不是数值阈值作为闸门。
            if _LOG_VERBOSE:
                log.info("llama.cpp[%s]: %s", int(level), message)
            else:
                log.debug("llama.cpp: %s", message)

        # [EN] The callback typedef moved between llama.cpp revisions: older builds
        # [EN] export ``llama_log_callback``, newer ones expose the same shape as
        # [EN] ``ggml_log_callback``. Pick whichever the loaded build provides instead
        # [EN] of pinning one name.
        # [ZH] 回调类型在 llama.cpp 不同修订间搬过家：旧构建导出 ``llama_log_callback``，
        # [ZH] 新构建以 ``ggml_log_callback`` 暴露同一形状。按已加载构建实际提供的名字
        # [ZH] 取，不把其中一个写死。
        callback_type = (getattr(api, "llama_log_callback", None)
                         or getattr(api, "ggml_log_callback", None))
        if callback_type is None:
            return
        callback = callback_type(_callback)
        # [EN] Retain every callback ever created so a concurrent second engine can
        # [EN] never free one that the native side is invoking.
        # [ZH] 保留所有创建过的回调，使并发的第二个引擎绝不会释放原生侧正在调用的那个。
        _LOG_CALLBACK_REFS.append(callback)
        _LOG_CALLBACK_REF = callback
        api.llama_log_set(_LOG_CALLBACK_REF, None)


def _ffi():
    """
    [EN]
    Import the C ABI declarations lazily. Keeping the import inside the function
    means a missing native library only fails model loading, not backend startup,
    so the "no model ready" state still works on a machine without the DLLs.

    [ZH]
    延迟导入 C ABI 声明。把导入放在函数内部，使缺少动态库时只有模型加载失败，
    后端启动不受影响 —— 即使机器上没有动态库，"无模型就绪"状态仍然可用。
    """
    import llama_capi  # noqa: PLC0415 - deliberate: keeps import failure local

    return llama_capi


class NativeLlama:
    """
    [EN]
    Minimal owner of a llama.cpp model, context and sampler chain.

    [ZH]
    llama.cpp 模型、上下文与采样链的最小持有者。
    """

    def __init__(
        self,
        model_path: str,
        n_ctx: int = 2048,
        n_threads: Optional[int] = None,
        n_threads_batch: Optional[int] = None,
        n_batch: int = 512,
        n_ubatch: int = 512,
        prefill_chunk_tokens: int = 0,
        prefill_yield_ms: int = 8,
        n_gpu_layers: int = 0,
        use_mlock: bool = False,
        use_mmap: bool = True,
        flash_attn: bool = False,
        type_k: int = 8,
        type_v: int = 8,
        rope_type: str = "none",
        rope_scale: float = 1.0,
        rope_freq_scale: float = 0.0,
        rope_orig_ctx: int = 0,
        chat_format: Optional[str] = None,
        logits_all: bool = False,
        seed: Optional[int] = None,
        seed_: Optional[int] = None,
        chat_handler=None,
        verbose: bool = False,
        **_ignored,
    ) -> None:
        # [EN] Compatibility surface first, before any native work: a vision model or a
        # [EN] logits consumer must fail loudly instead of silently losing its feature.
        # [ZH] 先处理兼容面，再做任何原生工作：视觉模型或依赖 logits 的调用方必须响亮失败，
        # [ZH] 而不是静默丢掉它的功能。
        if chat_handler is not None:
            raise NativeVisionUnsupported(
                "vision requires the llama-cpp-python runtime until T3-2: "
                "a non-None chat_handler was passed to NativeLlama")
        if logits_all:
            raise ValueError(
                "logits_all is not supported by the native runtime: "
                "the sampling path does not expose per-token logits")
        if chat_format:
            _warn_chat_format_once(chat_format)
        api = _ffi()
        _install_log_filter(verbose)
        # [EN] Backend registration must happen before the first model parameter/load
        # [EN] call: this revision no longer does it inside ``llama_backend_init()``.
        # [EN] A failed registration raises here, while the evidence is still in hand.
        # [ZH] 后端注册必须在首次读取模型参数 / 加载之前完成：该修订不再在
        # [ZH] ``llama_backend_init()`` 内做这件事。注册失败就在这里抛出，此时证据仍在手上。
        self._backend_info = _register_backends(api)
        #: [EN] Registration evidence for the future ``inference_status()``: library
        #: [EN] directory, backend count and the device names that actually loaded.
        #: [ZH] 供将来 ``inference_status()`` 使用的注册证据：库目录、后端个数与实际加载的
        #: [ZH] 设备名。
        self.backend_dir = self._backend_info.get("dir")
        self.backend_count = self._backend_info.get("reg_count")
        self.backend_devices = list(self._backend_info.get("devices") or [])
        self.backend_backends = list(self._backend_info.get("backends") or [])
        self._api = api
        self._model = None
        self._ctx = None
        self._vocab = None
        self.model_path = str(model_path)
        #: [EN] GGUF metadata under llama.cpp's original key names, read without
        #: [EN] loading the model. ``LLMEngine.model_info()`` maps every scalar entry,
        #: [EN] so a non-empty dict here is what keeps that call from returning ``{}``.
        #: [ZH] 按 llama.cpp 原始键名保存的 GGUF 元数据，无需加载模型即可读取。
        #: [ZH] ``LLMEngine.model_info()`` 会映射每个标量条目，因此这里非空才能让该调用
        #: [ZH] 不再返回 ``{}``。
        self.metadata = _read_model_metadata(self.model_path)
        # [EN] ``seed_`` is the historical typo of ``seed``; both are accepted and
        # [EN] ``seed_`` wins when they disagree (recorded once at debug level).
        # [ZH] ``seed_`` 是 ``seed`` 的历史笔误；两者都接受，取值冲突时 ``seed_`` 优先
        # [ZH] （debug 记一次）。
        if seed_ is not None and seed is not None:
            log.debug("seed 与 seed_ 同时给出，采用 seed_=%s（seed=%s 忽略）", seed_, seed)
        requested_seed = seed_ if seed_ is not None else seed
        default_seed = int(getattr(api, "LLAMA_DEFAULT_SEED", 0xFFFFFFFF))
        self.sampler_seed = (default_seed if requested_seed is None
                             else int(requested_seed) & 0xFFFFFFFF)
        self.n_batch = max(1, int(n_batch))
        self.n_ubatch = max(1, int(n_ubatch))
        self.n_threads = int(n_threads or max(1, (os.cpu_count() or 4) - 1))
        # [EN] Prefill gets its own, usually smaller, thread pool. ``<= 0`` (including the
        # [EN] ``None`` default) falls back to the decode thread count so an old caller
        # [EN] that never heard of this parameter keeps the historical behaviour.
        # [ZH] 预填充拥有自己（通常更小）的线程池。``<= 0``（含 ``None`` 默认）回落到
        # [ZH] decode 线程数，使不认识该参数的旧调用方保持历史行为。
        self.n_threads_batch = int(n_threads_batch or 0)
        if self.n_threads_batch <= 0:
            self.n_threads_batch = self.n_threads
        # [EN] Prefill chunk size: 0 follows ``n_batch``; otherwise clamped into
        # [EN] ``[32, n_batch]`` because a single ``llama_decode`` cannot exceed the batch.
        # [ZH] 预填充分块大小：0 跟随 ``n_batch``；否则钳制在 ``[32, n_batch]``，
        # [ZH] 因为单次 ``llama_decode`` 不能超过批大小。
        self.prefill_chunk_tokens = int(prefill_chunk_tokens or 0)
        # [EN] Inter-chunk yield. 0 means "do not sleep", which is what a benchmark needs
        # [EN] in order to measure the real prefill speed.
        # [ZH] 块间让核。0 表示"不睡"，基准测试需要它才能量到真实的预填充速度。
        self.prefill_yield_ms = max(0, min(50, int(prefill_yield_ms or 0)))
        #: [EN] Result of the most recent prefill, read by the engine and the tuner but
        #: [EN] never part of the wire protocol.
        #: [ZH] 最近一次预填充的测量结果，供引擎与调优器读取，不参与协议。
        self.last_prefill = {"tokens": 0, "seconds": 0.0, "tps": 0.0}
        self.n_gpu_layers = int(n_gpu_layers or 0)
        self.use_mlock = bool(use_mlock)
        self.use_mmap = bool(use_mmap)
        self.flash_attn = bool(flash_attn)
        self.type_k = int(type_k)
        self.type_v = int(type_v)
        # [EN] Requested vs effective configuration. The UI/ticket must be able to see
        # [EN] both, because the runtime may have to change one of them to make the
        # [EN] load succeed (quantized KV needs flash attention). The names follow the
        # [EN] T3-1a′ spec so the future ``inference_status`` reads them off the
        # [EN] instance directly.
        # [ZH] 请求值 vs 生效值。界面/工单必须能同时看到两份，因为运行期可能不得不改动其中
        # [ZH] 一项才能让加载成功（量化 KV 需要 flash attention）。命名沿用 T3-1a′ 规格，
        # [ZH] 使将来的 ``inference_status`` 能直接从实例读取。
        self.requested_type_k = self.type_k
        self.requested_type_v = self.type_v
        self.requested_flash_attn = self.flash_attn
        self.effective_flash_attn = self.flash_attn
        self.flash_attn_reason = ""
        # [EN] RoPE scaling: stored so the status line can report the effective mode.
        # [EN] The numeric enum values come from ``llama_rope_scaling_type``
        # [EN] (none = 0, linear = 1, yarn = 2); an unknown name keeps ``none``.
        # [ZH] RoPE 缩放：保存下来供状态行报告生效模式。数值枚举取自
        # [ZH] ``llama_rope_scaling_type``（none = 0，linear = 1，yarn = 2）；
        # [ZH] 未知名称按 ``none`` 处理。
        self.rope_type = str(rope_type or "none").strip().lower()
        if self.rope_type not in ("none", "linear", "yarn"):
            self.rope_type = "none"
        self.rope_scale = float(rope_scale or 1.0)
        if self.rope_scale < 1.0:
            self.rope_scale = 1.0
        # [EN] ``rope_freq_scale`` is the reciprocal of the extension factor. A value of
        # [EN] 0 means "keep whatever the model file declares", which is what ``none``
        # [EN] needs and also the safest default when the file already carries scaling.
        # [ZH] ``rope_freq_scale`` 是扩展倍数的倒数。取 0 表示"沿用模型文件自带取值"，
        # [ZH] 这既是 ``none`` 所需，也是文件自带缩放时最安全的默认。
        self.rope_freq_scale = float(rope_freq_scale or 0.0)
        if self.rope_type != "none" and self.rope_freq_scale <= 0.0:
            self.rope_freq_scale = 1.0 / self.rope_scale if self.rope_scale > 1.0 else 0.0
        self.rope_orig_ctx = max(0, int(rope_orig_ctx or 0))
        self._loaded = False
        self._t0 = time.perf_counter()

        params = api.llama_model_default_params()
        params.n_gpu_layers = self.n_gpu_layers
        # [EN] Windows/macOS wheels expose separate ``use_mmap`` / ``use_mlock`` flags;
        # [EN] the Linux wheel replaced them with a single ``load_mode`` enum. Mapping the
        # [EN] configuration onto whichever shape the loaded ABI declares keeps the
        # [EN] documented behaviour identical on all three platforms.
        # [ZH] Windows/macOS 轮子分别暴露 ``use_mmap`` / ``use_mlock`` 开关；Linux 轮子改为
        # [ZH] 单一的 ``load_mode`` 枚举。把配置映射到当前载入的 ABI 所声明的形状，使三个
        # [ZH] 平台的行为与文档一致。
        if hasattr(params, "use_mmap"):
            params.use_mmap = self.use_mmap
            params.use_mlock = self.use_mlock
        else:
            if self.use_mlock and self.use_mmap:
                params.load_mode = 3   # LLAMA_LOAD_MODE_MMAP_MLOCK
            elif self.use_mlock:
                params.load_mode = 2   # LLAMA_LOAD_MODE_MLOCK
            elif self.use_mmap:
                params.load_mode = 1   # LLAMA_LOAD_MODE_MMAP
            else:
                params.load_mode = 0   # LLAMA_LOAD_MODE_NONE
        # [EN] No split / tensor override: a single model file on one device.
        # [ZH] 不做分片或张量覆盖：单个模型文件、单个设备。
        model = api.llama_model_load_from_file(self.model_path.encode("utf-8"), params)
        if not model:
            raise NativeLlamaError(
                "llama_model_load_from_file returned NULL: "
                f"{self.model_path}")
        self._model = model
        self._vocab = api.llama_model_get_vocab(model)

        ctx_params = api.llama_context_default_params()
        ctx_params.n_ctx = max(1, int(n_ctx))
        ctx_params.n_batch = self.n_batch
        ctx_params.n_ubatch = self.n_ubatch
        ctx_params.n_threads = self.n_threads
        # [EN] Created with the prefill pool already in place; the switch helpers only
        # [EN] move between the two pools at request boundaries.
        # [ZH] 创建时就带上预填充线程池；切换辅助函数只负责在请求边界上往返两组线程。
        ctx_params.n_threads_batch = self.n_threads_batch
        # [EN] The flash-attention mode constant is resolved by
        # [EN] :func:`_flash_attn_enum_value`: module-level constant first, the
        # [EN] ``llama_flash_attn_type`` enum member second, and a loud
        # [EN] :class:`NativeRuntimeUnavailable` when neither exists.
        # [ZH] flash-attention 模式常量由 :func:`_flash_attn_enum_value` 解析：先模块级
        # [ZH] 常量，再 ``llama_flash_attn_type`` 枚举成员，两者都不存在则抛醒目的
        # [ZH] :class:`NativeRuntimeUnavailable`。
        # [EN] A quantized K/V cache only exists on the flash-attention path: the
        # [EN] loader rejects it otherwise ("quantized V cache requires flash_attn to
        # [EN] be enabled") and the context comes back NULL. Q8_0 is the Reader
        # [EN] default, so the quantized cache wins and flash attention is enabled to
        # [EN] carry it -- the alternative would silently double the KV-cache memory.
        # [ZH] 量化 K/V Cache 只存在于 flash-attention 路径上：否则加载器会拒绝
        # [ZH] （"quantized V cache requires flash_attn to be enabled"）并返回 NULL 上下文。
        # [ZH] Q8_0 是 Reader 默认值，因此以量化缓存为准，自动启用 flash attention ——
        # [ZH] 另一条路只会让 KV Cache 内存静默翻倍。
        if not self.flash_attn and (self.type_k > 1 or self.type_v > 1):
            self.flash_attn = True
            self.effective_flash_attn = True
            self.flash_attn_reason = "quantized V cache requires flash attention"
            log.debug("KV cache 量化（type_k=%d, type_v=%d）要求 flash attention，已自动启用",
                      self.type_k, self.type_v)
        ctx_params.flash_attn_type = _flash_attn_enum_value(api, self.flash_attn)
        # [EN] KV-cache width comes from the configuration file. Q8_0 is the Reader
        # [EN] default; F16 remains reachable only by editing the file.
        # [ZH] KV Cache 宽度来自配置文件。Q8_0 是 Reader 默认值；只有修改该文件
        # [ZH] 才能退回 F16。
        ctx_params.type_k = self.type_k
        ctx_params.type_v = self.type_v
        # [EN] RoPE scaling (v0.2.6-reader context extension). Every ABI variant in
        # [EN] ``llama_capi`` declares these fields, but they are applied defensively:
        # [EN] if a future library drops one, the runtime keeps its own default instead
        # [EN] of failing the model load. Only the three fields we actually control are
        # [EN] written; YaRN's mixing factors keep the library defaults so an untouched
        # [EN] configuration behaves exactly as before this feature existed.
        # [ZH] RoPE 缩放（v0.2.6-reader 上下文扩展）。``llama_capi`` 的三个 ABI 变体都
        # [ZH] 声明了这些字段，但这里仍做防御处理：若将来的库去掉某一项，运行时沿用自身
        # [ZH] 默认值，而不是让模型加载失败。只写我们真正控制的三个字段；YaRN 的混合
        # [ZH] 系数沿用库默认值，因此未启用该功能时行为与本功能存在之前完全一致。
        rope_mode = {"none": 0, "linear": 1, "yarn": 2}.get(self.rope_type, 0)
        if hasattr(ctx_params, "rope_scaling_type"):
            ctx_params.rope_scaling_type = rope_mode
        if self.rope_type != "none":
            if hasattr(ctx_params, "rope_freq_scale") and self.rope_freq_scale > 0.0:
                ctx_params.rope_freq_scale = self.rope_freq_scale
            if self.rope_orig_ctx > 0 and hasattr(ctx_params, "yarn_orig_ctx"):
                ctx_params.yarn_orig_ctx = int(self.rope_orig_ctx)
            if self.rope_scale > 1.0 and hasattr(ctx_params, "rope_freq_scale"):
                # [EN] The factor is the authoritative input: rewrite the reciprocal so a
                # [EN] hand-edited ``scale`` cannot disagree with ``freq_scale``.
                # [ZH] 倍数是权威输入：重写倒数，避免手改的 ``scale`` 与
                # [ZH] ``freq_scale`` 互相矛盾。
                ctx_params.rope_freq_scale = 1.0 / self.rope_scale
        ctx = api.llama_init_from_model(model, ctx_params)
        if not ctx:
            api.llama_model_free(model)
            self._model = None
            raise NativeLlamaError(
                f"llama_init_from_model returned NULL (n_ctx={n_ctx}); "
                "the requested context window may not fit in memory")
        self._ctx = ctx
        self._n_ctx = int(api.llama_n_ctx(ctx) or n_ctx)
        self._n_ctx_train = int(api.llama_model_n_ctx_train(model) or 0)
        self._n_vocab = int(api.llama_vocab_n_tokens(self._vocab) or 0)
        self._loaded = True
        #: [EN] Tokens currently present in the native KV cache, in order. The prefix is
        #: compared against the next prompt so a repeated or extended conversation only
        #: evaluates the new suffix instead of the whole history.
        #: [ZH] 当前位于原生 KV Cache 中的 token（按顺序）。下一个提示词会与之比较前缀，
        #: 使重复或多轮对话只对新增后缀求值，而不是重算全部历史。
        self._cached_tokens: List[int] = []
        load_ms = (time.perf_counter() - self._t0) * 1000.0
        if verbose:
            log.info(
                "native llama.cpp loaded in %.0f ms (n_ctx=%d, n_batch=%d, "
                "n_ubatch=%d, threads=%d, type_k=%d, type_v=%d, flash_attn=%s, "
                "mlock=%s, mmap=%s, gpu_layers=%d)",
                load_ms, self._n_ctx, self.n_batch, self.n_ubatch, self.n_threads,
                self.type_k, self.type_v, self.flash_attn, self.use_mlock,
                self.use_mmap, self.n_gpu_layers)
        self.load_ms = load_ms

    # [EN] ---------- introspection ----------
    # [ZH] ---------- 内省 ----------

    def n_ctx(self) -> int:
        return self._n_ctx

    def n_ctx_train(self) -> int:
        return self._n_ctx_train

    def n_vocab(self) -> int:
        return self._n_vocab

    def n_batch_size(self) -> int:
        return self.n_batch

    # [EN] ---------- tokenizer ----------
    # [ZH] ---------- 分词器 ----------

    def tokenize(self, text, add_bos: bool = False, special: bool = True) -> List[int]:
        """
        [EN]
        Tokenize text. Accepts ``bytes`` (the historical engine call shape) or
        ``str``. ``add_bos`` mirrors the old ``add_bos`` argument; chat prompts use
        the template's own special tokens, so ``add_special`` stays configurable.

        [ZH]
        对文本分词。接受 ``bytes``（引擎历史调用形态）或 ``str``。``add_bos`` 对应
        旧参数；聊天提示词使用模板自带的特殊标记，因此 ``add_special`` 保持可配置。
        """
        api = self._api
        if isinstance(text, str):
            data = text.encode("utf-8")
        else:
            data = bytes(text)
        if not data:
            return []
        needed = api.llama_tokenize(
            self._vocab, data, len(data), None, 0, bool(add_bos), bool(special))
        if needed == 0:
            return []
        if needed < 0:
            needed = -needed
        buf = (api.llama_token * int(needed))()
        written = api.llama_tokenize(
            self._vocab, data, len(data), buf, int(needed), bool(add_bos), bool(special))
        if written < 0:
            raise NativeLlamaError(f"tokenize failed ({written})")
        return [int(buf[i]) for i in range(int(written))]

    def detokenize(self, tokens: List[int]) -> str:
        return "".join(self._piece(int(token)) for token in tokens)

    def count_tokens(self, text) -> int:
        """
        [EN]
        Number of tokens ``text`` occupies. The engine counts prompt budget through
        its own ``count_tokens``; the native runtime mirrors that call shape so the
        measurement always comes from the same tokenizer that decodes the reply.

        [ZH]
        ``text`` 占用的 token 数量。引擎自己也有 ``count_tokens`` 用于估算提示词预算；
        原生运行期提供同形调用，使度量与实际解码回复的分词器始终同源。
        """
        return len(self.tokenize(text))

    def _piece(self, token: int) -> str:
        api = self._api
        size = 64
        while True:
            buf = ctypes.create_string_buffer(size)
            written = api.llama_token_to_piece(
                self._vocab, token, buf, size, 0, False)
            if written >= 0:
                return buf.raw[:written].decode("utf-8", "ignore")
            size = max(64, -written)

    # [EN] ---------- chat template ----------
    # [ZH] ---------- 聊天模板 ----------

    def chat_template(self) -> Optional[bytes]:
        api = self._api
        try:
            return api.llama_model_chat_template(self._model, None)
        except Exception:  # noqa: BLE001 - template lookup is optional
            return None

    def apply_chat_template(self, messages: List[dict], add_assistant: bool = True) -> str:
        """
        [EN]
        Render the message list with the template embedded in the GGUF metadata.
        A Qwen/ChatML style fallback is used only when the model carries no
        template; this keeps third-party models usable without a silent crash.

        [ZH]
        使用 GGUF 元数据内嵌的模板渲染消息列表。仅当模型不含模板时才退回 Qwen/ChatML
        风格，使第三方模型可用而不是静默崩溃。
        """
        api = self._api
        pairs = []
        for message in messages or []:
            role = str(message.get("role", "user") or "user")
            content = str(message.get("content", "") or "")
            pairs.append((role.encode("utf-8"), content.encode("utf-8")))
        n_msg = len(pairs)
        chat = (api.llama_chat_message * max(1, n_msg))()
        for index, (role, content) in enumerate(pairs):
            chat[index].role = role
            chat[index].content = content
        template = self.chat_template()
        if template:
            size = 2048
            while True:
                buf = ctypes.create_string_buffer(size)
                needed = api.llama_chat_apply_template(
                    template, chat, n_msg, bool(add_assistant), buf, size)
                if needed <= 0:
                    break
                if needed <= size:
                    return buf.raw[:needed].decode("utf-8", "replace")
                size = needed + 256
        # [EN] Fallback template (ChatML). Used only when the model has no template.
        # [ZH] 兜底模板（ChatML），仅在模型不含模板时使用。
        text = "".join(
            f"<|im_start|>{role}\n{content}<|im_end|>\n" for role, content in pairs)
        if add_assistant:
            text += "<|im_start|>assistant\n"
        return text

    # [EN] ---------- generation ----------
    # [ZH] ---------- 生成 ----------

    def _clear_memory(self) -> None:
        api = self._api
        memory = api.llama_get_memory(self._ctx)
        if memory:
            api.llama_memory_clear(memory, True)
        self._cached_tokens = []

    def _trim_cache(self, keep: int) -> None:
        """
        [EN]
        Drop everything after ``keep`` tokens from the KV cache. When ``keep`` is zero
        the whole cache is cleared (cheaper than a partial removal). This is what makes
        prefix reuse safe: the native memory and :attr:`_cached_tokens` always describe
        the same sequence.

        [ZH]
        从 KV Cache 中丢弃 ``keep`` 个 token 之后的内容。``keep`` 为零时整体清空（比部分
        删除更省）。这正是前缀复用安全的前提：原生内存与 :attr:`_cached_tokens` 始终描述
        同一条序列。
        """
        if keep >= len(self._cached_tokens):
            return
        api = self._api
        memory = api.llama_get_memory(self._ctx)
        if not memory:
            self._cached_tokens = []
            return
        if keep <= 0:
            api.llama_memory_clear(memory, True)
        else:
            api.llama_memory_seq_rm(memory, 0, keep, -1)
        del self._cached_tokens[keep:]

    @staticmethod
    def _common_prefix(left: List[int], right: List[int]) -> int:
        limit = min(len(left), len(right))
        index = 0
        while index < limit and left[index] == right[index]:
            index += 1
        return index

    def _prefill_chunk_size(self) -> int:
        """
        [EN]
        Tokens fed to one ``llama_decode`` call during prefill: ``prefill_chunk_tokens``
        when it is a positive value inside ``[32, n_batch]``, otherwise ``n_batch``.

        [ZH]
        预填充时单次 ``llama_decode`` 喂入的 token 数：``prefill_chunk_tokens`` 落在
        ``[32, n_batch]`` 内时用它，否则用 ``n_batch``。
        """
        limit = max(1, int(self.n_batch))
        chunk = int(self.prefill_chunk_tokens or 0)
        if chunk <= 0:
            return limit
        return int(min(limit, max(32, chunk)))

    def _record_prefill(self, tokens: int, seconds: float) -> None:
        """
        [EN] Publish the latest prefill measurement on the instance.
        [ZH] 把最近一次预填充的测量结果挂到实例上。
        """
        seconds = max(0.0, float(seconds))
        tps = (float(tokens) / seconds) if seconds > 0.0 else 0.0
        self.last_prefill = {
            "tokens": int(tokens),
            "seconds": round(seconds, 6),
            "tps": round(tps, 3),
        }

    def _yield_cores(self) -> None:
        """
        [EN] Hand the CPU back to the system between chunks, honouring
        ``prefill_yield_ms`` (0 sleeps not at all, which keeps benchmarks honest).
        [ZH] 在块与块之间把 CPU 交还系统，遵守 ``prefill_yield_ms``（0 表示完全不睡，
        以保持基准测试的真实性）。
        """
        delay_ms = int(self.prefill_yield_ms or 0)
        if delay_ms <= 0:
            return
        time.sleep(delay_ms / 1000.0)

    def _apply_n_threads(self, count: int) -> None:
        """
        [EN]
        Ask the context to run with ``count`` threads. Some ABI variants do not export
        ``llama_set_n_threads`` at all; the switch is then skipped instead of crashing
        the whole inference, and a failed call is only logged at debug level for the
        same reason -- the thread split is a responsiveness optimisation, not a
        correctness requirement.

        [ZH]
        要求上下文以 ``count`` 个线程运行。部分 ABI 变体根本没有导出
        ``llama_set_n_threads``；此时跳过切换，而不是把整个推理打崩。调用失败同样只在
        debug 级别记录，原因相同 —— 线程拆分是响应性优化，不是正确性前提。
        """
        setter = getattr(self._api, "llama_set_n_threads", None)
        if not callable(setter):
            log.debug("llama_set_n_threads 不可用，跳过线程切换")
            return
        try:
            value = max(1, int(count))
            setter(self._ctx, value, value)
        except Exception as exc:  # noqa: BLE001 - never break a reading session
            log.debug("llama_set_n_threads 调用失败，沿用当前线程数: %s", exc)

    def _run_prefill(self, tokens: List[int], on_prefill_progress=None,
                     should_abort=None) -> None:
        """
        [EN]
        Run one prefill with the prefill thread pool, then hand the cores back to the
        decode pool. The switch back lives in ``finally`` so that a completed prefill,
        an abort and a genuine ``llama_decode`` failure all leave the context on the
        decode thread count.

        [ZH]
        用预填充线程池跑一次预填充，然后把核心交回 decode 线程池。切回动作放在
        ``finally`` 里，因此正常完成、中止与真正的 ``llama_decode`` 失败三条路径都会让
        上下文回到 decode 线程数。
        """
        if not tokens:
            # [EN] A fully cached prompt has no prefill: no callback, no thread switch.
            # [ZH] 整段命中缓存时没有预填充：不回调、不切线程。
            return
        self._apply_n_threads(self.n_threads_batch)
        try:
            self._prefill(tokens, on_prefill_progress=on_prefill_progress,
                          should_abort=should_abort)
        finally:
            self._apply_n_threads(self.n_threads)

    def _prefill(self, tokens: List[int], on_prefill_progress=None,
                 should_abort=None) -> None:
        """
        [EN]
        Feed ``tokens`` into the context in ``_prefill_chunk_size()`` chunks.

        Three exits were added for the Reader prefill work (v0.2.7 step 2):

        * ``should_abort`` is consulted once before every chunk; a truthy answer raises
          :class:`NativeLlamaAborted`, which the engine can tell apart from a real fault;
        * ``on_prefill_progress(done, total, elapsed_s, tps)`` fires after every chunk so
          the caller can report progress; a callback that throws is logged at debug level
          and never interrupts the inference;
        * the loop yields the cores between chunks according to ``prefill_yield_ms``.

        Bookkeeping is unchanged: a chunk is added to ``_cached_tokens`` only after its
        ``llama_decode`` succeeded, so the native memory and the Python view agree even
        when a later chunk fails or aborts.

        [ZH]
        按 ``_prefill_chunk_size()`` 分块把 ``tokens`` 喂入上下文。

        v0.2.7 步 2 为 Reader 的预填充新增了三个出口：

        * 每块开始前查询一次 ``should_abort``；命中即抛 :class:`NativeLlamaAborted`，
          引擎可将其与真实故障区分开；
        * 每块结束后调用 ``on_prefill_progress(done, total, elapsed_s, tps)`` 上报进度；
          回调抛异常只记 debug 日志，绝不打断推理；
        * 块间按 ``prefill_yield_ms`` 让出核心。

        记账语义保持不变：只有 ``llama_decode`` 成功的分块才追加进 ``_cached_tokens``，
        因此即使后续分块失败或中止，原生内存与 Python 侧视图仍保持一致。
        """
        api = self._api
        total = len(tokens)
        if not total:
            self._record_prefill(0, 0.0)
            return
        chunk_size = self._prefill_chunk_size()
        started = time.perf_counter()
        position = 0
        try:
            while position < total:
                if should_abort is not None and should_abort():
                    raise NativeLlamaAborted(
                        f"prefill aborted at {position}/{total} tokens")
                chunk = tokens[position:position + chunk_size]
                array = (api.llama_token * len(chunk))(*chunk)
                batch = api.llama_batch_get_one(array, len(chunk))
                rc = api.llama_decode(self._ctx, batch)
                if rc != 0:
                    raise NativeLlamaError(f"llama_decode failed during prefill (rc={rc})")
                position += len(chunk)
                # [EN] Keep the cache bookkeeping in step with the native memory even when
                # [EN] a later chunk fails: what has been decoded is decoded.
                # [ZH] 即使后续分块失败，也让缓存记录与原生内存保持一致：已解码的部分就是
                # [ZH] 已解码。
                self._cached_tokens.extend(chunk)
                elapsed = time.perf_counter() - started
                self._record_prefill(position, elapsed)
                if on_prefill_progress is not None:
                    try:
                        on_prefill_progress(position, total, elapsed,
                                            position / elapsed if elapsed > 0.0 else 0.0)
                    except Exception as exc:  # noqa: BLE001 - reporting must not fail
                        log.debug("prefill 进度回调异常，已忽略: %s", exc)
                self._yield_cores()
        finally:
            # [EN] Also covers the abort path: the caller still gets the partial
            # [EN] measurement, which is what the engine reports as "stopped early".
            # [ZH] 中止路径同样覆盖：调用方仍拿到部分测量结果，引擎据此上报"提前停止"。
            self._record_prefill(position, time.perf_counter() - started)

    def _make_sampler(self, temperature: float, top_p: Optional[float],
                      top_k: Optional[int], repeat_penalty: Optional[float]):
        api = self._api
        chain = api.llama_sampler_chain_init(api.llama_sampler_chain_default_params())
        penalty = float(repeat_penalty or 1.0)
        if math.isfinite(penalty) and penalty != 1.0:
            # [EN] The Linux llama.cpp revision prepends ``n_vocab`` to this sampler.
            # [ZH] Linux 所用的 llama.cpp 修订为该采样器在前面增加了 ``n_vocab``。
            if sys.platform.startswith("linux"):
                sampler = api.llama_sampler_init_penalties(
                    self._n_vocab, DEFAULT_REPEAT_LAST_N, penalty, 0.0, 0.0)
            else:
                sampler = api.llama_sampler_init_penalties(
                    DEFAULT_REPEAT_LAST_N, penalty, 0.0, 0.0)
            api.llama_sampler_chain_add(chain, sampler)
        k = int(top_k if top_k is not None else DEFAULT_TOP_K)
        if k > 0:
            api.llama_sampler_chain_add(chain, api.llama_sampler_init_top_k(k))
        if top_p is not None and 0.0 < float(top_p) < 1.0:
            api.llama_sampler_chain_add(
                chain, api.llama_sampler_init_top_p(float(top_p), 1))
        if temperature is None or float(temperature) <= 0.0:
            api.llama_sampler_chain_add(chain, api.llama_sampler_init_greedy())
        else:
            api.llama_sampler_chain_add(
                chain, api.llama_sampler_init_temp(float(temperature)))
            api.llama_sampler_chain_add(
                chain, api.llama_sampler_init_dist(self.sampler_seed))
        return chain

    def _decode_one(self, token: int) -> None:
        api = self._api
        array = (api.llama_token * 1)(token)
        batch = api.llama_batch_get_one(array, 1)
        rc = api.llama_decode(self._ctx, batch)
        if rc != 0:
            raise NativeLlamaError(f"llama_decode failed during decode (rc={rc})")

    def _stream_tokens(
        self,
        messages: List[dict],
        max_tokens: int,
        temperature: float,
        top_p: Optional[float],
        top_k: Optional[int],
        repeat_penalty: Optional[float],
        on_prefill_progress=None,
        should_abort=None,
    ) -> Iterator[str]:
        prompt = self.apply_chat_template(messages, add_assistant=True)
        tokens = self.tokenize(prompt, add_bos=True, special=True)
        yield from self._generate(tokens, max_tokens, temperature, top_p, top_k,
                                   repeat_penalty, on_prefill_progress, should_abort)

    def _generate(
        self,
        tokens: List[int],
        max_tokens: int,
        temperature: float,
        top_p: Optional[float],
        top_k: Optional[int],
        repeat_penalty: Optional[float],
        on_prefill_progress=None,
        should_abort=None,
    ) -> Iterator[str]:
        api = self._api
        if len(tokens) >= self._n_ctx:
            raise NativeLlamaError(
                f"prompt is too long for the context window: {len(tokens)} tokens "
                f">= n_ctx {self._n_ctx}")
        # [EN] Prefix reuse: a repeated prompt, or a conversation whose history only
        # [EN] grew, evaluates just the new suffix. Without this the whole history would
        # [EN] be re-prefilled on every turn, which on CPU dominates the response time.
        # [ZH] 前缀复用：重复的提示词，或历史只是追加的多轮对话，只对新增后缀求值。
        # [ZH] 若不这样做，每一轮都会重算全部历史；在 CPU 上这会是响应时间的主要部分。
        common = self._common_prefix(tokens, self._cached_tokens)
        self._trim_cache(common)
        suffix = tokens[common:]
        if suffix:
            # [EN] Only a real prefill switches thread pools: a fully cached prompt has
            # [EN] no prefill phase, so there is nothing to report and nothing to switch.
            # [ZH] 只有真正发生预填充才切换线程池：整段命中缓存时没有预填充阶段，
            # [ZH] 既没有可上报的进度，也没有可切换的线程。
            self._run_prefill(suffix, on_prefill_progress, should_abort)
        elif tokens and common >= 1:
            # [EN] Full cache hit: no suffix is left, so without this branch not a single
            # [EN] ``llama_decode`` would run for this request and the logits buffer would
            # [EN] still describe the last token of the *previous* request --
            # [EN] ``llama_sampler_sample(ctx, -1)`` reads the logits of the most recent
            # [EN] decode, not the KV cache. A repeated prompt therefore sampled EOG
            # [EN] immediately (empty answer) or continued the previous sentence.
            # [EN] Re-evaluating only the last prompt token restores the logits for this
            # [EN] prompt at the cost of exactly one token; the rest of the prefix stays
            # [EN] reused, so this is not a full recompute.
            # [ZH] 整段命中缓存：没有后缀可预填充，因此本轮一次 ``llama_decode`` 都不会发生，
            # [ZH] logits 缓冲区仍停留在「上一次请求的最后一个 token」上 ——
            # [ZH] ``llama_sampler_sample(ctx, -1)`` 读的是最近一次 decode 的 logits，
            # [ZH] 而不是 KV Cache。于是重复的提示词要么首个采样即 EOG（空回答），
            # [ZH] 要么续写上一句。只重算最后一个提示词 token 即可把 logits 恢复成该提示词
            # [ZH] 的末位状态，代价恰好是 1 个 token；其余前缀照旧复用，故不属于全量重算。
            self._trim_cache(common - 1)
            self._run_prefill(tokens[common - 1:], on_prefill_progress, should_abort)
        sampler = self._make_sampler(temperature, top_p, top_k, repeat_penalty)
        decoder = codecs.getincrementaldecoder("utf-8")("ignore")
        produced = 0
        budget = max(1, int(max_tokens or 1))
        try:
            while produced < budget:
                token = int(api.llama_sampler_sample(sampler, self._ctx, -1))
                if token < 0 or api.llama_vocab_is_eog(self._vocab, token):
                    break
                piece = self._piece(token)
                text = decoder.decode(piece.encode("utf-8", "ignore"), False)
                produced += 1
                self._decode_one(token)
                self._cached_tokens.append(token)
                if text:
                    yield text
        finally:
            api.llama_sampler_free(sampler)

    def create_completion(self, prompt: str, max_tokens: int = 128,
                          temperature: float = 0.0, stream: bool = True,
                          echo: bool = False, on_prefill_progress=None,
                          should_abort=None, **kwargs) -> object:
        """
        [EN]
        Plain-text completion, used by the hardware auto-tuner. It shares the decode and
        sampling path with chat, but takes an already formatted prompt (no chat template)
        and returns ``choices[0].text`` chunks, matching the shape the tuner reads.

        [ZH]
        纯文本补全，供硬件自动调优使用。它与聊天共用解码与采样路径，但接收已格式化好的
        提示词（不套聊天模板），并返回 ``choices[0].text`` 形式的增量，与调优器读取的结构
        一致。
        """
        tokens = self.tokenize(prompt, add_bos=True, special=True)
        if stream:
            def generator() -> Iterator[Dict[str, object]]:
                for text in self._generate(tokens, int(max_tokens), float(temperature),
                                           kwargs.get("top_p"), kwargs.get("top_k"),
                                           kwargs.get("repeat_penalty"),
                                           on_prefill_progress, should_abort):
                    yield {"choices": [{"text": text, "index": 0}]}
            return generator()
        return {"choices": [{"text": "".join(self._generate(
            tokens, int(max_tokens), float(temperature), kwargs.get("top_p"),
            kwargs.get("top_k"), kwargs.get("repeat_penalty"),
            on_prefill_progress, should_abort)), "index": 0}]}

    def create_chat_completion(self, **kwargs) -> Dict[str, object]:
        """
        [EN]
        Compatibility shim with the removed Python binding: accepts the same
        keyword arguments and returns the same shape, either a completed response
        dict or a generator of OpenAI-style streaming chunks. The reasoning-tag
        splitting and heartbeat logic live in ``llm_engine`` and are unchanged.

        [ZH]
        与已移除的 Python 绑定的兼容层：接受相同关键字参数并返回相同结构 ——
        非流式返回完整响应字典，流式返回 OpenAI 风格增量块的生成器。思考标签分流与
        心跳逻辑仍留在 ``llm_engine``，未作改动。
        """
        messages = kwargs.get("messages") or []
        stream = bool(kwargs.get("stream", False))
        temperature = float(kwargs.get("temperature", 0.7))
        max_tokens = int(kwargs.get("max_tokens", 1024))
        top_p = kwargs.get("top_p")
        top_k = kwargs.get("top_k")
        repeat_penalty = kwargs.get("repeat_penalty")
        # [EN] v0.2.7 prefill hooks. They are pulled out here and passed to the prefill
        # [EN] loop only, so they never become part of the sampler parameters.
        # [ZH] v0.2.7 的预填充钩子。在这里单独取出并只传给预填充循环，绝不进入采样器参数。
        on_prefill_progress = kwargs.get("on_prefill_progress")
        should_abort = kwargs.get("should_abort")

        if stream:
            def generator() -> Iterator[Dict[str, object]]:
                for text in self._stream_tokens(
                        messages, max_tokens, temperature, top_p, top_k,
                        repeat_penalty, on_prefill_progress, should_abort):
                    yield {
                        "choices": [{"delta": {"content": text}, "index": 0}],
                        "object": "text_completion",
                    }
            return generator()

        text = "".join(self._stream_tokens(
            messages, max_tokens, temperature, top_p, top_k, repeat_penalty,
            on_prefill_progress, should_abort))
        return {
            "choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop", "index": 0}],
            "object": "chat.completion",
        }

    # [EN] ---------- lifecycle ----------
    # [ZH] ---------- 生命周期 ----------

    def reset(self) -> None:
        """[EN] Clear the KV cache, matching the old ``Llama.reset``.
        [ZH] 清空 KV Cache，与旧的 ``Llama.reset`` 一致。"""
        if self._ctx:
            self._clear_memory()

    def close(self) -> None:
        if not self._loaded:
            return
        api = self._api
        try:
            if self._ctx:
                api.llama_free(self._ctx)
        finally:
            self._ctx = None
            try:
                if self._model:
                    api.llama_model_free(self._model)
            finally:
                self._model = None
                self._vocab = None
                self._loaded = False

    def __del__(self) -> None:  # pragma: no cover - best-effort native cleanup
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False
