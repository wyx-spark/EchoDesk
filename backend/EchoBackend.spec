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
  在追加归档**之前**写入（与任务书"rcedit 在构建完成后执行"的差别见交付文档）。
  前端 .NET 程序没有追加数据，仍然用 rcedit 统一处理。

使用方式（在项目根目录执行）：
    pyinstaller --noconfirm --distpath <输出目录> backend/EchoBackend.spec
产物：<输出目录>/EchoDeskBackend/  （EchoDeskBackend.exe + _internal/）
发布脚本 tools\\build_release.ps1 会把该目录整理为 <发布根>/backend/。
"""
import os
import tempfile

from PyInstaller.utils.hooks import collect_all

# ---------- 可执行文件元数据（与 tools\set_metadata.bat 保持一致） ----------
APP_VERSION = "0.2.0.0"
COMPANY = "Void Entropia Studio"
PRODUCT = "EchoDesk"
EXE_NAME = "EchoDeskBackend.exe"
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
# pymupdf/xlrd，均为 MIT/BSD 或可选商业许可），行为更可预测、体积更小。
# 说明：playwright 必须整包收集——它自带 Node 驱动（driver/ 约 102MB，含 node.exe
# 与 playwright-core），自动化功能依赖该驱动启动本机 Edge，缺文件会在运行时才报错。
PACKAGES = ["llama_cpp", "pydantic", "huggingface_hub", "duckduckgo_search", "win32com", "playwright"]

# 明确排除用不到的重型依赖：EchoDesk 只解析 Office/TXT/PDF，
# 不需要 OCR / 深度学习 / 音频图像处理栈。排除后体积大幅下降，
# 同时减少随包分发的第三方组件数量（降低开源合规与杀毒误报风险）。
EXCLUDES = [
    "tkinter", "pyxtxt",
    "torch", "torchvision", "torchaudio", "tensorflow", "tensorflow_intel", "keras",
    "easyocr", "cv2", "skimage", "numba", "llvmlite", "scipy", "shapely",
    "pygame", "pynput", "imageio", "matplotlib", "sympy", "networkx", "pandas",
    "IPython", "notebook", "jupyter", "jupyter_client", "jupyter_core",
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

# 配置文件随包内置（onedir 下即位于 exe 同级目录，用户可直接编辑）
datas.append((os.path.join(SPECPATH, "config.json"), "."))

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


VERSION_FILE = _write_version_file()

a = Analysis(
    [os.path.join(SPECPATH, "main.py")],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["llama_cpp", "win32com", "playwright", "schedule"],
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
    icon=ICON_PATH if os.path.exists(ICON_PATH) else None,
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
