"""The in-place updater: the manifest a client reads, and who may sign it.

`tauri-plugin-updater` compares the running version against `latest.json` on the
newest release, matches this machine's bundle to a key in it, downloads the file
that key names and checks it against the signature beside it.  Four things can
go wrong that no test on this side would otherwise show, because they only ever
appear on a user's machine:

* a platform with no key -- the client reports "not installable" forever;
* a key naming an artifact that is not on the release, or one whose `.sig` was
  never produced -- an unsigned update, which the plugin refuses;
* a version field carrying the tag's `v` -- semver rejects it, so every client's
  check fails;
* a manifest signed by a key that does not match the public key baked into the
  binaries -- a signature that can never verify.

The assembler lives inside a workflow heredoc, so these cases lift it out and
run it rather than matching its text: a rewrite that kept the strings and broke
the behaviour would pass a text check.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DESKTOP = WORKFLOWS / "desktop.yml"
BASE_URL = "https://github.com/kai3316/clipsync/releases/download/v1.0.3"

# The bundle each leg produces, at the basename `upload-artifact` preserves.
ARTIFACTS = {
    "bundles/clipsync-desktop-windows/nsis/ClipSync_1.0.3_x64-setup.exe": "sig-win",
    "bundles/clipsync-desktop-macos/macos/ClipSync.app.tar.gz": "sig-mac",
    "bundles/clipsync-desktop-linux/appimage/ClipSync_1.0.3_amd64.AppImage": "sig-appimage",
    "bundles/clipsync-desktop-linux/deb/ClipSync_1.0.3_amd64.deb": "sig-deb",
}

# Every key the plugin can ask for, given the installers these legs build:
# `{os}-{arch}-{installer}` first, then `{os}-{arch}`.  Linux needs both of its
# own because a .deb and an AppImage are different payloads -- one bare
# `linux-x86_64` entry could only ever name one of them, and a .deb client
# handed AppImage bytes fails the installer's own format check.
EXPECTED_KEYS = {
    "windows-x86_64-nsis",
    "windows-x86_64",
    "darwin-aarch64-app",
    "darwin-aarch64",
    "linux-x86_64-appimage",
    "linux-x86_64-deb",
    "linux-x86_64",
}


def workflow_text(name="desktop.yml"):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def manifest_script():
    """The manifest assembler, lifted out of its heredoc in desktop.yml.

    Dedented by the block's own indentation rather than by a fixed count, so
    reindenting the step does not silently break these cases.
    """
    text = workflow_text()
    step = text.index("Write the update manifest")
    start = text.index("<<'PY'", step) + len("<<'PY'")
    end = text.index("\n          PY", start)
    body = text[start:end]
    lines = body.split("\n")
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    assert indents, "the manifest heredoc is empty"
    cut = min(indents)
    return "\n".join(line[cut:] if line.strip() else "" for line in lines)


def run_manifest(tmp_path, drop=(), unsign=(), version="1.0.3"):
    """Build a fake artifact tree, run the real assembler, return its result."""
    for name, signature in ARTIFACTS.items():
        if name in drop:
            continue
        artifact = tmp_path / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("payload", encoding="utf-8")
        if name not in unsign:
            Path(f"{artifact}.sig").write_text(signature + "\n", encoding="utf-8")

    script = tmp_path / "assemble.py"
    script.write_text(manifest_script(), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), version, BASE_URL],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_the_manifest_names_every_key_the_plugin_can_ask_for(tmp_path):
    result = run_manifest(tmp_path)
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)

    assert set(manifest["platforms"]) == EXPECTED_KEYS
    # The version is semver, so the tag's `v` must not survive into it --
    # every client's check fails on a version their semver parser rejects.
    assert manifest["version"] == "1.0.3"
    for key, entry in manifest["platforms"].items():
        assert entry["url"].startswith(BASE_URL + "/"), key
        assert entry["signature"].startswith("sig-"), key


def test_each_key_names_the_payload_its_installer_can_actually_install(tmp_path):
    """A key pointing at the wrong format installs nothing but an error.

    This is the whole reason the Linux pair exists, so it is checked by name
    rather than by shape.
    """
    result = run_manifest(tmp_path)
    assert result.returncode == 0, result.stderr
    platforms = json.loads(result.stdout)["platforms"]

    assert platforms["windows-x86_64-nsis"]["url"].endswith("-setup.exe")
    assert platforms["darwin-aarch64-app"]["url"].endswith(".app.tar.gz")
    assert platforms["linux-x86_64-appimage"]["url"].endswith(".AppImage")
    assert platforms["linux-x86_64-deb"]["url"].endswith(".deb")
    # The fallback for a machine whose bundle type the plugin could not read
    # installs an AppImage, so that is what it must name.
    assert platforms["linux-x86_64"]["url"].endswith(".AppImage")


def test_the_manifest_matches_the_macos_bundle_whatever_it_is_called(tmp_path):
    """The `.app.tar.gz` name is assembled by the bundler and has varied.

    At least one project has shipped it as `<productName>_<arch>.app.tar.gz`
    rather than `<productName>.app.tar.gz`, so the assembler matches on the
    suffix and the manifest has to carry whatever the file is really called --
    a URL naming a file that is not on the release is a 404 on every update.
    """
    original = tmp_path / "bundles/clipsync-desktop-macos/macos/ClipSync.app.tar.gz"
    renamed = original.with_name("ClipSync_1.0.3_aarch64.app.tar.gz")

    # Build the full tree, then swap the macOS artifact for the alternate name.
    for name, signature in ARTIFACTS.items():
        artifact = tmp_path / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("payload", encoding="utf-8")
        Path(f"{artifact}.sig").write_text(signature + "\n", encoding="utf-8")
    original.rename(renamed)
    Path(f"{original}.sig").rename(Path(f"{renamed}.sig"))

    script = tmp_path / "assemble.py"
    script.write_text(manifest_script(), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), "1.0.3", BASE_URL],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    platforms = json.loads(result.stdout)["platforms"]
    assert platforms["darwin-aarch64-app"]["url"].endswith("ClipSync_1.0.3_aarch64.app.tar.gz")
    assert platforms["darwin-aarch64-app"]["signature"] == "sig-mac"


def test_a_missing_platform_stops_the_manifest_rather_than_shipping_it(tmp_path):
    dropped = "bundles/clipsync-desktop-linux/deb/ClipSync_1.0.3_amd64.deb"
    result = run_manifest(tmp_path, drop={dropped})
    assert result.returncode != 0
    assert "linux-x86_64-deb" in result.stderr
    assert not result.stdout.strip()


def test_an_unsigned_artifact_is_never_published(tmp_path):
    """The signature is the only thing standing between a user and a bad update."""
    unsigned = "bundles/clipsync-desktop-windows/nsis/ClipSync_1.0.3_x64-setup.exe"
    result = run_manifest(tmp_path, unsign={unsigned})
    assert result.returncode != 0
    assert "ClipSync_1.0.3_x64-setup.exe" in result.stderr
    assert not result.stdout.strip()


def test_the_manifest_is_published_with_the_release(tmp_path):
    text = workflow_text()
    assert "latest.json" in text
    # Something has to *create* it.  The assembler prints the manifest on
    # stdout -- the cases above run it that way -- so only the shell redirect
    # turns that into the file the upload reads.  Asserting the upload alone is
    # what let 1.0.4 publish five installers and no manifest: `cat` failed,
    # `set -e` ended the step, and nothing on the release page said the app
    # would never be offered an update.
    #
    # Read off the invocation itself rather than searched for anywhere in the
    # file: the comment above this line quotes the redirect, so a loose match
    # would be satisfied by prose describing the thing it is meant to require.
    invocation = next(
        line for line in text.splitlines() if "python3" in line and "<<'PY'" in line
    )
    assert "> latest.json" in invocation, f"nothing writes latest.json: {invocation!r}"
    # It has to reach the release page, or the endpoint the app reads 404s.
    assert re.search(r"gh release upload .*latest\.json", text), "latest.json is never uploaded"


def test_the_updater_payloads_are_collected_and_gated_on_a_tag():
    text = workflow_text()
    upload = text[text.index("Upload the updater payloads") :]
    upload = upload[: upload.index("- name:", 10)]
    # Without the .sig there is nothing to sign the manifest with; without the
    # .app.tar.gz the macOS update has no payload.
    assert "**/*.sig" in upload
    assert "**/*.app.tar.gz" in upload
    # A pull request from a fork gets no secrets, so it must not collect these.
    assert "refs/tags/v" in upload


def test_signing_is_requested_only_where_a_key_can_exist():
    """`createUpdaterArtifacts` with no private key is a hard bundler error.

    tauri.conf.json commits the public key, so asking to sign without a key is
    not a no-op -- it fails the build.  CI gates it on the tag; the local
    script gates it on the environment.
    """
    desktop = workflow_text()
    assert "createUpdaterArtifacts" in desktop
    config_step = desktop[desktop.index("Write the packaging config") :]
    config_step = config_step[: config_step.index("- name:", 10)]
    assert "refs/tags/v" in config_step

    local = (ROOT / "scripts" / "build-tauri.ps1").read_text(encoding="utf-8")
    assert "createUpdaterArtifacts" in local
    assert "TAURI_SIGNING_PRIVATE_KEY" in local


def test_the_release_attaches_the_macos_payload_but_not_loose_signatures():
    text = workflow_text()
    step = text[text.index("Attach the desktop installers to the release") :]
    case_block = step[step.index('case "$f" in') :]
    case_block = case_block[: case_block.index("esac")]

    assert "*.app.tar.gz" in case_block, "the macOS update payload is never attached"
    # The signatures travel inside latest.json; loose copies on the release
    # page would only be one more thing to get out of step with it.
    assert "*.sig" not in case_block, "loose .sig files would clutter the release"


def test_the_public_key_and_endpoint_are_committed_and_agree_with_the_sidecar():
    """A manifest signed by a different key can never verify.

    The public key has to be in the binary at build time, so it is committed,
    and the endpoint has to name the same repository the sidecar checks -- two
    update paths that disagree about where releases live is a bug that only
    shows up on a user's machine.
    """
    config_path = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    updater = config["plugins"]["updater"]
    assert updater["pubkey"].strip(), "the updater has no public key to verify against"
    assert updater["windows"]["installMode"] in {"passive", "basicUi", "quiet"}

    source = (ROOT / "internal" / "system" / "updater.py").read_text(encoding="utf-8")
    match = re.search(r'_GITHUB_REPO\s*=\s*"([^"]+)"', source)
    assert match, "the sidecar's release repository moved"
    assert any(match.group(1) in endpoint for endpoint in updater["endpoints"]), (
        "the updater endpoint and the sidecar's repository disagree"
    )


def test_the_installing_phase_is_not_owned_by_the_sidecar():
    """Two writers, one state object -- the sidecar must not be able to undo it.

    The sidecar only ever reports idle/downloading/ready/failed, so opening
    settings mid-install would otherwise reset the card and put the download
    button back while the bundle is being replaced.
    """
    store = (ROOT / "desktop" / "src" / "stores" / "application.ts").read_text(encoding="utf-8")
    hydrate = store[store.index("async loadUpdateStatus") :]
    hydrate = hydrate[: hydrate.index("async checkUpdate")]
    assert 'phase !== "installing"' in hydrate, (
        "loadUpdateStatus can clobber an install in flight"
    )


@pytest.mark.parametrize("path", ["desktop/src/App.vue", "desktop/src/api/types.ts"])
def test_the_install_phase_is_known_to_the_client(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert "installing" in text, f"{path} does not know about the installing phase"
