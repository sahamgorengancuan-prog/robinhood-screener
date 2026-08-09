"""APScheduler wiring.

Two jobs, both with `max_instances=1` and `coalesce=True` so a slow cycle
queues rather than overlapping — concurrent cycles would double-count exposure.

APScheduler over cron/Celery because it needs no broker, no extra process, and
runs inside the same FastAPI process. That is the cheapest thing that works at
this cadence (minutes, not milliseconds).
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.pipeline.ingest import run_cycle
from app.services import Services

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _cycle_job(svc: Services) -> None:
    try:
        await run_cycle(svc)
    except Exception:  # noqa: BLE001 - the scheduler must survive a bad cycle
        log.exception("screening cycle raised")


def start_scheduler(svc: Services) -> AsyncIOScheduler:
    global _scheduler
    c = get_settings()
    if _scheduler is not None:
        return _scheduler

    sched = AsyncIOScheduler(timezone=c.timezone)
    sched.add_job(
        _cycle_job,
        trigger=IntervalTrigger(seconds=c.ingest_interval_s),
        args=[svc],
        id="screen_cycle",
        name="ingest + score + decide",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    sched.start()
    _scheduler = sched
    log.info("scheduler started: screening every %ds", c.ingest_interval_s)
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
