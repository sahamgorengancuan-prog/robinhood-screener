"""Live executor — the only code in this project that can spend real money.

Every guard is re-checked here, in this order, and any single failure aborts:

  1. run_mode == LIVE
  2. kill switch not engaged
  3. safe mode not active
  4. OKX credentials present
  5. the instrument exists and is `live` on OKX
  6. fresh book pulled *now* (not the scored snapshot)
  7. execution conditions still hold (liquidity/slippage)
  8. exposure caps
  9. post-only limit plan builds cleanly

The order is always post-only. If it would cross, OKX rejects it and we take the
no-fill instead of the bad fill.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.clients.base import ClientError
from app.clients.okx_trade import OKXTradeClient
from app.config import Settings
from app.execution import killswitch
from app.execution.order_safety import (
    SafetyVerdict,
    build_order_plan,
    check_execution_conditions,
    check_exposure,
    realized_slippage_bps,
)
from app.execution.paper import _record_from_plan
from app.execution.portfolio import exposure_for
from app.models import Evaluation, OrderRecord, Token

log = logging.getLogger(__name__)


async def execute_live_buy(
    session: Session,
    token: Token,
    evaluation: Evaluation,
    trade: OKXTradeClient,
    c: Settings,
    *,
    inst_id: str,
    reference_price: float | None,
    liquidity_at_decision: float | None,
    liquidity_now: float | None,
    slippage_now: float | None,
    entry_reason: str,
) -> tuple[OrderRecord | None, SafetyVerdict]:
    v = SafetyVerdict(allowed=True)

    # 1-3 — global switches
    if c.run_mode != "LIVE":
        return None, v.block(f"run_mode={c.run_mode}, refusing to place a live order")
    killed, kill_reason = killswitch.is_killed()
    if killed:
        return None, v.block(f"kill switch: {kill_reason}")
    safe, safe_reason = killswitch.safe_mode_status()
    if safe:
        return None, v.block(f"safe mode: {safe_reason}")

    # 4 — credentials
    if not trade.credentialed:
        return None, v.block("OKX trading credentials not configured")

    # 5 — instrument must really exist and be tradable
    try:
        meta = await trade.instrument_meta(inst_id)
    except ClientError as e:
        return None, v.block(f"could not load instrument metadata: {e}")
    if not meta or meta.get("state") != "live":
        return None, v.block(f"instrument {inst_id} is not live on OKX")

    lot_size = _f(meta.get("lotSz"))
    min_size = _f(meta.get("minSz"))

    # 6 — fresh book
    try:
        bid, ask = await trade.top_of_book(inst_id)
    except ClientError as e:
        return None, v.block(f"could not fetch order book: {e}")

    # 7 — conditions still valid
    cond = check_execution_conditions(liquidity_now, liquidity_at_decision, slippage_now, c)
    if not cond.allowed:
        return None, cond

    # 8 — exposure
    exposure = exposure_for(session, token.id, mode="LIVE")
    exp_v = check_exposure(exposure, c.position_usd, c)
    if not exp_v.allowed:
        return None, exp_v

    # 9 — plan
    plan, plan_v = build_order_plan(
        inst_id, bid, ask, reference_price, c.position_usd, c, lot_size, min_size
    )
    if plan is None or not plan_v.allowed:
        return None, plan_v

    record = _record_from_plan(token, evaluation, plan, entry_reason, mode="LIVE")
    session.add(record)
    session.flush()  # persist intent BEFORE the network call, so a crash mid-flight is auditable

    try:
        resp = await trade.place_limit_buy(
            inst_id=plan.inst_id,
            price=plan.limit_price,
            size_base=plan.size_base,
            client_order_id=record.client_order_id,
            post_only=True,
        )
    except ClientError as e:
        record.status = "rejected"
        record.error = str(e)
        session.flush()
        return record, plan_v.block(f"OKX rejected the order: {e}")

    record.exchange_order_id = resp.get("ordId")
    record.status = "live"
    record.raw = dict(record.raw or {}, place_response=resp)
    session.flush()

    log.warning(
        "LIVE ORDER PLACED %s %s sz=%s px=%s clOrdId=%s",
        inst_id, "buy", plan.size_base, plan.limit_price, record.client_order_id,
    )
    return record, plan_v


async def refresh_order(session: Session, record: OrderRecord, trade: OKXTradeClient) -> OrderRecord:
    """Poll an open order and record the fill outcome."""
    if record.mode != "LIVE" or not record.inst_id:
        return record
    try:
        data = await trade.get_order(record.inst_id, record.client_order_id)
    except ClientError as e:
        record.error = f"status poll failed: {e}"
        return record
    if not data:
        return record

    state = data.get("state")
    filled = _f(data.get("accFillSz")) or 0.0
    avg = _f(data.get("avgPx"))

    record.filled_base = filled
    record.avg_fill_price = avg
    record.raw = dict(record.raw or {}, last_status=data)

    mapping = {
        "live": "live",
        "partially_filled": "partially_filled",
        "filled": "filled",
        "canceled": "canceled",
        "mmp_canceled": "canceled",
    }
    record.status = mapping.get(state or "", record.status)

    if avg and record.limit_price:
        record.realized_slippage_bps = realized_slippage_bps(record.limit_price, avg)
    if _f(data.get("fee")) is not None:
        record.fee_usd = abs(_f(data.get("fee")) or 0.0)
    return record


async def cancel_stale_orders(
    session: Session, trade: OKXTradeClient, c: Settings, records: list[OrderRecord]
) -> list[OrderRecord]:
    """Cancel anything still resting past `ORDER_TTL_S`.

    A stale post-only buy is a standing bid into a market that has moved on —
    exactly the order you do not want filled.
    """
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    touched: list[OrderRecord] = []
    for r in records:
        if r.status not in ("live", "partially_filled") or not r.inst_id:
            continue
        created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=dt.timezone.utc)
        if (now - created).total_seconds() < c.order_ttl_s:
            continue
        try:
            await trade.cancel_order(r.inst_id, r.client_order_id)
            r.status = "canceled"
            r.exit_reason = f"TTL {c.order_ttl_s}s exceeded without a fill"
        except ClientError as e:
            r.error = f"cancel failed: {e}"
        touched.append(r)
    return touched


def _f(v) -> float | None:
    from app.clients.base import to_float

    return to_float(v)
