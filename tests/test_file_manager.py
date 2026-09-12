"""Shared open/reveal helper: platform commands and failure codes."""

import pytest

from internal.system import file_manager


@pytest.mark.parametrize("system,command", [
    ("win32", "startfile"),
    ("darwin", "open"),
    ("linux", "xdg-open"),
])
def test_open_file_uses_the_platform_command(system, command, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(file_manager.sys, "platform", system)
    monkeypatch.setattr(file_manager.os, "startfile", lambda p: calls.append([p]), raising=False)
    monkeypatch.setattr(
        file_manager.subprocess, "run", lambda args, check=False: calls.append(args)
    )
    target = tmp_path / "archive.zip"
    target.write_bytes(b"payload")
    expected = [str(target)] if command == "startfile" else [command, str(target)]
    assert file_manager.open_file(str(target)) == (True, str(target))
    assert calls == [expected]


@pytest.mark.parametrize("value", ["", "no/such/path.zip"])
def test_open_file_reports_a_missing_path(value, monkeypatch):
    calls = []
    monkeypatch.setattr(file_manager.os, "startfile", lambda p: calls.append([p]), raising=False)
    assert file_manager.open_file(value) == (False, file_manager.FILE_NOT_FOUND)
    assert calls == []


def test_open_file_reports_a_failed_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(file_manager.sys, "platform", "win32")

    def boom(path):
        raise OSError("no default app")

    monkeypatch.setattr(file_manager.os, "startfile", boom, raising=False)
    target = tmp_path / "archive.zip"
    target.write_bytes(b"payload")
    assert file_manager.open_file(str(target)) == (False, file_manager.OPEN_FAILED)


@pytest.mark.parametrize("system,command", [
    ("win32", "explorer"),
    ("darwin", "open"),
    ("linux", "xdg-open"),
])
def test_reveal_folder_uses_the_platform_command(
    system, command, monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(file_manager.sys, "platform", system)
    monkeypatch.setattr(file_manager.subprocess, "Popen", lambda args: calls.append(args))
    monkeypatch.setattr(
        file_manager.subprocess, "run", lambda args, check=False: calls.append(args)
    )
    target = tmp_path / "archive.zip"
    target.write_bytes(b"payload")
    assert file_manager.reveal_folder(str(target)) == (True, str(tmp_path))
    assert calls == [[command, str(tmp_path)]]


def test_reveal_folder_accepts_a_directory(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(file_manager.sys, "platform", "linux")
    monkeypatch.setattr(
        file_manager.subprocess, "run", lambda args, check=False: calls.append(args)
    )
    assert file_manager.reveal_folder(str(tmp_path)) == (True, str(tmp_path))
    assert calls == [["xdg-open", str(tmp_path)]]


@pytest.mark.parametrize("value", ["", "no/such/path.zip"])
def test_reveal_folder_reports_a_missing_path(value, monkeypatch):
    calls = []
    monkeypatch.setattr(
        file_manager.subprocess, "Popen", lambda args: calls.append(args)
    )
    assert file_manager.reveal_folder(value) == (False, file_manager.FILE_NOT_FOUND)
    assert calls == []


def test_reveal_folder_reports_a_failed_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(file_manager.sys, "platform", "win32")

    def boom(args):
        raise OSError("no file manager")

    monkeypatch.setattr(file_manager.subprocess, "Popen", boom)
    target = tmp_path / "archive.zip"
    target.write_bytes(b"payload")
    assert file_manager.reveal_folder(str(target)) == (False, file_manager.OPEN_FAILED)
