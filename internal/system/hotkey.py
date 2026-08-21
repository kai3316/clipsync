"""Cross-platform global hotkey manager.

Windows: RegisterHotKey + message-only window + WM_HOTKEY
macOS:   CGEvent via ctypes (CoreGraphics framework)
Linux:   pynput global hotkey listener
"""

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# ── Platform detection ──────────────────────────────────────────────

def _platform() -> str:
    import sys

    if sys.platform == "win32":
        return "windows"
    elif sys.platform == "darwin":
        return "macos"
    else:
        return "linux"


# ── Modifier key constants (internal representation) ────────────────
# These are defined independently of any OS API.  Each platform
# implementation translates these to its native modifier flags.

MOD_CONTROL = 0x01
MOD_ALT = 0x02
MOD_SHIFT = 0x04
MOD_WIN = 0x08  # Windows logo key / Command key


# ══════════════════════════════════════════════════════════════════════
# Platform-specific VK-code tables
# ══════════════════════════════════════════════════════════════════════

# Windows virtual key codes (VK_* from winuser.h)
_WIN_VK: dict[str, int] = {
    "`": 0xC0,
    "~": 0xC0,
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}

# macOS ADB keycodes (Carbon HIToolbox / IOKit — hardware-based, locale-independent)
_MAC_VK: dict[str, int] = {
    "`": 50,  # kVK_ANSI_Grave
    "~": 50,
    "space": 49,  # kVK_Space
    "tab": 48,  # kVK_Tab
    "enter": 36,  # kVK_Return
    "escape": 53,  # kVK_Escape
    "backspace": 51,  # kVK_Delete (Mac backspace)
    "delete": 51,
    "forwarddelete": 117,  # kVK_ForwardDelete
    "up": 126,  # kVK_UpArrow
    "down": 125,  # kVK_DownArrow
    "left": 123,  # kVK_LeftArrow
    "right": 124,  # kVK_RightArrow
    "home": 115,  # kVK_Home
    "end": 119,  # kVK_End
    "pageup": 116,  # kVK_PageUp
    "pagedown": 121,  # kVK_PageDown
    "f1": 122,
    "f2": 123,
    "f3": 124,
    "f4": 125,
    "f5": 126,
    "f6": 127,
    "f7": 128,
    "f8": 129,
    "f9": 130,
    "f10": 131,
    "f11": 132,
    "f12": 133,
}

# macOS letter keycodes (ADB layout, independent of keyboard locale)
_MAC_LETTER_VK: dict[str, int] = {
    "A": 0, "B": 11, "C": 8, "D": 2, "E": 14, "F": 3, "G": 5, "H": 4,
    "I": 34, "J": 38, "K": 40, "L": 37, "M": 46, "N": 45, "O": 31,
    "P": 35, "Q": 12, "R": 15, "S": 1, "T": 17, "U": 32, "V": 9,
    "W": 13, "X": 7, "Y": 16, "Z": 6,
}


# ══════════════════════════════════════════════════════════════════════
# Default shortcuts
# ══════════════════════════════════════════════════════════════════════

DEFAULT_SHORTCUTS: dict[str, str] = {
    "quick_paste": "Ctrl+`",
    "paste_1": "Ctrl+1",
    "paste_2": "Ctrl+2",
    "paste_3": "Ctrl+3",
    "paste_4": "Ctrl+4",
    "paste_5": "Ctrl+5",
    "paste_6": "Ctrl+6",
    "paste_7": "Ctrl+7",
    "paste_8": "Ctrl+8",
    "paste_9": "Ctrl+9",
    "paste_plain": "Ctrl+Shift+V",
    "toggle_monitor": "Ctrl+Shift+M",
    "show_window": "Ctrl+Shift+Space",
}


# ══════════════════════════════════════════════════════════════════════
# HotkeyManager
# ══════════════════════════════════════════════════════════════════════


