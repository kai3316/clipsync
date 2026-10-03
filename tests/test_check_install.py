"""The install check's diagnosis, on the states it was written for.

The states below are transcribed from a real machine, not invented.  What makes them
worth a test is that each one is *plausible*: a registry entry with a version, a
sidecar of the right size, WebView data from a window that has run -- every one of
those is evidence that the application is fine, and the application was not there.

`diagnose` is a function of what was found rather than of the machine, so these run
anywhere, including a CI runner where ClipSync is not installed at all.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_install import diagnose  # noqa: E402


def a_state(**overrides):
    """A healthy install, so each test can spoil exactly one thing."""
    state = {
        "install_dir": r"C:\Users\someone\AppData\Local\ClipSync",
        "install_dir_recorded": r"C:\Users\someone\AppData\Local\ClipSync",
        "install_dir_present": True,
        "main_binary": r"C:\Users\someone\AppData\Local\ClipSync\clipsync-desktop.exe",
        "main_present": True,
        "main_size": 17_765_376,
        "sidecar_present": True,
        "sidecar_size": 4_744_639,
        "uninstall_entry": True,
        "installed_version": "1.0.42",
        "webview_data": "",
        "user_data_dir": r"C:\Users\someone\AppData\Roaming\ClipSync",
        "user_data_present": True,
        "running": [],
    }
    state.update(overrides)
    return state


class TestAHealthyInstallIsNotFlagged:
    def test_nothing_is_reported(self):
        assert diagnose(a_state()) == []

    def test_user_data_alone_is_not_a_problem(self):
        """A machine where the app was never installed still has config, if a
        portable build ever ran.  That is not a broken install."""
        assert diagnose(a_state(main_present=False, install_dir_present=False,
                                sidecar_present=False, uninstall_entry=False)) != []
        # ...but it is reported as "no application", not as a half-removed one.
        problems = diagnose(a_state(main_present=False, install_dir_present=False,
                                    sidecar_present=False, uninstall_entry=False))
        assert not any("half-removed" in p for p in problems)


class TestTheFailureThisWasWrittenFor:
    def test_sidecar_without_the_window_is_named(self):
        """The state on the machine: 51 MB of Python runtime, no window.

        Nothing else complains.  The registry has a path and a version, the sidecar
        answers, and the installer exits 0 -- which is why the diagnosis has to be
        about the main binary specifically.
        """
        problems = diagnose(a_state(main_present=False))
        assert any("application binary is missing" in p for p in problems)
        assert any("sidecar is installed and the window is not" in p for p in problems)
        # And it says why the installer's exit code was no help.
        assert any("exits 0" in p for p in problems)

    def test_a_registry_entry_without_a_directory_is_named(self):
        problems = diagnose(a_state(main_present=False, install_dir_present=False))
        assert any("half-removed install" in p for p in problems)
        assert any("error opening file for writing" in p for p in problems)

    def test_webview_data_without_a_window_is_named(self):
        problems = diagnose(
            a_state(
                main_present=False,
                webview_data=r"C:\Users\someone\AppData\Local\com.clipsync.desktop",
            )
        )
        assert any("WebView data exists" in p for p in problems)

    def test_a_healthy_install_with_webview_data_is_fine(self):
        """The window has run, so its WebView data exists and should: not a problem."""
        problems = diagnose(
            a_state(webview_data=r"C:\Users\someone\AppData\Local\com.clipsync.desktop")
        )
        assert problems == []
