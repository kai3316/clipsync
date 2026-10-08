"""Check for ClipSync updates against GitHub releases.

Uses only the standard library (urllib) so the app gains no new
dependency.  The check is best-effort and never raises: any network or
parse failure simply returns "no update available".
"""

import contextlib
import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable

from internal.i18n import T
from internal.version import __version__

logger = logging.getLogger(__name__)

_GITHUB_REPO = "kai3316/clipsync"
_LATEST_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_RELEASES_PAGE = f"https://github.com/{_GITHUB_REPO}/releases/latest"

# Two desktop applications are published from this repository and they share one
# release, so the same platform has two assets and only one of them is the
# application this process belongs to.  Which one is not a property of the OS --
# it is a property of the shell that started this process, and the shell is the
# only party that knows it: the Tauri host names itself in the environment when
# it spawns the sidecar, and the legacy application runs this code in its own
# process, where nothing sets the variable.
SHELL_ENV = "CLIPSYNC_SHELL"
SHELL_TAURI = "tauri"
SHELL_LEGACY = "legacy"


def running_shell() -> str:
    """Which desktop shell this process is running under.

    Anything unrecognised -- unset, empty, or a value from a future shell this
    build has never heard of -- reads as the legacy shell rather than as the
    Tauri one.  That direction is the safe one: the legacy asset names are the
    ones this code has always returned, so an unrecognised shell gets the
    behaviour it had before the distinction existed instead of a new one it
    cannot install.
    """
    return (
        SHELL_TAURI
        if (os.environ.get(SHELL_ENV) or "").strip().lower() == SHELL_TAURI
        else SHELL_LEGACY
    )


def _machine() -> tuple[str, bool]:
    """(``platform.system()``, whether this CPU is 64-bit ARM)."""
    import platform as _platform

    machine = (_platform.machine() or "").lower()
    return _platform.system(), ("aarch64" in machine or "arm64" in machine)


def _legacy_asset_name() -> str:
    """The legacy application's release asset for this platform.

    One fixed name per platform, written by the packaging job.  Intel macOS is
    no longer built, and the name this returns there matches no asset, so a
    download on such a machine reports "no release for this platform" rather
    than guessing at one.
    """
    system, is_arm = _machine()
    if system == "Darwin":
        return "clipsync-macos-arm64.zip" if is_arm else "clipsync-macos-x64.zip"
    if system == "Windows":
        return "clipsync-windows.zip"
    if is_arm:
        return "clipsync-linux-arm64.tar.gz"
    return "clipsync-linux.tar.gz"


def _asset_matchers() -> list[re.Pattern]:
    """Patterns this process's release asset must match, best first.

    The legacy bundles have one fixed name each, so their pattern is the name
    itself.  The Tauri bundles carry the version in the filename
    (``ClipSync_1.0.10_x64-setup.exe``), which no fixed string can name, so they
    are matched by shape.

    macOS answers with **two** patterns, and the order is the preference.  On Windows and Linux
    the payload a person installs and the payload the updater consumes are the same file
    (`ClipSync_1.0.10_x64-setup.exe`, the `.AppImage`), so one pattern covers both -- except on
    Linux ARM64, where the two bundles spell that architecture differently and each gets its own
    pattern (see the branch below).  On macOS they differ: the updater unpacks
    `ClipSync.app.tar.gz` over the installed bundle, while a person runs the `.dmg`.

    `ClipSync.app.tar.gz` used to be excluded on the reasoning that it "is not a file a person
    installs", which is true and was the wrong test -- this cache is not for people, it is what
    this machine serves to a peer with no route to GitHub, and on macOS the payload is the file
    its own updater fetches.  Excluding it left the cache unable to hold anything newer than a
    stale `.dmg`, which is what a 1.0.54 host was measured serving to a 1.0.33 peer.

    The list is also the tie-break `get_cached_asset` uses: version decides between two
    candidates first, and at the same version the payload (the first entry) wins -- so a
    newer `.dmg` still wins over an older payload, while a same-version payload still wins.
    """
    system, is_arm = _machine()
    if running_shell() != SHELL_TAURI:
        return [re.compile("^" + re.escape(_legacy_asset_name()) + "$")]
    if system == "Darwin":
        arch = "aarch64" if is_arm else "x64"
        return [
            # Both spellings the bundler has used: `<product>.app.tar.gz` and
            # `<product>_<version>_<arch>.app.tar.gz`.
            re.compile(r"^ClipSync(?:_[0-9][^/]*)?\.app\.tar\.gz$"),
            re.compile(rf"^ClipSync_.*_{arch}\.dmg$"),
        ]
    if system == "Windows":
        return [re.compile(r"^ClipSync_.*_%s-setup\.exe$" % ("arm64" if is_arm else "x64"))]
    if is_arm:
        # The two Linux bundles spell ARM64 differently: Tauri's AppImage target
        # carries the Rust target triple's `aarch64`, while the deb carries
        # Debian's own `arm64`.  The single shared suffix below matched neither,
        # so an ARM64 machine could not name its own update.  The order is the
        # same preference as the x86_64 list: the AppImage payload first.
        return [
            re.compile(r"^ClipSync_.*_aarch64\.AppImage$"),
            re.compile(r"^ClipSync_.*_arm64\.deb$"),
        ]
    return [re.compile(r"^ClipSync_.*_amd64\.(?:AppImage|deb)$")]


