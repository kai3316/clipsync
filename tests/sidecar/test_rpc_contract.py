"""The IPC contract table and the code it describes are held to each other.

`notes/tauri/rpc-v1.md` is the document the migration points at when it claims the
window can reach a capability, so a method the dispatcher routes and the table
does not list is exactly the kind of gap the table exists to prevent -- and the
table carried a "this table is not yet complete" note for long enough that a
reader could not tell a documented absence from an undocumented one.

The extraction below is deliberately source-level rather than a list kept here:
a hand-written list would be a third copy of the same fact, and the whole point
is to catch the dispatcher moving.  It reads the same shapes the dispatcher is
written in -- `method == "x"`, `method in ("x", "y")`, `method in <dict>` where
the dict is either local or the imported favorites table, and
`method.startswith("x.")` whose family is named in the branch below it -- and
fails on a shape it does not understand rather than silently counting fewer
methods.

Both directions of both columns are held.  The table's *second* column is the
IPC method the dispatcher routes; its *first* column is the Tauri command the
host registers, held against `generate_handler!` in `main.rs`.  The command
column had no guard at all until the two were compared, and eleven registered
commands were absent from it -- including `ai_local`, which is how the family
below was found: a command column that is not held is a place a method family
can hide.
"""

import ast
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from internal.adapters.sidecar.favorites import METHODS as FAVORITES_METHODS

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RPC = os.path.join(_ROOT, "internal", "adapters", "sidecar", "rpc.py")
_CONTRACT = os.path.join(_ROOT, "notes", "tauri", "rpc-v1.md")

# The document these tests hold lives in `notes/`, which is where the project
# keeps its process record: `docs/` is the GitHub Pages root, so a file under it
# is served to the public, and a migration ledger is not a thing to publish.
# `notes/` is not committed, which means a checkout without it -- CI, anyone
# else's clone -- has nothing to hold the code to.  The guard is skipped there
# rather than deleted: the failure it catches (a routed method nobody wrote
# down, a command that vanished from the table) is one that happens while
# somebody is editing this tree with the notes open beside them.
pytestmark = pytest.mark.skipif(
    not os.path.exists(_CONTRACT),
    reason="rpc-v1.md is in notes/, which is not committed",
)


_MAIN_RS = os.path.join(_ROOT, "desktop", "src-tauri", "src", "main.rs")

# A Tauri command name as `generate_handler!` lists it.
_HANDLER = re.compile(r"([a-z_][a-z0-9_]*)\s*,")

# A method name is a lowercase dotted name of two or three segments
# (`ai.profiles.update` is the three); the table writes it in the second column,
# sometimes with a trailing explanation, so the match is a find rather than a
# full-cell compare.
_METHOD = re.compile(r"\b[a-z][a-z_]*(?:\.[a-z][a-z_]*)+\b")


def _prefixed_families(tree) -> set:
    """Expand `method.startswith("ai.local.")` into the family it dispatches.

    A prefix is the one shape that cannot be read off the comparison itself: the
    branch validates the suffix against its own tuple and the method is built
    from the two halves, so the members live in the `action not in (...)` just
    inside it.  That tuple is read rather than guessed, and an unresolvable
    prefix fails -- which is the whole reason this function exists.  The walk
    above saw `method.startswith(...)` as a call, not a comparison, so before
    this was here the five `ai.local.*` methods were routed, documented nowhere,
    and counted by neither side of the comparison: the two sets matched because
    both were missing the same five names.
    """
    families = set()
    nodes = list(ast.walk(tree))

    # Anything else done to `method` is a shape this test cannot read, and it
    # must fail rather than be walked past -- `method.startswith` is an attribute
    # access too, so the calls are collected first and their own attribute nodes
    # are exempt.
    calls = [
        node
        for node in nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "method"
    ]
    for node in calls:
        if node.func.attr != "startswith":
            raise AssertionError(
                f"the dispatcher calls method.{node.func.attr}(), which this test cannot read"
            )
    read_attributes = {id(node.func) for node in calls}
    for node in nodes:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "method"
            and id(node) not in read_attributes
        ):
            raise AssertionError(
                "the dispatcher does something to `method` this test cannot read: " + ast.dump(node)
            )

    for node in calls:
        (argument,) = node.args
        assert isinstance(argument, ast.Constant) and isinstance(argument.value, str), ast.dump(
            argument
        )
        prefix = argument.value

        # The suffix is held in a name sliced off `method`, and that name is what
        # the branch validates -- so the two are found by following the slice
        # rather than by guessing at any `not in` in the file.
        holder = None
        for other in nodes:
            if (
                isinstance(other, ast.Assign)
                and len(other.targets) == 1
                and isinstance(other.targets[0], ast.Name)
                and isinstance(other.value, ast.Subscript)
                and isinstance(other.value.value, ast.Name)
                and other.value.value.id == "method"
            ):
                holder = other.targets[0].id
        assert holder, (
            f"the branch under `{prefix}` does not slice `method`, so it cannot be checked"
        )

        suffixes = None
        for other in nodes:
            if not (
                isinstance(other, ast.Compare)
                and isinstance(other.left, ast.Name)
                and other.left.id == holder
                and isinstance(other.ops[0], ast.NotIn)
                and isinstance(other.comparators[0], (ast.Tuple, ast.List, ast.Set))
                and other.comparators[0].elts
                and all(
                    isinstance(element, ast.Constant) and isinstance(element.value, str)
                    for element in other.comparators[0].elts
                )
            ):
                continue
            suffixes = {element.value for element in other.comparators[0].elts}
        assert suffixes, (
            f"the branch under `{prefix}` does not name its family against `{holder}`, "
            "so it cannot be checked"
        )
        families |= {prefix + suffix for suffix in suffixes}
    return families


