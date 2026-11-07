"""The macOS permission repair actions open the pane the user must toggle.

The same four names appear in three layers: the window's TS union, the native
host's validator and this sidecar's repair list.  They used to disagree -- the
two macOS panes were accepted here but refused by the host, so the buttons in
the diagnostics card did nothing.  The last test reads the other two sources
so the next action added to one side fails a runnable test rather than a
click.
"""

import re
from pathlib import Path
from types import SimpleNamespace

from internal.diagnostics import actions

ROOT = Path(__file__).resolve().parents[1]


def _quoted_actions(text: str, start: str, end: str) -> set[str]:
    block = text.split(start, 1)[1].split(end, 1)[0]
    return set(re.findall(r'"([a-z_]+)"', block))


def test_macos_accessibility_and_full_disk_actions_open_their_panes(monkeypatch):
    opened = []
    monkeypatch.setattr(actions.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(actions, "macos_open_settings", lambda uri: opened.append(uri) or True)
    cfg = SimpleNamespace(port=19990, web_port=19991)

    assert actions.request("accessibility", cfg) == {"ok": True}
    assert opened[-1] == actions.ACCESSIBILITY_PANE
    assert actions.request("full_disk_access", cfg) == {"ok": True}
    assert opened[-1] == actions.FULL_DISK_ACCESS_PANE


def test_macos_permission_actions_are_unsupported_elsewhere(monkeypatch):
    monkeypatch.setattr(actions.platform, "system", lambda: "Linux")
    cfg = SimpleNamespace(port=19990, web_port=19991)

    assert actions.request("accessibility", cfg)["ok"] is False
    assert actions.request("full_disk_access", cfg)["ok"] is False


def test_every_repair_action_is_known_to_the_window_and_the_native_host():
    expected = set(actions.ACTIONS)
    host = (ROOT / "desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")
    window = (ROOT / "desktop/src/api/types.ts").read_text(encoding="utf-8")

    assert _quoted_actions(host, "const DIAGNOSTIC_ACTIONS", "];") == expected
    assert _quoted_actions(window, "export type DiagnosticAction =", ";") == expected

