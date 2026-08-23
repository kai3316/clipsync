"""Connected devices API handlers.

All handlers return a (data_dict, status_code) tuple.
"""

import platform


def get_devices(cfg, get_connected_ids, get_discovered=None,
                get_resolved_hashes=None, get_pending_pairings=None,
                get_reconnect_states=None):
    """Return list of connected devices with status.

    Replicates the existing GET /api/devices logic from server.py
    (original lines 1233-1251), extended to deduplicate discovered peers
    against known/paired peers and to include any pending pairing requests.

    *get_resolved_hashes* maps hashed mDNS peer ids to real device ids
    (from the transport manager) so a discovered entry for an already-known
    device is not listed a second time — mirroring the desktop ``_get_peers``
    logic.  *get_pending_pairings* returns the pairing manager's pending
    requests (list of ``(peer_id, code, peer_name)`` tuples).
    *get_reconnect_states* optionally returns the transport manager's
    auto-reconnect bookkeeping (``{id: {attempts, max_attempts}}``, keyed by
    either id form) so an offline paired peer mid-reconnect carries
    ``reconnecting/reconnect_attempt/reconnect_max`` for the UI.
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

    # Attach auto-reconnect progress to offline known peers so the UI can
    # show "reconnecting (attempt N/M)" instead of a bare offline badge.
    # The bookkeeping is keyed by whichever id form reconnect scheduling
    # used — try the real device_id first, then its hashed mDNS form
    # (mirrors src/main.py get_device_states step 6).
    if get_reconnect_states is not None:
        try:
            reconnect_states = get_reconnect_states() or {}
        except Exception:
            reconnect_states = {}
        if reconnect_states:
            def _hashed(peer_id):
                try:
                    from internal.transport.discovery import Discovery
                    return Discovery._hash_device_id(peer_id)
                except Exception:
                    return ""
            for dev in devices:
                if dev.get("connected") or not dev.get("device_id"):
                    continue
                st = reconnect_states.get(dev["device_id"])
                if st is None:
                    st = reconnect_states.get(_hashed(dev["device_id"]))
                if isinstance(st, dict) and st:
                    dev["reconnecting"] = True
                    try:
                        dev["reconnect_attempt"] = int(st.get("attempts", 0) or 0)
                        dev["reconnect_max"] = int(st.get("max_attempts", 0) or 0)
                    except (TypeError, ValueError):
                        dev["reconnect_attempt"] = 0
                        dev["reconnect_max"] = 0

    seen_ids = {d["device_id"] for d in devices}
    known_names = {d["device_name"].lower() for d in devices}

    # Resolve hashed discovery ids to real peer ids (desktop _get_peers
    # ~2724): a hashed id that maps to a known/paired device is "seen".
    if get_resolved_hashes is not None:
        try:
            resolved = get_resolved_hashes() or {}
        except Exception:
            resolved = {}
        rev_resolved: dict[str, set] = {}
        for h_id, r_id in resolved.items():
            rev_resolved.setdefault(r_id, set()).add(h_id)
        for real_id in list(seen_ids):
            seen_ids.update(rev_resolved.get(real_id, ()))

    def _name_matches_known(disc_name: str) -> bool:
        """True when a discovered name looks like the same device as a known one.

        mDNS instance names are truncated, so a discovered name may be a
        *prefix* of a known full name — only that direction counts.  The
        reverse (discovered starts with known) swallowed genuinely new
        devices whose name merely extends a shorter known name ("MacBook"
        hid a real "MacBook-Pro-2").
        """
        dl = disc_name.lower()
        # The mDNS instance name now carries a unique "-<hash4>" suffix
        # (discovery registers "<8-char base>-<hash4>"); strip it before
        # matching so a suffixed advertisement still dedups against the
        # known full name, while the suffix keeps real instances unique.
        if len(dl) > 5 and dl[-5] == "-" and all(c in "0123456789abcdef" for c in dl[-4:]):
            dl = dl[:-5]
        for kn in known_names:
            if dl == kn:
                return True
            # A truncated discovery name that is a prefix of a known full
            # name is the same device.
            if len(dl) >= 8 and kn.startswith(dl):
                return True
        return False

    # Include discovered-but-unpaired peers so the web UI's "Discovered"
    # section is populated and users can initiate pairing from the phone.
    if get_discovered is not None:
        try:
            discovered = get_discovered() or {}
        except Exception:
            discovered = {}
        for peer_id, info in discovered.items():
            name = info.get("name", peer_id) if isinstance(info, dict) else str(info)
            if peer_id in seen_ids or _name_matches_known(name):
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

    result = {"devices": devices}

    # Pending pairing requests are push-only today (each request is broadcast
    # to connected clients), so a request that arrives while no web client is
    # attached would otherwise be invisible.  Include them in the snapshot
    # when the coordinator supplies the callback.
    if get_pending_pairings is not None:
        try:
            pending = get_pending_pairings() or []
        except Exception:
            pending = []
        pending_list = []
        for p in pending:
            if isinstance(p, dict):
                pending_list.append({
                    "peer_id": p.get("peer_id", ""),
                    "peer_name": p.get("peer_name", p.get("device_name", "")),
                    "code": p.get("code", ""),
                    "status": p.get("status", "pending"),
                })
            elif isinstance(p, (tuple, list)) and len(p) >= 3:
                pending_list.append({
                    "peer_id": p[0],
                    "code": p[1],
                    "peer_name": p[2],
                    # transient pairing lifecycle status: pending /
                    # confirmed_waiting / peer_confirmed / paired / cancelled
                    "status": p[3] if len(p) > 3 else "pending",
                })
        result["pending_pairings"] = pending_list

    return result, 200
