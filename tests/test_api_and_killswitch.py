"""Smoke tests for the API surface and the kill switch.

These guard properties that are easy to break accidentally: secret redaction,
the absence of an order-placing endpoint, and the kill switch's fail-closed
behaviour.
"""

from __future__ import annotations

import os

import pytest

from app import db as app_db
from app.config import reload_settings


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """A fully isolated app instance: temp DB, temp kill-switch file, no keys."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL_SWITCH"))
    monkeypatch.setenv("RUN_MODE", "ALERT_ONLY")
    monkeypatch.setenv("KILL_SWITCH", "false")
    monkeypatch.setenv("ALERT_CONSOLE", "false")
    monkeypatch.setenv("ALERT_FILE", "")
    monkeypatch.setenv("OKX_API_SECRET", "super-secret-value")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-bot-token")

    reload_settings()
    app_db.reset_engine()
    app_db.init_db()
    yield tmp_path
    app_db.reset_engine()
    reload_settings()


# ------------------------------------------------------------- kill switch
def test_kill_switch_starts_clear(app_env):
    from app.execution import killswitch

    engaged, _ = killswitch.is_killed()
    assert engaged is False


def test_kill_switch_file_engages_without_restart(app_env):
    from app.execution import killswitch

    (app_env / "KILL_SWITCH").write_text("stop")
    engaged, reason = killswitch.is_killed()
    assert engaged is True
    assert "file" in reason


def test_kill_switch_via_db_flag(app_env):
    from app.execution import killswitch

    killswitch.engage("unit test")
    engaged, reason = killswitch.is_killed()
    assert engaged is True and "unit test" in reason

    killswitch.release("test")
    engaged, _ = killswitch.is_killed()
    assert engaged is False


def test_kill_switch_fails_closed_when_the_check_breaks(app_env, monkeypatch):
    """If we cannot determine the kill switch state, we must assume engaged."""
    from app.execution import killswitch

    monkeypatch.setattr(killswitch, "get_settings", lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
    engaged, reason = killswitch.is_killed()
    assert engaged is True
    assert "failing closed" in reason


def test_safe_mode_round_trip(app_env):
    from app.execution import killswitch

    active, _ = killswitch.safe_mode_status()
    assert active is False

    killswitch.set_safe_mode(True, "BTC-USDT moved -7%", ttl_minutes=30)
    active, reason = killswitch.safe_mode_status()
    assert active is True and "BTC-USDT" in reason

    killswitch.set_safe_mode(False)
    active, _ = killswitch.safe_mode_status()
    assert active is False


def test_safe_mode_expires(app_env):
    from app.execution import killswitch

    killswitch.set_safe_mode(True, "stale", ttl_minutes=-1)  # already expired
    active, _ = killswitch.safe_mode_status()
    assert active is False


# --------------------------------------------------------------------- API
@pytest.fixture
def client(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    import app.scheduler as scheduler

    # No background scheduler in tests.
    monkeypatch.setattr(scheduler, "start_scheduler", lambda svc: None)
    monkeypatch.setattr(main, "start_scheduler", lambda svc: None)
    monkeypatch.setattr(main, "stop_scheduler", lambda: None)

    with TestClient(main.app) as c:
        yield c


def test_health_reports_no_live_trading_by_default(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["run_mode"] == "ALERT_ONLY"
    assert body["live_trading_possible"] is False


def test_config_endpoint_redacts_every_secret(client):
    cfg = client.get("/config").json()
    dumped = str(cfg)
    assert "super-secret-value" not in dumped
    assert "secret-bot-token" not in dumped
    assert cfg["okx_api_secret"] == "***"


def test_there_is_no_endpoint_that_places_an_order(client):
    """Orders may only originate from a decision that passed the gates."""
    paths = client.get("/openapi.json").json()["paths"]
    for path, methods in paths.items():
        for verb in methods:
            assert not (verb == "post" and "order" in path.lower()), f"{verb.upper()} {path}"


def test_add_token_grants_no_trading_permission(client):
    r = client.post("/tokens", json={"address": "0xdeadbeef", "symbol": "ABC"})
    assert r.status_code == 200 and r.json()["created"] is True
    # Adding it must not create an order of any kind.
    assert client.get("/orders").json() == []
    # Adding twice is idempotent.
    assert client.post("/tokens", json={"address": "0xdeadbeef"}).json()["created"] is False


def test_unknown_token_returns_404(client):
    assert client.get("/tokens/0xnotatoken").status_code == 404


def test_kill_switch_endpoints(client):
    assert client.post("/kill-switch/engage", json={"reason": "drill"}).json()["engaged"] is True
    assert client.get("/health").json()["kill_switch"]["engaged"] is True
    client.post("/kill-switch/release")
    assert client.get("/health").json()["kill_switch"]["engaged"] is False


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Robinhood Chain Token Screener" in r.text
