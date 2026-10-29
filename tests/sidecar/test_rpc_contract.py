"""The IPC contract table and the code it describes are held to each other.

`contracts/rpc-v1.md` is the document the migration points at when it claims the
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

The table is only half the chain, though, and the half that is written down is
the half that cannot go wrong quietly.  One operation is four spellings --
`favorites.list` in the sidecar, `list_favorites` on the wire, `allow-list-
favorites` in the ACL, `favoriteId` in the window -- and the ends of that chain
were the two with no guard at all: nothing compared the method string `main.rs`
hands the bridge to the table's second column, and nothing compared the names
and arguments `desktop/src/api/bridge.ts` sends to the commands `main.rs`
registers.  Neither failure is a compile error.  A method name the dispatcher
does not route comes back `METHOD_NOT_FOUND`; a command the host does not
register is a WebView error, and a mistyped argument is a parameter that
deserialises to nothing -- all three look like "the button did nothing".

So the same source-level reading is extended to both ends.  Nothing here is
generated, deliberately: the command bodies hold real behaviour -- `chat_action`
is the general form of four wrappers, `ai_local` builds its method from a
validated suffix -- and a generator that could emit them would be a second
implementation of the host.  A test that reads what is there and holds it
consistent is what the chain admits.
"""

import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from internal.adapters.sidecar import rpc as rpc_module
from internal.adapters.sidecar.favorites import METHODS as FAVORITES_METHODS

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RPC = os.path.join(_ROOT, "internal", "adapters", "sidecar", "rpc.py")
# In the repository, not in `notes/`.  These tables are the contract, and a
# contract that only the maintainer's checkout can read is not one: this file
# spent its first life skipped on CI and in every clone (see git history for the
# `skipif`), which meant the one guard against the dispatcher and the table
# drifting apart ran nowhere but one machine.  `docs/` is the GitHub Pages root
# and cannot hold it, so the tables live in `contracts/`.
_CONTRACT = os.path.join(_ROOT, "contracts", "rpc-v1.md")


_MAIN_RS = os.path.join(_ROOT, "desktop", "src-tauri", "src", "main.rs")
_TAURI_SRC = os.path.dirname(_MAIN_RS)
_BRIDGE_TS = os.path.join(_ROOT, "desktop", "src", "api", "bridge.ts")

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


