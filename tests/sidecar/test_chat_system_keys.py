"""Every system notice the sidecar can send has a sentence on every front.

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
shell is compared to them verbatim, and the panel and the phone are checked for
coverage (their wording is their own -- the panel is legacy, the phone's inline
dictionary is only a fallback for a payload with no resolvable key).
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from internal.i18n import _EN, LOCALES

ROOT = pathlib.Path(__file__).resolve().parents[2]

SIDECAR = ROOT / "internal" / "sync" / "nearby_chat.py"
SHARED = ROOT / "internal" / "web" / "static" / "locales"
SHELL = ROOT / "desktop" / "src" / "i18n" / "chat.ts"
PHONE = ROOT / "internal" / "web" / "static" / "mobile.html"

LOCALES_ON_DISK = ("zh-CN", "en")


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


def test_the_panel_has_every_key_in_both_locales(keys):
    for locale in LOCALES_ON_DISK:
        table = _EN if locale == "en" else LOCALES.get(locale, {})
        missing = sorted(key for key in keys if key not in table)
        assert not missing, f"internal/i18n {locale} has no sentence for {missing}"
