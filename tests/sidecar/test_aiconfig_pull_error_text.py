"""Every refusal the shell puts into words is a refusal the sidecar can send.

`pullAiRemote` shows the codes a pull came back with, and one of them is
translated rather than printed (`legacy_peer_read_only`), because the raw form
is a name and the reader has just spent a selection on it.  A translation keyed
on a string that no longer exists is worse than no translation at all: it is a
branch that can never run, and the code it was written for would print raw
again with nothing to say so.

So the shell's keys are held to the sidecar's literals, the way
`test_chat_system_keys.py` holds the fronts to the notice keys.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / "desktop" / "src" / "App.vue"
AI_CONFIG = ROOT / "internal" / "sync" / "ai_config.py"


def translated_codes() -> list[str]:
    """The codes `aiPullErrorText` translates, read off its own branches."""
    source = APP.read_text(encoding="utf-8")
    body = re.search(r"function aiPullErrorText\(code: string\): string \{(.*?)\n\}", source, re.S)
    assert body, "aiPullErrorText is gone; the codes below are what it translated"
    return re.findall(r'code === "([^"]+)"', body.group(1))


def test_the_shell_translates_at_least_one_code():
    """The check below passes on an empty list, which is the state it exists to
    catch: a rename that leaves the map reading nothing."""
    assert translated_codes(), "no code is translated any more"


def test_every_translated_code_is_one_the_sidecar_sends():
    sidecar = AI_CONFIG.read_text(encoding="utf-8")
    unknown = [code for code in translated_codes() if f'"{code}"' not in sidecar]
    assert unknown == [], f"the shell words a refusal the sidecar never sends: {unknown}"
