"""Kill switch and safe mode.

The kill switch trips on **any** of three independent signals, and it fails
closed — if checking it raises, it reports "engaged":

  1. `KILL_SWITCH=true` in the environment      (needs a restart)
  2. presence of the `KILL_SWITCH` file          (instant, no restart, works
     even if the API is wedged: `touch KILL_SWITCH`)
  3. the `kill_switch` row in `kv_state`         (settable over the API)

Safe mode is separate and automatic: when a reference asset moves violently,
live buying is suspended while alerting continues.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import KVState, utcnow

log = logging.getLogger(__name__)

KEY_KILL = "kill_switch"
KEY_SAFE_MODE = "safe_mode"


def _kv_get(key: str) -> dict | None:
    try:
        with session_scope() as s:
            row = s.execute(select(KVState).where(KVState.key == key)).scalar_one_or_none()
            return dict(row.value) if row and row.value else None
    except Exception as e:  # noqa: BLE001
        log.error("kv_get(%s) failed: %s", key, e)
        return None


def _kv_set(key: str, value: dict) -> None:
    with session_scope() as s:
        row = s.execute(select(KVState).where(KVState.key == key)).scalar_one_or_none()
        if row is None:
            s.add(KVState(key=key, value=value))
        else:
            row.value = value
            row.updated_at = utcnow()


def is_killed() -> tuple[bool, str]:
    """Returns (engaged, reason). Fails closed on any error."""
    try:
        c = get_settings()
        if c.kill_switch:
            return True, "KILL_SWITCH=true in environment"
        if c.kill_switch_file and os.path.exists(c.kill_switch_file):
            return True, f"kill switch file present at {c.kill_switch_file}"
        row = _kv_get(KEY_KILL)
        if row and row.get("engaged"):
            return True, f"kill switch set via API: {row.get('reason', 'no reason given')}"
        return False, ""
    except Exception as e:  # noqa: BLE001
        return True, f"kill switch check failed ({e}) — failing closed"


def engage(reason: str) -> None:
    _kv_set(KEY_KILL, {"engaged": True, "reason": reason, "at": utcnow().isoformat()})
    log.critical("KILL SWITCH ENGAGED: %s", reason)


def release(actor: str = "api") -> None:
    _kv_set(KEY_KILL, {"engaged": False, "reason": f"released by {actor}", "at": utcnow().isoformat()})
    log.warning("kill switch released by %s (file/env switches, if set, still apply)", actor)


# ------------------------------------------------------------------ safe mode
def set_safe_mode(active: bool, reason: str = "", ttl_minutes: int = 60) -> None:
    _kv_set(
        KEY_SAFE_MODE,
        {
            "active": active,
            "reason": reason,
            "at": utcnow().isoformat(),
            "expires_at": (utcnow() + dt.timedelta(minutes=ttl_minutes)).isoformat() if active else None,
        },
    )


def safe_mode_status() -> tuple[bool, str]:
    row = _kv_get(KEY_SAFE_MODE)
    if not row or not row.get("active"):
        return False, ""
    exp = row.get("expires_at")
    if exp:
        try:
            if dt.datetime.fromisoformat(exp) < utcnow():
                return False, ""
        except ValueError:
            pass
    return True, row.get("reason", "safe mode active")


async def evaluate_safe_mode(trade_client) -> tuple[bool, str]:
    """Trip safe mode when the reference asset is moving too fast.

    A violent tape means every liquidity and slippage number we measured minutes
    ago is stale. Alerting continues; buying does not.
    """
    c = get_settings()
    try:
        move = await trade_client.price_change_1h_pct(c.safe_mode_ref_instrument)
    except Exception as e:  # noqa: BLE001
        # Cannot see the market -> assume the worst.
        set_safe_mode(True, f"reference price unavailable ({e})", ttl_minutes=15)
        return True, f"reference price unavailable ({e})"

    if move is None:
        set_safe_mode(True, "reference price unavailable", ttl_minutes=15)
        return True, "reference price unavailable"

    if abs(move) > c.safe_mode_ref_move_1h_pct:
        reason = f"{c.safe_mode_ref_instrument} moved {move:+.2f}% (limit ±{c.safe_mode_ref_move_1h_pct}%)"
        set_safe_mode(True, reason, ttl_minutes=60)
        return True, reason

    set_safe_mode(False)
    return False, ""
