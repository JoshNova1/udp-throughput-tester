# PyInstaller spec — produces a one-folder distributable for desktop installs.
#
# Build:    pyinstaller desktop.spec
# Output:   dist/UDPThroughputTester/UDPThroughputTester(.exe)
#
# Bundle external tool binaries by dropping them into ./bin/ before building:
#
#   bin/
#     ffmpeg(.exe)
#     iperf3(.exe)
#     srt-live-transmit(.exe)   (optional — Windows users without this fall
#                                 back to ffmpeg's native SRT support)
#
# Licensing note: ffmpeg LGPL builds are redistributable. iperf3 is BSD.
# libsrt (and srt-live-transmit) are MPL-2.0. Bundle the corresponding
# LICENSE files alongside.
# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
HERE = Path(os.getcwd())

datas = [
    (str(HERE / "templates"), "templates"),
    (str(HERE / "static"), "static"),
    (str(HERE / "tests"), "tests"),
]

# Bundle anything dropped into ./bin/ as runtime tools.
bin_dir = HERE / "bin"
if bin_dir.exists():
    for item in bin_dir.iterdir():
        datas.append((str(item), "bin"))

hiddenimports = []
hiddenimports += collect_submodules("flask")
hiddenimports += collect_submodules("flask_sock")
hiddenimports += collect_submodules("simple_websocket")
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("tests")

a = Analysis(
    ["desktop.py"],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UDPThroughputTester",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,           # GUI app, no console window on Windows
    icon=str(HERE / "deploy" / "nctech.ico") if (HERE / "deploy" / "nctech.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="UDPThroughputTester",
)
