"""The two forms a peer id takes, and the one place that bridges them.

Discovery only ever puts the **hashed** device id on the wire (the mDNS TXT
record ``device_id_hash``), while pairing, ``cfg.peers`` and every device row in
the UI are keyed by the **real** device id.  So every builder of a device list
has to bridge the two forms — "is this bare mDNS sighting a device I already
listed?" and "is this known peer advertising on the LAN right now?".

Each builder used to answer those its own way, and they drifted:
``src/main.get_device_states`` hashed every known id into a seen-set, while
``internal.web.api.devices.get_devices`` did not — it leaned on the transport
manager's resolved-hash map (empty until a handshake happens) plus a device-NAME
heuristic.  A peer renamed on either side matched neither, so the web device
page listed it twice (once under its real id, once under its hash) while the
desktop view listed it once.

These helpers are that bridge.  ``Discovery`` is imported lazily inside them so
importing a caller (e.g. the web API) never drags zeroconf in, and so a broken
hash never propagates as an exception — an unhashable id degrades to "no hashed
form", which can only cost a dedup, never invent a wrong match.
"""

from collections.abc import Iterable


def hashed_id(peer_id: str) -> str:
    """The mDNS-advertised form of *peer_id*, or "" when it cannot be derived."""
    if not peer_id:
        return ""
    try:
        from internal.transport.discovery import Discovery

        return Discovery._hash_device_id(peer_id) or ""
    except Exception:
        return ""


def id_forms(peer_id: str) -> set[str]:
    """Every id form this peer can appear under: the real id and its hash."""
    if not peer_id:
        return set()
    forms = {peer_id}
    hashed = hashed_id(peer_id)
    if hashed:
        forms.add(hashed)
    return forms


def expand_id_forms(peer_ids: Iterable[str]) -> set[str]:
    """Union of :func:`id_forms` over *peer_ids*.

    Feed a device list's already-listed ids through this before matching bare
    mDNS sightings against it: a sighting arrives hashed, so a set of real ids
    alone can never recognise it.
    """
    out: set[str] = set()
    for peer_id in peer_ids:
        out |= id_forms(peer_id)
    return out


def is_on_network(peer_id: str, live_ids) -> bool:
    """True when *peer_id* is advertising on the LAN at this moment.

    *live_ids* is a set of ids seen by discovery right now (hashed, since that
    is what discovery keys its table by) — either id form is accepted so the
    caller does not have to know which it holds.
    """
    if not peer_id or not live_ids:
        return False
    if peer_id in live_ids:
        return True
    hashed = hashed_id(peer_id)
    return bool(hashed) and hashed in live_ids
