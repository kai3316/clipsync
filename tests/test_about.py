"""The closed link table behind the native About dialog, held to its refusals:
a raw string is never opened, and the table only ever yields an https address."""

from internal.system import about


def test_open_link_refuses_unknown_targets_and_raw_urls(monkeypatch):
    opened = []
    monkeypatch.setattr(about.webbrowser, "open", opened.append)
    for target in ["", "home", "https://evil.example.com", "../homepage", None, 5]:
        assert about.open_link(target) == (False, "UNKNOWN_TARGET")
    assert opened == []


def test_open_link_refuses_a_non_https_table_entry(monkeypatch):
    opened = []
    monkeypatch.setattr(about.webbrowser, "open", opened.append)
    monkeypatch.setitem(about.LINKS, "homepage", "file:///etc/passwd")
    assert about.open_link("homepage") == (False, "OPEN_FAILED")
    assert opened == []
