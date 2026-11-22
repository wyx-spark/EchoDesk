# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir（单文件夹多文件）模式的 EchoDesk 后端。

为什么用 onedir 而不是 onefile：
  * onefile 每次启动都要把上百 MB 依赖解压到临时目录，冷启动明显更慢；
  * onefile 的自解压行为是杀毒软件误报的主要来源；
  * onedir 依赖集中在 _internal 子文件夹，主目录干净、启动快、便于增量更新
    （只替换 backend 目录即可升级后端）。

为什么后端元数据不用 rcedit 写：
  PyInstaller 会把 PKG 归档**追加**在 PE 映像之后，而 rcedit 重写资源时会
  重建 PE 映像并丢掉这段追加数据，导致启动时报
  「Could not load PyInstaller's embedded PKG archive」。
  因此后端的版本资源与图标改用 PyInstaller 原生的 version / icon 参数，
  在追加归档**之前**写入（与设计规范“rcedit 在构建完成后执行”的差别见交付文档）。
  前端 .NET 程序没有追加数据，仍然用 rcedit 统一处理。

使用方式（在项目根目录执行）：
    pyinstaller --noconfirm --distpath <输出目录> backend/EchoBackend.spec
产物：<输出目录>/EchoDeskBackend/  （EchoDeskBackend.exe + _internal/）
发布脚本 tools\\build_release.ps1 会把该目录整理为 <发布根>/backend/。

跨平台（v0.2.1 跨平台 任务四）：本 spec 现在在三个平台上都能用，但有四处必须按平台
分支，否则非 Windows 上要么打包失败、要么打出带 Windows 假设的产物：
  * 可执行文件名带不带 .exe；
  * 版本资源（VSVersionInfo）与 .ico 图标是 **Windows 专属**的东西，
    PyInstaller 在 Linux/macOS 上不支持，传了会报错或产出无效资源；
  * `win32com` / `pywin32` 只在 Windows 上存在（collect_all 与 hiddenimports 都要跳过）；
  * macOS 的图标需要 .icns（本仓库只有 .ico），因此 macOS 上不打图标，
    GATEKEEPER 签名与 .app 打包由 tools/pack_crossplatform.py 处理。
