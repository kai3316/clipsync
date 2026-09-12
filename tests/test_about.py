"""The closed link table behind the native About dialog."""

from internal.system import about


def test_links_are_the_apps_own_https_addresses():
    assert about.LINKS == {
        "homepage": "https://github.com/kai3316/clipsync",
        "releases": "https://github.com/kai3316/clipsync/releases/latest",
    }


def test_open_link_opens_only_the_named_target(monkeypatch):
    opened = []
    monkeypatch.setattr(about.webbrowser, "open", opened.append)
    assert about.open_link("releases") == (True, about.RELEASES_URL)
    assert opened == [about.RELEASES_URL]


def test_open_link_refuses_unknown_targets_and_raw_urls(monkeypatch):
    opened = []
    monkeypatch.setattr(about.webbrowser, "open", opened.append)
    for target in ["", "home", "https://evil.example.com", "../homepage", None, 5]:
        assert about.open_link(target) == (False, "UNKNOWN_TARGET")
    assert opened == []


def test_open_link_reports_a_browser_failure(monkeypatch):
    def boom(_url):
        raise OSError("no browser")

    monkeypatch.setattr(about.webbrowser, "open", boom)
    assert about.open_link("homepage") == (False, "OPEN_FAILED")


def test_open_link_refuses_a_non_https_table_entry(monkeypatch):
    opened = []
    monkeypatch.setattr(about.webbrowser, "open", opened.append)
    monkeypatch.setitem(about.LINKS, "homepage", "file:///etc/passwd")
    assert about.open_link("homepage") == (False, "OPEN_FAILED")
    assert opened == []
