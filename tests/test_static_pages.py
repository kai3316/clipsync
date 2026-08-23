"""Static-page interpolation regression tests.

The web server's ``_interpolate_html`` globally replaces placeholders such as
``__CLIPSYNC_I18N_LOCALE__`` with a JSON string value.  A page that uses that
placeholder as a JS *property name* (``window.__CLIPSYNC_I18N_LOCALE__``)
breaks into ``window."zh-CN"`` — a SyntaxError that aborts the whole inline
script and leaves the page showing only its static title (the "mobile page
shows nothing" bug).  These tests lock the correct pattern: the LHS uses the
non-interpolated ``__I18N_LOCALE__`` alias, the RHS carries the value.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "internal", "web", "static",
)

_PAGES = ("mobile.html", "quickpaste.html")


def _interpolate(html: str, locale: str = "zh-CN", token: str = "tok") -> str:
    """Replicate the server's _interpolate_html replacements for the pages
    under test (locale + token are the ones these pages carry)."""
    html = html.replace("__TOKEN__", token)
    html = html.replace("__CLIPSYNC_I18N_LOCALE__", json.dumps(locale))
    return html


def test_static_pages_interpolation_does_not_break_inline_js():
    for page in _PAGES:
        with open(os.path.join(_STATIC, page), encoding="utf-8") as f:
            served = _interpolate(f.read())
        # The value placeholder must not corrupt a property access into
        # `window."zh-CN"` (the "only a title" bug).
        assert 'window."' not in served, f"{page}: locale value leaked into a JS property name"
        # The LHS must survive interpolation so the locale actually lands.
        assert "window.__I18N_LOCALE__ = " in served, (
            f"{page}: locale property assignment is missing after interpolation"
        )


def test_static_pages_never_use_interpolated_placeholder_as_property():
    for page in _PAGES:
        with open(os.path.join(_STATIC, page), encoding="utf-8") as f:
            raw = f.read()
        # The only allowed use of __CLIPSYNC_I18N_LOCALE__ is as a bare VALUE
        # (RHS of an assignment / argument).  Property access is forbidden.
        assert "window.__CLIPSYNC_I18N_LOCALE__" not in raw, (
            f"{page}: interpolated placeholder used as a JS property name"
        )
        # The value placeholder must still appear (so the server injects it).
        assert "__CLIPSYNC_I18N_LOCALE__" in raw


def test_index_page_uses_correct_pattern_too():
    with open(os.path.join(_STATIC, "index.html"), encoding="utf-8") as f:
        raw = f.read()
    assert "window.__I18N_LOCALE__ = __CLIPSYNC_I18N_LOCALE__;" in raw
    assert 'window."' not in _interpolate(raw)
