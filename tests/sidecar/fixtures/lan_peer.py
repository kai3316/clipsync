"""Real IPC/TLS test process with a private, in-memory OS clipboard substitute."""

import logging
import sys
import time
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from internal.adapters.sidecar.rpc import RpcServer
from internal.application.bootstrap import SidecarApplication
from internal.clipboard.clipboard import ClipboardMonitor
from internal.clipboard.format import ClipboardContent
from internal.infrastructure.runtime.lan import LanRuntime


class Monitor(ClipboardMonitor):
    def start(self, callback):
        self.callback = callback
        self.running = True

    def stop(self):
        self.running = False

    def changed(self):
        if self.running and time.time() >= self.suppress_until:
            self.callback()


class Clipboard:
    def __init__(self, monitor):
        self.monitor = monitor
        self.content = ClipboardContent()

    def read(self):
        return deepcopy(self.content)

    def write(self, content):
        self.content = deepcopy(content)
        self.monitor.changed()
        return True


class Discovery:
    """Saved loopback endpoints substitute for LAN multicast in this test."""

    def set_callbacks(self, found, lost):
        self.found, self.lost = found, lost

    def start(self):
        pass

    def stop(self):
        pass

    def _wake_recovery(self):
        pass


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.ERROR)
    monitor = Monitor()
    clipboard = Clipboard(monitor)
    app = SidecarApplication(
        clipboard_writer_factory=lambda: clipboard,
        runtime_factory=lambda *args: LanRuntime(
            *args, monitor=monitor, reader=clipboard, writer=clipboard, discovery=Discovery()
        ),
    )
    try:
        app.lifecycle.start()
        return RpcServer(app, sys.stdin.buffer, sys.stdout.buffer).serve()
    finally:
        if not app.lifecycle.stop():
            raise RuntimeError("Test peer did not release its runtime ownership")


if __name__ == "__main__":
    raise SystemExit(main())
