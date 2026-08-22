"""Observed distributions, so a threshold can be argued from data.

`MIN_UNIQUE_HOLDERS = 300` and its siblings are guesses. They may be good
guesses, but nothing in the system can tell you whether 300 is the 5th
percentile of this chain or the 95th — and on a young chain those are wildly
different decisions.

This module answers one question: *what do the tokens we have actually look
like?* It reports percentiles per metric, split by token age, because a 3-hour-
old token and a 3-week-old one are not the same population and pooling them
produces a distribution describing neither.

It deliberately does **not** set thresholds. A percentile is not a safety
standard: the bottom decile of a bad population is still bad, and "top 10% of
this chain by liquidity" can still be too thin to exit. Use this to see where a
proposed number falls, then decide. Safety floors that must hold in absolute
terms — liquidity being the main one — are derived from the trade instead, in
`liquidity_floor.py`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TokenSnapshot

#: Age buckets in hours. Boundaries are conventional, not discovered; they exist
#: so cohorts are comparable, and any of them can be empty.
AGE_BUCKETS: list[tuple[str, float, float]] = [
    ("<6h", 0.0, 6.0),
    ("6-24h", 6.0, 24.0),
    ("1-7d", 24.0, 168.0),
    (">7d", 168.0, float("inf")),
]

#: Metrics worth a distribution. Each maps to a snapshot column.
METRICS = [
    "liquidity_usd", "volume_24h", "unique_holders",
    "top1_holder_pct", "top10_holder_pct", "tx_count_24h",
]

#: Below this many observations a percentile is noise dressed as a number.
MIN_SAMPLE = 20


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile. `p` in 0..100."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return float(clean[0])
    rank = (p / 100.0) * (len(clean) - 1)
    low = int(rank)
    high = min(low + 1, len(clean) - 1)
    frac = rank - low
    return float(clean[low] * (1 - frac) + clean[high] * frac)


def percentile_of(value: float | None, population: list[float]) -> float | None:
    """Where `value` sits in `population`, as a 0-100 percentile."""
    clean = [v for v in population if v is not None]
    if value is None or not clean:
        return None
    below = sum(1 for v in clean if v < value)
    ties = sum(1 for v in clean if v == value)
    return (below + 0.5 * ties) / len(clean) * 100.0


@dataclass
class MetricStats:
    metric: str
    n: int
    p10: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None

    @property
    def trustworthy(self) -> bool:
        return self.n >= MIN_SAMPLE


@dataclass
class CohortStats:
    label: str
    n_snapshots: int
    metrics: dict[str, MetricStats] = field(default_factory=dict)


def bucket_for(age_hours: float | None) -> str | None:
    if age_hours is None:
        return None
    for label, low, high in AGE_BUCKETS:
        if low <= age_hours < high:
            return label
    return None


def collect(session: Session, *, since: dt.datetime | None = None) -> dict[str, CohortStats]:
    """Percentiles per metric, per age cohort, over stored snapshots."""
    stmt = select(TokenSnapshot)
    if since is not None:
        stmt = stmt.where(TokenSnapshot.captured_at >= since)
    rows = session.execute(stmt).scalars().all()

    grouped: dict[str, list[TokenSnapshot]] = {label: [] for label, _, _ in AGE_BUCKETS}
    for row in rows:
        bucket = bucket_for(row.token_age_hours)
        if bucket:
            grouped[bucket].append(row)

    out: dict[str, CohortStats] = {}
    for label, members in grouped.items():
        stats = CohortStats(label=label, n_snapshots=len(members))
        for metric in METRICS:
            values = [getattr(r, metric, None) for r in members]
            values = [float(v) for v in values if v is not None]
            stats.metrics[metric] = MetricStats(
                metric=metric, n=len(values),
                p10=percentile(values, 10), p25=percentile(values, 25),
                p50=percentile(values, 50), p75=percentile(values, 75),
                p90=percentile(values, 90),
            )
        out[label] = stats
    return out


def render(cohorts: dict[str, CohortStats]) -> str:
    """A plain-text table for the log or the console."""
    lines: list[str] = []
    for label, _, _ in AGE_BUCKETS:
        stats = cohorts.get(label)
        if stats is None or not stats.n_snapshots:
            lines.append(f"{label}: no snapshots")
            continue
        lines.append(f"{label}: {stats.n_snapshots} snapshot(s)")
        for metric in METRICS:
            m = stats.metrics[metric]
            if not m.n:
                lines.append(f"  {metric:<18} no data")
                continue
            def fmt(v):
                return "n/a" if v is None else (f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}")
            warn = "" if m.trustworthy else f"  (n={m.n}, below {MIN_SAMPLE} — treat as noise)"
            lines.append(
                f"  {metric:<18} p10 {fmt(m.p10):>12}  p25 {fmt(m.p25):>12}  "
                f"p50 {fmt(m.p50):>12}  p75 {fmt(m.p75):>12}  p90 {fmt(m.p90):>12}{warn}"
            )
    return "\n".join(lines)
