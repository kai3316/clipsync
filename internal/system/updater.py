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
    """Fetch the latest GitHub release JSON, or None on any failure."""
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
        logger.debug("Update check failed: %s", exc)
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
    """Return the release asset filename for the current platform.

    macOS → clipsync-macos.zip, Windows → clipsync-windows.zip,
    Linux x86_64 → clipsync-linux.tar.gz, Linux arm64 → clipsync-linux-arm64.tar.gz.
    """
    import platform as _platform

    system = _platform.system()
    machine = (_platform.machine() or "").lower()
    if system == "Darwin":
        return "clipsync-macos.zip"
    if system == "Windows":
        return "clipsync-windows.zip"
    # Linux
    if "aarch64" in machine or "arm64" in machine:
        return "clipsync-linux-arm64.tar.gz"
    return "clipsync-linux.tar.gz"


def download_latest_release(dest_dir: str) -> str | None:
    """Download the latest release asset for this platform into *dest_dir*.

    Reuses the same GitHub release lookup as :func:`check_for_update` and
    streams the matching asset to ``dest_dir/<asset-name>`` via urllib.

    Returns the saved file path on success, or None on any failure (network,
    missing asset, write error).  Never raises.
    """
    import os

    try:
        data = _fetch_latest_release(timeout=60.0)
        if not data:
            return None
        assets = data.get("assets") or []
        if not assets:
            logger.warning("Latest release has no downloadable assets")
            return None

        asset_name = _platform_asset_name()
        browser_url = None
        for asset in assets:
            if asset.get("name") == asset_name:
                browser_url = asset.get("browser_download_url")
                break
        if not browser_url:
            logger.warning("No download asset found for platform: %s", asset_name)
            return None

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
            # Verify the downloaded size against the release API so a
            # truncated-but-cleanly-EOF'd stream is rejected, not reported
            # as a successful download.
            asset_size = next(
                (a.get("size") for a in assets if a.get("name") == asset_name),
                None,
            )
            if asset_size:
                actual = os.path.getsize(temp_path)
                if actual != int(asset_size):
                    raise RuntimeError(
                        f"download size mismatch: expected {asset_size}, got {actual}"
                    )
            os.replace(temp_path, dest_path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
        logger.info("Downloaded release asset to %s", dest_path)
        return dest_path
    except Exception as exc:
        logger.error("Release download failed: %s", exc)
        return None
