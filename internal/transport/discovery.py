"""mDNS service discovery for finding ClipSync peers on the LAN.

Registers this device as a _clipsync._tcp service and discovers
other devices running ClipSync on the same local network.
"""

import json
import logging
import platform
import socket
import subprocess
import threading
from collections.abc import Callable

from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

from internal.platform import decode_console_output
from internal.transport.ids import peer_id_hash
from internal.transport.ids import sanitize_peer_str as _sanitize_peer_str
from internal.version import __version__

logger = logging.getLogger(__name__)

# Grace window after ``start_browsing()`` during which a previously-known
# peer must re-announce to stay "online".  Peers that do not re-announce
# vanished while browsing was paused and are reported lost (see
# ``_reconcile_after_resume``).
RECONFIRM_GRACE_SECONDS = 4.0


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

    # 2. Hostname-resolved addresses (Windows usually returns all adapters).
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            _add(info[4][0])
    except Exception:
        pass

    # 3. FQDN-resolved addresses (covers bare-hostname /etc/hosts gaps).
    try:
        for info in socket.getaddrinfo(socket.getfqdn(), None, family=socket.AF_INET):
            _add(info[4][0])
    except Exception:
        pass

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
        # Truncate the visible part (the real hostname is deliberately NOT
        # broadcast in plaintext on the LAN) and suffix a short id-hash so
        # two devices whose names share their first 8 characters register
        # distinct, non-colliding instances instead of fighting over one
        # mDNS name and resolving to each other.
        base = device_name[:8] if device_name else "ClipSync"
        self._display_name = f"{base}-{self._device_id_hash[:4]}"
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
        on_found(device_id, device_name, address, port)
        on_lost(device_id)
        """
        with self._lock:
            self._on_peer_found = on_found
            self._on_peer_lost = on_lost

    def start(self):
        """Register our service and start browsing for peers."""
        try:
            self._zc = Zeroconf()
        except Exception as e:
            logger.error("Failed to initialize mDNS: %s", e)
            return

        # Build properties – use hashed device_id to avoid exposing the
        # real device identity in plaintext mDNS TXT records.
        props = {
            b"device_id_hash": self._device_id_hash.encode("utf-8"),
            b"v": __version__.encode("utf-8"),
            b"os": platform.system().lower().encode("utf-8"),
            b"arch": (platform.machine() or "").lower().encode("utf-8"),
        }

        all_ips = get_all_local_addresses()
        for i, ip in enumerate(all_ips):
            props[f"alt_ip_{i}".encode()] = ip.encode()

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
        """Re-advertise when the local IP set changes (no sleep involved).

        Sleep/wake recovery rides the transport's wake callback; an ordinary
        interface switch does not.  A cheap socket-level enumeration every
        30 s notices a changed address set and rebuilds the registration with
        the fresh addresses, so peers can find us again immediately instead
        of only after an app restart.
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
                "Local addresses changed (%s -> %s) — re-registering mDNS",
                sorted(self._advertised_ips),
                sorted(current),
            )
            # Do NOT record `current` here: start_advertising() stamps
            # _advertised_ips only when zeroconf actually accepts the new
            # registration.  Claiming it up front made a failed re-register
            # permanent -- the next tick saw current == _advertised_ips and
            # never tried again, leaving peers pointed at the dead address.
            self._wake_recovery()

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
        props = {
            b"device_id_hash": self._device_id_hash.encode("utf-8"),
            b"v": __version__.encode("utf-8"),
            b"os": platform.system().lower().encode("utf-8"),
            b"arch": (platform.machine() or "").lower().encode("utf-8"),
        }
        all_ips = get_all_local_addresses()
        for i, ip in enumerate(all_ips):
            props[f"alt_ip_{i}".encode()] = ip.encode()
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
        """Re-register the mDNS service after wake-from-sleep.

        After sleep, network interfaces may have changed and the mDNS
        registration may be stale. We rebuild the service info with fresh
        addresses rather than re-registering the stale one, so other devices
        can discover us again on the new network.
        """
        if not self._zc:
            return
        with self._lock:
            info, self._service_info = self._service_info, None
        try:
            if info is not None:
                self._zc.unregister_service(info)
        except Exception:
            pass
        self.start_advertising()

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
        # Derive a privacy-safe display name from the service name.
        # The service name is e.g. "<display_name>._clipsync._tcp.local."
        try:
            peer_display = _sanitize_peer_str(name.split(".")[0])
        except (IndexError, TypeError):
            peer_display = peer_id_hash

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
                ):
                    return
                existing["address"] = address
                existing["port"] = port
                existing["name"] = peer_display
                existing["version"] = peer_version
                existing["os"] = peer_os
                existing["arch"] = peer_arch
            else:
                self._known_peers[peer_id_hash] = {
                    "name": peer_display,
                    "address": address,
                    "port": port,
                    "version": peer_version,
                    "os": peer_os,
                    "arch": peer_arch,
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
            on_found(peer_id_hash, peer_display, address, port)

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
