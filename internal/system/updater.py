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

    ``ClipSync.app.tar.gz`` is deliberately not among them even on macOS, where
    it is published: it is that application's *update payload*, which its own
    updater plugin unpacks over the installed bundle, and it is not a file a
    person installs.  What a user is handed here -- the .dmg, the -setup.exe,
    the AppImage -- is the thing they can actually run.
    """
    system, is_arm = _machine()
    if running_shell() != SHELL_TAURI:
        return [re.compile("^" + re.escape(_legacy_asset_name()) + "$")]
    if system == "Darwin":
        return [re.compile(r"^ClipSync_.*_%s\.dmg$" % ("aarch64" if is_arm else "x64"))]
    if system == "Windows":
        return [re.compile(r"^ClipSync_.*_%s-setup\.exe$" % ("arm64" if is_arm else "x64"))]
    return [re.compile(r"^ClipSync_.*_%s\.(?:AppImage|deb)$" % ("aarch64" if is_arm else "amd64"))]


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


def is_newer(latest: str, current: str) -> bool:
    """Whether *latest* is a higher version than *current*.

    The public spelling of :func:`_is_newer`, for callers outside this module —
    a peer's advertised version is the same question as a release tag's, and it
    is answered by the same comparison or the two say different things about
    the same pair of builds."""
    return _is_newer(latest, current)


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
) -> tuple[bool, str]:
    """Decide whether an update blob may be installed.

    *blob_path* is a received (P2P) or downloaded asset; *release_info* is what
    :func:`fetch_latest_asset_info` returned (None = GitHub unreachable or no
    verifiable digest).  *source* is "p2p" for a peer-sent blob or "github"
    for a file that :func:`download_latest_release` already size- and
    hash-checked against the release API while downloading.

    Policy (correctness, not hardening): never install a package whose bytes
    do not match the published asset, and never install one whose version is
    not newer than the running build.  When no release info is available at
    all, a P2P blob has nobody to answer to — reject it so the caller can fall
    back to the GitHub path; a freshly downloaded GitHub file was already
    verified against the API during download, so it may proceed.

    Returns ``(ok, verdict)`` with verdict one of:
      "ok"             verified and newer — safe to stage/apply
      "no_release_info" P2P blob with no authoritative reference
      "hash_mismatch"  bytes differ from the published asset
      "not_newer"      release is not newer than the running version
    """
    if not release_info:
        # GitHub path: download_latest_release already verified size+sha256
        # against the release API before saving the file.
        if source == "github":
            return True, "ok"
        return False, "no_release_info"

    if not _is_newer(release_info.get("version", ""), current_version):
        return False, "not_newer"

    expected = release_info.get("sha256") or ""
    if not expected:
        # No digest to compare against — treat like missing release info.
        if source == "github":
            return True, "ok"
        return False, "no_release_info"
    try:
        actual = sha256_file(blob_path)
    except OSError as exc:
        logger.warning("Cannot hash update blob %s: %s", blob_path, exc)
        return False, "hash_mismatch"
    if actual != expected.lower():
        return False, "hash_mismatch"
    return True, "ok"


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
        return dest_path, None, version
    except Exception as exc:
        logger.error("Release download failed: %s", exc)
        return None, T("web.update_download_failed", reason=exc), ""


def _cache_dir() -> str:
    """Directory where downloaded update assets are cached for P2P serving."""
    from internal.config.config import _config_dir

    return os.path.join(_config_dir(), "update_cache")


def cache_asset(asset_path: str) -> str | None:
    """Copy the verified *asset_path* into the update cache for later P2P
    serving. Returns the cached path, or None on failure (never raises)."""
    import shutil

    try:
        d = _cache_dir()
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, os.path.basename(asset_path))
        shutil.copyfile(asset_path, dest)
        logger.info("Cached update asset at %s", dest)
        return dest
    except OSError as exc:
        logger.warning("Failed to cache update asset: %s", exc)
        return None


def get_cached_asset() -> str | None:
    """The cached update asset for this platform and shell, or None.

    Looked up by pattern rather than by name because the Tauri bundles carry the
    version in their filename, and because one cache directory can hold both
    applications' assets: a machine that has run the legacy app and the Tauri
    app has one of each here, and serving the wrong one to a peer of the same
    platform is the mistake this naming exists to prevent.  The peer checks what
    it is sent against its own platform's digest before it will install it, so a
    mismatch is caught there too -- but it costs a transfer to find out.
    """
    directory = _cache_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    for matcher in _asset_matchers():
        for name in names:
            path = os.path.join(directory, name)
            if matcher.match(name) and os.path.isfile(path):
                return path
    return None
