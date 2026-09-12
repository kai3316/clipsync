# -*- mode: python ; coding: utf-8 -*-
"""Business-only sidecar. The legacy desktop bundle remains a separate target."""

import os

root = os.path.abspath(SPECPATH)
web_static = os.path.join(root, "internal", "web", "static")
a = Analysis(
    ["src/sidecar_main.py"],
    pathex=[root],
    binaries=[],
    datas=[(web_static, os.path.join("internal", "web", "static"))],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "customtkinter", "pystray", "internal.ui", "src.main"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="clipsync-sidecar",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
