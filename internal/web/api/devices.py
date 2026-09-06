"""Connected devices API handlers.

All handlers return a (data_dict, status_code) tuple.
"""

import platform

from internal.platform import friendly_platform_name
from internal.transport.peer_id import expand_id_forms, hashed_id, is_on_network


def _relay_reachable(cfg, peer_id: str) -> bool:
    """True when this peer can be reached over the public relay right now.

    Mirrors ``Application._peer_is_internet_reachable`` plus the
    ``internet_sync_enabled`` gate that ``_relay_publish_to_peer`` applies —
    everything it needs lives on ``cfg``, so no host callback is threaded in.

    The device page needs this to stop OFFERING actions that cannot work: chat
    and the connectivity probe both reach a LAN-offline peer only through the
    relay, so on a paired-but-offline device with no relay path they were
    buttons that could do nothing but fail.
    """
    if not peer_id or not getattr(cfg, "internet_sync_enabled", False):
        return False
    if peer_id in (getattr(cfg, "netpair_secrets", {}) or {}):
        return True
    if peer_id in (getattr(cfg, "peer_relay_secrets", {}) or {}):
        peer = (getattr(cfg, "peers", {}) or {}).get(peer_id)
        return peer is not None and bool(getattr(peer, "paired", False))
    return False


def get_devices(
    cfg,
    get_connected_ids,
    get_discovered=None,
    get_resolved_hashes=None,
    get_pending_pairings=None,
    get_reconnect_states=None,
):
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
    # Live mDNS sightings, fetched up front: the known-peer loop below needs
    # them to decide whether a cfg.peers row still has ANY presence on the
    # network right now.  Keyed by the HASHED device id (discovery never puts a
    # real id on the wire), so membership is tested through _on_network.
    try:
        discovered = (get_discovered() or {}) if get_discovered is not None else {}
    except Exception:
        discovered = {}
    live_ids = set(discovered)

    def _on_network(peer_id: str) -> bool:
        """True when this peer is advertising on the LAN at this moment."""
        return is_on_network(peer_id, live_ids)

    devices = [
        {
            "device_id": cfg.device_id,
            "device_name": cfg.device_name,
            "connected": True,
            "paired": True,
            # The local device has no peer connection to encrypt; its OS is known
            # directly from the host platform so the UI can show the right icon.
            "encrypted": False,
            "os": friendly_platform_name(platform.system()),
            "note": "",
            "known": True,
        }
    ]
    # A paired peer is ALWAYS listed — it is a trust relationship the user
    # established, not cache, so it stays visible (offline, with its last known
    # address) until explicitly removed.  Everything else must earn its place on
    # the page from LIVE network state: a row that is neither paired, nor
    # connected, nor advertising on mDNS right now is stale config cache and is
    # dropped.  That is what kept phantom cards around for devices long gone
    # from the LAN.  Note this is NOT the old `if is_conn or peer.paired` filter
    # it replaces: a device the user just rejected keeps its card for as long as
    # it is really on the network (it lands in the Discovered section), instead
    # of vanishing the instant the socket closed.
    for peer in list(cfg.peers.values()):
        is_conn = peer.device_id in connected_ids
        if not peer.paired and not is_conn and not _on_network(peer.device_id):
            continue
        devices.append(
            {
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
                # Known to us (in cfg.peers) rather than a bare mDNS sighting, so
                # the UI can badge an unpaired one "Not paired" instead of the
                # misleading "🔍 Discovered" (it may not be on mDNS at all).
                "known": True,
                # Can we still reach it while it is LAN-offline?  Gates the card's
                # chat + test-connection actions, which have no other transport.
                "relay_reachable": _relay_reachable(cfg, peer.device_id),
            }
        )

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
            for dev in devices:
                if dev.get("connected") or not dev.get("device_id"):
                    continue
                st = reconnect_states.get(dev["device_id"])
                if st is None:
                    st = reconnect_states.get(hashed_id(dev["device_id"]))
                if isinstance(st, dict) and st:
                    dev["reconnecting"] = True
                    try:
                        dev["reconnect_attempt"] = int(st.get("attempts", 0) or 0)
                        dev["reconnect_max"] = int(st.get("max_attempts", 0) or 0)
                    except (TypeError, ValueError):
                        dev["reconnect_attempt"] = 0
                        dev["reconnect_max"] = 0

    # Every id form each listed row can be sighted under.  Discovery only ever
    # puts the HASHED device id on the wire while these rows are keyed by the
    # real one, so without the hashed forms the sweep below cannot recognise its
    # own peer and lists it a SECOND time as a bare "Discovered" card under a
    # different device_id.  This is the only *exact* dedup for a known peer:
    # rev_resolved covers just the peers the transport manager has resolved a
    # hash for (ones we connected to since start-up), and the name heuristic
    # further down silently misses a device renamed on either side.
    seen_ids = expand_id_forms(d["device_id"] for d in devices)
    # Exclude the local device's own name: it is never in get_discovered()
    # (discovery filters self by device_id hash), so including it only hides a
    # genuinely different device that happens to share our name/prefix.
    known_names = {d["device_name"].lower() for d in devices if d["device_id"] != cfg.device_id}

    # Removed/archived devices (the forget action) so the device page can
    # offer a Restore management surface.  Read here (before the discovered
    # sweep) so a forgotten device that is still advertising on the LAN is
    # NOT re-surfaced as a fresh "Discovered" device — it lives only in the
    # Removed archive until restored or purged.  Newest removal first.
    removed_peers = getattr(cfg, "removed_peers", None) or {}
    # A forgotten device keeps advertising under its HASHED mDNS id, while the
    # archive keys it by the REAL device_id — so match on both forms, or a
    # forgotten device reappears as "Discovered" while also sitting in the
    # Removed archive.
    removed_ids = expand_id_forms(getattr(p, "device_id", "") for p in removed_peers.values())

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
    # `discovered` was already fetched at the top (the known-peer filter needs
    # it), so re-reading it here would risk a second, inconsistent snapshot.
    if get_discovered is not None:
        for peer_id, info in discovered.items():
            name = info.get("name", peer_id) if isinstance(info, dict) else str(info)
            if peer_id in seen_ids or peer_id in removed_ids or _name_matches_known(name):
                continue
            devices.append(
                {
                    "device_id": peer_id,
                    "device_name": name,
                    "connected": False,
                    "paired": False,
                    "encrypted": False,
                    "os": None,
                    "note": "",
                    # A bare mDNS sighting — genuinely "Discovered", unlike the
                    # known peers above.
                    "known": False,
                }
            )

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
                pending_list.append(
                    {
                        "peer_id": p.get("peer_id", ""),
                        "peer_name": p.get("peer_name", p.get("device_name", "")),
                        "code": p.get("code", ""),
                        "status": p.get("status", "pending"),
                        # Short Authentication String derived from both devices'
                        # certificate fingerprints; empty when unknown, in which
                        # case the UI omits the row.
                        "sas": p.get("sas", ""),
                    }
                )
            elif isinstance(p, (tuple, list)) and len(p) >= 3:
                pending_list.append(
                    {
                        "peer_id": p[0],
                        "code": p[1],
                        "peer_name": p[2],
                        # transient pairing lifecycle status: pending /
                        # confirmed_waiting / peer_confirmed / paired / cancelled
                        "status": p[3] if len(p) > 3 else "pending",
                        # 5th element (optional): the pairing SAS to display on
                        # the confirmation card for cross-device comparison.
                        "sas": p[4] if len(p) > 4 else "",
                    }
                )
        result["pending_pairings"] = pending_list

    # Removed/archived devices (the forget action) so the device page can
    # offer a Restore management surface.  Newest removal first.  `removed_peers`
    # was read above (for the discovered-sweep dedup); the defensive getattr
    # there also lets a config shape without the archive (or a test stub)
    # yield an empty list instead of crashing the snapshot.
    result["removed"] = [
        {
            "device_id": p.device_id,
            "device_name": p.device_name,
            "removed_at": p.removed_at,
            "paired": p.paired,
            "has_address": bool(p.last_ip and p.last_port),
        }
        for p in sorted(
            removed_peers.values(),
            key=lambda p: p.removed_at,
            reverse=True,
        )
    ]

    return result, 200
