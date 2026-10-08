"""The sidecar's download leaves the release signature beside the asset.

Reported as the Windows machine opening a folder instead of installing a peer-sent update:
`install_staged_update` requires `peer_verified` **and** a non-empty signature, or it calls
`reveal_staged_update` -- `update.open_folder`, the Explorer window the reader saw.  Two things
cache an asset and only one of them kept the signature:

    shell   `keep_downloaded_installer` -> `update.cache_asset` with a signature   (has one)
    sidecar `update_service._finish`    -> `updater.cache_asset` with none

so a machine that took the sidecar's own download path served installer bytes with no signature,
and
every peer that received them was sent to a manual install.

The manifest lookup is tested against a stand-in rather than the network: the live fetch is a
handshake to github.com, which answered a timeout while this was written, and a test that needs the
network is a test that fails for reasons other than the code.
"""

from __future__ import annotations

import io
import json

import pytest

from internal.system import updater

MANIFEST = {
    "version": "1.0.66",
    "platforms": {
        "windows-x86_64-nsis": {
            "url": "https://github.com/o/r/releases/download/v1.0.66/ClipSync_1.0.66_x64-setup.exe",
            "signature": "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZQo=",
        },
        "darwin-aarch64-app": {
            "url": "https://github.com/o/r/releases/download/v1.0.66/ClipSync.app.tar.gz",
            "signature": "bWFjb3Mtc2ln",
        },
    },
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def manifest(monkeypatch):
    def fake_urlopen(_request, timeout=0, context=None):
        return FakeResponse(json.dumps(MANIFEST).encode("utf-8"))

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)


def test_the_signature_is_found_by_the_name_the_url_ends_in(manifest):
    """The lookup matches the URL's basename, not a rebuilt platform key.

    The key carries the bundle target (`windows-x86_64-nsis`, `darwin-aarch64-app`), which is a
    mapping this module would otherwise have to keep a second copy of.
    """
    assert updater._manifest_signature_for("ClipSync_1.0.66_x64-setup.exe").startswith("dW50")
    assert updater._manifest_signature_for("ClipSync.app.tar.gz") == "bWFjb3Mtc2ln"


def test_an_asset_the_manifest_does_not_name_answers_nothing(manifest):
    """A miss is not an error: no signature is the behaviour that came before this."""
    assert updater._manifest_signature_for("ClipSync_9.9.9_x64-setup.exe") == ""
    assert updater._manifest_signature_for("") == ""


def test_a_manifest_that_cannot_be_read_answers_nothing(monkeypatch):
    """Offline is the normal case for the feature this signature exists for: it must not raise."""

    def boom(*_args, **_kwargs):
        raise OSError("network is unreachable")

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    assert updater._manifest_signature_for("ClipSync_1.0.66_x64-setup.exe") == ""


def test_a_download_leaves_the_signature_in_the_cache(manifest, tmp_path, monkeypatch):
    """The point of the lookup: after a download, `get_cached_signature` answers.

    Driven through `download_latest_release` rather than the helper, because the claim is about what
    a download leaves behind.
    """
    asset = b"installer-bytes"
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        updater,
        "_fetch_latest_release",
        lambda timeout=0.0, failure=None: {
            "tag_name": "v1.0.66",
            "assets": [
                {
                    "name": "ClipSync_1.0.66_x64-setup.exe",
                    "browser_download_url": "https://example.invalid/ClipSync_1.0.66_x64-setup.exe",
                    "size": len(asset),
                    "digest": "sha256:" + __import__("hashlib").sha256(asset).hexdigest(),
                }
            ],
        },
    )
    monkeypatch.setattr(updater, "_asset_matchers", lambda: [
        __import__("re").compile(r"^ClipSync_.*_x64-setup\.exe$")
    ])

    def fake_urlopen(request, timeout=0, context=None):
        # Two requests arrive: the asset, and the manifest this looks up for the signature.  An
        # earlier version returned the asset bytes for both -- which overrode the fixture above --
        # so the manifest was not JSON, the lookup answered "", and the test read as a broken fix
        # rather than a broken stand-in.
        url = getattr(request, "full_url", str(request))
        if url.endswith("latest.json"):
            return FakeResponse(json.dumps(MANIFEST).encode("utf-8"))
        return FakeResponse(asset)

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    path, reason, version = updater.download_latest_release(str(tmp_path / "dl"))
    assert reason is None and path, f"the download itself failed: {reason}"
    assert version == "v1.0.66"
    cached = updater.get_cached_signature("ClipSync_1.0.66_x64-setup.exe")
    assert cached.startswith("dW50"), (
        "a download must leave the signature behind, or its cache cannot be served to a peer that "
        "has no route to the release server"
    )
