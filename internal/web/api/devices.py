"""Connected devices API handlers.

All handlers return a (data_dict, status_code) tuple.
"""

import platform


def get_devices(cfg, get_connected_ids):
    """Return list of connected devices with status.

    Replicates the existing GET /api/devices logic from server.py
    (original lines 1233-1251).
    """
    connected_ids = set(get_connected_ids()) if get_connected_ids else set()
    devices = [{
        "device_id": cfg.device_id,
        "device_name": cfg.device_name,
        "connected": True,
        "paired": True,
        # The local device has no peer connection to encrypt; its OS is known
        # directly from the host platform so the UI can show the right icon.
        "encrypted": False,
        "os": platform.system(),
        "note": "",
    }]
    for peer in list(cfg.peers.values()):
        is_conn = peer.device_id in connected_ids
        if is_conn or peer.paired:
            devices.append({
                "device_id": peer.device_id,
                "device_name": peer.device_name,
                "connected": is_conn,
                "paired": peer.paired,
                # Transport is always TLS 1.3, so any live connection is
                # encrypted end-to-end — mirror desktop's "🔒 encrypted" badge.
                "encrypted": is_conn,
                # Per-peer OS is not exchanged yet; leave unset so the
                # frontend falls back to the generic 💻 icon.
                "os": getattr(peer, "os", "") or None,
                "note": getattr(peer, "notes", "") or "",
            })
    return {"devices": devices}, 200
