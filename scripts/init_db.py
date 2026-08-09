#!/usr/bin/env python3
"""Create the SQLite schema. Idempotent."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.logging_conf import configure_logging  # noqa: E402


def main() -> None:
    c = get_settings()
    configure_logging(c.log_level)
    init_db()
    print(f"schema ready at {c.database_url}")


if __name__ == "__main__":
    main()
