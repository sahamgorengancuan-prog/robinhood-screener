"""End-of-cycle digest.

The digest exists because silence from a screener that rejects everything looks
exactly like a screener that has stopped running. The risk it introduces is that
a ranking of rejected tokens reads as a buy list, so most of these tests are
about the message refusing to be misread.
"""

from __future__ import annotations

import asyncio

import pytest

from app.alerts.digest import build_digest, rank_results, send_digest
from app.config import get_settings


def row(symbol, score, state="REJECT", reason="HARD gate failure: liquidity too low", **over):
    r = {
        "symbol": symbol,
        "token": "0x" + symbol.lower().ljust(40, "a")[:40],
        "score": score,
        "state": state,
        "reason": reason,
    }
    r.update(over)
    return r


def digest(results, **over):
    kwargs = dict(tokens_screened=len(results), duration_s=79.4, top_n=3)
    kwargs.update(over)
    return build_digest(results, **kwargs)


# ------------------------------------------------------------------ ranking
def test_top_three_are_the_three_highest_scores():
    out = rank_results([row("A", 10.0), row("B", 90.0), row("C", 50.0), row("D", 70.0)], 3)
    assert [r["symbol"] for r in out] == ["B", "D", "C"]


def test_a_token_with_no_score_is_not_ranked_as_zero():
    """An errored token has no score. Treating that as 0.0 would rank it against
    tokens that were actually measured."""
    out = rank_results([row("OK", 5.0), {"symbol": "ERR", "state": "ERROR", "token": "0xerr"}], 3)
    assert [r["symbol"] for r in out] == ["OK"]


def test_fewer_than_three_results_still_produce_a_digest():
    assert "1. A" in digest([row("A", 30.0)])


def test_no_scored_results_produce_no_message():
    assert digest([]) is None
    assert digest([{"symbol": "ERR", "state": "ERROR", "token": "0x"}]) is None


def test_top_n_is_configurable():
    text = digest([row(s, float(i)) for i, s in enumerate("ABCDE")], top_n=2)
    assert "2. D" in text
    assert "3. " not in text


# --------------------------------------------------------- refusing misreading
def test_an_all_rejected_digest_says_so_in_words():
    """The single most important line in the message."""
    text = digest([row("A", 73.0), row("B", 56.9), row("C", 47.5)])
    assert "Tidak ada yang lolos" in text
    assert "bukan rekomendasi beli" in text


def test_every_rejected_entry_carries_its_rejection_reason():
    text = digest([row("A", 73.0, reason="HARD gate failure: contract scan unavailable")])
    assert "contract scan unavailable" in text


def test_each_entry_shows_its_state():
    text = digest([row("A", 73.0, state="REJECT"), row("B", 60.0, state="WATCH")])
    assert "REJECT" in text and "WATCH" in text


def test_a_mixed_digest_does_not_claim_everything_was_rejected():
    text = digest([row("A", 73.0, state="ALERT"), row("B", 56.0, state="REJECT")])
    assert "Tidak ada yang lolos" not in text


def test_the_message_states_it_creates_no_orders():
    assert "Tidak ada order" in digest([row("A", 10.0)])


# ------------------------------------------------------- unreliable rankings
def test_dead_endpoints_mark_the_ranking_untrustworthy():
    """A score computed with half its inputs missing ranks the tokens whose data
    happened to arrive, not the tokens that are best. The operator's own cycle
    ranked ten tokens while six OKX endpoints were returning 401."""
    text = digest([row("A", 73.0)], dead_connections=[
        "okx_market POST /api/v6/dex/market/price-info",
        "okx_market GET /api/v6/dex/market/token/holder",
    ])
    assert "TIDAK DAPAT DIPERCAYA" in text
    assert "price-info" in text


def test_a_healthy_cycle_carries_no_unreliability_warning():
    assert "TIDAK DAPAT DIPERCAYA" not in digest([row("A", 73.0)], dead_connections=[])


def test_a_long_dead_list_is_truncated_not_dumped():
    text = digest([row("A", 1.0)], dead_connections=[f"src{i}" for i in range(9)])
    assert "dan 6 lainnya" in text


# ------------------------------------------------------------------ rendering
def test_long_reasons_are_truncated():
    text = digest([row("A", 10.0, reason="x" * 500)])
    assert "…" in text
    assert "x" * 200 not in text


def test_addresses_are_shortened_but_still_identifiable():
    text = digest([row("A", 10.0, token="0x0bd7d308f8e1639fab988df18a8011f41eacad73")])
    assert "0x0bd7d3" in text and "acad73" in text
    assert "0x0bd7d308f8e1639fab988df18a8011f41eacad73" not in text


def test_a_missing_symbol_does_not_crash():
    text = digest([{"token": "0xabc", "score": 5.0, "state": "REJECT", "reason": "r"}])
    assert "?" in text


# --------------------------------------------------------------- delivery
def test_digest_is_skipped_when_disabled():
    c = get_settings().model_copy(update={"cycle_digest_enabled": False})
    assert asyncio.run(send_digest([row("A", 10.0)], c, tokens_screened=1, duration_s=1.0)) is False


def test_delivery_failure_never_breaks_the_cycle(monkeypatch):
    """A report must not be able to take down the thing it reports on."""
    import app.alerts.sinks as sinks

    async def boom(*a, **k):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(sinks, "_send_telegram", boom)
    c = get_settings().model_copy(update={
        "cycle_digest_enabled": True, "telegram_bot_token": "t", "telegram_chat_id": "c",
    })
    assert asyncio.run(send_digest([row("A", 10.0)], c, tokens_screened=1, duration_s=1.0)) is False


def test_digest_creates_no_alert_row():
    """It is a report. It must not enter the alert ledger or the dedupe path."""
    import inspect

    import app.alerts.digest as d

    source = inspect.getsource(d)
    assert "Alert(" not in source
    assert "dedupe" not in source.replace("dedupes against", "")


def test_the_cycle_wires_the_digest_after_the_summary():
    import inspect

    import app.pipeline.ingest as ingest

    src = inspect.getsource(ingest.run_cycle)
    assert "send_digest(" in src
    assert "dead_connections" in src, "the digest must know which upstreams were dark"
