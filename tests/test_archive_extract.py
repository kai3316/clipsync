"""The unpacking half of pasting a folder from another device.

The sender still ships one zip; the receiver is the side that has to survive it.
These pin the happy path and the three refusals that keep a hostile archive from
writing outside the folder it claims to be.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from internal.system.archive import create_archive, extract_folder_archive


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "Documents"
    (folder / "nested").mkdir(parents=True)
    (folder / "readme.txt").write_text("hello", encoding="utf-8")
    (folder / "nested" / "data.bin").write_bytes(b"\x00\x01")
    return folder


def test_a_folder_archive_unpacks_to_one_folder(tmp_path):
    source = _folder(tmp_path)
    archive, _ = create_archive(source, dest_dir=tmp_path / "archives")
    target = extract_folder_archive(archive, tmp_path / "received", "Documents")

    assert target == tmp_path / "received" / "Documents"
    assert (target / "readme.txt").read_text(encoding="utf-8") == "hello"
    assert (target / "nested" / "data.bin").read_bytes() == b"\x00\x01"
    assert archive.exists(), "the caller decides when the transport archive goes"


def test_a_name_collision_becomes_a_numbered_folder(tmp_path):
    source = _folder(tmp_path)
    archive, _ = create_archive(source, dest_dir=tmp_path / "archives")
    received = tmp_path / "received"
    (received / "Documents").mkdir(parents=True)

    target = extract_folder_archive(archive, received, "Documents")

    assert target == received / "Documents (1)"
    assert (target / "readme.txt").exists()


def test_an_archive_that_would_escape_is_refused(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "no")

    with pytest.raises(ValueError):
        extract_folder_archive(archive, tmp_path / "received", "evil")
    assert not (tmp_path / "outside.txt").exists()


def test_an_archive_with_two_roots_is_refused(tmp_path):
    archive = tmp_path / "two.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("one/a.txt", "a")
        bundle.writestr("two/b.txt", "b")

    with pytest.raises(ValueError):
        extract_folder_archive(archive, tmp_path / "received", "two")


def test_an_archive_with_a_symbolic_link_is_refused(tmp_path):
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        info = zipfile.ZipInfo("link/target")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        bundle.writestr(info, "/etc/passwd")

    with pytest.raises(ValueError):
        extract_folder_archive(archive, tmp_path / "received", "link")