def _platform_asset_label() -> str:
    """What this platform's asset is called in a sentence to the user.

    The legacy shell's answer is its filename, because that is a thing the
    reader can look for on the releases page.  The Tauri shell has no single
    filename to give -- the version is part of it -- so it says what the file is
    instead.
    """
    if running_shell() != SHELL_TAURI:
        return _legacy_asset_name()
    system, is_arm = _machine()
    machine = {"Darwin": "macOS", "Windows": "Windows"}.get(system, "Linux")
    return f"ClipSync ({machine} {'arm64' if is_arm else 'x64'})"


def _select_asset(assets: list) -> dict | None:
    """This platform's asset from a release's asset list, or None.

    Patterns are tried in order and each is matched against every asset before
    the next is tried, so the first pattern is a genuine preference rather than
    a tie-break between whatever the API happened to list first.
    """
    for matcher in _asset_matchers():
        for asset in assets:
            if matcher.match(asset.get("name") or ""):
                return asset
    return None


def _https_context() -> ssl.SSLContext:
    """An SSL context with a usable CA store on macOS / frozen builds.

    macOS Python (and PyInstaller-frozen apps on any OS) often lack the OS
    trust store in OpenSSL's default paths, so a plain ``urlopen`` fails with
    ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`` —
    the "update check failed" error seen on macOS.  certifi ships its own
    ``cacert.pem`` (bundled by PyInstaller's hook); prefer it when present.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _parse_version(version: str) -> tuple:
    """Parse a version string into a tuple of ints for comparison.

    Handles "v1.0.4", "1.0.4", "1.0.4-beta" etc.  Trailing non-numeric
    segments are dropped.
    """
    version = (version or "").strip().lstrip("vV")
    parts = []
    for chunk in version.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            parts.append(int(num))
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    """Return True if `latest` is a higher version than `current`."""
    lv = _parse_version(latest)
    cv = _parse_version(current)
    # Pad with zeros so "1.0" vs "1.0.4" compares correctly.
    n = max(len(lv), len(cv))
    lv = lv + (0,) * (n - len(lv))
    cv = cv + (0,) * (n - len(cv))
    return lv > cv


def _version_in_name(name: str) -> tuple:
    """The version a release asset's filename carries, or ``()`` for none.

    The Tauri bundles name their version (``ClipSync_1.0.10_x64-setup.exe``) and
    the legacy bundles name nothing, so this is what tells two cached installers
    of the same application apart -- and it is read back with the same parser
    the comparison uses, so a name and a tag cannot disagree about which of two
    builds is later.
    """
    match = re.search(r"\d+(?:\.\d+)+", name or "")
    return _parse_version(match.group(0)) if match else ()


def is_newer(latest: str, current: str) -> bool:
    """Whether *latest* is a higher version than *current*.

    The public spelling of :func:`_is_newer`, for callers outside this module —
    a peer's advertised version is the same question as a release tag's, and it
    is answered by the same comparison or the two say different things about
    the same pair of builds."""
    return _is_newer(latest, current)


def version_in_asset_name(name: str) -> str:
    """The version an installer's filename carries, as a dotted string, or "".

    The counterpart of :func:`_version_in_name` for callers that need to *show*
    the version rather than rank by it: a peer-sent archive arrives with no
    release info behind it, and its filename is then the only thing that says
    which build it is.  Read through the same parser the ranking uses, so the
    name a card prints and the order the cache sorts by cannot disagree.
    """
    parts = _version_in_name(name)
    return ".".join(str(part) for part in parts) if parts else ""


def _describe(exc: Exception | None) -> str:
    """One line naming why the release lookup failed.

    An HTTP status is the failure whose cause is not in the exception's text:
    urllib says "HTTP Error 403: Forbidden" and drops the body, and the body is
    where GitHub puts "API rate limit exceeded for <ip>".  That sentence is the
    difference between "wait an hour" and "something here is blocking the API",
    so it is read back and included.

    This is the log's line, not the window's: it is GitHub's own English, and
    it ends with an aside aimed at developers.  What a reader is shown comes
    from :func:`_reason` and the catalog; this travels beside it as the detail.
    """
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", "replace")[:400]
        message = ""
        with contextlib.suppress(Exception):
            message = (json.loads(body).get("message") or "").strip()
        return f"HTTP {exc.code}: {message}" if message else f"HTTP {exc.code}"
    if exc is None:
        return ""
    return f"{type(exc).__name__}: {exc}"


# What the lookup's failures are, in the caller's terms.  The distinction that
# matters is which of them a reader can do something about, and a rate limit is
# the one that looks like a bug and is not: nothing is broken, the answer is
# "ask again later".
RATE_LIMITED = "rate_limited"
UNREACHABLE = "unreachable"
REFUSED = "refused"
MALFORMED = "malformed"

# The sentence each failure is said in.  Absent from here means the raw detail
# is all there is to say, which is true of a failure this module has no name
# for -- and a name for it would be a guess.
_REASON_KEYS = {
    RATE_LIMITED: "update.error_rate_limited",
    UNREACHABLE: "update.error_unreachable",
    REFUSED: "update.error_refused",
    MALFORMED: "update.error_malformed",
}


def _reason(exc: Exception | None, detail: str) -> str:
    """Which kind of failure *exc* is, as one of the codes above.

    A 403 is GitHub's spelling of both "you are over the rate limit" and "this
    is refused", and the body is what tells them apart -- the same body
    :func:`_describe` already read for its message.
    """
    if exc is None:
        return ""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429) and "rate limit" in detail.lower():
            return RATE_LIMITED
        return REFUSED
    if isinstance(exc, (urllib.error.URLError, OSError, TimeoutError)):
        return UNREACHABLE
    return ""


def _retryable(exc: Exception) -> bool:
    """Whether trying again could plausibly answer differently.

    A 4xx is a decision rather than a blip: GitHub answering 403 for a rate
    limit answers 403 again one second later, and the retry spends another
    request from the very budget the refusal is about.  That budget is 60
    requests an hour per IP, shared by everything behind that IP, so three
    attempts per click is a limit this app helps itself into -- and the third
    refusal is the one that gets shown.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return not 400 <= exc.code < 500
    return True


