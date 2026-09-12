"""Resolve the diagnostics report's i18n keys from the web panel's catalog.

The web panel localizes the report client-side: ``internal/web/static/js/i18n.js``
plus the per-locale JSON files under ``internal/web/static/locales``.  Those keys
belong to the panel's catalog rather than the desktop shell's, so the sidecar
resolves them here for the saved language and attaches the strings as ``*_text``
fields, leaving the raw ``*_key``/``*_params`` pairs untouched so the web panel
keeps resolving for itself.

Missing keys are simply omitted — the client then falls back to the raw
``detail``/``hint`` strings the builder already provides.
"""

from __future__ import annotations

import json
import os

# Fixed group order and labels, mirroring DIAG_GROUP_DEFS in
# ``internal/web/static/components/diagnostics-panel.js``.
GROUP_LABEL_KEYS = (
    ("system", "diag.v2.group.system"),
    ("network", "diag.v2.group.network"),
    ("internet", "diag.v2.group.internet"),
    ("ai_config", "diag.v2.group.ai_config"),
    ("chat", "diag.v2.group.chat"),
    ("transfer", "diag.v2.group.transfer"),
    ("filesystem", "diag.v2.group.filesystem"),
)

# Item labels, mirroring DIAG_V2_ITEM_LABELS in the same file.  The report only
# carries item ids, so the display label has to be resolved from the id.
ITEM_LABEL_KEYS = {
    "app_version": "diag.v2.item.app_version",
    "uptime": "diag.v2.item.uptime",
    "data_dir": "diag.v2.item.data_dir",
    "log_path": "diag.v2.item.log_path",
    "lan_ip": "diag.v2.item.lan_ip",
    "tcp_port": "diag.v2.item.tcp_port",
    "mdns_service": "diag.v2.item.mdns_service",
    "web_service": "diag.v2.item.web_service",
    "firewall": "diag.v2.item.firewall",
    "internet_enabled": "diag.v2.item.internet_enabled",
    "relay_state": "diag.v2.item.relay_state",
    "brokers": "diag.v2.item.brokers",
    "netpair_count": "diag.v2.item.netpair_count",
    "pending_count": "diag.v2.item.pending_count",
    "watch_roots": "diag.v2.item.watch_roots",
    "local_entries": "diag.v2.item.local_entries",
    "last_collected": "diag.v2.item.last_collected",
    "trash_size": "diag.v2.item.trash_size",
    "chat_sessions": "diag.v2.item.chat_sessions",
    "active_transfers": "diag.v2.item.active_transfers",
    "transfer_failures": "diag.v2.item.transfer_failures",
    "history_db_size": "diag.v2.item.history_db_size",
    "disk_free": "diag.v2.item.disk_free",
}

FALLBACK_LOCALE = "en"

_cache: dict[str, dict] = {}


def locales_dir() -> str:
    """Directory holding the web panel's ``<locale>.json`` files."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web", "static", "locales",
    )


def translations(language: str) -> dict:
    """Flat key→string table for ``language``, falling back to ``en``.

    An unknown or missing locale yields the fallback table rather than an empty
    one, so the native client shows the same strings the web panel would.
    """
    code = (language or "").strip() or FALLBACK_LOCALE
    if code in _cache:
        return _cache[code]
    table: dict = {}
    for candidate in (code, FALLBACK_LOCALE):
        path = os.path.join(locales_dir(), f"{candidate}.json")
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            table = loaded
            break
    _cache[code] = table
    return table


def translate(table: dict, key, params=None):
    """Resolve one key with ``{name}`` placeholders, or None when unknown.

    Mirrors ``ClipsyncI18n.t``: every occurrence of a placeholder is replaced,
    and a key the table does not carry resolves to None so the caller keeps the
    builder's raw fallback string.
    """
    if not key or not isinstance(table, dict):
        return None
    text = table.get(key)
    if not isinstance(text, str):
        return None
    for name, value in (params or {}).items():
        text = text.replace("{" + str(name) + "}", str(value))
    return text


def _localized_entry(entry: dict, table: dict) -> dict:
    """Copy one check/item with resolved ``*_text`` fields added.

    A flat check carries ``guidance``; a v2 item carries ``hint``.  Each is
    resolved under its own name so the client can fall back per field.
    """
    row = dict(entry)
    text = translate(table, row.get("detail_key"), row.get("detail_params"))
    if text is not None:
        row["detail_text"] = text
    hint = translate(table, row.get("hint_key"), row.get("hint_params"))
    if hint is not None:
        row["hint_text"] = hint
    guidance = translate(table, row.get("guidance_key"), row.get("guidance_params"))
    if guidance is not None:
        row["guidance_text"] = guidance
    label = translate(table, ITEM_LABEL_KEYS.get(str(row.get("id"))))
    if label is not None:
        row["label_text"] = label
    return row


def localize(report: dict, language: str) -> dict:
    """Return ``report`` with display strings resolved for the native client.

    The input is not modified: the web panel resolves the same keys itself from
    the untouched ``*_key``/``*_params`` pairs.
    """
    table = translations(language)
    if not table:
        return report
    localized = dict(report)
    localized["checks"] = [
        _localized_entry(check, table) for check in report.get("checks") or []
    ]
    groups = {}
    for group_id, group in (report.get("groups") or {}).items():
        row = dict(group)
        label = translate(table, row.get("label_key"))
        if label is not None:
            row["label_text"] = label
        row["items"] = [_localized_entry(item, table) for item in group.get("items") or []]
        groups[group_id] = row
    localized["groups"] = groups
    return localized
