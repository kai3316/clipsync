"""Backward-compatible HTTP adapters for the shared favorites store."""

import contextlib
import json
import os
from datetime import datetime
from pathlib import Path

from internal.config.config import _config_dir
from internal.data.favorites_export import build_favorites_export as _build_favorites_export
from internal.infrastructure.persistence.favorites import SCHEMA, FavoritesRepository

_FAV_DB_PATH: str | None = None
_SCHEMA = SCHEMA
_MIGRATIONS = ["ALTER TABLE favorites ADD COLUMN position INTEGER NOT NULL DEFAULT 0"]


def _get_db_path() -> str:
    global _FAV_DB_PATH
    if _FAV_DB_PATH is None:
        _FAV_DB_PATH = os.path.join(_config_dir(), "favorites.db")
    return _FAV_DB_PATH


def _get_json_path() -> str:
    return os.path.join(_config_dir(), "favorites.json")


def _repository():
    return FavoritesRepository(_get_db_path(), _get_json_path())


def _get_conn():
    return _repository().connect()


def _ensure_db() -> None:
    _repository().ensure_db()


def _run_migrations(conn) -> None:
    FavoritesRepository.run_migrations(conn)
    conn.commit()


def _maybe_migrate() -> int:
    return _repository().maybe_migrate()


def _legacy_entry(entry):
    entry = dict(entry)
    if entry.get("updated") is None:
        entry.pop("updated", None)
    return entry


def _load_favorites() -> list:
    return [_legacy_entry(entry) for entry in _repository().get_all()]


def _body(body):
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        raise ValueError("invalid json") from None
    if not isinstance(data, dict):
        raise ValueError("invalid json")
    return data


def _string(data, name):
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def get_favorites():
    try:
        return {"favorites": _load_favorites()}, 200
    except Exception:
        return {"ok": False, "error": "database error"}, 500


def add_favorite(body):
    try:
        data = _body(body)
        title = _string(data, "title").strip()
        content = _string(data, "content")
        group = _string(data, "group").strip()
        if not title and not content:
            raise ValueError("title or content is required")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400
    try:
        entry = _repository().add(title, content, group)
        return {"ok": True, "favorite": _legacy_entry(entry)}, 200
    except Exception:
        return {"ok": False, "error": "database error"}, 500


def delete_favorite(body):
    try:
        data = _body(body)
        favorite_id = _string(data, "id").strip()
        if not favorite_id:
            raise ValueError("id is required")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400
    try:
        if not _repository().delete(favorite_id):
            return {"ok": False, "error": "not found"}, 404
        return {"ok": True}, 200
    except Exception:
        return {"ok": False, "error": "database error"}, 500


def update_favorite(body):
    try:
        data = _body(body)
        favorite_id = _string(data, "id").strip()
        if not favorite_id:
            raise ValueError("id is required")
        changes = {
            name: _string(data, name) for name in ("title", "content", "group") if name in data
        }
        if "position" in data:
            try:
                changes["position"] = int(data["position"])
            except (ValueError, TypeError, OverflowError):
                raise ValueError("position must be an integer") from None
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400
    try:
        entry = _repository().update(favorite_id, changes)
        if entry is None:
            return {"ok": False, "error": "not found"}, 404
        return {"ok": True, "favorite": entry}, 200
    except Exception:
        return {"ok": False, "error": "database error"}, 500


# A batch body is the panel's whole gesture — a drag, a group rename, a group
# delete — so the cap has to be higher than any real favourites list; it exists
# only so a malformed body cannot turn into an unbounded write loop on the
# phone-facing route.
_MAX_BATCH = 10000


def update_favorites(body):
    """PATCH /api/favorites: one favourite's fields, or a batch of them.

    The single-favourite body is legacy's own and goes to ``update_favorite``
    untouched.  ``{"updates": [{"id": ..., <fields>}, ...]}`` is additive and
    exists because of what a write now means: it is published, so a panel
    gesture that touches several favourites at once — a drag reordering every
    moved one, a group rename or a group delete — would otherwise put one
    snapshot per item on the wire behind a single gesture, each of them a
    partially applied change.  One request, one publish, one state: which is
    also one round trip instead of N on a phone.
    """
    try:
        data = _body(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and "updates" in data:
        return _apply_updates(data["updates"])
    return update_favorite(body)


def _changes(item):
    """The fields one batch entry asks for, checked as ``update_favorite`` does.

    ``position`` is the exception: whole numbers only here, rather than the
    legacy body's coerced ``int()``.  This shape is new, so it can refuse a
    ``true`` or a ``1.5`` that would otherwise be stored silently as a position.
    """
    changes = {}
    for name in ("title", "content", "group"):
        if name in item:
            changes[name] = _string(item, name)
    if "position" in item:
        position = item["position"]
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("position must be a whole number")
        changes["position"] = position
    return changes


def _apply_updates(updates):
    """Apply ``[{"id", ...fields}, ...]`` in one call.

    A favourite deleted while the phone was midway through a gesture is simply
    not there to change: the rest still land, as they did when each item was
    its own request.  The call only fails outright when nothing it named
    exists.
    """
    if not isinstance(updates, list) or not updates:
        return {"ok": False, "error": "updates must be a non-empty list"}, 400
    if len(updates) > _MAX_BATCH:
        return {"ok": False, "error": "updates is too long"}, 400
    pairs = []
    seen = set()
    for item in updates:
        if not isinstance(item, dict):
            return {"ok": False, "error": "updates entries must be objects"}, 400
        favorite_id = item.get("id")
        if not isinstance(favorite_id, str) or not favorite_id.strip():
            return {"ok": False, "error": "updates entries need an id"}, 400
        favorite_id = favorite_id.strip()
        if favorite_id in seen:
            return {"ok": False, "error": "updates repeats an id"}, 400
        try:
            changes = _changes(item)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        if not changes:
            return {"ok": False, "error": "updates entries need a field to change"}, 400
        seen.add(favorite_id)
        pairs.append((favorite_id, changes))
    try:
        repository = _repository()
        updated = sum(
            1
            for favorite_id, changes in pairs
            if repository.update(favorite_id, changes) is not None
        )
    except Exception:
        return {"ok": False, "error": "database error"}, 500
    if not updated:
        return {"ok": False, "error": "not found"}, 404
    return {"ok": True, "updated": updated}, 200


def export_favorites(body, dest_dir=None):
    """Export all favorites using the legacy signature and response."""
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid json"}, 400
    if not isinstance(data, dict):
        data = {}
    fmt = str(data.get("format", "markdown")).lower()
    if fmt not in ("markdown", "text"):
        return {"ok": False, "error": "unsupported format (use markdown or text)"}, 400
    try:
        favorites = _load_favorites()
    except Exception:
        return {"ok": False, "error": "database error"}, 500
    suffix = ".md" if fmt == "markdown" else ".txt"
    downloads = Path.home() / "Downloads"
    target_dir = (
        Path(dest_dir) if dest_dir else (downloads if downloads.is_dir() else Path(_config_dir()))
    )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        target_dir = Path(_config_dir())
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_path = str(target_dir / f"clipsync-favorites-{ts}{suffix}")
    try:
        text = _build_favorites_export(favorites, fmt)
    except Exception:
        return {"ok": False, "error": "export rendering failed"}, 500
    try:
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(text)
        with contextlib.suppress(OSError):
            os.chmod(dest_path, 0o600)
    except OSError:
        return {"ok": False, "error": "export write failed"}, 500
    return {
        "ok": True,
        "filepath": dest_path,
        "filename": os.path.basename(dest_path),
        "count": len(favorites),
        "format": fmt,
    }, 200
