# ClipSync Tauri Desktop

Implementation in progress. The native host, history, identity and initial LAN
runtime are connected; full functionality and platform parity remain unfinished.
Do not replace a production installation with this build.

## Double-Click Startup (Windows)

Double-click `Start-ClipSync.bat` in the repository root. It invokes
`scripts/start-desktop.ps1`, locates Node/Rust/Python, installs missing locked
frontend dependencies, incrementally builds the host and opens the native window.
It starts the new desktop, never the legacy `run.bat` entry. Missing Python
dependencies or build prerequisites produce an error instead of a silent exit.

The first build can take several minutes. The launcher console remains open
while the desktop runs; closing the desktop normally stops its local Vite server.
Only one launcher per checkout builds at a time. A second launch focuses the
running native window when available.

Data defaults to `.tauri-dev-data`, separate from the old installation. An
explicit absolute `CLIPSYNC_CONFIG_DIR` overrides it; `CLIPSYNC_PYTHON` overrides
Python discovery. The launcher chooses a free loopback port from 1420 through
1470. Source-only diagnostics, without starting the app:

```powershell
.\Start-ClipSync.bat -CheckOnly
```

To verify the full launcher from an unrelated working directory, including
occupied-port fallback and native IPC, run from this directory:

```powershell
$env:CLIPSYNC_SMOKE_LAUNCHER = "1"
node scripts/smoke-native.mjs
```

Close an existing launcher before this full test. The per-checkout launcher lock
intentionally prevents the test from taking over an already running user window.
`-CheckOnly` is safe while that window remains open.

## Development

From this directory:

```text
npm ci
npm run tauri -- dev
```

Requirements: Node 22+, Rust stable, platform Tauri native prerequisites, and a
Python environment with the repository dependencies installed. Set
`CLIPSYNC_PYTHON` to the Python executable if it is not on PATH. Development data
defaults to the repository's ignored `.tauri-dev-data` directory. Do not point a
development build at production data unless you intend to modify that history.

The web preview at `http://127.0.0.1:1420` intentionally has no business access.
Use the native Tauri window for real commands.

## Validation

```text
npm run build
npm test
npm run test:e2e
cargo test --manifest-path src-tauri/Cargo.toml --locked
python -m pytest tests/sidecar -q
```

Run Python tests from the repository root. Browser E2E currently uses installed
Microsoft Edge. With the debug binary built, Windows native E2E:

```text
node scripts/smoke-native.mjs
```

This starts/stops its own Vite process when needed and launches a real WebView2
window with a temporary debugging endpoint, only for the test process. It creates
synthetic data in an isolated temporary directory with sync paused,
checks actual IPC/mutations/permission denial/exit and leaves screenshots under
`test-results/`. Production builds must not enable that debugging endpoint.

From the repository root, build the Python executable and verify its real stdio:

```text
pwsh -File scripts/build-sidecar.ps1 -SelfTest
```

The packaged IPC smoke uses `--history-only` to avoid starting network discovery
or clipboard monitoring. Normal desktop startup enables the LAN runtime.

`scripts/build-tauri.ps1` additionally stages the target-named sidecar and builds
the desktop package. Its verification is not a claim of code signing, installer
upgrade/rollback testing, or platform release readiness.
