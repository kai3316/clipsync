"""mDNS service discovery for finding ClipSync peers on the LAN.

Registers this device as a _clipsync._tcp service and discovers
other devices running ClipSync on the same local network.
"""

import contextlib
import json
import logging
import platform
import socket
import subprocess
import threading
from collections.abc import Callable

from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

from internal.platform import decode_console_output
from internal.system.updater import running_shell
from internal.transport.ids import peer_id_hash
from internal.transport.ids import sanitize_peer_str as _sanitize_peer_str
from internal.version import __version__

logger = logging.getLogger(__name__)

# Grace window after ``start_browsing()`` during which a previously-known
# peer must re-announce to stay "online".  Peers that do not re-announce
# vanished while browsing was paused and are reported lost (see
# ``_reconcile_after_resume``).
RECONFIRM_GRACE_SECONDS = 4.0

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
        # Privacy: only advertise RFC1918 private LAN addresses.  Public,
        # VPN and virtual-adapter addresses are never useful for LAN discovery
        # and would expose more of the machine's network topology than needed.
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

    # Cap the advertised set: more IPs than this means unusual network
    # topology (many adapters) — advertising them all only broadens exposure.
    MAX_ADVERTISED_IPS = 10  # noqa: N806
    if len(addresses) > MAX_ADVERTISED_IPS:
        addresses = addresses[:MAX_ADVERTISED_IPS]

    if not addresses:
        logger.warning("Failed to enumerate local addresses")
    return addresses


def _get_interface_priorities():
    priorities = {}
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-NetIPAddress -AddressFamily IPv4 "
                    "| Select-Object IPAddress, InterfaceAlias "
                    "| ConvertTo-Json",
                ],
                capture_output=True,
                timeout=5,
                # Raw bytes + decode_console_output: InterfaceAlias is
                # localized ("以太网") and PowerShell writes it in the console
                # output codepage, while text=True would decode with the ANSI
                # one.  That mismatch raised UnicodeDecodeError inside
                # subprocess's reader thread and returned EMPTY output, so IP
                # detection silently found nothing; guessing the wrong codepage
                # instead mangles the alias.  Only IPAddress is matched below.
                # No console window: in the packaged (console=False) Windows
                # build, spawning the console-mode powershell.exe without this
                # flag flashes a black console box on every startup.
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                return priorities
            entries = json.loads(decode_console_output(result.stdout))
            if isinstance(entries, dict):
                entries = [entries]
            for e in entries:
                ip = e.get("IPAddress", "")
                iface = e.get("InterfaceAlias", "").lower()
                if not ip:
                    continue
                if any(k in iface for k in ("ethernet", "eth", "local area")):
                    priorities[ip] = 0
                elif any(k in iface for k in ("wi-fi", "wlan", "wireless", "wifi")):
                    priorities[ip] = 1
                elif any(k in iface for k in ("vpn", "tunnel", "tap", "ppp", "teredo")):
                    priorities[ip] = 2
                else:
                    priorities[ip] = 3
        else:
            result = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
            iface = ""
            for line in result.stdout.splitlines():
                if line and line[0] not in ("\t", " "):
                    iface = line.split(":")[0].split()[0].lower()
                elif "inet " in line and iface:
                    parts = line.strip().split()
                    try:
                        idx = parts.index("inet")
                        ip = parts[idx + 1]
                        if any(k in iface for k in ("eth", "en")):
                            priorities[ip] = 0
                        elif any(k in iface for k in ("wlan", "wl", "wi-fi")):
                            priorities[ip] = 1
                        elif any(k in iface for k in ("tun", "tap", "vpn", "ppp", "utun")):
                            priorities[ip] = 2
                        else:
                            priorities[ip] = 3
                    except (ValueError, IndexError):
                        pass
    except Exception:
        pass
    return priorities


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


