# -*- mode: python ; coding: utf-8 -*-
"""Business-only sidecar. The legacy desktop bundle remains a separate target.

Two shapes come out of this file, chosen by ``CLIPSYNC_SIDECAR_ONEDIR``:

* onefile (default) -- one ``clipsync-sidecar`` executable.  A launch unpacks
  the whole runtime into a *fresh* ``_MEIxxxx`` directory under the temp root.
* onedir (``CLIPSYNC_SIDECAR_ONEDIR=1``) -- a ``clipsync-sidecar/`` directory
  holding the executable and its ``_internal`` tree.  A launch starts it where
  it lies.

The difference is the unpacking.  Measured here by timing ``--help``, which
starts the interpreter and runs no application code at all: onefile 870 ms on
every run, onedir 180 ms.  On macOS that gap is wider still, because the
platform validates the code signature of every binary written to a path that
changes each time -- 8.5s against 0.15s warm on an M-series laptop, which is why
macOS has always built the directory.

Windows and Linux stayed onefile for the reason the release is shaped the way it
is: ``externalBin`` takes a single executable, and a directory has nowhere to
put itself.  That is a packaging constraint, not a preference, and it is lifted
by shipping the tree as a bundle *resource* -- which macOS already does.  So the
shape is now the packager's choice per platform rather than this file's.

A caller that stages the directory form must also tell the host where to find
it: ``internal/transport``'s sibling lookup in ``desktop/src-tauri/src/bridge.rs``
prefers ``sidecar/clipsync-sidecar`` when it is there, and falls back to the
onefile beside the host.
"""

import os
import sys

# Set by the packager for the platform that ships the tree; see the docstring.
_onedir = os.environ.get("CLIPSYNC_SIDECAR_ONEDIR", "").strip() not in ("", "0", "false")
_onedir = _onedir or sys.platform == "darwin"

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

if _onedir:
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
