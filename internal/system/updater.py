"""Check for ClipSync updates against GitHub releases.

Uses only the standard library (urllib) so the app gains no new
dependency.  The check is best-effort and never raises: any network or
parse failure simply returns "no update available".
"""

import json
import logging
import os
import urllib.request

from internal.version import __version__

logger = logging.getLogger(__name__)

_GITHUB_REPO = "kai3316/clipsync"
_LATEST_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_RELEASES_PAGE = f"https://github.com/{_GITHUB_REPO}/releases/latest"


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


def _fetch_latest_release(timeout: float = 6.0) -> dict | None:
    """Fetch the latest GitHub release JSON, or None on any failure.

    Retried a couple of times with a short backoff so a single transient
    network blip (Wi-Fi dropout, DNS hiccup) doesn't surface as a hard
    "check failed" to a user who just clicked the tray item.
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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                import time as _time
                _time.sleep(0.5 * (attempt + 1))
    logger.debug("Update check failed after 3 attempts: %s", last_exc)
    return None


def check_for_update(timeout: float = 6.0) -> dict:
    """Query GitHub for the latest release and compare to our version.

    Returns a dict:
        {"available": bool, "latest": str, "current": str, "url": str}
    On any failure (offline, rate-limited, parse error) returns
    {"available": False, "latest": "", "current": __version__, "url": ""}.
    """
    result = {"available": False, "latest": "", "current": __version__, "url": _RELEASES_PAGE}
    data = _fetch_latest_release(timeout)
    if not data:
        return result

    latest = (data.get("tag_name") or "").strip()
    if not latest:
        return result

    result["latest"] = latest
    result["available"] = _is_newer(latest, __version__)
    result["url"] = data.get("html_url") or _RELEASES_PAGE
    return result


def _platform_asset_name() -> str:
    """Return the release asset filename for the current platform + arch.

    macOS (Apple Silicon) → clipsync-macos-arm64.zip, Windows → clipsync-windows.zip,
    Linux x86_64 → clipsync-linux.tar.gz, Linux arm64 → clipsync-linux-arm64.tar.gz.
    Intel macOS is no longer built; that branch returns a name matching no asset,
    so the downloader reports "no release for this platform" gracefully.
    """
    import platform as _platform

    system = _platform.system()
    machine = (_platform.machine() or "").lower()
    is_arm = "aarch64" in machine or "arm64" in machine
    if system == "Darwin":
        return "clipsync-macos-arm64.zip" if is_arm else "clipsync-macos-x64.zip"
    if system == "Windows":
        return "clipsync-windows.zip"
    # Linux
    if is_arm:
        return "clipsync-linux-arm64.tar.gz"
    return "clipsync-linux.tar.gz"


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
    asset_name = _platform_asset_name()
    for asset in data.get("assets") or []:
        if asset.get("name") != asset_name:
            continue
        digest = asset.get("digest") or ""
        if not digest.startswith("sha256:") or len(digest) <= len("sha256:"):
            logger.info("Release %s has no sha256 digest for %s", tag, asset_name)
            return None
        return {
            "version": tag,
            "asset": asset_name,
            "sha256": digest[len("sha256:"):].lower(),
        }
    logger.info("Latest release %s has no asset named %s", tag, asset_name)
    return None


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


def download_latest_release(dest_dir: str) -> tuple[str | None, str | None]:
    """Download the latest release asset for this platform into *dest_dir*.

    Reuses the same GitHub release lookup as :func:`check_for_update` and
    streams the matching asset to ``dest_dir/<asset-name>`` via urllib.

    Returns ``(saved_path, None)`` on success, or ``(None, reason)`` on
    failure where *reason* is a short human-readable (localized) message
    describing the problem — e.g. that no release asset exists for this
    platform.  Never raises.
    """
    from internal.i18n import T

    import os

    try:
        data = _fetch_latest_release(timeout=60.0)
        if not data:
            return None, T("web.update_server_unreachable")
        assets = data.get("assets") or []
        if not assets:
            logger.warning("Latest release has no downloadable assets")
            return None, T("web.update_no_assets")

        asset_name = _platform_asset_name()
        matched = None
        for asset in assets:
            if asset.get("name") == asset_name:
                matched = asset
                break
        if not matched or not matched.get("browser_download_url"):
            logger.warning("No download asset found for platform: %s", asset_name)
            return None, T("web.update_no_release", name=asset_name)
        browser_url = matched["browser_download_url"]

        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, asset_name)
        # Download to a .part file and rename on success, so an interrupted
        # download never leaves a truncated file at the final installer path
        # (a leftover the user could double-click as if it were a real
        # release).
        temp_path = dest_path + ".part"
        req = urllib.request.Request(browser_url, headers={"User-Agent": "clipsync"})
        try:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                with open(temp_path, "wb") as out:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
            # Verify the downloaded size AND SHA-256 against the release API, so
            # a truncated, corrupted, or tampered asset is rejected before it is
            # exposed as a valid installer. The API digest is "sha256:<hex>".
            asset_size = matched.get("size")
            if asset_size:
                actual = os.path.getsize(temp_path)
                if actual != int(asset_size):
                    raise RuntimeError(
                        f"download size mismatch: expected {asset_size}, got {actual}"
                    )
            digest = matched.get("digest") or ""
            if digest.startswith("sha256:"):
                actual_sha = sha256_file(temp_path)
                if actual_sha != digest[len("sha256:"):]:
                    raise RuntimeError(
                        f"download checksum mismatch: expected {digest}, got sha256:{actual_sha}"
                    )
            os.replace(temp_path, dest_path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
        logger.info("Downloaded release asset to %s", dest_path)
        return dest_path, None
    except Exception as exc:
        logger.error("Release download failed: %s", exc)
        return None, T("web.update_download_failed", reason=exc)


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
    """Return the cached update asset path for this platform, or None."""
    p = os.path.join(_cache_dir(), _platform_asset_name())
    return p if os.path.isfile(p) else None
