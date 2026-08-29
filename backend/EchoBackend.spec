# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：自包含单文件 EchoBackend.exe。

使用方式（在项目根目录执行）：
    pyinstaller --noconfirm --distpath <输出目录> backend/EchoBackend.spec
"""
import os

from PyInstaller.utils.hooks import collect_all

# 需要完整收集（含数据文件与子模块）的第三方包
PACKAGES = ["llama_cpp", "pyxtxt", "pydantic", "huggingface_hub", "duckduckgo_search", "win32com"]

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

# 配置文件随包内置（运行时可通过 _MEIPASS 找到）
datas.append((os.path.join(SPECPATH, "config.json"), "."))

a = Analysis(
    [os.path.join(SPECPATH, "main.py")],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["llama_cpp", "pyxtxt", "win32com"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="EchoBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
