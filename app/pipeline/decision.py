"""Decision engine — turns (gates, score, context) into one of five states.

Precedence, highest to lowest. The first matching rule wins:

  1. HARD gate failed                      -> REJECT
  2. DATA gate failed (metric missing)     -> WATCH
  3. score < alert threshold               -> REJECT
  4. score < paper threshold               -> ALERT
  5. not stable across N snapshots         -> ALERT
  6. LIVE_ONLY gate failed / not on OKX    -> ALERT
  7. kill switch / safe mode               -> ALERT
  8. PAPER mode / live score not met       -> PAPER_BUY
  9. everything clean                      -> LIVE_BUY

Note rule 6: a token that is perfect on-chain but absent from OKX stops at
ALERT and is labelled "on-chain only / manual review". It is never forced into
a trade.
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


#: Gates whose failure *explains* the others. A liquidity-pool share has no
#: pool and one holder by construction, so leading the rejection with
#: "liquidity_usd unavailable" sends an operator to check an API that is working
#: fine. The structural fact goes first; the consequences follow.
ROOT_CAUSE_GATES = ("tradeable_token", "contract_verified", "contract_flags")


def root_cause_first(results):
    """Order hard failures so the explanation precedes its symptoms.

    Stable within each group, so the existing ordering is otherwise preserved.
    """
    rank = {name: i for i, name in enumerate(ROOT_CAUSE_GATES)}
    return sorted(results, key=lambda g: rank.get(g.name, len(rank)))


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
        reasons = "; ".join(g.reason for g in root_cause_first(buckets["hard"]))
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

    # 6 — PAPER_BUY means every risk gate passed. Unknown sniper/unlock/spread
    # therefore stays ALERT; it is not relabelled as a successful simulation.
    blockers: list[str] = []
    if buckets["live_only"]:
        blockers.extend(g.reason for g in buckets["live_only"])
    if ctx.okx_available is False:
        blockers.append("not listed on OKX — on-chain only / manual review")
    elif ctx.okx_available is None:
        blockers.append("OKX availability unresolved")
    if not snapshot.okx_inst_id and ctx.okx_available:
        blockers.append("no OKX spot instrument resolved for this token")
    if blockers:
        return Decision(state=DecisionState.ALERT, score=score, gates=gates,
                        reason="risk/venue review required — " + "; ".join(blockers))

    # 7 — global controls block all order creation, including paper records.
    if ctx.kill_switch:
        return Decision(state=DecisionState.ALERT, score=score, gates=gates,
                        reason="qualified, but kill switch is engaged")
    if ctx.safe_mode:
        return Decision(state=DecisionState.ALERT, score=score, gates=gates,
                        reason=f"qualified, but safe mode is active: {ctx.safe_mode_reason}")
    if not ctx.exposure_ok:
        return Decision(state=DecisionState.ALERT, score=score, gates=gates,
                        reason=f"qualified, but exposure limit blocks an order: {ctx.exposure_reason}")

    if ctx.run_mode == "ALERT_ONLY":
        return Decision(state=DecisionState.ALERT, score=score, gates=gates,
                        reason=f"score {score.total:.1f}; all risk gates passed; alert-only mode")

    if ctx.run_mode == "PAPER" or score.total < c.score_live_buy_min:
        return Decision(state=DecisionState.PAPER_BUY, score=score, gates=gates,
                        reason=f"score {score.total:.1f}; all risk gates passed; simulated only")

    # 8 — clean.
    return Decision(state=DecisionState.LIVE_BUY, score=score, gates=gates,
                    reason=f"score {score.total:.1f}, all risk gates passed, {stability_reason}, "
                           f"OKX instrument {snapshot.okx_inst_id} available")