def _fetch_latest_release(
    timeout: float = 6.0, failure: list[tuple[str, str]] | None = None
) -> dict | None:
    """Fetch the latest GitHub release JSON, or None on any failure.

    Retried a couple of times with a short backoff so a single transient
    network blip (Wi-Fi dropout, DNS hiccup) doesn't surface as a hard
    "check failed" to a user who just clicked the tray item -- but only for a
    failure that a retry could clear (:func:`_retryable`).

    A caller that has to explain the failure passes ``failure`` -- a list it
    owns, appended with ``(reason, detail)`` saying what went wrong.  Without it
    the reason is only logged, and that is how "could not reach the update
    server" reached the user with no cause attached: the answer was known right
    here and discarded one frame up.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                _LATEST_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "clipsync",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout, context=_https_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_exc = exc
            if not _retryable(exc):
                break
            if attempt < 2:
                import time as _time

                _time.sleep(0.5 * (attempt + 1))
    detail = _describe(last_exc)
    logger.debug("Update check failed: %s", detail or last_exc)
    if failure is not None:
        failure.append((_reason(last_exc, detail), detail))
    return None


def _say(reason: str, detail: str) -> str:
    """The failure as a sentence in the active language, *detail* if unnamed."""
    key = _REASON_KEYS.get(reason)
    if key:
        text = T(key)
        if text != key:
            return text
    return detail


def check_for_update(timeout: float = 6.0) -> dict:
    """Query GitHub for the latest release and compare to our version.

    Returns a dict:
        {"available": bool, "latest": str, "current": str, "url": str,
         "error": str}
    On any failure (offline, rate-limited, parse error) returns
    {"available": False, "latest": "", "current": __version__,
     "url": <the releases page>, "error": "<why>"}.

    The reason is part of the answer rather than a log line.  A check the user
    asked for that comes back "could not reach the update server" and nothing
    else leaves them unable to tell a blocked network from a rate limit from a
    bug in here -- and the first of those is theirs to fix, not ours.
    """
    result = {
        "available": False,
        "latest": "",
        "current": __version__,
        "url": _RELEASES_PAGE,
        # `error` is a sentence to show; `reason` is the same failure as a code,
        # for a caller that wants to say it differently; `detail` is the raw
        # one-liner (GitHub's own words, or the OS's) for the log.
        "error": "",
        "reason": "",
        "detail": "",
    }
    failure: list[tuple[str, str]] = []
    data = _fetch_latest_release(timeout, failure)
    if not data:
        reason, detail = failure[0] if failure else ("", "")
        result["reason"] = reason
        result["detail"] = detail
        result["error"] = _say(reason, detail)
        logger.debug("Update check has no answer: %s (%s)", reason or "unclassified", detail)
        return result

    latest = (data.get("tag_name") or "").strip()
    if not latest:
        # A 200 with no tag in it.  Nothing the user can act on, but it is not
        # a network problem and must not be reported as one.
        result["reason"] = MALFORMED
        result["detail"] = "release has no tag_name"
        result["error"] = _say(MALFORMED, result["detail"])
        return result

    result["latest"] = latest
    result["available"] = _is_newer(latest, __version__)
    result["url"] = data.get("html_url") or _RELEASES_PAGE
    return result


def sha256_file(path: str) -> str:
    """Streaming SHA-256 of *path*, returned as lowercase hex."""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_latest_asset_info(timeout: float = 10.0) -> dict | None:
    """Return authoritative info about this platform's latest release asset.

    Queries the same GitHub release API as :func:`check_for_update` and picks
    this platform's asset.  Returns ``{"version": <tag>, "asset": <name>,
    "sha256": <hex>}`` or None when anything is missing — notably when GitHub
    publishes no ``sha256:`` digest for the asset (older releases), because a
    caller cannot then tell a good blob from a bad one.
    """
    try:
        data = _fetch_latest_release(timeout=timeout)
    except Exception as exc:  # never raises for callers on the install path
        logger.debug("fetch_latest_asset_info failed: %s", exc)
        return None
    if not data:
        return None
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    matched = _select_asset(data.get("assets") or [])
    asset_name = (matched or {}).get("name") or ""
    if not matched:
        logger.info("Latest release %s has no asset for %s", tag, _platform_asset_label())
        return None
    digest = matched.get("digest") or ""
    if not digest.startswith("sha256:") or len(digest) <= len("sha256:"):
        logger.info("Release %s has no sha256 digest for %s", tag, asset_name)
        return None
    return {
        "version": tag,
        "asset": asset_name,
        "sha256": digest[len("sha256:") :].lower(),
    }


def verify_update_blob(
    blob_path: str,
    release_info: dict | None,
    current_version: str,
    source: str = "p2p",
    peer_digest: str = "",
) -> tuple[bool, str]:
    """Decide whether an update blob may be installed.

    *blob_path* is a received (P2P) or downloaded asset; *release_info* is what
    :func:`fetch_latest_asset_info` returned (None = GitHub unreachable or no
    verifiable digest).  *source* is "p2p" for a peer-sent blob or "github"
    for a file that :func:`download_latest_release` already size- and
    hash-checked against the release API while downloading.  *peer_digest* is
    the SHA-256 the sending device declared for the same file.

    Policy: never install a package whose bytes do not match what it is being
    checked against, and never install one whose version is not newer than the
    running build.  The published digest is always preferred, and when it is
    available a mismatch is a rejection whatever the peer said.

    When the release endpoint cannot be reached there is no published digest to
    check against, and that is the one situation a peer-sent blob exists for —
    the sending device has the installer, this one may have no route to GitHub
    at all.  Refusing there would leave the feature unable to do the only thing
    it is for, so the peer's own digest is accepted instead: it is what catches
    a corrupt or truncated transfer, which is the part of the check that can be
    settled without the network.  Authenticity rests on the exchange's own
    gates — this machine asked *that* device for its cached asset (the ledger
    the caller armed by asking), the offer had to claim this platform and a
    newer version, and the link is TLS with a pinned certificate.

    Returns ``(ok, verdict)`` with verdict one of:
      "ok"             verified against the published digest — safe to stage
      "peer_verified"  verified against the sending device's digest only
      "no_release_info" P2P blob with no digest to check at all
      "hash_mismatch"  bytes differ from the digest they were checked against
      "not_newer"      release is not newer than the running version
    """
    if not release_info:
        # GitHub path: download_latest_release already verified size+sha256
        # against the release API before saving the file.
        if source == "github":
            return True, "ok"
        return _verify_against(blob_path, peer_digest, "peer_verified", current_version)

    if not _is_newer(release_info.get("version", ""), current_version):
        return False, "not_newer"

    expected = release_info.get("sha256") or ""
    if not expected:
        # No published digest to compare against.  The blob is still whatever
        # the peer sent, so it gets the peer's own check — never a free pass.
        if source == "github":
            return True, "ok"
        return _verify_against(blob_path, peer_digest, "peer_verified", current_version)

    if not _matches(blob_path, expected):
        return False, "hash_mismatch"
    return True, "ok"


def _matches(blob_path: str, expected: str) -> bool:
    """Whether *blob_path* hashes to *expected*; False when it cannot be read."""
    try:
        return sha256_file(blob_path) == expected.lower()
    except OSError as exc:
        logger.warning("Cannot hash update blob %s: %s", blob_path, exc)
        return False


def _verify_against(
    blob_path: str, digest: str, verdict: str, current_version: str = ""
) -> tuple[bool, str]:
    """Fallback check for a blob with no published digest: the sender's word.

    A peer too old to declare one leaves nothing to check, which is the one
    case that is still refused rather than trusted.

    The archive's name is the release asset's name, so it carries the version
    the sender claims to be sending.  Nothing else here can be checked against
    the published release, so that claim is held to the same rule a published
    one is: it has to be newer than what is running.
    """
    claimed = version_in_asset_name(os.path.basename(blob_path))
    if current_version and claimed and not _is_newer(claimed, current_version):
        return False, "not_newer"
    if not digest:
        return False, "no_release_info"
    if not _matches(blob_path, digest):
        return False, "hash_mismatch"
    return True, verdict


def _manifest_signature_for(asset_name: str, timeout: float = 10.0) -> str:
    """The release signature `latest.json` publishes for *asset_name*, or "".

    The manifest is the one place a signature is available without the updater plugin, so the
    download that already happens here can leave it beside the cached asset.  Without it this
    machine serves a signature-less installer and every peer that receives one falls back to a
    manual install -- `install_staged_update` requires `peer_verified` **and** a non-empty
    signature, or it calls `reveal_staged_update`, which opens the folder.  Measured: the Windows
    entry carries 420 bytes of signature.

    The entry is found by the URL it names rather than by rebuilding the platform key: that key
    carries the bundle target (`windows-x86_64-nsis`, `darwin-aarch64-app`), and a second copy of
    that mapping is a second thing to get wrong.  Any failure answers "" -- no signature is the
    behaviour that came before, not an error.
    """
    if not asset_name:
        return ""
    url = f"https://github.com/{_GITHUB_REPO}/releases/latest/download/latest.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "clipsync"})
        with urllib.request.urlopen(req, timeout=timeout, context=_https_context()) as resp:
            manifest = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("Could not read the release manifest for a signature: %s", exc)
        return ""
    for entry in (manifest.get("platforms") or {}).values():
        named = str((entry or {}).get("url") or "")
        if not named:
            continue
        if os.path.basename(named.split("?")[0]) != asset_name:
            continue
        return str((entry or {}).get("signature") or "").strip()
    logger.debug("The release manifest names no signature for %r", asset_name)
    return ""


def download_latest_release(
    dest_dir: str,
    progress_cb: Callable | None = None,
) -> tuple[str | None, str | None, str]:
    """Download the latest release asset for this platform into *dest_dir*.

    Reuses the same GitHub release lookup as :func:`check_for_update` and
    streams the matching asset to ``dest_dir/<asset-name>`` via urllib.
    *progress_cb* is called with ``(downloaded_bytes, total_bytes)`` after each
    64 KiB chunk (total may be unknown/0 for a few servers).

    Returns ``(saved_path, None, version)`` on success, or
    ``(None, reason, "")`` on failure where *reason* is a short human-readable
    (localized) message describing the problem — e.g. that no release asset
    exists for this platform.  Never raises.
    """
    import os

    from internal.i18n import T

    try:
        data = _fetch_latest_release(timeout=60.0)
        if not data:
            return None, T("web.update_server_unreachable"), ""
        assets = data.get("assets") or []
        if not assets:
            logger.warning("Latest release has no downloadable assets")
            return None, T("web.update_no_assets"), ""

        matched = _select_asset(assets)
        if not matched or not matched.get("browser_download_url"):
            label = _platform_asset_label()
            logger.warning("No download asset found for platform: %s", label)
            return None, T("web.update_no_release", name=label), ""
        asset_name = matched["name"]
        browser_url = matched["browser_download_url"]
        version = (data.get("tag_name") or "").strip()

        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, asset_name)
        # Download to a .part file and rename on success, so an interrupted
        # download never leaves a truncated file at the final installer path
        # (a leftover the user could double-click as if it were a real
        # release).
        temp_path = dest_path + ".part"
        req = urllib.request.Request(browser_url, headers={"User-Agent": "clipsync"})
        try:
            with urllib.request.urlopen(req, timeout=60.0, context=_https_context()) as resp:
                # Total size: prefer the response Content-Length, fall back to
                # the release API's asset size (a few servers omit the header).
                total = None
                try:
                    cl = resp.headers.get("Content-Length")
                    if cl:
                        total = int(cl)
                except (AttributeError, TypeError, ValueError):
                    total = None
                if not total:
                    total = matched.get("size")
                downloaded = 0
                with open(temp_path, "wb") as out:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb is not None:
                            try:
                                progress_cb(downloaded, total or 0)
                            except Exception:
                                logger.debug("Update progress callback failed", exc_info=True)
            # Verify the downloaded size AND SHA-256 against the release API, so
            # a truncated, corrupted, or tampered asset is rejected before it is
            # exposed as a valid installer. The API digest is "sha256:<hex>".
            #
            # Both are required rather than checked when present.  This is the
            # path with nobody to answer to on arrival: the caller above takes
            # "already size- and hash-checked while downloading" as the reason a
            # GitHub download needs no further verification, and that is only
            # true while a digest is published for the asset.  An asset the API
            # describes without one therefore fails the download, which is the
            # policy fetch_latest_asset_info already applies when it looks the
            # digest up — the two paths agree that a release nothing can verify
            # is not an installer.
            asset_size = matched.get("size")
            if not asset_size:
                raise RuntimeError("release asset has no published size")
            actual = os.path.getsize(temp_path)
            if actual != int(asset_size):
                raise RuntimeError(
                    f"download size mismatch: expected {asset_size}, got {actual}"
                )
            digest = matched.get("digest") or ""
            if not digest.startswith("sha256:"):
                raise RuntimeError("release asset has no published sha256 digest")
            actual_sha = sha256_file(temp_path)
            if actual_sha != digest[len("sha256:") :]:
                raise RuntimeError(
                    f"download checksum mismatch: expected {digest}, got sha256:{actual_sha}"
                )
            os.replace(temp_path, dest_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(temp_path)
            raise
        logger.info("Downloaded release asset to %s", dest_path)
        # Kept beside the asset so this machine can hand a peer something the peer can check
        # offline.  A failure here is not a download failure: the installer is on disk and
        # installable, and the only cost is that peers keep the manual path.  See
        # `_manifest_signature_for`.
        signature = _manifest_signature_for(asset_name)
        if signature:
            cache_signature(asset_name, signature)
        else:
            logger.debug("Downloaded %s with no signature to cache", asset_name)
        return dest_path, None, version
    except Exception as exc:
        logger.error("Release download failed: %s", exc)
        return None, T("web.update_download_failed", reason=exc), ""


def _cache_dir() -> str:
    """Directory where downloaded update assets are cached for P2P serving."""
    from internal.config.config import _config_dir

    return os.path.join(_config_dir(), "update_cache")


def cache_asset(asset_path: str, name: str = "") -> str | None:
    """Keep the verified *asset_path* in the update cache, for serving to a peer.

    *name* is the release asset's own filename, for a caller whose path does not
    end in one: the desktop downloads through a temporary file of the host's
    naming, so the name travels beside the path and is what the file is kept
    under.  It is checked against this process's own asset patterns before
    anything is copied, which is two things at once -- a file that is not an
    installer of *this* application never fills the cache, and a name that is
    not a filename never names a destination (`..`, a separator, a drive).

    The cache holds one installer of each application: a machine upgrades
    through one release after another, so the same name-pattern accumulates,
    and the older files are what a peer that is behind would be sent by
    :func:`get_cached_asset` if they were left.  The other application's asset
    is not this one's to delete -- one cache directory is shared by both, and a
    machine that has run the legacy app and the Tauri app has one of each here.

    Returns the cached path, or None on failure (never raises).
    """
    import shutil

    base = os.path.basename(name or asset_path)
    if not any(matcher.match(base) for matcher in _asset_matchers()):
        logger.warning("Refusing to cache %r: not an installer for this shell", base)
        return None
    try:
        d = _cache_dir()
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, base)
        # Copied rather than moved: the caller still owns the file.  The update
        # service stages the same download for the user to install by hand, and
        # that move has to find it where the download left it.
        shutil.copyfile(asset_path, dest)
        # Keep the newest of each pattern, and never delete the one file this shell's own updater
        # consumes.  Without that protection the sweep makes the fix above fire on itself: the
        # payload lands, the sweep treats it as a sibling of the .dmg and removes it, and the
        # stale .dmg is the only candidate again.
        keep = _updater_payload_pattern()
        for other in os.listdir(d):
            if other == base or not any(m.match(other) for m in _asset_matchers()):
                continue
            if keep is not None and keep.match(other) and not keep.match(base):
                continue
            if _cache_rank(os.path.join(d, other)) > _cache_rank(dest):
                continue
            with contextlib.suppress(OSError):
                os.remove(os.path.join(d, other))
            # The signature is kept beside the asset it signs, so it goes when the asset goes.
            # The sweep used to leave it: `.sig` is not an installer, so the filter above skipped
            # it, and the cache collected one 420-byte orphan per release.  Found on a real machine
            # as "里面有好多不同版本的更新包，旧的都保留了" -- the installers had in fact been
            # swept correctly, and what remained were those stale signatures, which is why it
            # looked as though nothing was being cleaned up.
            with contextlib.suppress(OSError):
                os.remove(os.path.join(d, other + ".sig"))
        # And a signature whose asset is already gone, which an older build left.  Nothing else will
        # ever reap these, and they are cheap to find.
        for orphan in os.listdir(d):
            if not orphan.endswith(".sig"):
                continue
            if os.path.exists(os.path.join(d, orphan[: -len(".sig")])):
                continue
            with contextlib.suppress(OSError):
                os.remove(os.path.join(d, orphan))
        logger.info("Cached update asset at %s", dest)
        return dest
    except OSError as exc:
        logger.warning("Failed to cache update asset: %s", exc)
        return None


def _updater_payload_pattern() -> re.Pattern | None:
    """The pattern of the file *this* shell's updater downloads, or None if it is the installer.

    Only macOS differs: its updater unpacks `ClipSync.app.tar.gz`, where Windows and Linux update
    from the same file a person installs.  Named as a function rather than a constant because the
    answer depends on the platform this process is running on.
    """
    if running_shell() != SHELL_TAURI:
        return None
    system, _is_arm = _machine()
    if system != "Darwin":
        return None
    return re.compile(r"^ClipSync(?:_[0-9][^/]*)?\.app\.tar\.gz$")


def _cache_rank(path: str) -> tuple:
    """How a cached asset is ordered against its siblings: version, then age."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return (_version_in_name(os.path.basename(path)), mtime)


