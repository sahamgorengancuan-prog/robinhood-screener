"""Diagnostics tests.

The property that matters most here: **diagnostics must never raise.** They are
what an operator runs when things are already broken, so a check that throws
instead of reporting FAIL is worse than useless.
"""

from __future__ import annotations

import pytest

from app.diagnostics import (
    FAIL,
    OK,
    SKIP,
    WARN,
    CheckResult,
    build_diagnostic_services,
    check_run_mode,
    readiness,
    run_all_checks,
    summarize_checks,
)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from app import db as app_db
    from app.config import reload_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'diag.db'}")
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL_SWITCH"))
    monkeypatch.setenv("KILL_SWITCH", "false")
    monkeypatch.setenv("RUN_MODE", "ALERT_ONLY")
    # No network endpoints at all — everything must SKIP or FAIL cleanly.
    monkeypatch.setenv("RH_NODE_RPC_URL", "")
    monkeypatch.setenv("RH_DATA_ENABLED", "false")
    monkeypatch.setenv("EXPLORER_ENABLED", "false")
    monkeypatch.setenv("OKX_MARKET_ENABLED", "false")
    monkeypatch.setenv("OKX_CEX_BASE_URL", "http://127.0.0.1:1")  # refused immediately
    monkeypatch.setenv("CHAINLINK_ENABLED", "false")

    reload_settings()
    app_db.reset_engine()
    app_db.init_db()
    yield tmp_path
    app_db.reset_engine()
    reload_settings()


# ------------------------------------------------------------- pure helpers
def test_summarize_counts_every_status():
    results = [
        CheckResult("a", "g", OK, ""), CheckResult("b", "g", OK, ""),
        CheckResult("c", "g", WARN, ""), CheckResult("d", "g", FAIL, ""),
        CheckResult("e", "g", SKIP, ""),
    ]
    assert summarize_checks(results) == {OK: 2, WARN: 1, FAIL: 1, SKIP: 1}


def test_check_result_row_shape():
    r = CheckResult("Node RPC", "Chain", OK, "connected", latency_ms=42.4)
    row = r.as_row()
    assert len(row) == 6
    assert row[0].endswith("OK") and "🟢" in row[0]
    assert row[3] == "42 ms"


def test_row_handles_missing_latency():
    assert CheckResult("x", "g", SKIP, "").as_row()[3] == "—"


def test_readiness_reports_not_ready_when_nothing_works():
    kind, msg = readiness([CheckResult("Node RPC", "g", FAIL, ""),
                           CheckResult("OKX Market", "g", FAIL, "")])
    assert kind == FAIL
    assert "WATCH" in msg  # tells the operator the safe consequence, not just "error"


def test_readiness_reports_alert_only_without_trading_auth():
    kind, msg = readiness([
        CheckResult("Node RPC", "g", OK, ""),
        CheckResult("OKX Market", "g", OK, ""),
        CheckResult("OKX trading auth", "g", SKIP, ""),
    ])
    assert kind == OK
    assert "ALERT_ONLY" in msg


def test_readiness_reports_paper_ready_when_all_green():
    kind, msg = readiness([
        CheckResult("Node RPC", "g", OK, ""),
        CheckResult("OKX Market", "g", OK, ""),
        CheckResult("OKX trading auth", "g", OK, ""),
    ])
    assert kind == OK
    assert "PAPER" in msg


def test_partial_readiness_is_warn_not_fail():
    kind, _ = readiness([CheckResult("Node RPC", "g", OK, ""),
                         CheckResult("OKX Market", "g", FAIL, "")])
    assert kind == WARN


# ------------------------------------------------------------- safety posture
def test_safety_posture_is_ok_when_live_is_impossible(isolated):
    r = check_run_mode(__import__("app.config", fromlist=["get_settings"]).get_settings())
    assert r.status == OK
    assert r.detail["live_trading_possible"] is False


def test_safety_posture_warns_when_live_is_possible(isolated, monkeypatch):
    from app.config import get_settings, reload_settings

    monkeypatch.setenv("RUN_MODE", "LIVE")
    reload_settings()
    r = check_run_mode(get_settings())
    assert r.status == WARN
    assert r.detail["live_trading_possible"] is True
    assert "LIVE ORDERS ARE POSSIBLE" in r.summary


def test_kill_switch_flips_safety_posture_back_to_ok(isolated, monkeypatch):
    from app.config import get_settings, reload_settings

    monkeypatch.setenv("RUN_MODE", "LIVE")
    reload_settings()
    (isolated / "KILL_SWITCH").write_text("stop")
    r = check_run_mode(get_settings())
    assert r.status == OK
    assert r.detail["live_trading_possible"] is False


# ------------------------------------------------------------------- runner
@pytest.mark.asyncio
async def test_run_all_checks_never_raises_with_everything_broken(isolated):
    results = await run_all_checks()
    assert results, "diagnostics returned nothing"
    assert all(isinstance(r, CheckResult) for r in results)
    # Database and safety posture are local, so they must still report OK.
    by_name = {r.name: r for r in results}
    assert by_name["Database"].status == OK
    assert by_name["Safety posture"].status == OK


@pytest.mark.asyncio
async def test_unreachable_host_reports_fail_not_exception(isolated):
    results = await run_all_checks()
    okx = next(r for r in results if r.name == "OKX instruments")
    assert okx.status == FAIL
    assert okx.fix, "a failing check must tell the operator what to do"


@pytest.mark.asyncio
async def test_disabled_sources_skip_with_a_reason(isolated):
    results = await run_all_checks()
    for name in ("Node RPC", "Data API", "Chainlink oracle"):
        r = next(x for x in results if x.name == name)
        assert r.status == SKIP
        assert r.fix, f"{name} skipped without explaining the consequence"


@pytest.mark.asyncio
async def test_diagnostics_do_not_retry(isolated):
    """A connection test must fail fast, not retry with backoff."""
    from app.config import get_settings

    svc = build_diagnostic_services(get_settings())
    try:
        assert svc.trade.max_retries == 0
        assert svc.node.max_retries == 0
        assert svc.market.max_retries == 0
    finally:
        await svc.aclose()
