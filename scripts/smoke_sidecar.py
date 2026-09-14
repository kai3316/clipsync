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


def companion_get(status, path):
    """One authenticated Companion GET, returning the raw body."""
    base = f"http://127.0.0.1:{status.get('actual_port')}"
    separator = "&" if "?" in path else "?"
    token = quote(status.get("token") or "", safe="")
    with build_opener(ProxyHandler({})).open(
        base + path + separator + "token=" + token, timeout=5,
    ) as response:
        return response.read()


def verify_share(child, status, directory):
    """A desktop share reaches the phone's own list and download route.

    The one check that ties the three layers together: the RPC the window
    calls, the copy the sidecar makes, and the two Companion routes the phone
    reads.  Run against the packaged executable and over the real IPC wire, so
    it covers the framing and the packaging as well as the behaviour.
    """
    source = Path(directory) / "smoke-share-source.txt"
    payload = b"clipsync smoke share"
    source.write_bytes(payload)
    staged = child.call("companion.share_file", {"path": str(source)})
    require(staged.get("ok") is True and staged.get("size") == len(payload),
            "Share did not stage the file")
    name = staged.get("name")
    require(isinstance(name, str) and bool(name), "Share returned no name")
    listed = json.loads(companion_get(status, "/api/files"))
    require(any(row.get("name") == name for row in listed.get("files") or []),
            "Shared file is not in the phone's list")
    require(companion_get(status, "/api/download?file=" + quote(name, safe="")) == payload,
            "Shared file did not download")
    require(source.read_bytes() == payload, "Share moved the original file")
    print("Shared file reaches the phone's list and download: PASS")


def require_port_closed(port):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(.05)
    raise SmokeError("Companion listener survived shutdown")


def verify_clear_token(child):
    """The packaged build can clear the access token, and the clear serves.

    The legacy web panel's second token button, and the one thing the capability
    comparison above cannot see: `clear_token` is a parameter of a method that
    already existed, so a build predating it advertises the same list and would
    refuse the parameter instead.  Run last -- it leaves the companion serving
    without a token, which is the point.
    """
    cleared = child.call("companion.configure", {"enabled": True, "clear_token": True})
    require(not cleared.get("token"), "Clearing the token left one behind")
    require(cleared.get("access_url") is None, "A cleared companion still offers a token URL")
    url = cleared.get("url")
    require(isinstance(url, str) and url and "token=" not in url,
            "The cleared address still carries a token")
    # Served without one, because an empty expected token is what the companion
    # accepts every request against.
    body = companion_get({**cleared, "token": ""}, "/api/history")
    require(b"total" in body, "The companion did not serve without a token")
    print("Cleared access token serves without one: PASS")


def source_module(name):
    return Path(__file__).resolve().parents[1] / "internal" / "application" / name


def capability_expression():
    """The sidecar's own capability expression, from `bootstrap.py`."""
    import ast

    for node in ast.walk(ast.parse(source_module("bootstrap.py").read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "capabilities":
                return value
    raise SmokeError("No capability list found in bootstrap.py")


def fold_capabilities(node, runtime_up):
    """Read one capability expression the way the sidecar's own runtime would.

    The value is ``[...] + ([...] if <engine up> else [])``, so a plain literal
    read sees half of it and ``runtime_up`` decides the branch.  Same fold as
    `tests/sidecar/test_e2e_host_capabilities.py`, which holds the Playwright
    fixture to the same list.
    """
    import ast

    if isinstance(node, ast.List):
        return [ast.literal_eval(element) for element in node.elts]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return fold_capabilities(node.left, runtime_up) + fold_capabilities(node.right, runtime_up)
    if isinstance(node, ast.IfExp):
        return fold_capabilities(node.body if runtime_up else node.orelse, runtime_up)
    return []


def verify_capabilities(status):
    """The packaged build is this tree, not an older one.

    A stale artifact is not hypothetical: a build the record credited was ten
    Python modules behind, and every other check in this file would have passed,
    because those exercise routes that existed in both builds.  The list the
    sidecar advertises is where a new method shows up first, and this reads the
    same list out of `bootstrap.py` -- so an executable built before a method was
    added cannot agree with the tree it is being verified against.

    Which half it should carry is read from the state it reports rather than
    assumed: the engine's methods are advertised only while the engine is up.
    Both directions are checked, so a method this tree has dropped is caught too.
    """
    advertised = status.get("capabilities")
    require(isinstance(advertised, list) and bool(advertised),
            "Sidecar advertises no capabilities")
    runtime_up = status.get("sync_state") in ("running", "paused")
    expected = fold_capabilities(capability_expression(), runtime_up)
    # The floor fits the engine-down half, which is the smaller one: this is
    # called with whichever state the sidecar reports.
    require(len(expected) > 40, f"Read only {len(expected)} capabilities from bootstrap.py")
    missing = sorted(set(expected) - set(advertised))
    extra = sorted(set(advertised) - set(expected))
    require(not missing, "Packaged sidecar is older than this tree; it does not know: "
                         + ", ".join(missing))
    require(not extra, "Packaged sidecar advertises what this tree does not grant: "
                       + ", ".join(extra))
    print(f"Packaged capabilities match this tree ({len(expected)}): PASS")


def verify_companion_runtime(command, directory):
    # Disable clipboard synchronization in this disposable profile.  The shared
    # directory is named too: left unset it resolves to the user's own
    # ~/Downloads/ClipSync, and a smoke test must not write there.
    # ``config_version`` is written out because the companion is ON by default:
    # this profile wants it off so the enable transition below is a real one,
    # and a config below version 3 carrying ``web_enabled: false`` is exactly
    # what the v3 migration brings forward.  Version 3 makes this an off that
    # was chosen rather than a default that has not moved yet.
    #
    # ``service_type`` is the one field here that is not about this profile's
    # own settings.  Starting a runtime registers a real mDNS service — that is
    # what the runtime is for — so a sidecar started under the app's service
    # type joins the LAN it is being tested on.  The app already running on this
    # machine then discovers it, and lists it: same hostname, so it reads as the
    # user's own machine, under a device id that belongs to no config and is
    # gone as soon as the process is killed (a killed process sends no goodbye,
    # so the entry lingers for the record's TTL).  A service type of its own
    # keeps this profile off the app's radar while exercising everything else.
    config = {"config_version": 3, "encryption_enabled": False, "sync_enabled": False,
              "web_enabled": False, "port": temporary_port(),
              "service_type": "_clipsync-smoke._tcp.local.",
              "file_receive_dir": str(Path(directory) / "received")}
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
            verify_share(child, status, directory)
            if not iteration:
                verify_capabilities(child.call("app.status"))
            previous_token = status["token"]
            stopped = child.call("companion.configure", {"enabled": False})
            require(not stopped["running"] and stopped["token"] is None
                    and stopped["access_url"] is None, "Companion did not stop")
            require_port_closed(port)
            # Exit while enabled, then verify persisted startup in the next process.
            status = child.call("companion.configure", {"enabled": True, "port": port})
            verify_http(status)
            if iteration:
                # Last iteration, last check: this one leaves the companion
                # serving without a token, which is what it is asserting.
                verify_clear_token(child)
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