def get_cached_asset() -> str | None:
    """The cached update asset for this platform and shell, or None.

    Looked up by pattern rather than by name because the Tauri bundles carry the
    version in their filename, and because one cache directory can hold both
    applications' assets: a machine that has run the legacy app and the Tauri
    app has one of each here, and serving the wrong one to a peer of the same
    platform is the mistake this naming exists to prevent.  The peer checks what
    it is sent against its own platform's digest before it will install it, so a
    mismatch is caught there too -- but it costs a transfer to find out.

    Candidates are ranked across *all* patterns, not pattern by pattern.  The
    pattern order used to decide first, and on macOS that made an older
    `ClipSync.app.tar.gz` beat a newer `.dmg`: the payload's pattern is the
    preferred one, and `cache_asset` protects the payload from the deletion
    sweep, so both files can be present at once.  Version now decides first; the
    pattern's own preference only breaks a version tie (the updater payload
    first), and mtime breaks that.

    Sorting the names would be the obvious way and is the wrong one: it is the
    text that is compared, and "1.0.9" is greater than "1.0.10" as text.  So the
    version in the name is parsed back out, and a file whose name carries no
    version at all -- the legacy bundles -- ranks below any that does.
    """
    directory = _cache_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    best: tuple[tuple, str] | None = None
    for preference, matcher in enumerate(_asset_matchers()):
        for name in names:
            path = os.path.join(directory, name)
            if not matcher.match(name) or not os.path.isfile(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            # Version first; a tie is settled by the pattern's own preference
            # (lower index = the payload this shell's updater consumes) and
            # only then by mtime.
            rank = (_version_in_name(name), -preference, mtime)
            if best is None or rank > best[0]:
                best = (rank, path)
    return best[1] if best else None


def cache_signature(asset_name: str, signature: str) -> bool:
    """Keep the release's minisign signature beside the cached asset.

    The signature is what lets a peer that cannot reach the release manifest
    still decide the bytes came from the release signing key, so it is cached
    under ``<asset>.sig`` next to the asset it signs and served with it (see
    ``LanRuntime._serve_cached_update``).  The name is checked against this
    shell's own asset patterns for the same reason :func:`cache_asset` checks
    it: a name that is not one of our installers must not become a path under
    the cache directory.  Returns False when there is nothing to keep.
    """
    base = os.path.basename(str(asset_name or ""))
    if not base or not any(matcher.match(base) for matcher in _asset_matchers()):
        return False
    text = str(signature or "").strip()
    if not text:
        return False
    try:
        directory = _cache_dir()
        os.makedirs(directory, exist_ok=True)
        dest = os.path.join(directory, base + ".sig")
        temp = dest + ".tmp"
        with open(temp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp, dest)
        logger.info("Cached update signature at %s", dest)
        return True
    except OSError:
        logger.warning("Failed to cache the signature for %s", base, exc_info=True)
        return False


def get_cached_signature(asset_name: str) -> str:
    """The minisign signature cached beside *asset_name*, or "" when none."""
    base = os.path.basename(str(asset_name or ""))
    if not base:
        return ""
    try:
        with open(os.path.join(_cache_dir(), base + ".sig"), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""
