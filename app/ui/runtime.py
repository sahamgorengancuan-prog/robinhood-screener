"""Background scheduler control for the UI.

The control panel can start and stop the continuous screening loop in its own
process, so `START.bat` is genuinely the only thing an operator has to launch.

The scheduler runs with `max_instances=1` and `coalesce=True`, so a slow cycle
queues rather than overlapping — two concurrent cycles would double-count
exposure against the daily cap.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "scheduler": None,
    "services": None,
    "started_at": None,
    "last_run": None,
    "last_summary": None,
    "runs": 0,
    "errors": 0,
}


def is_running() -> bool:
    sched = _state["scheduler"]
    return bool(sched and getattr(sched, "running", False))


def status() -> dict[str, Any]:
    return {
        "running": is_running(),
        "started_at": _state["started_at"],
        "last_run": _state["last_run"],
        "runs": _state["runs"],
        "errors": _state["errors"],
        "last_summary": _state["last_summary"],
    }


async def _job() -> None:
    from app.pipeline.ingest import run_cycle

    try:
        summary = await run_cycle(_state["services"])
        _state["last_summary"] = summary
        _state["runs"] += 1
    except Exception as e:  # noqa: BLE001 - a bad cycle must not kill the loop
        _state["errors"] += 1
        log.exception("scheduled cycle failed: %s", e)
    finally:
        _state["last_run"] = dt.datetime.now(dt.timezone.utc)


def start(interval_s: int | None = None) -> tuple[bool, str]:
    """Start the loop. Returns (ok, message).

    **Must be called from inside a running event loop.** `AsyncIOScheduler`
    binds to the loop that is current when it starts; called from a worker
    thread it raises "no running event loop". Gradio runs sync handlers in a
    threadpool, so the UI handler that calls this has to be `async def`.
    """
    with _lock:
        if is_running():
            return False, "Scheduler sudah berjalan."

        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        from app.config import get_settings
        from app.services import build_services

        c = get_settings()
        every = int(interval_s or c.ingest_interval_s)
        if every < 30:
            return False, "Interval minimum 30 detik."

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False, ("Tidak ada event loop yang berjalan — panggil start() dari "
                           "handler async, bukan dari thread biasa.")

        try:
            _state["services"] = build_services(c)
            sched = AsyncIOScheduler(timezone=c.timezone)
            sched.add_job(
                _job,
                trigger=IntervalTrigger(seconds=every),
                id="screen_cycle",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
                next_run_time=dt.datetime.now(dt.timezone.utc),  # run immediately
            )
            sched.start()
        except Exception as e:  # noqa: BLE001
            _state["scheduler"] = None
            return False, f"Gagal memulai scheduler: {e}"

        _state["scheduler"] = sched
        _state["started_at"] = dt.datetime.now(dt.timezone.utc)
        return True, f"Scheduler berjalan setiap {every} detik."


def stop() -> tuple[bool, str]:
    with _lock:
        sched = _state["scheduler"]
        if not sched:
            return False, "Scheduler tidak sedang berjalan."
        try:
            sched.shutdown(wait=False)
        except Exception as e:  # noqa: BLE001
            log.warning("scheduler shutdown raised: %s", e)
        _state["scheduler"] = None
        _state["started_at"] = None
        return True, "Scheduler dihentikan. Screening otomatis berhenti."


def reset_for_tests() -> None:
    _state.update({"scheduler": None, "services": None, "started_at": None,
                   "last_run": None, "last_summary": None, "runs": 0, "errors": 0})
