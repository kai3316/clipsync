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
src/main.py              # Application entry point
internal/
  clipboard/             # Platform-specific clipboard I/O
  config/                # JSON config persistence
  i18n/                  # Internationalization (EN, ZH)
  platform/              # OS integration (autostart, notifications)
  protocol/              # Wire format encoding/decoding
  security/              # Encryption, pairing, identity
  sync/                  # Sync orchestration and file transfer
  transport/             # TLS connections and mDNS discovery
  ui/                    # Dashboard (5 panels), settings, system tray
  web/                   # Built-in HTTP server + mobile PWA
tests/                   # pytest test suite
```

## Adding a new feature

- Core logic goes in the appropriate `internal/` subpackage
- UI for the feature goes in `internal/ui/dashboard.py` or `settings_window.py`
- Config fields are defined in `internal/config/config.py`
- Add tests in the `tests/` directory
- Wire everything together in `src/main.py`

## Reporting issues

Use the GitHub Issues tracker. Include:
- Your OS and Python version
- Steps to reproduce
- Expected vs actual behavior
- Any relevant log output (Settings → Logs → Export)
