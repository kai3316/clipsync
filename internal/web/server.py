"""Built-in HTTP server for ClipSync Web Companion.

Serves a mobile-optimised web page that lets phones on the same LAN
view clipboard history, push text to the desktop, and transfer files.
Supports WebSocket connections at /ws for real-time data push.

The server delegates API routes to routes.py, WebSocket handling to
ws.py, and serves static files from internal/web/static/.
"""

import hmac
import json
import logging
import mimetypes
import os
import socket
import sys
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from io import BytesIO
import threading

logger = logging.getLogger(__name__)

# Compiled i18n JSON cache, keyed by locale code.  _interpolate_html runs on
# every static asset request; re-reading and re-parsing the locale JSON file
# each time is wasteful.
_i18n_cache: dict[str, str] = {}

# ── Upload directory ─────────────────────────────────────────────

def _get_upload_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Downloads", "ClipSync")
    os.makedirs(d, exist_ok=True)
    return d


# ── Simple multipart form parser (no external deps) ──────────────

def _parse_multipart(body: bytes, content_type: str) -> dict:
    """Parse multipart/form-data. Returns {field_name: (filename, data)}."""
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part.split("=", 1)[1].strip().strip('"').strip("'")
            break
    if not boundary:
        return {}
    b_bytes = boundary.encode("utf-8", errors="surrogateescape")
    delimiter = b"--" + b_bytes
    if not body.startswith(delimiter):
        return {}
    body = body[len(delimiter):]
    # A multipart boundary is only meaningful as a standalone delimiter line
    # ("\r\n--<boundary>").  Split on that full delimiter rather than the raw
    # boundary bytes so file content that happens to contain the boundary
    # sequence is not truncated or corrupted.
    parts = body.split(b"\r\n--" + b_bytes)
    result = {}
    for part in parts:
        if not part:
            continue
        # Strip only the leading CRLF left over from the preceding boundary
        # line.  Never strip trailing bytes — they belong to the file content.
        part = part.lstrip(b"\r\n")
        # The closing boundary contributes a trailing "--" to the last part.
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        if b"\r\n\r\n" not in part:
            continue
        header_section, body_data = part.split(b"\r\n\r\n", 1)
        headers_text = header_section.decode("utf-8", errors="replace")
        field_name = None
        filename = None
        for line in headers_text.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for disp_part in line.split(";"):
                    disp_part = disp_part.strip()
                    key_lower = disp_part.lower().split("=", 1)[0].strip()
                    val = disp_part.split("=", 1)[1].strip().strip('"') if "=" in disp_part else ""
                    if key_lower == "name":
                        field_name = val
                    elif key_lower == "filename":
                        filename = val
        if field_name and body_data:
            result[field_name] = (filename or "", body_data)
    return result


# ── PWA icons (generated at startup) ────────────────────────────

