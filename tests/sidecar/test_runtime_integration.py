"""Cross-process stdio, real TLS, persisted pairing and clipboard-use-case integration."""

import collections
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import pytest

from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.config.config import Config, PeerInfo, save
from internal.infrastructure.runtime.lan import LanRuntime
from internal.security.encryption import EncryptionManager, make_password_hash
from internal.security.pairing import PairingManager

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "lan_peer.py"

# How long a notice may take to reach the other device before that counts as
# failure.  Not a rounder number chosen here: a notice whose first send found no
# link is retried by the maintenance tick for `PAIRING_SEND_WAIT` seconds while
# the tick redials the peer, so the runtime's own contract already allows more
# than the ten this file used to allow -- and a window shorter than the contract
# reports the mechanism as broken while it is doing exactly what it documents.
#
# It cannot hide the case this file exists for.  `_end_pairing` arms that retry
# only when the first send *failed*; a notice written successfully and then lost
# in the teardown (`forget_peer` follows immediately) is never retried, so it is
# still missing at 15s and still fails here.  Waiting past 12s also lets the
# runtime's own expiry fire, which logs the divergence into the tail this file
# attaches to the failure.
NOTICE_BUDGET = LanRuntime.PAIRING_SEND_WAIT + 3


class PeerProcess:
    def __init__(self, directory):
        self.events = []
        self.frames = queue.Queue()
        self.log = collections.deque(maxlen=500)
        self._log_lock = threading.Lock()
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(FIXTURE)],
            cwd=ROOT,
            env={**os.environ, "CLIPSYNC_CONFIG_DIR": str(directory)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stderr = threading.Thread(target=self._drain_errors, daemon=True)
        self.stderr.start()
        self.closed = False
        try:
            frame = self.frames.get(timeout=15)
            # A process that died before its handshake says so here, and the
            # reason is on the stderr this class already keeps: without it the
            # failure is a bare "None is not None" and the cause has to be
            # re-derived from the source every time.
            assert frame is not None and frame["type"] == "ready", (
                f"the sidecar exited before it was ready\n"
                f"  sidecar log tail:\n{self.log_tail()}"
            )
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=5)
            self._close_pipes()
            raise

    def _read(self):
        try:
            for raw in self.process.stdout:
                self.frames.put(json.loads(raw))
        finally:
            self.frames.put(None)

    def _drain_errors(self):
        # Kept, not discarded.  The pipe has to be drained whether or not
        # anything reads it, but a sidecar that says nothing about why it went
        # quiet is how a lost-frame bug was read as a tight timeout for two
        # days: the one place that knew what happened was this pipe.
        for raw in self.process.stderr:
            with self._log_lock:
                self.log.append(raw.decode("utf-8", "replace").rstrip())

    def log_tail(self, limit=40):
        with self._log_lock:
            return "\n".join(list(self.log)[-limit:])

    def call(self, method, **params):
        request_id = str(uuid.uuid4())
        self.process.stdin.write((json.dumps({
            "type": "request", "id": request_id, "method": method, "params": params,
        }) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 15
        while True:
            frame = self.frames.get(timeout=max(0.01, deadline - time.monotonic()))
            assert frame is not None, "Sidecar ended before responding"
            if frame["type"] == "event":
                self.events.append(frame)
                continue
            if frame["type"] == "resync":
                continue
            assert frame["type"] == "response", frame
            assert frame["id"] == request_id, frame
            assert frame["ok"], frame.get("error")
            return frame["result"]

    def wait(self, method, predicate, timeout=10, **params):
        deadline = time.monotonic() + timeout
        while True:
            result = self.call(method, **params)
            if predicate(result):
                return result
            if time.monotonic() >= deadline:
                # The sidecar's own account of the window, not just the
                # predicate that never came true: what it was doing while the
                # clock ran out is the only part that says why.
                raise AssertionError(
                    f"Timed out waiting for runtime state\n"
                    f"  last {method} answer: {result}\n"
                    f"  sidecar log tail:\n{self.log_tail()}"
                )
            time.sleep(0.03)

    def _close_pipes(self):
        self.reader.join(timeout=5)
        self.stderr.join(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()

    def close(self):
        if self.closed:
            return
        try:
            if self.process.poll() is None:
                assert self.call("app.shutdown") == {"accepted": True}
            code = self.process.wait(timeout=12)
            # A sidecar that cannot release its runtime exits 1 and says why in
            # a traceback -- on the stderr this class already keeps.  A bare
            # "1 == 0" leaves the cause to be re-derived from the source every
            # time, which is exactly what the handshake above refuses to do.
            assert code == 0, (
                f"the sidecar exited {code} after shutdown\n"
                f"  sidecar log tail:\n{self.log_tail()}"
            )
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self._close_pipes()
            self.closed = True

    def terminate(self):
        """Stop the process the way a machine does: no shutdown call, no exit.

        The point is the *other* side, which has to notice on its own — a
        graceful ``close`` sends nothing either, but it leaves a socket that
        closed from the inside, and what a peer that fell off the network
        looks like is the case worth driving.
        """
        if self.closed:
            return
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        self._close_pipes()
        self.closed = True


def configure_pair(tmp_path, monkeypatch, encrypted, password):
    configs = []
    with ExitStack() as sockets:
        for name in ("left", "right"):
            listener = sockets.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            # The phone companion binds a port of its own, and it is on by
            # default (``Config.web_enabled``).  Two sidecars on one machine
            # would otherwise both want the default 19991, and whether that
            # collides depends on the platform: Windows lets a second socket
            # take a port that is already being listened on, so the clash is
            # invisible there, while on macOS and Linux the second bind is
            # EADDRINUSE and that process answers every command with
            # COMPANION_START_FAILED.  Each peer gets its own, the way its sync
            # port above already does -- two *machines* would each have one,
            # which is what these two stand in for.
            companion = sockets.enter_context(socket.socket())
            companion.bind(("127.0.0.1", 0))
            cfg = Config(
                device_id=name, device_name=name,
                port=listener.getsockname()[1],
                web_port=companion.getsockname()[1], encryption_enabled=encrypted,
                source_tracking_enabled=False, retry_capture_enabled=False,
                sync_debounce=0.01, filter_enabled_categories=[],
                file_receive_dir=str(tmp_path / name / "received"),
            )
            identity = PairingManager(name, name).load_or_create_identity("", "")
            cfg.private_key_pem = identity.private_key_pem
            cfg.certificate_pem = identity.certificate_pem
            if password:
                cfg.encryption_password_hash = make_password_hash(password, identity.fingerprint)
            configs.append((cfg, identity))
        for (cfg, identity), (other, _) in (configs, configs[::-1]):
            cfg.peers[other.device_id] = PeerInfo(
                other.device_id, other.device_name, other.certificate_pem,
                paired=False, last_ip="127.0.0.1", last_port=other.port,
            )
            with monkeypatch.context() as context:
                context.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path / cfg.device_id))
                encryption = (
                    EncryptionManager(identity.fingerprint, password=password)
                    if encrypted else None
                )
                save(cfg, encryption)
                repository = ClipboardHistoryDB(
                    storage_path=str(tmp_path / cfg.device_id / "clipboard_history.db"),
                    enc_mgr=encryption,
                )
                try:
                    repository.add(ClipboardContent(types={
                        ContentType.TEXT: f"{cfg.device_name.title()} clipboard fixture".encode(),
                    }))
                finally:
                    repository.close()


@pytest.mark.parametrize(
    "encrypted,password", [(False, ""), (True, ""), (True, "shared-test-secret")]
)
def test_real_rpc_pairing_bidirectional_copy_and_restart_trust(
    tmp_path, monkeypatch, encrypted, password
):
    configure_pair(tmp_path, monkeypatch, encrypted, password)
    with ExitStack() as cleanup:
        left = PeerProcess(tmp_path / "left")
        cleanup.callback(left.close)
        right = PeerProcess(tmp_path / "right")
        cleanup.callback(right.close)
        for peer in (left, right):
            if password:
                assert peer.call("app.status")["health"] == "locked"
                assert peer.call("app.unlock", password=password) == {"unlocked": True}
        left_item = left.call("history.list")["items"][0]["id"]
        right_item = right.call("history.list")["items"][0]["id"]
        assert left.call("pairing.start", device_id="right") == {"accepted": True}
        # Until the two are connected no code exists, and a known-but-unpaired
        # device that is not on the network yet has no row either -- so the
        # wait has to read an empty list as "not yet", not crash on it.
        left_devices = left.wait(
            "devices.list", lambda d: bool(d["items"] and d["items"][0]["pairing_code"])
        )
        right_devices = right.wait(
            "devices.list", lambda d: bool(d["items"] and d["items"][0]["pairing_code"])
        )
        code = left_devices["items"][0]["pairing_code"]
        assert code == right_devices["items"][0]["pairing_code"]
        assert left_devices["items"][0]["sas"] == right_devices["items"][0]["sas"]
        assert not left.call("pairing.confirm", device_id="right", code=code)["paired"]
        assert not right.call("devices.list")["items"][0]["paired"]
        right.call("pairing.confirm", device_id="left", code=code)
        left.wait("devices.list", lambda d: bool(d["items"]) and d["items"][0]["paired"])
        right.wait("devices.list", lambda d: bool(d["items"]) and d["items"][0]["paired"])

        assert left.call("history.copy", entry_id=left_item) == {"copied": True}
        right.wait("history.list", lambda d: any(
            item["preview"] == "Left clipboard fixture" for item in d["items"]
        ))
        assert right.call("history.copy", entry_id=right_item) == {"copied": True}
        left.wait("history.list", lambda d: any(
            item["preview"] == "Right clipboard fixture" for item in d["items"]
        ))
        assert any(e["name"] == "history.changed" for e in left.events)
        # Exercise the same commands as the desktop over real process/TLS boundaries.
        payload = bytes(range(256)) * 8192
        source = tmp_path / "source.bin"
        source.write_bytes(payload)
        transfer_id = left.call("transfers.send", paths=[str(source)])["transfer_id"]
        right.wait("transfers.list", lambda result: any(
            row["id"] == transfer_id and row["status"] == "pending"
            for row in result["active"]
        ))
        assert right.call("transfers.action", action="accept", transfer_id=transfer_id)["ok"]
        received = right.wait("transfers.list", lambda result: any(
            row["id"] == transfer_id and row["status"] == "completed"
            for row in result["history"]
        ))
        record = next(row for row in received["history"] if row["id"] == transfer_id)
        received_path = Path(record["path"]).resolve()
        assert received_path.is_relative_to((tmp_path / "right" / "received").resolve())
        assert received_path.read_bytes() == payload
        left.wait("transfers.list", lambda result: any(
            row["id"] == transfer_id and row["status"] == "completed"
            for row in result["history"]
        ))
        before_files = set(received_path.parent.iterdir())
        rejected_id = left.call("transfers.send", paths=[str(source)])["transfer_id"]
        right.wait("transfers.list", lambda result: any(
            row["id"] == rejected_id and row["status"] == "pending"
            for row in result["active"]
        ))
        assert right.call("transfers.action", action="reject", transfer_id=rejected_id)["ok"]
        for peer in (left, right):
            rejected = peer.wait("transfers.list", lambda result: any(
                row["id"] == rejected_id and row["status"] == "failed"
                for row in result["history"]
            ))
            assert not any(row["id"] == rejected_id for row in rejected["active"])
        assert set(received_path.parent.iterdir()) == before_files
        # `chat_session_id`, not `session_id`: the frame envelope reserves the
        # latter for the transport session, and the host refuses a result that
        # claims it -- fatally, which is a restart loop rather than a failed
        # command.  This call goes through `Peer.call` and so never meets that
        # check, which is exactly why the key went wrong here unnoticed.
        opened = left.call("chat.invite", peer_id="right", peer_name="right")
        assert opened["connecting"] is False, opened
        session_id = opened["chat_session_id"]
        # Direct send: the invitation is the whole handshake, so both sides are
        # live without an accept step between them.
        for peer in (left, right):
            peer.wait("chat.sessions", lambda result: any(
                row["session_id"] == session_id and row["status"] == "active"
                for row in result["sessions"]
            ))
        for sender, receiver, text in (
            (left, right, "left to right over TLS"),
            (right, left, "right to left over TLS"),
        ):
            assert sender.call("chat.action", action="send", session_id=session_id, text=text)["ok"]
            # `text` is bound as a default so the predicate closes over this
            # round's string rather than the loop variable `wait` may read later.
            receiver.wait(
                "chat.messages",
                lambda result, text=text: any(
                    row.get("text") == text and not row.get("outgoing")
                    for row in result["messages"]
                ),
                session_id=session_id,
            )
            assert receiver.call("chat.action", action="read", session_id=session_id)["ok"]
            sessions = receiver.call("chat.sessions")["sessions"]
            assert next(row for row in sessions if row["session_id"] == session_id)["unread"] == 0
        assert left.call("chat.typing", session_id=session_id, typing=True)["ok"]
        right.wait("chat.sessions", lambda result: any(
            row["session_id"] == session_id and row.get("peer_typing")
            for row in result["sessions"]
        ))
        attachment = left.call("chat.file", action="send", session_id=session_id, path=str(source))
        assert attachment["ok"]
        attachment_id = attachment["transfer_id"]
        # Direct send: nothing here accepts the offer.  An incoming attachment
        # is taken on arrival and never stops at `await_accept`, so reaching
        # `done` without that call is itself the proof that it was accepted.
        right.wait("chat.messages", lambda result: any(
            row.get("transfer_id") == attachment_id and row["status"] == "done"
            for row in result["messages"]
        ), session_id=session_id)
        opened = right.call("chat.open_file", session_id=session_id, transfer_id=attachment_id)
        attachment_path = Path(opened["path"]).resolve()
        assert attachment_path.is_relative_to((tmp_path / "right" / "received").resolve())
        assert attachment_path.read_bytes() == payload
        left.wait("chat.messages", lambda result: any(
            row.get("transfer_id") == attachment_id and row["status"] == "done"
            for row in result["messages"]
        ), session_id=session_id)
        # Per-chunk progress and the outcome travel on the event stream alone —
        # no read reports them, so this is all that moves a chat file bubble.
        for peer in (left, right):
            for name, ok in (("chat.file.progress", False), ("chat.file.done", True)):
                peer.wait("devices.list", lambda _result, p=peer, n=name, s=ok: any(
                    event["name"] == n
                    and event["data"]["transfer_id"] == attachment_id
                    and (not s or event["data"]["success"])
                    for event in p.events
                ))
        assert left.call("sync.set_enabled", enabled=False) == {"enabled": False}
        assert left.call("app.status")["sync_state"] == "paused"

    with ExitStack() as cleanup:
        left = PeerProcess(tmp_path / "left")
        cleanup.callback(left.close)
        right = PeerProcess(tmp_path / "right")
        cleanup.callback(right.close)
        if password:
            left.call("app.unlock", password=password)
            right.call("app.unlock", password=password)
        assert left.call("app.status")["sync_state"] == "paused"
        assert left.call("devices.list")["items"][0]["paired"]
        assert right.call("devices.list")["items"][0]["paired"]
        left.wait(
            "devices.list",
            lambda d: bool(d["items"])
            and d["items"][0]["connection_state"] in ("online", "connected"),
        )
        assert left.call("pairing.unpair", device_id="right") == {"accepted": True}
        # Unpairing drops the link, and a device that is neither paired nor
        # present has no row at all -- so a missing row is the same answer as
        # one that is no longer paired, not a different one.  Read either as
        # "left no longer counts it as paired"; which of the two it is depends
        # on whether the connection outlived the unpair.
        left_rows = left.call("devices.list")["items"]
        assert not left_rows or not left_rows[0]["paired"]
        try:
            right.wait(
                "devices.list",
                lambda d: not d["items"] or not d["items"][0]["paired"],
                timeout=NOTICE_BUDGET,
            )
        except AssertionError as exc:
            # Both sides: this failure is one process not knowing what the
            # other did, so which log is empty is itself the finding.
            raise AssertionError(f"{exc}\n  --- left log tail ---\n{left.log_tail()}") from exc
    assert not (tmp_path / "left" / ".lock").exists()
    assert not (tmp_path / "right" / ".lock").exists()
