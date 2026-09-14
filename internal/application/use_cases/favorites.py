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
            # Every group the user has, which is the groups their favourites
            # are in plus the ones they made and have not filled yet.
            groups = sorted(
                {entry["group"] for entry in entries if entry["group"]}
                | set(self._call(self.repository.group_names))
            )
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
            # The sidebar's counts are taken over the whole library, not over
            # the search: a count that moved while the reader typed would be
            # answering a question they did not ask.  `groups` are the ones to
            # draw, `group_counts` what to write beside each.
            group_counts = {name: 0 for name in groups}
            for entry in entries:
                if entry["group"]:
                    group_counts[entry["group"]] = group_counts.get(entry["group"], 0) + 1
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
            return {
                "items": items,
                "total": len(matches),
                "offset": offset,
                "groups": groups,
                "group_counts": group_counts,
                "library_total": len(entries),
            }
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
        # Filing a favourite into a group is how a group comes to exist; the
        # registry only has to be told explicitly for one made empty.
        self._register(group)
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
        self._register(group)
        return {"favorite": self._full(entry)}

    def _register(self, group):
        """Put a name in the registry, ignoring an empty one and a repeat."""
        if isinstance(group, str) and group.strip():
            self._call(self.repository.register_group, group)

    def reorder(self, favorite_ids):
        """Put these favourites in the order given, as one call.

        The client sends the ids it moved, in their new order, and nothing
        about positions: where they land is a fact about the whole library and
        the client is looking at a page of it.  The positions the ids already
        hold are sorted and handed back out in the new order, which is what
        makes this safe — the set of positions occupied is unchanged, so no
        favourite outside the list can be pushed past or landed on, whether
        they are numbered consecutively or gapped by earlier deletions.

        A favourite deleted while the drag was in flight is simply not there to
        move; one remaining id is no reorder at all.
        """
        if type(favorite_ids) is not list or not 1 <= len(favorite_ids) <= 100:
            raise ApplicationError("INVALID_ARGUMENT", "Invalid favorites order")
        for favorite_id in favorite_ids:
            self._id(favorite_id)
        if len(set(favorite_ids)) != len(favorite_ids):
            raise ApplicationError("INVALID_ARGUMENT", "Favorites order repeats an id")
        entries = self._call(self.repository.get_all)
        positions = {entry["id"]: entry["position"] for entry in entries}
        present = [favorite_id for favorite_id in favorite_ids if favorite_id in positions]
        if len(present) < 2:
            raise ApplicationError("NOT_FOUND", "No favorites in the order exist")
        slots = sorted(positions[favorite_id] for favorite_id in present)
        moved = 0
        for favorite_id, position in zip(present, slots, strict=True):
            if positions[favorite_id] == position:
                continue
            entry = self._call(
                self.repository.update,
                favorite_id,
                {"position": position},
                validate=self._validate,
                validate_existing=self._full,
            )
            if entry is not None:
                moved += 1
        return {"moved": moved}

    def create_group(self, name):
        """Make a group that has nothing in it yet, so it can be filled."""
        self._text(name, "group", 128)
        if not name.strip():
            raise ApplicationError("INVALID_ARGUMENT", "Invalid group name")
        self._register(name)
        return {"groups": self._all_groups()}

    def rename_group(self, name, new_name):
        """Rename a group, taking everything filed under it along.

        The registry entry moves even when the group is empty, which is the
        whole reason the registry exists.
        """
        self._text(name, "group", 128)
        self._text(new_name, "group", 128)
        new_name = new_name.strip()
        if not name.strip() or not new_name:
            raise ApplicationError("INVALID_ARGUMENT", "Invalid group name")
        if new_name == name:
            return {"renamed": 0, "groups": self._all_groups()}
        moved = self._reassign(name, new_name)
        self._call(self.repository.rename_group, name, new_name)
        return {"renamed": moved, "groups": self._all_groups()}

    def delete_group(self, name):
        """Delete a group.  Its favourites are kept and left unfiled."""
        self._text(name, "group", 128)
        if not name.strip():
            raise ApplicationError("INVALID_ARGUMENT", "Invalid group name")
        moved = self._reassign(name, "")
        self._call(self.repository.forget_group, name)
        return {"moved": moved, "groups": self._all_groups()}

    def _reassign(self, name, new_name):
        """Move every favourite filed under ``name`` to ``new_name``."""
        moved = 0
        for entry in self._call(self.repository.get_all):
            if entry["group"] != name:
                continue
            updated = self._call(
                self.repository.update,
                entry["id"],
                {"group": new_name},
                validate=self._validate,
                validate_existing=self._full,
            )
            if updated is not None:
                moved += 1
        if new_name:
            self._register(new_name)
        return moved

    def _all_groups(self):
        """The group list as `list` reports it, for a caller that changed one."""
        return self.list(limit=1)["groups"]

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
