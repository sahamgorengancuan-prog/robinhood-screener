"""Gradio control panel — the single operator surface.

`START.bat` launches only this. Everything an operator needs happens here:

  0. Setup              — configure the whole system, written to `.env`
  1. Koneksi & API Test — prove every source works before trusting a number
  2. Screener           — run one cycle, or start the continuous loop
  3. Token Inspector    — evaluate one contract, read-only, nothing persisted
  4. Threshold Lab      — move a slider, watch the decision change instantly
  5. Risiko & Order     — kill switch, exposure, order ledger
  6. Konfigurasi        — effective settings, secrets redacted

What the UI can and cannot do
-----------------------------
It **can** write `.env` (that is what makes one-click setup possible) and it can
start and stop the screening loop. Writes preserve the file's comments and keep
a `.env.bak`, and a blank secret field never erases a stored one.

It **cannot** place an order. There is no order-submitting control anywhere in
this module, and a test asserts it never calls the execution functions. Orders
may only ever originate from a decision that passed the risk gates. The panel
can *stop* trading; it cannot *start* a trade.

Switching RUN_MODE to LIVE with real (non-simulated) keys is possible here, and
the save handler says so in the loudest terms it can — but the kill switch and
the exposure caps remain the only things standing between the bot and your
balance. That is the trade the convenience buys.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import html
import inspect
import json
import logging
from pathlib import Path

import gradio as gr
from sqlalchemy import desc, func, select

from app.config import Settings, get_settings, reload_settings
from app.db import init_db, session_scope
from app.diagnostics import (
    FAIL,
    OK,
    SKIP,
    WARN,
    build_diagnostic_services,
    readiness,
    run_all_checks,
    summarize_checks,
)
from app.execution import killswitch
from app.models import Alert, Evaluation, OrderRecord, Token, TokenSnapshot
from app.pipeline.decision import DecisionContext, decide
from app.pipeline.ingest import dry_run_token, run_cycle
from app.pipeline.risk import evaluate_gates
from app.pipeline.scoring import score_snapshot
from app.schemas import DecisionState, NormalizedSnapshot, Severity, TokenRef
from app.services import build_services
from app.ui import envfile, runtime
from app.ui.theme import CSS, banner, metric_cards, score_bars, state_pill, theme
from app.alerts.formatter import build_body

log = logging.getLogger(__name__)


# --------------------------------------------------------------- error guard
#: Marks the output slot that should receive the error banner in `guarded()`.
ERROR_SLOT = object()


def _error_banner(fn_name: str, exc: BaseException) -> str:
    # The message can contain anything, including a URL or a chunk of a
    # response body, so it is escaped before it goes into the banner's HTML.
    return banner(
        FAIL,
        f"<b>{html.escape(type(exc).__name__)}</b> di <code>{html.escape(fn_name)}</code>: "
        f"{html.escape(str(exc)) or '(tanpa pesan)'}"
        "<br><span style='opacity:.75'>Traceback lengkap ada di jendela terminal "
        "yang membuka panel ini.</span>",
    )


def guarded(fn, *fallback):
    """Render a handler's crash into the page instead of losing it to a toast.

    Gradio shows an unhandled exception as a bare red "Error" pill carrying no
    text at all — it hides the message unless `launch(show_error=True)`. In a
    panel whose entire purpose is diagnosing why a number looks wrong, a
    content-free "Error" is the least useful thing it could say, and it is
    exactly what an operator saw on a fresh Windows install.

    Wrapping happens at the wiring site rather than on the functions
    themselves, so the handlers stay ordinary callables that tests can call and
    let raise.

    `fallback` supplies one value per output component; the slot passed as
    `ERROR_SLOT` receives the error banner.
    """
    def _fallback(exc: BaseException):
        log.exception("panel handler %s failed", getattr(fn, "__name__", fn))
        name = getattr(fn, "__name__", "handler")
        values = tuple(
            _error_banner(name, exc) if v is ERROR_SLOT else v for v in fallback
        )
        return values[0] if len(values) == 1 else values

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — the whole point
                return _fallback(exc)
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — the whole point
            return _fallback(exc)
    return wrapper


def fmt_bps(v: float | None) -> str:
    """Basis points, with enough precision to stay honest.

    A deep pool gives sub-1bps slippage on a $25 clip; printing "0 bps" reads as
    "no cost measured" when it actually means "very small". Show two decimals
    below 1, one below 10.
    """
    if v is None:
        return "n/a"
    if v < 1:
        return f"{v:.2f} bps"
    if v < 10:
        return f"{v:.1f} bps"
    return f"{v:.0f} bps"


# ===========================================================================
# Service handling
# ===========================================================================
def settings_with_overrides(
    rpc_url: str = "", okx_key: str = "", okx_secret: str = "",
    okx_pass: str = "", okx_project: str = "", chain_id: str = "",
) -> Settings:
    """Apply in-session overrides on top of `.env`. Nothing is persisted."""
    c = get_settings()
    patch: dict = {}
    if rpc_url.strip():
        patch["rh_node_rpc_url"] = rpc_url.strip()
    if okx_key.strip():
        patch["okx_api_key"] = okx_key.strip()
    if okx_secret.strip():
        patch["okx_api_secret"] = okx_secret.strip()
    if okx_pass.strip():
        patch["okx_api_passphrase"] = okx_pass.strip()
    if okx_project.strip():
        patch["okx_project_id"] = okx_project.strip()
    if str(chain_id).strip():
        try:
            patch["rh_chain_id"] = int(str(chain_id).strip())
        except ValueError:
            pass
    return c.model_copy(update=patch) if patch else c


# ===========================================================================
# Tab 1 — Connection & API test
# ===========================================================================
async def do_connection_test(address, rpc_url, okx_key, okx_secret, okx_pass, okx_project, chain_id):
    c = settings_with_overrides(rpc_url, okx_key, okx_secret, okx_pass, okx_project, chain_id)
    address = (address or "").strip() or None

    svc = build_diagnostic_services(c)
    try:
        results = await run_all_checks(address, settings=c, svc=svc)
    finally:
        await svc.aclose()

    counts = summarize_checks(results)
    kind, message = readiness(results)

    head = (
        f"<b>{message}</b><br>"
        f"🟢 {counts[OK]} ok &nbsp;·&nbsp; 🟡 {counts[WARN]} warning &nbsp;·&nbsp; "
        f"🔴 {counts[FAIL]} gagal &nbsp;·&nbsp; ⚪ {counts[SKIP]} dilewati"
    )

    rows = [r.as_row(use_emoji=True) for r in results]

    # Only surface details that carry observed field names or a raw payload —
    # that is what you need to reconcile an unverified API contract.
    details = {
        r.name: r.detail for r in results
        if r.detail and any(k in r.detail for k in ("observed_keys", "parsed", "unparsed", "raw", "sample"))
    }

    fixes = [f"**{r.emoji} {r.name}** — {r.fix}" for r in results if r.fix]
    fix_md = "\n\n".join(fixes) if fixes else "_Tidak ada tindakan yang diperlukan._"

    return banner(kind, head), rows, details or {"info": "no field-mapping data returned"}, fix_md


async def do_quick_ping(rpc_url):
    """Single fast check: is the RPC alive at all?"""
    c = settings_with_overrides(rpc_url=rpc_url)
    if not c.rh_node_rpc_url:
        return banner(FAIL, "RH_NODE_RPC_URL kosong — isi di .env atau di kolom override di atas.")
    svc = build_diagnostic_services(c)
    try:
        import time

        t0 = time.perf_counter()
        cid = await svc.node.chain_id()
        blk = await svc.node.block_number()
        ms = (time.perf_counter() - t0) * 1000
        return banner(OK, f"<b>RPC hidup</b> — chain ID <b>{cid}</b>, block <b>{blk:,}</b>, {ms:.0f} ms")
    except Exception as e:  # noqa: BLE001
        return banner(FAIL, f"<b>RPC gagal:</b> {e}")
    finally:
        await svc.aclose()


async def do_alert_test():
    """Send a real test notification through every configured sink."""
    c = get_settings()
    from app.alerts import sinks

    active = []
    if c.alert_console:
        active.append("console")
    if c.alert_file:
        active.append(f"file ({c.alert_file})")
    if c.alert_webhook_url:
        active.append("webhook")
    if c.telegram_bot_token and c.telegram_chat_id:
        active.append("telegram")
    if not active:
        return banner(WARN, "Tidak ada sink alert yang aktif. Atur ALERT_WEBHOOK_URL atau TELEGRAM_* di .env.")

    body = (
        "[TEST] Robinhood Chain Screener\n"
        "=" * 40 + "\n"
        f"Uji koneksi alert pada {dt.datetime.now(dt.timezone.utc).isoformat()}\n"
        "Jika Anda membaca ini, jalur notifikasi bekerja."
    )
    delivered = {}
    if c.alert_webhook_url:
        delivered["webhook"] = await sinks._send_webhook(c.alert_webhook_url, {"test": True, "body": body})
    if c.telegram_bot_token and c.telegram_chat_id:
        delivered["telegram"] = await sinks._send_telegram(c.telegram_bot_token, c.telegram_chat_id, body)
    if c.alert_file:
        delivered["file"] = await sinks._send_file(c.alert_file, body)
    if c.alert_console:
        delivered["console"] = await sinks._send_console("test", body)

    failed = [k for k, v in delivered.items() if not v]
    if failed:
        return banner(FAIL, f"Sink gagal: <b>{', '.join(failed)}</b>. Berhasil: {', '.join(k for k,v in delivered.items() if v) or '—'}")
    return banner(OK, f"Semua sink berhasil menerima pesan uji: <b>{', '.join(delivered)}</b>")


# ===========================================================================
# Tab 2 — Screener
# ===========================================================================
def load_screener(state_filter: str, min_score: float):
    init_db()
    with session_scope() as s:
        latest = (
            select(Evaluation.token_id, func.max(Evaluation.created_at).label("mx"))
            .group_by(Evaluation.token_id).subquery()
        )
        q = (
            select(Token, Evaluation)
            .join(latest, latest.c.token_id == Token.id)
            .join(Evaluation, (Evaluation.token_id == Token.id) & (Evaluation.created_at == latest.c.mx))
            .order_by(desc(Evaluation.score_total))
        )
        if state_filter and state_filter != "ALL":
            q = q.where(Evaluation.state == state_filter)
        rows = []
        for tok, ev in s.execute(q).all():
            if ev.score_total < min_score:
                continue
            rows.append([
                ev.state, tok.symbol or "?", tok.address,
                round(ev.score_total, 1),
                round(ev.score_liquidity, 1), round(ev.score_holders, 1),
                round(ev.score_volume, 1), round(ev.score_tokenomics, 1), round(ev.score_momentum, 1),
                "yes" if tok.okx_available else ("no" if tok.okx_available is False else "?"),
                (ev.decision_reason or "")[:150],
            ])

        counts = dict(
            s.execute(
                select(Evaluation.state, func.count(Evaluation.id))
                .join(latest, (latest.c.token_id == Evaluation.token_id) & (latest.c.mx == Evaluation.created_at))
                .group_by(Evaluation.state)
            ).all()
        )

    cards = metric_cards([
        ("Tracked", str(sum(counts.values())), "token dievaluasi"),
        ("Live buy", str(counts.get("LIVE_BUY", 0)), "lolos semua gate"),
        ("Paper buy", str(counts.get("PAPER_BUY", 0)), "simulasi"),
        ("Alert", str(counts.get("ALERT", 0)), "watchlist"),
        ("Watch", str(counts.get("WATCH", 0)), "data kurang"),
        ("Reject", str(counts.get("REJECT", 0)), "gagal gate"),
    ])
    return rows, cards


async def do_run_cycle(progress=gr.Progress()):
    progress(0.05, desc="Menyiapkan client…")
    init_db()
    c = get_settings()
    svc = build_services(c)
    try:
        progress(0.2, desc="Menjalankan siklus screening…")
        summary = await run_cycle(svc)
    except Exception as e:  # noqa: BLE001
        return banner(FAIL, f"Siklus gagal: {e}"), [], ""
    finally:
        await svc.aclose()

    progress(0.9, desc="Memuat hasil…")
    rows, cards = load_screener("ALL", 0.0)
    by_state = " · ".join(f"{k}: {v}" for k, v in summary["by_state"].items()) or "tidak ada token"
    msg = (f"Siklus selesai dalam <b>{summary['duration_s']:.1f}s</b> — "
           f"{summary['tokens']} token &nbsp;|&nbsp; {by_state}")
    kind = OK if summary["tokens"] else WARN
    if not summary["tokens"]:
        msg += ("<br>Tidak ada kandidat. Aktifkan OKX hot-token / RPC mint-log discovery, "
                "atau tambahkan kontrak manual di tab Token Inspector.")

    # A source that answered nothing all cycle is the difference between "these
    # tokens are genuinely bad" and "the screener was blind". Say which, here,
    # rather than only in a terminal the operator may never look at.
    # An endpoint that never answered is the difference between "these tokens
    # are genuinely bad" and "the screener was blind". Say which, here, rather
    # than only in a terminal the operator may never look at.
    dead = summary.get("dead_connections") or []
    if dead:
        kind = WARN
        detail = "<br>".join(
            f"&nbsp;&nbsp;• <b>{html.escape(name)}</b> — "
            f"{html.escape(summary['connections'][name]['error'] or 'gagal')}"
            for name in dead[:8]
        )
        more = f"<br>&nbsp;&nbsp;… dan {len(dead) - 8} endpoint lain" if len(dead) > 8 else ""
        msg += (f"<br>⚠️ <b>{len(dead)} endpoint tidak pernah menjawab siklus ini:</b><br>"
                f"{detail}{more}"
                "<br>Selama ini terjadi, field yang mereka isi terbaca <i>unavailable</i> dan "
                "token ditolak secara fail-closed — penolakan itu belum tentu soal tokennya. "
                "Periksa tab <b>Koneksi &amp; API Test</b>.")
    return banner(kind, msg), rows, cards


def add_token_to_watchlist(address: str, symbol: str):
    address = (address or "").strip()
    if not address:
        return banner(WARN, "Masukkan alamat kontrak terlebih dahulu.")
    init_db()
    with session_scope() as s:
        existing = s.execute(select(Token).where(Token.address == address)).scalar_one_or_none()
        if existing:
            return banner(WARN, f"<b>{address}</b> sudah dilacak.")
        s.add(Token(chain="robinhood", address=address, symbol=(symbol or "").strip() or None))
    return banner(OK, f"<b>{address}</b> ditambahkan. Menambahkan token <b>tidak</b> memberi izin trading — "
                      f"ia hanya masuk antrean screening.")


# ===========================================================================
# Tab 3 — Token inspector
# ===========================================================================
def render_gates(decision) -> str:
    order = {Severity.HARD: 0, Severity.LIVE_ONLY: 1, Severity.DATA: 2, Severity.SOFT: 3}
    cls = {Severity.HARD: "hard", Severity.LIVE_ONLY: "live", Severity.DATA: "data", Severity.SOFT: "data"}
    mark = {Severity.HARD: "✕", Severity.LIVE_ONLY: "!", Severity.DATA: "?", Severity.SOFT: "·"}

    failed = sorted([g for g in decision.gates if not g.passed], key=lambda g: order[g.severity])
    lines = [f'<span class="{cls[g.severity]}">{mark[g.severity]} <b>{g.name}</b> — {g.reason}</span>'
             for g in failed]
    passed = [g for g in decision.gates if g.passed]
    lines += [f'<span class="pass">✓ <b>{g.name}</b> — {g.reason}</span>' for g in passed]
    return f'<div class="gatelist">{"<br>".join(lines)}</div>'


async def do_inspect(address: str, rpc_url, okx_key, okx_secret, okx_pass, okx_project, chain_id):
    address = (address or "").strip()
    if not address:
        return (banner(WARN, "Masukkan alamat kontrak."), "", "", "", {})

    c = settings_with_overrides(rpc_url, okx_key, okx_secret, okx_pass, okx_project, chain_id)
    svc = build_services(c)
    try:
        snap, decision = await dry_run_token(svc, TokenRef(address=address), c)
    except Exception as e:  # noqa: BLE001
        return (banner(FAIL, f"Inspeksi gagal: {e}"), "", "", "", {})
    finally:
        await svc.aclose()

    head = (
        f"{state_pill(decision.state.value)} &nbsp; <b>{snap.token.symbol or '?'}</b> "
        f"— {snap.token.name or 'unknown'} &nbsp;·&nbsp; skor <b>{decision.score.total:.1f}/100</b>"
        f"<br><span style='opacity:.7;font-size:13px'>{decision.reason}</span>"
    )
    kind = {"LIVE_BUY": OK, "PAPER_BUY": OK, "ALERT": WARN, "WATCH": WARN, "REJECT": FAIL}[decision.state.value]

    bars = score_bars([(x.name, x.points, x.weight) for x in decision.score.components],
                      decision.score.total)

    cards = metric_cards([
        ("Likuiditas", f"${snap.liquidity_usd:,.0f}" if snap.liquidity_usd else "n/a", "reconciled (min)"),
        ("Volume 24h", f"${snap.volume_24h:,.0f}" if snap.volume_24h else "n/a",
         f"{snap.tx_count_24h or '?'} tx"),
        ("Holder", f"{snap.unique_holders:,}" if snap.unique_holders else "n/a",
         f"growth {snap.holder_growth_24h_pct:+.1f}%" if snap.holder_growth_24h_pct is not None else "growth n/a"),
        ("Top1 / Top10", f"{snap.top1_holder_pct:.1f}% / {snap.top10_holder_pct:.1f}%"
         if snap.top1_holder_pct is not None and snap.top10_holder_pct is not None else "n/a", "konsentrasi"),
        ("Slippage", fmt_bps(snap.slippage_bps),
         f"pada ${snap.slippage_notional_usd:,.0f}" if snap.slippage_notional_usd else ""),
        ("Umur token", f"{snap.token_age_hours/24:.1f} hari" if snap.token_age_hours else "n/a",
         "dari deploy block"),
        ("24h", f"{snap.price_change_24h_pct:+.1f}%" if snap.price_change_24h_pct is not None else "n/a",
         "anti-chase"),
        ("OKX", "tersedia" if snap.okx_available else ("tidak" if snap.okx_available is False else "?"),
         snap.okx_inst_id or "on-chain only"),
    ])

    detail = {
        "missing_fields": snap.missing_fields,
        "sources": snap.sources,
        "price_sources": snap.price_sources,
        "contract_flags": snap.contract_flags,
        "tokenomics_flags": snap.tokenomics_flags,
        "price_reconciliation": snap.raw.get("price_reconciliation"),
        "contract_detail": snap.raw.get("contract_detail"),
    }

    return banner(kind, head), bars + cards, render_gates(decision), build_body(snap, decision), detail


# ===========================================================================
# Tab 4 — Threshold lab
# ===========================================================================
LAB_INPUTS = [
    "liquidity_usd", "volume_24h", "volume_1h", "tx_count_24h", "buy_ratio_24h",
    "unique_holders", "top1_holder_pct", "top10_holder_pct", "holder_growth_24h_pct",
    "spread_bps", "token_age_days", "price_change_24h_pct", "drawdown_from_ath_pct",
    "contract_verified", "contract_flags", "okx_available",
]


def lab_evaluate(
    liquidity, volume_24h, volume_1h, tx_count, buy_ratio, holders, top1, top10, growth,
    spread, age_days, change_24h, drawdown, verified, flags, okx_avail,
    min_liq, min_liq_live, max_slip, max_top1, max_top10, min_holders,
    max_turnover, max_change, alert_min, paper_min, live_min, position_usd,
):
    """Recompute gates + score + decision from the sliders. Pure, no I/O."""
    c = get_settings().model_copy(update={
        "min_liquidity_usd": min_liq,
        "min_liquidity_usd_live": min_liq_live,
        "max_slippage_bps": int(max_slip),
        "max_top1_holder_pct": max_top1,
        "max_top10_holder_pct": max_top10,
        "min_unique_holders": int(min_holders),
        "max_volume_to_liquidity_ratio": max_turnover,
        "max_price_change_24h_pct": max_change,
        "score_alert_min": alert_min,
        "score_paper_buy_min": paper_min,
        "score_live_buy_min": live_min,
        "position_usd": position_usd,
    })

    from app.pipeline.metrics import estimate_slippage_bps

    now = dt.datetime.now(dt.timezone.utc)
    verified_val = {"verified": True, "unverified": False, "unknown": None}[verified]
    okx_val = {"tersedia": True, "tidak tersedia": False, "belum diketahui": None}[okx_avail]

    snap = NormalizedSnapshot(
        token=TokenRef(address="0x" + "11" * 20, symbol="LAB", name="Lab Token", decimals=18),
        captured_at=now,
        price_usd=0.05,
        price_sources={"okx_market": 0.05, "dex_pool": 0.0501},
        price_divergence_pct=0.1,
        liquidity_usd=liquidity,
        market_cap_usd=liquidity * 6,
        fdv_usd=liquidity * 8,
        total_supply=120_000_000.0,
        circulating_supply=90_000_000.0,
        volume_1h=volume_1h,
        volume_24h=volume_24h,
        tx_count_24h=int(tx_count),
        buy_ratio_24h=buy_ratio,
        trade_sample_size=500,
        unique_trader_ratio=0.40,
        top_trader_volume_pct=5.0,
        filtered_trade_pct=1.0,
        unique_holders=int(holders),
        top1_holder_pct=top1,
        top10_holder_pct=top10,
        whale_concentration=0.02,
        holder_growth_24h_pct=growth,
        spread_bps=spread if okx_val else None,
        slippage_bps=estimate_slippage_bps(liquidity, position_usd),
        slippage_notional_usd=position_usd,
        token_age_hours=age_days * 24,
        price_change_24h_pct=change_24h,
        price_vs_7d_base_pct=change_24h * 1.5,
        drawdown_from_ath_pct=drawdown,
        contract_verified=verified_val,
        contract_flags=list(flags or []),
        is_proxy=False,
        sniper_wallet_pct=3.0,
        bundled_buy_pct=4.0,
        suspicious_holder_pct=1.0,
        days_to_major_unlock=90.0,
        okx_available=okx_val,
        okx_inst_id="LAB-USDT" if okx_val else None,
    )

    gates = evaluate_gates(snap, c)
    score = score_snapshot(snap, c)
    ctx = DecisionContext(
        consecutive_passes=c.stability_required_snapshots,
        recent_scores=[score.total] * 3,
        run_mode=c.run_mode,
        okx_available=snap.okx_available,
    )
    decision = decide(snap, gates, score, ctx, c)

    kind = {"LIVE_BUY": OK, "PAPER_BUY": OK, "ALERT": WARN, "WATCH": WARN, "REJECT": FAIL}[decision.state.value]
    head = (f"{state_pill(decision.state.value)} &nbsp; skor <b>{decision.score.total:.1f}/100</b>"
            f"<br><span style='opacity:.75;font-size:13px'>{decision.reason}</span>")

    bars = score_bars([(x.name, x.points, x.weight) for x in decision.score.components], decision.score.total)
    turnover = volume_24h / liquidity if liquidity else 0
    extra = metric_cards([
        ("Turnover", f"{turnover:.2f}x", f"batas {max_turnover:.1f}x"),
        ("Slippage", fmt_bps(snap.slippage_bps), f"batas {max_slip:.0f} bps"),
        ("Share 1h", f"{volume_1h/volume_24h*100:.1f}%" if volume_24h else "n/a", "batas 35%"),
    ])
    return banner(kind, head), bars + extra, render_gates(decision)


# ===========================================================================
# Tab 5 — Risk & orders
# ===========================================================================
def risk_status():
    init_db()
    killed, kill_reason = killswitch.is_killed()
    safe, safe_reason = killswitch.safe_mode_status()
    c = get_settings()

    live_possible = c.run_mode == "LIVE" and not killed and not safe
    if live_possible:
        head = ("<b>LIVE TRADING AKTIF</b> — sistem ini dapat menempatkan order dengan uang "
                f"{'DEMO' if c.okx_simulated else '<b>SUNGGUHAN</b>'}.")
        kind = WARN
    else:
        reasons = []
        if c.run_mode != "LIVE":
            reasons.append(f"mode {c.run_mode}")
        if killed:
            reasons.append(f"kill switch: {kill_reason}")
        if safe:
            reasons.append(f"safe mode: {safe_reason}")
        head = f"<b>Tidak ada order live yang mungkin</b> — {' · '.join(reasons)}"
        kind = OK

    with session_scope() as s:
        day_start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        live_today = s.execute(
            select(func.coalesce(func.sum(OrderRecord.requested_usd), 0.0)).where(
                OrderRecord.mode == "LIVE", OrderRecord.created_at >= day_start,
                OrderRecord.status.in_(("created", "live", "partially_filled", "filled")))
        ).scalar_one()
        paper_today = s.execute(
            select(func.coalesce(func.sum(OrderRecord.requested_usd), 0.0)).where(
                OrderRecord.mode == "PAPER", OrderRecord.created_at >= day_start,
                OrderRecord.status.in_(("created", "live", "partially_filled", "filled")))
        ).scalar_one()
        n_orders = s.execute(select(func.count(OrderRecord.id))).scalar_one()

    cards = metric_cards([
        ("Mode", c.run_mode, "OKX demo" if c.okx_simulated else "OKX real"),
        ("Kill switch", "ENGAGED" if killed else "clear", kill_reason[:40] if killed else "3 pemicu"),
        ("Safe mode", "ACTIVE" if safe else "clear", safe_reason[:40] if safe else c.safe_mode_ref_instrument),
        ("Exposure live hari ini", f"${live_today:,.2f}", f"batas ${c.max_exposure_daily_usd:,.0f}"),
        ("Exposure paper hari ini", f"${paper_today:,.2f}", "terpisah dari live"),
        ("Clip size", f"${c.position_usd:,.2f}", f"maks ${c.max_exposure_per_token_usd:,.0f}/token"),
        ("Total order", str(n_orders), "paper + live"),
    ])
    return banner(kind, head), cards


def load_orders():
    init_db()
    with session_scope() as s:
        rows = s.execute(select(OrderRecord).order_by(desc(OrderRecord.created_at)).limit(200)).scalars().all()
        return [[
            o.created_at.strftime("%Y-%m-%d %H:%M"), o.mode, o.inst_id or "?", o.status,
            round(o.requested_usd, 2), o.limit_price, o.filled_base, o.avg_fill_price,
            o.realized_slippage_bps, (o.entry_reason or "")[:120], (o.error or "")[:80],
        ] for o in rows]


def load_alerts():
    init_db()
    with session_scope() as s:
        rows = s.execute(select(Alert).order_by(desc(Alert.created_at)).limit(100)).scalars().all()
        return [[a.created_at.strftime("%Y-%m-%d %H:%M"), a.state, a.title,
                 ", ".join(k for k, v in (a.delivered or {}).items() if v) or "—"] for a in rows]


def do_engage_kill(reason: str):
    killswitch.engage(reason.strip() or "manual dari UI")
    b, cards = risk_status()
    return b, cards


def do_release_kill(confirm: str):
    if confirm.strip().upper() != "RELEASE":
        return banner(WARN, "Ketik <b>RELEASE</b> di kolom konfirmasi untuk melepas kill switch."), gr.update()
    killswitch.release("gradio ui")
    b, cards = risk_status()
    return b, cards


# ===========================================================================
# Tab 6 — Config
# ===========================================================================
def load_config():
    c = get_settings().model_dump()
    redacted = {}
    for k, v in c.items():
        if any(s in k for s in ("secret", "passphrase", "api_key", "token", "chat_id")):
            redacted[k] = "***" if v else ""
        else:
            redacted[k] = v
    return redacted


# ===========================================================================
# Tab 0 — Setup wizard
# ===========================================================================
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

SETUP_FIELDS = [
    "RH_NODE_RPC_URL", "RH_CHAIN_ID",
    "DEXSCREENER_CHAIN_SLUG", "GECKOTERMINAL_NETWORK",
    "RH_DATA_ENABLED", "RH_DATA_RPC_URL",
    "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE", "OKX_PROJECT_ID",
    "OKX_TRADE_API_KEY", "OKX_TRADE_API_SECRET", "OKX_TRADE_API_PASSPHRASE",
    "OKX_SIMULATED", "RUN_MODE", "POSITION_USD", "MAX_EXPOSURE_DAILY_USD",
    "ALERT_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
]


# Fields backed by a Gradio component that rejects arbitrary strings. Feeding a
# Dropdown an empty value raises "not in the list of choices" and leaves the
# control blank, which then breaks the save. Every one of these needs a valid
# fallback drawn from the live settings.
CHOICE_FIELDS = {
    "RH_DATA_ENABLED": ("false", "true"),
    "OKX_SIMULATED": ("true", "false"),
    "RUN_MODE": ("ALERT_ONLY", "PAPER", "LIVE"),
}
NUMBER_FIELDS = {"POSITION_USD", "MAX_EXPOSURE_DAILY_USD"}


def _setting_default(key: str):
    c = get_settings()
    mapping = {
        "RH_DATA_ENABLED": str(c.rh_data_enabled).lower(),
        "OKX_SIMULATED": str(c.okx_simulated).lower(),
        "RUN_MODE": c.run_mode,
        "POSITION_USD": c.position_usd,
        "MAX_EXPOSURE_DAILY_USD": c.max_exposure_daily_usd,
        "RH_NODE_RPC_URL": c.rh_node_rpc_url,
        "RH_CHAIN_ID": str(c.rh_chain_id) if c.rh_chain_id else "",
        "RH_DATA_RPC_URL": c.rh_data_rpc_url,
        "DEXSCREENER_CHAIN_SLUG": c.dexscreener_chain_slug,
        "GECKOTERMINAL_NETWORK": c.geckoterminal_network,
        "ALERT_WEBHOOK_URL": c.alert_webhook_url,
    }
    return mapping.get(key, "")


def load_setup_values():
    """Populate the form from .env, with secrets shown as a placeholder.

    Falls back to the effective settings rather than an empty string, so the
    controls always hold a value their component will accept.
    """
    current = envfile.read_env(ENV_PATH)
    out = []
    for key in SETUP_FIELDS:
        val = current.get(key, "")

        if key in envfile.SECRET_KEYS:
            out.append("***tersimpan***" if val else "")
            continue

        if key in CHOICE_FIELDS:
            choices = CHOICE_FIELDS[key]
            if val not in choices:
                fallback = _setting_default(key)
                val = fallback if fallback in choices else choices[0]
            out.append(val)
            continue

        if key in NUMBER_FIELDS:
            try:
                out.append(float(val) if val != "" else float(_setting_default(key)))
            except (TypeError, ValueError):
                out.append(float(_setting_default(key) or 0))
            continue

        out.append(val or str(_setting_default(key) or ""))
    return out


async def do_detect_chain_id(rpc_url: str):
    """Read the chain ID from the node rather than making the user find it."""
    rpc_url = (rpc_url or "").strip()
    if not rpc_url:
        return gr.update(), banner(WARN, "Isi RPC URL dulu.")
    c = get_settings().model_copy(update={"rh_node_rpc_url": rpc_url})
    svc = build_diagnostic_services(c)
    try:
        cid = await svc.node.chain_id()
        blk = await svc.node.block_number()
    except Exception as e:  # noqa: BLE001
        return gr.update(), banner(FAIL, f"Tidak bisa membaca chain ID: {e}")
    finally:
        await svc.aclose()
    if cid is None:
        return gr.update(), banner(FAIL, "Node menjawab tapi tidak memberi chain ID.")
    return str(cid), banner(
        OK, f"Chain ID <b>{cid}</b> terdeteksi (block {blk:,}). Klik <b>Simpan</b> untuk menyimpannya."
    )


def do_save_setup(*values):
    """Write the form to .env and hot-reload settings."""
    form = dict(zip(SETUP_FIELDS, values))

    # gr.Number hands back floats; "25.0" vs "25" would churn .env on every save.
    for key in NUMBER_FIELDS:
        val = form.get(key)
        if isinstance(val, float) and val.is_integer():
            form[key] = str(int(val))

    requested_mode = (form.get("RUN_MODE") or "").strip().upper()
    simulated = str(form.get("OKX_SIMULATED", "")).strip().lower()

    current = envfile.read_env(ENV_PATH)
    updates, kept_secrets = envfile.merge_form(current, form)

    if not updates:
        # "Nothing changed" is a claim about the operator's input, so it has to
        # say what it compared against. Reporting a bare no-op sent an operator
        # hunting for a save bug when the form and the file genuinely matched —
        # or worse, when a blank secret had been quietly preserved.
        msg = (f"Tidak ada perubahan untuk disimpan — isian form sudah sama persis dengan "
               f"<code>{ENV_PATH.name}</code> ({len(current)} kunci terbaca).")
        if kept_secrets:
            msg += ("<br>Secret yang dibiarkan kosong tetap dipertahankan: "
                    f"<b>{', '.join(sorted(kept_secrets))}</b>. Untuk menggantinya, "
                    "tempel nilai barunya — mengosongkan field tidak menghapus secret.")
        msg += ("<br><span style='opacity:.75'>Tab Setup hanya mengelola "
                f"{len(SETUP_FIELDS)} kunci. Threshold risiko, interval, dan digest "
                f"diubah langsung di <code>{ENV_PATH.name}</code>.</span>")
        return banner(WARN, msg), gr.update()

    try:
        changed = envfile.write_env(ENV_PATH, updates)
    except OSError as e:
        return banner(FAIL, f"Gagal menulis .env: {e}"), gr.update()

    reload_settings()
    c = get_settings()

    cleared = [k for k in changed if not updates.get(k)]
    msg = f"Tersimpan ke <code>.env</code>: <b>{', '.join(changed)}</b>"
    if cleared:
        msg += f"<br>Dikosongkan: <b>{', '.join(cleared)}</b>"
    if kept_secrets:
        msg += (f"<br><span style='opacity:.75'>Secret dipertahankan (field dibiarkan kosong): "
                f"{', '.join(sorted(kept_secrets))}</span>")
    kind = OK
    if requested_mode == "LIVE" and simulated in ("false", "0", "no"):
        kind = WARN
        msg += ("<br><b>Mode LIVE dengan uang sungguhan aktif.</b> Kill switch dan batas "
                "exposure adalah satu-satunya yang membatasi kerugian. Periksa tab "
                "Risiko &amp; Order sekarang.")
    elif requested_mode == "LIVE":
        msg += "<br>Mode LIVE pada OKX demo — tidak ada uang sungguhan."

    return banner(kind, msg), load_config()


def do_init_db():
    try:
        init_db()
    except Exception as e:  # noqa: BLE001
        return banner(FAIL, f"Gagal menyiapkan database: {e}")
    c = get_settings()
    return banner(OK, f"Database siap: <code>{c.database_url}</code>")


def setup_status():
    """A short, honest readiness summary for the top of the setup tab."""
    c = get_settings()
    items = []
    items.append(("RPC node", "terisi" if c.rh_node_rpc_url else "KOSONG",
                  "wajib untuk data on-chain"))
    items.append(("Chain ID", str(c.rh_chain_id) if c.rh_chain_id else "belum diset",
                  "dipakai OKX sebagai chainIndex"))
    n_price = sum([bool(c.dexscreener_chain_slug), bool(c.geckoterminal_network), bool(c.okx_api_key)])
    items.append(("Sumber harga", str(n_price),
                  "butuh >=2 untuk LIVE_BUY" if n_price < 2 else "cross-check aktif"))
    items.append(("OKX market", "terisi" if c.okx_api_key else "kosong", "opsional"))
    items.append(("OKX trading", "terisi" if c.okx_trade_api_key else "kosong",
                  "hanya untuk PAPER/LIVE"))
    items.append(("Mode", c.run_mode, "demo" if c.okx_simulated else "REAL MONEY"))
    items.append(("Clip", f"${c.position_usd:,.0f}", f"harian ${c.max_exposure_daily_usd:,.0f}"))
    return metric_cards([(a, b, d) for a, b, d in items])


# ===========================================================================
# Pipeline control
# ===========================================================================
def scheduler_status_html():
    st = runtime.status()
    if st["running"]:
        started = st["started_at"].strftime("%H:%M:%S") if st["started_at"] else "?"
        last = st["last_run"].strftime("%H:%M:%S") if st["last_run"] else "belum"
        return banner(OK, f"<b>Screening otomatis BERJALAN</b> — mulai {started} · "
                          f"siklus selesai: {st['runs']} · terakhir {last} · error {st['errors']}")
    return banner(WARN, "Screening otomatis <b>berhenti</b>. Klik <b>Mulai otomatis</b> "
                        "untuk menjalankannya terus-menerus.")


async def do_start_scheduler(interval_min: float):
    """Async on purpose: APScheduler's AsyncIOScheduler binds to the running
    loop, and Gradio executes sync handlers in a worker thread where there
    isn't one."""
    ok, msg = runtime.start(int((interval_min or 5) * 60))
    return scheduler_status_html(), banner(OK if ok else WARN, msg)


