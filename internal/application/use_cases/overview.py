"""Read model behind the web Companion's overview page."""

from __future__ import annotations

import datetime
import logging
import platform
import time

from internal.platform import friendly_platform_name
from internal.platform.network import detect_network_type
from internal.version import __version__

logger = logging.getLogger(__name__)

# Content-type labels the history rows carry for an image entry.  The web
# overview's "images" counter matches the desktop dashboard's.
IMAGE_TYPES = ("IMAGE", "IMAGE_PNG", "IMAGE_EMF", "PICTURE")


def _count_connected_sync_peers(runtime) -> tuple[int, list[str]]:
    """Connected peers that are paired — an active *sync session*, not a socket.

    A chat-only or mid-pairing connection is live but not a trusted sync peer,
    so counting it would disagree with the frontend's paired-only math.
    """
    try:
        paired_ids = {
            getattr(peer, "device_id", "") for peer in runtime.pairing.get_paired_peers()
        }
    except Exception:
        paired_ids = set()
    try:
        connected = list(runtime.transport.get_connected_peers() or [])
    except Exception:
        connected = []
    names: list[str] = []
    try:
        names = [
            name
            for pid, name in runtime.transport.get_connected_peers_with_names()
            if pid in paired_ids
        ]
    except Exception:
        logger.debug("Connected peer names unavailable", exc_info=True)
    return sum(1 for pid in connected if pid in paired_ids), names


def _history_rows(history) -> list[dict]:
    if history is None:
        return []
    try:
        return history.get_all()
    except Exception:
        logger.debug("History snapshot for the overview failed", exc_info=True)
        return []


def _is_today(timestamp) -> bool:
    try:
        return datetime.datetime.fromtimestamp(float(timestamp)).date() == datetime.date.today()
    except Exception:
        return False


def build_overview(cfg, history, runtime, start_time, lan_ip="") -> dict:
    """Aggregate the dashboard counters the web overview panel renders.

    Mirrors the legacy ``Application._get_overview_data``.  Every group
    degrades to a zero instead of failing the page: a locked or partially
    started application still has to render its overview.
    """
    connected_count, connected_names = _count_connected_sync_peers(runtime)
    try:
        paired_count = len(runtime.pairing.get_paired_peers())
    except Exception:
        paired_count = 0

    active_transfers = 0
    transfer_completed = 0
    try:
        active_transfers = sum(
            1
            for item in runtime.file_transfer.get_transfers() or []
            if item.get("status") not in ("completed", "cancelled", "failed")
        )
        transfer_completed = sum(
            1 for item in runtime.file_transfer.get_history() or [] if item.get("success")
        )
    except Exception:
        logger.debug("Transfer counters for the overview failed", exc_info=True)

    rows = _history_rows(history)
    discovered_count = 0
    discovering = visible = False
    try:
        discovered_count = len(runtime.discovered_peers())
    except Exception:
        logger.debug("Discovery count for the overview failed", exc_info=True)
    try:
        discovery = runtime.discovery_state()
        discovering = bool(discovery.get("enabled"))
        visible = bool(discovery.get("visible"))
    except Exception:
        logger.debug("Discovery state for the overview failed", exc_info=True)

    network_type, network_detail = detect_network_type()
    return {
        "connected_count": connected_count,
        "paired_count": paired_count,
        "discovered_count": discovered_count,
        "connected_names": connected_names,
        "history_count": len(rows),
        "history_today": sum(1 for row in rows if _is_today(row.get("timestamp", 0))),
        "history_pinned": sum(1 for row in rows if row.get("pinned")),
        "history_images": sum(
            1 for row in rows if str(row.get("content_type", "")).upper() in IMAGE_TYPES
        ),
        "active_transfers": active_transfers,
        "transfer_completed": transfer_completed,
        "discovering": discovering,
        "visible": visible,
        "sync_enabled": bool(getattr(cfg, "sync_enabled", True)),
        "web_enabled": bool(getattr(cfg, "web_enabled", True)),
        "uptime_seconds": max(0, int(time.time()) - int(start_time or time.time())),
        "local_ip": lan_ip,
        "port": getattr(cfg, "web_port", 0),
        "platform": friendly_platform_name(platform.system()),
        "version": __version__,
        "network_type": network_type,
        "network_detail": network_detail,
        "recent_items": [
            {
                "text": (row.get("text_preview") or "")[:80],
                "type": str(row.get("content_type") or "TEXT"),
                "time": row.get("timestamp", 0),
                "pinned": bool(row.get("pinned")),
            }
            for row in rows[:6]
        ],
    }
