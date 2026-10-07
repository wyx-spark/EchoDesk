# -*- coding: utf-8 -*-
"""多语言（任务书 任务三）：语言包加载、回退链与文案取用。

设计要点
--------
* **语言包位置**（按优先级，先找到的先用，便于社区在不重新打包的情况下贡献语言）：

  1. ``<程序目录>/lang/<locale>.json``      —— 用户/翻译者可直接替换的外置语言包
  2. ``<exe 目录>/lang/<locale>.json``      —— 便携版后端目录
  3. ``sys._MEIPASS/lang/<locale>.json``    —— PyInstaller 打包内置（onedir 下即 _internal/lang）
  4. ``<本文件所在目录>/lang/<locale>.json`` —— 源码运行

* **回退链**（任务书 3.4）：``请求语言 → en → zh-Hans → 键名本身``。
  返回键名本身是刻意的：让缺失的键在界面上显眼，而不是显示空白。

* **按需加载**（任务书 3.9）：只把当前语言与回退链上的语言读进内存，
  不会一次性加载所有语言包。

* **带注释的语言包**（任务书 3.3）：值可以是字符串，也可以是
  ``{"text": "...", "comment": "给翻译者的上下文说明", "placeholders": {...}}``；
  以 ``_`` 或 ``//`` 开头的键视为注释，加载时忽略。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Dict, List, Optional

# ---------- 语言与回退链 ----------

#: 支持的语言（发布时内置语言包的语言）
SUPPORTED_LOCALES: List[str] = ["zh-Hans", "en"]

#: 默认语言（所有键的权威来源，缺失时以它为准）
SOURCE_LOCALE = "zh-Hans"

#: 全局回退顺序：先英文，再中文，最后键名
FALLBACK_CHAIN: List[str] = ["en", "zh-Hans"]

#: 语言展示名（用于设置界面的下拉框；语言名一律用该语言自己的写法）
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
    """把各种区域标签归一化成语言包文件名（``zh-CN`` → ``zh-Hans``）。

    无法识别时返回 ``None``（调用方继续走回退链）。这里不依赖 babel 之类
    的第三方库，手工维护常见映射即可，避免为一个字符串比较引入依赖。
    """
    if not tag:
        return None
    text = str(tag).strip().replace("_", "-")
    if not text:
        return None
    low = text.lower()

    # 已经是标准写法
    for known in LOCALE_NAMES:
        if low == known.lower():
            return known

    parts = low.split("-")
    lang = parts[0]
    script = parts[1] if len(parts) > 1 and len(parts[1]) == 4 else ""
    region = parts[-1] if len(parts) > 1 else ""

    if lang == "zh":
        # 繁体：脚本标签 Hant，或港澳台新马（新马两地简体、繁体都有使用，
        # 这里按常见约定：TW/HK/MO 归繁体，其余归简体）
        if script == "hant" or region in ("tw", "hk", "mo"):
            return "zh-Hant"
        return "zh-Hans"
    if lang == "pt":
        return "pt-BR" if region in ("br", "") else "pt"
    if lang in ("en", "ja", "ko", "de", "fr", "es", "ru", "ar"):
        return lang
    # 未识别的语言：保留原始标签，若存在同名语言包仍可使用
    return text


def detect_system_locale() -> str:
    """检测系统区域对应的语言（任务书 3.4 自动检测）。

    优先用 Windows 的区域设置，其次用 ``LANG`` 环境变量；都拿不到时用源语言。
    """
    candidates: List[str] = []
    if os.name == "nt":
        try:
            import locale as _locale

            # GetUserDefaultLocaleName 比 locale.getdefaultlocale 更准确
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


# ---------- 语言包定位 ----------


def _search_dirs() -> List[str]:
    """语言包搜索目录（按优先级）。"""
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
    # 去重但保持顺序
    seen = set()
    out = []
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def find_locale_file(locale: str) -> Optional[str]:
    """查找某个语言包的绝对路径；找不到返回 ``None``。"""
    if not locale:
        return None
    for directory in _search_dirs():
        path = os.path.join(directory, f"{locale}.json")
        if os.path.isfile(path):
            return path
    return None


def available_locales() -> List[str]:
    """列出实际可用的语言（内置包 + 用户外置包）。

    注意：模块分片 ``<locale>.<模块>.json``（任务书 3.5）不是独立语言，
    必须把点号后的部分去掉，否则设置界面的语言下拉框里会出现
    ``zh-Hans.automation`` 这种伪语言。
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
    # 内置语言排前面，其余按名称排序
    ordered = [loc for loc in SUPPORTED_LOCALES if loc in found]
    ordered += sorted(loc for loc in found if loc not in ordered)
    return ordered