def _routed_methods() -> set:
    """Every method the dispatcher routes, read from its own source.

    Raises instead of returning a short set: a group form this walk does not
    understand must fail the test, not quietly shrink the comparison.
    """
    with open(_RPC, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    groups = {"FAVORITES_METHODS": set(FAVORITES_METHODS)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            keys = {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            for target in node.targets:
                if isinstance(target, ast.Name):
                    groups[target.id] = keys

    methods = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)):
            continue
        if node.left.id != "method":
            continue
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            if isinstance(operator, ast.Eq):
                assert isinstance(comparator, ast.Constant), ast.dump(comparator)
                methods.add(comparator.value)
            elif isinstance(operator, ast.In):
                if isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                    for element in comparator.elts:
                        assert isinstance(element, ast.Constant), ast.dump(element)
                        methods.add(element.value)
                elif isinstance(comparator, ast.Name):
                    assert comparator.id in groups, (
                        f"the dispatcher routes a group this test cannot resolve: {comparator.id}"
                    )
                    methods |= groups[comparator.id]
                else:
                    raise AssertionError(ast.dump(comparator))
            else:
                raise AssertionError(
                    f"the dispatcher compares `method` with {type(operator).__name__}, "
                    "which this test cannot read"
                )
    methods |= _prefixed_families(tree)
    assert methods, "no dispatched methods were found; the dispatcher was rewritten"
    return methods


def _documented_methods() -> set:
    with open(_CONTRACT, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    documented = set()
    for line in lines:
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or cells[1] == "IPC method":
            continue
        documented |= set(_METHOD.findall(cells[1]))
    return documented


def _documented_commands() -> set:
    """The first column's command names.  A row may carry several.

    Every name in that column is a Tauri command, which is what makes a plain
    word scan safe here: a `none (...)` row puts its prose in the *second*
    column, and the rows that need to say "the sidecar serves this to someone
    else" say it there.
    """
    with open(_CONTRACT, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    commands = set()
    for line in lines:
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or cells[1] == "IPC method":
            continue
        commands |= set(re.findall(r"[a-z_][a-z0-9_]*", cells[0]))
    assert len(commands) > 100, "the contract table lost its command column"
    return commands


def _registered_commands() -> set:
    """The commands `generate_handler!` registers, read from `main.rs`."""
    with open(_MAIN_RS, encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(r"generate_handler!\s*\[(.*?)\]", source, re.S)
    assert match, "main.rs no longer registers its handlers in one list"
    commands = set(_HANDLER.findall(match.group(1)))
    assert len(commands) > 100, "the handler list lost most of its commands"
    return commands


def test_every_routed_method_is_documented_and_nothing_else_is():
    routed = _routed_methods()
    documented = _documented_methods()

    undocumented = sorted(routed - documented)
    assert not undocumented, (
        "the dispatcher routes methods the IPC contract does not list: "
        + ", ".join(undocumented)
    )
    invented = sorted(documented - routed)
    assert not invented, (
        "the IPC contract lists methods the dispatcher does not route: "
        + ", ".join(invented)
    )


def test_every_registered_command_is_documented_and_nothing_else_is():
    """The other half of the same table, which had no guard at all.

    `main.rs` holds its own list to `build.rs` and to `capabilities/main.json`
    (`every_handler_command_is_registered_in_build_and_acl`), so a command cannot
    be added without an ACL entry -- but nothing held that list to the document
    a reader actually reads.  Eleven registered commands were absent from the
    table, and one of them, `ai_local`, is a host name for five IPC methods that
    were absent from the method column too.
    """
    registered = _registered_commands()
    documented = _documented_commands()

    undocumented = sorted(registered - documented)
    assert not undocumented, (
        "main.rs registers commands the IPC contract's command column does not list: "
        + ", ".join(undocumented)
    )
    invented = sorted(documented - registered)
    assert not invented, (
        "the IPC contract's command column names commands main.rs does not register: "
        + ", ".join(invented)
    )


def test_the_contract_no_longer_claims_to_be_incomplete():
    """The coverage above is the guarantee; this is only its consequence.

    While the table was partial it said so, and a reader had to take the note's
    word for which rows were missing.  With the two held equal, that sentence
    would now be false.
    """
    with open(_CONTRACT, encoding="utf-8") as handle:
        document = handle.read()
    assert "this table is not yet complete" not in document
    assert "still absent here" not in document
