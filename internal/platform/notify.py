import logging
import queue
import threading

logger = logging.getLogger(__name__)


class NotificationManager:
    def __init__(self):
        self._tray_icon = None  # set later via set_tray()
        self._enabled = True
        self._pipe = None  # multiprocessing pipe (macOS subprocess)
        self._pipe_lock = threading.Lock()  # guards _pipe.send (multi-writer)
        self._send_queue: queue.Queue | None = None
        self._send_thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    def set_tray(self, tray_icon):
        """Set the pystray Icon reference for notifications."""
        self._tray_icon = tray_icon

    def set_pipe(self, pipe):
        """Set a multiprocessing pipe for macOS subprocess notifications.

        Starts a background daemon thread that drains a queue and sends
        notifications through the pipe.  This prevents ``pipe.send()``
        from ever blocking a calling thread (e.g. the main thread) when
        the pipe buffer is full.

        Calling this again (e.g. after a tray-subprocess restart) stops the
        previous sender thread first so it doesn't leak and compete for the
        same pipe.
        """
        self._stop_sender()
        self._pipe = pipe
        self._send_queue = queue.Queue()
        self._send_thread = threading.Thread(
            target=self._pipe_sender, daemon=True,
            name="notify-pipe-sender",
        )
        self._send_thread.start()

    def _stop_sender(self):
        """Stop the current pipe-sender thread, if any (idempotent)."""
        thread = self._send_thread
        q = self._send_queue
        self._send_thread = None
        self._send_queue = None
        if thread is not None and q is not None and thread.is_alive():
            try:
                q.put((None, None))  # sentinel stops the loop
            except Exception:
                pass

    def send_pipe(self, msg):
        """Send a picklable message over the macOS tray pipe, thread-safely.

        Multiple writers share the pipe: the notification sender thread and
        (for tray state-sync) the main thread both call ``Connection.send``,
        which is not internally synchronized — interleaved writes desync the
        receiver.  A lock around each ``send`` keeps frames intact.
        """
        with self._pipe_lock:
            self._pipe.send(msg)

    def _pipe_sender(self):
        """Background thread: drain _send_queue and forward to the pipe.

        Captures the queue and pipe at start so a later ``set_pipe`` (which
        swaps in a new queue/pipe and stops this thread via a sentinel) never
        causes this thread to drain the new queue too.
        """
        q = self._send_queue
        while True:
            try:
                title, message = q.get()
                if title is None:  # sentinel to stop the thread
                    break
                try:
                    self.send_pipe(("show_notification", title, message))
                except Exception:
                    logger.debug("Notification via pipe failed", exc_info=True)
            except Exception:
                break

    def show(self, title: str, message: str):
        """Show a desktop notification if tray is available.

        On macOS the notification is queued for a background thread so
        ``pipe.send()`` never blocks the calling thread.
        """
        if not self._enabled:
            return
        if self._send_queue is not None:
            try:
                self._send_queue.put_nowait((title, message))
            except queue.Full:
                pass  # drop notification if the queue is full (shouldn't happen)
            return
        if self._tray_icon:
            try:
                self._tray_icon.notify(message, title=title)
            except NotImplementedError:
                logger.debug("pystray notify not implemented for this backend")
                self._fallback_notify(title, message)
            except Exception:
                logger.debug("Desktop notification failed", exc_info=True)

    def is_available(self) -> bool:
        """Return True if this platform can deliver desktop notifications.

        macOS/Windows deliver through the pystray icon (always available once
        the tray runs); Linux requires the ``notify-send`` command.  Callers
        use this to surface a one-time warning when the user enabled
        notifications but the OS cannot actually show them.
        """
        import sys

        if sys.platform == "linux":
            import shutil

            return shutil.which("notify-send") is not None
        return True

    @staticmethod
    def _fallback_notify(title: str, message: str):
        """Fallback desktop notification via system command (Linux).

        Logs at WARNING when ``notify-send`` is missing or fails so a silent
        notification failure is visible in the logs instead of disappearing.
        """
        import shutil
        import subprocess
        import sys
        if sys.platform == "linux":
            if shutil.which("notify-send") is None:
                logger.warning(
                    "notify-send is not installed; desktop notifications unavailable"
                )
                return
            try:
                subprocess.run(
                    ["notify-send", title, message],
                    capture_output=True, timeout=5,
                )
            except Exception as exc:
                logger.warning("notify-send failed: %s", exc)

    @staticmethod
    def play_sound():
        """Play a short system notification sound (per platform).

        The caller gates this on the user's ``sound_enabled`` setting. This
        only ever fails silently — a missing sound tool must not break the
        notification flow.
        """
        import subprocess
        import sys
        try:
            if sys.platform == "win32":
                import winsound
                # SystemNotification is the Windows "you got something" sound;
                # async so it doesn't block the caller.
                winsound.PlaySound("SystemNotification",
                                   winsound.SND_ALIAS | winsound.SND_ASYNC)
            elif sys.platform == "darwin":
                subprocess.run(
                    ["afplay", "/System/Library/Sounds/Ping.aiff"],
                    capture_output=True, timeout=5)
            else:
                # Linux: try the freedesktop complete sound via a few tools.
                # Only stop at the first tool that actually succeeds — a tool
                # may exist yet fail (missing sound file, no audio server), in
                # which case the next one should get a chance.
                for cmd in (["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                            ["canberra-gtk-play", "-i", "complete"],
                            ["aplay", "/usr/share/sounds/alsa/Front_Center.wav"]):
                    try:
                        r = subprocess.run(cmd, capture_output=True, timeout=5)
                        if r.returncode == 0:
                            break
                    except Exception:
                        continue
        except Exception:
            logger.debug("play_sound failed", exc_info=True)


notification_mgr = NotificationManager()
