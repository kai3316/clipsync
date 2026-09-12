"""Transport-independent favorites validation and bounded DTOs."""

import base64
import contextlib
import math
import os
from datetime import datetime
from pathlib import Path

from internal.application.errors import ApplicationError
from internal.data.favorites_export import build_favorites_export


class FavoritesUseCase:
    def __init__(self, repository, write_clipboard=None, history_lookup=None, export_dir=None):
        self.repository = repository
        self._write_clipboard = write_clipboard
        self._history_lookup = history_lookup
        self._export_dir = export_dir

    @staticmethod
    def _text(value, name, maximum, minimum=0):
        if not isinstance(value, str) or not minimum <= len(value) <= maximum:
            raise ApplicationError("INVALID_ARGUMENT", f"Invalid {name}")
        return value

    @staticmethod
    def _number(value, name, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ApplicationError("INVALID_ARGUMENT", f"Invalid {name}")

    def _id(self, value):
        self._text(value, "favorite id", 64, 1)
        if not value.strip():
            raise ApplicationError("INVALID_ARGUMENT", "Invalid favorite id")

    def _validate(self, entry):
        self._id(entry["id"])
        for name, maximum in (("title", 256), ("content", 65536), ("group", 128)):
            self._text(entry[name], name, maximum)
        self._number(entry["position"], "position", 0, 1000000)
        self._timestamps(entry)
        if not entry["title"].strip() and not entry["content"]:
            raise ApplicationError("INVALID_ARGUMENT", "Title or content is required")

    @staticmethod
    def _timestamps(entry):
        for name in ("created", "updated"):
            value = entry[name]
            if name == "updated" and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ApplicationError("DATA_INVALID", "Stored favorite has invalid timestamps")

    def _full(self, entry):
        if entry is None:
            raise ApplicationError("NOT_FOUND", "Favorite no longer exists")
        try:
            self._validate(entry)
        except (ApplicationError, KeyError, TypeError):
            raise ApplicationError(
                "DATA_INVALID", "Stored favorite exceeds supported limits or requires recovery"
            ) from None
        return entry

    @staticmethod
    def _call(action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except ApplicationError:
            raise
        except Exception:
            raise ApplicationError("STORAGE_ERROR", "Favorites storage is unavailable") from None

    def list(self, query="", group="", offset=0, limit=30):
        self._text(query, "query", 512)
        self._text(group, "group", 128)
        self._number(offset, "offset", 0, 1000000)
        self._number(limit, "limit", 1, 100)
        entries = self._call(self.repository.get_all)
        try:
            groups = sorted({entry["group"] for entry in entries if entry["group"]})
            for name in groups:
                self._text(name, "stored group", 128)
            needle = query.casefold()
            matches = [
                entry
                for entry in entries
                if (not group or entry["group"] == group)
                and (
                    not needle
                    or needle in entry["title"].casefold()
                    or needle in entry["content"].casefold()
                )
            ]
            items = []
            for entry in matches[offset : offset + limit]:
                self._id(entry["id"])
                self._text(entry["title"], "stored title", 256)
                self._number(entry["position"], "stored position", 0, 1000000)
                self._timestamps(entry)
                if not isinstance(entry["content"], str):
                    raise ApplicationError("DATA_INVALID", "Stored favorite has invalid content")
                items.append(
                    {
                        **{
                            key: entry[key]
                            for key in ("id", "title", "group", "position", "created", "updated")
                        },
                        "preview": entry["content"][:256],
                    }
                )
            return {"items": items, "total": len(matches), "offset": offset, "groups": groups}
        except (ApplicationError, KeyError, TypeError, AttributeError):
            raise ApplicationError("DATA_INVALID", "Stored favorites require recovery") from None

    def get(self, favorite_id):
        self._id(favorite_id)
        return {"favorite": self._full(self._call(self.repository.get, favorite_id))}

    def add(self, title, content, group=""):
        for name, value, maximum in (
            ("title", title, 256),
            ("content", content, 65536),
            ("group", group, 128),
        ):
            self._text(value, name, maximum)
        entry = self._call(self.repository.add, title, content, group, validate=self._validate)
        return {"favorite": entry}

    def update(self, favorite_id, title=None, content=None, group=None, position=None):
        self._id(favorite_id)
        changes = {}
        for name, value, maximum in (
            ("title", title, 256),
            ("content", content, 65536),
            ("group", group, 128),
        ):
            if value is not None:
                self._text(value, name, maximum)
                changes[name] = value
        if position is not None:
            self._number(position, "position", 0, 1000000)
            changes["position"] = position
        entry = self._call(
            self.repository.update,
            favorite_id,
            changes,
            validate=self._validate,
            validate_existing=self._full,
        )
        return {"favorite": self._full(entry)}

    def delete(self, favorite_id):
        self._id(favorite_id)
        if not self._call(self.repository.delete, favorite_id):
            raise ApplicationError("NOT_FOUND", "Favorite no longer exists")
        return {"deleted": True}

    def copy(self, favorite_id):
        entry = self.get(favorite_id)["favorite"]
        if self._write_clipboard is None:
            raise ApplicationError("NOT_SUPPORTED", "Clipboard writer is not available")
        try:
            if self._write_clipboard(entry["content"]) is not True:
                raise RuntimeError()
        except Exception:
            raise ApplicationError(
                "CLIPBOARD_WRITE_FAILED", "Could not write clipboard", retryable=True
            ) from None
        return {"copied": True}

    def batch_add(self, entry_ids, group=""):
        """Add several history entries to favorites, preserving selection order.

        Entries are inserted one by one (appending after the current maximum
        position) so a favourite added or edited elsewhere during the batch is
        never rolled back, and the FULL stored text is kept — the history
        preview is truncated at ingest and would silently cut the clip short.
        """
        if self._history_lookup is None:
            raise ApplicationError("NOT_SUPPORTED", "History lookup is not available")
        if (type(entry_ids) is not list or not 1 <= len(entry_ids) <= 100
                or len(set(entry_ids)) != len(entry_ids)
                or any(type(value) is not str or not 0 < len(value) <= 64
                       for value in entry_ids)):
            raise ApplicationError("INVALID_ARGUMENT", "Invalid history entry ids")
        self._text(group, "group", 128)
        added = []
        for entry_id in entry_ids:
            entry = self._call(self._history_lookup, entry_id)
            if not isinstance(entry, dict):
                continue
            types = entry.get("types") or {}
            text_b64 = types.get("TEXT", "") if isinstance(types, dict) else ""
            full_text = ""
            if isinstance(text_b64, str) and text_b64:
                try:
                    full_text = base64.b64decode(text_b64).decode("utf-8", errors="replace")
                except Exception:
                    full_text = ""
            preview = full_text or str(entry.get("text_preview", "") or "")
            content = full_text or str(entry.get("text_preview", "") or "")
            title = preview[:50] if preview else "(empty)"
            created = self._call(
                self.repository.add, title, content, group, validate=self._validate
            )
            added.append(created["id"])
        return {"added": len(added), "ids": added}

    def export(self, format="markdown", dest_dir=None):
        """Write every favourite to a Markdown/text file and report the path."""
        if format not in ("markdown", "text"):
            raise ApplicationError("INVALID_ARGUMENT", "Unsupported export format")
        favorites = self._call(self.repository.get_all)
        suffix = ".md" if format == "markdown" else ".txt"
        downloads = Path.home() / "Downloads"
        fallback = Path(self._export_dir) if self._export_dir else Path.cwd()
        target_dir = Path(dest_dir) if dest_dir else (downloads if downloads.is_dir() else fallback)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            target_dir = fallback
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ApplicationError(
                    "EXPORT_FAILED", "Could not create the export directory", retryable=True
                ) from exc
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest_path = target_dir / f"clipsync-favorites-{stamp}{suffix}"
        try:
            text = build_favorites_export(favorites, format)
            with open(dest_path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            raise ApplicationError(
                "EXPORT_FAILED", "Could not write the export file", retryable=True
            ) from exc
        with contextlib.suppress(OSError):
            os.chmod(dest_path, 0o600)
        return {
            "filepath": str(dest_path),
            "filename": dest_path.name,
            "count": len(favorites),
            "format": format,
        }
