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

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


async def main(address: str | None, as_json: bool) -> None:
    c = get_settings()
    configure_logging("ERROR" if as_json else c.log_level)
    init_db()

    results = await run_all_checks(address)

    if as_json:
        print(json.dumps([r.as_dict() for r in results], indent=2, default=str))
        return

    print(f"\n{BOLD}{'=' * 78}{RESET}")
    print(f"{BOLD}  CONNECTION & API PROBE{RESET}")
    print(f"{DIM}  run_mode={c.run_mode}  okx_simulated={c.okx_simulated}"
          f"  rh_data_enabled={c.rh_data_enabled}{RESET}")
    print(f"{BOLD}{'=' * 78}{RESET}\n")

    group = None
    for r in results:
        if r.group != group:
            group = r.group
            print(f"\n{BOLD}{group}{RESET}")
        lat = f"{r.latency_ms:>6.0f}ms" if r.latency_ms is not None else "      —"
        print(f"  {r.icon} {r.status:<5} {lat}  {r.name:<22} {r.summary}")
        if r.fix:
            print(f"          {DIM}→ {r.fix}{RESET}")

    # Observed field names are the whole point for the unverified endpoints.
    print(f"\n{BOLD}{'-' * 78}{RESET}")
    print(f"{BOLD}OBSERVED API FIELDS{RESET}  {DIM}(compare against the pick() candidates in app/clients/){RESET}")
    print(f"{BOLD}{'-' * 78}{RESET}")
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
        print(f"\n  {DIM}No API returned a payload to inspect. Pass a token address to probe more.{RESET}")

    counts = summarize_checks(results)
    kind, message = readiness(results)
    print(f"\n{BOLD}{'=' * 78}{RESET}")
    print(f"  🟢 {counts[OK]} ok   🟡 {counts[WARN]} warn   🔴 {counts[FAIL]} fail   ⚪ {counts[SKIP]} skip")
    print(f"\n  {BOLD}{message}{RESET}")
    print(f"\n  {DIM}Sources that failed report their metrics as UNAVAILABLE, which routes tokens")
    print(f"  to WATCH — never to a buy. That is the intended degradation path.{RESET}")
    print(f"{BOLD}{'=' * 78}{RESET}\n")

    sys.exit(1 if kind == FAIL else 0)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    asyncio.run(main(args[0] if args else None, "--json" in sys.argv))
