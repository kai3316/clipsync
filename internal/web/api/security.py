"""Path/URL safety helpers for the web API.

The web API is reachable from any LAN client holding the bearer token, so
filesystem paths supplied by clients must be confined to ClipSync-owned
directories.  Without this, a token holder could point an import/send
handler at any file on the host (e.g. ``~/.ssh/id_rsa``) and read it back.
"""

import ipaddress
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

# URL schemes the /api/nav endpoint is allowed to open locally.  Everything
# else (file://, javascript:, data:, …) is rejected to prevent a token holder
# from driving local desktop behaviour beyond the app's intent.
_ALLOWED_NAV_SCHEMES = {"http", "https"}

# Network ranges an outbound request must never resolve into.  The user names
# the translation endpoint explicitly and a self-hosted LibreTranslate lives on
# a home network or on the machine itself, so private space and loopback are
# legitimate destinations and are allowed.
#
# What is not: link-local, which is where every cloud metadata service sits
# (169.254.169.254), and the multicast/reserved/unspecified blocks that are
# never a service address.  The other half of this defence is refusing
# redirects — see ``_NoRedirect`` — because a redirect names a second
# destination this check never saw.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::ffff:169.254.0.0/112",  # IPv4-mapped link-local (cloud metadata)
        "64:ff9b::/96",  # NAT64
        "ff00::/8",
    )
)


def confine_path(path: str, root: Path) -> Path | None:
    """Resolve *path* and return it only if it lies within *root*.

    Returns ``None`` (rejected) for empty input or any path outside *root*,
    including those that escape via ``..`` or symlinks.
    """
    if not path:
        return None
    try:
        resolved = Path(os.path.realpath(os.path.expanduser(path)))
        root_resolved = Path(os.path.realpath(root))
    except (OSError, ValueError):
        return None
    if resolved.is_relative_to(root_resolved):
        return resolved
    return None


def is_safe_nav_url(url: str) -> bool:
    """Return True only for http/https URLs."""
    if not url:
        return False
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    return scheme in _ALLOWED_NAV_SCHEMES


def _real(path) -> Path:
    """realpath + expanduser, or something that compares unequal to anything."""
    try:
        return Path(os.path.realpath(os.path.expanduser(str(path))))
    except (OSError, ValueError):
        return Path(os.path.join(os.sep, "\0unresolvable"))


def _system_roots() -> list[Path]:
    """Places no user data directory should ever be.

    On Windows the environment variables are the authoritative location, but
    they can be absent — a service context, a stripped environment — and the
    blocklist would then be empty, which is the one outcome that must not
    happen.  The well-known defaults are therefore added alongside whatever
    the environment says.
    """
    out: list[Path] = []
    if os.name == "nt":
        for var in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(var)
            if value:
                out.append(_real(value))
        drive = os.path.splitdrive(os.path.expanduser("~"))[0] or os.path.splitdrive(
            os.environ.get("SYSTEMROOT", "")
        )[0]
        if not drive:
            drive = "C:"
        out.extend(_real(p) for p in (f"{drive}\\Windows", f"{drive}\\Program Files"))
    else:
        out.extend(
            _real(p)
            for p in (
                "/etc",
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
                "/var",
                "/sys",
                "/proc",
                "/dev",
                "/boot",
                "/root",
                "/System",
                "/Library",
                "/private",
            )
        )
    return out


def _other_user_homes() -> list[Path]:
    """Home directories that are not this user's."""
    home = _real(os.path.expanduser("~"))
    bases = []
    if os.name == "nt":
        parent = home.parent
        if parent != home:
            bases.append(parent)
    else:
        bases.extend([Path("/home"), Path("/Users")])
    out: list[Path] = []
    for base in bases:
        try:
            entries = list(base.iterdir())
        except (OSError, ValueError):
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            resolved = _real(entry)
            if resolved != home:
                out.append(resolved)
    return out


# `_system_roots()` and `_other_user_homes()` walk the drive root / the users
# directory and resolve every entry, and `is_safe_receive_dir` runs on every
# request that touches the upload directory — including the file panel's
# polling.  Cached for a short window: the set changes when a user account is
# added or the drive layout changes, never inside a session.
_HOST_ROOTS_TTL = 60.0
_host_roots_cache: tuple[float, tuple[list[Path], list[Path]]] | None = None
_host_roots_lock = threading.Lock()


def _host_roots() -> tuple[list[Path], list[Path]]:
    with _host_roots_lock:
        now = time.monotonic()
        cached = _host_roots_cache
        if cached is not None and now - cached[0] < _HOST_ROOTS_TTL:
            return cached[1]
        roots = (_system_roots(), _other_user_homes())
        globals()["_host_roots_cache"] = (now, roots)
        return roots


