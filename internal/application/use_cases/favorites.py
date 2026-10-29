"""Transport-independent favorites validation and bounded DTOs."""

import base64
import contextlib
import json
import logging
import math
import os
from pathlib import Path

from internal.application.errors import ApplicationError
from internal.application.use_cases.search import matcher as query_matcher
from internal.clipboard.format import decode_text
from internal.data.favorites_export import (
    FAVORITES_JSON_VERSION,
    build_favorites_export,
    build_favorites_json,
)

logger = logging.getLogger(__name__)

#: The largest favourites document an import will read, and the most rows it
#: will take from one.  Both are guards against pointing the importer at the
#: wrong file — a backup zip, a history export, an installer — rather than
#: limits anyone is expected to reach: 2000 favourites of 64 KiB each is 128 MiB
#: of text, which the 8 MiB ceiling refuses long before the row count does.
IMPORT_MAX_BYTES = 8 * 1024 * 1024
IMPORT_MAX_ITEMS = 2000


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
        for name, maximum in (("title", 256), ("content", 65536), ("image", 65536), ("group", 128)):
            self._text(entry.get(name, ""), name, maximum)
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
            # Terms must all be found, and may land in either field: a favourite
            # is remembered by what it says *and* the group it was filed under.
            hit = query_matcher(query)
            matches = [
                entry
                for entry in entries
                if (not group or entry["group"] == group)
                and hit(entry["title"], entry["content"])
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
                        "image": entry.get("image", ""),
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

    def add(self, title, content, group="", force=False):
        """Add a favourite, refusing to do it twice by accident.

        A body already in the library is answered with it rather than stored
        again — but answered, not merged: the same snippet under two titles is
        something readers do deliberately, so the caller gets the row it matched
        and decides.  ``force`` is that decision, and the row is written on the
        second call.  A favourite that is only a title has no body to compare,
        so it is never a duplicate.
        """
        for name, value, maximum in (
            ("title", title, 256),
            ("content", content, 65536),
            ("group", group, 128),
        ):
            self._text(value, name, maximum)
        if not force and content.strip():
            existing = self._call(self.repository.find_by_content, content)
            if existing is not None:
                return {
                    "favorite": None,
                    "duplicate": {
                        "id": existing["id"],
                        "title": existing["title"],
                        "group": existing["group"],
                    },
                }
        entry = self._call(self.repository.add, title, content, group, validate=self._validate)
        # Filing a favourite into a group is how a group comes to exist; the
        # registry only has to be told explicitly for one made empty.
        self._register(group)
        return {"favorite": entry, "duplicate": None}

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
        updates = [
            (favorite_id, {"position": position})
            for favorite_id, position in zip(present, slots, strict=True)
            if positions[favorite_id] != position
        ]
        # One transaction: a half-applied order is a library the reader cannot
        # explain, and the answer ("moved: n") would be describing a state that
        # is not the one on disk.
        moved = self._call(
            self.repository.batch,
            updates,
            validate=self._validate,
            validate_existing=self._full,
        )["updated"]
        return {"moved": sum(1 for entry in moved if entry is not None)}

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
        return {"renamed": moved, "groups": self._all_groups()}

    def delete_group(self, name, purge=False):
        """Delete a group.

        Its favourites are kept and left unfiled by default — the legacy
        panel's rule, and the one the dialog promises.  ``purge`` is the reader
        answering "and the things in it too": the entries go first, then the
        group, because the other order would leave the entries looking like
        they belonged to a group that no longer exists (and ``_reassign`` would
        have to move each of them to the empty group first, publishing a state
        nobody asked to see).

        Both are one transaction: deleting a group of thirty rows and failing
        on the tenth left ten rows unfiled and twenty in a group that no longer
        existed — a state no dialog describes and no undo restores.
        """
        self._text(name, "group", 128)
        if not name.strip():
            raise ApplicationError("INVALID_ARGUMENT", "Invalid group name")
        members = [
            entry["id"]
            for entry in self._call(self.repository.get_all)
            if entry["group"] == name
        ]
        if purge:
            removed = self._call(
                self.repository.batch, (), members, forget=(name,)
            )["deleted"]
            # The ids, not only the count: a host with that favourite open in
            # its editor has to know whether the row it is showing still
            # exists, and a count cannot answer that.
            return {
                "moved": 0,
                "removed": len(removed),
                "removed_ids": removed,
                "groups": self._all_groups(),
            }
        updates = [(favorite_id, {"group": ""}) for favorite_id in members]
        moved = self._call(
            self.repository.batch,
            updates,
            forget=(name,),
            validate=self._validate,
            validate_existing=self._full,
        )["updated"]
        return {
            "moved": sum(1 for entry in moved if entry is not None),
            "removed": 0,
            "removed_ids": [],
            "groups": self._all_groups(),
        }

    def _reassign(self, name, new_name):
        """Move every favourite filed under ``name`` to ``new_name``.

        The registry entry moves in the same transaction: a rename that landed
        its rows but not its group name left every one of them filed under a
        group the sidebar did not have.
        """
        members = [
            entry["id"]
            for entry in self._call(self.repository.get_all)
            if entry["group"] == name
        ]
        updates = [(favorite_id, {"group": new_name}) for favorite_id in members]
        moved = self._call(
            self.repository.batch,
            updates,
            register=(new_name,) if new_name else (),
            rename=(name, new_name) if new_name else None,
            validate=self._validate,
            validate_existing=self._full,
        )["updated"]
        return sum(1 for entry in moved if entry is not None)

    def _all_groups(self):
        """The group list as `list` reports it, for a caller that changed one."""
        return self.list(limit=1)["groups"]

    def delete(self, favorite_id):
        self._id(favorite_id)
        if not self._call(self.repository.delete, favorite_id):
            raise ApplicationError("NOT_FOUND", "Favorite no longer exists")
        return {"deleted": True}

    def batch_delete(self, favorite_ids):
        """Delete several favourites as one call.

        A row that is already gone is not a failure here: the goal is "these
        ids are not in the library", and that either already holds or it will.
        One by one, the same click would be N round trips, N published changes
        and N re-reads of a list the reader is watching — which is exactly the
        cost this action exists to remove.

        The answer names the ids that actually went, because the host needs to
        know whether the row its editor is showing still exists.
        """
        if type(favorite_ids) is not list or not 1 <= len(favorite_ids) <= 100:
            raise ApplicationError("INVALID_ARGUMENT", "Invalid favorites selection")
        for favorite_id in favorite_ids:
            self._id(favorite_id)
        if len(set(favorite_ids)) != len(favorite_ids):
            raise ApplicationError("INVALID_ARGUMENT", "Favorites selection repeats an id")
        # One transaction: the reader selected N rows and asked for all of them
        # to go, so "some of them went" is not an answer the UI can draw.
        deleted = self._call(self.repository.batch, (), favorite_ids)["deleted"]
        return {"deleted": len(deleted), "ids": deleted}

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
                    # Same rule as the legacy web route that also favourites a
                    # row: the text is kept for good, so it is decoded by the
                    # clipboard's own rules rather than replaced into U+FFFD.
                    full_text = decode_text(base64.b64decode(text_b64))
                except Exception:
                    full_text = ""
            preview = full_text or str(entry.get("text_preview", "") or "")
            content = full_text or str(entry.get("text_preview", "") or "")
            title = preview[:50] if preview else "(empty)"
            image = ""
            if not full_text:
                # An image favourite was a placeholder string before this: the
                # picture itself has to be kept next to the text, or the list
                # has nothing to draw and the favourite is an "[Image]" row
                # forever.  The history preview is already a bounded thumbnail.
                from internal.application.use_cases.history import picture_of

                for format_name in ("IMAGE_PNG", "IMAGE"):
                    encoded = types.get(format_name) if hasattr(types, "get") else None
                    if not isinstance(encoded, str) or not encoded:
                        continue
                    try:
                        image, _width, _height = picture_of(base64.b64decode(encoded))
                    except Exception:
                        image = ""
                    if image:
                        if not content:
                            content = str(entry.get("text_preview", "") or "Image")
                        if title == "(empty)":
                            title = content[:50]
                        break
            created = self._call(
                self.repository.add, title, content, group, validate=self._validate, image=image
            )
            added.append(created["id"])
        return {"added": len(added), "ids": added}

    def export(self, format="markdown", dest_dir=None):
        """Write every favourite to a Markdown/text/JSON file and report the path."""
        if format not in ("markdown", "text", "json"):
            raise ApplicationError("INVALID_ARGUMENT", "Unsupported export format")
        favorites = self._call(self.repository.get_all)
        suffix = {"markdown": ".md", "text": ".txt", "json": ".json"}[format]
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
        # Reserved through the filesystem, not by looking first: two exports in
        # the same second used to pick one name and the second truncated the
        # first, leaving a file that looked like a complete export of half the
        # library.
        from internal.fsutil import unique_export_path

        dest_path = unique_export_path(target_dir, "clipsync-favorites", suffix, sep="-")
        try:
            text = (
                build_favorites_json(favorites)
                if format == "json"
                else build_favorites_export(favorites, format)
            )
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

    def import_file(self, path):
        """Read a favourites file this project exported, and add what is new.

        Two rules, both about not damaging the library it lands in.  The whole
        file is read and checked before any row is written, so a damaged file
        leaves the library exactly as it was.  And a row the library already
        holds — by body, or by title when it has no body — is counted and
        skipped rather than asked about: a migration is not the place for one
        question per row, because the answer is the same every time and the
        reader is told what happened when it is over.

        Order is the file's own: rows are appended as they come, so a library
        exported and imported elsewhere keeps the sequence it had.  Ids and
        positions are not in the file at all — an id is a fact about the
        library that minted it, and a position about the rows around it.
        """
        self._text(path, "import path", 4096, 1)
        try:
            with open(path, "rb") as handle:
                raw = handle.read(IMPORT_MAX_BYTES + 1)
        except OSError as exc:
            raise ApplicationError(
                "IMPORT_FAILED", "Could not read the favorites file", retryable=True
            ) from exc
        if len(raw) > IMPORT_MAX_BYTES:
            raise ApplicationError("INVALID_ARGUMENT", "Favorites file is too large")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApplicationError(
                "INVALID_ARGUMENT", "Not a ClipSync favorites file"
            ) from exc
        rows = self._import_rows(document)
        # One read of the library rather than one per row, and the sets grow as
        # rows are added: a file that repeats a body must import it once.
        bodies, titles = set(), set()
        for entry in self._call(self.repository.get_all):
            if str(entry.get("content") or "").strip():
                bodies.add(entry["content"])
            else:
                titles.add(str(entry.get("title") or ""))
        added = 0
        for row in rows:
            content, title = row["content"], row["title"]
            if content.strip():
                if content in bodies:
                    continue
                bodies.add(content)
            else:
                if title in titles:
                    continue
                titles.add(title)
            self._call(
                self.repository.add, title, content, row["group"], validate=self._validate
            )
            self._register(row["group"])
            added += 1
        return {"added": added, "skipped": len(rows) - added, "total": len(rows)}

    @staticmethod
    def _import_rows(document):
        """The rows of a favourites file, every one of them checked.

        Nothing is written until this has passed, which is the whole reason it
        is a separate step: a file with a broken row at the end must not leave
        the first four hundred in a library the reader thought they had not
        touched.
        """
        if not isinstance(document, dict) or document.get("kind") != "clipsync-favorites":
            raise ApplicationError("INVALID_ARGUMENT", "Not a ClipSync favorites file")
        version = document.get("version")
        if type(version) is not int or version != FAVORITES_JSON_VERSION:
            raise ApplicationError("INVALID_ARGUMENT", "Unsupported favorites file version")
        rows = document.get("favorites")
        if not isinstance(rows, list):
            raise ApplicationError("INVALID_ARGUMENT", "Not a ClipSync favorites file")
        if len(rows) > IMPORT_MAX_ITEMS:
            raise ApplicationError("INVALID_ARGUMENT", "Favorites file has too many entries")
        checked = []
        for index, row in enumerate(rows, start=1):
            entry = {"title": "", "content": "", "group": ""}
            if isinstance(row, dict):
                for name, maximum in (("title", 256), ("content", 65536), ("group", 128)):
                    value = row.get(name, "")
                    if isinstance(value, str) and len(value) <= maximum:
                        entry[name] = value
                        continue
                    # The refusal a reader sees is about the file as a whole;
                    # which row it was stays here, where a log can say it.
                    logger.info("favorites import: entry %d has an invalid %s", index, name)
                    raise ApplicationError(
                        "INVALID_ARGUMENT", "Favorites file has an entry that cannot be imported"
                    )
            if not entry["title"].strip() and not entry["content"]:
                logger.info("favorites import: entry %d has neither a title nor a body", index)
                raise ApplicationError(
                    "INVALID_ARGUMENT", "Favorites file has an entry that cannot be imported"
                )
            checked.append(entry)
        return checked
