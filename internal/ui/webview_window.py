"""WebView window wrapper — opens the web UI in a native app-like window.

Uses the system's default browser in "app mode" (no browser chrome) via
subprocess.  Zero external dependencies — relies only on the standard library.
"""

import logging
import os
import platform
import shutil
import subprocess
import threading

logger = logging.getLogger(__name__)

# Ordered list of browsers we prefer for app-mode windows.
# Each entry: (executable_name, [[arg_template, ...], platform_check]).
# Each arg_template has {url}, {width}, {height} substituted.
# On macOS, the first arg is the app name, args are passed separately to open -a.
_BROWSERS = [
    # ── Windows ───────────────────────────────────────────────────────
    ("msedge",    ["--app={url}", "--window-size={width},{height}"], ["Windows"]),
    ("chrome",    ["--app={url}", "--window-size={width},{height}"], ["Windows"]),
    ("chromium",  ["--app={url}", "--window-size={width},{height}"], ["Windows"]),
    ("brave",     ["--app={url}", "--window-size={width},{height}"], ["Windows"]),
    ("firefox",   ["--new-window", "{url}", "--width={width}", "--height={height}"], ["Windows"]),
    # ── macOS ─────────────────────────────────────────────────────────
    # Chrome-based browsers are launched directly via their bundle binary
    # with `--app` (see _launch_browser): `open -a ... --args --app={url}`
    # drops the `--args` when the browser is already running and merely
    # activates the existing window, so the app URL would never load.
    # Safari has no `--app` mode; it opens the URL as a normal document.
    ("Google Chrome", ["--app={url}"], ["Darwin"]),
    ("Microsoft Edge", ["--app={url}"], ["Darwin"]),
    ("Chromium", ["--app={url}"], ["Darwin"]),
    ("Brave Browser", ["--app={url}"], ["Darwin"]),
    ("Safari", ["{url}"], ["Darwin"]),  # Safari doesn't support --app, opens normally
    # ── Linux ─────────────────────────────────────────────────────────
    ("google-chrome",     ["--app={url}", "--window-size={width},{height}"], ["Linux"]),
    ("google-chrome-stable", ["--app={url}", "--window-size={width},{height}"], ["Linux"]),
    ("chromium-browser",  ["--app={url}", "--window-size={width},{height}"], ["Linux"]),
    ("chromium",          ["--app={url}", "--window-size={width},{height}"], ["Linux"]),
    ("microsoft-edge",    ["--app={url}", "--window-size={width},{height}"], ["Linux"]),
    ("brave-browser",     ["--app={url}", "--window-size={width},{height}"], ["Linux"]),
    ("firefox",           ["--new-window", "{url}", "--width={width}", "--height={height}"], ["Linux"]),
]


def _find_browser() -> tuple[str, list[str]] | None:
    """Find an installed browser that supports app mode.

    Returns (executable_path_or_name, [arg_template, ...]) or None.
    """
    system = platform.system()
    for browser, args_tmpl, platforms in _BROWSERS:
        if system not in platforms:
            continue
        if system == "Darwin":
            # macOS: check if the .app bundle exists
            app_path = f"/Applications/{browser}.app"
            if os.path.isdir(app_path):
                return browser, args_tmpl
        else:
            # Windows / Linux: check if the executable is on PATH
            if shutil.which(browser) is not None:
                return browser, args_tmpl
            # Windows: also check common install paths not on PATH
            if system == "Windows":
                _win_paths = _WINDOWS_BROWSER_PATHS.get(browser)
                if _win_paths:
                    for p in _win_paths:
                        if os.path.isfile(p):
                            return p, args_tmpl
    return None


# Windows install paths for browsers not typically on PATH
_WINDOWS_BROWSER_PATHS: dict[str, list[str]] = {
    "msedge": [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ],
    "chrome": [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ],
    "brave": [
        os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ],
    "firefox": [
        os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"),
    ],
}


def find_available_browser() -> str | None:
    """Return the name of the first available browser, or None."""
    result = _find_browser()
    if result is None:
        return None
    return result[0]


