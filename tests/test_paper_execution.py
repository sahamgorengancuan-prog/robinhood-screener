"""Integration tests for paper execution against a real (in-memory) database.

These cover the parts that pure unit tests cannot: exposure accumulating across
orders, and the fill model behaving the way the docs claim.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.execution.paper import simulate_buy
from app.execution.portfolio import exposure_for
from app.models import Base, Evaluation, Token


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


@pytest.fixture
def token_and_eval(session):
    tok = Token(chain="robinhood", address="0x" + "ab" * 20, symbol="GOOD")
    session.add(tok)
    session.flush()
    ev = Evaluation(token_id=tok.id, score_total=85.0, state="PAPER_BUY", hard_fail=False,
                    insufficient_data=False)
    session.add(ev)
    session.flush()
    return tok, ev


def test_paper_buy_is_recorded_with_its_reason(session, token_and_eval, settings):
    tok, ev = token_and_eval
    rec, verdict = simulate_buy(
        session, tok, ev, "GOOD-USDT", bid=1.00, ask=1.001, reference_price=1.0005,
        c=settings, entry_reason="score 85.4, all gates passed",
    )
    assert rec is not None and verdict.allowed
    assert rec.mode == "PAPER"
    assert rec.side == "buy" and rec.ord_type == "post_only"
    # An order must never exist without the reasoning that produced it.
    assert rec.entry_reason == "score 85.4, all gates passed"
    assert rec.evaluation_id == ev.id


def test_paper_limit_sits_below_the_bid(session, token_and_eval, settings):
    tok, ev = token_and_eval
    rec, _ = simulate_buy(session, tok, ev, "GOOD-USDT", 1.00, 1.001, 1.0005, settings, "test")
    assert rec.limit_price < 1.00


def test_paper_notional_respects_the_clip_size(session, token_and_eval, settings):
    tok, ev = token_and_eval
    rec, _ = simulate_buy(session, tok, ev, "GOOD-USDT", 1.00, 1.001, 1.0005, settings, "test")
    assert rec.requested_usd <= settings.position_usd * 1.01


def test_exposure_accumulates_and_then_blocks(session, token_and_eval, settings):
    """Repeated buys on the same token must hit the per-token cap."""
    tok, ev = token_and_eval
    placed = 0
    for _ in range(10):
        rec, verdict = simulate_buy(session, tok, ev, "GOOD-USDT", 1.00, 1.001, 1.0005, settings, "test")
        if rec is None:
            assert any("cap" in r or "positions" in r or "order count" in r for r in verdict.reasons)
            break
        placed += 1
    else:
        pytest.fail("exposure cap never triggered")

    assert placed >= 1
    state = exposure_for(session, tok.id, "PAPER")
    assert state.token_exposure_usd <= settings.max_exposure_per_token_usd


def test_default_offset_fills_under_the_touch_assumption(session, token_and_eval, settings):
    tok, ev = token_and_eval
    rec, _ = simulate_buy(session, tok, ev, "GOOD-USDT", 1.00, 1.001, 1.0005, settings, "test")
    assert rec.status == "filled"
    assert rec.avg_fill_price == rec.limit_price
    assert rec.fee_usd > 0


def test_offset_beyond_the_touch_assumption_does_not_fill(session, token_and_eval, settings):
    tok, ev = token_and_eval
    c = settings.model_copy(update={"limit_offset_bps": 300, "paper_assumed_touch_bps": 60.0})
    rec, _ = simulate_buy(session, tok, ev, "GOOD-USDT", 1.00, 1.001, 1.0005, c, "test")
    assert rec.status == "unfilled"
    assert rec.filled_base == 0.0


def test_no_book_means_no_simulated_fill(session, token_and_eval, settings):
    tok, ev = token_and_eval
    rec, verdict = simulate_buy(session, tok, ev, "GOOD-USDT", None, None, 1.0, settings, "test")
    assert rec is None and not verdict.allowed


def test_paper_orders_do_not_consume_live_budget(session, token_and_eval, settings):
    tok, ev = token_and_eval
    simulate_buy(session, tok, ev, "GOOD-USDT", 1.00, 1.001, 1.0005, settings, "test")
    live_state = exposure_for(session, tok.id, "LIVE")
    assert live_state.token_exposure_usd == 0.0
    assert live_state.orders_today == 0


def test_wide_spread_prevents_a_paper_order_too(session, token_and_eval, settings):
    """Paper mode must apply the same safety checks, or its results are useless
    as a proxy for live behaviour."""
    tok, ev = token_and_eval
    rec, verdict = simulate_buy(session, tok, ev, "GOOD-USDT", 1.00, 1.10, 1.05, settings, "test")
    assert rec is None and not verdict.allowed
    assert any("spread" in r for r in verdict.reasons)
