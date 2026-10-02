"""Keep a proven record of the handshake ordering the connection layer needs.

This is a probe, not production code: it runs the three-frame exchange over a real
TLS loopback with real certificates and the real pairing helpers, and prints
whether both sides verified.  It exists because the first attempt at this in the
connection layer failed for a reason that had nothing to do with the crypto and
everything to do with ordering, and the design was then abandoned as unworkable.

The design, now measured working:

    accept:  -> identity(cert_a, nonce_a)          send first; no dependency
    dial:    -> identity(cert_d, nonce_d)          holds nonce_a from frame one
             -> verify(proof_d = sign(nonce_a, nonce_d), nonce_d)
    accept:  reads both frames; verify(proof_a = sign(nonce_d, nonce_a), nonce_a)
    dial:    reads the verify frame; verify(proof_a)

Neither side waits on a proof before sending one: the accept's proof needs
nonce_d, and the dial sends nonce_d itself, so the dial never blocks on the accept
having read anything.
"""

import socket
import ssl
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from internal.security.pairing import PairingManager  # noqa: E402


def identity(device_id):
    manager = PairingManager(device_id, device_id.upper())
    manager.load_or_create_identity(str(Path(tempfile.mkdtemp())), "")
    return manager


def context(manager, server_side):
    obj = manager.get_identity()
    scratch = Path(tempfile.mkdtemp())
    cert = scratch / "c.pem"
    key = scratch / "k.pem"
    cert.write_text(obj.certificate_pem)
    key.write_text(obj.private_key_pem)
    ctx = (
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        if server_side
        else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    )
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


def send(sock, *fields):
    """One frame of length-prefixed fields, so a multi-line PEM cannot be split."""
    payload = b""
    for field in fields:
        raw = str(field).encode("utf-8")
        payload += len(raw).to_bytes(4, "big") + raw
    sock.sendall(len(payload).to_bytes(4, "big") + payload)


def recv(sock):
    """Read one frame; the field count comes from the frame, never from the caller."""
    header = b""
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            return []
        header += chunk
    length = int.from_bytes(header, "big")
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            return []
        body += chunk
    fields, pos = [], 0
    while pos < len(body):
        size = int.from_bytes(body[pos : pos + 4], "big")
        pos += 4
        fields.append(body[pos : pos + size].decode("utf-8"))
        pos += size
    return fields


def run():
    server, dialer = identity("server"), identity("dialer")
    server.add_peer("dialer", "Dialer", dialer.get_identity().certificate_pem, paired=True)
    dialer.add_peer("server", "Server", server.get_identity().certificate_pem, paired=True)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    out = {}

    def serve():
        raw, _ = listener.accept()
        with context(server, True).wrap_socket(raw, server_side=True) as tls:
            nonce_a = server.make_handshake_nonce()
            send(tls, "identity", server.get_identity().certificate_pem, nonce_a)
            cert_d, nonce_d = recv(tls)[1:]
            proof_d, nonce_d_echo = recv(tls)[1:]
            out["accept_verified_dialer"] = server.verify_handshake_nonce(
                cert_d, nonce_d_echo or nonce_d, nonce_a, proof_d
            )
            send(tls, "proof", server.sign_handshake_nonce(nonce_d, nonce_a), nonce_a)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        context(dialer, False).wrap_socket(raw, server_hostname="x") as tls,
    ):
        cert_a, nonce_a = recv(tls)[1:]
        nonce_d = dialer.make_handshake_nonce()
        send(tls, "identity", dialer.get_identity().certificate_pem, nonce_d)
        send(tls, "proof", dialer.sign_handshake_nonce(nonce_a, nonce_d), nonce_d)
        proof_a, _echo = recv(tls)[1:]
        out["dialer_verified_accept"] = dialer.verify_handshake_nonce(
            cert_a, nonce_a, nonce_d, proof_a
        )

    thread.join(5)
    listener.close()
    return out


if __name__ == "__main__":
    result = run()
    for key in sorted(result):
        print(f"  {key:26s} {result[key]}")
    print("VERDICT:", "works" if result and all(result.values()) else "DOES NOT WORK")
