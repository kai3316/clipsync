"""Every sentence the sidecar names for a reader exists on the front that shows it.

A system row travels as a dotted key rather than as a sentence -- ``text`` is
empty and ``text_key`` carries ``chat.system.*`` -- because one entry is
rendered by four different front ends and the wording has to follow the
reader's language.  That makes the key a contract with four tables, and nothing
in the code makes a new one fail: an unresolvable key silently degrades, which
is what happened to the shell, where ``ChatView`` fell through to printing
``chat.system.peer_offline`` on screen.

So the inventory is read from the sidecar's own source, the way
``test_rpc.py`` reads the dispatcher, and each front is held to it: the shared
locale files the Companion resolves against are the canonical sentences, the
shell is compared to them verbatim, and the phone and the sidecar's own Python
catalogue (``internal/i18n``, which the web server falls back to) are checked
for coverage (their wording is their own -- the phone's inline dictionary is
only a fallback for a payload with no resolvable key).

The same technique covers the other namespaces the sidecar names, as far as the
key schemes honestly allow -- four catalogues carry overlapping sentences and
nothing else compares them.

``error.*``, and the update failures beside it, are the sidecar's own:
``internal/application/errors.py``'s two tables (the sentence a code has, and
the sentence a code cannot tell apart), ``internal/system/updater.py``'s
``_REASON_KEYS`` and ``internal/system/update_service.py``'s ``_REJECTION_KEYS``
turn a code into a key, and ``T`` resolves that key before anything crosses the
wire.  The catalogue held here is ``internal/i18n``'s, and it is the only one:
the shell's catalog is keyed by the Chinese source string, and the Rust host's
table says outright that a code the sidecar sends must *not* have an entry there
(``desktop/src-tauri/src/i18n.rs``), because a generic sentence would overwrite
the one ``errors.py`` already worded.

``diag.*`` belongs to the other catalogue.  ``internal/diagnostics/report.py``
and ``internal/diagnostics/localize.py`` name these keys, and ``localize.py``
resolves them out of ``internal/web/static/locales/*.json`` -- leaving the raw
``*_key`` pairs in place so a client can resolve them itself.  So the web
catalogues are what is held to the report's literals: ``internal/i18n`` carries
no ``diag.*`` key by design, and the shell and the phone render the ``*_text``
fields the sidecar has already resolved.

Two catalogues are deliberately not held to an inventory read from the sidecar,
and this file does not pretend to check them:

* the shell's own (``desktop/src/i18n/en.ts`` and its siblings).  Its keys are
  the Chinese source strings themselves, gettext-style, so the default output
  stays byte-identical (see ``desktop/src/i18n/index.ts``); measured on
  2026-10-16, 0 of its keys appear in either the web or the Python catalogue,
  and 0 of the web catalogue's appear in it.  Holding it to a key list read from
  the sidecar would mean inventing a key scheme it does not have.  The one
  namespace that does cross -- ``chat.system.*``, as a dotted key on the wire --
  is checked above, verbatim, and the shell's own completeness (every literal in
  ``src/`` has an entry) is ``desktop/tests/i18n.test.ts``'s.
* the Rust host's 19 strings (``desktop/src-tauri/src/i18n.rs``) are the tray
  labels and the failures the host raises in its own right; the sidecar names
  none of them, so there is no inventory to read.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

from internal.i18n import _EN, LOCALES

ROOT = pathlib.Path(__file__).resolve().parents[2]

INTERNAL = ROOT / "internal"
SIDECAR = ROOT / "internal" / "sync" / "nearby_chat.py"
SHARED = ROOT / "internal" / "web" / "static" / "locales"
SHELL = ROOT / "desktop" / "src" / "i18n" / "chat.ts"
PHONE = ROOT / "internal" / "web" / "static" / "mobile.html"

LOCALES_ON_DISK = ("zh-CN", "en")

# A dotted key, and nothing else: the shape every one of these tables is keyed
# by, and the filter that keeps a `*_KEYS` table's unrelated values out.
_KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")


def sent_keys() -> set[str]:
    """The keys the sidecar's source can put on the wire."""
    return set(re.findall(r'text_key="([^"]+)"', SIDECAR.read_text(encoding="utf-8")))


def shared_tables() -> dict[str, dict[str, str]]:
    return {
        locale: json.loads((SHARED / f"{locale}.json").read_text(encoding="utf-8"))
        for locale in LOCALES_ON_DISK
    }


def shell_tables() -> dict[str, dict[str, str]]:
    """The shell's ``NOTICES`` map, unescaped back into plain strings.

    Read line by line rather than with one regex: the sentences contain
    ``{name}``, so anything that stops at the first closing brace stops inside
    the first placeholder.
    """
    tables: dict[str, dict[str, str]] = {}
    locale: str | None = None
    for line in SHELL.read_text(encoding="utf-8").splitlines():
        header = re.match(r'\s*"([A-Za-z-]+)":\s*\{\s*$', line)
        if header:
            locale = header.group(1)
            tables[locale] = {}
            continue
        if locale is None:
            continue
        if re.match(r"\s*\},\s*$", line):
            locale = None
            continue
        pair = re.match(r'\s*"([^"]+)":\s*"((?:[^"\\]|\\.)*)",\s*$', line)
        if pair:
            tables[locale][pair.group(1)] = json.loads(f'"{pair.group(2)}"')
    return {name: rows for name, rows in tables.items() if rows}


