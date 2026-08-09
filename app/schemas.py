"""Normalized in-memory schema shared by every pipeline stage.

The single most important rule in this file: **missing is not zero**.
Every metric is `None` until a source actually produced it. A `None` can never
satisfy a risk gate — it routes the token to WATCH or REJECT instead. This is
what stops the screener from buying something because an API returned an empty
body and a naive parser turned it into `0.0`.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DecisionState(str, Enum):
    REJECT = "REJECT"
    WATCH = "WATCH"
    ALERT = "ALERT"
    PAPER_BUY = "PAPER_BUY"
    LIVE_BUY = "LIVE_BUY"

    @property
    def rank(self) -> int:
        return {"REJECT": 0, "WATCH": 1, "ALERT": 2, "PAPER_BUY": 3, "LIVE_BUY": 4}[self.value]


class Severity(str, Enum):
    HARD = "HARD"          # unconditional reject
    LIVE_ONLY = "LIVE_ONLY"  # blocks LIVE_BUY, allows ALERT/PAPER_BUY
    SOFT = "SOFT"          # score penalty only
    DATA = "DATA"          # metric unavailable -> WATCH


class TokenRef(BaseModel):
    chain: str = "robinhood"
    address: str
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.address.lower()}"


class NormalizedSnapshot(BaseModel):
    """Every downstream stage reads only this. Clients never leak their shapes past
    the normalizer."""

    model_config = ConfigDict(extra="forbid")

    token: TokenRef
    captured_at: dt.datetime

    # price / liquidity
    price_usd: float | None = None
    price_sources: dict[str, float] = Field(default_factory=dict)
    price_divergence_pct: float | None = None
    liquidity_usd: float | None = None
    fdv_usd: float | None = None
    market_cap_usd: float | None = None
    total_supply: float | None = None
    circulating_supply: float | None = None

    # volume
    volume_5m: float | None = None
    volume_1h: float | None = None
    volume_24h: float | None = None
    tx_count_24h: int | None = None
    buy_ratio_24h: float | None = None

    # holders
    unique_holders: int | None = None
    top1_holder_pct: float | None = None
    top10_holder_pct: float | None = None
    whale_concentration: float | None = None
    holder_growth_24h_pct: float | None = None

    # microstructure
    spread_bps: float | None = None
    slippage_bps: float | None = None
    slippage_notional_usd: float | None = None

    # momentum / age
    token_age_hours: float | None = None
    price_change_1h_pct: float | None = None
    price_change_24h_pct: float | None = None
    price_vs_7d_base_pct: float | None = None
    drawdown_from_ath_pct: float | None = None

    # risk
    tokenomics_flags: list[str] = Field(default_factory=list)
    contract_flags: list[str] = Field(default_factory=list)
    contract_verified: bool | None = None
    is_proxy: bool | None = None
    sniper_wallet_pct: float | None = None
    bundled_buy_pct: float | None = None
    days_to_major_unlock: float | None = None

    # venue
    okx_available: bool | None = None
    okx_inst_id: str | None = None

    # provenance
    sources: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    def note_missing(self, field: str) -> None:
        if field not in self.missing_fields:
            self.missing_fields.append(field)

    def set_field(self, field: str, value: Any, source: str) -> None:
        """Assign a value and record where it came from. No-op for None so a
        later, poorer source cannot erase a good value."""
        if value is None:
            return
        setattr(self, field, value)
        self.sources[field] = source


class GateResult(BaseModel):
    name: str
    passed: bool
    severity: Severity
    reason: str
    value: Any = None
    threshold: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity.value,
            "reason": self.reason,
            "value": self.value,
            "threshold": self.threshold,
        }


class ScoreComponent(BaseModel):
    name: str
    weight: float
    ratio: float          # 0..1 quality within the component
    points: float         # ratio * weight
    reasons: list[str] = Field(default_factory=list)


class ScoreResult(BaseModel):
    total: float
    components: list[ScoreComponent]
    reasons: list[str] = Field(default_factory=list)

    def component(self, name: str) -> ScoreComponent | None:
        return next((c for c in self.components if c.name == name), None)

    def points(self, name: str) -> float:
        c = self.component(name)
        return c.points if c else 0.0


class Decision(BaseModel):
    state: DecisionState
    score: ScoreResult
    gates: list[GateResult]
    reason: str
    hard_fail: bool = False
    insufficient_data: bool = False

    @property
    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]
