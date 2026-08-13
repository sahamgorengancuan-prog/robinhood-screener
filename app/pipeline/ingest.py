"""Ingestion + evaluation orchestration.

One cycle:

    discover -> for each token: collect -> normalize -> persist snapshot
             -> gate -> score -> decide -> persist evaluation
             -> alert -> execute (paper/live)

Collection is fault-tolerant per source. If OKX is down, on-chain metrics still
land and the token routes to WATCH; it never routes to a buy on partial data.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.clients.base import ClientError, to_float, to_int
from app.config import Settings, get_settings
from app.db import session_scope
from app.execution import killswitch
from app.execution.live import execute_live_buy
from app.execution.paper import simulate_buy
from app.execution.portfolio import exposure_for
from app.execution.order_safety import check_exposure
from app.models import Evaluation, Token, TokenSnapshot, utcnow
from app.pipeline.decision import DecisionContext, decide
from app.pipeline.normalize import NON_HOLDER_ADDRESSES, RawBundle, normalize
from app.pipeline.risk import evaluate_gates, summarize
from app.pipeline.scoring import score_snapshot
from app.schemas import DecisionState, NormalizedSnapshot, TokenRef
from app.services import Services, build_services
from app.alerts.sinks import dispatch

log = logging.getLogger(__name__)

# How far back the on-chain fallback walks when rebuilding holders from logs.
HOLDER_LOG_LOOKBACK_BLOCKS = 200_000


# ===========================================================================
# Discovery
# ===========================================================================
async def discover_tokens(svc: Services) -> list[TokenRef]:
    """Candidate tokens for this cycle.

    Primary source is the Data API's token feed. When it is disabled or fails,
    we fall back to whatever is already tracked in the DB rather than inventing
    candidates — this screener never guesses at contract addresses.
    """
    refs: list[TokenRef] = []

    if svc.data.enabled:
        try:
            for item in await svc.data.list_tokens(limit=svc.settings.max_tokens_per_cycle):
                addr = item.get("address") or item.get("contract_address") or item.get("contractAddress")
                if not addr:
                    continue
                refs.append(
                    TokenRef(
                        address=str(addr),
                        symbol=item.get("symbol"),
                        name=item.get("name"),
                        decimals=to_int(item.get("decimals")),
                    )
                )
        except ClientError as e:
            log.error("token discovery via Data API failed: %s", e)

    if not refs:
        with session_scope() as s:
            rows = s.execute(
                select(Token).where(Token.muted.is_(False)).limit(svc.settings.max_tokens_per_cycle)
            ).scalars().all()
            refs = [
                TokenRef(chain=t.chain, address=t.address, symbol=t.symbol, name=t.name, decimals=t.decimals)
                for t in rows
            ]
        if refs:
            log.info("discovery fell back to %d tracked tokens", len(refs))

    return refs[: svc.settings.max_tokens_per_cycle]


# ===========================================================================
# Collection
# ===========================================================================
async def collect(svc: Services, ref: TokenRef, history: dict[str, Any]) -> RawBundle:
    """Gather everything about one token. Every source is individually guarded."""
    b = RawBundle(ref)
    addr = ref.address

    async def _okx() -> None:
        if not svc.market.enabled:
            return
        chain_index = str(svc.settings.rh_chain_id) if svc.settings.rh_chain_id else None
        if not chain_index:
            # Without a confirmed chainIndex an OKX lookup would query the wrong
            # chain. Better to report "unresolved" than to match a same-named
            # token elsewhere.
            log.debug("RH_CHAIN_ID not set; skipping OKX on-chain lookups for %s", addr)
            return
        try:
            info = await svc.market.price_info(chain_index, [addr])
            b.okx_price_info = info.get(addr.lower())
        except ClientError as e:
            log.info("okx price_info failed for %s: %s", addr, e)
        try:
            b.okx_basic_info = await svc.market.token_basic_info(chain_index, addr)
        except ClientError:
            pass

    async def _okx_listing() -> None:
        """Resolve whether the token is tradable on OKX spot.

        Matching is strict: the symbol must exist as a live SPOT instrument.
        A name collision on a different asset is the most expensive possible bug
        here, so an unresolved symbol yields False, never a guess.
        """
        if not ref.symbol:
            b.okx_available = None
            return
        try:
            inst = await svc.trade.find_spot_instrument(ref.symbol, svc.settings.okx_quote_ccy)
        except ClientError as e:
            log.info("okx instrument lookup failed for %s: %s", ref.symbol, e)
            b.okx_available = None
            return
        b.okx_inst_id = inst
        b.okx_available = inst is not None
        if inst:
            try:
                b.okx_book = await svc.trade.top_of_book(inst)
            except ClientError:
                b.okx_book = (None, None)

    async def _dexscreener() -> None:
        """Primary free market source. Fills the two holes nothing else covered:
        buy/sell counts and per-window volume."""
        if not svc.dexscreener.enabled:
            return
        try:
            m = await svc.dexscreener.token_market(addr)
        except ClientError as e:
            log.info("dexscreener failed for %s: %s", addr, e)
            return
        if not m:
            return
        b.dexscreener = m
        b.dex_pool_price = m.get("price_usd")
        b.dex_pool_liquidity_usd = m.get("liquidity_usd")
        b.buys_24h = m.get("buys_24h")
        b.sells_24h = m.get("sells_24h")
        if not ref.symbol and m.get("symbol"):
            ref.symbol = m["symbol"]
        if not ref.name and m.get("name"):
            ref.name = m["name"]

    async def _geckoterminal() -> None:
        """Independent second price source — without it, price reconciliation
        has nothing to cross-check and LIVE_BUY stays unreachable."""
        if not svc.geckoterminal.enabled:
            return
        try:
            b.geckoterminal = await svc.geckoterminal.token_market(addr)
        except ClientError as e:
            log.info("geckoterminal failed for %s: %s", addr, e)

    async def _onchain() -> None:
        if not svc.node.enabled:
            return
        try:
            supply_raw = await svc.node.erc20_total_supply(addr)
            decimals = await svc.node.erc20_decimals(addr)
            b.onchain_decimals = decimals
            if supply_raw is not None and decimals is not None:
                b.onchain_supply = supply_raw / (10**decimals)
            if not ref.symbol:
                ref.symbol = await svc.node.erc20_symbol(addr)
            if not ref.name:
                ref.name = await svc.node.erc20_name(addr)
        except ClientError as e:
            log.info("onchain erc20 read failed for %s: %s", addr, e)

        try:
            b.contract_flags, b.contract_detail = await svc.node.contract_risk_flags(addr)
        except ClientError as e:
            log.info("contract scan failed for %s: %s", addr, e)

        try:
            latest = await svc.node.block_number()
            if latest:
                deploy_block = await svc.node.find_deploy_block(addr, latest)
                if deploy_block is not None:
                    ts = await svc.node.get_block_timestamp(deploy_block)
                    if ts:
                        b.deployed_at = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        except ClientError as e:
            log.info("deploy-block lookup failed for %s: %s", addr, e)

    async def _verification() -> None:
        if not svc.explorer.enabled:
            return
        b.contract_verified = await svc.explorer.is_verified(addr)
        b.explorer_counters = await svc.explorer.token_counters(addr)

    async def _holders() -> None:
        # Preferred: indexed holder list from the Data API.
        if svc.data.enabled:
            try:
                res = await svc.data.token_holders(addr, limit=100)
                b.rh_holders = res
                balances = [
                    h["balance"]
                    for h in res["holders"]
                    if h.get("balance") and str(h.get("address", "")).lower() not in NON_HOLDER_ADDRESSES
                ]
                if balances:
                    b.holder_balances = balances
                    b.holder_basis_is_partial = False
                    return
            except ClientError as e:
                log.info("rh_data holders failed for %s: %s", addr, e)

        # Fallback: explorer top-holders list.
        if svc.explorer.enabled:
            items = await svc.explorer.token_holders(addr)
            balances = []
            for it in items:
                bal = to_float(it.get("value"))
                holder = (it.get("address") or {})
                haddr = holder.get("hash") if isinstance(holder, dict) else holder
                if bal and str(haddr or "").lower() not in NON_HOLDER_ADDRESSES:
                    dec = b.onchain_decimals if b.onchain_decimals is not None else 18
                    balances.append(bal / (10**dec))
            if balances:
                b.holder_balances = balances
                b.holder_basis_is_partial = False

    await asyncio.gather(
        _dexscreener(), _geckoterminal(), _okx(), _okx_listing(),
        _onchain(), _verification(), _holders(), return_exceptions=True
    )

    # Oracle sanity check on the quote asset, when configured.
    if svc.chainlink.enabled and ref.symbol:
        b.chainlink_price = await svc.chainlink.latest_price(ref.symbol)

    b.prev_holders = history.get("prev_holders")
    b.prev_holders_at = history.get("prev_holders_at")
    b.price_history_7d = history.get("price_history_7d", [])
    return b


# ===========================================================================
# History
# ===========================================================================
def load_history(session: Session, token_id: int) -> dict[str, Any]:
    week_ago = utcnow() - dt.timedelta(days=7)
    rows = session.execute(
        select(TokenSnapshot)
        .where(TokenSnapshot.token_id == token_id, TokenSnapshot.captured_at >= week_ago)
        .order_by(desc(TokenSnapshot.captured_at))
        .limit(2016)  # 7d at 5-minute cadence
    ).scalars().all()

    prices = [r.price_usd for r in rows if r.price_usd]

    # Holder baseline: the oldest snapshot within the last ~36h that has a count.
    target = utcnow() - dt.timedelta(hours=24)
    with_holders = [r for r in rows if r.unique_holders]
    baseline = min(
        with_holders,
        key=lambda r: abs((_aware(r.captured_at) - target).total_seconds()),
        default=None,
    )

    return {
        "prev_holders": baseline.unique_holders if baseline else None,
        "prev_holders_at": _aware(baseline.captured_at) if baseline else None,
        "price_history_7d": list(reversed(prices)),
    }


def _aware(d: dt.datetime) -> dt.datetime:
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def recent_evaluations(session: Session, token_id: int, limit: int = 10) -> list[Evaluation]:
    return list(
        session.execute(
            select(Evaluation)
            .where(Evaluation.token_id == token_id)
            .order_by(desc(Evaluation.created_at))
            .limit(limit)
        ).scalars().all()
    )


def count_consecutive_passes(evals: list[Evaluation]) -> int:
    """How many of the most recent evaluations were clean, unbroken."""
    n = 0
    for e in evals:  # already newest-first
        if e.hard_fail or e.insufficient_data or e.state not in ("PAPER_BUY", "LIVE_BUY", "ALERT"):
            break
        n += 1
    return n


# ===========================================================================
# Persistence
# ===========================================================================
def upsert_token(session: Session, ref: TokenRef, bundle: RawBundle) -> Token:
    tok = session.execute(
        select(Token).where(Token.chain == ref.chain, Token.address == ref.address)
    ).scalar_one_or_none()

    if tok is None:
        tok = Token(chain=ref.chain, address=ref.address)
        session.add(tok)

    tok.symbol = ref.symbol or tok.symbol
    tok.name = ref.name or tok.name
    tok.decimals = bundle.onchain_decimals if bundle.onchain_decimals is not None else tok.decimals
    tok.total_supply = bundle.onchain_supply if bundle.onchain_supply is not None else tok.total_supply
    tok.deployed_at = bundle.deployed_at or tok.deployed_at
    tok.contract_verified = (
        bundle.contract_verified if bundle.contract_verified is not None else tok.contract_verified
    )
    tok.is_proxy = bundle.contract_detail.get("is_proxy", tok.is_proxy)
    if bundle.okx_available is not None:
        tok.okx_available = bundle.okx_available
        tok.okx_inst_id = bundle.okx_inst_id
        tok.okx_checked_at = utcnow()

    session.flush()
    return tok


def persist_snapshot(session: Session, token: Token, snap: NormalizedSnapshot) -> TokenSnapshot:
    row = TokenSnapshot(
        token_id=token.id,
        captured_at=snap.captured_at,
        price_usd=snap.price_usd,
        price_sources=snap.price_sources,
        price_divergence_pct=snap.price_divergence_pct,
        liquidity_usd=snap.liquidity_usd,
        fdv_usd=snap.fdv_usd,
        market_cap_usd=snap.market_cap_usd,
        volume_5m=snap.volume_5m,
        volume_1h=snap.volume_1h,
        volume_24h=snap.volume_24h,
        tx_count_24h=snap.tx_count_24h,
        buy_ratio_24h=snap.buy_ratio_24h,
        unique_holders=snap.unique_holders,
        top1_holder_pct=snap.top1_holder_pct,
        top10_holder_pct=snap.top10_holder_pct,
        whale_concentration=snap.whale_concentration,
        holder_growth_24h_pct=snap.holder_growth_24h_pct,
        spread_bps=snap.spread_bps,
        slippage_bps=snap.slippage_bps,
        slippage_notional_usd=snap.slippage_notional_usd,
        token_age_hours=snap.token_age_hours,
        price_change_1h_pct=snap.price_change_1h_pct,
        price_change_24h_pct=snap.price_change_24h_pct,
        price_vs_7d_base_pct=snap.price_vs_7d_base_pct,
        drawdown_from_ath_pct=snap.drawdown_from_ath_pct,
        tokenomics_flags=snap.tokenomics_flags,
        contract_flags=snap.contract_flags,
        sniper_wallet_pct=snap.sniper_wallet_pct,
        bundled_buy_pct=snap.bundled_buy_pct,
        days_to_major_unlock=snap.days_to_major_unlock,
        sources=snap.sources,
        missing_fields=snap.missing_fields,
        raw=snap.raw,
    )
    session.add(row)
    session.flush()
    return row


def persist_evaluation(session: Session, token: Token, snapshot_row: TokenSnapshot, decision) -> Evaluation:
    ev = Evaluation(
        token_id=token.id,
        snapshot_id=snapshot_row.id,
        score_total=decision.score.total,
        score_liquidity=decision.score.points("liquidity"),
        score_holders=decision.score.points("holders"),
        score_volume=decision.score.points("volume"),
        score_tokenomics=decision.score.points("tokenomics"),
        score_momentum=decision.score.points("momentum"),
        state=decision.state.value,
        hard_fail=decision.hard_fail,
        insufficient_data=decision.insufficient_data,
        gate_results=[g.as_dict() for g in decision.gates],
        score_reasons=[{"component": c.name, "points": c.points, "reasons": c.reasons}
                       for c in decision.score.components],
        decision_reason=decision.reason,
    )
    session.add(ev)
    session.flush()
    return ev


# ===========================================================================
# Per-token pipeline
# ===========================================================================
async def process_token(svc: Services, ref: TokenRef, c: Settings) -> dict[str, Any]:
    with session_scope() as session:
        existing = session.execute(
            select(Token).where(Token.chain == ref.chain, Token.address == ref.address)
        ).scalar_one_or_none()
        history = load_history(session, existing.id) if existing else {}
        prior_evals = recent_evaluations(session, existing.id) if existing else []
        prior_liquidity = None
        if existing:
            last = session.execute(
                select(TokenSnapshot)
                .where(TokenSnapshot.token_id == existing.id)
                .order_by(desc(TokenSnapshot.captured_at))
                .limit(1)
            ).scalar_one_or_none()
            prior_liquidity = last.liquidity_usd if last else None
        consecutive = count_consecutive_passes(prior_evals)
        recent_scores = [e.score_total for e in prior_evals[:5]]

    bundle = await collect(svc, ref, history)
    snap = normalize(bundle, c)

    gates = evaluate_gates(snap, c)
    score = score_snapshot(snap, c)

    killed, kill_reason = killswitch.is_killed()
    safe, safe_reason = killswitch.safe_mode_status()

    with session_scope() as session:
        token = upsert_token(session, ref, bundle)
        snapshot_row = persist_snapshot(session, token, snap)

        exposure = exposure_for(session, token.id, mode="LIVE" if c.run_mode == "LIVE" else "PAPER")
        exp_v = check_exposure(exposure, c.position_usd, c)

        ctx = DecisionContext(
            recent_scores=recent_scores + [score.total],
            consecutive_passes=consecutive + 1,
            kill_switch=killed,
            safe_mode=safe,
            safe_mode_reason=safe_reason or kill_reason,
            run_mode=c.run_mode,
            exposure_ok=exp_v.allowed,
            exposure_reason="; ".join(exp_v.reasons) if not exp_v.allowed else "",
            okx_available=snap.okx_available,
        )

        decision = decide(snap, gates, score, ctx, c)
        ev = persist_evaluation(session, token, snapshot_row, decision)

        await dispatch(session, snap, decision, c, token_id=token.id, evaluation_id=ev.id)

        order_info: dict[str, Any] | None = None

        if decision.state == DecisionState.PAPER_BUY and c.paper_enabled:
            record, verdict = simulate_buy(
                session, token, ev, snap.okx_inst_id or f"{ref.symbol}-{c.okx_quote_ccy}",
                bundle.okx_book[0], bundle.okx_book[1], snap.price_usd, c,
                entry_reason=decision.reason,
            )
            order_info = {
                "mode": "PAPER",
                "placed": record is not None,
                "status": record.status if record else None,
                "reasons": verdict.reasons,
            }

        elif decision.state == DecisionState.LIVE_BUY:
            record, verdict = await execute_live_buy(
                session, token, ev, svc.trade, c,
                inst_id=snap.okx_inst_id or "",
                reference_price=snap.price_usd,
                liquidity_at_decision=prior_liquidity,
                liquidity_now=snap.liquidity_usd,
                slippage_now=snap.slippage_bps,
                entry_reason=decision.reason,
            )
            order_info = {
                "mode": "LIVE",
                "placed": record is not None and record.status in ("live", "filled", "partially_filled"),
                "status": record.status if record else None,
                "reasons": verdict.reasons,
            }

        return {
            "token": ref.address,
            "symbol": ref.symbol,
            "state": decision.state.value,
            "score": score.total,
            "reason": decision.reason,
            "order": order_info,
        }


async def dry_run_token(svc: Services, ref: TokenRef, c: Settings):
    """Evaluate one token and return the result **without writing anything**.

    Used by the UI's inspector so an operator can examine a contract without
    adding it to the tracked set or creating snapshot rows. Same code path as
    `process_token` up to the decision; only the persistence and execution
    steps are omitted.
    """
    with session_scope() as session:
        existing = session.execute(
            select(Token).where(Token.chain == ref.chain, Token.address == ref.address)
        ).scalar_one_or_none()
        history = load_history(session, existing.id) if existing else {}
        prior_evals = recent_evaluations(session, existing.id) if existing else []

    bundle = await collect(svc, ref, history)
    snap = normalize(bundle, c)
    gates = evaluate_gates(snap, c)
    score = score_snapshot(snap, c)

    killed, kill_reason = killswitch.is_killed()
    safe, safe_reason = killswitch.safe_mode_status()

    ctx = DecisionContext(
        recent_scores=[e.score_total for e in prior_evals[:5]] + [score.total],
        consecutive_passes=count_consecutive_passes(prior_evals) + 1,
        kill_switch=killed,
        safe_mode=safe,
        safe_mode_reason=safe_reason or kill_reason,
        run_mode=c.run_mode,
        okx_available=snap.okx_available,
    )
    return snap, decide(snap, gates, score, ctx, c)


# ===========================================================================
# Cycle
# ===========================================================================
async def run_cycle(svc: Services | None = None) -> dict[str, Any]:
    c = get_settings()
    own = svc is None
    svc = svc or build_services(c)
    started = utcnow()

    try:
        killed, kill_reason = killswitch.is_killed()
        if killed:
            log.warning("kill switch engaged (%s) — screening continues, execution disabled", kill_reason)

        if c.run_mode == "LIVE" and svc.trade.credentialed:
            safe, reason = await killswitch.evaluate_safe_mode(svc.trade)
            if safe:
                log.warning("safe mode active: %s", reason)

        refs = await discover_tokens(svc)
        log.info("cycle start: %d candidate tokens", len(refs))

        results: list[dict[str, Any]] = []
        for ref in refs:
            try:
                results.append(await process_token(svc, ref, c))
            except Exception as e:  # noqa: BLE001 - one bad token must not kill the cycle
                log.exception("token %s failed: %s", ref.address, e)
                results.append({"token": ref.address, "state": "ERROR", "reason": str(e)})

        summary = {
            "started_at": started.isoformat(),
            "finished_at": utcnow().isoformat(),
            "duration_s": (utcnow() - started).total_seconds(),
            "tokens": len(results),
            "by_state": _tally(results),
            "results": results,
        }
        log.info("cycle done in %.1fs: %s", summary["duration_s"], summary["by_state"])
        return summary
    finally:
        if own:
            await svc.aclose()


def _tally(results: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r["state"]] = out.get(r["state"], 0) + 1
    return out
