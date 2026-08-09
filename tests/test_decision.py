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


def test_missing_data_routes_to_watch_not_reject(now, settings):
    snap = make_snapshot(now, unique_holders=None)
    d = run(snap, settings)
    assert d.state == DecisionState.WATCH
    assert d.insufficient_data


def test_hard_failure_takes_precedence_over_missing_data(now, settings):
    snap = make_snapshot(now, unique_holders=None, top1_holder_pct=90.0)
    d = run(snap, settings)
    assert d.state == DecisionState.REJECT
    assert d.hard_fail


# -------------------------------------------------------------- alert / paper
def test_clean_token_in_alert_only_mode_stops_at_paper(now, settings, good_snapshot):
    d = run(good_snapshot, settings)
    assert d.state == DecisionState.PAPER_BUY
    assert "run_mode=ALERT_ONLY" in d.reason


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
    assert d.state == DecisionState.PAPER_BUY
    assert "on-chain only" in d.reason


def test_unresolved_okx_availability_never_reaches_live(now, settings):
    snap = make_snapshot(now, okx_available=None, okx_inst_id=None, spread_bps=None)
    d = run(snap, live_settings(settings))
    assert d.state == DecisionState.PAPER_BUY
    assert "unresolved" in d.reason


def test_kill_switch_blocks_live_buy(now, good_snapshot, settings):
    d = run(good_snapshot, live_settings(settings), kill_switch=True)
    assert d.state == DecisionState.PAPER_BUY
    assert "kill switch" in d.reason


def test_safe_mode_blocks_live_buy(now, good_snapshot, settings):
    d = run(good_snapshot, live_settings(settings), safe_mode=True,
            safe_mode_reason="BTC-USDT moved -7.2%")
    assert d.state == DecisionState.PAPER_BUY
    assert "safe mode" in d.reason


def test_exposure_cap_blocks_live_buy(now, good_snapshot, settings):
    d = run(good_snapshot, live_settings(settings), exposure_ok=False,
            exposure_reason="daily cap reached")
    assert d.state == DecisionState.PAPER_BUY
    assert "exposure limit" in d.reason


def test_live_only_gate_blocks_live_but_still_papers(now, settings):
    snap = make_snapshot(now, contract_flags=["UPGRADEABLE_PROXY"])
    d = run(snap, live_settings(settings))
    assert d.state == DecisionState.PAPER_BUY
    assert "mutable contract surface" in d.reason


def test_score_below_live_threshold_stops_at_paper(now, settings):
    c = live_settings(settings).model_copy(update={"score_live_buy_min": 99.5})
    d = run(make_snapshot(now), c)
    assert d.state == DecisionState.PAPER_BUY
    assert "below live threshold" in d.reason


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
