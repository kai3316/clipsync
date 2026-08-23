"""Check for ClipSync updates against GitHub releases.

Uses only the standard library (urllib) so the app gains no new
dependency.  The check is best-effort and never raises: any network or
parse failure simply returns "no update available".
"""

import json
import logging
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


def _sha256_file(path: str) -> str:
    """Streaming SHA-256 of *path*, returned as lowercase hex."""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
                actual_sha = _sha256_file(temp_path)
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
