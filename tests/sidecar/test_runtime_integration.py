"""Cross-process stdio, real TLS, persisted pairing and clipboard-use-case integration."""

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
from internal.security.encryption import EncryptionManager, make_password_hash
from internal.security.pairing import PairingManager

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "lan_peer.py"


class PeerProcess:
    def __init__(self, directory):
        self.events = []
        self.frames = queue.Queue()
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
            assert frame is not None and frame["type"] == "ready"
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
        for _ in self.process.stderr:
            pass

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

    def wait(self, method, predicate, **params):
        deadline = time.monotonic() + 10
        while True:
            result = self.call(method, **params)
            if predicate(result):
                return result
            assert time.monotonic() < deadline, "Timed out waiting for runtime state"
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
            assert self.process.wait(timeout=12) == 0
        finally:
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
            cfg = Config(
                device_id=name, device_name=name,
                port=listener.getsockname()[1], encryption_enabled=encrypted,
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
        left_devices = left.wait("devices.list", lambda d: bool(d["items"][0]["pairing_code"]))
        right_devices = right.wait("devices.list", lambda d: bool(d["items"][0]["pairing_code"]))
        code = left_devices["items"][0]["pairing_code"]
        assert code == right_devices["items"][0]["pairing_code"]
        assert left_devices["items"][0]["sas"] == right_devices["items"][0]["sas"]
        assert not left.call("pairing.confirm", device_id="right", code=code)["paired"]
        assert not right.call("devices.list")["items"][0]["paired"]
        right.call("pairing.confirm", device_id="left", code=code)
        left.wait("devices.list", lambda d: d["items"][0]["paired"])
        right.wait("devices.list", lambda d: d["items"][0]["paired"])

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
        invited = left.call("chat.invite", peer_id="right", peer_name="right")
        assert invited["connecting"] is False, invited
        session_id = invited["chat_session_id"]
        right.wait("chat.sessions", lambda result: any(
            row["session_id"] == session_id and row["status"] == "invited"
            for row in result["sessions"]
        ))
        assert right.call("chat.action", action="accept", session_id=session_id)["ok"]
        left.wait("chat.sessions", lambda result: any(
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
        right.wait("chat.messages", lambda result: any(
            row.get("transfer_id") == attachment_id and row["status"] == "await_accept"
            for row in result["messages"]
        ), session_id=session_id)
        assert right.call(
            "chat.file", action="accept", session_id=session_id, transfer_id=attachment_id
        )["ok"]
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
        left.wait("devices.list", lambda d: d["items"][0]["connection_state"] in (
            "online", "connected"
        ))
        assert left.call("pairing.unpair", device_id="right") == {"accepted": True}
        assert not left.call("devices.list")["items"][0]["paired"]
        right.wait("devices.list", lambda d: not d["items"][0]["paired"])
    assert not (tmp_path / "left" / ".lock").exists()
    assert not (tmp_path / "right" / ".lock").exists()
