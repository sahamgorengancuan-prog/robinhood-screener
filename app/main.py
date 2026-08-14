"""FastAPI service.

Read-mostly by design. The only state-changing endpoints are the kill switch,
a manual token add, and a manual cycle trigger — there is deliberately no
"place order" endpoint, because orders may only ever originate from a decision
that passed the gates.
"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session, init_db, session_scope
from app.execution import killswitch
from app.logging_conf import configure_logging
from app.models import Alert, Evaluation, OrderRecord, Token, TokenSnapshot
from app.pipeline.ingest import run_cycle
from app.scheduler import start_scheduler, stop_scheduler
from app.services import build_services

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    init_db()
    svc = build_services(settings)
    app.state.services = svc
    start_scheduler(svc)
    try:
        yield
    finally:
        stop_scheduler()
        await svc.aclose()


app = FastAPI(
    title="Robinhood Chain Token Screener",
    version="0.1.0",
    description="Risk-gated early-stage token screener with optional OKX execution.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------- health
@app.get("/health")
def health() -> dict[str, Any]:
    killed, kill_reason = killswitch.is_killed()
    safe, safe_reason = killswitch.safe_mode_status()
    return {
        "status": "ok",
        "run_mode": settings.run_mode,
        "kill_switch": {"engaged": killed, "reason": kill_reason},
        "safe_mode": {"active": safe, "reason": safe_reason},
        "okx_simulated": settings.okx_simulated,
        "live_trading_possible": settings.run_mode == "LIVE" and not killed and not safe,
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


@app.get("/config")
def config() -> dict[str, Any]:
    """Effective thresholds. Secrets are never returned."""
    c = settings.model_dump()
    for k in list(c):
        if any(s in k for s in ("secret", "passphrase", "api_key", "token", "chat_id")):
            c[k] = "***" if c[k] else ""
    return c


# --------------------------------------------------------------------- tokens
@app.get("/tokens")
def list_tokens(
    session: Session = Depends(get_session),
    limit: int = Query(100, le=500),
    state: str | None = None,
) -> list[dict[str, Any]]:
    latest_eval = (
        select(Evaluation.token_id, func.max(Evaluation.created_at).label("mx"))
        .group_by(Evaluation.token_id)
        .subquery()
    )
    q = (
        select(Token, Evaluation)
        .join(latest_eval, latest_eval.c.token_id == Token.id)
        .join(
            Evaluation,
            (Evaluation.token_id == Token.id) & (Evaluation.created_at == latest_eval.c.mx),
        )
        .order_by(desc(Evaluation.score_total))
        .limit(limit)
    )
    if state:
        q = q.where(Evaluation.state == state.upper())

    out = []
    for tok, ev in session.execute(q).all():
        out.append(
            {
                "address": tok.address,
                "symbol": tok.symbol,
                "name": tok.name,
                "score": ev.score_total,
                "state": ev.state,
                "okx_available": tok.okx_available,
                "okx_inst_id": tok.okx_inst_id,
                "contract_verified": tok.contract_verified,
                "evaluated_at": ev.created_at.isoformat(),
                "reason": ev.decision_reason,
            }
        )
    return out


@app.get("/tokens/{address}")
def token_detail(address: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    tok = session.execute(select(Token).where(Token.address == address)).scalar_one_or_none()
    if not tok:
        raise HTTPException(404, f"token {address} not tracked")

    snap = session.execute(
        select(TokenSnapshot)
        .where(TokenSnapshot.token_id == tok.id)
        .order_by(desc(TokenSnapshot.captured_at))
        .limit(1)
    ).scalar_one_or_none()
    ev = session.execute(
        select(Evaluation)
        .where(Evaluation.token_id == tok.id)
        .order_by(desc(Evaluation.created_at))
        .limit(1)
    ).scalar_one_or_none()

    return {
        "token": {
            "address": tok.address, "symbol": tok.symbol, "name": tok.name,
            "decimals": tok.decimals, "total_supply": tok.total_supply,
            "deployed_at": tok.deployed_at.isoformat() if tok.deployed_at else None,
            "contract_verified": tok.contract_verified, "is_proxy": tok.is_proxy,
            "okx_available": tok.okx_available, "okx_inst_id": tok.okx_inst_id,
        },
        "latest_snapshot": _snap_dict(snap) if snap else None,
        "latest_evaluation": _eval_dict(ev) if ev else None,
    }


@app.get("/tokens/{address}/history")
def token_history(
    address: str, session: Session = Depends(get_session), limit: int = Query(200, le=2000)
) -> list[dict[str, Any]]:
    tok = session.execute(select(Token).where(Token.address == address)).scalar_one_or_none()
    if not tok:
        raise HTTPException(404, f"token {address} not tracked")
    rows = session.execute(
        select(TokenSnapshot)
        .where(TokenSnapshot.token_id == tok.id)
        .order_by(desc(TokenSnapshot.captured_at))
        .limit(limit)
    ).scalars().all()
    return [_snap_dict(r) for r in rows]


class AddTokenRequest(BaseModel):
    address: str
    symbol: str | None = None
    name: str | None = None
    chain: str = "robinhood"


@app.post("/tokens")
def add_token(req: AddTokenRequest) -> dict[str, Any]:
    """Track a contract manually. Adding a token only schedules it for
    screening — it grants no trading permission of any kind."""
    with session_scope() as s:
        existing = s.execute(
            select(Token).where(Token.chain == req.chain, Token.address == req.address)
        ).scalar_one_or_none()
        if existing:
            return {"created": False, "address": existing.address}
        s.add(Token(chain=req.chain, address=req.address, symbol=req.symbol, name=req.name))
    return {"created": True, "address": req.address}


# ------------------------------------------------------------------ decisions
@app.get("/alerts")
def list_alerts(session: Session = Depends(get_session), limit: int = Query(50, le=200)) -> list[dict[str, Any]]:
    rows = session.execute(select(Alert).order_by(desc(Alert.created_at)).limit(limit)).scalars().all()
    return [
        {"created_at": a.created_at.isoformat(), "state": a.state, "title": a.title,
         "payload": a.payload, "delivered": a.delivered}
        for a in rows
    ]


@app.get("/orders")
def list_orders(session: Session = Depends(get_session), limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
    rows = session.execute(select(OrderRecord).order_by(desc(OrderRecord.created_at)).limit(limit)).scalars().all()
    return [
        {
            "created_at": o.created_at.isoformat(), "mode": o.mode, "inst_id": o.inst_id,
            "status": o.status, "client_order_id": o.client_order_id,
            "exchange_order_id": o.exchange_order_id, "requested_usd": o.requested_usd,
            "limit_price": o.limit_price, "size_base": o.size_base,
            "filled_base": o.filled_base, "avg_fill_price": o.avg_fill_price,
            "realized_slippage_bps": o.realized_slippage_bps,
            "entry_reason": o.entry_reason, "error": o.error,
        }
        for o in rows
    ]


# ---------------------------------------------------------------- kill switch
class KillRequest(BaseModel):
    reason: str = "manual"


@app.post("/kill-switch/engage")
def kill_engage(req: KillRequest) -> dict[str, Any]:
    killswitch.engage(req.reason)
    return {"engaged": True, "reason": req.reason}


@app.post("/kill-switch/release")
def kill_release() -> dict[str, Any]:
    killswitch.release("api")
    killed, reason = killswitch.is_killed()
    return {"engaged": killed, "reason": reason,
            "note": "env var and sentinel file switches are not cleared by this endpoint"}


@app.post("/run-cycle")
async def trigger_cycle() -> dict[str, Any]:
    """Run one screening cycle immediately (for testing and manual review)."""
    return await run_cycle(app.state.services)


# ----------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(session: Session = Depends(get_session)) -> str:
    tokens = list_tokens(session=session, limit=50)
    killed, kill_reason = killswitch.is_killed()

    rows = "".join(
        f"<tr><td>{t['symbol'] or '?'}</td><td class=mono>{t['address'][:18]}…</td>"
        f"<td class=score>{t['score']:.1f}</td>"
        f"<td class='state {t['state']}'>{t['state']}</td>"
        f"<td>{'yes' if t['okx_available'] else 'no'}</td>"
        f"<td class=reason>{(t['reason'] or '')[:160]}</td></tr>"
        for t in tokens
    ) or "<tr><td colspan=6>No evaluations yet. POST /run-cycle to screen.</td></tr>"

    return f"""<!doctype html><meta charset=utf-8>
