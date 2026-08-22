from __future__ import annotations

import pytest

from app.pipeline.decision import DecisionContext, decide
from app.pipeline.risk import evaluate_gates
from app.pipeline.scoring import score_snapshot
from app.schemas import DecisionState, NormalizedSnapshot, TokenRef
from tests.conftest import make_snapshot


def run(snap, settings, **ctx_kwargs):
    gates = evaluate_gates(snap, settings)
    score = score_snapshot(snap, settings)
    ctx = DecisionContext(
        consecutive_passes=settings.stability_required_snapshots,
        recent_scores=[score.total] * 3,
        run_mode=settings.run_mode,
        okx_available=snap.okx_available,
        **ctx_kwargs,
    )
    return decide(snap, gates, score, ctx, settings)


def live_settings(settings):
    return settings.model_copy(update={"run_mode": "LIVE"})


# ------------------------------------------------------------------ defaults
def test_default_is_reject_on_empty_data(now, settings):
    empty = NormalizedSnapshot(token=TokenRef(address="0x" + "00" * 20), captured_at=now)
    d = run(empty, settings)
    # Empty data hits DATA gates first -> WATCH, never a buy.
    assert d.state in (DecisionState.WATCH, DecisionState.REJECT)
    assert d.state.rank < DecisionState.PAPER_BUY.rank


def test_hard_failure_beats_a_perfect_score(now, settings):
    snap = make_snapshot(now, top1_holder_pct=45.0)
    d = run(snap, settings)
    assert d.state == DecisionState.REJECT
    assert d.hard_fail
    assert "HARD gate failure" in d.reason


def test_missing_holder_data_is_explicit_reject(now, settings):
    snap = make_snapshot(now, unique_holders=None)
    d = run(snap, settings)
    assert d.state == DecisionState.REJECT
    assert d.hard_fail


def test_hard_failure_takes_precedence_over_missing_data(now, settings):
    snap = make_snapshot(now, unique_holders=None, top1_holder_pct=90.0)
    d = run(snap, settings)
    assert d.state == DecisionState.REJECT
    assert d.hard_fail


# -------------------------------------------------------------- alert / paper
def test_clean_token_in_alert_only_mode_stops_at_alert(now, settings, good_snapshot):
    d = run(good_snapshot, settings)
    assert d.state == DecisionState.ALERT
    assert "alert-only" in d.reason


def test_unstable_score_downgrades_to_alert(now, settings, good_snapshot):
    gates = evaluate_gates(good_snapshot, settings)
    score = score_snapshot(good_snapshot, settings)
    ctx = DecisionContext(consecutive_passes=1, recent_scores=[score.total], run_mode="LIVE",
                          okx_available=True)
    d = decide(good_snapshot, gates, score, ctx, settings)
    assert d.state == DecisionState.ALERT
    assert "consecutive clean snapshots" in d.reason


def test_wildly_swinging_score_downgrades_to_alert(now, settings, good_snapshot):
    gates = evaluate_gates(good_snapshot, settings)
    score = score_snapshot(good_snapshot, settings)
    ctx = DecisionContext(
        consecutive_passes=5,
        recent_scores=[40.0, 90.0, 55.0, 88.0],  # stdev far above the limit
        run_mode="LIVE",
        okx_available=True,
    )
    d = decide(good_snapshot, gates, score, ctx, settings)
    assert d.state == DecisionState.ALERT
    assert "unstable" in d.reason


# ------------------------------------------------------------------ live buy
def test_live_buy_requires_live_mode(now, good_snapshot, settings):
    d = run(good_snapshot, live_settings(settings))
    assert d.state == DecisionState.LIVE_BUY


def test_token_absent_from_okx_never_reaches_live(now, settings):
    snap = make_snapshot(now, okx_available=False, okx_inst_id=None, spread_bps=None)
    d = run(snap, live_settings(settings))
    assert d.state == DecisionState.ALERT
    assert "on-chain only" in d.reason


def test_unresolved_okx_availability_never_reaches_live(now, settings):
    snap = make_snapshot(now, okx_available=None, okx_inst_id=None, spread_bps=None)
    d = run(snap, live_settings(settings))
    assert d.state == DecisionState.ALERT
    assert "unresolved" in d.reason


def test_kill_switch_blocks_live_buy(now, good_snapshot, settings):
    d = run(good_snapshot, live_settings(settings), kill_switch=True)
    assert d.state == DecisionState.ALERT
    assert "kill switch" in d.reason


def test_safe_mode_blocks_live_buy(now, good_snapshot, settings):
    d = run(good_snapshot, live_settings(settings), safe_mode=True,
            safe_mode_reason="BTC-USDT moved -7.2%")
    assert d.state == DecisionState.ALERT
    assert "safe mode" in d.reason


def test_exposure_cap_blocks_live_buy(now, good_snapshot, settings):
    d = run(good_snapshot, live_settings(settings), exposure_ok=False,
            exposure_reason="daily cap reached")
    assert d.state == DecisionState.ALERT
    assert "exposure limit" in d.reason


