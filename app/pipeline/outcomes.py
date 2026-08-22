"""Forward-return labelling — the half of the dataset that takes time to earn.

Everything else in this project can be built in an afternoon. This cannot: a
label for a 7-day horizon requires seven days to pass. That makes it the one
component whose cost is measured in calendar time rather than effort, and the
reason to start writing it before the model that will consume it exists.

Three properties are deliberate.

**Every snapshot is labelled, not only the interesting ones.** A dataset built
from tokens the screener liked teaches a model to agree with the screener. The
rejects are the majority class and the only source of negative evidence.

**A label is never inferred from price alone.** A token whose price rises while
its pool empties has not gone up; it has become unsellable. Liquidity drawdown
is recorded next to the return so the two can disagree.

**Sampling is admitted.** `max_return_pct` is the maximum across sampling
instants, not the true intra-window high. At the default 15-minute cadence a
spike lasting five minutes is invisible. `observations` travels with every row
so a downstream model can weight — or discard — thinly sampled labels.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SnapshotOutcome, TokenSnapshot, utcnow

log = logging.getLogger(__name__)

#: Horizons to label. Short ones are for reflex signals, long ones for whether
#: the move survived. Keys are stored verbatim in the `horizon` column.
HORIZONS: dict[str, int] = {
    "1h": 3_600,
    "6h": 21_600,
    "24h": 86_400,
    "3d": 259_200,
    "7d": 604_800,
}

#: A pool losing this share of its depth marks the row as a suspected pull.
#: Deliberately not called "rug": this is an observation, not an accusation.
RUG_LIQUIDITY_DRAWDOWN_PCT = 90.0


def _aware(d: dt.datetime) -> dt.datetime:
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


@dataclass
class Window:
    base: TokenSnapshot
    horizon: str
    following: list[TokenSnapshot]
    complete: bool


def measure(window: Window) -> dict[str, object]:
    """Reduce a window of subsequent snapshots to one labelled row."""
    base_price = window.base.price_usd
    prices = [(s, s.price_usd) for s in window.following if s.price_usd and s.price_usd > 0]

    row: dict[str, object] = {
        "base_price": base_price,
        "observations": len(prices),
        "window_complete": window.complete,
        "max_price": None, "min_price": None,
        "max_return_pct": None, "min_return_pct": None, "end_return_pct": None,
        "reached_2x": None, "reached_5x": None, "reached_10x": None,
        "seconds_to_2x": None,
        "liquidity_drawdown_pct": None, "rug_suspected": None,
    }

    # No usable base price means no return can be defined. Leaving the fields
    # null is correct; filling them with zeros would invent a flat outcome.
    if not base_price or base_price <= 0 or not prices:
        return row

    values = [p for _, p in prices]
    row["max_price"] = max(values)
    row["min_price"] = min(values)
    row["max_return_pct"] = (max(values) / base_price - 1.0) * 100.0
    row["min_return_pct"] = (min(values) / base_price - 1.0) * 100.0
    row["end_return_pct"] = (values[-1] / base_price - 1.0) * 100.0

    peak = max(values) / base_price
    row["reached_2x"] = peak >= 2.0
    row["reached_5x"] = peak >= 5.0
    row["reached_10x"] = peak >= 10.0

    base_at = _aware(window.base.captured_at)
    for snap, price in prices:
        if price / base_price >= 2.0:
            row["seconds_to_2x"] = int((_aware(snap.captured_at) - base_at).total_seconds())
            break

    base_liq = window.base.liquidity_usd
    liqs = [s.liquidity_usd for s in window.following if s.liquidity_usd is not None]
    if base_liq and base_liq > 0 and liqs:
        drawdown = (1.0 - min(liqs) / base_liq) * 100.0
        row["liquidity_drawdown_pct"] = drawdown
        row["rug_suspected"] = drawdown >= RUG_LIQUIDITY_DRAWDOWN_PCT
    return row


def pending_horizons(snapshot_at: dt.datetime, now: dt.datetime, done: set[str]) -> list[str]:
    """Horizons whose window has fully elapsed and which are not yet labelled.

    A window is only labelled once it has closed. Labelling early would write a
    row that quietly means "so far", and nothing downstream could tell it apart
    from a settled one.
    """
    age = (now - _aware(snapshot_at)).total_seconds()
    return [h for h, seconds in HORIZONS.items() if age >= seconds and h not in done]


def label_snapshots(session: Session, *, now: dt.datetime | None = None,
                    max_snapshots: int = 500) -> dict[str, int]:
    """Fill in outcomes for every snapshot whose windows have closed.

    Bounded per call so a long-idle database catches up over several cycles
    instead of stalling one.
    """
    now = now or utcnow()
    oldest_window = max(HORIZONS.values())
    cutoff = now - dt.timedelta(seconds=min(HORIZONS.values()))

    candidates = session.execute(
        select(TokenSnapshot)
        .where(TokenSnapshot.captured_at <= cutoff)
        .order_by(TokenSnapshot.captured_at.desc())
        .limit(max_snapshots)
    ).scalars().all()
    if not candidates:
        return {"labelled": 0, "snapshots": 0}

    existing: dict[int, set[str]] = {}
    for sid, horizon in session.execute(
        select(SnapshotOutcome.snapshot_id, SnapshotOutcome.horizon)
        .where(SnapshotOutcome.snapshot_id.in_([s.id for s in candidates]))
    ).all():
        existing.setdefault(sid, set()).add(horizon)

    todo = [
        (snap, pending_horizons(snap.captured_at, now, existing.get(snap.id, set())))
        for snap in candidates
    ]
    todo = [(s, hs) for s, hs in todo if hs]
    if not todo:
        return {"labelled": 0, "snapshots": 0}

    # One query per token rather than per snapshot-horizon.
    written = 0
    by_token: dict[int, list[tuple[TokenSnapshot, list[str]]]] = {}
    for snap, horizons in todo:
        by_token.setdefault(snap.token_id, []).append((snap, horizons))

    for token_id, items in by_token.items():
        earliest = min(_aware(s.captured_at) for s, _ in items)
        series = session.execute(
            select(TokenSnapshot)
            .where(
                TokenSnapshot.token_id == token_id,
                TokenSnapshot.captured_at >= earliest,
                TokenSnapshot.captured_at <= earliest + dt.timedelta(seconds=oldest_window),
            )
            .order_by(TokenSnapshot.captured_at)
        ).scalars().all()

        for snap, horizons in items:
            base_at = _aware(snap.captured_at)
            for horizon in horizons:
                end = base_at + dt.timedelta(seconds=HORIZONS[horizon])
                following = [
                    s for s in series
                    if s.id != snap.id and base_at < _aware(s.captured_at) <= end
                ]
                row = measure(Window(base=snap, horizon=horizon,
                                     following=following, complete=True))
                session.add(SnapshotOutcome(
                    snapshot_id=snap.id, token_id=token_id, horizon=horizon, **row))
                written += 1

    log.info("labelled %d outcome row(s) across %d snapshot(s)", written, len(todo))
    return {"labelled": written, "snapshots": len(todo)}
