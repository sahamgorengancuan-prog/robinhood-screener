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
from app.pipeline.acceleration import Sample, acceleration
from app.pipeline.outcomes import label_snapshots
from app.pipeline.source_health import SourceReport, run_sources, use_report
from app.pipeline.normalize import NON_HOLDER_ADDRESSES, RawBundle, normalize
from app.pipeline.risk import evaluate_gates, summarize
from app.pipeline.scoring import score_snapshot
from app.schemas import DecisionState, NormalizedSnapshot, TokenRef
from app.services import Services, build_services
from app.alerts.digest import send_digest
from app.alerts.sinks import dispatch

log = logging.getLogger(__name__)

# How far back the on-chain fallback walks when rebuilding holders from logs.
HOLDER_LOG_LOOKBACK_BLOCKS = 200_000


# ===========================================================================
# Discovery
# ===========================================================================
async def discover_tokens(svc: Services) -> list[TokenRef]:
    """Candidate tokens for this cycle.

    Sources are merged and deduplicated: OKX's official hot-token feed for
    trending/active tokens, recent ERC-20 mint logs from the Node API for new
    contracts, then the local tracked set. Alchemy has no documented global
    token-list endpoint, so it is deliberately not used for discovery.
    """
    refs: list[TokenRef] = []

    chain_index = str(svc.settings.rh_chain_id or "")
    if svc.market.enabled and svc.settings.okx_hot_token_enabled and chain_index:
        try:
            rows = await svc.market.hot_tokens(
                chain_index,
                time_frame=svc.settings.okx_hot_token_timeframe,
                limit=svc.settings.max_tokens_per_cycle,
                filters={
                    "liquidityMin": svc.settings.min_liquidity_usd,
                    "volumeMin": svc.settings.min_volume_24h_usd,
                    "holdersMin": svc.settings.min_unique_holders,
                    "top10HoldPercentMax": svc.settings.max_top10_holder_pct,
                    "priceChangePercentMax": svc.settings.max_price_change_24h_pct,
                },
            )
            for item in rows:
                addr = item.get("tokenContractAddress")
                if not addr:
                    continue
                refs.append(
                    TokenRef(
                        address=str(addr).lower(),
                        symbol=item.get("tokenSymbol"),
                        name=item.get("tokenName"),
                        decimals=to_int(item.get("decimal")),
                        discovery_data=item,
                    )
                )
        except ClientError as e:
            log.error("token discovery via OKX hot-token failed: %s", e)

    if svc.node.enabled:
        try:
            latest = await svc.node.block_number()
            if latest is not None:
                start = max(0, latest - svc.settings.discovery_lookback_blocks + 1)
                for address in await svc.node.discover_minted_contracts(start, latest):
                    refs.append(TokenRef(address=address))
        except ClientError as e:
            log.error("token discovery via mint logs failed: %s", e)

    with session_scope() as s:
        rows = s.execute(
            select(Token).where(Token.muted.is_(False)).limit(svc.settings.max_tokens_per_cycle)
        ).scalars().all()
        refs.extend(
            TokenRef(chain=t.chain, address=t.address, symbol=t.symbol, name=t.name, decimals=t.decimals)
            for t in rows
        )

    unique: dict[str, TokenRef] = {}
    for ref in refs:
        unique.setdefault(ref.address.lower(), ref)

    return list(unique.values())[: svc.settings.max_tokens_per_cycle]


