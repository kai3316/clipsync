"""Verify a real packaged sidecar in an isolated temporary data directory."""

import argparse
import json
import os
import queue
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener


class SmokeError(RuntimeError):
    """Only fixed, credential-free messages may cross this boundary."""


class SidecarProcess:
    def __init__(self, command, directory):
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "CLIPSYNC_CONFIG_DIR": str(directory)},
        )
        self.frames = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.counter = 0

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    frame = json.loads(line)
                except (ValueError, UnicodeError):
                    self.frames.put(None)
                    return
                self.frames.put(frame)
        finally:
            self.frames.put(None)

    def receive(self, deadline):
        try:
            frame = self.frames.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise SmokeError("Sidecar response timed out") from None
        if not isinstance(frame, dict) or frame.get("type") == "fatal":
            raise SmokeError("Sidecar stream failed")
        return frame

    def ready(self):
        frame = self.receive(time.monotonic() + 40)
        if frame.get("type") != "ready" or frame.get("protocol") != 1:
            raise SmokeError("Unexpected sidecar handshake")

    def call(self, method, params=None):
        self.counter += 1
        request_id = str(self.counter)
        request = {"type": "request", "id": request_id, "method": method,
                   "params": params or {}}
        try:
            self.process.stdin.write((json.dumps(request) + "\n").encode())
            self.process.stdin.flush()
        except OSError:
            raise SmokeError("Sidecar input closed") from None
        deadline = time.monotonic() + 30
        while True:
            frame = self.receive(deadline)
            if frame.get("type") in ("event", "resync"):
                continue
            if (frame.get("type") != "response" or frame.get("id") != request_id
                    or frame.get("ok") is not True):
                raise SmokeError("Sidecar RPC failed")
            return frame["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()


def temporary_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def require(condition, message):
    if not condition:
        raise SmokeError(message)


def verify_http(status):
    port = status.get("actual_port")
    token = status.get("token")
    require(status.get("running") is True and isinstance(port, int)
            and isinstance(token, str) and bool(token), "Companion is not running")
    base = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))
    try:
        try:
            with opener.open(base + "/mobile.html", timeout=5):
                raise SmokeError("Unauthenticated Companion access was allowed")
        except HTTPError as error:
            require(error.code in (401, 403), "Unexpected authentication response")
            error.close()
        for path, marker in (
            ("/mobile.html", b"<html"),
            ("/js/format.js", None),
            ("/manifest.json?page=mobile", b"start_url"),
            ("/icon-192.png", b"\x89PNG"),
            ("/api/history", b"total"),
        ):
            separator = "&" if "?" in path else "?"
            with opener.open(
                base + path + separator + "token=" + quote(token, safe=""), timeout=5,
            ) as response:
                body = response.read()
                require(response.status == 200 and bool(body), "Companion resource failed")
                if marker:
                    require(marker in body, "Unexpected Companion resource content")
    except Exception:
        raise SmokeError("Companion HTTP verification failed") from None


def require_port_closed(port):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(.05)
    raise SmokeError("Companion listener survived shutdown")


def verify_companion_runtime(command, directory):
    # Disable clipboard synchronization in this disposable profile.
    config = {"encryption_enabled": False, "sync_enabled": False,
              "web_enabled": False, "port": temporary_port()}
    (Path(directory) / "config.json").write_text(json.dumps(config), encoding="utf-8")
    port = temporary_port()
    previous_token = None
    for iteration in range(2):
        child = SidecarProcess(command, directory)
        try:
            child.ready()
            require(child.call("app.status")["health"] == "ready", "Sidecar not ready")
            status = child.call("companion.status")
            if iteration:
                require(status["token"] == previous_token, "Companion credentials not preserved")
                verify_http(status)
            else:
                require(not status["running"], "Companion unexpectedly started")
            status = child.call("companion.configure", {"enabled": True, "port": port})
            verify_http(status)
            previous_token = status["token"]
            stopped = child.call("companion.configure", {"enabled": False})
            require(not stopped["running"] and stopped["token"] is None
                    and stopped["access_url"] is None, "Companion did not stop")
            require_port_closed(port)
            # Exit while enabled, then verify persisted startup in the next process.
            status = child.call("companion.configure", {"enabled": True, "port": port})
            verify_http(status)
            require(child.call("app.shutdown")["accepted"] is True, "Shutdown rejected")
            require(child.process.wait(timeout=30) == 0, "Sidecar shutdown failed")
            require_port_closed(port)
        except subprocess.TimeoutExpired:
            raise SmokeError("Sidecar shutdown timed out") from None
        finally:
            child.close()
        print(f"Companion authenticated HTTP/stop/exit/restart {iteration + 1}: PASS")


def verify_companion_resources(executable: str) -> None:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(executable)
    entries = {name.replace("\\", "/"): name for name in archive.toc}
    static = Path(__file__).resolve().parents[1] / "internal" / "web" / "static"
    files = [path for path in static.rglob("*") if path.is_file()]
    assert files, "Companion source resources are missing"
    for path in files:
        name = "internal/web/static/" + path.relative_to(static).as_posix()
        assert name in entries, f"Packaged Companion resource missing: {name}"
        assert archive.extract(entries[name]) == path.read_bytes(), (
            f"Packaged Companion resource is stale: {name}"
        )
    print(f"Packaged Companion resources ({len(files)} files): PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable")
    args = parser.parse_args()
    verify_companion_resources(os.path.abspath(args.executable))
    with tempfile.TemporaryDirectory(prefix="clipsync-sidecar-smoke-") as directory:
        for iteration in range(2):
            requests = [
                {"type": "request", "id": "status", "method": "app.status", "params": {}},
                {"type": "request", "id": "history", "method": "history.list", "params": {}},
                {"type": "request", "id": "stop", "method": "app.shutdown", "params": {}},
            ]
            result = subprocess.run(
                [os.path.abspath(args.executable), "--history-only"],
                input="".join(json.dumps(request) + "\n" for request in requests).encode(),
                capture_output=True,
                timeout=20,
                env={**os.environ, "CLIPSYNC_CONFIG_DIR": directory},
            )
            if result.returncode != 0:
                raise SystemExit(f"Sidecar failed with exit code {result.returncode}")
            frames = [json.loads(line) for line in result.stdout.splitlines()]
            assert len(frames) == 4, "Unexpected stdout messages"
            assert frames[0]["type"] == "ready" and frames[0]["protocol"] == 1
            assert frames[1]["result"]["health"] == "ready"
            assert frames[2]["result"]["items"] == []
            assert frames[3]["result"]["accepted"] is True
            print(f"Packaged IPC startup/status/history/shutdown/reopen {iteration + 1}: PASS")
    with tempfile.TemporaryDirectory(prefix="clipsync-companion-smoke-") as directory:
        verify_companion_runtime([os.path.abspath(args.executable)], directory)


if __name__ == "__main__":
    try:
        main()
    except SmokeError as error:
        raise SystemExit(str(error)) from None
