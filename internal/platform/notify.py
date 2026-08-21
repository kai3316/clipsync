import logging
import queue
import threading

logger = logging.getLogger(__name__)


class NotificationManager:
    def __init__(self):
        self._tray_icon = None  # set later via set_tray()
        self._enabled = True
        self._pipe = None  # multiprocessing pipe (macOS subprocess)
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
        """
        self._pipe = pipe
        self._send_queue = queue.Queue()
        self._send_thread = threading.Thread(
            target=self._pipe_sender, daemon=True,
            name="notify-pipe-sender",
        )
        self._send_thread.start()

    def _pipe_sender(self):
        """Background thread: drain _send_queue and forward to the pipe."""
        while True:
            try:
                title, message = self._send_queue.get()
                if title is None:  # sentinel to stop the thread
                    break
                try:
                    self._pipe.send(("show_notification", title, message))
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

    @staticmethod
    def _fallback_notify(title: str, message: str):
        """Fallback desktop notification via system command (Linux)."""
        import subprocess
        import sys
        if sys.platform == "linux":
            try:
                subprocess.run(
                    ["notify-send", title, message],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

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
                for cmd in (["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                            ["canberra-gtk-play", "-i", "complete"],
                            ["aplay", "/usr/share/sounds/alsa/Front_Center.wav"]):
                    try:
                        subprocess.run(cmd, capture_output=True, timeout=5)
                        break
                    except Exception:
                        continue
        except Exception:
            logger.debug("play_sound failed", exc_info=True)


notification_mgr = NotificationManager()
