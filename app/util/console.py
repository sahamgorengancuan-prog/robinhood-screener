"""Console capability detection — required for Windows `cmd.exe`.

Two things break a Python CLI on a default Windows console and both are silent
until they aren't:

1. **Encoding.** `cmd.exe` defaults to a legacy code page (cp437/cp850/cp1252).
   Printing an emoji or an arrow raises `UnicodeEncodeError` and kills the
   process mid-run. Every status icon and every alert in this project contains
   non-ASCII characters, so this is not theoretical.

2. **ANSI colour.** Windows 10+ supports VT sequences but only after they are
   explicitly enabled for the console handle. Without that, `\033[1m` prints as
   literal garbage.

This module fixes what it can (switch stdout to UTF-8, ask Windows to enable VT)
and reports what it cannot, so callers can fall back to ASCII instead of
crashing.
"""

from __future__ import annotations

import os
import sys

_UNICODE_OK: bool | None = None
_ANSI_OK: bool | None = None


def _reconfigure_utf8() -> None:
    """Force UTF-8 on stdout/stderr where the interpreter allows it.

    `errors="replace"` is deliberate: a mangled character is an acceptable
    outcome, a crashed screening cycle is not.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _enable_windows_vt() -> bool:
    """Ask the Windows console to interpret ANSI escapes. Returns success."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        )
    except Exception:  # noqa: BLE001 - detection must never raise
        return False


def _can_encode(sample: str, enc: str | None) -> bool:
    try:
        sample.encode(enc or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _detect_unicode(original_encoding: str | None) -> bool:
    """Decide whether emoji will actually *render*, not just not crash.

    Explicit override wins:  SCREENER_UNICODE=1 / SCREENER_ASCII=1.

    On Windows this is deliberately pessimistic. Even after switching the code
    page to UTF-8, the classic `cmd.exe` console host renders emoji as boxes or
    mojibake because of its raster font — so we only claim Unicode where we know
    it works: Windows Terminal (`WT_SESSION`) or the VS Code terminal. Plain
    `cmd.exe` and the legacy PowerShell console get clean ASCII markers instead.
    Printing garbage is not better than printing `[ OK ]`.
    """
    if os.environ.get("SCREENER_UNICODE") == "1":
        return True
    if os.environ.get("SCREENER_ASCII") == "1":
        return False

    if os.name == "nt":
        return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM") == "vscode")

    return _can_encode("🟢→·✓", original_encoding)


def init_console() -> None:
    """Call once at process start, before any printing."""
    global _UNICODE_OK, _ANSI_OK

    # Capture the real encoding before we override it — that is what determines
    # whether the terminal can render, not what we force the stream to emit.
    original = getattr(sys.stdout, "encoding", None)
    _UNICODE_OK = _detect_unicode(original)

    # Always switch to UTF-8 with errors="replace" regardless: it guarantees no
    # UnicodeEncodeError can kill a screening cycle, whichever markers we use.
    _reconfigure_utf8()

    _ANSI_OK = _enable_windows_vt() and bool(getattr(sys.stdout, "isatty", lambda: False)())


def unicode_ok() -> bool:
    if _UNICODE_OK is None:
        init_console()
    return bool(_UNICODE_OK)


def ansi_ok() -> bool:
    if _ANSI_OK is None:
        init_console()
    return bool(_ANSI_OK)


def bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if ansi_ok() else text


def dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if ansi_ok() else text


# Status markers. The ASCII column is what a default cmd.exe window gets.
_ICONS = {
    "OK": ("🟢", "[+]"),
    "WARN": ("🟡", "[!]"),
    "FAIL": ("🔴", "[x]"),
    "SKIP": ("⚪", "[-]"),
}


def status_icon(status: str) -> str:
    uni, ascii_ = _ICONS.get(status, ("⚪", "[ ?? ]"))
    return uni if unicode_ok() else ascii_


def safe(text: str) -> str:
    """Downgrade a string to ASCII when the console cannot render it.

    Used for alert bodies, which contain arrows and box characters.
    """
    if unicode_ok():
        return text
    replacements = {
        "→": "->", "←": "<-", "·": "-", "—": "-", "–": "-",
        "✓": "+", "✕": "x", "✗": "x", "▪": "*", "•": "*",
        "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
        "±": "+/-", "×": "x", "≥": ">=", "≤": "<=",
    }
    for a, b in replacements.items():
        text = text.replace(a, b)
    return text.encode("ascii", "replace").decode("ascii")
