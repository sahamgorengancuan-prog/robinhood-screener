#!/usr/bin/env python3
"""Headless pipeline runner.

`START.bat` opens the control panel and everything is done there; this script is
the scriptable equivalent, for a scheduled task or a terminal.

Does the whole job end to end, in the order an operator actually needs it:

    1. make sure .env exists          (copied from .env.example on first run)
    2. create/upgrade the database
    3. test every API and print the verdict
    4. run ONE screening cycle
    5. print the results and where to look next

The logic lives here rather than in the .bat on purpose: batch script is the
one part of this project that cannot be tested on the machine it was written
on, so it is kept to "find Python, make a venv, call this file".

    python scripts/one_click.py [--skip-checks] [--ui] [--token 0x...]

Safety: if `.env` says RUN_MODE=LIVE with real (non-simulated) OKX keys, this
refuses to run unattended and demands a typed confirmation. A double-clicked
icon must never be one click away from spending real money.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.util.console import bold, dim, init_console, safe, status_icon  # noqa: E402

BAR = "=" * 74


def say(text: str = "") -> None:
    """Single output funnel, so every line is console-safe by construction."""
    print(safe(text))


def hr(title: str = "") -> None:
    say("\n" + bold(BAR))
    if title:
        say(bold("  " + title))
        say(bold(BAR))


def step(n: int, total: int, text: str) -> None:
    say(f"\n{bold(f'[{n}/{total}]')} {text}")


# ---------------------------------------------------------------- step 1
def ensure_env() -> bool:
    """Returns True if this is a first run (fresh .env just created)."""
    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        say(f"  {status_icon('OK')} .env found")
        return False
    if not example.exists():
        say(f"  {status_icon('FAIL')} neither .env nor .env.example found — is the repo complete?")
        raise SystemExit(2)
    shutil.copyfile(example, env)
    say(f"  {status_icon('WARN')} .env did not exist — created from .env.example")
    say(dim("      Defaults are safe: RUN_MODE=ALERT_ONLY, no keys, no orders possible."))
    say(dim("      Open .env and set RH_NODE_RPC_URL to get real data."))
    return True


def load_settings_or_explain():
    """Turn a pydantic validation error into something a non-developer can act on.

    This script is launched by double-clicking an icon. A raw traceback in a
    console window that closes is the worst possible failure mode, so a bad
    `.env` has to name the offending line and the fix.
    """
    from app.config import get_settings

    try:
        return get_settings()
    except Exception as e:  # noqa: BLE001
        hr("CONFIGURATION ERROR")
        say(f"  {status_icon('FAIL')} .env could not be read.\n")
        for line in str(e).splitlines():
            line = line.strip()
            if line and not line.startswith("For further information"):
                print("    " + safe(line))
        say("\n  Fix the setting named above in .env, or delete .env and re-run")
        say("  to regenerate it from .env.example with safe defaults.")
        say(bold(BAR) + "\n")
        raise SystemExit(2) from None


# ---------------------------------------------------------------- guard
def confirm_live_mode() -> None:
    from app.config import get_settings

    c = get_settings()
    if c.run_mode != "LIVE":
        return

    from app.execution import killswitch

    killed, reason = killswitch.is_killed()
    if killed:
        say(f"  {status_icon('OK')} RUN_MODE=LIVE but the kill switch is engaged ({reason})")
        return

    if c.okx_simulated:
        say(f"  {status_icon('WARN')} RUN_MODE=LIVE on OKX demo trading (simulated) — no real money")
        return

    hr("REAL MONEY MODE")
    say(f"  {status_icon('FAIL')} RUN_MODE=LIVE and OKX_SIMULATED=false.")
    say("  This run can place REAL orders with REAL funds.")
    say(f"  Clip ${c.position_usd:,.2f} · per-token cap ${c.max_exposure_per_token_usd:,.2f} "
          f"· daily cap ${c.max_exposure_daily_usd:,.2f}")
    say(bold(BAR))
    try:
        answer = input("\n  Type LIVE to continue, anything else to abort: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "LIVE":
        say("\n  Aborted. Nothing was traded.")
        raise SystemExit(1)


# ---------------------------------------------------------------- step 3
async def run_checks() -> str:
    from app.diagnostics import FAIL, readiness, run_all_checks, summarize_checks

    results = await run_all_checks()
    for r in results:
        lat = f"{r.latency_ms:>6.0f}ms" if r.latency_ms is not None else "      -"
        print(safe(f"  {r.icon} {r.status:<5}{lat}  {r.name:<22} {r.summary}"))

    counts = summarize_checks(results)
    kind, message = readiness(results)
    say(f"\n  {counts['OK']} ok · {counts['WARN']} warn · "
        f"{counts['FAIL']} fail · {counts['SKIP']} skip")
    say("  " + bold(message))

    if kind == FAIL:
        say(dim("\n  Continuing anyway: unreachable sources make metrics UNAVAILABLE, which"))
        say(dim("  routes tokens to WATCH. Nothing can be bought on missing data."))
    return kind


# ---------------------------------------------------------------- step 4
async def run_pipeline(token: str | None) -> dict:
    from app.pipeline.ingest import run_cycle
    from app.services import build_services
    from app.config import get_settings

    if token:
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import Token

        with session_scope() as s:
            exists = s.execute(select(Token).where(Token.address == token)).scalar_one_or_none()
            if not exists:
                s.add(Token(chain="robinhood", address=token))
                say(f"  {status_icon('OK')} added {token} to the watchlist")

    svc = build_services(get_settings())
    try:
        return await run_cycle(svc)
    finally:
        await svc.aclose()


def print_results(summary: dict) -> None:
    if not summary["tokens"]:
        say(f"\n  {status_icon('WARN')} No candidate tokens.")
        say(dim("      Enable the Data API for automatic discovery, or add a contract:"))
        say(dim("      python scripts/one_click.py --token 0xYourContract"))
        return

    say(f"\n  {summary['tokens']} token(s) in {summary['duration_s']:.1f}s")
    order = {"LIVE_BUY": 0, "PAPER_BUY": 1, "ALERT": 2, "WATCH": 3, "REJECT": 4, "ERROR": 5}
    rows = sorted(summary["results"], key=lambda r: (order.get(r.get("state"), 9), -(r.get("score") or 0)))

    say("\n  " + bold(f"{'STATE':<10} {'SYMBOL':<10} {'SCORE':>6}  REASON"))
    for r in rows:
        score = r.get("score")
        print(safe(f"  {r.get('state', '?'):<10} {str(r.get('symbol') or '?'):<10} "
                   f"{(f'{score:.1f}' if score is not None else '-'):>6}  {(r.get('reason') or '')[:70]}"))

    tally = " · ".join(f"{k}={v}" for k, v in summary["by_state"].items())
    print(f"\n  {safe(tally)}")


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Run one full screening pipeline.")
    ap.add_argument("--skip-checks", action="store_true", help="skip the API connection tests")
    ap.add_argument("--ui", action="store_true", help="open the control panel when the cycle finishes")
    ap.add_argument("--token", help="add a contract address to the watchlist before screening")
    args = ap.parse_args()

    init_console()
    hr("ROBINHOOD CHAIN SCREENER  -  ONE-CLICK PIPELINE")

    total = 5 if not args.skip_checks else 4
    n = 0

    n += 1
    step(n, total, "Checking configuration")
    fresh = ensure_env()

    from app.logging_conf import configure_logging

    c = load_settings_or_explain()
    configure_logging("WARNING")  # keep the console readable; full logs go to the API/UI
    if not fresh:
        print(safe(f"  {status_icon('OK')} .env loaded · mode {c.run_mode} · "
                   f"OKX {'demo' if c.okx_simulated else 'REAL MONEY'}"))
    confirm_live_mode()

    n += 1
    step(n, total, "Preparing database")
    from app.db import init_db

    init_db()
    say(f"  {status_icon('OK')} {c.database_url}")

    if not args.skip_checks:
        n += 1
        step(n, total, "Testing connections and APIs")
        asyncio.run(run_checks())

    n += 1
    step(n, total, "Running one screening cycle")
    summary = asyncio.run(run_pipeline(args.token))

    n += 1
    step(n, total, "Results")
    print_results(summary)

    hr("DONE")
    say("  Alerts   : data\\alerts.log")
    say("  Database : " + c.database_url.replace("sqlite:///", ""))
    say("  Panel    : START.bat           (setup, results, thresholds, kill switch)")
    if c.run_mode == "ALERT_ONLY":
        say(dim("\n  Mode is ALERT_ONLY: nothing was traded, and nothing could be."))
    say(bold(BAR) + "\n")

    if args.ui:
        from app.ui.gradio_app import main as ui_main

        say("  Starting the control panel — close this window to stop it.\n")
        ui_main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\n\n  Interrupted.")
        raise SystemExit(130)
