"""Per-cycle accounting of what every upstream data source actually did.

A screener that fails closed produces the same verdict — *unavailable, reject* —
whether a token genuinely has no liquidity pool or the API that would have
reported the pool answered `401`. Those two situations look identical in a gate
reason string, and they call for completely different responses from an
operator: one is the screener working, the other is the screener blind.

So every call is counted in one of three buckets:

``ok``      the source answered and produced usable data
``empty``   the source answered, and has nothing for this token — a normal,
            expected outcome for an untraded contract
``failed``  the call raised: HTTP error, timeout, auth rejection, bad payload

At the end of a cycle the tally is logged as a single block, and any source that
produced nothing at all is called out by name. That is the difference between
"read 22 scattered INFO lines and infer" and "see which connection is down".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Iterator

log = logging.getLogger(__name__)

#: Addresses and numeric ids are stripped out of endpoint paths, so 22 tokens
#: produce one row per endpoint rather than 22 near-identical rows.
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")
_NUMBER = re.compile(r"/\d+")


def endpoint_label(client: str, method: str, path: str) -> str:
    path = _ADDRESS.sub("{address}", path or "/")
    path = _NUMBER.sub("/{n}", path)
    return f"{client} {method.upper()} {path}"

#: Error text is truncated before it becomes a dictionary key, so a message
#: carrying a request id or a timestamp cannot spawn unbounded distinct entries.
_ERROR_SIGNATURE_CHARS = 140


def error_signature(exc: BaseException) -> str:
    text = str(exc).strip() or "(no message)"
    if len(text) > _ERROR_SIGNATURE_CHARS:
        text = text[:_ERROR_SIGNATURE_CHARS] + "…"
    return f"{type(exc).__name__}: {text}"


@dataclass
class SourceOutcome:
    ok: int = 0
    empty: int = 0
    failed: int = 0
    errors: Counter[str] = field(default_factory=Counter)

    @property
    def calls(self) -> int:
        return self.ok + self.empty + self.failed

    @property
    def silent(self) -> bool:
        """Answered nothing usable, all cycle, despite being asked."""
        return self.calls > 0 and self.ok == 0

    @property
    def worst_error(self) -> str | None:
        return self.errors.most_common(1)[0][0] if self.errors else None


class SourceReport:
    """Mutable tally shared by every token in one cycle."""

    def __init__(self) -> None:
        self._sources: dict[str, SourceOutcome] = {}
        self._conn: dict[str, SourceOutcome] = {}

    def _slot(self, name: str) -> SourceOutcome:
        return self._sources.setdefault(name, SourceOutcome())

    # -- connection health, recorded by the HTTP layer ----------------------
    # This is the authoritative answer to "is this connection erroring?".
    # Several clients turn a ClientError into `None` at their own boundary, so
    # by the time the pipeline sees the result a 403 is indistinguishable from
    # "this token has nothing". The transport layer still knows the difference.
    def http_ok(self, client: str, method: str, path: str) -> None:
        self._conn.setdefault(endpoint_label(client, method, path), SourceOutcome()).ok += 1

    def http_failed(self, client: str, method: str, path: str, exc: BaseException) -> bool:
        slot = self._conn.setdefault(endpoint_label(client, method, path), SourceOutcome())
        slot.failed += 1
        signature = error_signature(exc)
        first_time = signature not in slot.errors
        slot.errors[signature] += 1
        return first_time

    @property
    def connections(self) -> dict[str, SourceOutcome]:
        return dict(self._conn)

    def failing_connections(self) -> list[str]:
        return sorted(n for n, o in self._conn.items() if o.failed)

    def dead_connections(self) -> list[str]:
        """Endpoints that never once answered — the ones worth waking up for."""
        return sorted(n for n, o in self._conn.items() if o.silent and o.failed)

    # -- recording ---------------------------------------------------------
    def ok(self, name: str) -> None:
        self._slot(name).ok += 1

    def empty(self, name: str) -> None:
        self._slot(name).empty += 1

    def failed(self, name: str, exc: BaseException) -> bool:
        """Record a failure. Returns True the first time this exact error is
        seen for this source, so callers can log the detail once instead of
        once per token — 22 identical tracebacks bury the summary they were
        meant to support."""
        slot = self._slot(name)
        slot.failed += 1
        signature = error_signature(exc)
        first_time = signature not in slot.errors
        slot.errors[signature] += 1
        return first_time

    def record(self, name: str, value: object) -> None:
        """Classify a successful call by whether it actually produced data."""
        self.ok(name) if value else self.empty(name)

    # -- reading -----------------------------------------------------------
    @property
    def sources(self) -> dict[str, SourceOutcome]:
        return dict(self._sources)

    def silent_sources(self) -> list[str]:
        return sorted(n for n, o in self._sources.items() if o.silent)

    def failing_sources(self) -> list[str]:
        return sorted(n for n, o in self._sources.items() if o.failed)

    @staticmethod
    def _table(title: str, slots: dict[str, SourceOutcome]) -> list[str]:
        if not slots:
            return []
        width = max(len(n) for n in slots)
        lines = [title]
        for name in sorted(slots):
            o = slots[name]
            parts = [f"{o.ok:4d} ok"]
            if o.empty:
                parts.append(f"{o.empty:4d} no-data")
            if o.failed:
                parts.append(f"{o.failed:4d} FAILED")
            row = f"  {name:<{width}}  " + "   ".join(parts)
            if o.worst_error:
                row += f"   {o.worst_error}"
            lines.append(row)
        return lines

    def render(self, *, tokens: int | None = None) -> str:
        """The block written to the log at the end of a cycle.

        Two tables, because they answer two different questions and conflating
        them is what made a 403 look like an untraded token.
        """
        if not self._sources and not self._conn:
            return "source health: no upstream calls were made this cycle"

        suffix = f" — {tokens} token(s) this cycle" if tokens is not None else ""
        lines = [f"source health{suffix}"]

        lines += self._table("connections (is the endpoint answering)", self._conn)
        dead = self.dead_connections()
        if dead:
            lines.append("  -> never answered once: " + ", ".join(dead))

        data = self._table("data coverage (did a value actually arrive)", self._sources)
        if data:
            lines.append("")
            lines += data
            silent = self.silent_sources()
            if silent:
                lines.append(
                    "  -> produced nothing usable all cycle: " + ", ".join(silent)
                    + "  (every field these feed reads 'unavailable' and fails closed)"
                )
        return "\n".join(lines)

    def log(self, *, tokens: int | None = None) -> None:
        """WARNING when something is down, INFO when everything answered."""
        text = self.render(tokens=tokens)
        bad = self.failing_connections() or self.silent_sources()
        (log.warning if bad else log.info)("%s", text)


#: The report the HTTP layer should write to, if any. A ContextVar rather than
#: a parameter because every client method would otherwise have to thread it
#: through, and a diagnostic must never change the shape of the code it watches.
_CURRENT: ContextVar[SourceReport | None] = ContextVar("source_report", default=None)


def current_report() -> SourceReport | None:
    return _CURRENT.get()


@contextlib.contextmanager
def use_report(report: SourceReport) -> Iterator[SourceReport]:
    token = _CURRENT.set(report)
    try:
        yield report
    finally:
        _CURRENT.reset(token)


def record_http_ok(client: str, method: str, path: str) -> None:
    report = _CURRENT.get()
    if report is not None:
        report.http_ok(client, method, path)


def record_http_failure(client: str, method: str, path: str, exc: BaseException) -> None:
    report = _CURRENT.get()
    if report is not None and report.http_failed(client, method, path, exc):
        log.warning("connection %s failed: %s", endpoint_label(client, method, path),
                    error_signature(exc))


async def run_sources(
    report: SourceReport,
    sources: dict[str, Callable[[], Awaitable[object]]],
) -> None:
    """Run source coroutines concurrently without ever losing an exception.

    `asyncio.gather(..., return_exceptions=True)` is what keeps one dead upstream
    from killing a whole cycle — but it *returns* exceptions rather than raising
    them, and the returned list was being discarded. Anything that was not a
    `ClientError` handled inside the coroutine therefore disappeared with no log
    line at all: a timeout, a bad payload, an `AttributeError` in a parser.

    Here each result is attributed back to the source that produced it.
    """
    names: list[str] = list(sources)
    results: Iterable[object] = await asyncio.gather(
        *(sources[name]() for name in names), return_exceptions=True
    )
    for name, result in zip(names, results):
        if isinstance(result, asyncio.CancelledError):
            raise result  # cancellation is not a source failure
        if isinstance(result, BaseException):
            if report.failed(name, result):
                # First sighting: the traceback is worth its space, because an
                # exception reaching here was not handled by the source itself.
                log.warning(
                    "source %s raised %s: %s", name, type(result).__name__, result,
                    exc_info=result,
                )
            else:
                log.debug("source %s failed again: %s", name, error_signature(result))
