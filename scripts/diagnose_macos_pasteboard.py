"""Why `[NSPasteboard generalPasteboard]` returns nil on this Mac.

Written for a real machine: one log held **9605** lines of "NSPasteboard.generalPasteboard
returned nil" and **zero** of "changeCount", so the ctypes bridge never built and the
clipboard monitor spent its whole life on the fallback path.  The message is specific
enough to narrow the fault, and it rules two others out: `find_library("objc")` did not
fail, and `objc_getClass(b"NSPasteboard")` did not return nil -- otherwise the log would say
so.  What returned nil is the pasteboard instance itself.

That is the shape of a process with no connection to the pasteboard server, which is not a
code error and not something the code can fix from where it stands.  This script separates
the possibilities so the answer is measured rather than guessed:

  1. Can `libobjc` be found and loaded at all?
  2. Does `objc_getClass("NSPasteboard")` resolve the class?
  3. Does `objc_getClass("NSApplication")` resolve -- is AppKit even loaded?
  4. Does `generalPasteboard` return nil, and does it change after `NSApplicationLoad()`?
  5. Does an `NSAutoreleasePool` around the call change the answer?
  6. Does the `pbpaste` fallback work (so we know the pasteboard server is reachable at all)?
  7. Is there a GUI session: `$SSH_CONNECTION`, `launchctl managername`, the console user?

Run it on the Mac, from the same context the application runs in -- clicking it in Finder
if that is how it is started, because a shell inherits an environment the app may not have:

    python3 scripts/diagnose_macos_pasteboard.py

Every check prints what it saw.  Nothing is changed on the machine.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import subprocess
import sys


def line(label: str, value: object) -> None:
    print(f"  {label:<34} {value}")


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def main() -> int:
    print("macOS pasteboard bridge diagnosis")
    line("platform", sys.platform)
    line("python", sys.version.split()[0])
    line("frozen (PyInstaller)", getattr(sys, "frozen", False))

    section("1. Where this process sits")
    line("uid", os.getuid() if hasattr(os, "getuid") else "n/a")
    line("$SSH_CONNECTION", os.environ.get("SSH_CONNECTION", "(not set)"))
    line("$TERM_PROGRAM", os.environ.get("TERM_PROGRAM", "(not set)"))
    line("$DISPLAY", os.environ.get("DISPLAY", "(not set)"))
    for command, label in (
        (["launchctl", "managername"], "launchctl managername"),
        (["stat", "-f", "%Su", "/dev/console"], "console owner"),
        (["id", "-un"], "current user"),
    ):
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=5)
            line(label, (done.stdout or done.stderr).strip() or "(empty)")
        except Exception as exc:  # noqa: BLE001 - a diagnosis reports, it does not raise
            line(label, f"failed: {exc}")

    section("2. The objc library")
    path = ctypes.util.find_library("objc")
    line("find_library('objc')", path or "NOT FOUND")
    if not path:
        print("\n  The bridge stops here: no libobjc to load.")
        return 1
    try:
        objc = ctypes.cdll.LoadLibrary(path)
        line("LoadLibrary", "ok")
    except Exception as exc:  # noqa: BLE001
        line("LoadLibrary", f"FAILED: {exc}")
        return 1

    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    objc.objc_msgSend.restype = ctypes.c_void_p

    section("3. The classes it needs")
    classes = {}
    for name in (b"NSPasteboard", b"NSApplication", b"NSAutoreleasePool", b"NSObject"):
        handle = objc.objc_getClass(name)
        classes[name] = handle
        line(f"objc_getClass({name.decode()})", hex(handle) if handle else "nil")

    if not classes[b"NSApplication"]:
        print("\n  AppKit is not loaded in this process.  `generalPasteboard` needs it.")

    section("4. generalPasteboard, before and after NSApplicationLoad")
    selector = objc.sel_registerName(b"generalPasteboard")

    def pasteboard() -> int:
        return objc.objc_msgSend(classes[b"NSPasteboard"], selector) or 0

    before = pasteboard()
    line("before NSApplicationLoad", hex(before) if before else "nil")

    if classes[b"NSApplication"]:
        # NSApplicationLoad() is a C function rather than a message, and it is the documented
        # way to bring AppKit up in a process that is not a bundled application -- which is
        # exactly what a sidecar is.  A pasteboard that works after this line and not before
        # names the fault precisely.
        try:
            appkit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("AppKit") or "")
            appkit.NSApplicationLoad.restype = ctypes.c_bool
            loaded = appkit.NSApplicationLoad()
            line("NSApplicationLoad()", loaded)
        except Exception as exc:  # noqa: BLE001
            line("NSApplicationLoad()", f"unavailable: {exc}")
    after = pasteboard()
    line("after NSApplicationLoad", hex(after) if after else "nil")

    section("5. With an autorelease pool around the call")
    if classes[b"NSAutoreleasePool"]:
        try:
            alloc = objc.sel_registerName(b"alloc")
            init = objc.sel_registerName(b"init")
            drain = objc.sel_registerName(b"drain")
            pool = objc.objc_msgSend(classes[b"NSAutoreleasePool"], alloc)
            pool = objc.objc_msgSend(pool, init)
            line("pool allocated", hex(pool) if pool else "nil")
            pooled = pasteboard()
            line("generalPasteboard in pool", hex(pooled) if pooled else "nil")
            if pool:
                objc.objc_msgSend(pool, drain)
        except Exception as exc:  # noqa: BLE001
            line("autorelease pool", f"failed: {exc}")
    else:
        line("NSAutoreleasePool", "class not found")

    section("6. The fallback path (is the pasteboard server reachable at all?)")
    for prefer in ("txt", "html"):
        try:
            done = subprocess.run(
                ["pbpaste", "-Prefer", prefer], capture_output=True, timeout=5
            )
            line(
                f"pbpaste -Prefer {prefer}",
                f"rc={done.returncode} stdout={len(done.stdout)}B stderr={done.stderr[:60]!r}",
            )
        except Exception as exc:  # noqa: BLE001
            line(f"pbpaste -Prefer {prefer}", f"failed: {exc}")

    section("7. Verdict")
    if after or before:
        print("  The bridge works from here.  If the application still reports nil, the")
        print("  difference is the environment it runs in, not this code.")
    else:
        print("  generalPasteboard returns nil in this context.  The application cannot")
        print("  use the ctypes bridge and falls back to pbpaste -- which is why the")
        print("  fallback's cost per poll mattered, and why the monitor could still")
        print("  report changes at all.")
        if os.environ.get("SSH_CONNECTION"):
            print("  This shell is over SSH, which is a context the application does not")
            print("  use.  Run it the way the application is started for the real answer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