class WebViewWindow:
    """Open a URL in a standalone app-mode browser window.

    Launches the system browser with no navigation bar, tabs, or other
    browser chrome — it looks and feels like a native application window.

    Usage::

        win = WebViewWindow("http://127.0.0.1:9580/index.html?token=abc")
        win.start()
        # ... later ...
        win.stop()
    """

    def __init__(
        self,
        url: str,
        title: str = "ClipSync",
        width: int = 900,
        height: int = 700,
    ):
        if not url:
            raise ValueError("url must not be empty")
        self._url = url
        self._title = title
        self._width = width
        self._height = height
        self._process: subprocess.Popen | None = None
        self._monitor_thread: threading.Thread | None = None
        self._browser_name: str | None = None
        self._browser_args: list[str] | None = None

    # ── Public API ──────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the web UI in a browser app-mode window."""
        if self.is_running():
            logger.debug("WebViewWindow: already running, not starting again")
            return

        result = _find_browser()
        if result is None:
            logger.warning("WebViewWindow: no supported browser found, "
                           "falling back to default browser")
            self._start_fallback()
            return

        self._browser_name, self._browser_args = result
        self._launch_browser()

    def stop(self) -> None:
        """Close the browser window if it was launched by us."""
        self._stop_monitor()
        proc = self._process
        self._process = None
        if proc is not None:
            logger.info("WebViewWindow: terminating browser process (PID %d)",
                        proc.pid)
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            except Exception:
                logger.debug("WebViewWindow: failed to terminate browser process",
                             exc_info=True)

    def is_running(self) -> bool:
        """Return True if the browser process is still alive."""
        proc = self._process
        if proc is None:
            return False
        return proc.poll() is None

    # ── Browser detection ───────────────────────────────────────────

    @staticmethod
    def find_browser() -> str | None:
        """Return the name of the first available browser, or None."""
        return find_available_browser()

    # ── Internal helpers ────────────────────────────────────────────

    def _launch_browser(self) -> None:
        """Launch the detected browser with app-mode flags."""
        system = platform.system()
        width = self._width
        height = self._height
        url = self._url
        browser = self._browser_name
        args_tmpl = self._browser_args

        # Substitute placeholders in arg templates
        args = [
            a.format(url=url, width=width, height=height)
            for a in args_tmpl
        ]

        if system == "Darwin":
            if browser == "Safari":
                # Safari can't run --app; open the URL as a normal document.
                full_cmd = ["open", "-a", browser, url]
            else:
                # Chrome-based browsers: invoke the bundle binary directly so
                # `--app={url}` opens a standalone window that loads the URL
                # even when the browser is already running. `open -a ... --args`
                # is ignored for a running instance.
                binary = os.path.join(
                    "/Applications", browser + ".app", "Contents", "MacOS", browser
                )
                if os.path.isfile(binary):
                    full_cmd = [binary] + args
                else:
                    full_cmd = ["open", "-a", browser] + args
            logger.info("WebViewWindow: %s", " ".join(full_cmd))
            self._process = subprocess.Popen(
                full_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # Windows / Linux: browser executable + app-mode flags
            full_cmd = [browser] + args
            logger.info("WebViewWindow: %s", " ".join(full_cmd))
            self._process = subprocess.Popen(
                full_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        # Start a monitor thread that clears self._process when it exits
        self._start_monitor()

    def _start_fallback(self) -> None:
        """Fallback: open URL in the default system browser."""
        import webbrowser
        logger.info("WebViewWindow: opening in default browser (fallback)")
        webbrowser.open_new(self._url)

    def _start_monitor(self) -> None:
        """Start a background thread that watches the browser process."""
        proc = self._process
        if proc is None:
            return

        def _watch():
            try:
                proc.wait()
            except Exception:
                pass
            if self._process is proc:
                self._process = None
                logger.debug("WebViewWindow: browser process exited")

        self._monitor_thread = threading.Thread(
            target=_watch, daemon=True, name="webview-monitor",
        )
        self._monitor_thread.start()

    def _stop_monitor(self) -> None:
        """Signal the monitor thread to stop (it exits when the process dies)."""
        self._monitor_thread = None


# ── Convenience function ─────────────────────────────────────────────


def open_webview(url: str, title: str = "ClipSync",
                 width: int = 900, height: int = 700) -> WebViewWindow:
    """Create and start a WebViewWindow, returning it for lifecycle control.

    The caller is responsible for keeping a reference and calling ``stop()``
    when done.
    """
    win = WebViewWindow(url=url, title=title, width=width, height=height)
    win.start()
    return win
