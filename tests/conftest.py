"""Keep the test session's file log out of the developer's real log directory.

``setup_file_logging`` writes to ``config._log_dir()`` -- on Windows
``%APPDATA%\\ClipSync``, elsewhere the user's own log/data directory.  Tests that
start the sidecar entrypoint call it incidentally, so without this a plain
``pytest`` run appended to the log the developer reads about their own machine.

The override is session-scoped and only set when the caller has not set one;
tests that want their own directory monkeypatch ``config._log_dir`` as before.
"""

import os
import shutil
import tempfile
from pathlib import Path

_created: Path | None = None
if not os.environ.get("CLIPSYNC_LOG_DIR"):
    _created = Path(tempfile.mkdtemp(prefix="clipsync-test-logs-"))
    os.environ["CLIPSYNC_LOG_DIR"] = str(_created)


def pytest_sessionfinish(session, exitstatus):
    """Drop the session's log scratch directory; nothing else points at it."""
    if _created is not None:
        shutil.rmtree(_created, ignore_errors=True)
