"""Python business process for the Tauri desktop host."""

import argparse
import logging
import sys

from internal.adapters.sidecar.rpc import RpcServer, encode_frame
from internal.application.bootstrap import SidecarApplication
from internal.application.errors import ApplicationError


def _fatal(error: dict) -> None:
    try:
        sys.stdout.buffer.write(encode_frame({"type": "fatal", "error": error}))
        sys.stdout.buffer.flush()
    except (OSError, ValueError):
        # The parent may already have closed its pipe during startup or exit.
        pass


def _shutdown(app) -> bool:
    for _ in range(2):
        try:
            if app.lifecycle.stop() is not False:
                return True
        except Exception:
            logging.error("Sidecar resource cleanup failed")
    logging.error("Sidecar cleanup incomplete after bounded retries")
    return False


def _recover() -> int:
    """Move damaged data aside, once, and report what moved.

    A separate mode rather than an RPC method because the damage this repairs is
    exactly what stops the sidecar from starting, so there is no session to
    carry the request.  Emits one frame and exits: `fatal` when the pass itself
    failed, otherwise `recovered` with one entry per artifact that moved — an
    empty list meaning the directory was already usable.
    """
    from internal.data.recovery import quarantine

    try:
        items = quarantine()
    except Exception:
        logging.error("Data recovery failed", exc_info=True)
        _fatal({
            "code": "RECOVERY_FAILED",
            "message": "Could not repair the data directory",
            "retryable": False,
        })
        return 2
    sys.stdout.buffer.write(encode_frame({"type": "recovered", "items": items}))
    sys.stdout.buffer.flush()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--history-only", action="store_true",
        help="Isolated history/IPC verification without network or clipboard monitoring",
    )
    parser.add_argument(
        "--recover", action="store_true",
        help="Move damaged configuration, identity or history aside, then exit",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    if args.recover:
        return _recover()
    app = None
    result = 0
    try:
        runtime_factory = None
        if not args.history_only:
            from internal.infrastructure.runtime.lan import LanRuntime

            runtime_factory = LanRuntime
        app = SidecarApplication(runtime_factory=runtime_factory)
        app.lifecycle.start()
        result = RpcServer(app, sys.stdin.buffer, sys.stdout.buffer).serve()
    except ApplicationError as exc:
        _fatal(exc.as_dict())
        result = 2
    except (BrokenPipeError, KeyboardInterrupt):
        result = 0
    except Exception:
        logging.error("Sidecar startup failed")
        _fatal({
            "code": "STARTUP_FAILED", "message": "Could not start ClipSync", "retryable": False,
        })
        result = 2
    finally:
        if app is not None and not _shutdown(app):
            result = 3
    return result


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