def _pick_best_address(candidates: list[str], our_ip: str) -> str:
    """Choose the remote address most likely reachable on the LAN.

    Prefers an address on the same /24 subnet as our own (peers on the same
    network share that prefix), then any private address, then the rest.
    Falls back to the first candidate on a tie.
    """

    def _subnet(ip: str) -> str:
        parts = ip.split(".")
        return ".".join(parts[:3]) if len(parts) == 4 else ip

    our_sub = _subnet(our_ip)

    def rank(ip: str) -> tuple:
        if _subnet(ip) == our_sub:
            return (0,)
        if _is_private_ip(ip):
            return (1,)
        return (2,)

    # Tie-break by the address string, not the list index — mDNS address order
    # is unstable across announcements, so index-based tie-breaking made the
    # chosen address flap between two same-subnet interfaces.
    return min(candidates, key=lambda ip: (rank(ip), ip))


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
        self._on_peer_found: Callable | None = None
        self._on_peer_lost: Callable | None = None
        self._lock = threading.Lock()
        self._known_peers: dict[str, dict] = {}  # peer_id -> info
        self._service_to_peer: dict[str, str] = {}  # service_name -> peer_id
        self._our_ip = "127.0.0.1"
        # Network-change watcher state: the set of addresses we advertised,
        # plus a stop signal for the light poll in _network_watch_loop.
        self._netmon_stop = threading.Event()
        self._advertised_ips: frozenset[str] = frozenset()
        # Post-resume reconcile bookkeeping (see start_browsing /
        # _reconcile_after_resume): peers re-confirmed during the grace
        # window + the timer that reports the rest lost.
        self._reconfirm: set[str] = set()
        self._reconcile_timer: threading.Timer | None = None

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

    def _service_props(self) -> dict[bytes, bytes]:
        """The TXT record we publish, for both registrations.

        Built in one place because it is published twice — once by ``start``
        and again by every ``start_advertising`` — and a field added to one
        copy only would be there or not depending on whether this device had
        been hidden and shown again.
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
        for i, ip in enumerate(get_all_local_addresses()):
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

        props = self._service_props()
        all_ips = get_all_local_addresses()
        local_ip = _get_local_address()
        self._our_ip = local_ip
        logger.info("Registering mDNS on %s (all IPs: %s)", local_ip, all_ips)

        # Register our service – use a truncated display name so the
        # real hostname is not broadcast in plaintext on the LAN.
        #
        # Advertise ALL local addresses (not just the "best" one) so peers
        # on any of our subnets can reach us. Previously we only advertised
        # a single IP chosen by a fragile interface heuristic, which broke
        # discovery whenever that IP belonged to a VPN/virtual adapter.
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

        # Browse for peers
        self._browser = ServiceBrowser(
            self._zc,
            self._service_type,
            handlers=[self._on_service_state_change],
        )
        logger.info("Started browsing for peers")

        # Remember the advertised address set and start the watcher that
        # re-registers when the local interfaces change (Wi-Fi <-> Ethernet,
        # VPN toggle, DHCP renewal moving us to another subnet) — those
        # produce no sleep/wake event, so without this the stale mDNS
        # advertisement keeps pointing peers at a dead address.
        self._advertised_ips = frozenset(all_ips)
        self._netmon_stop.clear()
        threading.Thread(
            target=self._network_watch_loop,
            daemon=True,
            name="clipsync-netwatch",
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
        if self._browser:
            self._browser.cancel()
            self._browser = None
            logger.info("Stopped browsing for peers")
        # A pending post-resume reconcile belongs to the browse session we are
        # pausing — cancel it so a later start_browsing() starts a fresh one
        # instead of stacking timers.
        if self._reconcile_timer is not None:
            self._reconcile_timer.cancel()
            self._reconcile_timer = None

    def start_browsing(self):
        """Resume discovering new peers. Requires start() to have been called."""
        if self._browser is not None:
            return
        if self._zc is None:
            return
        with self._lock:
            # Snapshot the peers known before the pause: any that do NOT
            # re-announce within the grace window vanished while we were not
            # listening and must be reported lost (a paused browser fires no
            # Removed events, so they would otherwise ghost forever).
            stale = set(self._known_peers)
            self._reconfirm.clear()
            # The new browser re-fires Added for every live service, so the
            # name→id mapping is rebuilt from scratch.
            self._service_to_peer.clear()
        self._browser = ServiceBrowser(
            self._zc,
            self._service_type,
            handlers=[self._on_service_state_change],
        )
        if self._reconcile_timer is not None:
            self._reconcile_timer.cancel()
        self._reconcile_timer = threading.Timer(
            RECONFIRM_GRACE_SECONDS,
            self._reconcile_after_resume,
            args=(stale,),
        )
        self._reconcile_timer.daemon = True
        self._reconcile_timer.start()
        logger.info("Resumed browsing for peers")

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
        props = self._service_props()
        all_ips = get_all_local_addresses()
        local_ip = _get_local_address()
        self._our_ip = local_ip
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
            self._advertised_ips = frozenset(all_ips)
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

    def _handle_service_added(self, zeroconf, service_type, name):
        info = zeroconf.get_service_info(service_type, name)
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
        address = _pick_best_address(candidates, our_ip)

        with self._lock:
            existing = self._known_peers.get(peer_id_hash)
            self._service_to_peer[name] = peer_id_hash
            # This peer just re-announced — the post-resume reconcile timer
            # must not report it lost.
            self._reconfirm.add(peer_id_hash)
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

    def _reconcile_after_resume(self, stale: set):
        """Report lost any peer that did not re-announce after a browse resume.

        Fires on the ``RECONFIRM_GRACE_SECONDS`` timer armed by
        ``start_browsing()``.  ``_handle_service_added`` adds each live peer to
        ``self._reconfirm`` as it re-announces, so ``stale - _reconfirm`` is
        exactly the set of peers that vanished while browsing was paused.  They
        leave ``_known_peers`` and fire ``on_lost`` — same effect a Removed
        event would have had had we been listening.
        """
        lost: list[str] = []
        with self._lock:
            self._reconcile_timer = None
            gone = stale - self._reconfirm
            for pid in gone:
                if pid in self._known_peers:
                    del self._known_peers[pid]
                lost.append(pid)
            # Drop any service-name mapping still pointing at a lost peer so a
            # later Added for the same name starts clean.
            for name in [n for n, pid in self._service_to_peer.items() if pid in gone]:
                self._service_to_peer.pop(name, None)
            on_lost = self._on_peer_lost
        for pid in lost:
            logger.info("Peer lost after browse resume: %s", pid)
            if on_lost:
                try:
                    on_lost(pid)
                except Exception:
                    logger.debug("on_lost callback failed for %s", pid, exc_info=True)
