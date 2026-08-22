"""End-of-cycle digest: the top N screened tokens, sent even when all were rejected.

Why this exists
---------------
The alert path only fires for tokens that *passed*. On a chain this young that
can mean silence for days, which is indistinguishable from a broken bot. The
digest closes that gap: every cycle reports what it looked at and how it ranked,
so an operator can tell "nothing qualified" apart from "nothing ran".

What it must never become
-------------------------
A ranking of rejected tokens is one careless sentence away from reading as a
buy list. It is not one, and the message says so in its own words rather than
relying on the reader to remember. Three guards:

  * every entry carries its state and, when rejected, the reason it was rejected;
  * a digest whose entries are all REJECT says so in the header, in plain words;
  * when an upstream went dark during the cycle the ranking is marked unreliable,
    because a score computed with half the inputs missing ranks the tokens whose
    data happened to arrive, not the tokens that are best.

The digest never creates an Alert row, never dedupes against one, and cannot
influence a decision. It is a report.
"""

from __future__ import annotations

import logging

from app.config import Settings

log = logging.getLogger(__name__)

#: Rejection reasons are long and the interesting part is at the front.
REASON_CHARS = 140


def _short_address(address: str) -> str:
    return f"{address[:8]}…{address[-6:]}" if len(address) > 18 else address


def rank_results(results: list[dict], top_n: int) -> list[dict]:
    """Highest score first. Rows with no score sort last rather than as zero.

    A token that errored has no score, and treating that as 0.0 would quietly
    rank it against tokens that were actually measured.
    """
    scored = [r for r in results if isinstance(r.get("score"), (int, float))]
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top_n]


def build_digest(
    results: list[dict],
    *,
    tokens_screened: int,
    duration_s: float,
    dead_connections: list[str] | None = None,
    top_n: int = 3,
) -> str | None:
    """Render the digest, or None when there is nothing to report."""
    top = rank_results(results, top_n)
    if not top:
        return None

    dead = dead_connections or []
    states = {r.get("state") for r in top}
    all_rejected = states == {"REJECT"}

    lines: list[str] = []
    lines.append(f"📊 Ringkasan siklus — {tokens_screened} token discan dalam {duration_s:.0f}s")

    if all_rejected:
        lines.append("❌ Tidak ada yang lolos. Peringkat di bawah adalah "
                     "*yang paling tidak buruk*, bukan rekomendasi beli.")
    else:
        lines.append("Peringkat berdasarkan skor. Status tiap token tertera di bawah.")

    if dead:
        lines.append("")
        lines.append(f"⚠️ {len(dead)} endpoint mati siklus ini — peringkat ini TIDAK DAPAT DIPERCAYA.")
        lines.append("Skor dihitung tanpa sebagian input, jadi yang naik peringkat adalah token "
                     "yang datanya kebetulan sampai, bukan token yang terbaik.")
        for name in dead[:3]:
            lines.append(f"  • {name}")
        if len(dead) > 3:
            lines.append(f"  • … dan {len(dead) - 3} lainnya")

    lines.append("")
    for i, row in enumerate(top, 1):
        symbol = row.get("symbol") or "?"
        address = _short_address(str(row.get("token") or ""))
        score = row.get("score")
        state = row.get("state") or "?"
        lines.append(f"{i}. {symbol} — skor {score:.1f} — {state}")
        lines.append(f"   {address}")
        reason = str(row.get("reason") or "").strip()
        if reason and state == "REJECT":
            if len(reason) > REASON_CHARS:
                reason = reason[:REASON_CHARS].rstrip() + "…"
            lines.append(f"   ↳ {reason}")

    lines.append("")
    lines.append("Digest ini laporan, bukan sinyal. Tidak ada order yang dibuat darinya.")
    return "\n".join(lines)


async def send_digest(
    results: list[dict],
    c: Settings,
    *,
    tokens_screened: int,
    duration_s: float,
    dead_connections: list[str] | None = None,
) -> bool:
    """Deliver the digest to the configured sinks. Never raises into the cycle."""
    if not c.cycle_digest_enabled:
        return False

    body = build_digest(
        results,
        tokens_screened=tokens_screened,
        duration_s=duration_s,
        dead_connections=dead_connections,
        top_n=c.cycle_digest_top_n,
    )
    if body is None:
        return False

    # Imported here so the digest module stays importable without the sink
    # stack, which keeps it testable as a pure renderer.
    from app.alerts.sinks import _send_console, _send_telegram

    sent = False
    try:
        if c.telegram_bot_token and c.telegram_chat_id:
            sent = await _send_telegram(c.telegram_bot_token, c.telegram_chat_id, body)
        if c.alert_console:
            await _send_console("Ringkasan siklus", body)
    except Exception as exc:  # noqa: BLE001 — a report must never break a cycle
        log.warning("cycle digest delivery failed: %s", exc)
        return False
    return sent
