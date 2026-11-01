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


def test_a_path_that_is_gone_between_the_pick_and_the_send_is_refused(tmp_path):
    missing = tmp_path / "vanished.txt"
    with pytest.raises(FileNotFoundError):
        create_archive([missing], dest_dir=tmp_path / "out")
    with pytest.raises(ValueError):
        create_archive([], dest_dir=tmp_path / "out")


def test_the_run_reports_the_totals_before_it_starts_and_then_how_far_it_got(tmp_path):
    folder = _folder(tmp_path)
    reports = []
    archive, count = create_archive(
        folder, dest_dir=tmp_path / "out", on_progress=lambda *row: reports.append(row)
    )
    assert count == 2
    # The first report is the one a several-gigabyte folder needs: what is about
    # to happen, before any of it has.  Two files of one byte each, so the
    # totals are checkable by hand.
    assert reports[0] == (0, 2, 0, 2)
    # One report per member after that, and the last one is the whole folder.
    assert reports[-1] == (2, 2, 2, 2)
    assert [done for done, _total, _bytes, _all in reports] == [0, 1, 2]
    # Reported, not applied: the archive is the same one it always was.
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == ["Photos/a.txt", "Photos/nested/b.txt"]


def test_a_run_with_no_callback_still_archives(tmp_path):
    # The progress is a report the caller asks for, not a step of the archiving;
    # the peer-to-peer and clip-file callers pass nothing.
    archive, count = create_archive(_folder(tmp_path), dest_dir=tmp_path / "out")
    assert count == 2 and archive.is_file()


def test_a_member_that_cannot_be_stat_ed_is_skipped_not_counted(tmp_path):
    # totalling the walk must not resurrect the failure mode `is_file()` had
    # already made benign: a broken link is not a file to pack.
    folder = _folder(tmp_path)
    broken = folder / "gone.txt"
    try:
        broken.symlink_to(folder / "nowhere.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create a dangling symlink")
    archive, count = create_archive(folder, dest_dir=tmp_path / "out")
    assert count == 2
    with zipfile.ZipFile(archive) as bundle:
        assert "Photos/gone.txt" not in bundle.namelist()
