"""The packaging guard, on the mistake it was written for.

A sidecar built on one day and packaged on another is not a cosmetic problem, and the
measurement is the proof: a 1.0.45 host displayed "版本 1.0.33", because the window's
version comes from the *sidecar's* status and the sidecar beside it had been built two
weeks earlier.  Nothing in the build complained, and the installer's version said 1.0.45.

`newer_sources` is a function of a path and mtimes, so these run anywhere and need neither
PyInstaller nor a built sidecar.

The file is loaded by path rather than imported: its name has a hyphen, so it is not an
importable module name, and `importlib` is what the check itself has to do too.
"""

import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load():
    spec = importlib.util.spec_from_file_location(
        "make_package_config", ROOT / "scripts" / "make-package-config.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def a_tree(tmp_path, *, binary_age, source_age):
    """A root with a sidecar and one source file, aged as asked."""
    for tree in ("internal", "src"):
        (tmp_path / tree).mkdir(parents=True, exist_ok=True)
    source = tmp_path / "internal" / "version.py"
    source.write_text('__version__ = "1.0.45"\n', encoding="utf-8")
    binary = tmp_path / "sidecar" / "clipsync-sidecar.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"stub")

    now = time.time()
    os.utime(binary, (now - binary_age, now - binary_age))
    os.utime(source, (now - source_age, now - source_age))
    return binary, source


def point_at(module, tmp_path):
    """Aim the module at the temporary tree instead of the repository."""
    module.ROOT = tmp_path
    module.SOURCE_TREES = ("internal", "src")


def test_a_sidecar_older_than_its_sources_is_reported(tmp_path):
    """The case that shipped: sources changed after the binary was built."""
    module = load()
    binary, source = a_tree(tmp_path, binary_age=3600, source_age=60)
    point_at(module, tmp_path)

    assert module.newer_sources(binary) == [source], "the newer source should be reported"


def test_a_sidecar_newer_than_its_sources_passes(tmp_path):
    """The healthy case, so the check cannot pass by always reporting something."""
    module = load()
    binary, _ = a_tree(tmp_path, binary_age=60, source_age=3600)
    point_at(module, tmp_path)

    assert module.newer_sources(binary) == []


def test_a_missing_sidecar_is_not_reported_as_stale(tmp_path):
    """Nothing staged is a different problem, and its message belongs elsewhere."""
    module = load()
    binary, _ = a_tree(tmp_path, binary_age=60, source_age=3600)
    binary.unlink()
    point_at(module, tmp_path)

    assert module.newer_sources(binary) == []