async def do_stop_scheduler():
    ok, msg = runtime.stop()
    return scheduler_status_html(), banner(OK if ok else WARN, msg)


# ===========================================================================
# Blocks
# ===========================================================================
def build_ui() -> gr.Blocks:
    c = get_settings()

    # Gradio 6 moved theme/css from the Blocks constructor to launch().
    with gr.Blocks(title="Robinhood Chain Screener") as demo:
        gr.Markdown(
            "# 🛡️ Robinhood Chain Token Screener\n"
            "Screener token early-stage dengan **risk gate keras**. Tugas utamanya adalah **menolak**. "
            "Data yang hilang tidak pernah dianggap aman — token yang tidak bisa diukur tidak bisa dibeli."
        )

        # ---- shared credential overrides (session-only) -------------------
        with gr.Accordion("🔑 Override kredensial (hanya untuk sesi ini — tidak ditulis ke disk)", open=False):
            gr.Markdown(
                "Isi di sini untuk **menguji** kredensial sebelum menaruhnya di `.env`. "
                "Nilai ini hidup di memori proses saja dan hilang saat restart. "
                "Kosongkan untuk memakai `.env`."
            )
            with gr.Row():
                rpc_url = gr.Textbox(label="RH_NODE_RPC_URL", placeholder=c.rh_node_rpc_url or "https://…",
                                     scale=3)
                chain_id = gr.Textbox(label="RH_CHAIN_ID", placeholder=str(c.rh_chain_id or "auto-detect"),
                                      scale=1)
            with gr.Row():
                okx_key = gr.Textbox(label="OKX_API_KEY", type="password")
                okx_secret = gr.Textbox(label="OKX_API_SECRET", type="password")
                okx_pass = gr.Textbox(label="OKX_API_PASSPHRASE", type="password")
                okx_project = gr.Textbox(label="OKX_PROJECT_ID", type="password")

        creds = [rpc_url, okx_key, okx_secret, okx_pass, okx_project, chain_id]

        # ===================================================== TAB 0
        with gr.Tabs():
            with gr.Tab("🚀 Setup"):
                gr.Markdown(
                    "Isi di sini sekali, klik **Simpan**, dan seluruh sistem terkonfigurasi. "
                    "Nilai ditulis ke file `.env` (komentar dan urutannya dipertahankan, "
                    "dan salinan cadangan `.env.bak` dibuat). "
                    "**Kolom rahasia yang dibiarkan kosong tidak akan menghapus nilai lama.**"
                )
                setup_cards = gr.HTML(setup_status())

                with gr.Accordion("1. Robinhood Chain — wajib", open=True):
                    gr.Markdown(
                        "Satu-satunya yang benar-benar wajib. Tanpa ini, supply, flag kontrak, "
                        "umur token, dan distribusi holder semuanya tidak tersedia — dan token "
                        "yang tidak bisa diukur tidak bisa dibeli."
                    )
                    with gr.Row():
                        f_rpc = gr.Textbox(label="RPC URL (JSON-RPC)", scale=4,
                                           placeholder="https://...")
                        f_chain = gr.Textbox(label="Chain ID", scale=1,
                                             placeholder="kosongkan lalu deteksi")
                        detect_btn = gr.Button("🔎 Deteksi", scale=1)
                    with gr.Row():
                        f_data_on = gr.Dropdown(["false", "true"], value="false", scale=1,
                                                label="Data API aktif?")
                        f_data_url = gr.Textbox(label="Alchemy Robinhood RPC URL (opsional)", scale=3)
                    gr.Markdown(
                        "_Data API memakai metode JSON-RPC Alchemy Token/Transfers. Ia menambah "
                        "metadata dan activity, bukan global token listing atau top holders._"
                    )

                with gr.Accordion("1b. Data pasar gratis — TANPA API KEY", open=True):
                    gr.Markdown(
                        "**Tidak perlu daftar, tidak perlu kunci.** Dua sumber ini mengisi "
                        "lubang terbesar di pipeline:\n\n"
                        "- **DexScreener** — satu-satunya sumber gratis yang melaporkan "
                        "**jumlah transaksi beli vs jual**, yang dibutuhkan `buy_ratio_24h`. "
                        "Juga volume per window (5m/1h/24h), likuiditas, dan umur pool.\n"
                        "- **GeckoTerminal** — **sumber harga kedua yang independen**. Tanpa "
                        "dua sumber, rekonsiliasi harga tidak punya pembanding dan "
                        "**LIVE_BUY tidak akan pernah tercapai** berapa pun skornya.\n\n"
                        "Yang perlu diisi hanya *slug chain*. Tiap penyedia memakai nama "
                        "berbeda untuk chain yang sama, dan slug yang salah memberi harga "
                        "token lain."
                    )
                    with gr.Row():
                        f_ds_slug = gr.Textbox(
                            label="DexScreener chain slug",
                            placeholder="mis. ethereum, base, arbitrum",
                            info="kosong = terima pool dari chain mana pun (berisiko)",
                        )
                        f_gt_net = gr.Textbox(
                            label="GeckoTerminal network slug",
                            placeholder="mis. eth, base, arbitrum",
                            info="kosong = GeckoTerminal mati, sumber harga tinggal satu",
                        )

                with gr.Accordion("2. OKX — data pasar (opsional)", open=False):
                    gr.Markdown("Untuk harga, likuiditas, dan volume. Screening tetap jalan tanpa ini.")
                    with gr.Row():
                        f_okx_key = gr.Textbox(label="OKX_API_KEY", type="password")
                        f_okx_sec = gr.Textbox(label="OKX_API_SECRET", type="password")
                    with gr.Row():
                        f_okx_pass = gr.Textbox(label="OKX_API_PASSPHRASE", type="password")
                        f_okx_proj = gr.Textbox(label="OKX_PROJECT_ID", type="password")

                with gr.Accordion("3. OKX — trading (hanya untuk PAPER/LIVE)", open=False):
                    gr.Markdown(
                        "**Pakai API key terpisah dari data pasar.** Beri izin *trade saja* — "
                        "jangan pernah izin withdraw — dan kunci ke IP server Anda."
                    )
                    with gr.Row():
                        f_tr_key = gr.Textbox(label="OKX_TRADE_API_KEY", type="password")
                        f_tr_sec = gr.Textbox(label="OKX_TRADE_API_SECRET", type="password")
                    with gr.Row():
                        f_tr_pass = gr.Textbox(label="OKX_TRADE_API_PASSPHRASE", type="password")
                        f_sim = gr.Dropdown(["true", "false"], value="true",
                                            label="OKX_SIMULATED (demo trading)")

                with gr.Accordion("4. Mode & ukuran posisi", open=True):
                    with gr.Row():
                        f_mode = gr.Dropdown(
                            ["ALERT_ONLY", "PAPER", "LIVE"], value=c.run_mode, label="RUN_MODE",
                            info="ALERT_ONLY = tidak ada order sama sekali. Mulai dari sini.",
                        )
                        f_pos = gr.Number(value=c.position_usd, label="POSITION_USD",
                                          info="ukuran per order")
                        f_daily = gr.Number(value=c.max_exposure_daily_usd,
                                            label="MAX_EXPOSURE_DAILY_USD",
                                            info="batas total per hari")
                    gr.Markdown(
                        "_Ambil batas risiko lainnya (likuiditas, holder, slippage, dst.) di tab "
                        "**Threshold Lab** — geser slider, lihat efeknya, lalu salin ke `.env`._"
                    )

                with gr.Accordion("5. Notifikasi (opsional)", open=False):
                    f_webhook = gr.Textbox(label="ALERT_WEBHOOK_URL")
                    with gr.Row():
                        f_tg_token = gr.Textbox(label="TELEGRAM_BOT_TOKEN", type="password")
                        f_tg_chat = gr.Textbox(label="TELEGRAM_CHAT_ID", type="password")

                with gr.Row():
                    save_btn = gr.Button("💾 Simpan ke .env", variant="primary", scale=2)
                    initdb_btn = gr.Button("🗄️ Siapkan database", scale=1)
                    reload_setup_btn = gr.Button("↻ Muat ulang dari .env", scale=1)
                setup_result = gr.HTML()

                setup_inputs = [
                    f_rpc, f_chain, f_ds_slug, f_gt_net, f_data_on, f_data_url,
                    f_okx_key, f_okx_sec, f_okx_pass, f_okx_proj,
                    f_tr_key, f_tr_sec, f_tr_pass,
                    f_sim, f_mode, f_pos, f_daily,
                    f_webhook, f_tg_token, f_tg_chat,
                ]

            with gr.Tab("🩺 Koneksi & API Test"):
                gr.Markdown(
                    "Jalankan ini **sebelum** mempercayai angka apa pun. Selain status hidup/mati, "
                    "panel ini menampilkan **nama field yang benar-benar dikembalikan** tiap API — "
                    "sehingga perubahan kontrak upstream terlihat sebelum memengaruhi keputusan."
                )
                with gr.Row():
                    probe_addr = gr.Textbox(
                        label="Alamat token (opsional — mengaktifkan uji ERC-20, scan kontrak, verifikasi, price-info)",
                        placeholder="0x…", scale=4,
                    )
                    # One button on purpose. The separate "Ping RPC" did a strict
                    # subset of check_node_rpc, so it could only ever agree with
                    # this one or confuse the operator by disagreeing.
                    test_btn = gr.Button("🩺 Test Semua Koneksi & API", variant="primary", scale=2)

                conn_banner = gr.HTML(banner(SKIP, "Belum diuji. Klik <b>Test Semua Koneksi &amp; API</b>."))
                conn_table = gr.Dataframe(
                    headers=["Status", "Grup", "Pemeriksaan", "Latensi", "Ringkasan", "Tindakan"],
                    datatype=["str"] * 6, interactive=False, wrap=True, value=[],
                )
                with gr.Accordion("🔧 Tindakan yang disarankan", open=True):
                    conn_fixes = gr.Markdown("_Belum diuji._")
                with gr.Accordion("🔍 Field yang dikembalikan API (untuk mencocokkan parser)", open=False):
                    gr.Markdown(
                        "Bandingkan `observed_keys` dengan daftar kandidat di "
                        "`app/clients/okx_market.py` dan `app/clients/rh_data.py`. "
                        "Field yang tidak terparse tetap `None` dan mengirim token ke WATCH — "
                        "**tidak pernah menjadi 0**."
                    )
                    conn_detail = gr.JSON(label="detail", value={})

                gr.Markdown("### Uji jalur notifikasi")
                with gr.Row():
                    alert_btn = gr.Button("📨 Kirim alert uji ke semua sink")
                alert_result = gr.HTML()

                test_btn.click(guarded(do_connection_test, ERROR_SLOT, [], {}, ""),
                               [probe_addr] + creds,
                               [conn_banner, conn_table, conn_detail, conn_fixes])
                alert_btn.click(guarded(do_alert_test, ERROR_SLOT), None, [alert_result])

            # ===================================================== TAB 2
            with gr.Tab("📊 Screener"):
                gr.Markdown("### Kontrol pipeline")
                sched_banner = gr.HTML(scheduler_status_html())
                with gr.Row():
                    run_btn = gr.Button("▶️ Jalankan 1 siklus sekarang", variant="primary", scale=2)
                    sched_interval = gr.Number(value=c.ingest_interval_s / 60, precision=1,
                                               label="Interval (menit)", scale=1)
                    start_sched_btn = gr.Button("🔁 Mulai otomatis", scale=1)
                    stop_sched_btn = gr.Button("⏹️ Hentikan", scale=1)
                sched_msg = gr.HTML()
                gr.Markdown(
                    "_Mode otomatis menjalankan siklus terus-menerus di dalam jendela ini. "
                    "Menutup jendela menghentikannya. Untuk menghentikan **trading** tanpa "
                    "menghentikan screening, pakai kill switch di tab Risiko._"
                )

                gr.Markdown("### Hasil")
                with gr.Row():
                    refresh_btn = gr.Button("🔄 Muat ulang tabel", scale=1)
                with gr.Row():
                    state_filter = gr.Dropdown(
                        ["ALL", "LIVE_BUY", "PAPER_BUY", "ALERT", "WATCH", "REJECT"],
                        value="ALL", label="Filter state", scale=1,
                    )
                    min_score = gr.Slider(0, 100, value=0, step=1, label="Skor minimum", scale=2)

                cycle_banner = gr.HTML()
                screener_cards = gr.HTML()
                screener_table = gr.Dataframe(
                    headers=["State", "Symbol", "Contract", "Skor", "Liq", "Holder", "Vol",
                             "Tokenomics", "Momentum", "OKX", "Alasan"],
                    datatype=["str", "str", "str", "number", "number", "number", "number",
                              "number", "number", "str", "str"],
                    interactive=False, wrap=True, value=[],
                )

                gr.Markdown("### Tambah kontrak ke watchlist")
                gr.Markdown(
                    "Menambahkan token hanya menjadwalkannya untuk di-screening. "
                    "Ini **tidak** memberi izin trading apa pun."
                )
                with gr.Row():
                    add_addr = gr.Textbox(label="Alamat kontrak", placeholder="0x…", scale=3)
                    add_sym = gr.Textbox(label="Symbol (opsional)", scale=1)
                    add_btn = gr.Button("➕ Tambah", scale=1)
                add_result = gr.HTML()

                run_btn.click(guarded(do_run_cycle, ERROR_SLOT, [], ""),
                              None, [cycle_banner, screener_table, screener_cards])
                refresh_btn.click(guarded(load_screener, [], ERROR_SLOT),
                                  [state_filter, min_score], [screener_table, screener_cards])
                state_filter.change(load_screener, [state_filter, min_score], [screener_table, screener_cards])
                min_score.release(load_screener, [state_filter, min_score], [screener_table, screener_cards])
                add_btn.click(add_token_to_watchlist, [add_addr, add_sym], [add_result])

            # ===================================================== TAB 3
            with gr.Tab("🔬 Token Inspector"):
                gr.Markdown(
                    "Evaluasi satu kontrak lewat pipeline penuh: ambil data → normalisasi → "
                    "21 risk gate → skor → keputusan. **Read-only** — tidak ada baris yang ditulis "
                    "ke database dan tidak ada order yang dibuat."
                )
                with gr.Row():
                    insp_addr = gr.Textbox(label="Alamat kontrak", placeholder="0x…", scale=4)
                    insp_btn = gr.Button("🔬 Inspeksi", variant="primary", scale=1)

                insp_banner = gr.HTML()
                insp_metrics = gr.HTML()
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("#### Hasil risk gate")
                        insp_gates = gr.HTML()
                    with gr.Column(scale=1):
                        gr.Markdown("#### Alert lengkap")
                        insp_alert = gr.Code(label="", language=None, lines=28)
                with gr.Accordion("🔍 Provenance & field yang hilang", open=False):
                    insp_detail = gr.JSON(value={})

                insp_btn.click(do_inspect, [insp_addr] + creds,
                               [insp_banner, insp_metrics, insp_gates, insp_alert, insp_detail])

            # ===================================================== TAB 4
            with gr.Tab("🎛️ Threshold Lab"):
                gr.Markdown(
                    "Geser slider dan lihat keputusan berubah **seketika**. Semuanya offline dan murni — "
                    "tidak ada jaringan, tidak ada database. Gunakan ini untuk mengkalibrasi threshold "
                    "sebelum menyalinnya ke `.env`, dan untuk memahami *mengapa* sebuah token ditolak."
                )

                # Result first, and sticky: moving a slider whose effect is
                # off-screen teaches nothing.
                with gr.Column(elem_id="lab-result"):
                    lab_banner = gr.HTML()
                    lab_score = gr.HTML()

                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("#### Metrik token (hipotetis)")
                        lab_liq = gr.Slider(10_000, 10_000_000, value=1_500_000, step=10_000,
                                            label="Likuiditas USD")
                        lab_v24 = gr.Slider(10_000, 30_000_000, value=2_200_000, step=10_000,
                                            label="Volume 24h USD")
                        lab_v1h = gr.Slider(0, 5_000_000, value=95_000, step=5_000, label="Volume 1h USD")
                        lab_tx = gr.Slider(0, 20_000, value=4_100, step=50, label="Jumlah tx 24h")
                        lab_buy = gr.Slider(0.0, 1.0, value=0.51, step=0.01, label="Buy ratio 24h")
                        lab_hold = gr.Slider(0, 50_000, value=6_500, step=100, label="Unique holders")
                        lab_top1 = gr.Slider(0, 100, value=4.0, step=0.5, label="Top1 holder %")
                        lab_top10 = gr.Slider(0, 100, value=18.0, step=0.5, label="Top10 holder %")
                        lab_growth = gr.Slider(-50, 2000, value=12.0, step=1, label="Holder growth 24h %")
                        lab_spread = gr.Slider(0, 500, value=20.0, step=1, label="Spread (bps)")
                        lab_age = gr.Slider(0, 365, value=12, step=1, label="Umur token (hari)")
                        lab_chg = gr.Slider(-90, 500, value=4.0, step=1, label="Perubahan harga 24h %")
                        lab_dd = gr.Slider(0, 95, value=22.0, step=1, label="Turun dari puncak lokal %")
                        lab_ver = gr.Radio(["verified", "unverified", "unknown"], value="verified",
                                           label="Status verifikasi kontrak")
                        lab_flags = gr.CheckboxGroup(
                            ["OWNER_CAN_MINT", "MINT_FUNCTION", "BLACKLIST", "PAUSABLE",
                             "UPGRADEABLE_PROXY", "MUTABLE_FEES", "OWNER_PRIVILEGE_ACTIVE"],
                            value=[], label="Flag kontrak",
                        )
                        lab_okx = gr.Radio(["tersedia", "tidak tersedia", "belum diketahui"],
                                           value="tersedia", label="Ketersediaan OKX")

                    with gr.Column(scale=1):
                        gr.Markdown("#### Threshold (batas risiko)")
                        th_liq = gr.Slider(10_000, 2_000_000, value=c.min_liquidity_usd, step=10_000,
                                           label="MIN_LIQUIDITY_USD")
                        th_liq_live = gr.Slider(10_000, 5_000_000, value=c.min_liquidity_usd_live,
                                                step=10_000, label="MIN_LIQUIDITY_USD_LIVE")
                        th_slip = gr.Slider(10, 1000, value=c.max_slippage_bps, step=5,
                                            label="MAX_SLIPPAGE_BPS")
                        th_top1 = gr.Slider(1, 60, value=c.max_top1_holder_pct, step=1,
                                            label="MAX_TOP1_HOLDER_PCT")
                        th_top10 = gr.Slider(5, 95, value=c.max_top10_holder_pct, step=1,
                                             label="MAX_TOP10_HOLDER_PCT")
                        th_holders = gr.Slider(0, 5000, value=c.min_unique_holders, step=25,
                                               label="MIN_UNIQUE_HOLDERS")
                        th_turn = gr.Slider(1, 50, value=c.max_volume_to_liquidity_ratio, step=0.5,
                                            label="MAX_VOLUME_TO_LIQUIDITY_RATIO")
                        th_chg = gr.Slider(5, 500, value=c.max_price_change_24h_pct, step=5,
                                           label="MAX_PRICE_CHANGE_24H_PCT")
                        th_alert = gr.Slider(0, 100, value=c.score_alert_min, step=1,
                                             label="SCORE_ALERT_MIN")
                        th_paper = gr.Slider(0, 100, value=c.score_paper_buy_min, step=1,
                                             label="SCORE_PAPER_BUY_MIN")
                        th_live = gr.Slider(0, 100, value=c.score_live_buy_min, step=1,
                                            label="SCORE_LIVE_BUY_MIN")
                        th_pos = gr.Slider(5, 1000, value=c.position_usd, step=5, label="POSITION_USD")

                gr.Markdown("#### Hasil gate")
                lab_gates = gr.HTML()

                lab_all = [lab_liq, lab_v24, lab_v1h, lab_tx, lab_buy, lab_hold, lab_top1, lab_top10,
                           lab_growth, lab_spread, lab_age, lab_chg, lab_dd, lab_ver, lab_flags, lab_okx,
                           th_liq, th_liq_live, th_slip, th_top1, th_top10, th_holders, th_turn,
                           th_chg, th_alert, th_paper, th_live, th_pos]
                lab_out = [lab_banner, lab_score, lab_gates]

                for comp in lab_all:
                    if isinstance(comp, gr.Slider):
                        comp.release(lab_evaluate, lab_all, lab_out)
                    else:
                        comp.change(lab_evaluate, lab_all, lab_out)

                demo.load(guarded(lab_evaluate, ERROR_SLOT, "", ""), lab_all, lab_out)

            # ===================================================== TAB 5
            with gr.Tab("🛡️ Risiko & Order"):
                risk_banner = gr.HTML()
                risk_cards = gr.HTML()
                risk_refresh = gr.Button("🔄 Muat ulang status")

                gr.Markdown("### Kill switch")
                gr.Markdown(
                    "Tiga pemicu independen: variabel env, file `KILL_SWITCH`, dan flag database. "
                    "**Gagal tertutup** — jika pemeriksaannya sendiri error, ia melapor *engaged*. "
                    "Screening dan alert tetap jalan saat aktif; hanya eksekusi yang berhenti."
                )
                with gr.Row():
                    with gr.Column():
                        kill_reason = gr.Textbox(label="Alasan", placeholder="mis. volatilitas ekstrem")
                        kill_btn = gr.Button("🛑 ENGAGE KILL SWITCH", variant="stop",
                                             elem_classes="big-kill")
                    with gr.Column():
                        rel_confirm = gr.Textbox(label="Ketik RELEASE untuk konfirmasi",
                                                 placeholder="RELEASE")
                        rel_btn = gr.Button("✅ Lepas kill switch")

                gr.Markdown("### Order ledger")
                gr.Markdown(
                    "Setiap order tertaut ke evaluasi yang membenarkannya — **tidak ada order tanpa alasan**. "
                    "UI ini sengaja tidak punya tombol pembuat order: order hanya boleh lahir dari "
                    "keputusan yang lolos gate."
                )
                orders_table = gr.Dataframe(
                    headers=["Waktu", "Mode", "Instrument", "Status", "USD", "Limit", "Filled",
                             "Avg fill", "Slippage bps", "Alasan entry", "Error"],
                    interactive=False, wrap=True, value=[],
                )
                orders_btn = gr.Button("🔄 Muat order")

                gr.Markdown("### Riwayat alert")
                alerts_table = gr.Dataframe(
                    headers=["Waktu", "State", "Judul", "Terkirim ke"],
                    interactive=False, wrap=True, value=[],
                )
                alerts_btn = gr.Button("🔄 Muat alert")

                risk_refresh.click(risk_status, None, [risk_banner, risk_cards])
                kill_btn.click(do_engage_kill, [kill_reason], [risk_banner, risk_cards])
                rel_btn.click(do_release_kill, [rel_confirm], [risk_banner, risk_cards])
                orders_btn.click(load_orders, None, [orders_table])
                alerts_btn.click(load_alerts, None, [alerts_table])

            # ===================================================== TAB 6
            with gr.Tab("⚙️ Konfigurasi"):
                gr.Markdown(
                    "Nilai efektif saat ini. **Semua secret disamarkan.** "
                    "Untuk mengubahnya, edit `.env` lalu restart — konfigurasi sengaja tidak "
                    "bisa diubah dari UI, supaya batas risiko tidak bisa dilonggarkan lewat browser."
                )
                cfg_json = gr.JSON(value=load_config())
                cfg_btn = gr.Button("🔄 Muat ulang")
                cfg_btn.click(load_config, None, [cfg_json])

                gr.Markdown(
                    "### Urutan yang benar\n"
                    "1. `Test Semua Koneksi` sampai hijau\n"
                    "2. `RUN_MODE=ALERT_ONLY` minimal 2 minggu — baca setiap alert\n"
                    "3. `RUN_MODE=PAPER` + `OKX_SIMULATED=true` minimal 1 minggu\n"
                    "4. Kerjakan `docs/SECURITY_CHECKLIST.md`\n"
                    "5. `RUN_MODE=LIVE` **dengan** `OKX_SIMULATED=true` dulu\n"
                    "6. Baru `OKX_SIMULATED=false`, dengan `POSITION_USD` sekecil mungkin\n\n"
                    "**Belum ada logika jual di sistem ini.** Exit dilakukan manual — "
                    "jangan tinggalkan berjalan tanpa pengawasan dengan modal yang Anda sayangi."
                )

        # ---- setup tab wiring -------------------------------------------
        detect_btn.click(do_detect_chain_id, [f_rpc], [f_chain, setup_result])
        save_btn.click(do_save_setup, setup_inputs, [setup_result, cfg_json]) \
                .then(setup_status, None, [setup_cards])
        initdb_btn.click(do_init_db, None, [setup_result])
        reload_setup_btn.click(guarded(load_setup_values, *([gr.skip()] * len(setup_inputs))),
                               None, setup_inputs) \
                        .then(setup_status, None, [setup_cards])

        # ---- pipeline control wiring ------------------------------------
        start_sched_btn.click(do_start_scheduler, [sched_interval], [sched_banner, sched_msg])
        stop_sched_btn.click(do_stop_scheduler, None, [sched_banner, sched_msg])
        run_btn.click(scheduler_status_html, None, [sched_banner])

        # initial load
        demo.load(guarded(risk_status, ERROR_SLOT, ""), None, [risk_banner, risk_cards])
        demo.load(guarded(load_screener, [], ERROR_SLOT),
                  [state_filter, min_score], [screener_table, screener_cards])
        demo.load(guarded(load_setup_values, *([gr.skip()] * len(setup_inputs))),
                  None, setup_inputs)
        demo.load(guarded(setup_status, ERROR_SLOT), None, [setup_cards])

    return demo


def main() -> None:
    import os

    from app.logging_conf import configure_logging

    c = get_settings()
    configure_logging(c.log_level)
    init_db()

    # Bind to loopback by default: these controls are unauthenticated.
    host = os.getenv("GRADIO_HOST", "127.0.0.1")
    port = int(os.getenv("GRADIO_PORT", "7860"))
    share = os.getenv("GRADIO_SHARE", "false").lower() == "true"
    # start_ui.bat sets this so a double-click lands the user on the page.
    inbrowser = os.getenv("GRADIO_INBROWSER", "false").lower() == "true"

    if host not in ("127.0.0.1", "localhost"):
        log.warning(
            "Gradio is binding to %s. This UI has NO authentication and can engage/release the "
            "kill switch — put it behind a reverse proxy with auth, or bind to 127.0.0.1.", host
        )

    # `show_api` was removed in Gradio 6; theme and css moved here.
    build_ui().queue().launch(
        server_name=host,
        server_port=port,
        share=share,
        inbrowser=inbrowser,
        theme=theme(),
        css=CSS,
        # Without this Gradio renders every exception as a bare "Error" pill with
        # no text, which tells an operator nothing about what to fix.
        show_error=True,
    )


if __name__ == "__main__":
    main()
