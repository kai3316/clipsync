"""mDNS service discovery for finding ClipSync peers on the LAN.

Registers this device as a _clipsync._tcp service and discovers
other devices running ClipSync on the same local network.
"""

import contextlib
import logging
import platform
import random
import socket
import threading
import time
from collections.abc import Callable

import ifaddr
from zeroconf import (
    DNSOutgoing,
    DNSQuestion,
    RecordUpdateListener,
    ServiceBrowser,
    ServiceInfo,
    Zeroconf,
)

# The wire numbers for a PTR question.  Not exported at the top level -- the
# protocol constants have no public names -- and the alternative is writing
# 12 and 1 into a query nobody can check.
from zeroconf.const import _CLASS_IN, _FLAGS_QR_QUERY, _TYPE_PTR

from internal.system.updater import running_shell
from internal.transport.ids import peer_id_hash
from internal.transport.ids import sanitize_peer_str as _sanitize_peer_str
from internal.version import __version__

logger = logging.getLogger(__name__)

# The presence round: how often this machine asks the network who is here, and
# how long a name may go unheard before it is reported lost.
#
# The pair is the whole latency budget for "a device left": it is reported at
# the first round after PRESENCE_TIMEOUT, so between 20 and 30 seconds -- and a
# device has to miss two consecutive answers to get there, which is the budget
# for one dropped multicast packet.  Both are worth more than they look: the
# browser fires Added only on *novelty*, so before this a peer was reported lost
# when its cached PTR lapsed (never, the library clamps it to 1125 s) and a peer
# that had been reported lost could not come back at all until it restarted --
# see _PresenceListener.
PRESENCE_INTERVAL_SECONDS = 10.0
PRESENCE_TIMEOUT_SECONDS = 20.0

# Announce on every Nth round -- 60 seconds, deliberately half of the 120 s TTL
# a peer caches our A records for.  A peer running this build re-hears us every
# round anyway (it asks, we answer); a peer running an older build asks nothing
# after its four startup queries, and this is the only thing keeping our address
# from expiring in its cache.
ANNOUNCE_EVERY_ROUNDS = 6

# How long to wait for the answers to one query.  A peer that is there answers
# in 20-120 ms (the library's own jitter), so this is generous for the normal
# case and covers the one path that is slower: a record multicast in the last
# second is held back for another second by the library's flood protection
# (RFC 6762 section 14).  An answer that misses the window is not lost -- the
# listener stamps it whenever it lands.
SCAN_SETTLE_SECONDS = 0.6

# A delivered record counts as "a peer spoke" only if its packet arrived just
# now.  This is what separates that from "the cache handed back a record it has
# held since the peer last spoke": the cache replay on listener registration and
# the ten-second expiry sweep both deliver records whose ``created`` is old.
PRESENCE_STALE_MILLIS = 2 * PRESENCE_INTERVAL_SECONDS * 1000

# How long ``get_service_info`` may take when a peer's record is not in the
# cache.  The library's own default is 3000 ms, and the presence round must not
# be able to spend that per peer: a responder that sends a PTR without its
# SRV/TXT additionals turns each re-read into a stall, and the round runs on a
# thread the runtime is waiting on.
_SERVICE_INFO_TIMEOUT_MS = 1000

# How long the host-name and FQDN lookups may hold up address enumeration.  A
# ceiling on a working-but-slow resolver, not a target: a name that resolves at
# all resolves in single-digit milliseconds.  A name that is in no zone has
# held callers of ``get_all_local_addresses`` for the better part of a minute,
# and those callers include the app's startup path (the companion logs the URL
# it is reachable at) and the pairing QR -- both of which are on the thread the
# user is waiting on.
HOSTNAME_LOOKUP_TIMEOUT = 2.0


def _resolved_host_addresses(timeout: float) -> list[str]:
    """Every IPv4 address the host name and the FQDN resolve to, bounded.

    Both lookups share one worker thread and one deadline: the thread that
    called us must never be the one sitting in the resolver.  ``getfqdn()`` is
    in here rather than in the caller for the same reason -- it is a reverse
    lookup of the host name, and on the machines this bound exists for it is
    the slowest of the three.

    The worker is a daemon and may outlive the wait, so what is returned is a
    snapshot: an address it appends after the deadline is one this call does
    not see, which is the same outcome as a lookup that failed.
    """
    found: list[str] = []

    def _worker() -> None:
        for name in (socket.gethostname(), socket.getfqdn()):
            try:
                for info in socket.getaddrinfo(name, None, family=socket.AF_INET):
                    found.append(info[4][0])
            except Exception as exc:
                # One line, not a traceback: this runs on every enumeration --
                # the network watcher alone calls it twice a minute -- and on
                # macOS the FQDN is the reverse name of the host's own `::`,
                # which no resolver answers.  A four-frame traceback per minute
                # said nothing the name and the errno do not, and buried the
                # lines that mattered in a log the user is asked to send.
                logger.debug("Address lookup for %r failed: %s", name, exc)

    worker = threading.Thread(target=_worker, daemon=True, name="lan-address-lookup")
    worker.start()
    worker.join(timeout)
    return list(found)


def get_all_local_addresses():
    """Enumerate every non-loopback IPv4 address on this host.

    ``socket.gethostname()`` alone is unreliable: on macOS/Linux it can
    resolve to only the loopback address (or a single adapter), which made
    us advertise an unreachable IP and caused peers on the same network to
    fail to discover us. We combine three sources and deduplicate:

      1. the primary routable address (UDP connect trick — no packets sent),
      2. every address the hostname resolves to,
      3. every address the FQDN resolves to.
    """
    addresses: list[str] = []
    seen: set[str] = set()

    def _add(ip: str) -> None:
        ip = (ip or "").strip()
        if not ip or ip == "0.0.0.0" or ip.startswith("127.") or ip.startswith("169.254."):
            return
        # Privacy: only advertise RFC1918 private addresses.  A public one is
        # never useful for LAN discovery and would expose more of the machine's
        # network topology than needed.  A tunnel's address is private by range
        # and is dropped further down, where the adapters are known.
        if not _is_private_ip(ip):
            return
        if ip not in seen:
            seen.add(ip)
            addresses.append(ip)

    # 1. Primary routable address — this is what a peer on the same subnet
    #    would actually use to reach us.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            _add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass

    # 2+3. Hostname-resolved addresses (Windows usually returns all adapters)
    #    plus the FQDN's, which covers the bare-hostname /etc/hosts gap.
    #    Bounded, and for a reason worth naming: these are the only two calls
    #    in this function that can take arbitrarily long.  On a host whose own
    #    name no resolver knows -- a CI runner, a laptop that just joined a
    #    guest network, anything behind a VPN that took the name server with it
    #    -- they block for the resolver's own timeout, which is tens of
    #    seconds, and this function is on the app's startup path.
    #
    #    The bound costs no correctness: step 1 resolves no names at all, so
    #    the primary routable address is already in hand by the time it
    #    applies, and a lookup that misses the deadline contributes exactly
    #    what one that fails contributes.
    for ip in _resolved_host_addresses(HOSTNAME_LOOKUP_TIMEOUT):
        _add(ip)

    addresses = _advertisable(addresses, _adapter_kinds())

    # Cap the advertised set: more IPs than this means unusual network
    # topology (many adapters) — advertising them all only broadens exposure.
    MAX_ADVERTISED_IPS = 10  # noqa: N806
    if len(addresses) > MAX_ADVERTISED_IPS:
        addresses = addresses[:MAX_ADVERTISED_IPS]

    if not addresses:
        logger.warning("Failed to enumerate local addresses")
    return addresses


