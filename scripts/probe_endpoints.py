#!/usr/bin/env python3
"""Connectivity + contract probe. Run this FIRST, before trusting any output.

It answers the questions this repo cannot answer offline:

  * Does the Node RPC respond, and what is the real chain ID?
  * Is the Data API reachable, and what keys does it actually return?
  * Does OKX accept our signature, and what fields come back?
  * Is the explorer serving verification status?

For each source it prints the observed top-level keys, so you can compare them
against the candidate names in the clients and extend those lists if they
differ. Nothing here is inferred — it reports only what the APIs returned.

    python scripts/probe_endpoints.py [0xTokenAddress]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.logging_conf import configure_logging  # noqa: E402
from app.services import build_services  # noqa: E402


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def show(label: str, value) -> None:
    print(f"  {label:<26} {value}")


async def main(address: str | None) -> None:
    c = get_settings()
    configure_logging(c.log_level)
    svc = build_services(c)

    section("CONFIG")
    show("run_mode", c.run_mode)
    show("okx_simulated", c.okx_simulated)
    show("rh_data_enabled", c.rh_data_enabled)
    show("explorer_enabled", c.explorer_enabled)
    show("okx_market_enabled", c.okx_market_enabled)

    # ---------------------------------------------------------------- node
    section("ROBINHOOD CHAIN — NODE API (JSON-RPC)")
    if not svc.node.enabled:
        print("  SKIPPED: RH_NODE_RPC_URL is not set.")
    else:
        try:
            chain_id = await svc.node.chain_id()
            block = await svc.node.block_number()
            show("eth_chainId", chain_id)
            show("eth_blockNumber", block)
            if c.rh_chain_id and chain_id and c.rh_chain_id != chain_id:
                print(f"  !! RH_CHAIN_ID={c.rh_chain_id} does not match the node's {chain_id}")
            elif chain_id and not c.rh_chain_id:
                print(f"  -> set RH_CHAIN_ID={chain_id} in your .env")
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {e}")

    # ------------------------------------------------------------ data api
    section("ROBINHOOD CHAIN — DATA API")
    probe = await svc.data.probe()
    print(f"  {json.dumps(probe, indent=2)}")
    if probe.get("sample_keys"):
        print("\n  Compare these keys against the `pick()` candidates in")
        print("  app/clients/rh_data.py and extend them if they differ.")

    # ----------------------------------------------------------- explorer
    section("EXPLORER (BLOCKSCOUT)")
    if not svc.explorer.enabled:
        print("  SKIPPED: EXPLORER_ENABLED=false")
    elif address:
        info = await svc.explorer.contract_info(address)
        if info is None:
            print("  no contract record returned (unverified, or endpoint unavailable)")
        else:
            show("keys", sorted(info.keys())[:20])
            show("is_verified", await svc.explorer.is_verified(address))
    else:
        print("  pass a token address to probe contract verification")

    # ---------------------------------------------------------- okx market
    section("OKX MARKET API")
    probe = await svc.market.probe()
    print(f"  {json.dumps(probe, indent=2)}")
    if probe.get("ok") is False:
        print("\n  Check OKX_API_KEY / SECRET / PASSPHRASE / PROJECT_ID.")
        print("  A signature error usually means the signed request path did not")
        print("  include the query string exactly as sent.")

    if address and c.rh_chain_id:
        try:
            info = await svc.market.price_info(str(c.rh_chain_id), [address])
            item = info.get(address.lower())
            show("price_info keys", sorted(item.keys()) if item else "no data for this token")
            if item:
                from app.clients.okx_market import OKXMarketClient

                mapped = OKXMarketClient.extract_market(item)
                missing = [k for k, v in mapped.items() if v is None]
                show("mapped ok", [k for k, v in mapped.items() if v is not None])
                show("UNMAPPED (fix pick())", missing)
        except Exception as e:  # noqa: BLE001
            print(f"  price_info FAILED: {e}")

    # ----------------------------------------------------------- okx trade
    section("OKX TRADING API")
    show("credentialed", svc.trade.credentialed)
    show("simulated", svc.trade.simulated)
    try:
        insts = await svc.trade.spot_instruments()
        show("live SPOT instruments", len(insts))
        ref = await svc.trade.ticker(c.safe_mode_ref_instrument)
        show(f"{c.safe_mode_ref_instrument} last", (ref or {}).get("last"))
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED: {e}")

    if svc.trade.credentialed:
        try:
            show("USDT available", await svc.trade.balance("USDT"))
        except Exception as e:  # noqa: BLE001
            print(f"  balance FAILED: {e}")

    section("SUMMARY")
    print("  Sources reporting OK above are usable. Anything that failed will")
    print("  cause its metrics to be reported as UNAVAILABLE, which routes tokens")
    print("  to WATCH — never to a buy. That is the intended degradation path.")

    await svc.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