def _routing_body(tree: ast.Module) -> ast.Module:
    """`Dispatcher._dispatch`'s own statements, which is where routing happens.

    Scoping the walk is as load-bearing as the shapes it understands.  Reading
    every comparison against a name called ``method`` in the whole module made
    the *other* reader of that name a router: the latency budget's
    ``method in BLOCKING_HANDLERS`` names a table of handlers it ***excuses***,
    and the whole module's walk counted every key of it as routed -- so the
    check on that table could not fail, every stale row having been folded into
    the very set it was compared against.  A guard a wrong `if` can widen is not
    holding anything, and the fix is to ask the one function that routes.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "Dispatcher"):
            continue
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == "_dispatch":
                return ast.Module(body=member.body, type_ignores=[])
    raise AssertionError(
        "Dispatcher._dispatch is gone; this test reads it to find what is routed"
    )


def _routed_methods() -> set:
    """Every method the dispatcher routes, read from its own source.

    Raises instead of returning a short set: a group form this walk does not
    understand must fail the test, not quietly shrink the comparison.
    """
    with open(_RPC, encoding="utf-8") as handle:
        body = _routing_body(ast.parse(handle.read()))
    groups = {"FAVORITES_METHODS": set(FAVORITES_METHODS)}
    for node in ast.walk(body):
        # Annotated or not: a table `_dispatch` compares `method` against is
        # read the same way either way, and an annotated one is an `AnnAssign`.
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Dict):
            continue
        groups[target.id] = {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }

    methods = set()
    for node in ast.walk(body):
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
    methods |= _prefixed_families(body)
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


# The other end of the chain.  Below the contract document there is no compiler:
# `main.rs` hands the bridge a *string*, `bridge.ts` invokes a *string*, and Tauri
# binds an argument object to parameters by name -- so a method the dispatcher
# does not route comes back `METHOD_NOT_FOUND`, a command the host does not
# register is an error from the WebView, and a mistyped argument is a parameter
# that deserialises to nothing.  None of the three is a compile error, and all
# three reach a user as "the button did nothing".
#
# These are the commands `generate_handler!` registers that no window code calls,
# each with the reason it is registered anyway.  The table is checked rather than
# trusted: an entry fails the test the moment the window calls the command, so a
# row cannot outlive the reason it was written for.
UNCALLED_BY_THE_WINDOW = {
    "chat_action": "the body `chat_action_command!` expands the four chat commands into",
}

# What Tauri supplies to a command itself rather than reading from the argument
# object the window sends, so they are not names the window owes.
_HOST_SUPPLIED = frozenset({"window", "app", "host", "webview", "state"})


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _group_end(source: str, open_at: int, opener: str, closer: str) -> int:
    """The index just past the group opening at *open_at*.

    `open_at` is where the caller found the bracket, so the scan starts there and
    a string containing an unbalanced bracket of the other kind is not looked for
    -- these sources carry no such string in a group this test reads.
    """
    depth = 0
    for index in range(open_at, len(source)):
        if source[index] == opener:
            depth += 1
        elif source[index] == closer:
            depth -= 1
            if depth == 0:
                return index + 1
    raise AssertionError(f"unbalanced {opener!r} at offset {open_at}")


def _arguments(body: str, angles: bool = False) -> list[str]:
    """The top-level comma-separated pieces of an argument or parameter list.

    `angles` is the difference between the two, and it is asked for only where it
    is needed: a Rust parameter list puts commas inside its generic types
    (`State<'_, Host>`), while an argument list at a call site has no `<` for a
    later `>` to close.
    """
    pieces, depth, current = [], 0, ""
    for character in body:
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif angles and character == "<":
            depth += 1
        elif angles and character == ">":
            depth -= 1
        if character == "," and depth == 0:
            pieces.append(current)
            current = ""
        else:
            current += character
    pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def _host_sources() -> dict:
    """Every `.rs` file of the Tauri host, by name.

    The whole directory rather than `main.rs`, because the bridge is reached from
    beside it too: `app.shutdown` is called from `bridge.rs`, on the shutdown path
    that has no window left to ask.
    """
    return {
        name: _read(os.path.join(_TAURI_SRC, name))
        for name in sorted(os.listdir(_TAURI_SRC))
        if name.endswith(".rs")
    }


def _forwarding_helpers(source: str) -> dict:
    """Name -> where its `method: &str` parameter sits, for the host's forwards.

    Read off the definitions rather than listed here, so the two that exist are
    not a pair this test happens to know: `chat_call` is the front door of the
    chat family and `tray_call` is how the native tray reaches the bridge, and a
    third would be found the same way it would be used.
    """
    helpers = {}
    for match in re.finditer(r"\bfn\s+([a-z_][a-z0-9_]*)\s*\(", source):
        end = _group_end(source, match.end() - 1, "(", ")")
        parameters = _arguments(source[match.end():end - 1], angles=True)
        for index, parameter in enumerate(parameters):
            if re.search(r"\bmethod\s*:\s*&str\b", parameter):
                helpers[match.group(1)] = index
    return helpers


def _rust_host_methods() -> set:
    """Every sidecar method the host names, read from the host's own source.

    Three shapes, which is what the host is written in:

    * `.call("a.b", ...)` and `.request("a.b", ...)` -- the bridge's two entry
      points;
    * a call of a *forwarding helper*, being any function that takes a
      `method: &str` and passes it to one of those two;
    * `.call(&format!("ai.local.{}", action), ...)` -- a family built from a
      validated suffix, expanded against the methods the dispatcher routes,
      which is the one shape whose members cannot be read off the call.

    A helper handed something that is not a method literal fails rather than
    being walked past: an unread name here would shrink the set being compared,
    which is the failure mode this whole file exists to avoid.  The `format!`
    expansion cannot catch a typo in a prefix, but it fails when the prefix
    routes nothing, which is the same mistake one rename later.
    """
    sources = _host_sources()
    main = sources["main.rs"]

    methods = set()
    for source in sources.values():
        methods |= set(
            re.findall(r'\.(?:call|request)\(\s*"([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)"', source)
        )

    for name, position in _forwarding_helpers(main).items():
        for match in re.finditer(rf"(?<!fn )\b{name}\s*\(", main):
            end = _group_end(main, match.end() - 1, "(", ")")
            arguments = _arguments(main[match.end():end - 1])
            literal = position < len(arguments) and re.fullmatch(r'"([^"]*)"', arguments[position])
            assert literal, (
                f"{name}() is handed a method this test cannot read, so the method it "
                f"names would go unchecked: {arguments[position:position + 1]}"
            )
            methods.add(literal.group(1))

    routed = _routed_methods()
    for prefix in re.findall(
        r'\.(?:call|request)\(\s*&format!\(\s*"([a-z][a-z0-9_.]*)\{\}"', main
    ):
        family = {method for method in routed if method.startswith(prefix)}
        assert family, f"the host builds method names under {prefix!r}, which routes nothing"
        methods |= family

    assert len(methods) > 100, "the host method scan lost most of its methods"
    return methods


def _camel(name: str) -> str:
    """`favorite_id` as the window writes it: Tauri maps camelCase to snake_case."""
    head, *rest = name.split("_")
    return head + "".join(word.title() for word in rest)


def _rust_commands() -> dict:
    """Tauri command -> the argument names the window owes it, in camelCase.

    Both spellings of a command are here: the hand-written ones, and the four
    `chat_action_command!` expands, whose parameters live in the macro body and
    whose names live in its invocations.  Reading only the first spelling would
    have left those four to be compared against nothing -- and they are the four
    the chat panel calls on every message.
    """
    main = _host_sources()["main.rs"]
    commands = {}

    for match in re.finditer(
        r"#\[tauri::command\]\s*(?:pub\s+)?(?:async\s+)?fn\s+([a-z_][a-z0-9_]*)\s*\(", main
    ):
        end = _group_end(main, match.end() - 1, "(", ")")
        commands[match.group(1)] = _parameter_names(main[match.end():end - 1])

    for match in re.finditer(r"macro_rules!\s+([a-z_][a-z0-9_]*)\s*\{", main):
        macro = match.group(1)
        # Only a macro that is *invoked* with a command name is a command, and
        # only then does a body this test cannot read become a problem: a macro
        # that generates them and is not understood would take its commands out
        # of the comparison below entirely.
        expanded = re.findall(rf"\b{macro}!\s*\(\s*([a-z_][a-z0-9_]*)", main)
        if not expanded:
            continue
        body = main[match.end():_group_end(main, match.end() - 1, "{", "}") - 1]
        signature = re.search(r"#\[tauri::command\][^{]*?\bfn\s+(\$\w+)\s*\(", body)
        assert signature, f"{macro}! expands commands, and this test cannot read how"
        end = _group_end(body, signature.end() - 1, "(", ")")
        parameters = _parameter_names(body[signature.end():end - 1])
        for name in expanded:
            commands[name] = parameters

    assert len(commands) > 100, "the command scan lost most of its commands"
    return commands


def _parameter_names(parameters: str) -> set:
    """The window's names for a Rust parameter list, minus what Tauri supplies."""
    names = set()
    for parameter in _arguments(parameters, angles=True):
        name = parameter.split(":")[0].strip()
        if name and name not in _HOST_SUPPLIED:
            names.add(_camel(name))
    return names


