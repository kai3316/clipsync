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
sys.path.insert(0, _PROJ_ROOT)

# Single source of truth for versioning (bump internal/version.py only).
from internal.version import __version__ as _VERSION

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
    "internal.ui.onboarding",
    "internal.ui.settings_window",
    "internal.ui.systray",
    "internal.security.encryption",
]

# Bundle the web UI by filesystem path. Do NOT use collect_data_files("internal.web")
# here: for a dotted package it checks the package in an isolated subprocess whose
# PYTHONPATH (CONF["pathex"]) is not populated yet at the top of the spec, so it
# silently finds nothing and the static web UI is left out of the bundle (the web
# server then only serves the minimal fallback page).
datas = [
    (os.path.join(_PROJ_ROOT, "internal", "web", "static"), "internal/web/static"),
]
# Hardening: bundle customtkinter's runtime data (themes, assets) explicitly
# instead of relying solely on pyinstaller-hooks-contrib.
datas += collect_data_files("customtkinter")

# NOTE: the old "pyobjc_framework_Cocoa" hiddenimport was removed — that is a
# pip distribution name, not an importable module, so PyInstaller logged a
# no-op "hidden import not found". main.py's _hide_dock() imports rubicon.objc
# inside a try/except and falls back to ctypes, so no macOS ObjC hiddenimport
# is required.
if sys.platform == "linux":
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
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity="-",
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="clipsync",
    )
    app = BUNDLE(
        coll,
        name="clipsync.app",
        icon=os.path.join(_PROJ_ROOT, "assets", "icon.icns"),
        bundle_identifier="com.clipsync.app",
        info_plist={
            "CFBundleName": "ClipSync",
            "CFBundleDisplayName": "ClipSync",
            "CFBundleShortVersionString": _VERSION,
            "CFBundleVersion": _VERSION,
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
        upx=False,
        console=False,
        icon=os.path.join(_PROJ_ROOT, "assets", "icon.ico") if sys.platform == "win32" else None,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
