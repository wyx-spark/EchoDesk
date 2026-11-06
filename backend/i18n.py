# -*- coding: utf-8 -*-
"""
[EN]
Localization: language-pack loading, fallback chain, and text lookup.

Design notes:

* Language-pack search order (first match wins, so that communities can
  contribute translations without rebuilding the application):

  1. ``<program directory>/lang/<locale>.json``: external pack that users and
     translators can replace directly.
  2. ``<exe directory>/lang/<locale>.json``: portable backend directory.
  3. ``sys._MEIPASS/lang/<locale>.json``: PyInstaller bundled pack (for onedir
     builds this resolves to ``_internal/lang``).
  4. ``<directory containing this file>/lang/<locale>.json``: source-tree run.

* Fallback chain: ``requested locale -> en -> zh-Hans -> the key itself``.
  Returning the key itself is intentional: a missing key is visible in the
  interface instead of silently rendering an empty string.

* Lazy loading: only the active locale and the locales reachable through the
  fallback chain are read into memory; all packs are not loaded eagerly.

* Annotated packs: a value may be a string or an object of the form
  ``{"text": "...", "comment": "translator context", "placeholders": {...}}``.
  Keys beginning with ``_`` or ``//`` are treated as comments and ignored.

[ZH]
本地化：语言包加载、回退链与文案查询。

设计说明：

* 语言包搜索顺序（先匹配者优先，使社区无需重新构建应用即可贡献翻译）：

  1. ``<程序目录>/lang/<locale>.json``：用户与翻译者可直接替换的外置语言包。
  2. ``<exe 目录>/lang/<locale>.json``：便携版后端目录。
  3. ``sys._MEIPASS/lang/<locale>.json``：PyInstaller 内置语言包（onedir 构建下
     对应 ``_internal/lang``）。
  4. ``<本文件所在目录>/lang/<locale>.json``：源码运行目录。

* 回退链：``请求语言 -> en -> zh-Hans -> 键名本身``。返回键名本身是有意设计：
  缺失的键会在界面上显式可见，而不是静默渲染为空串。

* 按需加载：仅将当前语言与回退链可达的语言读入内存，不会一次性加载全部语言包。

* 带注释的语言包：值可以是字符串，也可以是
  ``{"text": "...", "comment": "翻译者上下文", "placeholders": {...}}`` 形式的对象。
  以 ``_`` 或 ``//`` 开头的键视为注释并在加载时忽略。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Dict, List, Optional

# ---------- [EN] Languages and fallback chain / [ZH] 语言与回退链 ----------

#: [EN] Locales for which language packs are bundled at release time.
#: [ZH] 发布时内置语言包的语言。
SUPPORTED_LOCALES: List[str] = ["zh-Hans", "en"]

#: [EN] Default locale and authoritative source for all keys.
#: [ZH] 默认语言，也是所有键的权威来源。
SOURCE_LOCALE = "zh-Hans"

#: [EN] Global fallback order: English, then Simplified Chinese, then the key.
#: [ZH] 全局回退顺序：英文、简体中文，最后是键名本身。
FALLBACK_CHAIN: List[str] = ["en", "zh-Hans"]

#: [EN] Display names used by the settings selector. Each locale name is written
#: [EN] in its own language.
#: [ZH] 设置界面下拉框使用的展示名。每个语言名使用该语言自身的写法。
LOCALE_NAMES: Dict[str, str] = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "pt-BR": "Português (Brasil)",
    "ru": "Русский",
    "ar": "العربية",
}


def normalize_locale(tag: Optional[str]) -> Optional[str]:
    """
    [EN]
    Normalize a locale tag to the corresponding language-pack file name
    (``zh-CN`` becomes ``zh-Hans``).

    Returns ``None`` for unrecognized input so the caller continues along the
    fallback chain. No third-party library such as babel is required; a small
    explicit mapping avoids introducing a dependency for string comparison.

    [ZH]
    将区域标签规范化为对应的语言包文件名（``zh-CN`` 转为 ``zh-Hans``）。

    无法识别时返回 ``None``，由调用方继续沿回退链处理。此处不依赖 babel 等
    第三方库；维护一份显式映射即可，避免为字符串比较引入依赖。
    """
    if not tag:
        return None
    text = str(tag).strip().replace("_", "-")
    if not text:
        return None
    low = text.lower()

    # [EN] The tag already uses a canonical spelling.
    # [ZH] 标签已经是规范写法。
    for known in LOCALE_NAMES:
        if low == known.lower():
            return known

    parts = low.split("-")
    lang = parts[0]
    script = parts[1] if len(parts) > 1 and len(parts[1]) == 4 else ""
    region = parts[-1] if len(parts) > 1 else ""

    if lang == "zh":
        # [EN] Traditional Chinese is selected by the Hant script tag or by the
        # [EN] TW/HK/MO regions; all other Chinese regions default to Simplified.
        # [ZH] 繁体中文由 Hant 脚本标签或 TW/HK/MO 区域判定；其他中文区域默认
        # [ZH] 使用简体中文。
        if script == "hant" or region in ("tw", "hk", "mo"):
            return "zh-Hant"
        return "zh-Hans"
    if lang == "pt":
        return "pt-BR" if region in ("br", "") else "pt"
    if lang in ("en", "ja", "ko", "de", "fr", "es", "ru", "ar"):
        return lang
    # [EN] Unknown language: preserve the original tag so that a matching pack
    # [EN] can still be used if one exists.
    # [ZH] 未识别的语言：保留原始标签，若存在同名语言包仍可使用。
    return text


def detect_system_locale() -> str:
    """
    [EN]
    Detect the locale corresponding to the system region.

    The Windows regional setting is preferred, followed by the ``LANG``
    environment variable; the source locale is used when neither is available.

    [ZH]
    检测系统区域对应的语言。

    优先使用 Windows 区域设置，其次使用 ``LANG`` 环境变量；两者均不可用时
    使用源语言。
    """
    candidates: List[str] = []
    if os.name == "nt":
        try:
            import locale as _locale

            # [EN] GetUserDefaultLocaleName is more accurate than locale.getdefaultlocale.
            # [ZH] GetUserDefaultLocaleName 比 locale.getdefaultlocale 更准确。
            import ctypes

            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
                candidates.append(buf.value)
        except Exception:
            pass
        try:
            import locale as _locale

            candidates.append(_locale.getdefaultlocale()[0] or "")
        except Exception:
            pass
    for env in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(env)
        if value:
            candidates.append(value)
    for raw in candidates:
        normalized = normalize_locale(raw)
        if normalized:
            return normalized
    return SOURCE_LOCALE


# ---------- [EN] Language-pack discovery / [ZH] 语言包定位 ----------


def _search_dirs() -> List[str]:
    """
    [EN]
    Return the language-pack search directories in priority order.

    [ZH]
    按优先级返回语言包搜索目录。
    """
    dirs: List[str] = []
    try:
        from config import program_root

        dirs.append(os.path.join(program_root(), "lang"))
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        dirs.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "lang"))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "lang"))
    dirs.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang"))
    # [EN] Remove duplicates while preserving order.
    # [ZH] 去重，同时保持原有顺序。
    seen = set()
    out = []
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def find_locale_file(locale: str) -> Optional[str]:
    """
    [EN]
    Return the absolute path of a language pack, or ``None`` if it is absent.

    [ZH]
    返回语言包的绝对路径；语言包不存在时返回 ``None``。
    """
    if not locale:
        return None
    for directory in _search_dirs():
        path = os.path.join(directory, f"{locale}.json")
        if os.path.isfile(path):
            return path
    return None


def available_locales() -> List[str]:
    """
    [EN]
    List the locales actually available from bundled and external packs.

    Module shards named ``<locale>.<module>.json`` are not independent locales.
    The portion after the dot must be removed; otherwise the settings selector
    would expose pseudo-locales such as ``zh-Hans.automation``.

    [ZH]
    列出内置语言包与用户外置语言包中实际可用的语言。

    模块分片 ``<locale>.<模块>.json`` 不是独立语言。必须去除点号后的部分；
    否则设置界面的语言下拉框会出现 ``zh-Hans.automation`` 这类伪语言。
    """
    found: Dict[str, str] = {}
    for directory in _search_dirs():
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".json"):
                continue
            locale = name[:-5].split(".")[0]
            if locale:
                found.setdefault(locale, directory)
    # [EN] Bundled locales come first; the remaining locales are sorted by name.
    # [ZH] 内置语言排在前面，其余语言按名称排序。
    ordered = [loc for loc in SUPPORTED_LOCALES if loc in found]
    ordered += sorted(loc for loc in found if loc not in ordered)
    return ordered


# ---------- [EN] Language-pack parsing / [ZH] 语言包解析 ----------


def _flatten(node: Any, prefix: str, out: Dict[str, str]) -> None:
    """
    [EN]
    Flatten a nested language pack into ``a.b.c -> text`` entries.

    [ZH]
    将嵌套语言包拍平为 ``a.b.c -> 文本`` 条目。
    """
    if isinstance(node, dict):
        # [EN] Annotated single-entry form: {"text": ..., "comment": ...}.
        # [ZH] 带注释的单条形式：{"text": ..., "comment": ...}。
        if "text" in node and isinstance(node["text"], str):
            out[prefix] = node["text"]
            return
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            if key.startswith("_") or key.startswith("//") or key.startswith("$"):
                # [EN] Comment key.
                # [ZH] 注释键。
                continue
            child = f"{prefix}.{key}" if prefix else key
            _flatten(value, child, out)
        return
    if isinstance(node, str):
        out[prefix] = node


def _pack_files(locale: str) -> List[str]:
    """
    [EN]
    Return all pack files for a locale: the main file plus module shards.

    In addition to ``<locale>.json``, every ``<locale>.<module>.json`` file in
    the same directory is merged into the pack. Keys in module shards override
    keys in the main file, so a translator can maintain a large section in a
    dedicated file without editing the main file.

    [ZH]
    返回某语言的全部语言包文件：主文件与模块分片。

    除 ``<locale>.json`` 外，同目录下所有 ``<locale>.<模块>.json`` 文件也会合并
    进来。模块分片中的同名键覆盖主文件中的键，使翻译者可以在独立文件中维护
    大段文案，而无需修改主文件。
    """
    main = find_locale_file(locale)
    if not main:
        return []
    directory = os.path.dirname(main)
    prefix = f"{locale}."
    files = [main]
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return files
    for name in names:
        if name.lower().endswith(".json") and name.startswith(prefix):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                files.append(path)
    return files


def load_pack(locale: str) -> Dict[str, str]:
    """
    [EN]
    Load and flatten a language pack, including module shards. Missing or
    malformed files yield an empty dictionary rather than an exception.

    [ZH]
    读取并拍平语言包（含模块分片）。文件缺失或损坏时返回空字典，不抛出异常。
    """
    flat: Dict[str, str] = {}
    for path in _pack_files(locale):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        # [EN] A malformed language pack must not prevent the application from starting.
        # [ZH] 语言包格式错误不得阻止应用启动。
        except Exception as e:
            print(f"⚠️ 语言包 {path} 读取失败：{e}", file=sys.stderr, flush=True)
            continue
        _flatten(data, "", flat)
    return flat


def lang_file_path(locale: str) -> str:
    """
    [EN]
    Return the expected path of a locale pack for diagnostic messages. The file
    is not guaranteed to exist.

    [ZH]
    返回某语言包的期望路径，用于诊断提示；不保证文件存在。
    """
    dirs = _search_dirs()
    base = dirs[-1] if dirs else os.getcwd()
    return os.path.join(base, f"{locale}.json")


class _SafeDict(dict):
    """
    [EN]
    Mapping used by ``str.format_map``: missing placeholders are preserved
    verbatim instead of raising ``KeyError``.

    [ZH]
    供 ``str.format_map`` 使用的映射：缺失的占位符原样保留，不抛出 ``KeyError``。
    """

    def __missing__(self, key: str) -> str:  # noqa: D105
        return "{" + key + "}"


class Translator:
    """
    [EN]
    Language-pack reader. It is thread-safe and loads only the active locale and
    the fallback chain.

    [ZH]
    语言包读取器。该读取器线程安全，仅加载当前语言与回退链。
    """

    def __init__(self, locale: Optional[str] = None, auto: bool = True) -> None:
        self._lock = threading.RLock()
        self._packs: Dict[str, Dict[str, str]] = {}
        self._locale = SOURCE_LOCALE
        self._requested = ""
        self.auto = auto
        self.set_locale(locale or ("auto" if auto else SOURCE_LOCALE))

    # ---- [EN] Locale switching / [ZH] 语言切换 ----

    @property
    def locale(self) -> str:
        """
        [EN]
        Return the currently effective locale.

        [ZH]
        返回当前实际生效的语言。
        """
        return self._locale

    @property
    def requested(self) -> str:
        """
        [EN]
        Return the locale requested by the user, which may be ``auto``.

        [ZH]
        返回用户请求的语言；该值可能为 ``auto``。
        """
        return self._requested

    def set_locale(self, locale: Optional[str]) -> str:
        """
        [EN]
        Switch locale, reload language packs, and return the effective locale.

        ``auto`` or an empty value selects the system region. If the requested
        locale has no pack, the first available locale in the fallback chain is
        used and recorded as the effective locale.

        [ZH]
        切换语言、重新加载语言包，并返回实际生效的语言。

        ``auto`` 或空值表示按系统区域自动检测。若请求的语言没有语言包，则使用
        回退链中第一个可用语言，并将其记录为实际生效语言。
        """
        raw = (locale or "auto").strip()
        self._requested = raw
        if raw.lower() in ("auto", ""):
            want = detect_system_locale() if self.auto else SOURCE_LOCALE
        else:
            want = normalize_locale(raw) or SOURCE_LOCALE

        with self._lock:
            self._packs = {}
            # [EN] Lazy loading: active locale plus fallback chain, deduplicated
            # [EN] while preserving order.
            # [ZH] 按需加载：当前语言与回退链，去重并保持顺序。
            chain = [want] + [loc for loc in FALLBACK_CHAIN if loc != want]
            effective = ""
            for loc in chain:
                pack = load_pack(loc)
                if pack:
                    self._packs[loc] = pack
                    if not effective:
                        effective = loc
            if not effective:
                # [EN] No pack could be loaded; the reader still returns keys and
                # [EN] therefore remains operational.
                # [ZH] 无法加载任何语言包；读取器仍可返回键名并保持可用。
                effective = SOURCE_LOCALE
            self._locale = effective
        return self._locale

    def reload(self) -> str:
        """
        [EN]
        Reload language packs so that translators do not need to restart the
        application after editing a pack.

        [ZH]
        重新读取语言包，使翻译者修改语言包后无需重启应用。
        """
        return self.set_locale(self._requested or self._locale)

    # ---- [EN] Text lookup / [ZH] 文案取用 ----

    def t(self, key: str, **kwargs: Any) -> str:
        """
        [EN]
        Return one localized string using the order active locale, fallback
        chain, then the key itself.

        [ZH]
        按“当前语言、回退链、键名本身”的顺序返回一条本地化文案。
        """
        text = self.raw(key)
        if text is None:
            return key
        if kwargs:
            try:
                return text.format_map(_SafeDict(**kwargs))
            except Exception:
                return text
        return text

    def raw(self, key: str) -> Optional[str]:
        """
        [EN]
        Return the raw string without placeholder substitution, or ``None`` when
        no locale contains the key.

        [ZH]
        返回未经占位符替换的原始文案；所有语言均不包含该键时返回 ``None``。
        """
        with self._lock:
            for loc in [self._locale] + [x for x in FALLBACK_CHAIN if x != self._locale]:
                pack = self._packs.get(loc)
                if pack and key in pack:
                    return pack[key]
            # [EN] Lazily load a locale that has not been read yet, for example
            # [EN] when a caller explicitly queries another locale.
            # [ZH] 按需加载尚未读取的语言，例如调用方显式查询其他语言时。
            for loc in FALLBACK_CHAIN:
                if loc in self._packs:
                    continue
                pack = load_pack(loc)
                if pack:
                    self._packs[loc] = pack
                    if key in pack:
                        return pack[key]
        return None

    def has(self, key: str) -> bool:
        return self.raw(key) is not None

    def keys(self) -> List[str]:
        """
        [EN]
        Return the keys available in the active pack, excluding fallback packs.

        [ZH]
        返回当前语言包中可用的键，不包含回退语言包。
        """
        with self._lock:
            return sorted(self._packs.get(self._locale, {}).keys())

    def info(self) -> Dict[str, Any]:
        """
        [EN]
        Return state for the frontend and diagnostics. The settings selector
        renders from this payload.

        [ZH]
        返回供前端与诊断使用的状态。设置界面的语言下拉框据此渲染。
        """
        return {
            "locale": self._locale,
            "requested": self._requested,
            "available": available_locales(),
            "names": {loc: LOCALE_NAMES.get(loc, loc) for loc in available_locales()},
            "fallback": FALLBACK_CHAIN,
        }


_translator: Optional[Translator] = None


def get() -> Translator:
    """
    [EN]
    Return the process-wide translator singleton.

    [ZH]
    返回进程级翻译器单例。
    """
    global _translator
    if _translator is None:
        _translator = Translator()
    return _translator


def t(key: str, **kwargs: Any) -> str:
    """
    [EN]
    Module-level convenience wrapper equivalent to ``get().t(...)``.

    [ZH]
    模块级便捷入口，等价于 ``get().t(...)``。
    """
    return get().t(key, **kwargs)


def set_locale(locale: Optional[str]) -> str:
    return get().set_locale(locale)


# ---------------------------------------------------------------------------
# [EN] User-facing logging / [ZH] 面向用户的日志
# ---------------------------------------------------------------------------
def log_user(logger: Any, key: str, *args: Any, locale: Optional[str] = None,
             level: str = "info", **kwargs: Any) -> str:
    """
    [EN]
    Write a user-facing log entry whose text comes from the language pack.

    Every backend stderr line is forwarded by the frontend to the interface
    status bar, so Chinese text emitted through ``log.info`` is user-visible
    interface text and must follow the active interface language. Updating only
    a few call sites would leave a mixture of localized and non-localized
    messages that is harder to reason about than consistent single-language
    output.

    Classification rule:

    * User-facing messages (the user is waiting and the line explains what is
      happening) use ``log_user``.
    * Developer-facing messages (internal paths, cache hit counts, state dumps)
      continue to use ordinary single-language ``log.info``.

    ``level`` defaults to ``"info"`` and selects the logger method. Many
    status-bar messages are warnings, errors, or exceptions; they must remain at
    their original severity so that ``backend_crash.log`` and operational views
    are not distorted. The function therefore forwards the requested level
    instead of creating a second logging entry point.

    Two implementation constraints:

    1. ``log.info(t(key))`` must never be used: the translated text would be
       treated as a ``%`` format string, so a literal percent sign (for example
       "completed 50%") could raise or consume subsequent arguments. This
       function always calls ``logger.<level>("%s", text)``.
    2. Text is resolved at call time, not import time. ``set_locale`` is invoked
       by the frontend after startup; messages emitted earlier necessarily use
       the default locale.

    ``*args`` are positional arguments for ``str.format``; ``**kwargs`` is
    normally sufficient.

    [ZH]
    写入一条面向用户的日志，文本来自语言包。

    后端每一行 stderr 都会被前端转发到界面状态栏，因此通过 ``log.info`` 输出的
    中文属于用户可见的界面文本，必须跟随当前界面语言。仅修改少量调用点会留下
    本地化与非本地化消息混杂的状态，其可解释性低于一致的单语文案。

    分类规则：

    * 面向用户的消息（用户正在等待，且该行解释当前正在发生什么）使用
      ``log_user``。
    * 面向开发者的消息（内部路径、缓存命中次数、状态转储）继续使用普通的
      单语 ``log.info``。

    ``level`` 默认为 ``"info"``，用于选择日志方法。状态栏消息中有许多属于
    warning、error 或 exception；这些消息必须保持原有严重级别，以免扭曲
    ``backend_crash.log`` 与运维视图。因此本函数转发指定级别，而不是另建
    日志入口。

    两个实现约束：

    1. 不得使用 ``log.info(t(key))``：译文会被当作 ``%`` 格式串，文本中出现
       字面百分号（例如“已完成 50%”）时可能抛出异常或消费后续参数。本函数
       固定调用 ``logger.<level>("%s", text)``。
    2. 文案在调用时解析，而非导入时解析。``set_locale`` 由前端在启动后调用；
       早于该调用的消息只能使用默认语言。

    ``*args`` 是传给 ``str.format`` 的位置参数；通常使用 ``**kwargs`` 即可。
    """
    text = t(key, **kwargs)
    if args:
        text = text.format(*args)
    # [EN] Read the locale argument after formatting so that a missing value
    # [EN] cannot cause a silent failure. Reserved for future extension.
    # [ZH] 在格式化之后读取 locale 参数，避免调用方漏传时静默失败。该参数保留
    # [ZH] 用于后续扩展。
    _ = locale
    getattr(logger, level)("%s", text)
    return text