def _window_commands() -> dict:
    """`bridge.ts`: Tauri command -> the argument keys the window sends, or None.

    Read with a balanced scan rather than a regex because a call carries a
    generic type argument first and that type is an object of its own
    (`command<FavoritesPage>("list_favorites", {...})`), so the `>` a regex would
    stop at is as likely to close a nested type as the generic.  The helper's own
    declaration is skipped by name: it is the one `command(` in the file that is
    not a call.
    """
    source = _read(_BRIDGE_TS)
    commands = {}
    for match in re.finditer(r"(?<!function )\bcommand\b", source):
        after = match.end()
        if after < len(source) and source[after] == "<":
            after = _group_end(source, after, "<", ">")
        while after < len(source) and source[after] in " \t\r\n":
            after += 1
        if after >= len(source) or source[after] != "(":
            continue
        end = _group_end(source, after, "(", ")")
        arguments = _arguments(source[after + 1:end - 1])
        literal = re.fullmatch(r'"([a-z_][a-z0-9_]*)"', arguments[0])
        assert literal, f"bridge.ts calls command() with {arguments[0]!r}, not a command name"
        keys = None
        if len(arguments) > 1 and arguments[1].startswith("{"):
            keys = {
                key.split(":")[0].strip()
                for key in _arguments(arguments[1][1:-1])
                if re.fullmatch(r"[A-Za-z_$][\w$]*", key.split(":")[0].strip())
            }
        commands[literal.group(1)] = keys

    assert len(commands) > 100, "the bridge scan lost most of its commands"
    return commands