# How good an address is to advertise, and to dial from, lowest first.
#
# ``virtual`` ranks worse than ``other`` deliberately.  ``other`` is the bucket
# every real adapter lands in when its name says nothing this code recognises —
# a Linux bridge, an unusual vendor string — and a tunnel that outranked one of
# those would be preferred over the very wire a peer is plugged into.  A virtual
# adapter is chosen only when it is the only address there is, which is the
# machine reachable through nothing else.
_ADAPTER_PRIORITY = {"ethernet": 0, "wifi": 1, "other": 2, "virtual": 3}

# Substrings naming a tunnel or a virtual adapter rather than a piece of wire.
# Matched against the adapter's *description* as well as its name, which is what
# makes this independent of the interface language: Windows localizes the
# friendly name and never the description.  Matching the friendly name alone
# read a live tunnel as an ordinary adapter -- "VirtualNet" and "Heysocks" match
# nothing in a keyword list built from "vpn"/"tunnel"/"tap" -- and then handed
# its address to every peer on the LAN, which dialled it and found nothing.
_VIRTUAL_MARKERS = (
    "vpn",
    "tunnel",
    "tap",
    "tun",
    "wintun",
    "wireguard",
    "openvpn",
    "nordlynx",
    "tailscale",
    "zerotier",
    "clash",
    "mihomo",
    "singbox",
    "sing-box",
    "v2ray",
    "xray",
    "shadowsocks",
    "socks",
    "proxy",
    "warp",
    "teredo",
    "ppp",
    "bridge",
    "br-",
    "virtual",
    "hyper-v",
    "vethernet",
    "vmware",
    "virtualbox",
    "vmnet",
    "vbox",
    "docker",
    "wsl",
    "veth",
    "awdl",
    "ipsec",
    "loopback",
    "bluetooth",
)
_WIFI_MARKERS = ("wi-fi", "wifi", "wlan", "wireless", "802.11", "airport")
_ETHERNET_MARKERS = ("ethernet", "local area")

# The interface's own name, where that is all a platform offers: macOS's ``en0``
# and ``utun3``, Linux's ``eth0``/``enp3s0``/``wlp2s0``.  Prefixes rather than
# substrings, because a name is short and a stray match inside one is not a hint.
_WIRE_NAME_PREFIXES = ("en", "eth")
_WIFI_NAME_PREFIXES = ("wl", "ww")


def _kind_of(text: str, name: str = "") -> str:
    """Which sort of adapter *text* describes: ethernet, wifi, virtual or other.

    The tunnel test runs first, and that order is the point: "vEthernet (WSL)"
    and "Hyper-V Virtual Ethernet Adapter" both contain "eth", and a virtual
    adapter classified as a cable would be preferred over the real one beside it.

    *name* is the interface's own name, which is all an ``ip``/``ifconfig``
    world offers — ``en0``, ``eth0``, ``wlp2s0``, ``utun3``.  It carries the
    same distinction the description does, and it is the *only* thing that does
    where no description exists, so it is matched both ways round: as a prefix,
    to tell a wire from a wireless one, and as text, where the markers above
    catch the tunnels.  ``utun3`` is a tunnel because it holds "tun", not
    because it does not hold "en".
    """
    haystack = (text or "").lower()
    if any(marker in haystack for marker in _VIRTUAL_MARKERS):
        return "virtual"
    if any(marker in haystack for marker in _WIFI_MARKERS):
        return "wifi"
    if (name or "").lower().startswith(_WIRE_NAME_PREFIXES):
        return "ethernet"
    if (name or "").lower().startswith(_WIFI_NAME_PREFIXES):
        return "wifi"
    if any(marker in haystack for marker in _ETHERNET_MARKERS):
        return "ethernet"
    return "other"


def _adapter_kinds() -> dict[str, str]:
    """Every local IPv4 address, mapped to the kind of adapter carrying it.

    One enumeration, three readers: ``get_all_local_addresses`` drops what must
    not be advertised, ``_get_interface_priorities`` ranks the rest, and the
    candidates a peer offers are ranked against our own tunnels.

    Read from ``ifaddr`` — the same interface list zeroconf itself binds its
    sockets to — rather than from the platform's own tooling.  It reports each
    adapter's description ("VirtualNet Tunnel", "TAP-Windows Adapter V9",
    "Intel(R) Ethernet Controller I226-V") next to its address, needs no process
    spawn and no console codepage, and reads the same way on all three
    platforms.  An enumeration that fails yields no kinds, which is the
    behaviour every caller had before kinds existed.
    """
    kinds: dict[str, str] = {}
    try:
        for adapter in ifaddr.get_adapters():
            # ``name`` is the interface's own name (``en0``, ``eth0``) and
            # ``nice_name`` its description; on Windows the first is a GUID and
            # the second is the vendor's words, and on POSIX it is the other way
            # round.  Both go in, and the name half is offered to the prefix
            # rules as well.
            name = adapter.name or ""
            text = f"{adapter.nice_name or ''} {name}"
            for entry in adapter.ips:
                if not getattr(entry, "is_IPv4", False):
                    continue
                kinds[entry.ip] = _kind_of(text, name)
    except Exception as exc:
        logger.debug("Could not classify the local adapters: %s", exc)
    return kinds


