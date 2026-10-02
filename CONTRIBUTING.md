# Contributing to ClipSync

## Setup

```bash
git clone https://github.com/kai3316/clipsync.git
cd clipsync
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

## Development workflow

1. Create a feature branch: `git checkout -b feat/my-feature`
2. Make your changes
3. Run linting: `ruff check .`
4. Run tests: `python -m pytest tests/ -v`
5. If you touched the desktop app, also run its type check and tests from
   `desktop/`: `npm run typecheck && npm test`. `npm test` runs vitest, which
   *strips* types rather than checking them, so a change to the shape of an RPC
   reply can pass every test there and still fail the release build —
   `npm run typecheck` is the only thing that checks it.
6. Commit with a descriptive message
7. Submit a pull request

## Code style

- Python 3.12+ with type hints where practical
- 4-space indentation, 100-character line limit
- Follow existing patterns in the codebase
- Keep changes focused — one PR, one purpose

## Project structure

```
desktop/                 # The desktop application (Rust + Tauri 2, Vue 3)
  src/                   #   its interface; api/bridge.ts is the only invoke site
  src-tauri/             #   the native host, which spawns the sidecar below
src/sidecar_main.py      # The Python service that host runs
src/main.py              # The previous Tk application's entry point
internal/                # Business logic, shared by every front end
  adapters/sidecar/      #   the IPC method table (rpc.py) and its favourites rows
  application/           #   lifecycle, bootstrap, use cases, event journal
  clipboard/             #   platform clipboard I/O, history store, dedup, filtering
  config/                #   JSON config persistence, and the one field-rules table
  data/                  #   export, backup, recovery, log files
  diagnostics/           #   the seven report groups
  i18n/                  #   strings for the Python front ends
  infrastructure/        #   runtime wiring: LAN runtime, companion, persistence
  platform/              #   OS integration (autostart, notifications, processes)
  protocol/              #   wire format encoding/decoding
  security/              #   encryption, pairing, identity
  sync/                  #   sync orchestration, file transfer, chat, AI config
  system/                #   updater, QR, archive, file manager, hotkeys (Tk only)
  transport/             #   TLS connections, mDNS discovery, internet relay
  ui/                    #   the previous Tk interface (not in the desktop bundle)
  web/                   #   HTTP server, REST API and the mobile companion page
contracts/               # The IPC table and legacy surface map, held by tests
tests/                   # pytest suite (tests/sidecar/ for the IPC layer)
```

## Adding a new feature

- Core logic goes in the appropriate `internal/` subpackage
- A new capability is registered in `internal/adapters/sidecar/rpc.py` and
  written into `contracts/rpc-v1.md`; the Tauri command beside it goes in
  `desktop/src-tauri/src/main.rs` and `build.rs`. `tests/sidecar/test_rpc_contract.py`
  fails if the two disagree
- Desktop interface work goes in `desktop/src/` — the store in
  `stores/application.ts`, the bridge wrapper in `api/bridge.ts`
- The previous Tk interface is `internal/ui/dashboard.py` and
  `settings_window.py`, wired in `src/main.py`. It is maintenance-only; new
  features belong in the desktop application
- Config fields are defined in `internal/config/config.py`, in the `FIELD_RULES`
  table that the loader, the backup restore and the web settings API all read
- Add tests in the `tests/` directory

## Reporting issues

Use the GitHub Issues tracker. Include:
- Your OS and Python version
- Steps to reproduce
- Expected vs actual behavior
- Any relevant log output (Settings → Logs → Export)