def test_every_method_the_host_names_is_one_the_dispatcher_routes():
    """A method the host names is a method the sidecar has to answer.

    Nothing holds the two together.  The name crosses the bridge as a string and
    comes back as an error object, so a rename on either side is a call that
    starts failing in a window nobody is looking at -- the same silence the
    contract table was written to end, one layer further out.

    Both directions, because the surface has been pruned before: the reverse says
    every routed method still has a caller, which is what a retired panel leaves
    behind.  A method kept for the web API would have to be counted as an
    exception here, and the day one is, that is a decision worth writing down.
    """
    named = _rust_host_methods()
    routed = _routed_methods()

    unknown = sorted(named - routed)
    assert not unknown, (
        "the Rust host names sidecar methods the dispatcher does not route: " + ", ".join(unknown)
    )
    unreached = sorted(routed - named)
    assert not unreached, (
        "the dispatcher routes methods the Rust host never names: " + ", ".join(unreached)
    )


def test_the_window_calls_only_commands_the_host_registers():
    """`bridge.ts` and `generate_handler!` are two lists of the same commands.

    A name in one and not the other is a rejected promise in the WebView, which
    the window reports as a failure with no cause.  `chat_action` is the one
    command nothing in the window calls -- it is the body the four chat commands
    are expanded from -- and it is recorded rather than ignored, so the list of
    exceptions is a claim the test holds instead of a set subtraction nobody
    revisits.
    """
    registered = _registered_commands()
    called = set(_window_commands())

    unknown = sorted(called - registered)
    assert not unknown, (
        "bridge.ts calls commands main.rs does not register: " + ", ".join(unknown)
    )
    unused = sorted(registered - called)
    assert unused == sorted(UNCALLED_BY_THE_WINDOW), (
        "the commands no window code calls have changed; they are now "
        + ", ".join(unused)
        + " and the recorded reasons are for " + ", ".join(sorted(UNCALLED_BY_THE_WINDOW))
    )


def test_the_window_names_every_argument_the_command_reads():
    """Tauri binds the argument object to parameters by name, and says nothing.

    An object missing a key is a parameter that deserialises to nothing, so the
    command runs on a default it never chose -- and the failure is wherever that
    value is used, not at the call.  Comparing the keys to the parameter list is
    the only place the two spellings meet: `favorite_id` in Rust is `favoriteId`
    on the wire, and the name is what binds them.
    """
    declared = _rust_commands()
    sent = _window_commands()

    mismatched = []
    for name in sorted(sent):
        if name not in declared:
            continue
        wanted, given = declared[name], sent[name] or set()
        if wanted != given:
            mismatched.append(
                f"{name}: sends {sorted(given)}, reads {sorted(wanted)}"
            )
    assert not mismatched, "the window and the command disagree about arguments:\n  " + "\n  ".join(
        mismatched
    )


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


def test_every_handler_the_budget_excuses_is_one_the_dispatcher_routes():
    """The latency budget's exception table is a claim about the dispatcher.

    A row in ``BLOCKING_HANDLERS`` says "the window is meant to wait for this
    one", so a row for a method nothing routes is a decision recorded about a
    handler that does not exist -- and the stale row is worse than a missing
    one, because it is the shape an entry takes when the method it excused has
    been renamed and something else now answers to nothing.

    What this does *not* check is the other half of the table's purpose: that
    every handler which really does block has a row.  That would mean deciding
    which runtime callables reach ``_connect_and_wait`` or build an archive, and
    a test that guessed at that would pass by guessing correctly and fail on the
    next rename.  The table is read at dispatch time instead
    (``Dispatcher._report_overrun``), so the half that cannot be checked
    statically is the half that names itself in the log when it happens.
    """
    routed = _routed_methods()
    stale = sorted(set(rpc_module.BLOCKING_HANDLERS) - routed)
    assert not stale, (
        "the blocking table excuses methods the dispatcher does not route: "
        + ", ".join(stale)
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
