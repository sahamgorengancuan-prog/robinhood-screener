#!/usr/bin/env python3
"""Connectivity + API contract probe. Run this FIRST, before trusting any output.

Terminal front-end for `app.diagnostics` — the same checks the Gradio UI runs,
so the two can never disagree about whether a source works.

It answers the questions this repo cannot answer offline:

  * Does the Node RPC respond, and what is the real chain ID?
  * Is the Data API reachable, and what keys does it actually return?
  * Does OKX accept our signature, and which of our fields actually parse?
  * Is the explorer serving verification status?

Nothing here is inferred — it reports only what the APIs returned.

    python scripts/probe_endpoints.py [0xTokenAddress] [--json]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.diagnostics import (  # noqa: E402
    FAIL,
    OK,
    SKIP,
    WARN,
    readiness,
    run_all_checks,
    summarize_checks,
)
from app.logging_conf import configure_logging  # noqa: E402
from app.util.console import bold, dim, init_console, safe  # noqa: E402


async def main(address: str | None, as_json: bool) -> None:
    c = get_settings()
    init_console()
    configure_logging("ERROR" if as_json else c.log_level)
    init_db()

    results = await run_all_checks(address)

    if as_json:
        print(json.dumps([r.as_dict() for r in results], indent=2, default=str))
        return

    print("\n" + bold("=" * 78))
    print(bold("  CONNECTION & API PROBE"))
    print(dim(f"  run_mode={c.run_mode}  okx_simulated={c.okx_simulated}"
              f"  rh_data_enabled={c.rh_data_enabled}"))
    print(bold("=" * 78) + "\n")

    group = None
    for r in results:
        if r.group != group:
            group = r.group
            print("\n" + bold(group))
        lat = f"{r.latency_ms:>6.0f}ms" if r.latency_ms is not None else "      -"
        print(safe(f"  {r.icon} {r.status:<5} {lat}  {r.name:<22} {r.summary}"))
        if r.fix:
            print(dim(safe(f"          -> {r.fix}")))

    # Observed field names are the whole point for the unverified endpoints.
    print("\n" + bold("-" * 78))
    print(bold("OBSERVED API FIELDS") + dim("  (compare against the pick() candidates in app/clients/)"))
    print(bold("-" * 78))
    any_fields = False
    for r in results:
        keys = r.detail.get("observed_keys")
        if keys:
            any_fields = True
            print(f"\n  {r.name}:")
            print(f"    keys      : {', '.join(keys)}")
            if r.detail.get("parsed"):
                print(f"    parsed    : {', '.join(r.detail['parsed'])}")
            if r.detail.get("unparsed"):
                print(f"    UNPARSED  : {', '.join(r.detail['unparsed'])}")
    if not any_fields:
        print("\n" + dim("  No API returned a payload to inspect. Pass a token address to probe more."))

    counts = summarize_checks(results)
    kind, message = readiness(results)
    print("\n" + bold("=" * 78))
    from app.util.console import status_icon

    print(f"  {status_icon(OK)} {counts[OK]} ok   {status_icon(WARN)} {counts[WARN]} warn   "
          f"{status_icon(FAIL)} {counts[FAIL]} fail   {status_icon(SKIP)} {counts[SKIP]} skip")
    print("\n  " + bold(message))
    print("\n" + dim("  Sources that failed report their metrics as UNAVAILABLE, which routes"))
    print(dim("  tokens to WATCH - never to a buy. That is the intended degradation path."))
    print(bold("=" * 78) + "\n")

    sys.exit(1 if kind == FAIL else 0)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    asyncio.run(main(args[0] if args else None, "--json" in sys.argv))
