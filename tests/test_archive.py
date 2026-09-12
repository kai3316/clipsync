"""Folder archiving for the transfers panel's 发送文件夹 button."""

import zipfile
from pathlib import Path

import pytest

from internal.system.archive import ArchiveEmptyError, create_archive


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "Photos"
    (folder / "nested").mkdir(parents=True, exist_ok=True)
    (folder / "a.txt").write_text("a", encoding="utf-8")
    (folder / "nested" / "b.txt").write_text("b", encoding="utf-8")
    return folder


def test_archives_every_file_under_the_folder(tmp_path):
    folder = _folder(tmp_path)
    archive, count = create_archive(folder, dest_dir=tmp_path / "out")
    assert count == 2
    assert archive.is_file() and archive.suffix == ".zip"
    # The folder keeps its place as the archive's top level, so extracting gives
    # back the folder that was picked rather than a loose pile of its files —
    # the legacy panel's `relative_to(p.parent)`.
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == ["Photos/a.txt", "Photos/nested/b.txt"]
        assert bundle.read("Photos/nested/b.txt") == b"b"


def test_names_the_archive_after_the_folder(tmp_path):
    # What the panel showed while it zipped and what the receiver's row reads.
    archive, _ = create_archive(_folder(tmp_path), dest_dir=tmp_path / "out")
    assert archive.name.startswith("Photos")
    archive2, _ = create_archive(_folder(tmp_path), dest_dir=tmp_path / "out")
    # Two sends of the same folder must not fight over one name.
    assert archive != archive2


@pytest.mark.parametrize("shape", ["empty", "only-empty-subfolders"])
def test_a_folder_with_nothing_to_send_is_refused(tmp_path, shape):
    folder = tmp_path / "Empty"
    folder.mkdir()
    if shape == "only-empty-subfolders":
        (folder / "deeper").mkdir()
    with pytest.raises(ArchiveEmptyError):
        create_archive(folder, dest_dir=tmp_path / "out")


def test_a_single_file_keeps_its_own_name(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("x", encoding="utf-8")
    archive, count = create_archive(target, dest_dir=tmp_path / "out")
    # The panel sent a picked file as itself, not inside a folder of its name.
    assert count == 1
    assert archive.name.startswith("notes")
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == ["notes.txt"]


def test_several_picks_become_one_archive_of_themselves(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    folder = tmp_path / "Documents"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    folder.mkdir()
    (folder / "c.txt").write_text("c", encoding="utf-8")
    archive, count = create_archive([first, second, folder], dest_dir=tmp_path / "out")
    # One archive for the whole pick, which is what makes it one transfer; the
    # panel named this shape after the count rather than after any single pick.
    assert count == 3
    assert archive.name.startswith("files-3")
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == ["Documents/c.txt", "a.txt", "b.txt"]


def test_one_pick_is_named_after_itself_even_as_a_list(tmp_path):
    # The picker answers with a list however many it holds, so a one-item list
    # has to read as one pick rather than as "files-1".
    target = tmp_path / "notes.txt"
    target.write_text("x", encoding="utf-8")
    archive, count = create_archive([target], dest_dir=tmp_path / "out")
    assert count == 1
    assert archive.name.startswith("notes")


def test_a_path_that_is_gone_between_the_pick_and_the_send_is_refused(tmp_path):
    missing = tmp_path / "vanished.txt"
    with pytest.raises(FileNotFoundError):
        create_archive([missing], dest_dir=tmp_path / "out")


def test_nothing_picked_is_refused(tmp_path):
    with pytest.raises(ValueError):
        create_archive([], dest_dir=tmp_path / "out")


def test_creates_its_destination_directory(tmp_path):
    destination = tmp_path / "out" / "nested"
    archive, _ = create_archive(_folder(tmp_path), dest_dir=destination)
    assert archive.parent == destination
