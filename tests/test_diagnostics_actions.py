"""The macOS permission repair actions open the pane the user must toggle."""

from types import SimpleNamespace

from internal.diagnostics import actions


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