def _advertisable(addresses: list[str], kinds: dict[str, str]) -> list[str]:
    """*addresses* without the tunnel ones, unless those are all there is.

    A tunnel's address is private by range -- WireGuard's 10.x, OpenVPN's
    172.16-31.x -- so it survives every other filter here, and it is still not
    somewhere a peer on this network can reach us: the peer that dials it dials
    a black hole and lists a device it cannot talk to.  The exception is a
    machine whose only way to be reached IS the tunnel; that one advertises it,
    because the alternative is advertising nothing at all.
    """
    reachable = [ip for ip in addresses if kinds.get(ip) != "virtual"]
    return reachable or addresses


def _get_interface_priorities() -> dict[str, int]:
    """Sort key per local address, lowest wins (see ``_ADAPTER_PRIORITY``)."""
    fallback = _ADAPTER_PRIORITY["other"]
    return {ip: _ADAPTER_PRIORITY.get(kind, fallback) for ip, kind in _adapter_kinds().items()}


def _virtual_addresses() -> set[str]:
    """Addresses carried by a tunnel or a virtual adapter on this machine."""
    return {ip for ip, kind in _adapter_kinds().items() if kind == "virtual"}


def _get_local_address():
    all_ips = get_all_local_addresses()
    if not all_ips:
        logger.warning(
            "No non-loopback IP found, falling back to 127.0.0.1 "
            "-- this device will not be reachable from other hosts"
        )
        return "127.0.0.1"
    priorities = _get_interface_priorities()
    sorted_ips = sorted(all_ips, key=lambda ip: priorities.get(ip, 99))
    best = sorted_ips[0]
    logger.info("Local IPs: %s (selected %s)", all_ips, best)
    return best


def _clip_bytes(value: str, limit: int) -> str:
    """Trim *value* to at most *limit* UTF-8 bytes, whole characters only.

    Trimming by characters is not the same bound: the wire caps a TXT string
    by bytes, and a name of emoji reaches it at half the character count.
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _is_private_ip(ip: str) -> bool:
    """True if *ip* is an RFC1918 private LAN address."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)