<title>Robinhood Chain Screener</title>
<style>
 body{{font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:2rem;background:#0f1115;color:#d8dee9}}
 h1{{font-size:1.2rem}} table{{border-collapse:collapse;width:100%;margin-top:1rem}}
 th,td{{border-bottom:1px solid #262b33;padding:.45rem .6rem;text-align:left;vertical-align:top}}
 th{{color:#8b95a5;font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:.05em}}
 .mono{{color:#7f8a9b}} .score{{font-weight:700}} .reason{{color:#8b95a5;font-size:.85em}}
 .state{{font-weight:700}}
 .LIVE_BUY{{color:#a3e635}} .PAPER_BUY{{color:#38bdf8}} .ALERT{{color:#fbbf24}}
 .WATCH{{color:#94a3b8}} .REJECT{{color:#f87171}}
 .banner{{padding:.6rem .9rem;border-radius:6px;margin-bottom:1rem}}
 .warn{{background:#3b1d1d;color:#fca5a5}} .ok{{background:#16261a;color:#a3e635}}
</style>
<h1>Robinhood Chain Token Screener</h1>
<div class="banner {'warn' if killed else 'ok'}">
  mode: <b>{settings.run_mode}</b> &nbsp;|&nbsp; kill switch:
  <b>{'ENGAGED — ' + kill_reason if killed else 'clear'}</b> &nbsp;|&nbsp;
  OKX simulated: <b>{settings.okx_simulated}</b>
</div>
<table>
<tr><th>symbol</th><th>contract</th><th>score</th><th>state</th><th>okx</th><th>reason</th></tr>
{rows}
</table>
<p class=reason>Endpoints: /health /config /tokens /alerts /orders /run-cycle /docs</p>
"""


@app.get("/alerts/{alert_id}/text", response_class=PlainTextResponse)
def alert_text(alert_id: int, session: Session = Depends(get_session)) -> str:
    a = session.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    return a.body


# ------------------------------------------------------------------- helpers
def _snap_dict(s: TokenSnapshot) -> dict[str, Any]:
    return {
        "captured_at": s.captured_at.isoformat(),
        "price_usd": s.price_usd, "price_divergence_pct": s.price_divergence_pct,
        "liquidity_usd": s.liquidity_usd, "volume_24h": s.volume_24h, "volume_1h": s.volume_1h,
        "trade_sample_size": s.trade_sample_size,
        "unique_trader_ratio": s.unique_trader_ratio,
        "top_trader_volume_pct": s.top_trader_volume_pct,
        "filtered_trade_pct": s.filtered_trade_pct,
        "unique_holders": s.unique_holders, "top1_holder_pct": s.top1_holder_pct,
        "top10_holder_pct": s.top10_holder_pct, "holder_growth_24h_pct": s.holder_growth_24h_pct,
        "spread_bps": s.spread_bps, "slippage_bps": s.slippage_bps,
        "token_age_hours": s.token_age_hours, "price_change_24h_pct": s.price_change_24h_pct,
        "contract_flags": s.contract_flags, "tokenomics_flags": s.tokenomics_flags,
        "sniper_wallet_pct": s.sniper_wallet_pct,
        "bundled_buy_pct": s.bundled_buy_pct,
        "suspicious_holder_pct": s.suspicious_holder_pct,
        "missing_fields": s.missing_fields, "sources": s.sources,
    }


def _eval_dict(e: Evaluation) -> dict[str, Any]:
    return {
        "created_at": e.created_at.isoformat(), "state": e.state, "score_total": e.score_total,
        "components": {
            "liquidity": e.score_liquidity, "holders": e.score_holders, "volume": e.score_volume,
            "tokenomics": e.score_tokenomics, "momentum": e.score_momentum,
        },
        "hard_fail": e.hard_fail, "insufficient_data": e.insufficient_data,
        "gates": e.gate_results, "reason": e.decision_reason,
    }
