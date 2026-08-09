#!/usr/bin/env python3
"""Run one screening cycle and print the summary. No scheduler, no API.

    python scripts/run_once.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.logging_conf import configure_logging  # noqa: E402
from app.pipeline.ingest import run_cycle  # noqa: E402


async def main() -> None:
    c = get_settings()
    configure_logging(c.log_level)
    init_db()
    summary = await run_cycle()
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    for r in summary["results"]:
        print(f"  {r.get('state','?'):<10} {str(r.get('symbol') or '?'):<10} "
              f"score={r.get('score', 0):<6} {r.get('token','')[:14]}… {r.get('reason','')[:90]}")


if __name__ == "__main__":
    asyncio.run(main())
