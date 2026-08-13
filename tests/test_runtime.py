"""Tests for the in-UI scheduler control."""

from __future__ import annotations

import pytest

from app.ui import runtime


@pytest.fixture(autouse=True)
def clean_runtime():
    runtime.reset_for_tests()
    yield
    if runtime.is_running():
        runtime.stop()
    runtime.reset_for_tests()


def test_starts_stopped():
    assert runtime.is_running() is False
    assert runtime.status()["running"] is False


def test_stop_when_not_running_is_reported_not_raised():
    ok, msg = runtime.stop()
    assert ok is False
    assert "tidak sedang berjalan" in msg


def test_rejects_absurdly_short_interval():
    ok, msg = runtime.start(5)
    assert ok is False
    assert "minimum" in msg
    assert runtime.is_running() is False


@pytest.mark.asyncio
async def test_start_stop_cycle():
    """APScheduler's asyncio scheduler needs a running loop, hence the async test."""
    ok, msg = runtime.start(3600)
    assert ok, msg
    assert runtime.is_running() is True

    # Starting twice must not spawn a second loop that double-counts exposure.
    ok2, msg2 = runtime.start(3600)
    assert ok2 is False
    assert "sudah berjalan" in msg2

    ok3, _ = runtime.stop()
    assert ok3 is True
    assert runtime.is_running() is False


@pytest.mark.asyncio
async def test_status_reports_counters():
    runtime.start(3600)
    st = runtime.status()
    assert st["running"] is True
    assert st["runs"] == 0
    assert st["errors"] == 0
    assert st["started_at"] is not None
    runtime.stop()


@pytest.mark.asyncio
async def test_job_records_errors_without_raising(monkeypatch):
    """A failing cycle must increment the error count, not kill the loop."""
    import app.pipeline.ingest as ingest

    async def boom(*a, **k):
        raise RuntimeError("simulated cycle failure")

    monkeypatch.setattr(ingest, "run_cycle", boom)
    await runtime._job()
    assert runtime.status()["errors"] == 1
    assert runtime.status()["runs"] == 0


def test_start_outside_an_event_loop_is_refused_not_crashed():
    """Gradio runs sync handlers in a worker thread; AsyncIOScheduler needs the
    loop. Starting from a plain thread must report, not raise."""
    ok, msg = runtime.start(3600)
    assert ok is False
    assert "event loop" in msg
    assert runtime.is_running() is False


@pytest.mark.asyncio
async def test_ui_handlers_are_coroutines():
    """If these become sync again, the scheduler silently stops working."""
    import inspect

    from app.ui.gradio_app import do_start_scheduler, do_stop_scheduler

    assert inspect.iscoroutinefunction(do_start_scheduler)
    assert inspect.iscoroutinefunction(do_stop_scheduler)