def diag_keys() -> set[str]:
    """Every ``diag.*`` key a literal in the sidecar's source names.

    Read over the whole tree rather than the three files that name one today,
    so a report builder added elsewhere is held the same way.  Every one of
    these is resolved by ``internal/diagnostics/localize.py`` out of the web
    catalogue, which is what the test below holds.
    """
    found: set[str] = set()
    for path in sorted(INTERNAL.rglob("*.py")):
        found |= set(re.findall(r'"(diag\.[a-z0-9_.]+)"', path.read_text(encoding="utf-8")))
    return found


def sidecar_resolved_keys() -> set[str]:
    """The keys the sidecar turns into a sentence for itself, before the wire.

    Read with ``ast`` rather than a regex, because a key reaches ``T`` three
    ways and a pattern that knows only the first would shrink silently: a
    literal argument, a value in a module-level ``*_KEYS`` table (the sentence
    a code has), and a module-level ``*_KEY`` constant (the sentence a code
    cannot be told apart from).  ``diag.*`` is subtracted: those keys belong to
    the web catalogue and are resolved from it, not from ``internal/i18n``.
    """
    found: set[str] = set()
    for path in sorted(INTERNAL.rglob("*.py")):
        if path.parent.name == "i18n":
            continue  # the catalogue itself, not a caller
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "T" or not node.args:
                continue
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and _KEY.fullmatch(first.value)
            ):
                found.add(first.value)
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                value, targets = node.value, [node.target]
            elif isinstance(node, ast.Assign):
                value, targets = node.value, node.targets
            else:
                continue
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not names or value is None:
                continue
            if isinstance(value, ast.Dict) and all(n.endswith("_KEYS") for n in names):
                strings = [
                    v.value
                    for v in value.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                ]
            elif (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and all(n.endswith("_KEY") for n in names)
            ):
                strings = [value.value]
            else:
                continue
            found |= {s for s in strings if _KEY.fullmatch(s)}
    return {key for key in found if not key.startswith("diag.")}


@pytest.fixture(scope="module")
def keys() -> set[str]:
    found = sent_keys()
    # If the extraction ever stops matching, every assertion below would pass
    # on an empty set -- the exact failure this file exists to prevent.
    assert found, f"no text_key literal found in {SIDECAR}"
    return found


def test_the_shared_catalog_is_the_canonical_sentence_set(keys):
    tables = shared_tables()
    for locale, table in tables.items():
        missing = sorted(key for key in keys if key not in table)
        assert not missing, f"{locale}.json has no sentence for {missing}"


def test_the_shell_renders_the_shared_sentences(keys):
    shell = shell_tables()
    assert set(shell) == set(LOCALES_ON_DISK), sorted(shell)
    for locale, table in shared_tables().items():
        for key in sorted(keys):
            assert shell[locale].get(key) == table.get(key), f"{locale}: {key}"


def test_the_phone_resolves_every_key(keys):
    source = PHONE.read_text(encoding="utf-8")
    phone = set(re.findall(r"tk === '([^']+)'", source))
    missing = sorted(key for key in keys if key not in phone)
    assert not missing, f"mobile.html maps no message for {missing}"


def test_the_server_catalogue_has_every_key_in_both_locales(keys):
    for locale in LOCALES_ON_DISK:
        table = _EN if locale == "en" else LOCALES.get(locale, {})
        missing = sorted(key for key in keys if key not in table)
        assert not missing, f"internal/i18n {locale} has no sentence for {missing}"


def test_the_server_catalogue_has_every_sentence_the_sidecar_resolves_itself():
    keys = sidecar_resolved_keys()
    assert keys, "no key table or T() literal found in internal/"
    for locale in LOCALES_ON_DISK:
        table = _EN if locale == "en" else LOCALES.get(locale, {})
        missing = sorted(key for key in keys if key not in table)
        assert not missing, f"internal/i18n {locale} has no sentence for {missing}"


def test_the_two_server_catalogues_carry_the_same_keys():
    """``_EN`` and ``_ZH`` are the same set of sentences.

    One is the fallback for the other, so a key only English has is a Chinese
    reader's English sentence -- the degradation this file is about, one step
    earlier in the chain.  The wording differs, as it must; the key set does not.
    """
    zh = LOCALES.get("zh-CN", {})
    assert set(_EN) == set(zh), (
        f"english only: {sorted(set(_EN) - set(zh))}; "
        f"chinese only: {sorted(set(zh) - set(_EN))}"
    )


def test_the_web_catalogues_carry_every_diagnostics_sentence():
    keys = diag_keys()
    assert keys, "no diag.* literal found in internal/"
    for locale, table in shared_tables().items():
        missing = sorted(key for key in keys if key not in table)
        assert not missing, f"{locale}.json has no sentence for {missing}"


def test_the_two_web_catalogues_carry_the_same_keys():
    tables = shared_tables()
    en, zh = tables.get("en", {}), tables.get("zh-CN", {})
    assert set(en) == set(zh), (
        f"english only: {sorted(set(en) - set(zh))}; "
        f"chinese only: {sorted(set(zh) - set(en))}"
    )