def _make_icon(size: int, dark: bool = False) -> bytes:
    """Generate a simple clipboard icon PNG with PIL."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = size // 8
    r = size // 6
    # Background rounded rect
    bg_color = (26, 39, 50, 255) if dark else (26, 82, 118, 255)
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=r, fill=bg_color,
    )
    # Clipboard shape: white rectangle with top clip
    cx = size // 2
    cy = size // 2
    bw, bh = size * 3 // 8, size * 5 // 12
    left, top = cx - bw // 2, cy - bh // 2
    # Board body
    draw.rounded_rectangle(
        [left, top + r // 2, left + bw, top + bh],
        radius=r // 2, fill=(255, 255, 255, 240),
    )
    # Clip on top
    clip_w = bw // 2
    draw.rounded_rectangle(
        [cx - clip_w // 2, top - r // 2, cx + clip_w // 2, top + r],
        radius=r // 3, fill=(255, 255, 255, 240),
    )
    # Lines on clipboard
    lx = left + bw // 5
    lw = bw * 3 // 5
    line_color = bg_color
    for li in range(3):
        ly = top + bh // 3 + li * (bh // 5)
        draw.rectangle([lx, ly, lx + lw, ly + size // 30], fill=line_color)

    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ── Minimal fallback HTML (used when static/index.html is absent) ─

_FALLBACK_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#1A5276">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>ClipSync Web</title>
<style>
:root {
  --bg: #F0F2F5; --card: #FFFFFF; --text: #111827;
  --sub: #6B7280; --accent: #4F46E5; --accent2: #7C3AED;
  --border: #E5E7EB; --success: #059669;
  --radius: 12px;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;line-height:1.5}
.app{max-width:600px;margin:0 auto;padding:16px}
header{position:sticky;top:0;z-index:50;padding:12px 0;display:flex;align-items:center;justify-content:space-between;
  backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
  background:rgba(240,242,245,.8);border-bottom:1px solid var(--border)}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{width:36px;height:36px;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:10px;font-size:18px;color:#fff}
.logo-text{font-size:18px;font-weight:700}
.icon-btn{background:var(--card);border:1px solid var(--border);border-radius:8px;
  width:36px;height:36px;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:16px}
.devices-card{background:var(--card);border-radius:var(--radius);padding:12px;margin-bottom:12px;border:1px solid var(--border)}
.devices-title{font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.device-row{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px}
.status-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.status-dot.online{background:var(--success);box-shadow:0 0 6px rgba(5,150,105,.5)}
.tabs{display:flex;gap:4px;margin-bottom:12px;background:var(--card);border-radius:var(--radius);padding:4px;border:1px solid var(--border)}
.tab{flex:1;text-align:center;padding:10px 0;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;color:var(--sub);
  border:none;background:transparent;font-family:var(--font)}
.tab.active{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}
.push-card{background:var(--card);border-radius:var(--radius);padding:14px;margin-bottom:14px;border:1px solid var(--border)}
.push-input{width:100%;min-height:80px;background:var(--bg);border:1.5px solid var(--border);
  border-radius:10px;padding:10px;font-size:14px;font-family:var(--font);color:var(--text);resize:vertical}
.push-row{display:flex;align-items:center;justify-content:space-between;margin-top:10px;gap:10px}
.push-btn{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:none;
  border-radius:10px;padding:8px 18px;font-size:13px;font-weight:600;cursor:pointer;font-family:var(--font)}
.push-btn:disabled{opacity:.4;pointer-events:none}
.clip-item{background:var(--card);border-radius:var(--radius);padding:12px;margin-bottom:8px;
  border:1px solid var(--border);cursor:pointer}
.clip-preview{font-size:13px;line-height:1.5;word-break:break-all;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.clip-meta{font-size:11px;color:var(--sub);margin-top:4px}
.clip-actions{display:flex;gap:6px;margin-top:8px}
.clip-action-btn{padding:4px 10px;border-radius:6px;border:1px solid var(--border);
  font-size:11px;font-weight:600;cursor:pointer;background:var(--card);color:var(--sub);font-family:var(--font)}
.btn-delete{color:#DC2626}
.btn-pin{color:var(--accent)}
.empty{text-align:center;padding:40px 20px;color:var(--sub);font-size:13px}
.toast{position:fixed;bottom:32px;left:50%;transform:translateX(-50%);
  background:#111827;color:#fff;padding:10px 22px;border-radius:20px;font-size:13px;font-weight:600;
  opacity:0;pointer-events:none;transition:opacity .3s;z-index:100}
.toast.show{opacity:1}
.upload-area{border:2px dashed var(--border);border-radius:10px;padding:24px;text-align:center;cursor:pointer;margin-bottom:10px}
.file-item{background:var(--card);border-radius:var(--radius);padding:12px;margin-bottom:8px;
  border:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:10px}
.file-dl{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:none;
  border-radius:6px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;font-family:var(--font)}
</style>
</head>
<body>
<div class="app">
<header>
<div class="logo"><span class="logo-icon">&#x1F4CB;</span><span class="logo-text">ClipSync</span></div>
<button class="icon-btn" id="refreshBtn" onclick="refresh()">&#x21BB;</button>
</header>
<div class="devices-card">
<div class="devices-title" id="devicesTitle">Connected Devices</div>
<div id="devicesList"></div>
</div>
<div class="tabs">
<button class="tab active" id="tabHistory" onclick="switchTab('history')">History</button>
<button class="tab" id="tabFiles" onclick="switchTab('files')">Files</button>
</div>
<div id="panelHistory">
<div class="push-card">
<textarea class="push-input" id="pushInput" rows="3" placeholder="Paste text here to send to desktop..."></textarea>
<div class="push-row">
<button class="push-btn" id="pushBtn" onclick="pushText()">Push</button>
</div>
</div>
<div id="historyList"></div>
<div id="statusMsg" style="text-align:center;padding:20px;color:var(--sub)">Loading...</div>
</div>
<div id="panelFiles" style="display:none">
<div class="upload-area" onclick="document.getElementById('fileInput').click()">
<div style="font-size:32px;margin-bottom:6px">&#x1F4E4;</div>
<div style="font-size:13px;color:var(--sub)">Tap to select a file to upload</div>
<div id="uploadFileName" style="display:none;margin-top:8px;color:var(--accent);font-weight:600"></div>
</div>
<input type="file" id="fileInput" style="display:none" onchange="onFileSelected(this)">
<div class="push-row">
<button class="push-btn" id="uploadBtn" onclick="uploadFile()" disabled>Upload</button>
</div>
<div id="fileList"></div>
<div id="fileStatusMsg" style="text-align:center;padding:20px;color:var(--sub)">Loading...</div>
</div>
</div>
<div class="toast" id="toast"></div>
<script>
var TOKEN = "__TOKEN__";
var DEVICE = {id: "__DEVICE_ID__", name: "__DEVICE_NAME__"};
var _cache = {history:[],devices:[],files:[]}, _currentTab = "history", _selectedFile = null;
var _toastTimer = 0;

function api(path) {
  var sep = path.indexOf("?") >= 0 ? "&" : "?";
  return fetch(path + sep + "token=" + encodeURIComponent(TOKEN))
    .then(function(r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
}

function refresh() {
  var btn = document.getElementById("refreshBtn");
  btn.style.transform = "rotate(360deg)";
  btn.style.transition = "transform .5s";
  setTimeout(function(){ btn.style.transform = ""; }, 500);
  Promise.all([api("/api/history"), api("/api/devices")]).then(function(r) {
    _cache.history = r[0].items || []; _cache.devices = r[1].devices || [];
    renderDevices(); renderHistory();
    document.getElementById("statusMsg").style.display = "none";
  }).catch(function() {
    document.getElementById("statusMsg").textContent = "Connection lost.";
    document.getElementById("statusMsg").style.display = "";
  });
  if (_currentTab === "files") loadFiles();
}

function loadFiles() {
  api("/api/files").then(function(r) {
    _cache.files = r.files || []; renderFiles();
    document.getElementById("fileStatusMsg").style.display = "none";
  }).catch(function() {
    document.getElementById("fileStatusMsg").textContent = "Failed to load files.";
    document.getElementById("fileStatusMsg").style.display = "";
  });
}

function switchTab(tab) {
  _currentTab = tab;
  document.getElementById("tabHistory").classList.toggle("active", tab === "history");
  document.getElementById("tabFiles").classList.toggle("active", tab === "files");
  document.getElementById("panelHistory").style.display = tab === "history" ? "" : "none";
  document.getElementById("panelFiles").style.display = tab === "files" ? "" : "none";
  if (tab === "files") loadFiles();
}

function renderDevices() {
  var list = document.getElementById("devicesList");
  var devs = _cache.devices;
  document.getElementById("devicesTitle").textContent = "Connected Devices (" + devs.length + ")";
  if (!devs.length) { list.innerHTML = '<div class="device-row">No devices connected</div>'; return; }
  list.innerHTML = devs.map(function(d) {
    return '<div class="device-row"><span class="status-dot ' + (d.connected ? 'online' : '') + '"></span>' +
      '<span>' + esc(d.device_name) + '</span></div>';
  }).join("");
}

function esc(s) { return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

function renderHistory() {
  var items = _cache.history, list = document.getElementById("historyList");
  if (!items.length) { list.innerHTML = '<div class="empty">No clipboard history yet</div>'; return; }
  list.innerHTML = items.map(function(item, i) {
    var preview = esc(item.text_preview || "");
    var pinned = item.pinned ? " &#x1F4CC;" : "";
    return '<div class="clip-item" onclick="copyItem(' + i + ')">' +
      '<div class="clip-preview">' + pinned + (preview || item.content_type) + '</div>' +
      '<div class="clip-meta">' + esc(item.source_name || item.source_device || "") + '</div>' +
      '<div class="clip-actions">' +
        '<button class="clip-action-btn btn-pin" onclick="event.stopPropagation();togglePin(' + i + ')">' +
          (item.pinned ? 'Unpin' : 'Pin') + '</button>' +
        '<button class="clip-action-btn btn-delete" onclick="event.stopPropagation();deleteItem(' + i + ')">Delete</button>' +
      '</div></div>';
  }).join("");
}

function copyItem(index) {
  var item = _cache.history[index]; if (!item) return;
  var eid = item.entry_id; if (!eid) { showToast("No content"); return; }
  // List responses carry only metadata + text_preview; fetch the full
  // content (types) from the detail endpoint to copy it.
  api("/api/history/item?entry_id=" + encodeURIComponent(eid)).then(function(d) {
    var types = (d.item && d.item.types) || {};
    var keys = Object.keys(types); if (!keys.length) { showToast("No content"); return; }
    var decoded = atob(types[keys[0]]);
    var bytes = new Uint8Array(decoded.length);
    for (var i = 0; i < decoded.length; i++) bytes[i] = decoded.charCodeAt(i);
    var text = new TextDecoder("utf-8").decode(bytes);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function() { showToast("Copied!"); }).catch(function() {});
    } else {
      var ta = document.createElement("textarea"); ta.value = text;
      ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta);
      ta.select(); document.execCommand("copy"); document.body.removeChild(ta);
      showToast("Copied!");
    }
  }).catch(function() { showToast("Copy failed"); });
}

function pushText() {
  var text = document.getElementById("pushInput").value.trim();
  if (!text) return;
  var btn = document.getElementById("pushBtn");
  btn.disabled = true; btn.textContent = "...";
  fetch("/api/push?token=" + encodeURIComponent(TOKEN), {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({text: text})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.ok) { document.getElementById("pushInput").value = ""; showToast("Sent!"); refresh(); }
    else showToast(d.error || "Failed");
  }).catch(function() { showToast("Server error"); })
  .finally(function() { btn.disabled = false; btn.textContent = "Push"; });
}

function deleteItem(index) {
  if (!confirm("Delete this item?")) return;
  fetch("/api/delete?token=" + encodeURIComponent(TOKEN), {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({index: index})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.ok) { showToast("Deleted"); refresh(); } else showToast(d.error || "Failed");
  }).catch(function() { showToast("Server error"); });
}

function togglePin(index) {
  fetch("/api/pin?token=" + encodeURIComponent(TOKEN), {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({index: index})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.ok) { showToast(d.pinned ? "Pinned" : "Unpinned"); refresh(); }
    else showToast(d.error || "Failed");
  }).catch(function() { showToast("Server error"); });
}

function onFileSelected(input) {
  if (input.files.length) {
    _selectedFile = input.files[0];
    document.getElementById("uploadFileName").style.display = "";
    document.getElementById("uploadFileName").textContent = _selectedFile.name;
    document.getElementById("uploadBtn").disabled = false;
  }
}

function uploadFile() {
  if (!_selectedFile) return;
  var btn = document.getElementById("uploadBtn");
  btn.disabled = true; btn.textContent = "...";
  var form = new FormData(); form.append("file", _selectedFile);
  fetch("/api/upload?token=" + encodeURIComponent(TOKEN), {method: "POST", body: form})
  .then(function(r) { return r.json(); }).then(function(d) {
    if (d.ok) { showToast("Uploaded: " + d.name);
      _selectedFile = null; document.getElementById("fileInput").value = "";
      document.getElementById("uploadFileName").style.display = "none";
      document.getElementById("uploadBtn").disabled = true;
      loadFiles();
    } else showToast(d.error || "Failed");
  }).catch(function() { showToast("Server error"); })
  .finally(function() { btn.disabled = false; btn.textContent = "Upload"; });
}

function renderFiles() {
  var list = document.getElementById("fileList");
  if (!_cache.files.length) { list.innerHTML = '<div class="empty">No files</div>'; return; }
  list.innerHTML = _cache.files.map(function(f) {
    return '<div class="file-item"><div style="flex:1"><div style="font-size:13px;font-weight:600">' +
      esc(f.name) + '</div><div style="font-size:11px;color:var(--sub)">' + f.size + ' ' + f.time + '</div></div>' +
      '<button class="file-dl" onclick="downloadFile(\'' + esc(f.name) + '\')">Download</button></div>';
  }).join("");
}

function downloadFile(filename) {
  var a = document.createElement("a");
  a.href = "/api/download?file=" + encodeURIComponent(filename) + "&token=" + encodeURIComponent(TOKEN);
  a.download = filename; document.body.appendChild(a); a.click(); document.body.removeChild(a);
}

function showToast(msg) {
  var el = document.getElementById("toast"); el.textContent = msg; el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function() { el.classList.remove("show"); }, 1800);
}

(function() {
  refresh(); setInterval(refresh, 3000);
})();
</script>
</body>
</html>"""


