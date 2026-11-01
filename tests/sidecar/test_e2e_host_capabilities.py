"""The Playwright fixture's capability list is the sidecar's list, not a copy.

`desktop/e2e/host.ts` answers `get_app_status` with a hand-written payload, and
the shell gates its own controls on that payload's `capabilities`
(`App.vue` -- `overviewAvailable`, `favoritesAvailable`, `syncAvailable`, and the
rest).  So the fixture decides which branches the only end-to-end test of the
real UI ever exercises: a capability missing there renders its control disabled
in every run, and an assertion written against that control would be written
against a state the application never reaches.

That is not hypothetical.  The list had drifted to the sidecar's *base* block
alone, while the same fixture reported `sync_state: "running"` -- the exact
state in which the sidecar appends the runtime block.  Nothing in the shell
gates on those fourteen names today, which is why nobody noticed.

So the fixture is held to the sidecar's own source, the way `test_rpc.py` holds
the dispatcher to it, and to the state it claims at the same time.
"""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "internal" / "application" / "bootstrap.py"
HOST = ROOT / "desktop" / "e2e" / "host.ts"


def sidecar_capabilities() -> list[str]:
    """The capability list as the sidecar builds it, folded whole.

    The value is ``[...] + ([...] if <runtime up> else [])``, so a plain
    ``literal_eval`` sees only half of it -- which is how the fixture came to
    carry half of it too.
    """
    for node in ast.walk(ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "capabilities":
                return fold(value)
    raise AssertionError(f"no capability list found in {BOOTSTRAP}")


def fold(node: ast.AST) -> list[str]:
    if isinstance(node, ast.List):
        return [ast.literal_eval(element) for element in node.elts]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return fold(node.left) + fold(node.right)
    if isinstance(node, ast.IfExp):
        return fold(node.body) + fold(node.orelse)
    return []


def fixture_capabilities() -> list[str]:
    source = HOST.read_text(encoding="utf-8")
    block = re.search(r"capabilities:\s*\[(.*?)\]", source, re.S)
    assert block, f"no capability list found in {HOST}"
    # Comments go first: the prose explaining why the runtime block is here
    # quotes the sync state, and a bare string scan would read that as a
    # capability named "running".
    without_comments = re.sub(r"//[^\n]*", "", block.group(1))
    return re.findall(r'"([^"]+)"', without_comments)


def test_the_fixture_reports_the_state_whose_capabilities_it_lists():
    """The guard's premise: this fixture claims a running engine."""
    source = HOST.read_text(encoding="utf-8")
    assert re.search(r'get_app_status:\s*\{[^}]*sync_state:\s*"running"', source, re.S), (
        "the fixture no longer reports sync_state: running; the list below is "
        "the one that belongs to a running engine, so either the state or the "
        "list has to change"
    )


def test_the_fixture_lists_every_capability_the_sidecar_grants():
    sidecar = sidecar_capabilities()
    fixture = fixture_capabilities()
    # Empty would pass every "missing" check below -- the failure mode this
    # file exists to prevent, so it is asserted rather than assumed.
    assert sidecar and fixture, (len(sidecar), len(fixture))
    assert sorted(set(fixture) - set(sidecar)) == [], "the fixture grants what the sidecar does not"
    assert sorted(set(sidecar) - set(fixture)) == [], (
        "the fixture withholds what the sidecar grants"
    )


def test_the_fixture_keeps_the_sidecars_order():
    """Order is not load-bearing, but a reordered copy is a hand-edit, and a
    hand-edit is how the last drift happened."""
    assert fixture_capabilities() == sidecar_capabilities()
