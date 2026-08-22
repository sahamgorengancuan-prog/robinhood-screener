"""Second derivatives — the rate at which a rate is changing.

A holder count of 1,000 says almost nothing. The sequence 100 → 180 → 320 → 700
says a great deal, and what it says is not contained in any single value or even
in a single growth rate. Growth answers "is it rising"; acceleration answers "is
the rise itself getting faster", which is the question that separates a token
entering expansion from one that already has.

Two windows of equal length are compared. Equal length is the point: comparing a
1-hour growth rate against a 24-hour one measures the window, not the token.

Everything here returns `None` rather than a number whenever the inputs cannot
support one — a missing sample, a zero baseline, unequal windows. A fabricated
0.0 would read as "no acceleration", which is a finding, not an absence.

**Naming is deliberately literal.** `buy_count_acceleration` counts *trades*,
not distinct wallets, because the free data sources report trade counts only.
Unique-buyer acceleration is a strictly better signal and a different metric;
calling this that would overstate what was measured.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

#: Two samples that should be one window apart but are further off than this
#: fraction are not comparable, and the pair is discarded.
WINDOW_TOLERANCE = 0.35


def growth_pct(current: float | None, previous: float | None) -> float | None:
    """Percentage change, or None when it cannot be defined.

    A zero baseline is the trap: going from 0 to 50 holders is not "infinite
    growth", it is the first measurement. Returns None rather than inf.
    """
    if current is None or previous is None or previous <= 0:
        return None
    return (current - previous) / previous * 100.0


@dataclass(frozen=True)
class Sample:
    at: dt.datetime
    value: float | None


def _aware(d: dt.datetime) -> dt.datetime:
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def pick_window(samples: list[Sample], target: dt.datetime) -> Sample | None:
    """The sample closest to `target`, or None if the series is empty."""
    usable = [s for s in samples if s.value is not None]
    if not usable:
        return None
    return min(usable, key=lambda s: abs((_aware(s.at) - target).total_seconds()))


def acceleration(
    samples: list[Sample],
    now: dt.datetime,
    window: dt.timedelta,
) -> dict[str, float | None]:
    """Growth over the last window, over the window before it, and the change.

    Needs three points: t, t-w, t-2w. With only two, growth is defined and
    acceleration is not — and that distinction is reported rather than smoothed
    over, because "we don't know yet" is the honest state for a new token.
    """
    blank: dict[str, float | None] = {
        "growth_pct": None, "prev_growth_pct": None, "acceleration_pp": None,
    }
    if not samples:
        return blank

    now = _aware(now)
    t0 = pick_window(samples, now)
    t1 = pick_window(samples, now - window)
    t2 = pick_window(samples, now - 2 * window)
    if t0 is None or t1 is None:
        return blank

    tolerance = window.total_seconds() * WINDOW_TOLERANCE

    def comparable(a: Sample, b: Sample) -> bool:
        gap = abs((_aware(a.at) - _aware(b.at)).total_seconds())
        return abs(gap - window.total_seconds()) <= tolerance

    out = dict(blank)
    if comparable(t0, t1):
        out["growth_pct"] = growth_pct(t0.value, t1.value)
    if t2 is not None and comparable(t1, t2):
        out["prev_growth_pct"] = growth_pct(t1.value, t2.value)

    if out["growth_pct"] is not None and out["prev_growth_pct"] is not None:
        # Percentage points, not a ratio: the difference between growing 20% and
        # growing 45% is +25pp, which stays meaningful when either is negative.
        out["acceleration_pp"] = out["growth_pct"] - out["prev_growth_pct"]
    return out
