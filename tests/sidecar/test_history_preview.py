"""The hover card over an image row and a file row.

The card was built for the text a row had to clamp, and it refused the two kinds
that have no words — an image's "preview" is ``[Image]`` and a file's is a name,
both of which the row already shows.  So the card stayed shut on exactly the
rows a glance is worth most for, and these tests cover what now opens on them,
and — as much — what does not travel with it.
"""

import base64
import os
from io import BytesIO
from unittest.mock import Mock

from PIL import Image

from internal.application.use_cases.history import (
    PREVIEW_FILES,
    HistoryUseCase,
    picture_of,
)
from internal.clipboard import file_ref


def stored(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def png(width: int, height: int, colour=(200, 30, 30)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def use_case(types):
    repository = Mock()
    repository.find_by_id.return_value = (0, {"entry_id": "0", "types": types})
    return HistoryUseCase(repository)


def card_for(types):
    return use_case(types).preview("0")


def test_an_image_row_answers_with_a_picture_and_its_own_size():
    card = card_for({"IMAGE_PNG": stored(png(1200, 800))})
    assert card["kind"] == "image"
    # The picture's size, not the card's: a card captions a 1200 px screenshot
    # as one after it has downscaled it.
    assert (card["width"], card["height"]) == (1200, 800)
    assert card["image"].startswith("data:image/")


def test_the_picture_travels_downscaled_rather_than_whole():
    """The point of the read: a hover must not ship the entry itself.

    A flat 2000×2000 bitmap is a few kilobytes as PNG, so the assertion is on
    the *pixels* — what came back must fit a card — rather than on the byte
    count, which the format is already choosing.
    """
    card = card_for({"IMAGE_PNG": stored(png(2000, 2000))})
    raw = base64.b64decode(card["image"].split(",", 1)[1])
    with Image.open(BytesIO(raw)) as picture:
        assert max(picture.size) <= 480
    # And the original's size still comes back beside it.
    assert (card["width"], card["height"]) == (2000, 2000)


def test_a_bitmap_this_build_cannot_decode_opens_no_card():
    """Nothing is claimed about a picture that cannot be rendered."""
    assert card_for({"IMAGE_PNG": stored(b"not a picture at all")})["kind"] == ""


def test_a_bitmap_past_the_budget_is_not_decoded_at_all(monkeypatch):
    """A large image answers empty rather than being read on a hover.

    The check is on the stored base64, so the decode never happens — which is
    the whole point: this is the one member of the stored bag that can be tens
    of megabytes, and a pointer crossing a row cannot pay for it.
    """
    decoded = []
    real = base64.b64decode

    def watch(*args, **kwargs):
        decoded.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(base64, "b64decode", watch)
    oversized = {"IMAGE_PNG": stored(png(64, 64)) + "A" * (48 * 1024 * 1024)}
    assert card_for(oversized)["kind"] == ""
    assert decoded == []


def test_a_file_row_answers_with_what_the_files_are(tmp_path):
    kept = tmp_path / "报告.txt"
    kept.write_bytes(b"x" * 2048)
    gone = tmp_path / "已移走.txt"
    card = card_for({"FILE": stored(f"{kept}\n{gone}\n".encode())})
    assert card["kind"] == "files"
    assert card["total"] == 2
    assert card["files"][0]["name"] == "报告.txt"
    assert card["files"][0]["size"] == 2048
    # The one thing the row cannot say and the card can: this one is not there
    # any more, which is what a user asking "why did this fail" is after.
    assert card["files"][0]["exists"] is True
    assert card["files"][1]["exists"] is False


def test_a_file_clip_carries_no_paths(tmp_path):
    """A history DTO ships no source paths, and the card is a history DTO.

    Everything a reader would do with a path is already a row action (在文件夹
    中显示, 下载), and the card is read rather than acted on.
    """
    kept = tmp_path / "secret-project-name.txt"
    kept.write_bytes(b"x")
    card = card_for({"FILE": stored(f"{kept}\n".encode())})
    assert str(tmp_path) not in repr(card)


def test_one_picture_in_a_file_clip_gets_a_picture(tmp_path):
    """A picture copied in a file manager is a *file* clip, and the glance at
    it is the same glance."""
    picture = tmp_path / "photo.png"
    picture.write_bytes(png(900, 700))
    card = card_for({"FILE": stored(f"{picture}\n".encode())})
    assert card["kind"] == "files"
    assert card["image"].startswith("data:image/")
    assert (card["width"], card["height"]) == (900, 700)


def test_a_file_that_is_not_a_picture_is_not_read(tmp_path):
    """Only a picture is opened; everything else is stat'd, which is the
    difference between a hover and a read of whatever the user last copied."""
    document = tmp_path / "notes.txt"
    document.write_bytes(b"x" * 4096)
    card = card_for({"FILE": stored(f"{document}\n".encode())})
    assert card["image"] == ""
    assert card["files"][0]["size"] == 4096


def test_a_clip_of_many_files_lists_what_fits_and_counts_the_rest(tmp_path):
    paths = []
    for index in range(PREVIEW_FILES + 3):
        path = tmp_path / f"file-{index}.bin"
        path.write_bytes(b"x")
        paths.append(str(path))
    card = card_for({"FILE": stored("\n".join(paths).encode())})
    assert card["total"] == len(paths)
    assert len(card["files"]) == PREVIEW_FILES
    # A drop of many is not thumbnailed from its first member: the read would
    # be unbounded work for a clip whose card is a list anyway.
    assert card["image"] == ""


def test_a_remote_file_row_lists_what_the_sender_described():
    """The file lives on another device, so this machine holds a description
    and nothing else — no path to stat, no bytes to thumbnail."""
    payload = file_ref.offer(
        "7",
        [
            {"name": "报告.pdf", "size": 3 * 1024 * 1024, "kind": "file"},
            {"name": "照片", "size": 0, "kind": "dir"},
        ],
    )
    card = card_for({"FILE_REMOTE": stored(payload)})
    assert card["kind"] == "files"
    assert card["total"] == 2
    assert [(f["name"], f["kind"]) for f in card["files"]] == [
        ("报告.pdf", "file"),
        ("照片", "dir"),
    ]
    # Never here, so never reported as missing: a card that said so would be
    # wrong about every remote row there is.
    assert all(file["exists"] for file in card["files"])


def test_a_text_row_opens_no_card():
    """The row's own preview is the whole of what there is to show."""
    assert card_for({"TEXT": stored(b"hello")})["kind"] == ""


def test_a_vector_image_opens_no_card():
    """``IMAGE_EMF`` is a picture this build cannot render, and it is stored as
    bytes that are not a bitmap; the card says nothing rather than holding a
    placeholder for one."""
    assert card_for({"IMAGE_EMF": stored(b"\x01\x00\x00\x00")})["kind"] == ""


def test_a_row_that_has_gone_answers_empty_rather_than_raising():
    """A hover is not a request the user made.

    Every other read here turns a row it cannot make sense of into an error,
    because every other read is answering something the user asked for.  A card
    follows the pointer, so a row deleted between the list and the hover — or
    one whose payload needs recovery — has to come back empty instead of
    raising into a window that never asked a question.
    """
    repository = Mock()
    repository.find_by_id.return_value = (0, None)
    assert HistoryUseCase(repository).preview("gone")["kind"] == ""

    repository.find_by_id.return_value = (0, {"entry_id": "0", "types": {"TEXT": "!!!"}})
    assert HistoryUseCase(repository).preview("0")["kind"] == ""


def test_a_picture_encodes_to_png_until_it_would_not_fit():
    """PNG is what a screenshot wants and JPEG is what a photograph needs, and
    the card picks between them on size rather than on the file's own kind."""
    from PIL import Image as PilImage

    flat = PilImage.new("RGB", (400, 400), (10, 20, 30))
    url, width, height = picture_of(_as_png(flat))
    assert url.startswith("data:image/png;base64,")
    assert (width, height) == (400, 400)

    # Noise is the case PNG cannot compress, so it is the case that falls back.
    noisy = PilImage.frombytes("RGB", (400, 400), os.urandom(400 * 400 * 3))
    url, _, _ = picture_of(_as_png(noisy))
    assert url.startswith("data:image/jpeg;base64,")


def _as_png(picture) -> bytes:
    out = BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()
