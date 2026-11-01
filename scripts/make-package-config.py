"""Generate the bundle config the way CI does, so a local build matches a released one.

`desktop/src-tauri/tauri.conf.json` has no version that tracks the application -- it
says `0.1.0` while `internal/version.py` says 1.0.45 -- and CI never uses it alone.  The
Windows job writes `build/tauri-package.json` with the real version and the sidecar staged
as a resource directory, then builds with `--config` pointing at it.  Building without that
gives `ClipSync_0.1.0_x64-setup.exe` and no sidecar, which is what a bare `npx tauri build`
produced here.

`scripts/tauri.mjs` is the standard `npm run tauri` entry: it runs this script
for a `build` and forwards the generated `--config`, so the same override CI
writes is what a local release build uses too.  `--shape auto` (the default)
picks the onedir layout on macOS and wherever ``CLIPSYNC_SIDECAR_ONEDIR`` is
set, and the onefile layout otherwise.

# Why it refuses a sidecar older than the source

`desktop/src-tauri/sidecar/` is gitignored, so it is whatever the last local build left
there -- and a build staged on one day will happily package sources written on another.
That is not a cosmetic problem: **the version the window displays comes from the sidecar,
not from the bundle**, so a stale sidecar makes a current application report an old
version.  Measured here: a 1.0.45 host displayed "版本 1.0.33", because the sidecar beside
it had been built two weeks earlier while `internal/version.py` said 1.0.33.

The staleness is checkable without running anything: PyInstaller compresses its archive, so
the version cannot be read out of the file, but the file's mtime against the sources' is
exact.  Anything newer than the binary is a source change the binary does not have, and the
message says which -- the version file being the case that bit.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from internal.version import __version__  # noqa: E402

# Every tree the sidecar's behaviour comes from.  The legacy application under `src/` is
# not part of it, but its files share the version module, and rebuilding when unsure costs
# a PyInstaller run rather than a wrong release.
SOURCE_TREES = ("internal", "src")
SIDECAR = ROOT / "desktop" / "src-tauri" / "sidecar" / "clipsync-sidecar.exe"
NEWER_ALLOWANCE_SECONDS = 1.0


def newer_sources(binary: Path) -> list[Path]:
    """Source files modified after `binary` was built."""
    if not binary.is_file():
        return []
    built = binary.stat().st_mtime
    found: list[Path] = []
    for tree in SOURCE_TREES:
        for path in (ROOT / tree).rglob("*.py"):
            try:
                if path.stat().st_mtime > built + NEWER_ALLOWANCE_SECONDS:
                    found.append(path)
            except OSError:
                continue
    return sorted(found)


def is_onedir(shape: str) -> bool:
    """Whether the sidecar is staged as a directory rather than one executable.

    ``auto`` is what the standard entry point uses: macOS always builds the
    directory (the spec forces it), Windows/Linux follow the same
    ``CLIPSYNC_SIDECAR_ONEDIR`` switch ``scripts/build-sidecar.ps1`` sets.  The
    explicit shapes exist so a caller that has already staged one kind cannot
    be handed a config for the other.
    """
    if shape == "onedir":
        return True
    if shape == "onefile":
        return False
    marker = os.environ.get("CLIPSYNC_SIDECAR_ONEDIR", "").strip().lower()
    return sys.platform == "darwin" or marker not in ("", "0", "false")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--shape",
        choices=("auto", "onedir", "onefile"),
        default="auto",
        help="which sidecar layout the bundle config should name (default: auto)",
    )
    args = parser.parse_args(argv)

    # A onedir sidecar is staged beside the host as a bundle resource; a
    # onefile is staged under `binaries/` for `externalBin`, which is the shape
    # `scripts/build-sidecar.ps1` writes when `-OneDir` was not asked for.
    onedir = is_onedir(args.shape)
    bundle = (
        {"resources": ["sidecar"]}
        if onedir
        else {"externalBin": ["binaries/clipsync-sidecar"]}
    )

    stale = newer_sources(SIDECAR) if onedir else []
    if stale:
        print(
            f"the staged sidecar is older than the sources it would be packaged with:\n"
            f"  {SIDECAR}\n"
            f"  built {SIDECAR.stat().st_mtime:.0f}, and these are newer:",
            file=sys.stderr,
        )
        for path in stale[:10]:
            print(f"    {path.relative_to(ROOT)}", file=sys.stderr)
        if len(stale) > 10:
            print(f"    ... and {len(stale) - 10} more", file=sys.stderr)
        print(
            "\nRebuild it first, or the window will report the version the sidecar was\n"
            "built at rather than the one being packaged:\n"
            "  powershell -File scripts/build-sidecar.ps1 -OneDir",
            file=sys.stderr,
        )
        return 1

    out = ROOT / "build"
    out.mkdir(exist_ok=True)
    config = out / "tauri-package.json"
    config.write_text(
        json.dumps({"version": __version__, "bundle": bundle}, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {config}")
    print(config.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
