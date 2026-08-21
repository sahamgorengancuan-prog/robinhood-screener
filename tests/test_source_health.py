"""Tests for per-cycle source accounting.

The property that matters: a screener which fails closed reports "unavailable"
identically whether a token has no pool or the API is down. These tests pin the
distinction, and pin that no exception can vanish.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.pipeline.source_health import SourceReport, error_signature, run_sources


def test_ok_and_empty_are_counted_separately():
    """"no pool for this token" and "the API answered" are the same call, and
    only one of them means the screener saw something."""
    r = SourceReport()
    r.record("dexscreener", {"liquidity_usd": 5_821})
    r.record("dexscreener", None)
    assert r.sources["dexscreener"].ok == 1
    assert r.sources["dexscreener"].empty == 1
    assert r.sources["dexscreener"].failed == 0
    assert not r.sources["dexscreener"].silent


def test_a_source_that_only_ever_returns_nothing_is_not_silent():
    """Empty is a real answer. Only zero successes counts as blind."""
    r = SourceReport()
    for _ in range(5):
        r.record("dexscreener", None)
    assert r.sources["dexscreener"].silent
    assert r.silent_sources() == ["dexscreener"]


def test_failures_keep_the_error_text():
    r = SourceReport()
    r.failed("okx.price_info", RuntimeError("401 Invalid OK-ACCESS-KEY"))
    assert "401 Invalid OK-ACCESS-KEY" in r.sources["okx.price_info"].worst_error
    assert "RuntimeError" in r.sources["okx.price_info"].worst_error


def test_repeated_identical_failure_is_reported_once():
    """22 tokens hitting one dead endpoint produced 22 tracebacks, which buried
    the summary they were supposed to support."""
    r = SourceReport()
    first = [r.failed("gecko", RuntimeError("404 Not Found")) for _ in range(22)]
    assert first[0] is True
    assert not any(first[1:]), "only the first sighting should be loggable"
    assert r.sources["gecko"].failed == 22


def test_distinct_errors_are_each_reported_once():
    r = SourceReport()
    assert r.failed("okx", RuntimeError("401 bad key")) is True
    assert r.failed("okx", RuntimeError("429 rate limited")) is True
    assert r.failed("okx", RuntimeError("401 bad key")) is False


def test_error_signature_is_bounded():
    """A message carrying a request id must not spawn unbounded distinct keys."""
    sig = error_signature(RuntimeError("x" * 5_000))
    assert len(sig) < 200


def test_render_names_the_blind_sources():
    r = SourceReport()
    r.record("node", {"supply": 1})
    r.failed("geckoterminal", RuntimeError("404 Not Found"))
    text = r.render(tokens=22)
    assert "22 token(s)" in text
    assert "geckoterminal" in text and "FAILED" in text
    assert "produced nothing usable" in text
    assert "fails closed" in text


def test_render_is_safe_with_no_calls():
    assert "no upstream calls" in SourceReport().render()


# ----------------------------------------------------------------- run_sources
def test_run_sources_never_swallows_an_exception():
    """`gather(return_exceptions=True)` returns exceptions instead of raising
    them; the returned list used to be discarded, so anything that was not a
    handled ClientError disappeared with no log line at all."""
    r = SourceReport()

    async def parser_bug():
        return {"a": 1}["missing"]

    asyncio.run(run_sources(r, {"activity": parser_bug}))
    assert r.sources["activity"].failed == 1
    assert "KeyError" in r.sources["activity"].worst_error


def test_run_sources_keeps_going_when_one_source_dies():
    """One dead upstream must not cost the cycle its other sources."""
    r = SourceReport()
    reached = []

    async def dead():
        raise RuntimeError("boom")

    async def alive():
        reached.append(True)
        return {"ok": 1}

    asyncio.run(run_sources(r, {"dead": dead, "alive": alive}))
    assert reached == [True]
    assert r.sources["dead"].failed == 1


def test_run_sources_logs_the_first_failure(caplog):
    r = SourceReport()

    async def dead():
        raise RuntimeError("401 Invalid OK-ACCESS-KEY")

    with caplog.at_level(logging.WARNING):
        asyncio.run(run_sources(r, {"okx": dead}))
    assert any("401 Invalid OK-ACCESS-KEY" in rec.getMessage() for rec in caplog.records)
    assert any(rec.exc_info for rec in caplog.records), "traceback should accompany the first sighting"


def test_run_sources_does_not_treat_cancellation_as_a_source_failure():
    r = SourceReport()

    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_sources(r, {"x": cancelled}))
    assert r.sources == {}


# --------------------------------------------------------------- wiring guard
def test_ingest_does_not_use_bare_gather_with_return_exceptions():
    """That call silently discards every exception it collects."""
    import inspect

    import app.pipeline.ingest as ingest

    source = inspect.getsource(ingest)
    assert "return_exceptions=True" not in source, (
        "use run_sources() so failures are attributed and logged"
    )


def test_cycle_summary_exposes_source_health():
    import inspect

    import app.pipeline.ingest as ingest

    src = inspect.getsource(ingest.run_cycle)
    assert "silent_sources" in src and "report.log(" in src


# ------------------------------------------------------- connection accounting
def test_connection_failures_are_recorded_even_when_the_client_swallows_them():
    """Several clients turn a ClientError into None at their own boundary, so a
    403 became indistinguishable from "this token has nothing". The transport
    layer still knows, and now says so."""
    r = SourceReport()
    err = RuntimeError("403 Forbidden")
    assert r.http_failed("explorer", "GET", "/api/v2/tokens/0xabc123def/holders", err) is True
    assert r.http_failed("explorer", "GET", "/api/v2/tokens/0xdeadbeef99/holders", err) is False
    label = next(iter(r.connections))
    assert "{address}" in label, "addresses must collapse into one row"
    assert r.connections[label].failed == 2
    assert r.dead_connections() == [label]


def test_a_connection_that_answered_once_is_not_dead():
    r = SourceReport()
    r.http_ok("explorer", "GET", "/api/v2/tokens/0xabc123/counters")
    r.http_failed("explorer", "GET", "/api/v2/tokens/0xdef456/counters", RuntimeError("500"))
    assert r.dead_connections() == []
    assert len(r.failing_connections()) == 1


def test_render_separates_connection_health_from_data_coverage():
    r = SourceReport()
    r.http_failed("okx_market", "POST", "/api/v6/dex/market/price-info", RuntimeError("403"))
    r.record("dexscreener", None)
    text = r.render(tokens=22)
    assert "22 token(s)" in text
    assert "connections (is the endpoint answering)" in text
    assert "data coverage" in text
    assert "never answered once" in text


def test_http_recording_is_a_no_op_without_an_active_report():
    """Diagnostics must never require the code they watch to be restructured."""
    from app.pipeline.source_health import record_http_failure, record_http_ok

    record_http_ok("x", "GET", "/y")
    record_http_failure("x", "GET", "/y", RuntimeError("z"))  # must not raise


def test_use_report_scopes_the_recording():
    from app.pipeline.source_health import current_report, record_http_ok, use_report

    r = SourceReport()
    assert current_report() is None
    with use_report(r):
        assert current_report() is r
        record_http_ok("explorer", "GET", "/api/v2/x")
    assert current_report() is None
    assert len(r.connections) == 1


def test_the_http_layer_reports_through_the_real_client():
    """End to end: a failing request must appear in the report without the
    calling code doing anything."""
    import httpx

    from app.clients.base import BaseHTTPClient, ClientError
    from app.pipeline.source_health import use_report

    transport = httpx.MockTransport(lambda request: httpx.Response(403, text="Forbidden"))

    class Explorer(BaseHTTPClient):
        name = "explorer"

    client = Explorer("https://example.invalid", timeout_s=1.0, max_retries=0)
    client._client = httpx.AsyncClient(transport=transport, base_url="https://example.invalid")

    r = SourceReport()

    async def go():
        with use_report(r):
            with pytest.raises(ClientError):
                await client.request("GET", "/api/v2/tokens/0xabcdef123456/holders")
        await client.aclose()

    asyncio.run(go())
    label = next(iter(r.connections))
    assert "explorer GET /api/v2/tokens/{address}/holders" == label
    assert r.connections[label].failed == 1
    assert "403" in r.connections[label].worst_error


def test_cycle_summary_exposes_dead_connections():
    import inspect

    import app.pipeline.ingest as ingest

    src = inspect.getsource(ingest.run_cycle)
    assert "dead_connections" in src
    assert "use_report(report)" in src, "the HTTP layer only records inside the scope"
