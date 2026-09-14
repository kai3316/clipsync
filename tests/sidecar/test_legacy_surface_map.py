"""The legacy web surface and the map of where the new page covers it.

The migration's obligation is that the new page can do everything the legacy
desktop fronts could, and that claim was audited by hand three times -- twice
wrongly (`migration-parity-audit.md` §十, §十二, where "the only gap is one" and
"the gap is not in the backend" both had to be taken back).  A sweep is true of
the tree it was done on; `notes/tauri/legacy-surface-map.md` is the same
enumeration written where a test can hold it.

What is held here is the enumeration and the resolution of every target, not the
judgement inside it.  A route added to `internal/web/` and never mapped fails; a
route whose row names a command `main.rs` does not register fails; the reading
that a given target really is that route's counterpart is not machine-checkable
and is not claimed.  The map's own header says so, and the same boundary is
written there rather than left for a reader to discover.

Both halves of the classification are read from source: the routes from the
comparisons the web server dispatches on, the `referenced in` column from the
literal `/api/...` strings each front's files carry.
"""

import ast
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WEB = os.path.join(_ROOT, "internal", "web")
_MAP = os.path.join(_ROOT, "notes", "tauri", "legacy-surface-map.md")

# The document these tests hold lives in `notes/`, which is where the project
# keeps its process record: `docs/` is the GitHub Pages root, so a file under it
# is served to the public, and a migration ledger is not a thing to publish.
# `notes/` is not committed, which means a checkout without it -- CI, anyone
# else's clone -- has nothing to hold the code to.  The guard is skipped there
# rather than deleted: the failure it catches (a routed method nobody wrote
# down, a command that vanished from the table) is one that happens while
# somebody is editing this tree with the notes open beside them.
pytestmark = pytest.mark.skipif(
    not os.path.exists(_MAP),
    reason="legacy-surface-map.md is in notes/, which is not committed",
)


_MAIN_RS = os.path.join(_ROOT, "desktop", "src-tauri", "src", "main.rs")

_ROUTE_FILES = ["routes.py", "server.py"] + [
    os.path.join("api", name)
    for name in sorted(os.listdir(os.path.join(_WEB, "api")))
    if name.endswith(".py")
]

# The desktop web panel and the companion, as the files each one is made of.
_PANEL = [os.path.join(_WEB, "static", "js"), os.path.join(_WEB, "static", "components"),
          os.path.join(_WEB, "static", "index.html")]
_PHONE = [os.path.join(_WEB, "static", "mobile.html")]

_REFERENCE = re.compile(r"/api/[A-Za-z0-9_/\-]*")
_HANDLER = re.compile(r"([a-z_][a-z0-9_]*)\s*,")

# A target that is not a command.  `not carried` is the one that owes an
# explanation, and the test below requires one.
_NON_COMMANDS = {"front-end", "companion only", "not carried"}


def _legacy_routes() -> set:
    """Every `/api/...` the server compares a path against, from its own source.

    Raises on a shape it cannot read, in the idiom of
    `test_rpc_contract.py`: a walk that silently counts fewer routes would
    quietly shrink the very enumeration this file exists to hold.
    """
    routes = set()
    for name in _ROUTE_FILES:
        path = os.path.join(_WEB, name)
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)):
                continue
            if node.left.id != "path":
                continue
            for operator, comparator in zip(node.ops, node.comparators, strict=True):
                if isinstance(operator, ast.Eq):
                    assert isinstance(comparator, ast.Constant), ast.dump(comparator)
                    routes.add(comparator.value)
                elif isinstance(operator, ast.In):
                    assert isinstance(comparator, (ast.Tuple, ast.List, ast.Set)), ast.dump(
                        comparator
                    )
                    for element in comparator.elts:
                        assert isinstance(element, ast.Constant), ast.dump(element)
                        routes.add(element.value)
                elif isinstance(operator, ast.NotEq):
                    # `path != "/"` excludes a route rather than claiming one;
                    # it is the root-path guard, and nothing under /api/ is
                    # dispatched that way.
                    assert isinstance(comparator, ast.Constant), ast.dump(comparator)
                    assert not str(comparator.value).startswith("/api/"), (
                        f"{name} excludes an /api/ route with `!=`, which this test cannot read: "
                        f"{comparator.value}"
                    )
                else:
                    raise AssertionError(
                        f"{name} compares `path` with {type(operator).__name__}, "
                        "which this test cannot read"
                    )
    return {route for route in routes if route.startswith("/api/")}


