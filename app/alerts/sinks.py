"""Alert delivery.

Sinks are independent and best-effort: a dead Telegram token must never stop the
console log or, worse, break the screening loop. Every sink reports its own
success into `delivered` so the DB records what actually went out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Alert
from app.schemas import Decision, DecisionState, NormalizedSnapshot
from app.alerts.formatter import build_body, build_payload, build_title, dedupe_key
from app.util.console import safe

log = logging.getLogger(__name__)

STATE_ORDER = {
    "REJECT": 0, "WATCH": 1, "ALERT": 2, "PAPER_BUY": 3, "LIVE_BUY": 4,
}


def should_alert(decision: Decision, c: Settings) -> bool:
    threshold = STATE_ORDER.get(c.alert_min_state.upper(), 2)
    return STATE_ORDER.get(decision.state.value, 0) >= threshold


async def _send_console(title: str, body: str) -> bool:
    # Alert bodies contain arrows and box characters; a default Windows console
    # cannot encode them and would raise mid-cycle.
    print("\n" + safe(body) + "\n", flush=True)
    return True


async def _send_file(path: str, body: str) -> bool:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(body + "\n\n")
        return True
    except OSError as e:
        log.error("alert file sink failed: %s", e)
        return False


async def _send_webhook(url: str, payload: dict[str, Any]) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(url, json=payload)
            return r.status_code < 400
    except Exception as e:  # noqa: BLE001
        log.error("webhook sink failed: %s", e)
        return False


async def _send_telegram(token: str, chat_id: str, body: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"<pre>{_escape(body[:3800])}</pre>",
                      "parse_mode": "HTML", "disable_web_page_preview": True},
            )
            return r.status_code < 400
    except Exception as e:  # noqa: BLE001
        log.error("telegram sink failed: %s", e)
        return False


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def dispatch(
    session: Session,
    snapshot: NormalizedSnapshot,
    decision: Decision,
    c: Settings,
    token_id: int | None = None,
    evaluation_id: int | None = None,
    force: bool = False,
) -> Alert | None:
    if not force and not should_alert(decision, c):
        return None

    key = dedupe_key(snapshot, decision)
    existing = session.execute(select(Alert).where(Alert.dedupe_key == key)).scalar_one_or_none()
    if existing is not None and not force:
        log.debug("alert suppressed (duplicate) %s", key)
        return None

    title = build_title(snapshot, decision)
    body = build_body(snapshot, decision)
    payload = build_payload(snapshot, decision)

    tasks: dict[str, Any] = {}
    if c.alert_console:
        tasks["console"] = _send_console(title, body)
    if c.alert_file:
        tasks["file"] = _send_file(c.alert_file, body)
    if c.alert_webhook_url:
        tasks["webhook"] = _send_webhook(c.alert_webhook_url, payload)
    if c.telegram_bot_token and c.telegram_chat_id:
        tasks["telegram"] = _send_telegram(c.telegram_bot_token, c.telegram_chat_id, body)

    delivered: dict[str, bool] = {}
    if tasks:
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, res in zip(tasks.keys(), results):
            delivered[name] = bool(res) if not isinstance(res, Exception) else False
            if isinstance(res, Exception):
                log.error("sink %s raised: %s", name, res)

    alert = Alert(
        token_id=token_id,
        evaluation_id=evaluation_id,
        state=decision.state.value,
        dedupe_key=key,
        title=title,
        body=body,
        payload=payload,
        delivered=delivered,
    )
    session.add(alert)
    session.flush()
    return alert


def render_json(snapshot: NormalizedSnapshot, decision: Decision) -> str:
    return json.dumps(build_payload(snapshot, decision), indent=2, default=str)
