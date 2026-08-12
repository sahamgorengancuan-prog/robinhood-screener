from __future__ import annotations

import logging
import sys

from app.util.console import init_console


def configure_logging(level: str = "INFO") -> None:
    # Must run before the first log record: on a default Windows console,
    # logging a non-ASCII character otherwise raises UnicodeEncodeError.
    init_console()

    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)-28s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