def _route_source(ip: str) -> str | None:
    """The local address this machine would reach *ip* from, or None.

    A UDP socket that is connected and never written to sends nothing: this asks
    the routing table a question, reads the answer off ``getsockname()`` and
    closes the socket.  None means the OS has no route to it at all, which is a
    real answer too — a peer's address this machine cannot reach is not the
    address to dial it at, however ordinary the number looks.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((ip, 5353))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _pick_best_address(
    candidates: list[str], our_ip: str, virtual_ips: frozenset[str] = frozenset()
) -> str:
    """Choose the remote address most likely reachable on the LAN.

    Ranked, best first:

      0. on the same /24 as our own address — a peer on this network;
      1. a private address this machine would reach over a real interface;
      2. a private address it would reach through a tunnel of its own, which is
         right for a peer that really is on the same VPN and wrong for one that
         is on this network;
      3. a private address it has no route to at all;
      4. the rest.

    Rank 1 against rank 2 is the one that matters with a VPN up.  Every private
    address used to rank equally and the tie was broken by the *string*, so a
    peer advertising both 192.168.1.238 and a tunnel's 10.8.0.2 was dialled at
    the tunnel address — "10." sorts before "192." — from any machine not on its
    own /24.  Which route a packet would leave by is the routing table's answer,
    so the routing table is asked.

    Falls back to the first candidate on a tie, and ties within a rank are still
    broken by the address string: mDNS address order is unstable across
    announcements, and index-based tie-breaking made the chosen address flap
    between two same-rank interfaces.
    """

    def _subnet(ip: str) -> str:
        parts = ip.split(".")
        return ".".join(parts[:3]) if len(parts) == 4 else ip

    our_sub = _subnet(our_ip)

    def rank(ip: str) -> tuple:
        if _subnet(ip) == our_sub:
            return (0,)
        if not _is_private_ip(ip):
            return (4,)
        source = _route_source(ip)
        if source is None:
            # Nowhere to send it: this machine cannot reach that address at all.
            return (3,)
        if source in virtual_ips:
            return (2,)
        return (1,)

    # Tie-break by the address string, not the list index — mDNS address order
    # is unstable across announcements, so index-based tie-breaking made the
    # chosen address flap between two same-subnet interfaces.
    return min(candidates, key=lambda ip: (rank(ip), ip))


class _PresenceListener(RecordUpdateListener):
    """Stamps every PTR this machine hears, so presence stops depending on the
    browser.

    The browser is the wrong clock for "is that device still there", because it
    fires ``Added`` only on *novelty*: a peer that re-announces the same record
    produces no event at all, an A/SRV/TXT that expires while its PTR lives is
    never re-queried, and a PTR that has been reported lost is not announced as
    Added again for as long as the cached copy lives -- and the library clamps a
    PTR's TTL up to 1125 seconds (``_DNS_PTR_MIN_TTL``), so "as long as" is
    measured in tens of minutes.  The record manager has no such opinion: every
    answer packet is handed to every listener as it arrives.  That asymmetry is
    the whole reason this class exists.

    It runs on the zeroconf event-loop thread, so it does one dict assignment
    and nothing else.  Two calls are forbidden from here and both are the
    obvious next refactor: ``update_service`` (deadlocks from this thread) and
    ``get_service_info`` (refuses to run on this thread).  Re-announcing because
    a peer was heard is also the wrong shape -- see ``_presence_round``.
    """

    def __init__(self, owner: "Discovery"):
        self._owner = owner

    def async_update_records(self, zc, now, records) -> None:
        owner = self._owner
        service_type = owner._service_type.lower()
        own = owner._instance_name
        cutoff = now - PRESENCE_STALE_MILLIS
        with owner._heard_lock:
            for update in records:
                record = update.new
                if record.type != _TYPE_PTR or record.name.lower() != service_type:
                    continue
                # Two ways a record arrives without anybody having spoken:
                # the registration replay (a record cached long ago, delivered
                # with ``old is None`` so it is indistinguishable from a new
                # one) and the ten-second sweep that delivers expired records.
                if record.created < cutoff or record.is_expired(now):
                    continue
                alias = record.alias
                if alias and alias != own:
                    owner._heard[alias] = now / 1000.0


class Discovery:
    """mDNS-based peer discovery."""

    @staticmethod
    def _hash_device_id(device_id: str) -> str:
        return peer_id_hash(device_id)

    def __init__(self, device_id: str, device_name: str, port: int, service_type: str):
        self._device_id = device_id
        self._device_name = device_name
        self._device_id_hash = self._hash_device_id(device_id)
        # The instance label is truncated and id-tagged (see _label); the name
        # the user chose rides in the TXT record instead, which is what peers
        # list this device under.
        self._display_name = self._label(device_name, self._device_id_hash)
        self._port = port
        self._service_type = service_type
        self._zc: Zeroconf | None = None
        self._service_info: ServiceInfo | None = None
        self._browser: ServiceBrowser | None = None
        self._listener: _PresenceListener | None = None
        self._on_peer_found: Callable | None = None
        self._on_peer_lost: Callable | None = None
        self._lock = threading.Lock()
        self._known_peers: dict[str, dict] = {}  # peer_id -> info
        self._service_to_peer: dict[str, str] = {}  # service_name -> peer_id
        self._our_ip = "127.0.0.1"
        # This machine's own tunnel and virtual-adapter addresses, refreshed
        # wherever the advertisement is rebuilt (see _advertised_ips).  A peer's
        # candidate addresses are ranked against it, and the mDNS callback
        # thread must not go looking for adapters to do that.
        self._virtual_ips: frozenset[str] = frozenset()
        # Network-change watcher state: the set of addresses we advertised,
        # plus a stop signal for the light poll in _network_watch_loop.
        self._netmon_stop = threading.Event()
        self._advertised_ips: frozenset[str] = frozenset()
        # Presence bookkeeping (see _presence_round): instance name -> the
        # monotonic second its last PTR arrived.  Written from the zeroconf
        # event loop by _PresenceListener and read by the presence thread, so
        # it carries its own lock -- never _lock, which the browser thread
        # holds while it works and which must not be held across a round.
        self._heard: dict[str, float] = {}
        self._heard_lock = threading.Lock()
        # One round at a time.  A manual scan runs on the RPC thread and the
        # loop's round on the presence thread; without this they would each
        # announce, ask and settle at the same time.
        self._scan_lock = threading.Lock()
        # Our own instance name, read live by the listener, which runs on the
        # event loop and so must not touch _service_info (lock-protected).
        self._instance_name = ""
        # Which round we are on, for the periodic announcement.
        self._rounds = 0

    def set_callbacks(self, on_found: Callable, on_lost: Callable):
        """Set callbacks for peer discovery events.

        ``on_found(peer_id_hash, name, address, port, version, os, arch, app, named)``

        and ``on_lost(peer_id_hash)``.  The trailing fields are what the peer
        advertised about itself; ``named`` says whether ``name`` is the one its
        user chose (see ``_handle_service_added``), which is what tells a caller
        whether it may replace a name it already holds.
        """
        with self._lock:
            self._on_peer_found = on_found
            self._on_peer_lost = on_lost

    @staticmethod
    def _label(device_name: str, device_id_hash: str) -> str:
        """The mDNS instance label: 8 characters of the name, plus an id tag.

        The hash suffix keeps two devices whose names share their first 8
        characters on distinct instances instead of fighting over one mDNS
        name and resolving to each other.
        """
        base = device_name[:8] if device_name else "ClipSync"
        return f"{base}-{device_id_hash[:4]}"

    def _published_name(self) -> str:
        """The device name to put in the TXT record, or "" to publish none.

        Peers list this device by the name the user gave it, and the truncated
        instance label is only a fallback for peers that hear nothing else —
        which is why the name has to travel somewhere, and why it travels here:
        the label cannot hold it, since 8 characters is what makes it
        collision-free.

        Deliberately nothing while the name is still this machine's hostname.
        The truncated label exists so a hostname is never broadcast in
        plaintext on the LAN, and a hostname the user never chose is not
        something this record should hand out. A name they did choose is a
        label they meant to be seen, and this is what makes it seen.
        """
        name = (self._device_name or "").strip()
        if not name or name == platform.node():
            return ""
        # A TXT string is capped at 255 bytes by the wire format, and the
        # settings page puts no cap on the name. Character count is not that
        # cap: 64 emoji are 256 bytes, which is a registration that raises
        # instead of a device that advertises.
        return _clip_bytes(_sanitize_peer_str(name), 200)

    def _service_props(self, addresses: list[str] | None = None) -> dict[bytes, bytes]:
        """The TXT record we publish, for both registrations.

        Built in one place because it is published twice — once by ``start``
        and again by every ``start_advertising`` — and a field added to one
        copy only would be there or not depending on whether this device had
        been hidden and shown again.

        *addresses* is the list the caller is also putting in the A records, so
        that the two halves of one registration describe the same machine.  Left
        out, it is enumerated here — which is what a caller with no A records to
        fill in wants.
        """
        # Use the hashed device_id to avoid exposing the real device identity
        # in plaintext mDNS TXT records.
        props = {
            b"device_id_hash": self._device_id_hash.encode("utf-8"),
            b"v": __version__.encode("utf-8"),
            b"os": platform.system().lower().encode("utf-8"),
            b"arch": (platform.machine() or "").lower().encode("utf-8"),
            # Which of the two applications published from this repository this
            # device is running.  Both are one release with two assets per
            # platform, so (os, arch) does not say which one a peer can use --
            # a Windows device here may be running either, and the installer
            # for one is not installable by the other.  It travels with the
            # same three fields for the same reason: the device list reads it
            # to decide whether an update exchange is even possible.
            b"app": running_shell().encode("utf-8"),
        }
        name = self._published_name()
        if name:
            props[b"n"] = name.encode("utf-8")
        if addresses is None:
            addresses = get_all_local_addresses()
        for i, ip in enumerate(addresses):
            props[f"alt_ip_{i}".encode()] = ip.encode()
        return props

    def set_device_name(self, device_name: str):
        """Rename this device's advertisement, live.

        The settings page said "takes effect for network discovery after a
        restart" because the name was captured here once, at construction, and
        nothing ever revisited it — the peers that had already listed this
        device kept the old name until a restart, and the ones that had not
        learned it from the TXT record but from a dial kept it forever.
        """
        name = (device_name or "").strip()
        with self._lock:
            if not name or name == self._device_name:
                return
            self._device_name = name
            label = self._label(name, self._device_id_hash)
            renamed = label != self._display_name
            self._display_name = label
            advertising = self._service_info is not None
        if not (advertising and renamed):
            return
        # A service instance name is part of the registration, so a rename is a
        # new registration: the old instance goes first, or both stay live and
        # a peer that learned either name keeps resolving to the dead one.
        self.stop_advertising()
        self.start_advertising()
        logger.info("Renamed this device to %s on the LAN", name)

    def start(self):
        """Register our service and start browsing for peers."""
        try:
            self._zc = Zeroconf()
        except Exception as e:
            logger.error("Failed to initialize mDNS: %s", e)
            return

        all_ips = get_all_local_addresses()
        props = self._service_props(all_ips)
        local_ip = _get_local_address()
        self._our_ip = local_ip
        logger.info("Registering mDNS on %s (all IPs: %s)", local_ip, all_ips)

        # Register our service – use a truncated display name so the
        # real hostname is not broadcast in plaintext on the LAN.
        #
        # Advertise ALL our LAN addresses (not just the "best" one) so peers
        # on any of our subnets can reach us. Previously we only advertised
        # a single IP chosen by a fragile interface heuristic, which broke
        # discovery whenever that IP belonged to a VPN/virtual adapter -- so
        # the lesson is that every address on a real adapter goes out, and the
        # tunnel's is the one address that must not (it is not reachable from
        # this network at all; see get_all_local_addresses).
        advertised = [socket.inet_aton(ip) for ip in all_ips]
        if not advertised:
            advertised = [socket.inet_aton(local_ip)]
        info = ServiceInfo(
            type_=self._service_type,
            name=f"{self._display_name}.{self._service_type}",
            addresses=advertised,
            port=self._port,
            properties=props,
        )

        # Same rule as start_advertising(): _service_info is only set once
        # zeroconf has accepted the registration, so is_advertising() never
        # claims we are discoverable while we are not.
        registered = None
        try:
            self._zc.register_service(info)
            registered = info
            logger.info("Registered mDNS service on port %d", self._port)
        except Exception as e:
            # A registration failure must not leave us undiscoverable —
            # retry once under a name with an extra distinguishing suffix
            # instead of only logging and giving up.
            logger.warning("mDNS registration failed (%s) — retrying with altered name", e)
            try:
                retry_info = ServiceInfo(
                    type_=self._service_type,
                    name=f"{self._display_name}-x.{self._service_type}",
                    addresses=advertised,
                    port=self._port,
                    properties=props,
                )
                self._zc.register_service(retry_info)
                registered = retry_info
                logger.info("Registered mDNS service on retry name")
            except Exception as e2:
                logger.warning("mDNS retry registration also failed: %s", e2)
                # Both attempts failed — leave _service_info None so
                # is_advertising() reports False and start_advertising() can
                # retry later, instead of believing we are still broadcasting.
        with self._lock:
            self._service_info = registered
            self._instance_name = registered.name if registered is not None else ""

        # Browse for peers
        self._open_browser()

        # Remember the advertised address set and start the watcher that
        # re-registers when the local interfaces change (Wi-Fi <-> Ethernet,
        # VPN toggle, DHCP renewal moving us to another subnet) — those
        # produce no sleep/wake event, so without this the stale mDNS
        # advertisement keeps pointing peers at a dead address.
        self._advertised_ips = frozenset(all_ips)
        self._virtual_ips = frozenset(_virtual_addresses())
        self._netmon_stop.clear()
        threading.Thread(
            target=self._network_watch_loop,
            daemon=True,
            name="clipsync-netwatch",
        ).start()
        threading.Thread(
            target=self._presence_loop,
            daemon=True,
            name="clipsync-presence",
        ).start()

    def _network_watch_loop(self):
        """Rebuild mDNS when the local IP set changes (no sleep involved).

        Sleep/wake recovery rides the transport's wake callback; an ordinary
        interface switch does not.  A cheap socket-level enumeration every
        30 s notices a changed address set and rebuilds the Zeroconf instance
        outright — both the registration *and* the browse sockets — so peers
        can find us again immediately instead of only after an app restart.
        """
        CHECK_INTERVAL = 30.0  # noqa: N806
        while not self._netmon_stop.wait(CHECK_INTERVAL):
            if self._zc is None:
                return
            try:
                current = frozenset(get_all_local_addresses())
            except Exception:
                continue
            if not current or current == self._advertised_ips:
                # An empty set is a transient disconnection — keep the old
                # registration rather than re-advertising 127.0.0.1.
                continue
            logger.info(
                "Local addresses changed (%s -> %s) — rebuilding mDNS",
                sorted(self._advertised_ips),
                sorted(current),
            )
            # Do NOT record `current` here: start_advertising() stamps
            # _advertised_ips only when zeroconf actually accepts the new
            # registration.  Claiming it up front made a failed re-register
            # permanent -- the next tick saw current == _advertised_ips and
            # never tried again, leaving peers pointed at the dead address.
            self._restart_zeroconf()

    # ── Presence ───────────────────────────────────────────────

    def _presence_loop(self):
        """Ask the network who is here, every ten seconds, forever.

        Runs beside the browser rather than replacing it: the browser is what
        resolves a service this machine has never seen, and this is what notices
        one that stopped answering, one that is answering from a different
        address, and one that came back after being reported lost.

        Gated on the browse session, which is the important half: with browsing
        paused this machine is not listening, and a round that ran anyway would
        report every peer lost for the crime of going unheard by a machine that
        had stopped listening.
        """
        while True:
            # Spread the rounds: devices that started together (a wake, a
            # reboot, two laptops opened at once) would otherwise ask in the
            # same instant for the rest of the session.
            delay = PRESENCE_INTERVAL_SECONDS * random.uniform(0.8, 1.2)
            if self._netmon_stop.wait(delay):
                return
            if self._browser is None:
                continue
            try:
                self._presence_round(
                    announce=self._rounds % ANNOUNCE_EVERY_ROUNDS == 0
                )
            except Exception:
                logger.debug("Presence round failed", exc_info=True)

    def _presence_round(self, announce=False):
        """One round: announce if it is due, ask, wait, read the answers back."""
        with self._scan_lock:
            zc = self._zc
            if zc is None or self._browser is None:
                return
            self._rounds += 1
            if announce:
                self._announce(zc)
            round_start = time.monotonic()
            # A question with no known answers, deliberately.  The library's own
            # service query carries the cached PTRs it is asking about, and a
            # responder that is entitled to suppress those answers sends
            # nothing at all — PTR, SRV, TXT and address alike — so a peer that
            # is sitting there answering questions is never asked one it will
            # answer.  That is how "the device is not discovered" happens while
            # the device is on the network.
            with contextlib.suppress(Exception):
                zc.send(self._build_query())
            self._netmon_stop.wait(SCAN_SETTLE_SECONDS)
            if self._browser is None:
                # Browsing was paused while we waited.
                return
            with self._heard_lock:
                answered = [
                    name
                    for name, when in self._heard.items()
                    if when >= round_start
                ]
            # Read back only the peers that answered just now, and only to
            # refresh their row.  An answer carries SRV, TXT and the addresses
            # as additionals beside the PTR, so this comes out of the cache;
            # and it is what turns "a peer whose address changed" into
            # something noticed within one round rather than whenever the
            # browser next happened to fire.  A peer that is gone answered
            # nothing, so it costs nothing here either — the sweep is what
            # takes it off the list.
            for name in answered:
                if self._netmon_stop.is_set():
                    return
                try:
                    self._handle_service_added(
                        zc,
                        self._service_type,
                        name,
                        timeout=_SERVICE_INFO_TIMEOUT_MS,
                    )
                except Exception:
                    # A torn-down instance raises out of get_service_info
                    # instead of returning None — _restart_zeroconf swaps and
                    # closes the Zeroconf this round is holding.
                    logger.debug("Presence re-read failed for %s", name, exc_info=True)
            self._sweep_presence()

    def _sweep_presence(self):
        """Report every peer that has gone unheard for ``PRESENCE_TIMEOUT``.

        A peer leaves the same way a goodbye takes it — out of ``_known_peers``,
        then ``on_lost`` — because the question "is this device still here" has
        one answer, and nothing else in the app has an opinion about it.

        Asked per *peer*, not per name.  A peer that renamed itself has two
        names pointing at it, and a per-name sweep would suppress each of them
        for the other's sake, so a device that renamed and then left would stay
        on the list for good.  A stamped name is dropped as it is swept, which is
        what keeps the sweeps that follow from reporting the same peer again —
        and a peer that answers later is stamped fresh and read back in, which is
        how a device that was briefly unreachable returns within a round.
        """
        now = time.monotonic()
        with self._heard_lock:
            for name in [
                name
                for name, when in self._heard.items()
                if now - when > PRESENCE_TIMEOUT_SECONDS
            ]:
                del self._heard[name]
            heard = set(self._heard)
        lost: list[str] = []
        with self._lock:
            for pid in [
                pid for pid in self._known_peers if not self._still_heard(pid, heard)
            ]:
                del self._known_peers[pid]
                lost.append(pid)
            # Drop the names of the peers that just left: a name is how this
            # machine answers "is that peer still here", and one that still
            # names a peer it has already reported lost would vouch for it in
            # the next sweep and let a goodbye report the same loss twice.
            for name in [
                name for name, pid in self._service_to_peer.items() if pid in lost
            ]:
                self._service_to_peer.pop(name, None)
            on_lost = self._on_peer_lost
        for pid in lost:
            logger.info("Peer lost: %s", pid)
            if on_lost:
                try:
                    on_lost(pid)
                except Exception:
                    logger.debug("on_lost callback failed for %s", pid, exc_info=True)

    def _still_heard(self, pid, heard):
        """Whether any of this peer's names is among the ones just heard.

        A peer with no name at all is not heard: ``start_browsing`` clears the
        mapping, so a peer that has not answered since the browse resumed is
        exactly a peer this cannot vouch for.
        """
        names = [name for name, peer in self._service_to_peer.items() if peer == pid]
        return any(name in heard for name in names)

    def _build_query(self) -> DNSOutgoing:
        """A PTR question for our own service type, with no known answers."""
        out = DNSOutgoing(_FLAGS_QR_QUERY, True)
        out.add_question(DNSQuestion(self._service_type, _TYPE_PTR, _CLASS_IN))
        return out

    def _announce(self, zc) -> None:
        """Say this machine's record again, without re-registering it.

        ``generate_service_broadcast`` rather than ``update_service``: the
        latter removes and re-adds the registration — which resurrects a service
        the user has just hidden — mutates the ``ServiceInfo`` that
        ``is_advertising`` and the window's visibility indicator read, and
        blocks for the better part of a second (three broadcasts, 225 ms apart)
        while it does it.  The record has not changed, so all this has to do is
        say it once more.
        """
        with self._lock:
            info = self._service_info
        if info is None:
            return
        with contextlib.suppress(Exception):
            zc.send(zc.generate_service_broadcast(info, None))

    def scan(self):
        """Ask the network who is here, and read the answers back.

        The round the loop runs, with the announcement always included: a
        refresh is the one moment a reader has asked this machine to say
        something as well as ask.  Blocks for about ``SCAN_SETTLE_SECONDS``, and
        ``_scan_lock`` is what keeps it from overlapping a round already in
        flight — a second click joins nothing and simply waits its turn.
        """
        try:
            self._presence_round(announce=True)
        except Exception:
            logger.debug("Manual scan failed", exc_info=True)

    def stop(self):
        self._netmon_stop.set()
        self.stop_browsing()
        self.stop_advertising()
        if self._zc:
            self._zc.close()
            self._zc = None
        logger.info("Discovery stopped")

    # ── Granular control ───────────────────────────────────────

    @property
    def is_browsing(self) -> bool:
        return self._browser is not None

    @property
    def is_advertising(self) -> bool:
        # Read under the lock like every other _service_info access: this is
        # what the UI's "visible on the LAN" indicator reads, and it must not
        # be able to observe the half-written state a concurrent
        # (re-)register leaves behind.
        with self._lock:
            return self._zc is not None and self._service_info is not None

    def stop_browsing(self):
        """Stop discovering new peers without affecting advertising."""
        self._close_browser()

    def start_browsing(self):
        """Resume discovering new peers. Requires start() to have been called."""
        if self._browser is not None:
            return
        if self._zc is None:
            return
        with self._lock:
            # The new browser re-fires Added for every live service, so the
            # name→id mapping is rebuilt from scratch.  Until a peer answers
            # the new browse nothing here vouches for it, which is deliberate:
            # the mapping is the evidence a sweep reads (see _still_heard), and
            # a peer whose evidence predates the pause is a peer this machine
            # has not heard from since it started listening again.  The round
            # that follows re-reads everyone who answers it, so a device that
            # is there is back in the mapping before the sweep asks.
            self._service_to_peer.clear()
        self._open_browser()

    def _open_browser(self):
        """Browse, and listen for the answers to the browsing.

        One place because the browser is built from two call sites — ``start``
        and ``start_browsing``, the second of which is also how
        ``_restart_zeroconf`` recovers after a network change — and the presence
        listener has to be attached on both.  It was two identical constructions
        before, which is exactly how one path ends up without the listener.
        """
        self._browser = ServiceBrowser(
            self._zc,
            self._service_type,
            handlers=[self._on_service_state_change],
        )
        self._listener = _PresenceListener(self)
        with contextlib.suppress(Exception):
            # No question: every record, and no replay of what the cache
            # already holds.  The listener's freshness test rejects a replayed
            # record anyway, so there is nothing to gain by asking for one.
            self._zc.add_listener(self._listener, None)
        logger.info("Started browsing for peers")

    def _close_browser(self):
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
            logger.info("Stopped browsing for peers")
        if self._listener is not None:
            # Removed from the instance it was added to: _restart_zeroconf
            # calls this before it swaps _zc, so ``self._zc`` is still the one
            # that holds the listener.
            with contextlib.suppress(Exception):
                self._zc.remove_listener(self._listener)
            self._listener = None

    def stop_advertising(self):
        """Unregister mDNS service without affecting browsing."""
        with self._lock:
            info, self._service_info = self._service_info, None
        if self._zc is None or info is None:
            return
        try:
            self._zc.unregister_service(info)
        except Exception as e:
            # zeroconf raises if the service was already gone (or the socket
            # died with the interface).  _service_info is cleared either way --
            # swallowing it here is what lets start_advertising() re-register.
            logger.debug("mDNS unregister failed: %s", e)
        logger.info("Stopped advertising this device")

    def start_advertising(self):
        """Re-register mDNS service. Requires start() to have been called."""
        with self._lock:
            if self._service_info is not None:
                return
        if self._zc is None:
            return
        # Rebuild service info (IPs may have changed, and ServiceInfo
        # can't be re-registered after unregistration).
        all_ips = get_all_local_addresses()
        props = self._service_props(all_ips)
        local_ip = _get_local_address()
        self._our_ip = local_ip
        # The tunnels moved with the addresses, and this is the pass that
        # notices: a VPN coming up adds one, and every peer's candidate list is
        # ranked against the set from here on.
        virtual = frozenset(_virtual_addresses())
        advertised = [socket.inet_aton(ip) for ip in all_ips]
        if not advertised:
            advertised = [socket.inet_aton(local_ip)]
        info = ServiceInfo(
            type_=self._service_type,
            name=f"{self._display_name}.{self._service_type}",
            addresses=advertised,
            port=self._port,
            properties=props,
        )
        # Publish into self._service_info only AFTER register_service()
        # succeeds.  Assigning first meant a failed registration still left the
        # attribute set, and the `is not None` guard above then rejected every
        # later attempt -- the device stayed permanently undiscoverable, with
        # is_advertising() cheerfully reporting True.
        try:
            self._zc.register_service(info)
        except Exception as e:
            logger.warning("Failed to re-register mDNS: %s", e)
            return
        with self._lock:
            self._service_info = info
            # Read by the presence listener, which runs on the event loop and
            # must not touch _service_info: the name is what tells our own
            # record apart from a peer's when it comes back to us.
            self._instance_name = info.name
            self._advertised_ips = frozenset(all_ips)
            self._virtual_ips = virtual
        logger.info("Resumed advertising this device on port %d", self._port)

    def _wake_recovery(self):
        """Rebuild mDNS after wake-from-sleep (the transport's wake callback).

        Same problem as an interface change: the address set the sockets were
        bound to is gone.
        """
        self._restart_zeroconf()

    def _restart_zeroconf(self):
        """Replace the Zeroconf instance, re-registering and re-browsing.

        A ``Zeroconf`` object binds one socket per local address *when it is
        constructed* and never rebinds them.  zeroconf only logs the resulting
        ``[Errno 65] No route to host`` (its ``error_received``) and keeps the
        dead socket in the poll set, so once an interface comes or goes — a
        VPN toggle, a Wi-Fi switch, a sleep — both halves of discovery go on
        using addresses that no longer exist: we answer nothing, we hear
        nothing, and every query leaves through a socket with no route.  Peers
        then stay invisible for the rest of the session, which is why the only
        thing that ever cleared this was restarting the app's sync.

        Re-registering on the same instance (what this used to do after a
        sleep, and what the network watcher used to do on every change) cannot
        fix that: a registration rides those same stale sockets.  The instance
        is cheap, so it is replaced outright.

        The replacement is built *before* the old one is torn down, so a
        construction failure leaves the working state untouched and the
        caller's next tick simply retries.
        """
        if self._netmon_stop.is_set() or self._zc is None:
            return
        try:
            fresh = Zeroconf()
        except Exception as e:
            logger.warning("Could not rebuild mDNS after a network change: %s", e)
            return
        if self._netmon_stop.is_set():
            # stop() ran while we were constructing; do not leave it behind.
            with contextlib.suppress(Exception):
                fresh.close()
            return
        self.stop_browsing()
        self.stop_advertising()  # unregisters on the instance we are leaving
        old, self._zc = self._zc, fresh
        with contextlib.suppress(Exception):
            old.close()
        self.start_advertising()
        self.start_browsing()
        logger.info("Rebuilt mDNS sockets after a network change")

    def _on_service_state_change(self, zeroconf, service_type, name, state_change):
        """Handle mDNS service add/update/remove events."""
        if state_change.name in ("Added", "Updated"):
            self._handle_service_added(zeroconf, service_type, name)
        elif state_change.name == "Removed":
            self._handle_service_removed(name)

    def _handle_service_added(self, zeroconf, service_type, name, timeout=3000):
        """Take one peer's answer and put it on the list.

        ``timeout`` bounds the cache miss: the library's ``get_service_info``
        waits its full timeout for a record that is not in the cache, and the
        presence round calls this per peer, so it passes something shorter than
        the three seconds a browser callback can afford to wait.  A responder
        that sends its PTR without the SRV/TXT additionals is what makes that
        path reachable at all.
        """
        info = zeroconf.get_service_info(service_type, name, timeout=timeout)
        if info is None:
            return

        props = info.properties or {}
        # Read the hashed device_id from TXT records.
        peer_id_hash = ""
        if b"device_id_hash" in props:
            peer_id_hash = props[b"device_id_hash"].decode("utf-8")

        # Skip our own service by comparing the hashed identity.
        if peer_id_hash == self._device_id_hash:
            return

        # A legacy / malformed service with no hashed id in its TXT records
        # cannot be addressed as a peer.  Skip it instead of creating a bogus
        # _known_peers entry keyed by "" (which ghosts the Discovered list).
        if not peer_id_hash:
            logger.debug("Discovered service %s with no device_id_hash — skipping", name)
            return

        # Advertised version/platform/arch (M2 P2P update). Absent for older
        # peers that predate these TXT fields.
        peer_version = props.get(b"v", b"").decode("utf-8", errors="replace")
        peer_os = props.get(b"os", b"").decode("utf-8", errors="replace")
        peer_arch = props.get(b"arch", b"").decode("utf-8", errors="replace")
        # Which application the peer runs (see ``_service_props``).  Empty for
        # any build that predates the field, which is not the same as this
        # one's shell -- an unknown peer is one whose answer cannot be read.
        peer_app = props.get(b"app", b"").decode("utf-8", errors="replace")

        # Collect every candidate address: all mDNS A/AAAA records plus the
        # alt_ip_N TXT records we advertise. mDNS may list them in any order
        # and the first is often not the one reachable on our subnet, so we
        # gather them all and pick the most likely one below.
        candidates: list[str] = []
        seen: set[str] = set()

        def _add_candidate(ip: str) -> None:
            ip = (ip or "").strip()
            if not ip or ip == "0.0.0.0" or ip.startswith("127.") or ip.startswith("169.254."):
                return
            if ip not in seen:
                seen.add(ip)
                candidates.append(ip)

        for raw in info.addresses or []:
            try:
                _add_candidate(socket.inet_ntoa(raw))
            except Exception:
                continue

        for key in props:
            if key.startswith(b"alt_ip_"):
                try:
                    _add_candidate(props[key].decode("utf-8"))
                except Exception:
                    continue

        if not candidates:
            logger.warning("Discovered service %s with no usable address", name)
            return

        port = info.port
        # The name to list this peer under: the one the user on the other side
        # chose, when they published it.  Falling back to the instance label —
        # a privacy-safe truncation of whatever name that device started with,
        # e.g. "<display_name>._clipsync._tcp.local." — which is all a peer
        # running a build from before the TXT name can offer.
        try:
            label = _sanitize_peer_str(name.split(".")[0])
        except (IndexError, TypeError):
            label = peer_id_hash
        chosen = props.get(b"n", b"")
        peer_name = ""
        if chosen:
            try:
                peer_name = _sanitize_peer_str(chosen.decode("utf-8"))
            except Exception:
                peer_name = ""
        # `named` is what tells the caller whether `peer_display` is the peer's
        # own answer or this machine's fallback guess: a caller that keeps a
        # name of its own (learned over the relay, say) must not have it
        # replaced by a truncation of a hostname it already knows better.
        named = bool(peer_name)
        peer_display = peer_name or label

        our_ip = getattr(self, "_our_ip", None) or _get_local_address()
        # Read off the instance rather than looked up: this runs on the
        # browser's own thread, and the set was refreshed by whichever pass last
        # built the advertisement — the same events that move it.
        address = _pick_best_address(candidates, our_ip, self._virtual_ips)

        with self._lock:
            existing = self._known_peers.get(peer_id_hash)
            self._service_to_peer[name] = peer_id_hash
            if existing is not None:
                # Refresh on re-announcement: the peer's address may have
                # changed (DHCP renewal, Wi-Fi reconnect, interface switch).
                # Previously we early-returned here, so a changed address was
                # never updated and peers became permanently unreachable.
                if (
                    existing.get("address") == address
                    and existing.get("port") == port
                    and existing.get("name") == peer_display
                    and existing.get("named") == named
                    # The version is part of "changed": a peer that upgrades
                    # re-announces from the same address and port, and that
                    # re-announcement is the only word this machine gets that
                    # the update landed.  Skipping it here left the device list
                    # showing the old version until something else moved.
                    and existing.get("version") == peer_version
                ):
                    return
                existing["address"] = address
                existing["port"] = port
                existing["name"] = peer_display
                existing["named"] = named
                existing["version"] = peer_version
                existing["os"] = peer_os
                existing["arch"] = peer_arch
                existing["app"] = peer_app
            else:
                self._known_peers[peer_id_hash] = {
                    "name": peer_display,
                    "named": named,
                    "address": address,
                    "port": port,
                    "version": peer_version,
                    "os": peer_os,
                    "arch": peer_arch,
                    "app": peer_app,
                }

        logger.info(
            "Discovered peer: %s at %s:%d (candidates: %s)",
            peer_display,
            address,
            port,
            candidates,
        )

        with self._lock:
            on_found = self._on_peer_found
        if on_found:
            # The advertised version/platform/arch/app travel with the sighting:
            # the device list shows them, and a peer on an older build of this
            # platform -- running this application -- is what the update offer is
            # for.  They were read above and kept here, but the callback dropped
            # them, so every consumer had to be told the device existed and then
            # ask a second time for what the first answer already carried.
            on_found(
                peer_id_hash,
                peer_display,
                address,
                port,
                peer_version,
                peer_os,
                peer_arch,
                peer_app,
                named,
            )

    def _handle_service_removed(self, name):
        """A peer said goodbye (an mDNS removal), so stop counting it as here.

        The other door out is ``_sweep_presence``, which is what decides a peer
        that stopped answering without saying anything — the case a goodbye
        cannot cover, because a device that is switched off, unplugged or out of
        range never sends one.
        """
        with self._lock:
            peer_id = self._service_to_peer.pop(name, None)
            if peer_id is None:
                return
            # A peer that renamed itself produces Removed(old-name) possibly
            # after Added(new-name): only declare it lost if no OTHER service
            # name still maps to it, otherwise a rename makes the device
            # "ghost offline" until the app restarts.
            if any(p == peer_id for p in self._service_to_peer.values()):
                logger.debug(
                    "Service %s removed but peer %s still advertised under "
                    "another name — not reporting loss",
                    name,
                    peer_id[:12],
                )
                return
            if peer_id in self._known_peers:
                del self._known_peers[peer_id]
            on_lost = self._on_peer_lost
        if peer_id:
            logger.info("Peer lost: %s", peer_id)
            if on_lost:
                on_lost(peer_id)