# ===========================================================================
# Collection
# ===========================================================================
async def collect(
    svc: Services,
    ref: TokenRef,
    history: dict[str, Any],
    report: SourceReport | None = None,
) -> RawBundle:
    """Gather everything about one token. Every source is individually guarded."""
    b = RawBundle(ref)
    addr = ref.address
    if ref.discovery_data:
        b.okx_hot = dict(ref.discovery_data)
        b.buys_24h = to_int(b.okx_hot.get("txsBuy"))
        b.sells_24h = to_int(b.okx_hot.get("txsSell"))

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
            if report.failed("okx.price_info", e):
                log.info("okx price_info failed for %s: %s", addr, e)
        else:
            report.record("okx.price_info", b.okx_price_info)
        try:
            b.okx_basic_info = await svc.market.token_basic_info(chain_index, addr)
        except ClientError as e:
            report.failed("okx.basic_info", e)
        else:
            report.record("okx.basic_info", b.okx_basic_info)
        for attr, factory in (
            ("okx_advanced_info", lambda: svc.market.advanced_info(chain_index, addr)),
            ("okx_holders", lambda: svc.market.holders(chain_index, addr)),
            ("okx_trades", lambda: svc.market.trades(chain_index, addr)),
            ("okx_liquidity", lambda: svc.market.top_liquidity(chain_index, addr)),
        ):
            label = "okx." + attr.removeprefix("okx_")
            try:
                setattr(b, attr, await factory())
            except ClientError as exc:
                if report.failed(label, exc):
                    log.info("okx %s failed for %s: %s", attr, addr, exc)
            else:
                report.record(label, getattr(b, attr))

    async def _okx_listing() -> None:
        """Resolve whether the token is tradable on OKX spot.

        Matching is contract-aware: symbol + chain label + ctAddr suffix + an
        exact live SPOT pair. Symbol-only matching can buy a different asset.
        """
        if not ref.symbol:
            b.okx_available = None
            return
        try:
            inst, availability, reason = await svc.trade.resolve_contract_spot(
                symbol=ref.symbol,
                contract_address=addr,
                chain_hint=svc.settings.okx_robinhood_chain_hint,
                quote_ccy=svc.settings.okx_quote_ccy,
            )
        except ClientError as e:
            if report.failed("okx_trade.instruments", e):
                log.info("okx instrument lookup failed for %s: %s", ref.symbol, e)
            b.okx_available = None
            b.okx_identity_reason = str(e)
            return
        else:
            report.record("okx_trade.instruments", availability is not None)
        b.okx_inst_id = inst
        b.okx_available = availability
        b.okx_identity_reason = reason
        if inst:
            try:
                b.okx_book = await svc.trade.top_of_book(inst)
                b.okx_book_slippage_bps = await svc.trade.buy_slippage_bps(
                    inst, svc.settings.position_usd
                )
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
            if report.failed("dexscreener", e):
                log.info("dexscreener failed for %s: %s", addr, e)
            return
        report.record("dexscreener", m)
        if not m:
            return
        b.dexscreener = m
        b.dex_pool_price = m.get("price_usd")
        b.dex_pool_liquidity_usd = m.get("liquidity_usd")
        b.buys_24h = b.buys_24h if b.buys_24h is not None else m.get("buys_24h")
        b.sells_24h = b.sells_24h if b.sells_24h is not None else m.get("sells_24h")
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
            if report.failed("geckoterminal", e):
                log.info("geckoterminal failed for %s: %s", addr, e)
        else:
            report.record("geckoterminal", b.geckoterminal)

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
            if report.failed("node.erc20", e):
                log.info("onchain erc20 read failed for %s: %s", addr, e)
        else:
            report.record("node.erc20", b.onchain_total_supply)

        try:
            b.contract_flags, b.contract_detail = await svc.node.contract_risk_flags(addr)
        except ClientError as e:
            if report.failed("node.bytecode", e):
                log.info("contract scan failed for %s: %s", addr, e)
        else:
            report.record("node.bytecode", b.contract_detail or b.contract_flags)

        try:
            latest = await svc.node.block_number()
            b.latest_block = latest
            if latest:
                deploy_block = await svc.node.find_deploy_block(addr, latest)
                if deploy_block is not None:
                    ts = await svc.node.get_block_timestamp(deploy_block)
                    if ts:
                        b.deployed_at = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        except ClientError as e:
            if report.failed("node.deploy_block", e):
                log.info("deploy-block lookup failed for %s: %s", addr, e)
        else:
            report.record("node.deploy_block", b.deployed_at)

    async def _verification() -> None:
        if not svc.explorer.enabled:
            return
        # No try/except here on purpose: a failure propagates to run_sources,
        # which attributes it to "explorer" and logs it with a traceback.
        b.contract_verified = await svc.explorer.is_verified(addr)
        report.record("explorer.verified", b.contract_verified is not None)
        b.explorer_counters = await svc.explorer.token_counters(addr)
        report.record("explorer.counters", b.explorer_counters)

    async def _data_metadata() -> None:
        if not svc.data.enabled:
            return
        try:
            b.rh_meta = await svc.data.token_metadata(addr)
        except ClientError as exc:
            if report.failed("rh_data.metadata", exc):
                log.info("alchemy token metadata failed for %s: %s", addr, exc)
        else:
            report.record("rh_data.metadata", b.rh_meta)

    async def _indexed_activity() -> None:
        if not svc.data.enabled or b.latest_block is None:
            return
        start = max(0, b.latest_block - svc.settings.discovery_lookback_blocks + 1)
        try:
            result = await svc.data.asset_transfers(
                from_block=hex(start), contract_addresses=[addr], max_count=1000
            )
            rows = result.get("transfers")
            b.rh_transfers = rows if isinstance(rows, list) else []
        except ClientError as exc:
            if report.failed("rh_data.transfers", exc):
                log.info("alchemy transfer history failed for %s: %s", addr, exc)
        else:
            report.record("rh_data.transfers", b.rh_transfers)

    async def _holders() -> None:
        # Alchemy's documented Token API is wallet-centric, not a global holder
        # list. OKX holder percentages are normalized directly; Blockscout
        # balances remain a free fallback.
        if b.okx_holders:
            return
        if svc.explorer.enabled:
            items = await svc.explorer.token_holders(addr)
            balances = []
            excluded = set(NON_HOLDER_ADDRESSES)
            excluded.update(
                str(pool.get("poolAddress") or "").lower()
                for pool in b.okx_liquidity
                if pool.get("poolAddress")
            )
            for it in items:
                bal = to_float(it.get("value"))
                holder = (it.get("address") or {})
                haddr = holder.get("hash") if isinstance(holder, dict) else holder
                if bal and str(haddr or "").lower() not in excluded:
                    dec = b.onchain_decimals if b.onchain_decimals is not None else 18
                    balances.append(bal / (10**dec))
            if balances:
                b.holder_balances = balances
                b.holder_basis_is_partial = False

    # Metadata/decimals and pool addresses must land before venue identity and
    # holder calculations. This avoids the old race that silently assumed 18
    # decimals while an on-chain read was still running.
    report = report if report is not None else SourceReport()
    await run_sources(report, {
        "dexscreener": _dexscreener,
        "geckoterminal": _geckoterminal,
        "okx": _okx,
        "node": _onchain,
        "explorer": _verification,
        "rh_data": _data_metadata,
    })
    if b.okx_basic_info:
        ref.symbol = ref.symbol or b.okx_basic_info.get("tokenSymbol")
        ref.name = ref.name or b.okx_basic_info.get("tokenName")
    if b.rh_meta:
        ref.symbol = ref.symbol or b.rh_meta.get("symbol")
        ref.name = ref.name or b.rh_meta.get("name")
    await run_sources(report, {
        "okx_trade": _okx_listing,
        "holders": _holders,
        "activity": _indexed_activity,
    })

    # Oracle sanity check on the quote asset, when configured.
    if svc.chainlink.enabled and ref.symbol:
        b.chainlink_price = await svc.chainlink.latest_price(ref.symbol)

    b.prev_holders = history.get("prev_holders")
    b.prev_holders_at = history.get("prev_holders_at")
    b.price_history_7d = history.get("price_history_7d", [])
    b.holder_series = history.get("holder_series", [])
    b.buy_count_series = history.get("buy_count_series", [])
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
        .limit(2016)  # supports up to a 5-minute cadence; default is 15 minutes
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

    # Full series for the acceleration layer. A single baseline supports a
    # growth rate; a second derivative needs the whole sequence.
    holder_series = [
        Sample(at=_aware(r.captured_at), value=float(r.unique_holders))
        for r in reversed(rows) if r.unique_holders is not None
    ]
    buy_series = [
        Sample(at=_aware(r.captured_at), value=float(r.buys_24h))
        for r in reversed(rows) if r.buys_24h is not None
    ]

    return {
        "prev_holders": baseline.unique_holders if baseline else None,
        "prev_holders_at": _aware(baseline.captured_at) if baseline else None,
        "price_history_7d": list(reversed(prices)),
        "holder_series": holder_series,
        "buy_count_series": buy_series,
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
        buys_24h=snap.buys_24h,
        sells_24h=snap.sells_24h,
        holder_growth_prev_pct=snap.holder_growth_prev_pct,
        holder_acceleration_pp=snap.holder_acceleration_pp,
        buy_count_growth_pct=snap.buy_count_growth_pct,
        buy_count_growth_prev_pct=snap.buy_count_growth_prev_pct,
        buy_count_acceleration_pp=snap.buy_count_acceleration_pp,
        buy_ratio_24h=snap.buy_ratio_24h,
        trade_sample_size=snap.trade_sample_size,
        unique_trader_ratio=snap.unique_trader_ratio,
        top_trader_volume_pct=snap.top_trader_volume_pct,
        filtered_trade_pct=snap.filtered_trade_pct,
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
        suspicious_holder_pct=snap.suspicious_holder_pct,
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
async def process_token(
    svc: Services, ref: TokenRef, c: Settings, report: SourceReport | None = None
) -> dict[str, Any]:
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

    bundle = await collect(svc, ref, history, report)
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

        # One tally for the whole cycle: 22 scattered INFO lines do not tell an
        # operator which connection is down, a single block at the end does.
        report = SourceReport()

        results: list[dict[str, Any]] = []
        with use_report(report):
            for ref in refs:
                try:
                    results.append(await process_token(svc, ref, c, report))
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
        # Labelling runs after screening, never before: it must not delay a
        # decision, and a failure here must not cost the cycle its results.
        try:
            with session_scope() as session:
                summary_labels = label_snapshots(session)
        except Exception:  # noqa: BLE001 - the dataset is not worth a dead cycle
            log.exception("forward-return labelling failed")
            summary_labels = {"labelled": 0, "snapshots": 0}

        report.log(tokens=len(results))
        summary["labels"] = summary_labels
        summary["connections"] = {
            name: {"ok": o.ok, "failed": o.failed, "error": o.worst_error}
            for name, o in report.connections.items()
        }
        summary["dead_connections"] = report.dead_connections()
        summary["sources"] = {
            name: {"ok": o.ok, "empty": o.empty, "failed": o.failed,
                   "error": o.worst_error}
            for name, o in report.sources.items()
        }
        summary["silent_sources"] = report.silent_sources()
        log.info("cycle done in %.1fs: %s", summary["duration_s"], summary["by_state"])

        # A report, not a signal: it creates no Alert row, dedupes against
        # nothing, and cannot influence a decision. It exists so that a cycle
        # which rejected everything is still visibly a cycle that ran.
        summary["digest_sent"] = await send_digest(
            results, c,
            tokens_screened=len(results),
            duration_s=summary["duration_s"],
            dead_connections=summary.get("dead_connections") or [],
        )
        return summary
    finally:
        if own:
            await svc.aclose()


def _tally(results: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r["state"]] = out.get(r["state"], 0) + 1
    return out
