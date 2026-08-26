"""File transfer API handlers.

All handlers return a (data_dict, status_code) tuple.
"""

import logging

logger = logging.getLogger(__name__)


def _format_eta(seconds: float) -> str:
    """Format an ETA in seconds as a short human-readable string."""
    if not seconds or seconds <= 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}m {secs}s"


def _map_active(t: dict) -> dict:
    """Map a FileTransferManager active-transfer dict to the web UI shape."""
    progress = max(0.0, min(float(t.get("progress", 0.0)), 1.0))
    paused = bool(t.get("paused", False))
    return {
        "id": t.get("transfer_id", ""),
        "filename": t.get("file_name", "Unknown file"),
        "size": t.get("file_size", 0),
        "direction": t.get("direction", "down"),
        "status": "paused" if paused else t.get("state", ""),
        "progress": round(progress * 100, 1),
        "speed": t.get("speed_bytes_per_sec", 0),
        "eta": _format_eta(t.get("eta_seconds", 0)),
    }


def _map_history(t: dict) -> dict:
    """Map a FileTransferManager history dict to the web UI shape."""
    reason = t.get("status", "")
    return {
        "id": t.get("transfer_id", ""),
        "filename": t.get("file_name", "Unknown file"),
        "size": t.get("file_size", 0),
        # Keep the generic bucket for styling, plus the specific reason
        # (error_disk, error_size_mismatch, error_missing_chunks, error_security,
        # rejected, peer_offline, timeout, cancelled) so the UI can render the
        # exact failure cause instead of a generic "Failed".
        "status": "cancelled" if t.get("cancelled") else ("completed" if t.get("success") else "failed"),
        "reason": reason,
        "path": t.get("saved_path") or t.get("source_path") or "",
        # Destination/source peer — failed OUTGOING rows carry it so the UI's
        # Retry action can be offered only where the host can actually re-send.
        "peer_id": t.get("peer_id", "") or "",
        "direction": t.get("direction", "down"),
        "timestamp": t.get("timestamp", 0),
    }


def get_transfers(on_get_transfers=None):
    """Return active and completed transfers.

    The transfer state lives on the host application's FileTransferManager,
    so it is read through the *on_get_transfers* callback (which returns an
    ``(active, history)`` tuple) rather than from the SyncManager.  The raw
    manager dicts are remapped to the field names the web UI expects.
    """
    if on_get_transfers is None:
        return {"active": [], "history": []}, 200
    try:
        active, history = on_get_transfers()
    except Exception:
        logger.exception("Failed to read transfer state")
        return {"active": [], "history": []}, 200
    return {
        "active": [_map_active(t) for t in (active or [])],
        "history": [_map_history(t) for t in (history or [])],
    }, 200


def _speed_quality(mbps: float) -> str:
    """Classify a measured LAN throughput into the labels the web UI uses.

    Thresholds match the desktop dashboard so the two UIs agree:
      > 10 Mbps -> "fast", > 2 Mbps -> "good", otherwise "slow".
    """
    if mbps > 10:
        return "fast"
    if mbps > 2:
        return "good"
    return "slow"


def get_speed_test(on_get_speed_test=None):
    """Return the current LAN speed-test state in the shape the web UI expects.

    The raw ``FileTransferManager.get_speed_test()`` dict uses internal keys
    (``state``, ``result_mbps``, ``chunks_sent``, ``total_chunks``) that are
    also consumed by the desktop dashboard. The web frontend instead polls for
    ``{done, mbps, progress, status, quality}`` — so we translate here rather
    than changing the shared manager dict.
    """
    if on_get_speed_test is None:
        return {"done": False, "mbps": None, "progress": 0.0, "status": "", "quality": ""}, 200
    try:
        raw = on_get_speed_test() or {}
    except Exception:
        logger.exception("Failed to read speed test state")
        raw = {}

    state = raw.get("state", "")
    total = int(raw.get("total_chunks", 0) or 0)
    sent = int(raw.get("chunks_sent", 0) or 0)
    mbps = float(raw.get("result_mbps", 0) or 0)
    done = state in ("done", "acknowledged")
    progress = (sent / total) if total else (1.0 if done else 0.0)
    progress = max(0.0, min(progress, 1.0))

    if done:
        status = ""
    elif state == "sending":
        status = f"Sending test data {sent}/{total}"
    else:
        status = ""

    return {
        "done": done,
        "mbps": round(mbps, 2) if done else None,
        "progress": progress,
        "status": status,
        "quality": _speed_quality(mbps) if done else "",
    }, 200
