"""A peer-sent update installer is stored once, not once per copy.

`file_transfer._reserve_dest_name` appends a counter when a name is taken, so a second copy of one
installer landed as `ClipSync_1.0.66_x64-setup (1).exe`.  That name matches none of the release's
asset patterns -- the exe pattern cannot match a ` (1)` counter -- so the cache sweep in
`updater.cache_asset` skips it for good.

The prune is scoped to names this application publishes and to the same version, because "the same
build" is not licence to delete whatever else the reader has in their receive folder.
"""

from __future__ import annotations

import os

import pytest

from internal.sync.file_transfer import FileTransferManager


@pytest.fixture
def manager(tmp_path):
    return FileTransferManager(device_id="a" * 16, output_dir=str(tmp_path / "recv"))


def touch(path, size=16):
    path.write_bytes(b"x" * size)
    return str(path)


def test_a_duplicate_installer_is_pruned_to_one(manager, tmp_path):
    """The report: two copies of one build, the second under a name nothing can sweep."""
    folder = tmp_path / "recv"
    folder.mkdir(parents=True, exist_ok=True)

    keep = touch(folder / "ClipSync_1.0.66_x64-setup.exe")
    first = touch(folder / "ClipSync_1.0.66_x64-setup (1).exe")
    second = touch(folder / "ClipSync_1.0.66_x64-setup (2).exe")

    manager._prune_older_update_blobs(keep)

    remaining = sorted(os.listdir(folder))
    assert remaining == ["ClipSync_1.0.66_x64-setup.exe"], remaining
    assert not os.path.exists(first) and not os.path.exists(second)


def test_a_different_build_is_left_alone(manager, tmp_path):
    """Keeping the newest is not licence to delete another version the reader may still want."""
    folder = tmp_path / "recv"
    folder.mkdir(parents=True, exist_ok=True)

    keep = touch(folder / "ClipSync_1.0.66_x64-setup.exe")
    older = touch(folder / "ClipSync_1.0.59_x64-setup.exe")

    manager._prune_older_update_blobs(keep)

    assert os.path.exists(older), "a different version is not this function's to remove"
    assert sorted(os.listdir(folder)) == [
        "ClipSync_1.0.59_x64-setup.exe",
        "ClipSync_1.0.66_x64-setup.exe",
    ]


def test_unrelated_files_are_untouched(manager, tmp_path):
    """The scope that matters: a receive folder holds the reader's own files too.

    A version-like name in a file this application did not publish is not an installer, and the
    first version of this prune would have removed it.
    """
    folder = tmp_path / "recv"
    folder.mkdir(parents=True, exist_ok=True)

    keep = touch(folder / "ClipSync_1.0.66_x64-setup.exe")
    mine = touch(folder / "notes.txt")
    theirs = touch(folder / "MyApp_1.0.66_x64-setup.exe")
    photo = touch(folder / "ClipSync holiday 1.0.66.jpg")

    manager._prune_older_update_blobs(keep)

    for survivor in (mine, theirs, photo):
        assert os.path.exists(survivor), (
            f"{os.path.basename(survivor)} was not this function's to remove"
        )


def test_a_name_with_no_version_prunes_nothing(manager, tmp_path):
    """No version means no "same build" to compare, so the safe answer is to do nothing."""

    folder = tmp_path / "recv"
    folder.mkdir(parents=True, exist_ok=True)

    keep = touch(folder / "ClipSync-installer.exe")
    other = touch(folder / "ClipSync-installer (1).exe")

    manager._prune_older_update_blobs(keep)

    assert os.path.exists(other)
