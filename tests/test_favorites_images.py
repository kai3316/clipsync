"""A favourited picture keeps its picture, not only the word "Image"."""

from __future__ import annotations

from internal.application.use_cases.favorites import FavoritesUseCase
from internal.infrastructure.persistence.favorites import FavoritesRepository

# 1x1 transparent PNG: enough for the downscaler to prove it kept a picture.
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_favouriting_an_image_stores_its_thumbnail(tmp_path):
    repository = FavoritesRepository(tmp_path / "favorites.db")
    history = {
        "img-1": {
            "types": {"IMAGE_PNG": PNG},
            "text_preview": "[Image]",
        }
    }
    service = FavoritesUseCase(repository, history_lookup=lambda entry_id: history.get(entry_id))
    result = service.batch_add(["img-1"])
    assert result["added"] == 1

    item = service.list()["items"][0]
    assert item["image"].startswith("data:image/")
    assert item["preview"] == "[Image]"


def test_an_old_database_gets_the_image_column_on_open(tmp_path):
    """The favourite table predates thumbnails; opening it must not lose rows."""
    path = tmp_path / "favorites.db"
    import sqlite3

    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE favorites (id TEXT PRIMARY KEY, title TEXT, content TEXT, "
            '"group" TEXT, created REAL, updated REAL)'
        )
        conn.execute("INSERT INTO favorites VALUES ('a', 't', 'body', 'g', 1.0, 2.0)")

    repository = FavoritesRepository(path)
    repository.ensure_db()
    service = FavoritesUseCase(repository)
    item = service.list()["items"][0]
    assert item["title"] == "t"
    assert item["image"] == ""
