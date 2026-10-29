"""Every runtime event the sidecar publishes has a reader on a front that shows it.

An event's name is a contract between four places that nothing compares: the
Python publisher (``internal/infrastructure/runtime/lan.py`` and the application
behind it), the window's allow-list of names it is willing to show
(``desktop/src/stores/application.ts``), the title table that words a notice
(``desktop/src/components/NoticeStack.vue``), and the two other fronts that
consume the same bus -- the Tauri host's tray/restart reads
(``desktop/src-tauri/src/bridge.rs``) and the phone's push table
(``internal/infrastructure/runtime/phone_push.py``).  A rename on one side and
not the others fails silently, which is what happened to ``sync.redacted``: it
was published from the day the clipboard filter was wired and nothing ever read
it, so a clip that left the device with its sensitive values replaced left
silently (the comment at ``application.ts`` records it).

So the inventory is read out of the sidecar's own source, the way
``test_chat_system_keys.py`` reads its keys and ``test_rpc.py`` reads its
dispatcher: every literal name at a publish call site, plus the names queued for
publishing in ``_publish_presence``.  Each front's handled set is then read from
that front's own source in its own idiom, and the two directions are checked --
a published name no front reads fails, and a front that names something nobody
publishes fails.

What this does *not* claim, so that no reader mistakes it for a whole answer:

* a name listed in ``KNOWINGLY_UNREAD`` is published and read by no front.  Each
  entry carries the reason, and the reason is checkable: the window re-reads the
  active page's snapshot on *every* event the sidecar publishes (the refresh
  timer is set unconditionally at the end of the handler in ``application.ts``),
  so a row one of these changes arrives anyway -- only the ping has no reader.
* a notice raised from inside an ``event.name ===`` comparison that is *not* in
  the allow-list -- ``chat.message``, ``chat.invite`` and ``netpair.peer.changed``
  today -- is reachable by the reverse direction only: renaming one leaves a
  title no comparison names, which fails, but a fourth such notice added without
  a title would not be caught here.
* the phone's WebSocket frame types (``history_updated``, ``transfer_progress``,
  ...) are a second seam with its own names; only the runtime event names it is
  fed by ``phone_push.py`` are read here.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

INTERNAL = ROOT / "internal"
SHELL_SRC = ROOT / "desktop" / "src"
HOST_SRC = ROOT / "desktop" / "src-tauri" / "src"
PHONE_PUSH = INTERNAL / "infrastructure" / "runtime" / "phone_push.py"
NOTICE_STACK = SHELL_SRC / "components" / "NoticeStack.vue"
APPLICATION = SHELL_SRC / "stores" / "application.ts"
SHELL_NOTICE_TITLES_TEST = ROOT / "desktop" / "tests" / "notice-titles.test.ts"

# The functions that put a name on the bus.  Exact names, not a prefix: the
# runtime also has `_publish_hello` (an MQTT greeting) and `_publish_relay` (a
# frame), and neither of those is an event name.
PUBLISHERS = ("publish", "_publish", "_publish_progress", "_publish_safely")

# A dotted name, and nothing else -- every event name the bus carries is one.
_NAME = re.compile(r"[a-z][a-z0-9_]*\.[a-zA-Z0-9_.]+")

# Published, and read by no front.  Listed rather than fixed, because a reader
# of its own is a decision about wording, not a repair: each of these changes
# something a front shows, and the window's unconditional refresh is what shows
# it.  An entry here that gains a reader fails the test below until it is
# removed, so the list cannot quietly go stale.
KNOWINGLY_UNREAD = {
    "chat.invite.response": (
        "the answer changes the session's status, which the chat page re-reads; "
        "the peer that asked is told on the wire, not through this event"
    ),
    "companion.changed": (
        "the mobile companion starting or stopping is read from the companion "
        "status the settings page re-reads, and the phone is the companion"
    ),
    "transfer.web_upload": (
        "the uploaded file lands in the history and the transfer list, both of "
        "which the window re-reads; the phone that sent it has its own reply"
    ),
    "transfers.changed": (
        "rpc.py publishes this beside a mutation the caller has already "
        "awaited, and the transfers page re-reads its list on every event"
    ),
    "url.sent": (
        "the click that sent the URL raises its own toast from the reply "
        "(`已发送网址到 {name}`); this event says the same thing to nobody"
    ),
}

# Names the window raises that are not events on the bus.  The `ui.*` keys name
# the page a click was made on rather than a subsystem, and `pairing.failed` is
# the window's own answer to a pairing attempt that did not go through -- the
# sidecar publishes `pairing.request` and `pairing.resolved`, and neither carries
# "the code was wrong".  Two of the `ui.*` keys (`ui.overview`, `ui.ai`) are
# named by the title table and by the shell's own `notice-titles.test.ts` while
# nothing in `src/` raises them today: legacy's overview and AI pages did, and
# the keys were kept with the titles.
WINDOW_OWN_NAMES = {"ui.overview", "ui.ai", "pairing.failed"}


def published_names() -> set[str]:
    """Every literal event name the sidecar can put on the event bus.

    Read with ``ast``, because a name reaches the bus two ways: a literal at a
    publish call site, and a tuple queued for publishing (``_publish_presence``
    builds ``(name, data)`` pairs and hands the list to ``_publish`` in a loop).
    A regex over ``publish(`` would find the first and miss the second, and the
    second is where ``device.connected`` lives.
    """
    found: set[str] = set()
    for path in sorted(INTERNAL.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_publisher(node.func) and node.args:
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and _NAME.fullmatch(first.value)
                ):
                    found.add(first.value)
        # `for name, data in events: self._publish(name, data)` -- the names
        # were written where the list was built, in the same module.
        for queued in _queued_publish_targets(tree):
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != "append" or not isinstance(node.func.value, ast.Name):
                    continue
                if node.func.value.id != queued or not node.args:
                    continue
                row = node.args[0]
                if not isinstance(row, ast.Tuple) or not row.elts:
                    continue
                head = row.elts[0]
                if (
                    isinstance(head, ast.Constant)
                    and isinstance(head.value, str)
                    and _NAME.fullmatch(head.value)
                ):
                    found.add(head.value)
    # `device.connected` reaches the bus only through that queue, so finding it
    # is what says the queue branch above ran -- an extraction that silently
    # stopped matching would otherwise pass on a smaller set.
    assert "device.connected" in found, "the queued-publish scan found nothing"
    return found


def _is_publisher(func: ast.expr) -> bool:
    if isinstance(func, ast.Attribute):
        return func.attr in PUBLISHERS
    if isinstance(func, ast.Name):
        return func.id in PUBLISHERS
    return False


def _queued_publish_targets(tree: ast.Module) -> set[str]:
    """Names of lists that are published row by row, as ``for a, b in X``."""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Name):
            continue
        if not isinstance(node.target, ast.Tuple):
            continue
        looped = {e.id for e in node.target.elts if isinstance(e, ast.Name)}
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and _is_publisher(inner.func) and inner.args:
                first = inner.args[0]
                if isinstance(first, ast.Name) and first.id in looped:
                    targets.add(node.iter.id)
    return targets


def _shell_files() -> list[pathlib.Path]:
    return sorted(
        path for path in SHELL_SRC.rglob("*") if path.suffix in (".ts", ".vue")
    )


def _shell_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _shell_files())


def notice_allow_list() -> set[str]:
    """The names the window is willing to show as a notice, from its own array."""
    text = APPLICATION.read_text(encoding="utf-8")
    match = re.search(r"\[([^\[\]]*)\]\.includes\(event\.name\)", text)
    assert match, f"the event allow-list is no longer readable in {APPLICATION}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def compared_names() -> set[str]:
    """The names the window compares the wire against, in any of its files."""
    found: set[str] = set()
    for path in _shell_files():
        found |= set(re.findall(r'event\.name === "([^"]+)"', path.read_text(encoding="utf-8")))
    return found


def raised_notice_names() -> set[str]:
    """Every name the window can hand to a notice, from the three call shapes.

    ``pushNotice(name)`` and ``toast(key)`` are the two functions, and one site
    pushes a notice by hand with a literal ``title:``; all three end up as the
    ``title`` the stack words, so all three are read.
    """
    text = _shell_source()
    return (
        notice_allow_list()
        | set(re.findall(r'(?:pushNotice|toast)\("([^"]+)"', text))
        | set(re.findall(r'title: "([^"]+)"', text))
    )


def title_cases() -> set[str]:
    return set(re.findall(r'case "([^"]+)":', NOTICE_STACK.read_text(encoding="utf-8")))


def shell_handled() -> set[str]:
    return notice_allow_list() | compared_names() | raised_notice_names()


def phone_handled() -> set[str]:
    """The event names the phone's push table is keyed by."""
    return set(
        re.findall(
            r'"([a-z][a-zA-Z0-9_.]+)":\s*PhonePush\._',
            PHONE_PUSH.read_text(encoding="utf-8"),
        )
    )


def host_handled() -> set[str]:
    """The event names the native host reads to decide what the tray shows.

    Two shapes: ``value["name"] == "x"`` for a single name, and the
    ``matches!(value["name"].as_str(), Some("x" | "y"))`` lists beside it.  The
    lists span lines, so the block is scanned to its matching paren rather than
    to the end of the line.
    """
    found: set[str] = set()
    for path in sorted(HOST_SRC.rglob("*.rs")):
        text = path.read_text(encoding="utf-8")
        found |= set(
            re.findall(
                r'\["name"\]\s*(?:\.as_str\(\))?\s*(?:==|!=)\s*"([a-z][a-zA-Z0-9_]*\.[a-zA-Z0-9_.]+)"',
                text,
            )
        )
        for match in re.finditer(r"matches!\(", text):
            index, depth = match.end(), 1
            while index < len(text) and depth:
                if text[index] == "(":
                    depth += 1
                elif text[index] == ")":
                    depth -= 1
                index += 1
            block = text[match.end():index]
            if '["name"]' in block:
                found |= set(re.findall(r'"([a-z][a-zA-Z0-9_]*\.[a-zA-Z0-9_.]+)"', block))
    return found


@pytest.fixture(scope="module")
def published() -> set[str]:
    found = published_names()
    assert found, "no published event name found in internal/"
    return found


def test_every_published_event_has_a_reader_or_a_reason(published):
    known = shell_handled() | phone_handled() | host_handled()
    assert known, "the three reader sets came back empty"
    unread = sorted(published - known)
    assert unread == sorted(KNOWINGLY_UNREAD), (
        "published and read by no front: "
        f"{sorted(name for name in unread if name not in KNOWINGLY_UNREAD)}; "
        "listed as unread but now read by one: "
        f"{sorted(name for name in KNOWINGLY_UNREAD if name not in unread)}"
    )


def test_the_window_names_only_events_the_sidecar_publishes(published):
    """Its allow-list and its comparisons are the wire, not a private vocabulary."""
    named = notice_allow_list() | compared_names()
    assert named, "the window's handled set came back empty"
    unknown = sorted(named - published)
    assert not unknown, f"the window handles {unknown}, which nothing publishes"


def test_the_window_raises_no_notice_without_a_title():
    raised = raised_notice_names()
    assert raised, "the window's notice names came back empty"
    untitled = sorted(raised - title_cases())
    assert not untitled, f"a notice keyed by {untitled} would show the key itself"


def test_the_title_table_words_only_notices_the_window_can_raise():
    """The other direction: a case label nothing raises is a rename's leftover."""
    raisable = raised_notice_names() | compared_names() | WINDOW_OWN_NAMES
    dead = sorted(title_cases() - raisable)
    assert not dead, f"{dead} has a title but nothing raises it"


def test_the_phone_and_the_host_name_only_published_events(published):
    for front, handled in (("phone_push", phone_handled()), ("the Tauri host", host_handled())):
        assert handled, f"no event name read out of {front}"
        unknown = sorted(handled - published)
        assert not unknown, f"{front} reads {unknown}, which nothing publishes"


def test_the_shells_own_notice_list_agrees_with_the_publisher(published):
    """The list the shell keeps by hand, held to the sidecar's publishers.

    ``desktop/tests/notice-titles.test.ts`` spells out every name a notice can be
    keyed by, deliberately by hand rather than derived from the store.  Read
    here, it is the one place the shell declares its handled set as a set -- so
    it is held to the publisher: a name on it is either an event the sidecar
    publishes or one of the window's own ``ui.*`` keys.
    """
    text = SHELL_NOTICE_TITLES_TEST.read_text(encoding="utf-8")
    match = re.search(
        r"PUSHED_NOTICE_NAMES\s*=\s*\[([^\]]*)\]", text
    )
    assert match, f"the shell's notice list is no longer readable in {SHELL_NOTICE_TITLES_TEST}"
    listed = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert listed, "the shell's notice list came back empty"
    unknown = sorted(
        name
        for name in listed
        if name not in published and not name.startswith("ui.") and name not in WINDOW_OWN_NAMES
    )
    assert not unknown, f"the shell's notice list names {unknown}, which nothing publishes"