def test_live_only_gate_blocks_live_and_requires_review(now, settings):
    snap = make_snapshot(now, contract_flags=["UPGRADEABLE_PROXY"])
    d = run(snap, live_settings(settings))
    assert d.state == DecisionState.ALERT
    assert "mutable contract surface" in d.reason


def test_score_below_live_threshold_stops_at_paper(now, settings):
    c = live_settings(settings).model_copy(update={"score_live_buy_min": 99.5})
    d = run(make_snapshot(now), c)
    assert d.state == DecisionState.PAPER_BUY
    assert "simulated only" in d.reason


# ---------------------------------------------------------------- thresholds
def test_low_score_is_rejected_even_with_clean_gates(now, settings):
    c = settings.model_copy(update={"score_alert_min": 99.9})
    d = run(make_snapshot(now), c)
    assert d.state == DecisionState.REJECT
    assert "below alert threshold" in d.reason


def test_mid_score_alerts_but_does_not_buy(now, settings):
    score = score_snapshot(make_snapshot(now), settings).total
    c = settings.model_copy(update={"score_alert_min": score - 1, "score_paper_buy_min": score + 1})
    d = run(make_snapshot(now), c)
    assert d.state == DecisionState.ALERT


@pytest.mark.parametrize(
    "state,expected_rank",
    [(DecisionState.REJECT, 0), (DecisionState.WATCH, 1), (DecisionState.ALERT, 2),
     (DecisionState.PAPER_BUY, 3), (DecisionState.LIVE_BUY, 4)],
)
def test_state_ordering(state, expected_rank):
    assert state.rank == expected_rank


# ------------------------------------------------------- root-cause ordering
def test_structural_failure_is_reported_before_its_symptoms(now, settings):
    """A pool share has no pool and one holder by construction. Leading the
    rejection with "liquidity_usd unavailable" sends an operator to check an API
    that is working perfectly."""
    snap = make_snapshot(
        now, contract_flags=["NOT_A_TRADEABLE_TOKEN:LP_SHARE"],
        liquidity_usd=None, slippage_bps=None, unique_holders=1,
        top1_holder_pct=100.0, top10_holder_pct=100.0,
    )
    d = run(snap, settings)
    assert d.state == DecisionState.REJECT
    body = d.reason.split("HARD gate failure: ", 1)[1]
    assert body.startswith("not a tradeable token"), body[:120]


def test_ordering_does_not_drop_or_duplicate_any_failure(now, settings):
    from app.pipeline.decision import root_cause_first
    from app.pipeline.risk import summarize

    snap = make_snapshot(now, contract_flags=["NOT_A_TRADEABLE_TOKEN:LP_SHARE"],
                         liquidity_usd=None, slippage_bps=None, top1_holder_pct=99.0)
    hard = summarize(evaluate_gates(snap, settings))["hard"]
    assert sorted(g.name for g in root_cause_first(hard)) == sorted(g.name for g in hard)


def test_ordering_is_a_no_op_when_no_root_cause_gate_failed(now, settings):
    from app.pipeline.decision import root_cause_first
    from app.pipeline.risk import summarize

    snap = make_snapshot(now, top1_holder_pct=45.0, liquidity_usd=1_000.0)
    hard = summarize(evaluate_gates(snap, settings))["hard"]
    assert [g.name for g in root_cause_first(hard)] == [g.name for g in hard]


# ----------------------------------------------------- reason string hygiene
def test_gates_blocked_by_the_same_input_say_it_once(now, settings):
    """tradeable_token and contract_flags both need the bytecode scan. A node
    returning nothing produced the identical sentence twice, and because the
    reason is truncated for display the duplicate pushed the *other* failures
    off the end — exactly the ones that would have said something new."""
    snap = make_snapshot(now, contract_flags=[], is_proxy=None)
    d = run(snap, settings)
    assert d.reason.count("contract bytecode scan unavailable") == 1


def test_a_fail_closed_reason_names_the_upstream_to_fix(now, settings):
    """"contract bytecode scan unavailable" reads as a verdict about the token
    when it is really a verdict about the node."""
    snap = make_snapshot(now, contract_flags=[], is_proxy=None, liquidity_usd=None,
                         slippage_bps=None, contract_verified=None)
    d = run(snap, settings)
    assert "[source: rh_node eth_getCode]" in d.reason
    assert "[source: explorer / Blockscout]" in d.reason


def test_dedupe_preserves_order_and_drops_nothing_distinct():
    from app.pipeline.decision import distinct_reasons

    class G:
        def __init__(self, reason):
            self.reason = reason

    out = distinct_reasons([G("a"), G("b"), G("a"), G("c"), G("b")])
    assert out == ["a", "b", "c"]


def test_unlock_reason_says_no_api_can_supply_it(now, settings):
    """The one dependency no key unlocks. An operator hunting for an API to buy
    would be hunting for something that does not exist."""
    snap = make_snapshot(now, days_to_major_unlock=None)
    d = run(snap, settings)
    assert "manual review, no API" in d.reason
