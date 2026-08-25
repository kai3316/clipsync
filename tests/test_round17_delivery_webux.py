"""Round 17 — Web UX layer for internet "did it get there?" delivery status.

Scope (per user clarification): delivery confirmation is for RELAY-CHAT
message bubbles, not clipboard history.
  1. Each OUTGOING relay-chat text bubble carries a tiny delivery stamp
     (✓已送达 / ✗未送达 / …发送中 / …待补发) at its bottom-right, matched by
     msg_id against the `internet_delivery` WS event map. Chat entries only
     expose msg_id from newer hosts; when the field is absent (or the id is
     unmatched) the stamp is hidden — fully defensive.
  2. Each internet-paired device card keeps ONE minimal line (clipboard
     offline-retry queue, which the backend ledger genuinely tracks): an
     orange "待补发 N" badge (only when > 0) plus the most recent send
     result (⏳/✅/❌). No expandable recent-sends list.
  3. Clipboard history rows do NOT get a delivery stamp.

Static wiring assertions only — the backend may not emit `internet_delivery`
for chat frames yet, so every assertion is about the frontend contract
(endpoint path, WS vocabulary, store map, locale keys) rather than a live
server.

Tests:
  1. chat-panel bubble: template + defensive msg_id matching.
  2. store: internetDelivery (per-peer) + internetDeliveryMsgs (msg_id map),
     fetch seed, applyInternetDelivery folding {peer_id, msg_id, status}.
  3. ws: internet_delivery event → store, known status vocabulary only.
  4. api: getInternetDelivery wrapper against the agreed path.
  5. device card: one-line delivery row, no expandable sends list.
  6. history item: NO delivery stamp (regression guard for the old scope).
  7. Locales: en / zh-CN key sets identical; delivery.* keys present and
     non-empty in both.
  8. JS node --check over every file this round touched.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")

_STATUSES = ("sent", "delivered", "failed", "queued")


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


# ── 1. Chat-bubble delivery stamp (the primary surface) ────────────────


def test_chat_bubble_has_delivery_stamp_in_template():
    src = _read("components", "chat-panel.js")
    assert "chat-bubble__delivery" in src
    assert 'v-if="chatDeliveryStatus(m)"' in src
    assert ':class="chatDeliveryClass(m)"' in src
    # Reads the shared delivery.* locale keys for the label + the
    # offline-retry hint (the title explains why a bubble is still sending).
    assert "chatDeliveryKey" in src
    assert "chatDeliveryIcon" in src
    assert "delivery.offline_retry_hint" in src


def test_chat_bubble_stamp_is_msg_id_matched_and_defensive():
    src = _read("components", "chat-panel.js")
    chunk = src.split("chatDeliveryStatus: function")[1].split("chatDeliveryClass")[0]
    # Only outgoing TEXT bubbles are stamped; a chat-level "failed" bubble
    # keeps its failed label instead.
    assert "m.outgoing" in chunk
    assert "m.kind !== 'text'" in chunk
    assert "m.status === 'failed'" in chunk
    # Defensive: no msg_id field (older host) → no stamp.
    assert "m.msg_id" in chunk
    assert "=== ''" in chunk or "!== 'text'" in chunk
    # The lookup reads the store's msg_id → status map.
    assert "internetDeliveryMsgs" in chunk
    # Only the known status vocabulary renders; anything else → null.
    for st in _STATUSES:
        assert f"'{st}'" in chunk, st


def test_chat_bubble_stamp_maps_status_to_locale_key():
    src = _read("components", "chat-panel.js")
    chunk = src.split("chatDeliveryKey: function")[1].split("_scrollToBottom")[0]
    # The label key is built from the delivery.* namespace; failed and sent
    # map to friendlier names (not_delivered / sending), delivered & queued
    # fall through to their own status name.
    assert "'delivery.' +" in chunk
    assert "not_delivered" in chunk
    assert "sending" in chunk
    assert "failed" in chunk
    assert "sent" in chunk


def test_chat_bubble_delivery_css_present():
    css = _read("index.html")
    assert ".chat-bubble__delivery {" in css
    for variant in ("--ok", "--err", "--sending", "--queued"):
        assert f".chat-bubble__delivery{variant} {{" in css, variant


# ── 2. Store state + methods ───────────────────────────────────────────


def test_store_declares_delivery_state():
    src = _read("js", "store.js")
    # Per-peer clipboard-queue state (device-card line).
    assert "internetDelivery:" in src
    # Chat-bubble msg_id → status map.
    assert "internetDeliveryMsgs:" in src
    assert "fetchInternetDelivery:" in src
    assert "applyInternetDelivery:" in src
    # The old history content_hash map is gone.
    assert "internetDeliveryHashes" not in src


def test_store_fetch_delivery_wraps_api_and_is_defensive():
    src = _read("js", "store.js")
    chunk = src.split("fetchInternetDelivery: function")[1].split("applyInternetDelivery")[0]
    assert "getInternetDelivery" in chunk
    # Older backend (no endpoint) settles instead of throwing.
    assert "loadFailed = true" in chunk
    # pending falls back to the queued count when the field is absent.
    assert "qCount" in chunk
    # The status vocabulary is validated before it is stored.
    for st in _STATUSES:
        assert f"'{st}'" in chunk, st


def test_store_apply_delivery_folds_event():
    src = _read("js", "store.js")
    chunk = src.split("applyInternetDelivery: function")[1].split("applyAiConfigFileResult")[0]
    # Takes the whole event object (peer_id, msg_id, status).
    assert "data.peer_id" in chunk
    assert "data.msg_id" in chunk
    assert "data.status" in chunk
    # The msg_id → status map is stamped for the chat bubble.
    assert "internetDeliveryMsgs[mid]" in chunk
    # The per-peer card state still updates: pending dedupes on msg_id.
    assert "msgStatus" in chunk
    assert "wasQueued" in chunk
    assert "lastStatus" in chunk
    # Contact events refresh the peer's last-sync time on its pairing card.
    assert "last_seen" in chunk
    for st in _STATUSES:
        assert f"'{st}'" in chunk, st


def test_store_apply_delivery_guards_missing_fields():
    src = _read("js", "store.js")
    chunk = src.split("applyInternetDelivery: function")[1].split("applyAiConfigFileResult")[0]
    # Missing peer_id or unknown status → no-op.
    assert "return;" in chunk
    assert "['sent', 'delivered', 'failed', 'queued'].indexOf(status) === -1" in chunk
    # Missing msg_id is tolerated (map simply not stamped).
    assert "msg_id === undefined" in chunk or "msg_id === null" in chunk


# ── 3. WS event wiring ─────────────────────────────────────────────────


def test_ws_handles_internet_delivery_event():
    src = _read("js", "ws.js")
    assert "case 'internet_delivery'" in src
    chunk = src.split("case 'internet_delivery'")[1].split("break;")[0]
    # Only the known status vocabulary reaches the store.
    for st in _STATUSES:
        assert f"'{st}'" in chunk, st
    assert "store.applyInternetDelivery(data)" in chunk
    # peer_id must be present for the event to be accepted.
    assert "data.peer_id" in chunk


# ── 4. API wrapper ─────────────────────────────────────────────────────


def test_api_delivery_wrapper_contract():
    src = _read("js", "api.js")
    assert "getInternetDelivery" in src
    assert "'/api/internetdelivery?peer_id='" in src
    assert "encodeURIComponent(peerId" in src


# ── 5. Device-card one-line delivery row (clipboard queue) ─────────────


def test_device_card_has_minimal_delivery_row():
    src = _read("components", "device-panel.js")
    assert "netpair-peer__delivery" in src
    assert "deliveryPending" in src
    assert "deliveryLastStatus" in src
    assert "deliveryLastClass" in src
    assert "deliveryLastIcon" in src
    assert "deliveryLastKey" in src
    assert "delivery.pending_badge" in src
    assert "delivery.offline_retry_hint" in src
    # The queued badge only renders when the count is > 0.
    assert "deliveryPending(peer) > 0" in src
    # The last-result text comes from the shared delivery.* locale keys
    # (failed → delivery.not_delivered, sent → delivery.sending).
    assert "'delivery.' +" in src
    assert "not_delivered" in src
    assert "sending" in src


def test_device_card_has_no_expandable_sends_list():
    # The simplified scope explicitly REMOVED the per-card recent-sends
    # expandable list — guard against it coming back as clutter.
    src = _read("components", "device-panel.js")
    assert "netpair-sends" not in src
    assert "deliverySends" not in src
    assert "expandedDelivery" not in src


def test_device_card_delivery_css_present():
    css = _read("index.html")
    assert ".netpair-peer__delivery {" in css
    assert ".netpair-delivery-badge {" in css
    assert ".netpair-delivery-badge--pending {" in css
    assert ".netpair-delivery-result {" in css
    for variant in ("--ok", "--err", "--sending", "--queued"):
        assert f".netpair-delivery-result{variant} {{" in css, variant


def test_device_card_loads_delivery_on_netpair_refresh():
    src = _read("components", "device-panel.js")
    assert "loadAllDeliveries" in src
    assert "fetchInternetDelivery" in src
    # Called from loadNetpairState so the status line appears as soon as the
    # paired list is known (and on every tab revisit).
    chunk = src.split("loadNetpairState: function")[1].split("generateNetpairCode")[0]
    assert "loadAllDeliveries" in chunk


# ── 6. History item has NO delivery stamp (old scope removed) ──────────


def test_history_item_has_no_delivery_stamp():
    src = _read("components", "history-item.js")
    assert "history-item__delivery" not in src
    assert "deliveryStatus" not in src
    assert "deliveryLabelKey" not in src
    assert "internetDeliveryHashes" not in src
    assert "content_hash" not in src


def test_history_delivery_css_absent():
    css = _read("index.html")
    assert ".history-item__delivery" not in css


# ── 7. Locale parity ───────────────────────────────────────────────────

_DELIVERY_KEYS = [
    "delivery.delivered",
    "delivery.not_delivered",
    "delivery.sending",
    "delivery.queued",
    "delivery.pending_badge",
    "delivery.offline_retry_hint",
]


def _locales():
    with open(os.path.join(_STATIC, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def test_locale_key_sets_identical():
    en, zh = _locales()
    en_only = set(en) - set(zh)
    zh_only = set(zh) - set(en)
    assert not en_only, f"keys missing from zh-CN.json: {sorted(en_only)}"
    assert not zh_only, f"keys missing from en.json: {sorted(zh_only)}"


def test_round17_delivery_keys_present_and_nonempty():
    en, zh = _locales()
    for key in _DELIVERY_KEYS:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key
    # The pending badge carries the count placeholder in both languages.
    assert "{count}" in en["delivery.pending_badge"]
    assert "{count}" in zh["delivery.pending_badge"]


# ── 8. JS syntax (node --check over every file this round touched) ─────


_TOUCHED_JS = [
    ("components", "chat-panel.js"),
    ("components", "device-panel.js"),
    ("components", "history-item.js"),
    ("js", "api.js"),
    ("js", "store.js"),
    ("js", "ws.js"),
]


def test_touched_js_passes_node_check(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        proc = subprocess.run(
            [node, "--check", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.returncode == 0, (
            f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"
        )


def test_no_nul_bytes_in_touched_js():
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"