def is_safe_receive_dir(path: str) -> tuple[bool, str]:
    """Whether *path* may become the directory received files land in.

    Empty means "not configured" and is accepted — the caller falls back to
    the default.  Anything non-empty is refused when it is a system
    directory, a drive/filesystem root, another user's home, or a dotfile
    directory inside this user's home (``~/.ssh``, ``~/.aws``, ``~/.gnupg``).

    This is the check ``file_receive_dir`` never had.  Every read and write
    handler confines itself to that directory, which is the right shape, but
    the directory itself was settable to anything — so the confinement marked
    the boundary of a directory the client chose, and a token holder could
    pick ``~/.ssh`` and then list, download, overwrite and delete it through
    the ordinary file endpoints.

    It is a blocklist rather than an allowlist on purpose: a receive
    directory on another drive or in a project folder is an ordinary thing
    for the user to want, and confining this to ``~`` would refuse it.
    """
    text = (path or "").strip()
    if not text:
        return True, ""
    resolved = _real(text)

    # A filesystem root ("C:\", "/") is refused: not a directory anyone keeps
    # uploads in, and it is what a too-short or too-clever value resolves to.
    try:
        if resolved == _real(resolved.anchor):
            return False, "filesystem_root"
    except (OSError, ValueError):
        return False, "unresolvable"

    # The home directory itself is refused.  ``~`` is not a place anyone keeps
    # uploads in, and every handler confines itself to *inside* the receive
    # directory — so a receive directory of ``~`` confines nothing and leaves
    # ``~/.ssh``, ``~/.aws`` and ``~/.gnupg`` listable, downloadable and
    # deletable through the ordinary file endpoints.  It also has to be refused
    # explicitly: the hidden-directory check below reads the path *relative* to
    # home, and for ``~`` itself that is empty, so nothing there could catch it.
    home = _real(os.path.expanduser("~"))
    if resolved == home:
        return False, "home_directory"

    system_roots, other_homes = _host_roots()
    for root in system_roots:
        if resolved.is_relative_to(root):
            return False, "system_directory"
    for root in other_homes:
        if resolved.is_relative_to(root):
            return False, "other_users_home"

    # Inside this home, dotfile directories hold credentials and keys; a
    # non-hidden directory is an ordinary place to put downloads.
    if resolved.is_relative_to(home):
        try:
            relative = resolved.relative_to(home)
        except ValueError:
            relative = None
        if relative is not None and any(part.startswith(".") for part in relative.parts):
            return False, "hidden_directory"

    return True, ""


def is_safe_outbound_url(url: str) -> tuple[bool, str]:
    """Whether the app may make an outbound request to *url*.

    Rejects anything but http/https, and any host whose DNS answers land in
    link-local (every cloud metadata service), multicast or reserved space.
    Every resolved address is checked, not just the first: a name that answers
    with both a public and a private address is still a way in.

    Private space and loopback are deliberately allowed — the endpoint is one
    the user typed, and a self-hosted translation service sits on exactly those
    addresses.  A redirect is not, which is why the caller must also refuse
    them (``_NoRedirect``).

    Returns ``(True, "")`` or ``(False, reason)``.
    """
    text = (url or "").strip()
    if not text:
        return False, "empty"
    parts = urlsplit(text)
    if parts.scheme.lower() not in _ALLOWED_NAV_SCHEMES:
        return False, "scheme"
    host = parts.hostname
    if not host:
        return False, "no_host"
    try:
        # ``.port`` raises ValueError on anything that is not a number in
        # 0..65535 — a value the config file can perfectly well hold, and one
        # that would otherwise escape as a 500 on every request that reads it.
        port = parts.port
    except ValueError:
        return False, "bad_port"
    if port is not None and not (0 < port < 65536):
        return False, "bad_port"
    try:
        answers = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError, ValueError):
        return False, "unresolvable"
    if not answers:
        return False, "unresolvable"
    for _family, _type, _proto, _canon, sockaddr in answers:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False, "unresolvable"
        # Normalised first: an IPv4-mapped IPv6 literal (::ffff:169.254.169.254)
        # is an IPv6Address, and comparing one against an IPv4 network is
        # silently False — so writing the metadata service in that form walked
        # straight past the blocklist.
        # ``ipv4_mapped`` exists only on an IPv6Address, which is exactly the
        # case that needs it.
        addr = getattr(addr, "ipv4_mapped", None) or addr
        if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
            return False, "reserved_address"
        if any(addr in net for net in _BLOCKED_NETWORKS):
            return False, "private_address"
    return True, ""
