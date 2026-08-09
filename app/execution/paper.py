"""Paper trading executor.

Simulates the exact order the live path would send, using the same
`build_order_plan` and the same safety checks. That is the whole point: paper
mode is not a different code path with optimistic assumptions, it is the live
path with the network call replaced.

Fill model, stated explicitly so results are never read as more precise than
they are:

  * the order is a post-only buy resting `limit_offset_bps` below the bid;
  * it fills if that offset is within `paper_assumed_touch_bps` — the distance
    below the bid the market is *assumed* to trade at some point during the
    order's TTL;
  * fill price = the limit price (a post-only buy cannot do better);
  * fee = maker fee (8bps by default).

`paper_assumed_touch_bps` is the one genuinely unverifiable number here. It is a
stand-in for short-horizon volatility, which we do not model. The default (60bps
over a 5-minute TTL) is deliberately generous, so paper results should be read
as an **optimistic** fill assumption: compare `realized_slippage_bps` on live
orders against paper before trusting the simulated hit rate, and lower the value
if live fills come in worse. It never affects the live path.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.config import Settings
from app.execution.order_safety import (
    OrderPlan,
    SafetyVerdict,
    build_order_plan,
    check_exposure,
    realized_slippage_bps,
)
from app.execution.portfolio import exposure_for
from app.models import Evaluation, OrderRecord, Token

log = logging.getLogger(__name__)

MAKER_FEE_BPS = 8.0


def new_client_order_id(prefix: str = "pap") -> str:
    # OKX clOrdId: alphanumeric, <=32 chars.
    return f"{prefix}{uuid.uuid4().hex[:20]}"


def simulate_buy(
    session: Session,
    token: Token,
    evaluation: Evaluation,
    inst_id: str,
    bid: float | None,
    ask: float | None,
    reference_price: float | None,
    c: Settings,
    entry_reason: str,
    lot_size: float | None = None,
    min_size: float | None = None,
) -> tuple[OrderRecord | None, SafetyVerdict]:
    exposure = exposure_for(session, token.id, mode="PAPER")
    verdict = check_exposure(exposure, c.position_usd, c)
    if not verdict.allowed:
        log.info("paper buy blocked for %s: %s", token.symbol or token.address, verdict.reasons)
        return None, verdict

    plan, plan_verdict = build_order_plan(
        inst_id, bid, ask, reference_price, c.position_usd, c, lot_size, min_size
    )
    if plan is None or not plan_verdict.allowed:
        return None, plan_verdict

    record = _record_from_plan(token, evaluation, plan, entry_reason, mode="PAPER")
    _apply_fill_model(record, plan, bid, ask, reference_price, c.paper_assumed_touch_bps)
    session.add(record)
    session.flush()

    log.info(
        "PAPER %s %s: %s @ %.10g -> %s",
        record.side, inst_id, record.size_base, record.limit_price, record.status,
    )
    return record, plan_verdict


def _record_from_plan(
    token: Token, evaluation: Evaluation, plan: OrderPlan, entry_reason: str, mode: str
) -> OrderRecord:
    return OrderRecord(
        token_id=token.id,
        evaluation_id=evaluation.id,
        mode=mode,
        inst_id=plan.inst_id,
        side="buy",
        ord_type="post_only",
        client_order_id=new_client_order_id("pap" if mode == "PAPER" else "liv"),
        requested_usd=plan.notional_usd,
        limit_price=plan.limit_price,
        size_base=plan.size_base,
        status="created",
        entry_reason=entry_reason,
        raw={"plan_reasons": plan.reasons},
    )


def _apply_fill_model(
    record: OrderRecord,
    plan: OrderPlan,
    bid: float | None,
    ask: float | None,
    reference_price: float | None,
    assumed_touch_bps: float,
) -> None:
    if bid is None or ask is None or bid <= 0:
        record.status = "unfilled"
        record.exit_reason = "no book available to simulate a fill"
        return

    reachable = bid * (1.0 - assumed_touch_bps / 10_000.0)
    if plan.limit_price < reachable:
        record.status = "unfilled"
        record.exit_reason = (
            f"post-only limit {plan.limit_price:.10g} is more than {assumed_touch_bps:.0f}bps "
            f"below the bid {bid:.10g} — assumed not reached within the order TTL"
        )
        record.raw = dict(record.raw or {}, fill_model="post_only_touch",
                          assumed_touch_bps=assumed_touch_bps)
        return

    record.status = "filled"
    record.filled_base = plan.size_base
    record.avg_fill_price = plan.limit_price
    record.fee_usd = plan.notional_usd * MAKER_FEE_BPS / 10_000.0
    if reference_price:
        record.realized_slippage_bps = realized_slippage_bps(reference_price, plan.limit_price)
    record.raw = dict(
        record.raw or {},
        fill_model="post_only_touch",
        assumed_touch_bps=assumed_touch_bps,
        maker_fee_bps=MAKER_FEE_BPS,
    )
