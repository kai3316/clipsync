"""System tray UI for ClipSync.

Cross-platform system tray icon with menu:
- Device name and status
- Enable/disable sync
- Settings...
- Connected peers list
- About / Quit

Uses pystray with Pillow for icon rendering.
"""

import ctypes
import logging
import sys
from collections.abc import Callable

import pystray
from PIL import Image, ImageDraw

from internal.i18n import T
from internal.platform.notify import notification_mgr

logger = logging.getLogger(__name__)


def _create_icon_image(size: int = 32) -> Image.Image:
    """Create a simple clipboard icon."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = size // 8
    padding = size // 5

    # Clipboard body
    x0 = margin
    y0 = margin + size // 6
    x1 = size - margin
    y1 = size - margin
    draw.rounded_rectangle([x0, y0, x1, y1], radius=size // 8, fill=(80, 140, 220))

    # Clipboard top bar
    bar_width = size // 3
    bar_x0 = (size - bar_width) // 2
    bar_y0 = margin
    bar_x1 = bar_x0 + bar_width
    bar_y1 = y0 + padding // 2
    draw.rounded_rectangle(
        [bar_x0, bar_y0, bar_x1, bar_y1],
        radius=size // 12, fill=(60, 110, 180),
    )

    # Paper lines
    line_color = (220, 230, 245)
    line_margin = size // 4
    line_spacing = size // 8
    for i in range(3):
        ly = y0 + padding + i * line_spacing
        draw.line(
            [x0 + line_margin, ly, x1 - line_margin, ly],
            fill=line_color, width=max(1, size // 20),
        )

    return img


class SystrayApp:
    """System tray application wrapper."""

    def __init__(
        self,
        device_name: str,
        on_enable_toggle: Callable | None = None,
        on_open_dashboard: Callable | None = None,
        on_open_settings: Callable | None = None,
        on_export_logs: Callable | None = None,
        on_quit: Callable | None = None,
        on_show_web_qr: Callable | None = None,
        on_send_url: Callable | None = None,
        on_check_update: Callable | None = None,
        on_about: Callable | None = None,
    ):
        self._device_name = device_name
        self._on_enable_toggle = on_enable_toggle
        self._on_open_dashboard = on_open_dashboard
        self._on_open_settings = on_open_settings
        self._on_export_logs = on_export_logs
        self._on_quit_cb = on_quit
        self._on_show_web_qr = on_show_web_qr
        self._on_send_url = on_send_url
        self._on_check_update = on_check_update
        self._on_about_cb = on_about
        self._syncing = True
        self._web_enabled = False
        self._tray = None
        self._icon_image = _create_icon_image()
        self._peers: list[str] = []

        # Custom message for thread-safe menu updates on Windows.
        # _update_menu() calls DestroyMenu on the current HMENU, which
        # conflicts with TrackPopupMenuEx if the context menu is open.
        # Posting WM_UPDATE_MENU guarantees _update_menu() runs on the
        # tray thread after any open menu has closed.
        if sys.platform == "win32":
            self._WM_UPDATE_MENU = 0x8000 + 0x100  # WM_APP + 256

    def set_syncing(self, enabled: bool):
        self._syncing = enabled
        if self._tray is None:
            return
        if sys.platform == "win32" and getattr(self, "_WM_UPDATE_MENU", None):
            hwnd = getattr(self._tray, "_hwnd", None)
            if hwnd:
                ctypes.windll.user32.PostMessageW(
                    hwnd, self._WM_UPDATE_MENU, 0, 0)
                return
        # The sync checkbox state is dynamic (checked=lambda), so a plain
        # refresh is enough — no menu rebuild needed. update_menu() marshals
        # the native rebuild onto the tray thread on GTK/AppIndicator.
        self._tray.update_menu()

    def set_web_enabled(self, enabled: bool):
        self._web_enabled = enabled
        self._schedule_menu_rebuild()

    def set_peers(self, peers: list[str]):
        self._peers = peers
        self._schedule_menu_rebuild()

    def _schedule_menu_rebuild(self):
        """Rebuild the tray menu on the tray thread (thread-safe).

        The menu descriptor bakes in the peer list / web-QR item, so the
        native menu must be rebuilt. pystray's ``menu`` setter does that, but
        mutating ``self._tray.menu`` from a non-tray thread races the
        GTK/AppIndicator event loop and can crash. On Windows we post
        WM_UPDATE_MENU to the tray window; elsewhere the rebuild is applied
        via an idle callback on the tray thread (GTK family).
        """
        if self._tray is None:
            return
        if sys.platform == "win32" and getattr(self, "_WM_UPDATE_MENU", None):
            hwnd = getattr(self._tray, "_hwnd", None)
            if hwnd:
                ctypes.windll.user32.PostMessageW(
                    hwnd, self._WM_UPDATE_MENU, 0, 0)
                return
        menu = self._build_full_menu()
        try:
            from gi.repository import GObject
        except Exception:
            # Non-GTK backend: fall back to pystray's setter, which marshals
            # the rebuild onto the tray thread where supported.
            self._tray.menu = menu
            return

        def _apply():
            self._tray.menu = menu
            return False  # one-shot idle callback

        GObject.idle_add(_apply)

    def _apply_pending_menu(self):
        """Apply the latest peer menu on the tray thread (WM_UPDATE_MENU handler)."""
        if self._tray:
            self._tray.menu = self._build_full_menu()

    def _build_peer_menu(self) -> pystray.Menu:
        """Build the peer submenu with static items."""
        if not self._peers:
            return pystray.Menu(
                pystray.MenuItem(T("tray.no_devices"), None, enabled=False),
            )
        items = [
            pystray.MenuItem(peer, None, enabled=False)
            for peer in self._peers
        ]
        return pystray.Menu(*items)

    def _build_full_menu(self) -> pystray.Menu:
        """Build the complete tray menu."""
        menu_items = [
            pystray.MenuItem("📋  " + T("ui.app_name"), None, enabled=False),
            pystray.MenuItem(
                T("tray.device", name=self._device_name), None, enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            # Sync toggle (checkbox)
            pystray.MenuItem(
                T("tray.sync"),
                self._on_toggle_sync,
                checked=lambda item: self._syncing,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "🖥  " + T("tray.show_dashboard"), self._on_open_dashboard_click,
            ),
            pystray.MenuItem(
                "📤  " + T("tray.send_url"), self._on_send_url_click,
            ),
        ]
        if self._web_enabled:
            menu_items.append(
                pystray.MenuItem("📱  " + T("tray.show_web_qr"), self._on_show_web_qr_click),
            )
        menu_items.append(
            pystray.MenuItem(
                "📶  " + T("tray.connected_devices"),
                self._build_peer_menu(),
            ),
        )
        menu_items.append(pystray.Menu.SEPARATOR)
        menu_items.append(
            pystray.MenuItem("⚙  " + T("tray.settings"), self._on_open_settings_click),
        )
        menu_items.extend([
            pystray.MenuItem("📝  " + T("tray.export_logs"), self._on_export_logs_click),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("🔍  " + T("tray.check_update"), self._on_check_update_click),
            pystray.MenuItem("ℹ️  " + T("tray.about"), self._on_about),
            pystray.MenuItem("⏻  " + T("tray.quit"), self._on_quit),
        ])
        return pystray.Menu(*menu_items)

    def run(self):
        """Run the system tray. Blocks until quit."""
        menu = self._build_full_menu()

        logger.info("Starting system tray")
        self._tray = pystray.Icon(
            "clipsync",
            self._icon_image,
            T("ui.app_name"),
            menu,
        )

        # Left-click opens dashboard on Windows/macOS; on Linux the menu is
        # always shown — AppIndicator ignores __call__ entirely, and on GTK
        # the dashboard may fail to restore after withdraw (CTkToplevel issue).
        if sys.platform != "linux":
            _orig_call = self._tray.__call__
            def _left_click():
                if self._on_open_dashboard:
                    self._on_open_dashboard()
                else:
                    _orig_call()
            self._tray.__call__ = _left_click

        # Register custom message handler so set_peers() can safely
        # trigger menu updates from the peer updater thread.
        if sys.platform == "win32" and getattr(self, "_WM_UPDATE_MENU", None):
            self._tray._message_handlers[self._WM_UPDATE_MENU] = (
                lambda w, l: self._apply_pending_menu() or 0
            )

        notification_mgr.set_tray(self._tray)
        self._tray.run()

    def stop(self):
        if self._tray:
            self._tray.stop()

    def _on_toggle_sync(self, icon, item):
        self._syncing = not self._syncing
        if self._on_enable_toggle:
            self._on_enable_toggle(self._syncing)
        logger.info("Sync %s via tray", "enabled" if self._syncing else "paused")
        # Do NOT call update_menu() here: this callback runs while the context
        # menu is open (TrackPopupMenuEx on Windows), and update_menu() tears
        # down the HMENU being tracked.  The checkbox is live-read via
        # checked=lambda, so the new state renders without a rebuild.

    def _on_open_dashboard_click(self, icon, item):
        if self._on_open_dashboard:
            self._on_open_dashboard()

    def _on_open_settings_click(self, icon, item):
        if self._on_open_settings:
            self._on_open_settings()

    def _on_export_logs_click(self, icon, item):
        if self._on_export_logs:
            self._on_export_logs()

    def _on_show_web_qr_click(self, icon, item):
        if self._on_show_web_qr:
            self._on_show_web_qr()

    def _on_send_url_click(self, icon, item):
        if self._on_send_url:
            self._on_send_url()

    def _on_check_update_click(self, icon, item):
        if self._on_check_update:
            self._on_check_update()

    def _on_about(self, icon, item):
        if self._on_about_cb:
            self._on_about_cb()
            return
        if self._tray:
            self._tray.notify(
                T("tray.about_message"),
                title=T("tray.about_title"),
            )

    def _on_quit(self, icon, item):
        if self._on_quit_cb:
            self._on_quit_cb()
        self.stop()