"""
import os
import re
import sys
import tempfile

from PyInstaller.utils.hooks import collect_all

IS_WINDOWS = sys.platform.startswith("win")

# ---------- 可执行文件元数据（与 tools\set_metadata.bat 保持一致） ----------
# 版本号**从唯一来源取**（v0.2.2 裁定①）：这里以前写死 "0.2.1.0"，升版本时没人改，
# 冻结发布树的后端 exe 就一直报旧版本号。spec 由 PyInstaller 执行，SPECPATH 指向本文件所在目录。
_here = globals().get("SPECPATH") or os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
from version import app_version_4            # noqa: E402

APP_VERSION = app_version_4()
COMPANY = "Void Entropia Studio"
PRODUCT = "EchoDesk"
EXE_NAME = "EchoDeskBackend.exe" if IS_WINDOWS else "EchoDeskBackend"
DESCRIPTION = "EchoDesk - Local AI Desktop Assistant"
COPYRIGHT = "Copyright (C) Void Entropia Studio"
COMMENTS = "Powered by Void Entropia Studio"

# 需要完整收集（含数据文件与子模块）的第三方包。
#
# 注意：**不要**把 pyxtxt 放进来（v0.2.0 已彻底移除对该库的依赖）。
# pyxtxt 的 estrattori 子包在 __init__ 里用 os.listdir 自动导入同目录全部解析器，
# 因此：(1) 不完整收集它的目录就会启动即报 FileNotFoundError；
#       (2) 完整收集（collect_all）会把 estrattori.ocr/audio 等拉进来，
#           连带引入 easyocr → torch/torchvision/scipy/cv2/skimage/numba，
#           后端体积从约 140MB 膨胀到近 1GB；
#       (3) 其依赖链含 GPL/LGPL 组件，对外分发会产生著佐权义务。
# 改用 backend/file_parser.py 中的轻量解析器（python-docx/openpyxl/python-pptx/
# pypdf/xlrd，均为 MIT/BSD），行为更可预测、体积更小。
# v0.2.1（设计规范 1.3）：.pdf 解析由 pymupdf（AGPL-3.0）换成 pypdf（BSD-3-Clause），
# 既消除对外分发时的著佐权义务，又把该依赖的安装体积从 52.9MB 降到约 1.4MB。
# 说明：playwright 必须整包收集——它自带 Node 驱动（driver/ 约 102MB，含 node.exe
# 与 playwright-core），自动化功能依赖该驱动启动本机 Edge，缺文件会在运行时才报错。
PACKAGES = ["pydantic", "huggingface_hub", "duckduckgo_search", "playwright"]
if IS_WINDOWS:
    # WPS/Office COM 只在 Windows 上存在（非 Windows 上装了也没有这个包）
    PACKAGES.append("win32com")

# 明确排除用不到的重型依赖：EchoDesk 只解析 Office/TXT/PDF，
# 不需要 OCR / 深度学习 / 音频图像处理栈。排除后体积大幅下降，
# 同时减少随包分发的第三方组件数量（降低开源合规与杀毒误报风险）。
EXCLUDES = [
    "tkinter", "pyxtxt",
    # v0.2.1：确保 AGPL 的 PyMuPDF 不会因任何间接 import 被误打包
    "pymupdf", "fitz",
    "torch", "torchvision", "torchaudio", "tensorflow", "tensorflow_intel", "keras",
    "easyocr", "cv2", "skimage", "numba", "llvmlite", "scipy", "shapely",
    "pygame", "pynput", "imageio", "matplotlib", "sympy", "networkx", "pandas",
    "IPython", "notebook", "jupyter", "jupyter_client", "jupyter_core",
    # ---------- v0.2.1 发布复核新增：被"可选 import"带进来的未声明组件 ----------
    # 背景：PyInstaller 按**打包机环境**分析，依赖里 `try: import X / except
    # ImportError` 形式的可选依赖只要打包机装了就会被打进发布物。
    # 用 tools\check_bundled_packages.py 比对「干净 venv 声明的包」与
    # 「_internal 里实际存在的顶层模块」时抓到下面三个 ——
    # 它们**都不在 backend\requirements.txt 里**，也就是说发布物含未声明组件。
    #
    # chardet：requests 的可选编码探测伙伴（requests\packages.py 懒加载）。
    #   requests 优先用 charset_normalizer（已声明并会随包分发），chardet 属冗余；
    #   而 chardet 是 LGPL，能不打就不打。
    # bidi / arabic_reshaper：pypdf 的可选 RTL 支持
    #   （pypdf\generic\_appearance_stream.py 里 `try: import arabic_reshaper` 守卫）。
    #   只在生成"从右到左文字的表单外观流"时用到，本项目只调 extract_text()，
    #   用不到；python-bidi 是 LGPL。已验证干净 venv（无二者）里
    #   `from pypdf import PdfReader` 正常，属于纯可选。
    # tzdata / tzlocal：tzlocal（开发机残留，来自与本项目无关的包）的依赖，
    #   纯时区数据；干净环境里 pydantic 只在 `timezone` extra 下才需要 tzdata。
    "chardet", "bidi", "arabic_reshaper", "tzdata", "tzlocal",
]

datas = []
binaries = []
hiddenimports = []
for pkg in PACKAGES:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# ---------- v0.2.7：随包的 vendored 原生 llama.cpp 运行期 ----------
# [EN] v0.2.7 removes the llama-cpp-python wheel; text and vision are served by the
# [EN] vendored native llama.cpp runtime in ``backend/native/<rid>/``. PyInstaller has no
# [EN] static hook for it -- the loader is a plain ctypes path lookup -- so the target
# [EN] RID's dynamic libraries are collected explicitly into ``native/`` next to the
# [EN] frozen modules. That is exactly where ``llama_ctypes._base_path()`` looks when
# [EN] frozen (``dirname(__file__)/native``, i.e. ``_internal/native``). Missing files
# [EN] abort the build here instead of producing a backend that cannot load a model.
# [ZH] v0.2.7 移除 llama-cpp-python 轮子：文本与视觉改由 ``backend/native/<rid>/`` 的
# [ZH] vendored 原生 llama.cpp 运行期承担。PyInstaller 没有对应的静态钩子 —— 加载器是
# [ZH] ctypes 按路径查找 —— 因此这里显式把目标 RID 的动态库收进 ``native/``，与冻结模块
# [ZH] 同级。这正是 ``llama_ctypes._base_path()`` 冻结后查找的位置
# [ZH] （``dirname(__file__)/native``，即 ``_internal/native``）。缺文件就在构建期直接
# [ZH] 失败，而不是产出一个加载不了模型的后端。
_NATIVE_RID = (
    "win-x64" if IS_WINDOWS
    else "osx-arm64" if sys.platform == "darwin"
    else "linux-x64" if sys.platform.startswith("linux")
    else ""
)
_NATIVE_SRC = os.path.join(SPECPATH, "native", _NATIVE_RID) if _NATIVE_RID else ""
#: 缺任何一个都无法加载运行期；与 tools/pack_backend.py 的 REQUIRED_NATIVE_LIBS 同源。
_NATIVE_REQUIRED = {
    "win-x64": ("llama.dll", "mtmd.dll", "ggml.dll", "ggml-base.dll"),
    "linux-x64": ("libllama.so", "libmtmd.so", "libggml.so", "libggml-base.so"),
    "osx-arm64": ("libllama.dylib", "libmtmd.dylib", "libggml.dylib", "libggml-base.dylib"),
}
_NATIVE_PATTERN = (
    r"\.dll$" if IS_WINDOWS
    else r"\.dylib$" if sys.platform == "darwin"
    else r"\.so(\.\d+)*$|\.so\.SOVERSION$"
)

if not _NATIVE_RID:
    raise SystemExit(
        "[EN] Unsupported build platform for the native llama.cpp runtime: %s\n"
        "[ZH] 该构建平台没有对应的原生 llama.cpp 运行期：%s"
        % (sys.platform, sys.platform))
if not os.path.isdir(_NATIVE_SRC):
    raise SystemExit(
        "[EN] Native runtime directory missing: %s\n"
        "[EN] Vendor the libraries first (see backend/native/VENDOR.md).\n"
        "[ZH] 原生运行期目录缺失：%s\n"
        "[ZH] 请先补齐这些库（出处与做法见 backend/native/VENDOR.md）。"
        % (_NATIVE_SRC, _NATIVE_SRC))

_NATIVE_NAMES = sorted(
    name for name in os.listdir(_NATIVE_SRC)
    if os.path.isfile(os.path.join(_NATIVE_SRC, name))
    and re.search(_NATIVE_PATTERN, name, re.IGNORECASE)
)
_NATIVE_MISSING = [name for name in _NATIVE_REQUIRED.get(_NATIVE_RID, ())
                   if name not in _NATIVE_NAMES]
if _NATIVE_MISSING:
    raise SystemExit(
        "[EN] Native runtime for %s is incomplete; missing: %s\n"
        "[ZH] %s 的原生运行期不完整；缺少：%s"
        % (_NATIVE_RID, ", ".join(_NATIVE_MISSING), _NATIVE_RID, ", ".join(_NATIVE_MISSING)))

# [EN] They are shipped as ``datas`` so PyInstaller copies them verbatim into ``native/``
# [EN] without running its own dependency analysis on the set. Note that PyInstaller
# [EN] additionally copies the libraries it can name from code that imports ``ctypes``
# [EN] into the bundle root; ``llama_native._backend_library_dir()`` therefore honours the
# [EN] directory pinned by ``llama_capi`` (``LLAMA_CPP_LIB_PATH``) instead of reading it
# [EN] back from the loaded handle, otherwise the root copy makes ggml backend
# [EN] registration scan the wrong directory (measured: ``reg_count=0``).
# [ZH] 这里用 ``datas``，PyInstaller 会把它们原样复制进 ``native/``，不对这一组做依赖分析。
# [ZH] 注意 PyInstaller 还会从导入 ``ctypes`` 的代码里认出共享库名，再复制一份到包根；
# [ZH] 因此 ``llama_native._backend_library_dir()`` 以 ``llama_capi`` 钉住的目录
# [ZH] （``LLAMA_CPP_LIB_PATH``）为准，而不是从句柄反读 —— 否则包根那一份会让 ggml 后端
# [ZH] 注册去扫描错的目录（实测 ``reg_count=0``）。
for _name in _NATIVE_NAMES:
    datas.append((os.path.join(_NATIVE_SRC, _name), "native"))

# 配置文件随包内置（onedir 下即位于 exe 同级目录，用户可直接编辑）
datas.append((os.path.join(SPECPATH, "config.json"), "."))

# 后端语言包（v0.2.1 任务三）：<locale>.json 与模块分片 <locale>.<模块>.json。
# 打包进 _internal/lang/，运行时由 backend/i18n.py 按"程序目录 lang/ → exe 目录 lang/
# → _MEIPASS/lang/ → 源码目录 lang/"的顺序查找，因此用户也可以把新的语言包直接
# 丢到程序目录的 lang/ 下生效，无需重新打包。
_LANG_DIR = os.path.join(SPECPATH, "lang")
if os.path.isdir(_LANG_DIR):
    for _name in sorted(os.listdir(_LANG_DIR)):
        if _name.lower().endswith(".json"):
            datas.append((os.path.join(_LANG_DIR, _name), "lang"))

# 项目图标（frontend 用同一个 ico，保证前后端图标一致）
ICON_PATH = os.path.normpath(os.path.join(SPECPATH, "..", "assets", "EchoDesk.ico"))


def _write_version_file() -> str:
    """生成 PyInstaller 版本资源文件（VSVersionInfo 格式）。

    语言表用简体中文 080404B0，与 VarFileInfo 的 Translation(0x0804, 1200) 对应。
    """
    parts = (APP_VERSION.split(".") + ["0", "0", "0", "0"])[:4]
    filevers = ", ".join(str(int(p)) for p in parts)

    def s(key: str, value: str) -> str:
        return f"        StringStruct('{key}', '{value}'),"

    fields = "\n".join([
        s("CompanyName", COMPANY),
        s("FileDescription", DESCRIPTION),
        s("FileVersion", APP_VERSION),
        s("InternalName", EXE_NAME),
        s("LegalCopyright", COPYRIGHT),
        s("OriginalFilename", EXE_NAME),
        s("ProductName", PRODUCT),
        s("ProductVersion", APP_VERSION),
        s("Comments", COMMENTS),
    ])
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({filevers}),
    prodvers=({filevers}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
{fields}
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [0x0804, 1200])])
  ]
)
"""
    path = os.path.join(tempfile.gettempdir(), "echodesk_backend_version_info.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


VERSION_FILE = _write_version_file() if IS_WINDOWS else None

a = Analysis(
    [os.path.join(SPECPATH, "main.py")],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    # v0.2.5：`schedule` 已从依赖中移除（全仓库无 import，定时触发由 automation.py 自实现）。
    # [EN] v0.2.7: llama_cpp is no longer a dependency, so it is no longer listed as a
    # [EN] hidden import; the native runtime is collected as files above. cuda_bootstrap
    # [EN] stays explicit because it is imported for its side effects at startup.
    # [ZH] v0.2.7：llama_cpp 已不再是依赖，因此不再列为 hidden import；原生运行期改由上
    # [ZH] 面的文件收集。cuda_bootstrap 仍显式列出，它是在启动时按副作用导入的。
    hiddenimports=hiddenimports + ["cuda_bootstrap", "playwright", "pypdf"]
    + (["win32com"] if IS_WINDOWS else []),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# onedir：EXE 与 COLLECT 成对出现（不再把依赖塞进单个 exe）
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EchoDeskBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=VERSION_FILE,
    icon=ICON_PATH if (IS_WINDOWS and os.path.exists(ICON_PATH)) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="EchoDeskBackend",
)
