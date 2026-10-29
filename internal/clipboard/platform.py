"""Platform-specific clipboard factory."""

import logging
import platform

logger = logging.getLogger(__name__)

_system = platform.system()

if _system == "Windows":
    from internal.clipboard.clipboard_windows import create_monitor as _create_monitor
    from internal.clipboard.clipboard_windows import (  # noqa: F401
        create_reader,
        create_writer,
    )
elif _system == "Darwin":
    from internal.clipboard.clipboard_darwin import create_monitor as _create_monitor
    from internal.clipboard.clipboard_darwin import (  # noqa: F401
        create_reader,
        create_writer,
    )
elif _system == "Linux":
    from internal.clipboard.clipboard_linux import create_monitor as _create_monitor
    from internal.clipboard.clipboard_linux import (  # noqa: F401
        create_reader,
        create_writer,
    )
else:
    raise RuntimeError(f"Unsupported platform: {_system}")


def create_monitor(poll_interval: float = 0.4):
    return _create_monitor(poll_interval=poll_interval)


logger.info("Clipboard backend: %s", _system)