def _referenced_in(paths) -> set:
    blob = ""
    for path in paths:
        if os.path.isdir(path):
            for base, _dirs, names in os.walk(path):
                for name in names:
                    if name.rsplit(".", 1)[-1] in ("js", "html"):
                        blob_path = os.path.join(base, name)
                        with open(blob_path, encoding="utf-8", errors="replace") as f:
                            blob += f.read()
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                blob += f.read()
    return set(_REFERENCE.findall(blob))


def _reached(route: str, references: set) -> bool:
    """Does the front reach `route`?  A literal, or a literal it sits under.

    Both fronts build the chat and transfer actions by concatenation
    (`api.js:1158` and `mobile.html:3188` both write `'/api/chat/file/' +
    action`), so a literal-only scan would read those sub-routes as used by
    neither page.  The rule is stated in the map's own header, with the four
    literals that trigger it.
    """
    return route in references or any(
        reference.endswith("/") and route.startswith(reference) for reference in references
    )


def _map_rows() -> dict:
    """route -> (referenced in, target, note), read from the map's table."""
    with open(_MAP, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    rows = {}
    for line in lines:
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "Route":
            continue
        # A row that does not split into four cells is a row this reader cannot
        # read, and it must fail rather than be skipped -- a note carrying a `|`
        # would otherwise drop that route out of the comparison silently.
        assert len(cells) == 4, f"the map has a row this reader cannot parse: {line[:120]}"
        route = cells[0].strip("`")
        assert route.startswith("/api/"), f"the map's route column is not a route: {route}"
        assert route not in rows, f"the map lists {route} twice"
        rows[route] = (cells[1], cells[2], cells[3])
    assert len(rows) > 90, "the map lost most of its rows"
    return rows


def _registered_commands() -> set:
    with open(_MAIN_RS, encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(r"generate_handler!\s*\[(.*?)\]", source, re.S)
    assert match, "main.rs no longer registers its handlers in one list"
    commands = set(_HANDLER.findall(match.group(1)))
    assert len(commands) > 100, "the handler list lost most of its commands"
    return commands


def test_every_legacy_route_is_mapped_and_nothing_else_is():
    routes = _legacy_routes()
    mapped = set(_map_rows())

    unmapped = sorted(routes - mapped)
    assert not unmapped, (
        "the legacy web server serves routes the surface map does not list: " + ", ".join(unmapped)
    )
    stale = sorted(mapped - routes)
    assert not stale, (
        "the surface map lists routes the legacy web server no longer serves: " + ", ".join(stale)
    )


def test_the_referenced_in_column_is_what_the_fronts_actually_contain():
    """The classification is measured, so it cannot be quietly asserted.

    A row claiming `panel` for a route only the phone mentions is the kind of
    error that reads as coverage, and it is exactly what a hand sweep produces.
    """
    panel, phone = _referenced_in(_PANEL), _referenced_in(_PHONE)
    assert len(panel) > 50 and len(phone) > 20, (
        "one of the fronts' sources read as almost empty; the scan is broken, not the map"
    )

    wrong = []
    for route, (where, _target, _note) in sorted(_map_rows().items()):
        in_panel, in_phone = _reached(route, panel), _reached(route, phone)
        expected = "both" if (in_panel and in_phone) else (
            "panel" if in_panel else ("phone" if in_phone else "none"))
        if where != expected:
            wrong.append(f"{route}: map says {where}, the sources say {expected}")
    assert not wrong, "the map's referenced-in column has drifted:\n  " + "\n  ".join(wrong)


def test_every_target_resolves_to_a_registered_command_or_says_why_not():
    commands = _registered_commands()
    unresolved = []
    for route, (_where, target, note) in sorted(_map_rows().items()):
        for part in (piece.strip() for piece in target.split(",")):
            if part in commands or part in _NON_COMMANDS:
                continue
            unresolved.append(f"{route}: {part!r} is neither registered nor a stated non-command")
        if target == "not carried" and not note:
            unresolved.append(f"{route}: not carried, and no reason given")
    assert not unresolved, "the surface map points at things that are not there:\n  " + "\n  ".join(
        unresolved
    )


def test_the_panel_owed_routes_are_the_ones_the_obligation_is_about():
    """A floor on the rows that carry the obligation, not on the whole table.

    The table could satisfy every test above while quietly reclassifying its
    panel rows as phone rows.  This holds the size of the half the migration
    actually owes something for.
    """
    rows = _map_rows()
    owed = [route for route, (where, target, _note) in rows.items() if where in ("panel", "both")]
    assert len(owed) > 90, (
        f"only {len(owed)} routes are recorded as used by the desktop panel; "
        "the panel's own surface cannot be that small"
    )
    not_carried = [route for route, (_w, target, _n) in rows.items() if target == "not carried"]
    assert not not_carried, (
        "these panel routes have no counterpart in the new page: " + ", ".join(sorted(not_carried))
    )
