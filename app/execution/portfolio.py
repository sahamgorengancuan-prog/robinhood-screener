"""Exposure accounting.

Reads the order ledger to answer one question: how much am I already risking?
Paper and live orders are tracked separately — a paper fill must never consume
live budget, and vice versa.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.execution.order_safety import ExposureState
from app.models import OrderRecord

# Statuses that represent capital actually committed.
COMMITTED = ("created", "live", "partially_filled", "filled")


def _day_start(now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def exposure_for(session: Session, token_id: int, mode: str, now: dt.datetime | None = None) -> ExposureState:
    start = _day_start(now)

    token_exposure = session.execute(
        select(func.coalesce(func.sum(OrderRecord.requested_usd), 0.0)).where(
            OrderRecord.token_id == token_id,
            OrderRecord.mode == mode,
            OrderRecord.status.in_(COMMITTED),
        )
    ).scalar_one()

    daily_exposure = session.execute(
        select(func.coalesce(func.sum(OrderRecord.requested_usd), 0.0)).where(
            OrderRecord.mode == mode,
            OrderRecord.status.in_(COMMITTED),
            OrderRecord.created_at >= start,
        )
    ).scalar_one()

    orders_today = session.execute(
        select(func.count(OrderRecord.id)).where(
            OrderRecord.mode == mode, OrderRecord.created_at >= start
        )
    ).scalar_one()

    open_positions = session.execute(
        select(func.count(func.distinct(OrderRecord.token_id))).where(
            OrderRecord.mode == mode,
            OrderRecord.status.in_(("live", "partially_filled", "filled")),
        )
    ).scalar_one()

    return ExposureState(
        open_positions=int(open_positions or 0),
        token_exposure_usd=float(token_exposure or 0.0),
        daily_exposure_usd=float(daily_exposure or 0.0),
        orders_today=int(orders_today or 0),
    )
