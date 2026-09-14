# -*- mode: python ; coding: utf-8 -*-
"""Business-only sidecar. The legacy desktop bundle remains a separate target.

macOS is built as a directory (``COLLECT``); every other platform stays onefile.

A onefile executable unpacks its whole runtime into a *fresh* ``_MEIxxxx``
directory under the temp root on every launch, and macOS validates the code
signature of every binary written there.  With a path that changes each time,
neither that validation nor the filesystem cache can be reused, so the cost is
paid in full on every start.  Measured on an M-series laptop by timing
``--help`` -- unpacking, the interpreter, and no application code at all:
onefile 8.5s on every run, the same application as a directory 0.15s once warm
(the single 19s first run is that same validation, paid once because the paths
do not move).  Windows has no per-file validation step, which is why the same
onefile costs it far less and why it keeps the single-file shape.
"""

import os
import sys

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

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="clipsync-sidecar",
        debug=False,
        strip=False,
        upx=False,
        console=True,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="clipsync-sidecar",
    )
else:
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
