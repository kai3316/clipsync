"""Keep the sidecar suite off the network.

The sidecar starts the shared update service at load, which fires the same
silent periodic release check the legacy app does.  No test may depend on
GitHub being reachable, so the single lookup chokepoint answers "no release"
unless a test patches it itself (a test's own monkeypatch is applied after
this fixture and therefore wins).
"""

import pytest

from internal.i18n import get_locale, set_locale
from internal.system import updater


@pytest.fixture(autouse=True)
def _offline_release_lookup(monkeypatch):
    monkeypatch.setattr(updater, "_fetch_latest_release", lambda timeout=6.0: None)


@pytest.fixture(autouse=True)
def _restore_locale():
    """Undo the sidecar's process-global locale change.

    Loading the application calls ``set_locale(config.language)`` — Chinese by
    default — which otherwise sticks for the rest of the pytest process and
    makes later tests that assume the English default (the legacy tray menu
    tests) fail on ordering alone.
    """
    before = get_locale()
    yield
    set_locale(before)
