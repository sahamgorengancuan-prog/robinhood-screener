"""Decision engine — turns (gates, score, context) into one of five states.

Precedence, highest to lowest. The first matching rule wins:

  1. HARD gate failed                      -> REJECT
  2. DATA gate failed (metric missing)     -> WATCH
  3. score < alert threshold               -> REJECT
  4. score < paper threshold               -> ALERT
  5. not stable across N snapshots         -> ALERT
  6. LIVE_ONLY gate failed / not on OKX /
     kill switch / safe mode / run_mode    -> PAPER_BUY
  7. everything clean                      -> LIVE_BUY

Note rule 6: a token that is perfect on-chain but absent from OKX stops at
PAPER_BUY and is labelled "on-chain only / manual review". It is never forced
into a trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.pipeline.metrics import stdev
from app.pipeline.risk import summarize
from app.schemas import Decision, DecisionState, GateResult, NormalizedSnapshot, ScoreResult


@dataclass
class DecisionContext:
    """Everything outside the snapshot that can veto a trade."""

    recent_scores: list[float] = field(default_factory=list)
    consecutive_passes: int = 0
    kill_switch: bool = False
    safe_mode: bool = False
    safe_mode_reason: str = ""
    run_mode: str = "ALERT_ONLY"
    exposure_ok: bool = True
    exposure_reason: str = ""
    okx_available: bool | None = None


def is_stable(ctx: DecisionContext, c: Settings) -> tuple[bool, str]:
    if ctx.consecutive_passes < c.stability_required_snapshots:
        return False, (
            f"only {ctx.consecutive_passes}/{c.stability_required_snapshots} consecutive clean snapshots"
        )
    if len(ctx.recent_scores) >= 2:
        sd = stdev(ctx.recent_scores)
        if sd is not None and sd > c.stability_max_score_stdev:
            return False, f"score unstable (stdev {sd:.1f} > {c.stability_max_score_stdev})"
    return True, "stable across required snapshots"


def decide(
    snapshot: NormalizedSnapshot,
    gates: list[GateResult],
    score: ScoreResult,
    ctx: DecisionContext,
    c: Settings,
) -> Decision:
    buckets = summarize(gates)

    # 1 — hard failures are terminal.
    if buckets["hard"]:
        reasons = "; ".join(g.reason for g in buckets["hard"])
        return Decision(state=DecisionState.REJECT, score=score, gates=gates,
                        reason=f"HARD gate failure: {reasons}", hard_fail=True)

    # 2 — missing data can never be assumed favourable.
    if buckets["data"]:
        reasons = "; ".join(g.reason for g in buckets["data"])
        return Decision(state=DecisionState.WATCH, score=score, gates=gates,
                        reason=f"insufficient data: {reasons}", insufficient_data=True)

    # 3 / 4 — score thresholds.
    if score.total < c.score_alert_min:
        return Decision(state=DecisionState.REJECT, score=score, gates=gates,
                        reason=f"score {score.total:.1f} below alert threshold {c.score_alert_min}")

    if score.total < c.score_paper_buy_min:
        return Decision(state=DecisionState.ALERT, score=score, gates=gates,
                        reason=f"score {score.total:.1f} clears alert but is below paper-buy "
                               f"threshold {c.score_paper_buy_min}")

    # 5 — a good score must persist before it means anything.
    stable, stability_reason = is_stable(ctx, c)
    if not stable:
        return Decision(state=DecisionState.ALERT, score=score, gates=gates,
                        reason=f"score {score.total:.1f} qualifies but {stability_reason}")

    # 6 — everything that blocks live execution but not simulation.
    blockers: list[str] = []
    if buckets["live_only"]:
        blockers.extend(g.reason for g in buckets["live_only"])
    if ctx.okx_available is False:
        blockers.append("not listed on OKX — on-chain only / manual review")
    elif ctx.okx_available is None:
        blockers.append("OKX availability unresolved")
    if not snapshot.okx_inst_id and ctx.okx_available:
        blockers.append("no OKX spot instrument resolved for this token")
    if score.total < c.score_live_buy_min:
        blockers.append(f"score {score.total:.1f} below live threshold {c.score_live_buy_min}")
    if ctx.kill_switch:
        blockers.append("kill switch engaged")
    if ctx.safe_mode:
        blockers.append(f"safe mode active: {ctx.safe_mode_reason}")
    if not ctx.exposure_ok:
        blockers.append(f"exposure limit: {ctx.exposure_reason}")
    if ctx.run_mode != "LIVE":
        blockers.append(f"run_mode={ctx.run_mode} (live trading not enabled)")

    if blockers:
        return Decision(state=DecisionState.PAPER_BUY, score=score, gates=gates,
                        reason="passes all risk gates; simulated only — " + "; ".join(blockers))

    # 7 — clean.
    return Decision(state=DecisionState.LIVE_BUY, score=score, gates=gates,
                    reason=f"score {score.total:.1f}, all risk gates passed, {stability_reason}, "
                           f"OKX instrument {snapshot.okx_inst_id} available")