# ---------- 语言包解析 ----------


def _flatten(node: Any, prefix: str, out: Dict[str, str]) -> None:
    """把嵌套语言包拍平成 ``a.b.c -> 文本``。"""
    if isinstance(node, dict):
        # 带注释的单条写法：{"text": ..., "comment": ...}
        if "text" in node and isinstance(node["text"], str):
            out[prefix] = node["text"]
            return
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            if key.startswith("_") or key.startswith("//") or key.startswith("$"):
                continue  # 注释键
            child = f"{prefix}.{key}" if prefix else key
            _flatten(value, child, out)
        return
    if isinstance(node, str):
        out[prefix] = node


def _pack_files(locale: str) -> List[str]:
    """返回某语言的全部语言包文件：主文件 + 同语言的模块分片。

    任务书 3.5 要求"语言包按模块拆分文件，便于多人协作（如 automation.json、
    marketplace.json）"，因此除了 ``<locale>.json``，同目录下所有
    ``<locale>.<模块>.json`` 也会被合并进来；模块分片里的同名键覆盖主文件，
    这样翻译者可以在单独文件里维护一大块文案而不必改主文件。
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
    """读取并拍平一个语言包（含模块分片）；文件缺失或损坏时返回空字典（不抛异常）。"""
    flat: Dict[str, str] = {}
    for path in _pack_files(locale):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # 语言包损坏不应让程序起不来
            print(f"⚠️ 语言包 {path} 读取失败：{e}", file=sys.stderr, flush=True)
            continue
        _flatten(data, "", flat)
    return flat


def lang_file_path(locale: str) -> str:
    """返回某语言的期望路径（用于报错提示，不保证存在）。"""
    dirs = _search_dirs()
    base = dirs[-1] if dirs else os.getcwd()
    return os.path.join(base, f"{locale}.json")


class _SafeDict(dict):
    """``str.format_map`` 用：缺失的占位符原样保留，不抛 KeyError。"""

    def __missing__(self, key: str) -> str:  # noqa: D105
        return "{" + key + "}"


class Translator:
    """语言包读取器（线程安全，只加载当前语言与回退链）。"""

    def __init__(self, locale: Optional[str] = None, auto: bool = True) -> None:
        self._lock = threading.RLock()
        self._packs: Dict[str, Dict[str, str]] = {}
        self._locale = SOURCE_LOCALE
        self._requested = ""
        self.auto = auto
        self.set_locale(locale or ("auto" if auto else SOURCE_LOCALE))

    # ---- 语言切换 ----

    @property
    def locale(self) -> str:
        """当前生效的语言。"""
        return self._locale

    @property
    def requested(self) -> str:
        """用户请求的语言（可能是 ``auto``）。"""
        return self._requested

    def set_locale(self, locale: Optional[str]) -> str:
        """切换语言并重载语言包；返回实际生效的语言。

        ``auto`` / 空值 表示按系统区域自动检测。请求的语言没有语言包时，
        按回退链取第一个可用的语言（并记下实际生效值）。
        """
        raw = (locale or "auto").strip()
        self._requested = raw
        if raw.lower() in ("auto", ""):
            want = detect_system_locale() if self.auto else SOURCE_LOCALE
        else:
            want = normalize_locale(raw) or SOURCE_LOCALE

        with self._lock:
            self._packs = {}
            # 按需加载：当前语言 + 回退链（去重，保持顺序）
            chain = [want] + [loc for loc in FALLBACK_CHAIN if loc != want]
            effective = ""
            for loc in chain:
                pack = load_pack(loc)
                if pack:
                    self._packs[loc] = pack
                    if not effective:
                        effective = loc
            if not effective:
                # 一个语言包都读不到：仍可用键名工作，不至于崩溃
                effective = SOURCE_LOCALE
            self._locale = effective
        return self._locale

    def reload(self) -> str:
        """重新读取语言包（翻译者改完文件不必重启程序）。"""
        return self.set_locale(self._requested or self._locale)

    # ---- 取文案 ----

    def t(self, key: str, **kwargs: Any) -> str:
        """取一条文案：按"当前语言 → 回退链 → 键名本身"返回。"""
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
        """取原始文案（不做占位符替换）；任何语言都没有该键时返回 ``None``。"""
        with self._lock:
            for loc in [self._locale] + [x for x in FALLBACK_CHAIN if x != self._locale]:
                pack = self._packs.get(loc)
                if pack and key in pack:
                    return pack[key]
            # 还没加载过的语言（例如调用方显式查询别的语言）临时读一次
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
        """当前语言包里可用的键（不含回退）。"""
        with self._lock:
            return sorted(self._packs.get(self._locale, {}).keys())

    def info(self) -> Dict[str, Any]:
        """给前端/诊断用的状态（设置界面语言下拉框据此渲染）。"""
        return {
            "locale": self._locale,
            "requested": self._requested,
            "available": available_locales(),
            "names": {loc: LOCALE_NAMES.get(loc, loc) for loc in available_locales()},
            "fallback": FALLBACK_CHAIN,
        }


_translator: Optional[Translator] = None


def get() -> Translator:
    """进程级单例。"""
    global _translator
    if _translator is None:
        _translator = Translator()
    return _translator


def t(key: str, **kwargs: Any) -> str:
    """模块级取文案快捷方式（等价于 ``get().t(...)``）。"""
    return get().t(key, **kwargs)


def set_locale(locale: Optional[str]) -> str:
    return get().set_locale(locale)


# ---------------------------------------------------------------------------
# 面向用户的日志（任务十四：INFO 日志双语）
# ---------------------------------------------------------------------------
def log_user(logger: Any, key: str, *args: Any, locale: Optional[str] = None,
             level: str = "info", **kwargs: Any) -> str:
    """写一条**面向用户**的日志：文案走语言包，中英双语。

    为什么需要这个统一入口（v0.2.2 自测问题修复记录 §12.3/§12.4 的收口要求）：
    后端每一行 stderr 都会被前端投到**界面状态栏**（WPF/Avalonia 两侧都是），
    所以 ``log.info`` 里的中文是用户看得见的界面文本 —— 它必须跟着界面语言走。
    零散地改几条会留下"有的双语、有的不双语"的状态，比一致的单语更难解释；
    分类标准（哪种该走这里）见记录 §12.3：

      * **面向用户**（用户正在等、且这句话解释了"在等什么"）→ 用 ``log_user``；
      * **面向开发者**（内部路径、缓存命中次数、状态转储…）→ 继续用普通 ``log.info`` 单语。

    ``level`` 是 v0.2.2 硬编码清查第 5 批加的（默认 ``"info"``，**行为不变**）：
    会进状态栏的日志里，多数其实是 ``warning``/``error``/``exception``
    （"用户点了按钮、操作失败"这一类），它们同样要双语，但级别不能降成 INFO ——
    降级会让 backend_crash.log 与运维视角失真。所以这里按级别转发，不另造入口。

    实现上的两个硬性细节：
    1. **绝不写 ``log.info(t(key))``**：那会把译文本身当成 ``%`` 格式串，
       译文里只要出现一个 ``%``（例如"完成 50%"）就会抛异常或吞掉后续参数。
       所以这里固定用 ``logger.<level>("%s", text)``。
    2. 文案**在调用时**解析（不是导入时）：``set_locale`` 由前端在启动后触发，
       早于它的日志只能落到默认语言 —— 这是设计约束，见记录 §12.3。

    ``*args`` 是按位置传给 ``str.format`` 的补充参数，一般用 ``**kwargs`` 就够。
    """
    text = t(key, **kwargs)
    if args:
        text = text.format(*args)
    # 放到 `text` 之后再拼 locale 变量，避免调用方漏传时静默出错（目前仅用于将来扩展）
    _ = locale
    getattr(logger, level)("%s", text)
    return text
