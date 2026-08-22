"""Print the observed distribution of every screening metric, by token age.

Use it to see where a proposed threshold actually falls before setting it:

    python scripts/calibrate.py            # everything on record
    python scripts/calibrate.py --days 7   # only the last week
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import init_db, session_scope  # noqa: E402
from app.models import utcnow  # noqa: E402
from app.pipeline.cohort import collect, render  # noqa: E402
from app.pipeline.liquidity_floor import effective_min_liquidity  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.util.console import init_console, safe  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=None,
                    help="only use snapshots from the last N days")
    args = ap.parse_args()

    init_console()
    init_db()
    since = utcnow() - dt.timedelta(days=args.days) if args.days else None

    with session_scope() as session:
        cohorts = collect(session, since=since)

    print(safe(render(cohorts)))

    c = get_settings()
    entry, why = effective_min_liquidity(c)
    live, live_why = effective_min_liquidity(c, live=True)
    print()
    print(safe("Current floors, for comparison with the columns above:"))
    print(safe(f"  liquidity (entry) ${entry:,.0f}  <- {why}"))
    print(safe(f"  liquidity (live)  ${live:,.0f}  <- {live_why}"))
    print(safe(f"  unique holders    {c.min_unique_holders}  <- fixed constant, not derived"))
    print(safe(f"  24h volume        ${c.min_volume_24h_usd:,.0f}  <- fixed constant, not derived"))
    print()
    print(safe("A percentile is not a safety standard: the bottom decile of a bad "
               "population is still bad. Read these to see where a number falls, "
               "then decide."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
