"""Connected devices API handlers.

All handlers return a (data_dict, status_code) tuple.
"""

import platform


def get_devices(cfg, get_connected_ids, get_discovered=None):
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
    # Include discovered-but-unpaired peers so the web UI's "Discovered"
    # section is populated and users can initiate pairing from the phone.
    if get_discovered is not None:
        try:
            discovered = get_discovered() or {}
        except Exception:
            discovered = {}
        known_ids = {d["device_id"] for d in devices}
        known_names = {d["device_name"] for d in devices}
        for peer_id, info in discovered.items():
            name = info.get("name", peer_id) if isinstance(info, dict) else str(info)
            if peer_id in known_ids or name in known_names:
                continue
            devices.append({
                "device_id": peer_id,
                "device_name": name,
                "connected": False,
                "paired": False,
                "encrypted": False,
                "os": None,
                "note": "",
            })
    return {"devices": devices}, 200
