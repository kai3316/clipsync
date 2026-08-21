# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for ClipSync.

Build locally:
    pip install pyinstaller
    pyinstaller clipsync.spec
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Project root — needed so PyInstaller finds the 'internal' package
_PROJ_ROOT = os.path.abspath(SPECPATH)

hiddenimports = collect_submodules("internal")
hiddenimports += [
    "zeroconf",
    "cryptography",
    "PIL",
    "pystray",
    "customtkinter",
    "qrcode",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "logging.handlers",
]
# Fallback: explicit internal modules in case collect_submodules misses them
hiddenimports += [
    "internal.clipboard.clipboard",
    "internal.clipboard.clipboard_windows",
    "internal.clipboard.clipboard_darwin",
    "internal.clipboard.clipboard_linux",
    "internal.clipboard.filter",
    "internal.clipboard.format",
    "internal.clipboard.history",
    "internal.clipboard.platform",
    "internal.config.config",
    "internal.platform.autostart",
    "internal.platform.notify",
    "internal.protocol.codec",
    "internal.security.pairing",
    "internal.sync.file_transfer",
    "internal.sync.manager",
    "internal.transport.connection",
    "internal.transport.discovery",
    "internal.ui.dashboard",
    "internal.ui.dialogs",
    "internal.ui.settings_window",
    "internal.ui.systray",
    "internal.security.encryption",
]

datas = collect_data_files("internal.web")

if sys.platform == "darwin":
    hiddenimports += ["pyobjc_framework_Cocoa"]
elif sys.platform == "linux":
    hiddenimports += ["pynput"]

a = Analysis(
    ["src/main.py"],
    pathex=[_PROJ_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="clipsync",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="clipsync",
    )
    app = BUNDLE(
        coll,
        name="clipsync.app",
        icon=None,
        bundle_identifier="com.clipsync.app",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSUIElement": True,
            "NSAppTransportSecurity": {
                "NSAllowsLocalNetworking": True,
            },
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="clipsync",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        icon="assets/icon.ico" if sys.platform == "win32" else None,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