# ── Static file directory ───────────────────────────────────────

def _get_static_dir() -> str:
    """Return the path to the static files directory."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ── Token validation helper ─────────────────────────────────────

def _validate_token(path: str, expected_token: str) -> bool:
    """Check that the query string contains the expected token."""
    qs = urllib.parse.urlparse(path).query
    params = urllib.parse.parse_qs(qs)
    tokens = params.get("token", [])
    # Constant-time comparison to avoid leaking token bytes via timing.
    return len(tokens) == 1 and hmac.compare_digest(tokens[0], expected_token)


# ── Safe request body reader ─────────────────────────────────────

# Upper bound on a single request body read.  Guards against a malicious or
# buggy client sending a huge Content-Length and wedging a worker thread
# waiting on bytes that never arrive.  Web uploads are the only large body
# this server accepts and are capped well below this.
_MAX_BODY_BYTES = 128 * 1024 * 1024


def _read_request_body(handler) -> bytes:
    """Read the request body per Content-Length, safely.

    Returns b"" when Content-Length is absent, malformed, non-positive, or
    exceeds _MAX_BODY_BYTES.  The endpoint then surfaces a 4xx response
    instead of a worker thread dying on a ValueError from int().
    """
    raw = handler.headers.get("Content-Length")
    if raw is None:
        return b""
    try:
        length = int(raw)
    except (ValueError, TypeError):
        return b""
    if length <= 0 or length > _MAX_BODY_BYTES:
        return b""
    return handler.rfile.read(length)


# ── File download response builder ──────────────────────────────

def _build_file_response(filepath: str, mime: str = "application/octet-stream"):
    """Build headers + body for a file download response.

    Returns (status, headers, body_bytes) tuple.
    Returns (404, ...) if file not found.
    """
    if not os.path.isfile(filepath):
        return 404, {"Content-Type": "application/json"}, json.dumps({"error": "not found"}).encode("utf-8")

    fsize = os.path.getsize(filepath)
    fname = os.path.basename(filepath)
    try:
        fname.encode("latin-1")
        disp = f'attachment; filename="{fname}"'
    except UnicodeEncodeError:
        encoded = urllib.parse.quote(fname, safe="")
        disp = f"attachment; filename=\"download\"; filename*=UTF-8''{encoded}"

    headers = {
        "Content-Type": mime,
        "Content-Length": str(fsize),
        "Content-Disposition": disp,
        "Cache-Control": "no-cache",
    }
    # For large files, return the filepath for streaming instead of loading into memory
    return 200, headers, filepath


# ═══════════════════════════════════════════════════════════════════
# ── WEB SERVER ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════

class WebServer:
    """Lightweight HTTP server for the ClipSync web companion."""

    FW_RULE_NAME = "ClipSync Web Companion"

    def __init__(self, cfg, clipboard_history, sync_mgr, get_connected_ids=None,
                 on_nav_url=None, on_forward_file=None,
                 get_overview_data=None, on_device_action=None,
                 on_transfer_action=None,
                 on_get_transfers=None,
                 on_speed_test_start=None, on_speed_test_poll=None,
                 on_window_close=None,
                 on_toggle_discovery=None, on_toggle_visibility=None,
                 on_settings_change=None,
                 on_show_web_qr=None, on_send_url=None,
                 get_discovered_peers=None,
                 get_resolved_hashes=None, get_pending_pairings=None,
                 enc_mgr=None, on_open_file=None, on_open_folder=None,
                 on_restart=None, on_reset_dedup=None,
                 get_certs=None, get_diagnostics=None,
                 on_update_download=None,
                 on_diagnostics_request=None):
        self._cfg = cfg
        self._sync_mgr = sync_mgr
        self._get_connected_ids = get_connected_ids
        self._get_discovered_peers = get_discovered_peers
        self._get_resolved_hashes = get_resolved_hashes
        self._get_pending_pairings = get_pending_pairings
        self._enc_mgr = enc_mgr
        self._on_open_file = on_open_file
        self._on_open_folder = on_open_folder
        self._on_restart = on_restart
        # Callback invoked before a web-driven clipboard write (paste_rich) so
        # the sync manager's dedup state is cleared and the write re-syncs to
        # peers.  Defaults to the sync manager's own method when available.
        if on_reset_dedup is None:
            _sync_mgr = self._sync_mgr
            def _default_reset_dedup():
                try:
                    if _sync_mgr is not None and hasattr(_sync_mgr, "reset_dedup_for_restore"):
                        _sync_mgr.reset_dedup_for_restore()
                except Exception:
                    logger.debug("reset_dedup_for_restore failed", exc_info=True)
            self._on_reset_dedup = _default_reset_dedup
        else:
            self._on_reset_dedup = on_reset_dedup
        self._on_show_web_qr = on_show_web_qr
        self._on_send_url = on_send_url
        self._on_nav_url = on_nav_url
        self._on_forward_file = on_forward_file
        self._get_overview_data = get_overview_data
        self._on_device_action = on_device_action
        self._on_transfer_action = on_transfer_action
        self._on_get_transfers = on_get_transfers
        self._on_speed_test_start = on_speed_test_start
        self._on_speed_test_poll = on_speed_test_poll
        self._on_window_close = on_window_close
        self._on_toggle_discovery = on_toggle_discovery
        self._on_toggle_visibility = on_toggle_visibility
        self._on_settings_change = on_settings_change
        self._get_certs = get_certs
        self._get_diagnostics = get_diagnostics
        self._on_update_download = on_update_download
        self._on_diagnostics_request = on_diagnostics_request
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._firewall_ok: bool = False
        # Pre-generate PWA icons
        self._icon_192 = _make_icon(192)
        self._icon_512 = _make_icon(512)
        self._upload_dir = _get_upload_dir()
        self._static_dir = _get_static_dir()

        # ── Upgrade history to SQLite-backed storage if needed ───
        self._history = self._upgrade_history(clipboard_history)

        # Import ws module lazily to avoid circular imports
        from internal.web.ws import WebSocketManager
        from internal.web.dialog import DialogManager

        self._ws_manager = WebSocketManager(
            cfg=cfg,
            history=self._history,
            sync_mgr=sync_mgr,
            get_connected_ids=get_connected_ids,
            get_discovered=get_discovered_peers,
            get_resolved_hashes=get_resolved_hashes,
            get_pending_pairings=get_pending_pairings,
        )

        self._dialog_mgr = DialogManager()
        self._dialog_mgr.ws_manager = self._ws_manager
        # When a new web client attaches, flush any dialogs that were queued
        # while no client was connected (e.g. an incoming file transfer that
        # arrived "blind") so they are shown instead of silently rejected.
        self._ws_manager.on_client_attached = self._dialog_mgr.flush_pending

    # ── History upgrade ──────────────────────────────────────────

    @staticmethod
    def _upgrade_history(clipboard_history):
        """Upgrade a ClipboardHistory to ClipboardHistoryDB if needed.

        If *clipboard_history* is already a ClipboardHistoryDB, return
        it unchanged.  If it is the legacy ClipboardHistory, create a
        ClipboardHistoryDB instance (which auto-migrates from the JSON
        file) and return that.

        Returns the history instance to use (ClipboardHistoryDB).
        """
        from internal.clipboard.history import ClipboardHistory
        from internal.clipboard.history_db import ClipboardHistoryDB

        if isinstance(clipboard_history, ClipboardHistoryDB):
            return clipboard_history

        if isinstance(clipboard_history, ClipboardHistory):
            logger.info(
                "Upgrading ClipboardHistory to ClipboardHistoryDB "
                "(auto-migrating from JSON if available)"
            )
            enc_mgr = getattr(clipboard_history, '_enc_mgr', None)
            max_entries = getattr(clipboard_history, 'MAX_ENTRIES', 50)
            return ClipboardHistoryDB(
                max_entries=max_entries,
                enc_mgr=enc_mgr,
            )

        # None or unknown type — create a fresh DB
        logger.info("Creating new ClipboardHistoryDB instance")
        return ClipboardHistoryDB()

    # ── Public properties ────────────────────────────────────────

    @property
    def firewall_ok(self) -> bool:
        return self._firewall_ok

    @property
    def is_running(self) -> bool:
        if self._thread is None or not self._thread.is_alive() or self._httpd is None:
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            sock.connect(("127.0.0.1", self._cfg.web_port))
            sock.close()
            return True
        except Exception:
            return False

    @property
    def ws_manager(self):
        """Expose the WebSocket manager for external use (e.g., by sync_mgr)."""
        return self._ws_manager

    @property
    def dialog_mgr(self):
        """Expose the DialogManager for webview-mode dialog replacement."""
        return self._dialog_mgr

    # ── Firewall management ──────────────────────────────────────

    @staticmethod
    def check_firewall_rule(ports: int | list[int] | None = None) -> tuple[bool, str]:
        """Check that the ClipSync rule allows all the given ports.

        `ports` may be a single int or a list of ints. The app needs both the
        TCP sync port and the web companion port open, and a Windows rule's
        LocalPort is a comma-separated list — so every requested port must be
        present. Returns (True, "OK") when all ports are allowed.
        """
        if sys.platform != "win32":
            return (True, "")
        if isinstance(ports, int):
            ports = [ports]
        ports = [str(p) for p in (ports or [])]
        import re
        try:
            import subprocess
            check = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 f"name={WebServer.FW_RULE_NAME}", "verbose"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
            )
            if check.returncode != 0 or WebServer.FW_RULE_NAME not in check.stdout:
                return (False, "Blocked")
            m = re.search(r"LocalPort:\s+(\S+)", check.stdout)
            present = set(m.group(1).split(",")) if m else set()
            missing = [p for p in ports if p not in present]
            if missing:
                actual = m.group(1) if m else "none"
                return (False, f"Wrong port (got {actual}, needs {','.join(missing)})")
            return (True, "OK")
        except Exception:
            return (False, "Unknown")

    def _open_firewall(self, port: int, web_port: int | None = None) -> bool:
        if sys.platform != "win32":
            return True
        # One rule must cover both the TCP sync port and the web companion
        # port, otherwise the diagnostics check / other peers on the TCP port
        # would report a "wrong port" mismatch against the single-port rule.
        ports = [port]
        if web_port and web_port != port:
            ports.append(web_port)
        import subprocess
        ok, detail = WebServer.check_firewall_rule(ports)
        if ok:
            return True
        if detail.startswith("Wrong port"):
            logger.info("Deleting stale firewall rule with wrong port")
            try:
                subprocess.run(
                    ["netsh", "advfirewall", "firewall", "delete", "rule",
                     f"name={WebServer.FW_RULE_NAME}"],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=10,
                )
            except Exception:
                pass
        try:
            result = subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 f"name={WebServer.FW_RULE_NAME}",
                 "dir=in", "action=allow",
                 f"localport={','.join(map(str, ports))}", "protocol=TCP",
                 "profile=any"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
            )
            if result.returncode == 0:
                logger.info("Firewall rule created for port %s",
                            ",".join(map(str, ports)))
                return True
            else:
                logger.warning("Failed to create firewall rule: %s",
                               result.stderr.strip() or result.stdout.strip())
                return False
        except Exception as e:
            logger.warning("Firewall setup error: %s", e)
            return False

    def _open_firewall_elevated(self, port: int, web_port: int | None = None) -> bool:
        """Repair the firewall rule through a UAC-elevated netsh call.

        Deleting/creating rules via netsh needs admin rights, and most runs
        are not elevated. When the unelevated attempt fails, write a small
        batch file with the exact delete+add commands and launch it with
        ShellExecuteW's "runas" verb so the user gets a UAC prompt. Returns
        True if the prompt was accepted (the elevated process then applies
        the rule), False if it was cancelled or failed to launch.
        """
        if sys.platform != "win32":
            return True
        ports = [port]
        if web_port and web_port != port:
            ports.append(web_port)
        import os as _os
        import tempfile as _tempfile
        bat = _os.path.join(
            _tempfile.gettempdir(),
            "clipsync_firewall_%s.bat" % "_".join(map(str, ports)))
        try:
            # Overwrite any pre-existing file so nothing stale runs elevated.
            with open(bat, "w", encoding="utf-8") as _f:
                _f.write("\r\n".join([
                    "@echo off",
                    f'netsh advfirewall firewall delete rule name="{WebServer.FW_RULE_NAME}" >nul 2>&1',
                    f'netsh advfirewall firewall add rule name="{WebServer.FW_RULE_NAME}" '
                    f'dir=in action=allow localport={",".join(map(str, ports))} protocol=TCP profile=any',
                ]) + "\r\n")
            import ctypes
            # ShellExecuteW returns a value >32 on success, but 1223
            # (ERROR_CANCELLED) when the user declined the UAC prompt —
            # that is a failure even though it is numerically >32.
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", bat, "", None, 1)
            return result > 32 and result != 1223
        except Exception:
            return False

    # ── Start / Stop ────────────────────────────────────────────

    def start(self) -> bool:
        if self._thread is not None:
            return True
        # Bind all interfaces: the dashboard serves the local webview AND
        # phones/tablets/other devices on the LAN (a core feature). Whether
        # remote devices may access is controlled by cfg.web_enabled (see
        # _companion_client_ok) — the server itself stays up for the UI.
        host = "0.0.0.0"
        port = self._cfg.web_port
        logger.info("Starting web companion on %s:%d", host, port)
        # Allow both the TCP sync port and the web companion port.
        self._firewall_ok = self._open_firewall(self._cfg.port, self._cfg.web_port)

        # Capture all dependencies for the handler closure
        cfg = self._cfg
        history = self._history
        sync_mgr = self._sync_mgr
        get_connected_ids = self._get_connected_ids
        get_discovered_peers = self._get_discovered_peers
        get_resolved_hashes = self._get_resolved_hashes
        get_pending_pairings = self._get_pending_pairings
        enc_mgr = self._enc_mgr
        on_open_file = self._on_open_file
        on_open_folder = self._on_open_folder
        on_restart = self._on_restart
        on_reset_dedup = self._on_reset_dedup
        on_show_web_qr = self._on_show_web_qr
        on_send_url = self._on_send_url
        on_nav_url = self._on_nav_url
        on_forward_file = self._on_forward_file
        get_overview_data = self._get_overview_data
        on_device_action = self._on_device_action
        on_transfer_action = self._on_transfer_action
        on_get_transfers = self._on_get_transfers
        on_speed_test_start = self._on_speed_test_start
        on_speed_test_poll = self._on_speed_test_poll
        on_window_close = self._on_window_close
        on_toggle_discovery = self._on_toggle_discovery
        on_toggle_visibility = self._on_toggle_visibility
        on_settings_change = self._on_settings_change
        get_certs = self._get_certs
        on_diagnostics_request = self._on_diagnostics_request
        get_diagnostics = self._get_diagnostics
        on_update_download = self._on_update_download
        upload_dir = self._upload_dir
        static_dir = self._static_dir
        icon_192 = self._icon_192
        icon_512 = self._icon_512
        ws_manager = self._ws_manager
        # Cap the number of concurrently handled connections so a flood of
        # slow clients cannot exhaust worker threads.
        connection_semaphore = threading.Semaphore(64)
        import internal.web.routes as api_routes

        class _Handler(BaseHTTPRequestHandler):
            # Slowloris defence: bound every socket operation on this
            # connection.  A client that opens a socket but never finishes the
            # request line / headers (or stalls mid-body) times out instead of
            # occupying a worker thread forever.  StreamRequestHandler.setup()
            # applies this to the underlying socket.
            timeout = 30

            def log_message(inner_self, fmt, *args):
                pass

            def handle(inner_self):
                # Cap the number of concurrently handled connections so a
                # flood of slow clients cannot exhaust worker threads.  When
                # the semaphore is exhausted the connection is rejected
                # outright rather than queued (which would grow threads
                # unboundedly).
                if not connection_semaphore.acquire(blocking=False):
                    try:
                        inner_self.connection.close()
                    except OSError:
                        pass
                    return
                try:
                    super().handle()
                finally:
                    connection_semaphore.release()

            def handle_one_request(self):
                # The client (web UI) disconnects abruptly when the app
                # restarts or the window closes mid-response. Writing the
                # response then raises BrokenPipeError/ConnectionResetError,
                # which would otherwise spill a noisy traceback to stderr on
                # every restart. Swallow those and mark the connection closed.
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True

            def _token_ok(inner_self) -> bool:
                return _validate_token(inner_self.path, cfg.web_token)

            def _companion_client_ok(inner_self) -> bool:
                """Allow the request when the companion serves it.

                The web companion's job is to serve the dashboard to phones /
                LAN devices. The local webview dashboard is reached over
                127.0.0.1. When the companion is turned off the server keeps
                running for the local dashboard but must refuse non-local
                clients — otherwise the dashboard breaks (it IS served by
                this server).
                """
                if cfg.web_enabled:
                    return True
                host = (inner_self.client_address or ("", 0))[0]
                return host in ("127.0.0.1", "::1")

            @staticmethod
            def _available_locales() -> set:
                """Return the set of available locale codes, discovered from
                JSON files in static/locales/ plus any registered in the
                Python i18n module."""
                locales = set()
                locales_dir = os.path.join(static_dir, "locales")
                try:
                    for fname in os.listdir(locales_dir):
                        if fname.endswith(".json"):
                            locales.add(fname[:-5])  # strip .json
                except OSError:
                    pass
                from internal.i18n import LOCALES
                locales.update(LOCALES.keys())
                return locales

            @staticmethod
            def _load_i18n_translations(cfg) -> str:
                """Load translations for the configured locale from a JSON
                locale file.  Falls back to the Python i18n dicts if the
                JSON file doesn't exist yet.

                The compiled JSON is cached per locale so static-asset
                requests don't re-read/re-parse the file every time."""
                from internal.i18n import LOCALES
                from internal.version import __version__
                locale = cfg.language if cfg.language in _Handler._available_locales() else "en"

                cached = _i18n_cache.get(locale)
                if cached is not None:
                    return cached

                # Try JSON locale file first
                locales_dir = os.path.join(static_dir, "locales")
                json_path = os.path.join(locales_dir, f"{locale}.json")
                try:
                    if not os.path.isfile(json_path):
                        raise OSError("no locale JSON file")
                    with open(json_path, "r", encoding="utf-8") as f:
                        translations = json.load(f)
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning("Failed to load locale JSON: %s, falling back to Python dict", json_path)
                    translations = dict(LOCALES.get(locale, LOCALES.get("en", {})))

                # The version lives in exactly one place (internal/version.py);
                # inject it here so the web UI never carries its own copy.
                translations["settings_window.about_version"] = "v" + __version__
                result = json.dumps(translations, ensure_ascii=False)
                _i18n_cache[locale] = result
                return result

            def _send_json(inner_self, data, status=200):
                inner_self.send_response(status)
                inner_self.send_header("Content-Type", "application/json; charset=utf-8")
                inner_self.send_header("Cache-Control", "no-cache")
                inner_self.send_header("Access-Control-Allow-Origin", "*")
                inner_self.end_headers()
                try:
                    inner_self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
                except OSError:
                    pass

            def _send_html(inner_self, html: str, status=200):
                inner_self.send_response(status)
                inner_self.send_header("Content-Type", "text/html; charset=utf-8")
                inner_self.send_header("Cache-Control", "no-cache")
                inner_self.end_headers()
                try:
                    inner_self.wfile.write(html.encode("utf-8"))
                except OSError:
                    pass

            def _send_companion_disabled_page(inner_self) -> None:
                """Serve a friendly page when the companion is off and a browser
                (phone / tablet) opens the dashboard — not a raw JSON error."""
                if cfg.language == "zh-CN":
                    title, desc = "Web 伴侣已关闭", "该设备的 Web 伴侣已被关闭，暂时无法访问其仪表盘。"
                else:
                    title, desc = ("Web companion disabled",
                                   "This device's web companion is turned off, so its "
                                   "dashboard is temporarily unavailable.")
                html = (
                    "<!doctype html><html lang='zh'><meta charset='utf-8'>"
                    "<title>" + title + "</title>"
                    "<body style='font-family:system-ui,-apple-system,sans-serif;"
                    "background:#0f1117;color:#e6e8ee;display:flex;align-items:center;"
                    "justify-content:center;height:100vh;margin:0'>"
                    "<div style='text-align:center;max-width:26rem;padding:1.5rem'>"
                    "<div style='font-size:2rem;margin-bottom:.5rem'>&#128241;&#128279;</div>"
                    "<h1 style='font-size:18px;margin:0 0 .5rem'>" + title + "</h1>"
                    "<p style='color:#9aa0ad;font-size:14px;line-height:1.5'>" + desc + "</p>"
                    "</div></body></html>"
                )
                inner_self._send_html(html, status=403)

            def _send_file(inner_self, filepath: str, mime: str = "application/octet-stream"):
                status, headers, body = _build_file_response(filepath, mime)
                inner_self.send_response(status)
                for key, val in headers.items():
                    inner_self.send_header(key, val)
                inner_self.send_header("Access-Control-Allow-Origin", "*")
                inner_self.end_headers()
                if status == 200:
                    try:
                        if isinstance(body, str) and os.path.isfile(body):
                            # Stream in chunks so large files are not loaded
                            # into memory all at once.
                            with open(body, "rb") as f:
                                while True:
                                    chunk = f.read(64 * 1024)
                                    if not chunk:
                                        break
                                    inner_self.wfile.write(chunk)
                        else:
                            inner_self.wfile.write(body)
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                        pass

            def _serve_static(inner_self, rel_path: str, mime: str | None = None):
                """Serve a file from the static directory. Text files get placeholder interpolation."""
                # Security: prevent path traversal
                safe_path = rel_path.lstrip("/").replace("\\", "/")
                if ".." in safe_path:
                    inner_self._send_json({"error": "forbidden"}, 403)
                    return

                full_path = os.path.normpath(os.path.join(static_dir, safe_path))
                # Ensure we don't escape the static directory
                if not full_path.startswith(os.path.normpath(static_dir)):
                    inner_self._send_json({"error": "forbidden"}, 403)
                    return

                if not os.path.isfile(full_path):
                    inner_self._send_json({"error": "not found"}, 404)
                    return

                # Determine MIME type with explicit fallbacks for Windows
                if mime is None:
                    mime, _ = mimetypes.guess_type(full_path)
                if mime is None:
                    # Explicit MIME map for common web assets (mimetypes may miss on Windows)
                    _MIME_MAP = {
                        ".js": "application/javascript; charset=utf-8",
                        ".mjs": "application/javascript; charset=utf-8",
                        ".css": "text/css; charset=utf-8",
                        ".html": "text/html; charset=utf-8",
                        ".json": "application/json; charset=utf-8",
                        ".svg": "image/svg+xml",
                        ".png": "image/png",
                        ".ico": "image/x-icon",
                        ".woff2": "font/woff2",
                    }
                    ext = os.path.splitext(full_path)[1].lower()
                    mime = _MIME_MAP.get(ext, "application/octet-stream")

                # Interpolate placeholders in text files (.html, .js, .css, .json, .svg)
                text_exts = (".html", ".js", ".css", ".json", ".svg")
                if safe_path.endswith(text_exts) or mime.startswith("text/"):
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        # Only do full interpolation for HTML files
                        is_html = safe_path.endswith(".html")
                        content = inner_self._interpolate_html(content, is_html)
                        data = content.encode("utf-8")
                    except Exception:
                        with open(full_path, "rb") as f:
                            data = f.read()
                else:
                    with open(full_path, "rb") as f:
                        data = f.read()

                inner_self.send_response(200)
                inner_self.send_header("Content-Type", mime)
                inner_self.send_header("Content-Length", str(len(data)))
                # no-cache so the browser always revalidates: without this the
                # WebView would serve a 1-hour-stale copy of CSS/JS and edits
                # would never appear after a restart.
                inner_self.send_header("Cache-Control", "no-cache")
                inner_self.send_header("Access-Control-Allow-Origin", "*")
                inner_self.end_headers()
                try:
                    inner_self.wfile.write(data)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass

            def _interpolate_html(self, content: str, is_html: bool = True) -> str:
                """Replace placeholders in static HTML/JS with runtime values.
                For HTML files: full interpolation (i18n, token, device info)
                For other files (JS/CSS): only token interpolation
                Translations are loaded from JSON locale files in
                internal/web/static/locales/, falling back to the Python
                i18n dicts for backward compatibility.
                """
                i18n_json = self._load_i18n_translations(cfg)
                locale = cfg.language if cfg.language in self._available_locales() else "en"
                locale_code_json = json.dumps(locale, ensure_ascii=False)

                # Always replace token (for cache busting and auth)
                content = content.replace("__TOKEN__", cfg.web_token)

                # Full interpolation only for HTML files
                if is_html:
                    replacements = [
                        ("__CLIPSYNC_I18N_DATA__", i18n_json),
                        ("__CLIPSYNC_I18N_LOCALE__", locale_code_json),
                        ("__I18N__", i18n_json),
                        # JSON-encode so a device name containing a quote or
                        # script tag cannot break the inline <script> literals.
                        ("__DEVICE_ID__", json.dumps(cfg.device_id, ensure_ascii=False)),
                        ("__DEVICE_NAME__", json.dumps(cfg.device_name, ensure_ascii=False)),
                    ]
                    for placeholder, value in replacements:
                        if placeholder in content:
                            content = content.replace(placeholder, value)

                return content

            # ── HTTP method handlers ─────────────────────────────

            def do_OPTIONS(inner_self):
                inner_self.send_response(204)
                inner_self.send_header("Access-Control-Allow-Origin", "*")
                inner_self.send_header("Access-Control-Allow-Methods",
                                       "GET, POST, PUT, PATCH, DELETE, OPTIONS")
                inner_self.send_header("Access-Control-Allow-Headers",
                                       "Content-Type, Authorization, X-Requested-With")
                inner_self.send_header("Access-Control-Max-Age", "86400")
                inner_self.end_headers()

            def do_GET(inner_self):
                parsed = urllib.parse.urlparse(inner_self.path)
                path = parsed.path
                qs = parsed.query
                query_params = urllib.parse.parse_qs(qs)

                # Companion off → local dashboard only. Browsers (phone/tablet)
                # opening the dashboard get a friendly page, not raw JSON.
                if not inner_self._companion_client_ok():
                    if path.startswith("/api/") or path == "/ws":
                        inner_self._send_json({"error": "web companion disabled"}, 403)
                    else:
                        inner_self._send_companion_disabled_page()
                    return

                # ── WebSocket upgrade at /ws ─────────────────────
                if path == "/ws":
                    # Token validation for WebSocket
                    if not inner_self._token_ok():
                        inner_self._send_json({"error": "invalid token"}, 403)
                        return
                    # Check for WebSocket upgrade headers
                    upgrade = inner_self.headers.get("Upgrade", "").lower()
                    if upgrade == "websocket":
                        # Build headers dict for ws manager
                        ws_headers = {}
                        for key, val in inner_self.headers.items():
                            ws_headers[key.lower()] = val
                        # Perform handshake
                        client = ws_manager.handle_handshake(
                            inner_self.request, inner_self.client_address, ws_headers
                        )
                        if client is None:
                            inner_self._send_json({"error": "websocket upgrade failed"}, 400)
                            return
                        # Enter read loop — blocks until client disconnects
                        client.serve()
                        ws_manager.remove_client(client)
                        return
                    else:
                        inner_self._send_json({"error": "websocket upgrade required"}, 426)
                        return

                # ── PWA icons and manifest (no token needed for icons) ─
                if path == "/icon-192.png":
                    inner_self.send_response(200)
                    inner_self.send_header("Content-Type", "image/png")
                    inner_self.send_header("Content-Length", str(len(icon_192)))
                    inner_self.send_header("Cache-Control", "public, max-age=86400")
                    inner_self.send_header("Access-Control-Allow-Origin", "*")
                    inner_self.end_headers()
                    try:
                        inner_self.wfile.write(icon_192)
                    except OSError:
                        pass
                    return

                if path == "/icon-512.png":
                    inner_self.send_response(200)
                    inner_self.send_header("Content-Type", "image/png")
                    inner_self.send_header("Content-Length", str(len(icon_512)))
                    inner_self.send_header("Cache-Control", "public, max-age=86400")
                    inner_self.send_header("Access-Control-Allow-Origin", "*")
                    inner_self.end_headers()
                    try:
                        inner_self.wfile.write(icon_512)
                    except OSError:
                        pass
                    return

                # ── Token validation for all other paths ─────────
                if not inner_self._token_ok():
                    inner_self._send_json({"error": "invalid token"}, 403)
                    return

                # ── Manifest ─────────────────────────────────────
                if path == "/manifest.json":
                    manifest = {
                        "name": "ClipSync Web",
                        "short_name": "ClipSync",
                        "start_url": f"/?token={cfg.web_token}",
                        "display": "standalone",
                        "background_color": "#F5F7FA",
                        "theme_color": "#1A5276",
                        "icons": [
                            {"src": f"/icon-192.png?token={cfg.web_token}",
                             "sizes": "192x192", "type": "image/png"},
                            {"src": f"/icon-512.png?token={cfg.web_token}",
                             "sizes": "512x512", "type": "image/png"},
                        ],
                    }
                    inner_self._send_json(manifest)
                    return

                # ── Index page ───────────────────────────────────
                if path == "/" or path == "/index.html":
                    # Try static/index.html first, fall back to embedded page
                    static_index = os.path.join(static_dir, "index.html")
                    if os.path.isfile(static_index):
                        try:
                            with open(static_index, "r", encoding="utf-8") as f:
                                html = f.read()
                            html = inner_self._interpolate_html(html)
                            inner_self._send_html(html)
                        except OSError:
                            inner_self._send_json({"error": "failed to load page"}, 500)
                    else:
                        html = _FALLBACK_HTML.replace("__TOKEN__", cfg.web_token)
                        html = html.replace("__DEVICE_ID__", cfg.device_id)
                        html = html.replace("__DEVICE_NAME__", cfg.device_name)
                        inner_self._send_html(html)
                    return

                # ── API routes ───────────────────────────────────
                if path.startswith("/api/"):
                    if path == "/api/download":
                        # File download: handled directly for streaming
                        fname = (query_params.get("file", [""])[0] or "").strip()
                        if not fname:
                            inner_self._send_json({"error": "invalid filename"}, 400)
                            return
                        # Only a bare filename is allowed: no path separators
                        # and no drive-relative names (e.g. "C:evil" on Windows
                        # is drive-relative and could escape the upload dir).
                        if os.path.basename(fname) != fname or ":" in fname:
                            inner_self._send_json({"error": "invalid filename"}, 400)
                            return
                        # Defense-in-depth: resolve and re-confine against the
                        # upload dir (also guards symlinks inside it).
                        from internal.web.api.security import confine_path
                        safe = confine_path(os.path.join(upload_dir, fname), upload_dir)
                        if safe is None:
                            inner_self._send_json({"error": "invalid filename"}, 400)
                            return
                        inner_self._send_file(str(safe))
                        return

                    # Delegate to routes module
                    status, content_type, body_bytes = api_routes.dispatch(
                        method="GET",
                        path=path,
                        query_params=query_params,
                        body=b"",
                        cfg=cfg,
                        history=history,
                        sync_mgr=sync_mgr,
                        get_connected_ids=get_connected_ids,
                        get_discovered=get_discovered_peers,
                        on_nav_url=on_nav_url,
                        on_forward_file=on_forward_file,
                        upload_dir=upload_dir,
                        dialog_mgr=self._dialog_mgr,
                        get_overview_data=get_overview_data,
                        on_device_action=on_device_action,
                        on_transfer_action=on_transfer_action,
                        on_get_transfers=on_get_transfers,
                        on_speed_test_start=on_speed_test_start,
                        on_speed_test_poll=on_speed_test_poll,
                        on_window_close=on_window_close,
                        on_toggle_discovery=on_toggle_discovery,
                        on_toggle_visibility=on_toggle_visibility,
                        get_resolved_hashes=get_resolved_hashes,
                        get_pending_pairings=get_pending_pairings,
                        enc_mgr=enc_mgr,
                        on_open_file=on_open_file,
                        on_open_folder=on_open_folder,
                        on_restart=on_restart,
                        on_reset_dedup=on_reset_dedup,
                        get_certs=get_certs,
                        get_diagnostics=get_diagnostics,
                        on_update_download=on_update_download,
                    )
                    inner_self.send_response(status)
                    inner_self.send_header("Content-Type", content_type)
                    inner_self.send_header("Cache-Control", "no-cache")
                    inner_self.send_header("Access-Control-Allow-Origin", "*")
                    inner_self.end_headers()
                    try:
                        inner_self.wfile.write(body_bytes)
                    except OSError:
                        pass
                    return

                # ── Static file serving ─────────────────────────
                serve_path = path if path != "/" else "/index.html"
                inner_self._serve_static(serve_path)

            def do_POST(inner_self):
                parsed = urllib.parse.urlparse(inner_self.path)
                path = parsed.path
                qs = parsed.query
                query_params = urllib.parse.parse_qs(qs)

                # Companion off → local dashboard only (LAN/phones get 403).
                if not inner_self._companion_client_ok():
                    inner_self._send_json({"error": "web companion disabled"}, 403)
                    return

                if not inner_self._token_ok():
                    inner_self._send_json({"error": "invalid token"}, 403)
                    return

                body = _read_request_body(inner_self)

                # ── File upload: handled directly (multipart) ────
                if path == "/api/upload":
                    content_type = inner_self.headers.get("Content-Type", "")
                    if "multipart" not in content_type:
                        inner_self._send_json({"ok": False, "error": "expect multipart/form-data"}, 400)
                        return
                    fields = _parse_multipart(body, content_type)
                    file_field = fields.get("file")
                    if not file_field:
                        inner_self._send_json({"ok": False, "error": "no file field"}, 400)
                        return
                    fname, fdata = file_field
                    if not fname:
                        fname = "uploaded_file"
                    target_device = ""
                    target_field = fields.get("device_id")
                    if target_field:
                        target_device = target_field[1].decode("utf-8", errors="replace")
                    # Sanitize filename
                    safe_name = os.path.basename(fname).replace("\\", "_").replace("/", "_")
                    if not safe_name:
                        safe_name = "uploaded_file"
                    dest = os.path.join(upload_dir, safe_name)
                    # Avoid overwriting
                    base, ext = os.path.splitext(safe_name)
                    counter = 1
                    while os.path.exists(dest):
                        dest = os.path.join(upload_dir, f"{base} ({counter}){ext}")
                        counter += 1
                    with open(dest, "wb") as f:
                        f.write(fdata)
                    logger.info("Web upload: %s (%d bytes) -> %s", safe_name, len(fdata), dest)
                    if target_device and target_device != cfg.device_id and on_forward_file:
                        on_forward_file(dest, target_device)
                    inner_self._send_json({"ok": True, "name": os.path.basename(dest), "size": len(fdata)})
                    return

                # ── Delegate other API routes ────────────────────
                if path.startswith("/api/"):
                    status, content_type, body_bytes = api_routes.dispatch(
                        method="POST",
                        path=path,
                        query_params=query_params,
                        body=body,
                        cfg=cfg,
                        history=history,
                        sync_mgr=sync_mgr,
                        get_connected_ids=get_connected_ids,
                        get_discovered=get_discovered_peers,
                        on_nav_url=on_nav_url,
                        on_forward_file=on_forward_file,
                        upload_dir=upload_dir,
                        dialog_mgr=self._dialog_mgr,
                        get_overview_data=get_overview_data,
                        on_device_action=on_device_action,
                        on_transfer_action=on_transfer_action,
                        on_get_transfers=on_get_transfers,
                        on_speed_test_start=on_speed_test_start,
                        on_speed_test_poll=on_speed_test_poll,
                        on_window_close=on_window_close,
                        on_toggle_discovery=on_toggle_discovery,
                        on_toggle_visibility=on_toggle_visibility,
                        on_settings_change=on_settings_change,
                        on_show_web_qr=on_show_web_qr,
                        on_send_url=on_send_url,
                        get_resolved_hashes=get_resolved_hashes,
                        get_pending_pairings=get_pending_pairings,
                        enc_mgr=enc_mgr,
                        on_open_file=on_open_file,
                        on_open_folder=on_open_folder,
                        on_restart=on_restart,
                        on_reset_dedup=on_reset_dedup,
                        get_certs=get_certs,
                        get_diagnostics=get_diagnostics,
                        on_update_download=on_update_download,
                    )
                    inner_self.send_response(status)
                    inner_self.send_header("Content-Type", content_type)
                    inner_self.send_header("Cache-Control", "no-cache")
                    inner_self.send_header("Access-Control-Allow-Origin", "*")
                    inner_self.end_headers()
                    try:
                        inner_self.wfile.write(body_bytes)
                    except OSError:
                        pass
                    return

                inner_self._send_json({"error": "not found"}, 404)

            def do_DELETE(inner_self):
                parsed = urllib.parse.urlparse(inner_self.path)
                path = parsed.path
                qs = parsed.query
                query_params = urllib.parse.parse_qs(qs)

                if not inner_self._token_ok():
                    inner_self._send_json({"error": "invalid token"}, 403)
                    return

                body = _read_request_body(inner_self)

                if path.startswith("/api/"):
                    status, content_type, body_bytes = api_routes.dispatch(
                        method="DELETE",
                        path=path,
                        query_params=query_params,
                        body=body,
                        cfg=cfg,
                        history=history,
                        sync_mgr=sync_mgr,
                        get_connected_ids=get_connected_ids,
                        get_discovered=get_discovered_peers,
                        on_nav_url=on_nav_url,
                        on_forward_file=on_forward_file,
                        upload_dir=upload_dir,
                        dialog_mgr=self._dialog_mgr,
                        get_overview_data=get_overview_data,
                        on_device_action=on_device_action,
                        on_transfer_action=on_transfer_action,
                        on_get_transfers=on_get_transfers,
                        on_speed_test_start=on_speed_test_start,
                        on_speed_test_poll=on_speed_test_poll,
                        on_window_close=on_window_close,
                        on_toggle_discovery=on_toggle_discovery,
                        on_toggle_visibility=on_toggle_visibility,
                    )
                    inner_self.send_response(status)
                    inner_self.send_header("Content-Type", content_type)
                    inner_self.send_header("Cache-Control", "no-cache")
                    inner_self.send_header("Access-Control-Allow-Origin", "*")
                    inner_self.end_headers()
                    try:
                        inner_self.wfile.write(body_bytes)
                    except OSError:
                        pass
                    return

                inner_self._send_json({"error": "not found"}, 404)

            def do_PATCH(inner_self):
                parsed = urllib.parse.urlparse(inner_self.path)
                path = parsed.path
                qs = parsed.query
                query_params = urllib.parse.parse_qs(qs)

                if not inner_self._token_ok():
                    inner_self._send_json({"error": "invalid token"}, 403)
                    return

                body = _read_request_body(inner_self)

                if path.startswith("/api/"):
                    status, content_type, body_bytes = api_routes.dispatch(
                        method="PATCH",
                        path=path,
                        query_params=query_params,
                        body=body,
                        cfg=cfg,
                        history=history,
                        sync_mgr=sync_mgr,
                        get_connected_ids=get_connected_ids,
                        get_discovered=get_discovered_peers,
                        on_nav_url=on_nav_url,
                        on_forward_file=on_forward_file,
                        upload_dir=upload_dir,
                        dialog_mgr=self._dialog_mgr,
                        get_overview_data=get_overview_data,
                        on_device_action=on_device_action,
                        on_transfer_action=on_transfer_action,
                        on_get_transfers=on_get_transfers,
                        on_speed_test_start=on_speed_test_start,
                        on_speed_test_poll=on_speed_test_poll,
                        on_window_close=on_window_close,
                        on_toggle_discovery=on_toggle_discovery,
                        on_toggle_visibility=on_toggle_visibility,
                    )
                    inner_self.send_response(status)
                    inner_self.send_header("Content-Type", content_type)
                    inner_self.send_header("Cache-Control", "no-cache")
                    inner_self.send_header("Access-Control-Allow-Origin", "*")
                    inner_self.end_headers()
                    try:
                        inner_self.wfile.write(body_bytes)
                    except OSError:
                        pass
                    return

                inner_self._send_json({"error": "not found"}, 404)

        # ── Create and start the HTTP server ──────────────────────
        try:
            self._httpd = ThreadingHTTPServer((host, port), _Handler)
        except OSError as e:
            logger.warning("Web server failed to bind %s:%d: %s", host, port, e)
            return False

        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="web-server")
        self._thread.start()
        logger.info("Web companion listening on http://%s:%d", self._get_lan_ip(), port)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            logger.info("Stopping web companion")
            self._ws_manager.shutdown()
            self._httpd.shutdown()
            self._httpd = None
        self._thread = None

    # ── Network helpers ──────────────────────────────────────────

    @staticmethod
    def _get_lan_ip() -> str:
        """Return the best LAN IP reachable from other devices on the local network.

        Prefers 192.168.x.x over 10.x.x.x over 172.16-31.x.x (VPN range).
        Falls back to the OS-chosen default route if no private IP is found.
        """
        all_ips = WebServer.get_all_ips()
        if not all_ips:
            return "127.0.0.1"
        def _priority(ip):
            if ip.startswith("192.168."):
                return 0
            if ip.startswith("10."):
                return 1
            if ip.startswith("172."):
                try:
                    second = int(ip.split(".")[1])
                    if 16 <= second <= 31:
                        return 2
                except ValueError:
                    pass
            return 3
        all_ips.sort(key=_priority)
        return all_ips[0]

    @staticmethod
    def get_all_ips() -> list[str]:
        ips = []
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET,
                                           socket.SOCK_STREAM, 0, socket.AI_PASSIVE):
                ip = info[4][0]
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
        if not ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(0)
                s.connect(("10.254.254.254", 1))
                default_ip = s.getsockname()[0]
                s.close()
                if default_ip and default_ip not in ips:
                    ips.append(default_ip)
            except Exception:
                if "127.0.0.1" not in ips:
                    ips.append("127.0.0.1")
        return ips