class HotkeyManager:
    """Register and manage global hotkeys.

    Usage::

        mgr = HotkeyManager()
        mgr.register("my_action", "Ctrl+Shift+K", my_callback)
        mgr.start()
        # ... app runs ...
        mgr.stop()
    """

    def __init__(self) -> None:
        self._running = False
        # hotkey_id -> (modifiers, vk_code, callback)
        self._hotkeys: dict[str, tuple[int, int, Callable]] = {}
        # hotkey_id -> original shortcut string (needed for Linux / pynput)
        self._shortcut_strings: dict[str, str] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._platform = _platform()

        # ── Platform-specific state ──
        # Windows
        self._win_hwnd: int | None = None
        self._win_class_atom: int | None = None
        self._win_id_map: dict[str, int] = {}  # string id -> RegisterHotKey int id
        self._win_id_rev: dict[int, str] = {}  # int id -> string id
        self._win_next_id: int = 1

        # macOS
        self._mac_tap: int | None = None  # CFMachPortRef
        self._mac_run_loop: int | None = None  # CFRunLoopRef
        self._mac_source: int | None = None  # CFRunLoopSourceRef

        # Linux
        self._linux_listener: object = None  # pynput GlobalHotKeys listener

    # ── Public API ─────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def register(self, hotkey_id: str, shortcut: str, callback: Callable) -> bool:
        """Register a global hotkey.

        Args:
            hotkey_id: Unique identifier (e.g. ``"quick_paste"``).
            shortcut: String like ``"Ctrl+`"``, ``"Ctrl+Shift+V"``.
            callback: Function called when hotkey is pressed (no args).

        Returns ``True`` on success, ``False`` if the shortcut string is invalid.
        Registration failures due to platform errors (e.g. shortcut already in use)
        are logged as warnings and do *not* raise exceptions.
        """
        try:
            modifiers, vk_code = self._parse_shortcut(shortcut)
        except ValueError as exc:
            logger.warning("Invalid shortcut '%s': %s", shortcut, exc)
            return False

        with self._lock:
            self._hotkeys[hotkey_id] = (modifiers, vk_code, callback)
            self._shortcut_strings[hotkey_id] = shortcut

        # If already running, register with the platform immediately.
        if self._running:
            self._platform_register_one(hotkey_id, modifiers, vk_code, shortcut)

        logger.debug(
            "Registered hotkey '%s': %s (mods=0x%04x, vk=0x%02x)",
            hotkey_id,
            shortcut,
            modifiers,
            vk_code,
        )
        return True

    def unregister(self, hotkey_id: str) -> None:
        """Remove a registered hotkey."""
        if self._running:
            self._platform_unregister_one(hotkey_id)
        with self._lock:
            self._hotkeys.pop(hotkey_id, None)
            self._shortcut_strings.pop(hotkey_id, None)
        logger.debug("Unregistered hotkey '%s'", hotkey_id)

    def start(self) -> None:
        """Start the hotkey listener thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="hotkey-mgr"
        )
        self._thread.start()
        logger.info("Hotkey manager started on %s", self._platform)

    def stop(self) -> None:
        """Stop the hotkey listener and clean up all OS resources."""
        if not self._running:
            return
        logger.info("Stopping hotkey manager ...")
        self._running = False
        self._platform_stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        with self._lock:
            self._hotkeys.clear()
            self._shortcut_strings.clear()
        logger.info("Hotkey manager stopped")

    def reload_from_config(
        self, shortcuts: dict[str, str], callback_factory: Callable[[str], Callable]
    ) -> None:
        """Reload shortcuts from a config dictionary.

        Args:
            shortcuts: ``dict`` mapping ``hotkey_id`` to ``shortcut_string``.
            callback_factory: ``fn(hotkey_id) -> callback``.
        """
        was_running = self._running
        if was_running:
            self._platform_stop()

        with self._lock:
            self._hotkeys.clear()
            self._shortcut_strings.clear()

        failed: list[str] = []
        for hotkey_id, shortcut in shortcuts.items():
            cb = callback_factory(hotkey_id)
            try:
                modifiers, vk_code = self._parse_shortcut(shortcut)
            except ValueError as exc:
                logger.warning(
                    "Cannot reload hotkey '%s': invalid shortcut '%s': %s",
                    hotkey_id,
                    shortcut,
                    exc,
                )
                failed.append(hotkey_id)
                continue
            self._hotkeys[hotkey_id] = (modifiers, vk_code, cb)
            self._shortcut_strings[hotkey_id] = shortcut

        if was_running:
            self._running = True
            self._platform_start()

        if failed:
            logger.warning(
                "%d hotkey(s) skipped during reload: %s", len(failed), ", ".join(failed)
            )
        logger.info("Reloaded %d hotkeys from config", len(shortcuts) - len(failed))

    # ── Shortcut parsing ───────────────────────────────────────────

    def _parse_shortcut(self, shortcut: str) -> tuple[int, int]:
        """Parse a shortcut string into ``(modifiers, virtual_key_code)``.

        Examples::

            "Ctrl+`"           -> (MOD_CONTROL, VK_OEM_3)
            "Ctrl+1"           -> (MOD_CONTROL, ord('1'))
            "Ctrl+Shift+V"     -> (MOD_CONTROL | MOD_SHIFT, ord('V'))
            "Alt+Space"        -> (MOD_ALT, VK_SPACE)
            "Ctrl+Shift+Space" -> (MOD_CONTROL | MOD_SHIFT, VK_SPACE)
        """
        parts = [p.strip() for p in shortcut.split("+")]
        if len(parts) < 2:
            raise ValueError("Shortcut must have at least one modifier and one key")

        key = parts[-1]
        modifier_parts = parts[:-1]

        modifiers = 0
        for mod in modifier_parts:
            ml = mod.lower()
            if ml in ("ctrl", "control"):
                modifiers |= MOD_CONTROL
            elif ml == "alt":
                modifiers |= MOD_ALT
            elif ml == "shift":
                modifiers |= MOD_SHIFT
            elif ml in ("win", "cmd", "command", "windows", "meta"):
                modifiers |= MOD_WIN
            else:
                raise ValueError(f"Unknown modifier: '{mod}'")

        vk_code = self._get_vk_code(key)
        return modifiers, vk_code

    def _get_vk_code(self, key: str) -> int:
        """Convert a key name to a platform-specific virtual key code."""
        key_lower = key.strip().lower()
        # Map tilde to backtick (same physical key)
        if key_lower == "~":
            key_lower = "`"

        # Single ASCII character (A-Z, 0-9)
        if len(key) == 1 and key.isascii():
            char = key.upper()
            if self._platform == "macos":
                if char in _MAC_LETTER_VK:
                    return _MAC_LETTER_VK[char]
                # Digits 0-9 on macOS (kVK_ANSI_0 = 29, kVK_ANSI_1 = 18, ...)
                if "0" <= char <= "9":
                    if char == "0":
                        return 29
                    return 18 + (ord(char) - ord("1"))
            # Windows / Linux fallback: use ord()
            return ord(char)

        # Named keys -- look up in platform table
        if self._platform == "windows":
            if key_lower in _WIN_VK:
                return _WIN_VK[key_lower]
        elif self._platform == "macos":
            if key_lower in _MAC_VK:
                return _MAC_VK[key_lower]
        elif self._platform == "linux":
            # Linux uses pynput strings; VK code is not directly used.
            # Return a dummy value for consistency.
            return hash(key_lower) & 0xFFFF

        raise ValueError(f"Unknown key: '{key}'")

    # ── Main event loop (dispatched to platform) ───────────────────

    def _run_loop(self) -> None:
        """Entry point for the background listener thread."""
        if self._platform == "windows":
            self._run_windows()
        elif self._platform == "macos":
            self._run_macos()
        else:
            self._run_linux()

    # ── Per-platform register / unregister (for live updates) ──────

    def _platform_register_one(
        self, hotkey_id: str, modifiers: int, vk_code: int, shortcut: str
    ) -> None:
        if self._platform == "windows":
            self._win_register_one(hotkey_id, modifiers, vk_code)
        elif self._platform == "linux" and self._linux_listener is not None:
            # Reload all shortcuts since pynput needs a complete map
            with self._lock:
                shortcuts_snapshot = dict(self._shortcut_strings)
            self._linux_start_with_map(shortcuts_snapshot)

    def _platform_unregister_one(self, hotkey_id: str) -> None:
        if self._platform == "windows":
            self._win_unregister_one(hotkey_id)
        elif self._platform == "linux" and self._linux_listener is not None:
            with self._lock:
                shortcuts_snapshot = dict(self._shortcut_strings)
            self._linux_start_with_map(shortcuts_snapshot)

    def _platform_start(self) -> None:
        """Re-start platform listening after reload."""
        if self._platform == "linux":
            with self._lock:
                shortcuts_snapshot = dict(self._shortcut_strings)
            self._linux_start_with_map(shortcuts_snapshot)
        # Windows / macOS: handled by the run loop itself

    def _platform_stop(self) -> None:
        """Signal the platform run loop to exit."""
        if self._platform == "windows":
            self._win_stop()
        elif self._platform == "macos":
            self._mac_stop()
        else:
            self._linux_stop()

    # ── Fire callback (thread-safe) ────────────────────────────────

    def _fire(self, hotkey_id: str) -> None:
        """Look up and invoke a hotkey's callback.

        Runs in the hotkey listener thread.  Exceptions are caught and
        logged so a misbehaving callback does not crash the listener.
        """
        with self._lock:
            entry = self._hotkeys.get(hotkey_id)
        if entry is None:
            return
        try:
            entry[2]()  # callback
        except Exception:
            logger.exception("Hotkey callback '%s' raised an exception", hotkey_id)


# ══════════════════════════════════════════════════════════════════════
# Windows: RegisterHotKey + message-only window + GetMessage loop
# ══════════════════════════════════════════════════════════════════════

if _platform() == "windows":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    # Window message constants
    _WM_HOTKEY = 0x0312
    _WM_CLOSE = 0x0010
    _WM_QUIT = 0x0012
    _WM_DESTROY = 0x0002
    _HWND_MESSAGE = -3

    # Internal -> Win32 modifier translation
    _WIN_MOD_MAP: dict[int, int] = {
        MOD_CONTROL: 0x0002,
        MOD_ALT: 0x0001,
        MOD_SHIFT: 0x0004,
        MOD_WIN: 0x0008,
    }

    def _to_win_mods(internal_mods: int) -> int:
        result = 0
        for int_bit, win_bit in _WIN_MOD_MAP.items():
            if internal_mods & int_bit:
                result |= win_bit
        return result

    # Window procedure type (must be defined before _WNDCLASSW)
    _WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong,  # LRESULT
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    # WNDCLASSW is not in ctypes.wintypes — define it here
    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style",         wintypes.UINT),
            ("lpfnWndProc",   _WNDPROC),
            ("cbClsExtra",    ctypes.c_int),
            ("cbWndExtra",    ctypes.c_int),
            ("hInstance",     wintypes.HINSTANCE),
            ("hIcon",         wintypes.HICON),
            ("hCursor",       ctypes.c_void_p),
            ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName",  wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    # Module-level reference so the window procedure can reach the manager.
    _win_mgr: "HotkeyManager | None" = None

    @_WNDPROC
    def _win_wnd_proc(
        hwnd: wintypes.HWND,
        msg: wintypes.UINT,
        wparam: wintypes.WPARAM,
        lparam: wintypes.LPARAM,
    ) -> int:
        if msg == _WM_HOTKEY:
            mgr = _win_mgr
            if mgr is not None:
                hotkey_id = mgr._win_id_rev.get(wparam)
                if hotkey_id is not None:
                    mgr._fire(hotkey_id)
            return 0
        elif msg == _WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ── Windows-specific methods attached to HotkeyManager ─────────

    def _win_register_one(
        self: HotkeyManager, hotkey_id: str, modifiers: int, vk_code: int
    ) -> None:
        if self._win_hwnd is None:
            logger.warning(
                "Cannot register hotkey '%s': window not created yet", hotkey_id
            )
            return
        int_id = self._win_next_id
        self._win_next_id += 1
        self._win_id_map[hotkey_id] = int_id
        self._win_id_rev[int_id] = hotkey_id
        win_mods = _to_win_mods(modifiers)
        ok = _user32.RegisterHotKey(self._win_hwnd, int_id, win_mods, vk_code)
        if not ok:
            err = _kernel32.GetLastError()
            logger.warning(
                "RegisterHotKey failed for '%s' (id=%d, mods=0x%x, vk=0x%x, err=%d)",
                hotkey_id,
                int_id,
                win_mods,
                vk_code,
                err,
            )

    def _win_unregister_one(self: HotkeyManager, hotkey_id: str) -> None:
        int_id = self._win_id_map.pop(hotkey_id, None)
        self._win_id_rev.pop(int_id, None)
        if int_id is not None and self._win_hwnd is not None:
            _user32.UnregisterHotKey(self._win_hwnd, int_id)

    def _run_windows(self: HotkeyManager) -> None:
        global _win_mgr
        _win_mgr = self

        # Register window class with a unique name
        class_name = f"ClipSyncHotkey_{id(self):x}"
        wnd_class = _WNDCLASSW()
        wnd_class.lpfnWndProc = _win_wnd_proc
        wnd_class.hInstance = _kernel32.GetModuleHandleW(None)
        wnd_class.lpszClassName = class_name
        atom = _user32.RegisterClassW(ctypes.byref(wnd_class))
        if not atom:
            logger.error("RegisterClassW failed: err=%d", _kernel32.GetLastError())
            _win_mgr = None
            return
        self._win_class_atom = atom

        # Create a message-only window
        hwnd = _user32.CreateWindowExW(
            0,
            class_name,
            class_name,
            0,
            0,
            0,
            0,
            0,
            _HWND_MESSAGE,
            None,
            wnd_class.hInstance,
            None,
        )
        if not hwnd:
            logger.error("CreateWindowExW failed: err=%d", _kernel32.GetLastError())
            _user32.UnregisterClassW(class_name, wnd_class.hInstance)
            _win_mgr = None
            return
        self._win_hwnd = hwnd

        # Register all currently-stored hotkeys with the new window
        self._win_id_map.clear()
        self._win_id_rev.clear()
        self._win_next_id = 1
        with self._lock:
            for hotkey_id, (mods, vk, _cb) in list(self._hotkeys.items()):
                _win_register_one(self, hotkey_id, mods, vk)

        logger.debug("Windows message-only window created (hwnd=0x%x)", hwnd)

        # Message loop
        msg = wintypes.MSG()
        while self._running:
            # PeekMessage with PM_REMOVE (1), non-blocking (0)
            if _user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                if msg.message == _WM_QUIT:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.02)

        logger.debug("Windows hotkey message loop exited")

    def _win_stop(self: HotkeyManager) -> None:
        global _win_mgr
        _win_mgr = None

        hwnd = self._win_hwnd
        class_atom = self._win_class_atom

        # Unregister all hotkeys
        with self._lock:
            for int_id in list(self._win_id_map.values()):
                if hwnd is not None:
                    _user32.UnregisterHotKey(hwnd, int_id)
            self._win_id_map.clear()
            self._win_id_rev.clear()

        # Destroy the window -- posts WM_DESTROY which calls PostQuitMessage
        if hwnd is not None:
            _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            time.sleep(0.05)  # Give the message loop time to process WM_CLOSE
            try:
                _user32.DestroyWindow(hwnd)
            except Exception:
                pass
            self._win_hwnd = None

        # Unregister the window class
        if class_atom is not None:
            try:
                _user32.UnregisterClassW(
                    ctypes.c_wchar_p(f"ClipSyncHotkey_{id(self):x}"),
                    _kernel32.GetModuleHandleW(None),
                )
            except Exception:
                pass
            self._win_class_atom = None

    # Attach Windows methods to HotkeyManager
    HotkeyManager._win_register_one = _win_register_one  # type: ignore[attr-defined]
    HotkeyManager._win_unregister_one = _win_unregister_one  # type: ignore[attr-defined]
    HotkeyManager._run_windows = _run_windows  # type: ignore[attr-defined]
    HotkeyManager._win_stop = _win_stop  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════
# macOS: CGEvent tap via ctypes (CoreGraphics framework)
# ══════════════════════════════════════════════════════════════════════

elif _platform() == "macos":
    import ctypes
    import ctypes.util

    _cg_available = False

    _cg_path = ctypes.util.find_library("CoreGraphics")
    _cf_path = ctypes.util.find_library("CoreFoundation")

    if _cg_path is None or _cf_path is None:
        logger.error(
            "CoreGraphics/CoreFoundation not found -- global hotkeys disabled on macOS"
        )
    else:
        _cg = ctypes.cdll.LoadLibrary(_cg_path)
        _cf = ctypes.cdll.LoadLibrary(_cf_path)

        # ── Type aliases ──────────────────────────────────────────
        _CGEventRef = ctypes.c_void_p
        _CGEventTapProxy = ctypes.c_void_p
        _CGEventType = ctypes.c_uint32
        _CGEventMask = ctypes.c_uint64
        _CGEventFlags = ctypes.c_uint64
        _CFMachPortRef = ctypes.c_void_p
        _CFRunLoopSourceRef = ctypes.c_void_p
        _CFRunLoopRef = ctypes.c_void_p

        # ── Constants ─────────────────────────────────────────────
        _kCGEventKeyDown = 10  # NX_KEYDOWN
        _kCGEventTapOptionDefault = 0
        _kCGHeadInsertEventTap = 0
        _kCGSessionEventTap = 1
        _kCGKeyboardEventKeycode = 9

        # CGEvent flags for modifiers
        #   kCGEventFlagMaskControl   = 0x00000001
        #   kCGEventFlagMaskShift     = 0x00000002
        #   kCGEventFlagMaskCommand   = 0x00000010
        #   kCGEventFlagMaskAlternate = 0x00080000

        def _to_mac_cg_flags(internal_mods: int) -> int:
            """Translate internal modifier flags to CGEventFlags."""
            result = 0
            if internal_mods & MOD_CONTROL:
                result |= 0x0001  # kCGEventFlagMaskControl
            if internal_mods & MOD_ALT:
                result |= 0x00080000  # kCGEventFlagMaskAlternate
            if internal_mods & MOD_SHIFT:
                result |= 0x0002  # kCGEventFlagMaskShift
            if internal_mods & MOD_WIN:
                result |= 0x0010  # kCGEventFlagMaskCommand
            return result

        # ── CGEventTap callback type ──────────────────────────────
        _CGEventTapCallBack = ctypes.CFUNCTYPE(
            _CGEventRef,  # return: event to pass through (or NULL to consume)
            _CGEventTapProxy,
            _CGEventType,
            _CGEventRef,
            ctypes.c_void_p,  # userInfo
        )

        # ── CGEvent function signatures ───────────────────────────
        _cg.CGEventTapCreate.restype = _CFMachPortRef
        _cg.CGEventTapCreate.argtypes = [
            ctypes.c_int,  # tap (kCGSessionEventTap = 1)
            ctypes.c_int,  # place (kCGHeadInsertEventTap = 0)
            ctypes.c_int,  # options
            _CGEventMask,  # eventsOfInterest
            _CGEventTapCallBack,  # callback
            ctypes.c_void_p,  # userInfo
        ]

        _cg.CGEventTapEnable.restype = None
        _cg.CGEventTapEnable.argtypes = [_CFMachPortRef, ctypes.c_bool]

        _cg.CGEventGetIntegerValueField.restype = ctypes.c_longlong
        _cg.CGEventGetIntegerValueField.argtypes = [_CGEventRef, ctypes.c_uint32]

        _cg.CGEventGetFlags.restype = _CGEventFlags
        _cg.CGEventGetFlags.argtypes = [_CGEventRef]

        # ── CoreFoundation function signatures ────────────────────
        _cf.CFMachPortCreateRunLoopSource.restype = _CFRunLoopSourceRef
        _cf.CFMachPortCreateRunLoopSource.argtypes = [
            ctypes.c_void_p,  # allocator (NULL)
            _CFMachPortRef,
            ctypes.c_long,  # order
        ]

        _cf.CFRunLoopGetCurrent.restype = _CFRunLoopRef
        _cf.CFRunLoopGetCurrent.argtypes = []

        # kCFRunLoopDefaultMode is a CFString constant, NOT NULL.  Passing
        # NULL as the mode makes CoreFoundation call CFHash(NULL) and abort
        # with "*** CFHash() called with NULL ***".
        _kCFRunLoopDefaultMode = ctypes.c_void_p.in_dll(_cf, "kCFRunLoopDefaultMode")

        _cf.CFRunLoopAddSource.restype = None
        _cf.CFRunLoopAddSource.argtypes = [
            _CFRunLoopRef,
            _CFRunLoopSourceRef,
            ctypes.c_void_p,  # mode (CFStringRef, e.g. kCFRunLoopDefaultMode)
        ]

        _cf.CFRunLoopRun.restype = None
        _cf.CFRunLoopRun.argtypes = []

        _cf.CFRunLoopStop.restype = None
        _cf.CFRunLoopStop.argtypes = [_CFRunLoopRef]

        # ── Module-level reference for the callback ────────────────
        _mac_mgr: "HotkeyManager | None" = None

        @_CGEventTapCallBack
        def _mac_event_callback(
            proxy: _CGEventTapProxy,
            event_type: _CGEventType,
            event: _CGEventRef,
            user_info: ctypes.c_void_p,
        ) -> _CGEventRef:
            """CGEventTap callback -- fires on every key down event."""
            mgr = _mac_mgr
            if mgr is None:
                return event

            keycode = _cg.CGEventGetIntegerValueField(event, _kCGKeyboardEventKeycode)
            flags = _cg.CGEventGetFlags(event)

            with mgr._lock:
                hotkeys_snapshot = dict(mgr._hotkeys)

            for hotkey_id, (internal_mods, vk_code, _cb) in hotkeys_snapshot.items():
                expected_flags = _to_mac_cg_flags(internal_mods)
                # All required modifier flags must be present.
                # Extra flags (CapsLock, NumLock) are ignored.
                if (flags & expected_flags) == expected_flags and keycode == vk_code:
                    mgr._fire(hotkey_id)
                    break  # Only fire the first matching hotkey

            return event  # Pass event through (do NOT consume it)

        _cg_available = True

    # ── macOS methods attached to HotkeyManager ────────────────────

    def _run_macos(self: HotkeyManager) -> None:
        global _mac_mgr

        if not _cg_available:
            logger.error("CoreGraphics unavailable -- global hotkeys disabled on macOS")
            return

        _mac_mgr = self

        # Create event tap for key down events
        events_of_interest = _CGEventMask(1 << _kCGEventKeyDown)
        tap = _cg.CGEventTapCreate(
            _kCGSessionEventTap,  # tap location
            _kCGHeadInsertEventTap,  # placement
            _kCGEventTapOptionDefault,  # options
            events_of_interest,
            _mac_event_callback,
            None,
        )
        if not tap:
            logger.error(
                "CGEventTapCreate failed -- global hotkeys disabled. "
                "ClipSync may need Accessibility permissions in "
                "System Settings > Privacy & Security > Accessibility."
            )
            _mac_mgr = None
            return

        self._mac_tap = tap

        # Create run loop source from the tap
        source = _cf.CFMachPortCreateRunLoopSource(None, tap, 0)
        if not source:
            logger.error("CFMachPortCreateRunLoopSource failed")
            _mac_mgr = None
            return
        self._mac_source = source

        # Get the current thread's run loop and add the source
        rl = _cf.CFRunLoopGetCurrent()
        self._mac_run_loop = rl
        _cf.CFRunLoopAddSource(rl, source, _kCFRunLoopDefaultMode)

        # Enable the tap
        _cg.CGEventTapEnable(tap, True)

        logger.debug("macOS CGEvent tap started (tap=0x%x)", tap)

        # Run the loop -- blocks until CFRunLoopStop is called
        _cf.CFRunLoopRun()

        logger.debug("macOS CGEvent run loop exited")

    def _mac_stop(self: HotkeyManager) -> None:
        global _mac_mgr
        _mac_mgr = None

        tap = self._mac_tap
        if tap is not None:
            try:
                _cg.CGEventTapEnable(tap, False)
            except Exception:
                pass
            self._mac_tap = None

        rl = self._mac_run_loop
        if rl is not None:
            try:
                _cf.CFRunLoopStop(rl)
            except Exception:
                pass
            self._mac_run_loop = None

        self._mac_source = None

    # Attach macOS methods
    HotkeyManager._run_macos = _run_macos  # type: ignore[attr-defined]
    HotkeyManager._mac_stop = _mac_stop  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════
# Linux: pynput global hotkey listener
# ══════════════════════════════════════════════════════════════════════

else:  # linux
    _pynput_available = False
    try:
        from pynput.keyboard import GlobalHotKeys as _GlobalHotKeys  # type: ignore[import-untyped]

        _pynput_available = True
    except ImportError:
        logger.warning(
            "pynput not installed -- global hotkeys unavailable on Linux. "
            "Install with: pip install pynput"
        )

    def _to_pynput_shortcut(shortcut: str) -> str:
        """Convert a human-readable shortcut string to pynput format.

        Example: ``"Ctrl+Shift+V"`` -> ``"<ctrl>+<shift>+v"``
        """
        parts = [p.strip() for p in shortcut.split("+")]
        converted: list[str] = []
        for part in parts:
            pl = part.lower()
            if pl in ("ctrl", "control"):
                converted.append("<ctrl>")
            elif pl == "alt":
                converted.append("<alt>")
            elif pl == "shift":
                converted.append("<shift>")
            elif pl in ("win", "cmd", "command", "windows", "meta"):
                converted.append("<cmd>")
            elif pl in ("`", "~", "grave"):
                converted.append("<grave>")
            elif pl in ("space", "tab", "enter", "escape", "backspace", "delete"):
                converted.append(f"<{pl}>")
            elif pl.startswith("f") and len(pl) >= 2 and pl[1:].isdigit():
                converted.append(f"<{pl}>")
            elif len(part) == 1 and part.isascii():
                converted.append(part.lower())
            else:
                # Fallback for unknown named keys
                converted.append(f"<{pl}>")
        return "+".join(converted)

    def _linux_start_with_map(self: HotkeyManager, shortcuts: dict[str, str]) -> None:
        """Start or restart the pynput listener with a shortcut string map."""
        if not _pynput_available:
            return

        # Build pynput mapping: "<ctrl>+<shift>+v" -> callback
        pynput_map: dict[str, Callable] = {}
        with self._lock:
            for hotkey_id, (_mods, _vk, cb) in self._hotkeys.items():
                shortcut = shortcuts.get(hotkey_id)
                if shortcut is None:
                    continue
                try:
                    pynput_fmt = _to_pynput_shortcut(shortcut)
                except Exception:
                    logger.debug("Cannot convert shortcut '%s' to pynput format", shortcut)
                    continue
                pynput_map[pynput_fmt] = cb

        # Stop existing listener
        old = self._linux_listener
        if old is not None:
            try:
                old.stop()  # type: ignore[union-attr]
            except Exception:
                pass
            self._linux_listener = None

        if not pynput_map:
            logger.debug("No pynput hotkeys to register")
            return

        try:
            listener = _GlobalHotKeys(pynput_map)
            listener.start()
            self._linux_listener = listener
            logger.info("Linux pynput listener started with %d hotkey(s)", len(pynput_map))
        except Exception:
            logger.exception("Failed to start pynput listener")

    def _linux_stop(self: HotkeyManager) -> None:
        listener = self._linux_listener
        if listener is not None:
            try:
                listener.stop()  # type: ignore[union-attr]
            except Exception:
                pass
            self._linux_listener = None

    def _run_linux(self: HotkeyManager) -> None:
        """Run the Linux hotkey listener thread."""
        if not _pynput_available:
            logger.error("pynput unavailable -- global hotkeys disabled on Linux")
            return

        # Start the pynput listener with currently registered shortcuts
        with self._lock:
            shortcuts_snapshot = dict(self._shortcut_strings)
        _linux_start_with_map(self, shortcuts_snapshot)

        # Keep the thread alive while the manager is running.
        # pynput's listener runs in its own thread, so we just sleep.
        while self._running:
            time.sleep(0.5)

        # Ensure listener is stopped
        _linux_stop(self)

    # Attach Linux methods
    HotkeyManager._run_linux = _run_linux  # type: ignore[attr-defined]
    HotkeyManager._linux_start_with_map = _linux_start_with_map  # type: ignore[attr-defined]
    HotkeyManager._linux_stop = _linux_stop  # type: ignore[attr-defined]
