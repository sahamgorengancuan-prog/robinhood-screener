"""Database schema.

Five concerns, five tables:
  token             — identity, one row per (chain, contract)
  token_snapshot    — append-only time series of raw+normalized metrics
  evaluation        — one row per scoring run: score, gates, decision
  order_record      — every paper and live order, with the evaluation that
                      justified it (audit trail: no order exists without a
                      reason attached)
  alert / kv_state  — outbound notifications and small runtime flags
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Token(Base):
    __tablename__ = "token"

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), default="robinhood")
    address: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(200))
    decimals: Mapped[int | None] = mapped_column(Integer)
    total_supply: Mapped[float | None] = mapped_column(Float)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deployed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deployer: Mapped[str | None] = mapped_column(String(64))

    contract_verified: Mapped[bool | None] = mapped_column(Boolean)
    is_proxy: Mapped[bool | None] = mapped_column(Boolean)

    # OKX availability. None = never resolved, False = confirmed absent.
    okx_available: Mapped[bool | None] = mapped_column(Boolean)
    okx_inst_id: Mapped[str | None] = mapped_column(String(64))
    okx_token_id: Mapped[str | None] = mapped_column(String(128))
    okx_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    snapshots: Mapped[list["TokenSnapshot"]] = relationship(back_populates="token")
    evaluations: Mapped[list["Evaluation"]] = relationship(back_populates="token")

    __table_args__ = (UniqueConstraint("chain", "address", name="uq_token_chain_address"),)


class TokenSnapshot(Base):
    """Append-only. Never updated — reruns create new rows so history is intact."""

    __tablename__ = "token_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("token.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    # ---- price / liquidity
    price_usd: Mapped[float | None] = mapped_column(Float)
    price_sources: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    price_divergence_pct: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    fdv_usd: Mapped[float | None] = mapped_column(Float)
    market_cap_usd: Mapped[float | None] = mapped_column(Float)

    # ---- volume
    volume_5m: Mapped[float | None] = mapped_column(Float)
    volume_1h: Mapped[float | None] = mapped_column(Float)
    volume_24h: Mapped[float | None] = mapped_column(Float)
    tx_count_24h: Mapped[int | None] = mapped_column(Integer)
    buy_ratio_24h: Mapped[float | None] = mapped_column(Float)

    # ---- holders
    unique_holders: Mapped[int | None] = mapped_column(Integer)
    top1_holder_pct: Mapped[float | None] = mapped_column(Float)
    top10_holder_pct: Mapped[float | None] = mapped_column(Float)
    whale_concentration: Mapped[float | None] = mapped_column(Float)
    holder_growth_24h_pct: Mapped[float | None] = mapped_column(Float)

    # ---- microstructure
    spread_bps: Mapped[float | None] = mapped_column(Float)
    slippage_bps: Mapped[float | None] = mapped_column(Float)
    slippage_notional_usd: Mapped[float | None] = mapped_column(Float)

    # ---- momentum / age
    token_age_hours: Mapped[float | None] = mapped_column(Float)
    price_change_1h_pct: Mapped[float | None] = mapped_column(Float)
    price_change_24h_pct: Mapped[float | None] = mapped_column(Float)
    price_vs_7d_base_pct: Mapped[float | None] = mapped_column(Float)
    drawdown_from_ath_pct: Mapped[float | None] = mapped_column(Float)

    # ---- risk flags
    tokenomics_flags: Mapped[list[Any] | None] = mapped_column(JSON)
    contract_flags: Mapped[list[Any] | None] = mapped_column(JSON)
    sniper_wallet_pct: Mapped[float | None] = mapped_column(Float)
    bundled_buy_pct: Mapped[float | None] = mapped_column(Float)
    days_to_major_unlock: Mapped[float | None] = mapped_column(Float)

    # ---- provenance: which client produced which field, and what was missing
    sources: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    missing_fields: Mapped[list[Any] | None] = mapped_column(JSON)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    token: Mapped[Token] = relationship(back_populates="snapshots")

    __table_args__ = (Index("ix_snapshot_token_time", "token_id", "captured_at"),)


class Evaluation(Base):
    __tablename__ = "evaluation"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("token.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("token_snapshot.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    score_total: Mapped[float] = mapped_column(Float, default=0.0)
    score_liquidity: Mapped[float] = mapped_column(Float, default=0.0)
    score_holders: Mapped[float] = mapped_column(Float, default=0.0)
    score_volume: Mapped[float] = mapped_column(Float, default=0.0)
    score_tokenomics: Mapped[float] = mapped_column(Float, default=0.0)
    score_momentum: Mapped[float] = mapped_column(Float, default=0.0)

    state: Mapped[str] = mapped_column(String(16), default="REJECT", index=True)
    hard_fail: Mapped[bool] = mapped_column(Boolean, default=True)
    insufficient_data: Mapped[bool] = mapped_column(Boolean, default=True)

    gate_results: Mapped[list[Any] | None] = mapped_column(JSON)
    score_reasons: Mapped[list[Any] | None] = mapped_column(JSON)
    decision_reason: Mapped[str | None] = mapped_column(Text)

    token: Mapped[Token] = relationship(back_populates="evaluations")


class OrderRecord(Base):
    __tablename__ = "order_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("token.id", ondelete="CASCADE"), index=True)
    evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("evaluation.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    mode: Mapped[str] = mapped_column(String(8))  # PAPER | LIVE
    inst_id: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8), default="buy")
    ord_type: Mapped[str] = mapped_column(String(16), default="post_only")

    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))

    requested_usd: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    size_base: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(24), default="created", index=True)
    filled_base: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float)
    realized_slippage_bps: Mapped[float | None] = mapped_column(Float)
    fee_usd: Mapped[float | None] = mapped_column(Float)

    entry_reason: Mapped[str | None] = mapped_column(Text)
    exit_reason: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Alert(Base):
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("token.id", ondelete="SET NULL"), index=True)
    evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("evaluation.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    state: Mapped[str] = mapped_column(String(16))
    dedupe_key: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    delivered: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class KVState(Base):
    """Tiny key/value store for runtime flags (kill switch, safe mode, cursors)."""

    __tablename__ = "kv_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
