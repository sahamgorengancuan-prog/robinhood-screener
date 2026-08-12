"""Windows-environment guards.

The target deployment is Windows, but CI and development happen on Linux, so
the Windows-specific failure modes have to be asserted rather than noticed.
Each test here corresponds to a bug that actually occurred while building the
launchers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BATS = sorted(ROOT.glob("*.bat"))

LAUNCHERS = [
    "run_pipeline.bat",     # the one-click entry point
    "start_ui.bat",
    "setup.bat",
    "run_api.bat",
    "test_connection.bat",
    "run_tests.bat",
    "EMERGENCY_STOP.bat",
    "resume_trading.bat",
    "_env.bat",
]


# ------------------------------------------------------------------ presence
def test_every_launcher_exists():
    missing = [n for n in LAUNCHERS if not (ROOT / n).exists()]
    assert missing == [], f"missing launchers: {missing}"


# ------------------------------------------------------------ line endings
@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_bat_files_use_crlf(bat: Path):
    """cmd.exe can mis-parse goto labels and paren blocks in LF-only .bat files."""
    raw = bat.read_bytes()
    lone_lf = re.findall(rb"(?<!\r)\n", raw)
    assert not lone_lf, f"{bat.name} contains {len(lone_lf)} LF line endings; must be CRLF"


def test_gitattributes_pins_bat_to_crlf():
    """Otherwise a clone on Linux-normalised git checks out broken launchers."""
    text = (ROOT / ".gitattributes").read_text()
    assert "*.bat text eol=crlf" in text


# --------------------------------------------------------------- batch syntax
@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_no_unescaped_ampersand(bat: Path):
    """`&` splits commands even inside a `rem` line — REM does not protect it.

    An unescaped `&&` in a comment in _env.bat executed a `goto` to a
    non-existent label and aborted the whole bootstrap.
    """
    for i, line in enumerate(bat.read_text().splitlines(), 1):
        stripped = line.replace("2>&1", "").replace("^&", "")
        assert "&" not in stripped, f"{bat.name}:{i} has an unescaped '&': {line.strip()}"


def _code_lines(bat: Path) -> list[tuple[int, str]]:
    """Executable lines only — `rem` comments are inert."""
    return [
        (i, line)
        for i, line in enumerate(bat.read_text().splitlines(), 1)
        if not line.strip().lower().startswith("rem")
    ]


@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_no_redirect_directly_after_variable(bat: Path):
    """`echo ... %TIME%> file` parses the trailing digit as a stream handle."""
    for i, line in _code_lines(bat):
        assert not re.search(r"%[A-Za-z_]+%>", line), \
            f"{bat.name}:{i} redirects straight after a variable: {line.strip()}"


@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_goto_targets_exist(bat: Path):
    text = bat.read_text()
    labels = set(re.findall(r"^:([A-Za-z_][A-Za-z0-9_]*)", text, re.MULTILINE))
    for line in text.splitlines():
        if line.strip().lower().startswith("rem"):
            continue  # documentation, not executed
        for target in re.findall(r"goto\s+:?([A-Za-z_][A-Za-z0-9_]*)", line):
            assert target in labels, f"{bat.name}: goto :{target} has no matching label"


def test_env_bat_does_not_setlocal():
    """_env.bat must export PYTHON into the caller's environment."""
    assert "setlocal" not in (ROOT / "_env.bat").read_text().lower()


@pytest.mark.parametrize("bat", [p for p in BATS if p.name != "_env.bat"], ids=lambda p: p.name)
def test_launchers_pause_so_the_window_stays_open(bat: Path):
    """A double-clicked window that closes instantly hides every error."""
    assert "pause" in bat.read_text().lower(), f"{bat.name} never pauses"


@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_paths_are_anchored_to_the_script_directory(bat: Path):
    """Double-clicking runs with an arbitrary working directory."""
    text = bat.read_text()
    if "python" in text.lower() or "cd /d" in text.lower():
        assert "%~dp0" in text, f"{bat.name} uses paths that assume the working directory"


# -------------------------------------------------------------- config guards
def test_blank_chain_id_in_env_does_not_crash():
    """`.env.example` ships RH_CHAIN_ID= blank; the first run must not explode."""
    from app.config import Settings

    c = Settings(_env_file=None, rh_chain_id="")
    assert c.rh_chain_id is None


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_chain_id_variants(value):
    from app.config import Settings

    assert Settings(_env_file=None, rh_chain_id=value).rh_chain_id is None


def test_env_example_parses_as_settings(tmp_path):
    """Ship-blocking: a fresh copy of .env.example must load."""
    from app.config import Settings

    env = tmp_path / ".env"
    env.write_text((ROOT / ".env.example").read_text(), encoding="utf-8")
    c = Settings(_env_file=str(env))
    assert c.run_mode == "ALERT_ONLY"
    assert c.rh_chain_id is None
    assert c.kill_switch is False


# ------------------------------------------------------------------- console
def test_ascii_fallback_when_console_cannot_render(monkeypatch):
    import app.util.console as console

    monkeypatch.setenv("SCREENER_ASCII", "1")
    monkeypatch.setattr(console, "_UNICODE_OK", None)
    monkeypatch.setattr(console, "_ANSI_OK", None)
    console.init_console()
    assert console.unicode_ok() is False
    for status in ("OK", "WARN", "FAIL", "SKIP"):
        icon = console.status_icon(status)
        assert icon.isascii(), f"{status} icon is not ASCII: {icon!r}"


def test_safe_downgrades_non_ascii(monkeypatch):
    import app.util.console as console

    monkeypatch.setenv("SCREENER_ASCII", "1")
    monkeypatch.setattr(console, "_UNICODE_OK", None)
    console.init_console()
    out = console.safe("liquidity → $1,000 · 12% ✓")
    assert out.isascii()
    assert "->" in out


def test_unicode_kept_when_supported(monkeypatch):
    import app.util.console as console

    monkeypatch.setenv("SCREENER_UNICODE", "1")
    monkeypatch.delenv("SCREENER_ASCII", raising=False)
    monkeypatch.setattr(console, "_UNICODE_OK", None)
    console.init_console()
    assert console.safe("a → b") == "a → b"


def test_plain_windows_console_defaults_to_ascii(monkeypatch):
    """cmd.exe renders emoji as boxes even at code page 65001, so don't try."""
    import app.util.console as console

    monkeypatch.delenv("SCREENER_UNICODE", raising=False)
    monkeypatch.delenv("SCREENER_ASCII", raising=False)
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setattr(console.os, "name", "nt")
    assert console._detect_unicode("cp437") is False


def test_windows_terminal_gets_unicode(monkeypatch):
    import app.util.console as console

    monkeypatch.delenv("SCREENER_UNICODE", raising=False)
    monkeypatch.delenv("SCREENER_ASCII", raising=False)
    monkeypatch.setenv("WT_SESSION", "abc-123")
    monkeypatch.setattr(console.os, "name", "nt")
    assert console._detect_unicode("cp437") is True


def test_console_detection_never_raises(monkeypatch):
    import app.util.console as console

    monkeypatch.setattr(console.sys, "stdout", object())  # no encoding, no isatty
    console.init_console()
    assert isinstance(console.unicode_ok(), bool)
    assert isinstance(console.ansi_ok(), bool)


# ------------------------------------------------------------- one-click flow
def test_one_click_script_exists_and_is_importable():
    import importlib.util

    path = ROOT / "scripts" / "one_click.py"
    assert path.exists()
    spec = importlib.util.spec_from_file_location("one_click", path)
    assert spec and spec.loader
    spec.loader.exec_module(importlib.util.module_from_spec(spec))  # must not run main()


def test_run_pipeline_bat_invokes_the_one_click_script():
    text = (ROOT / "run_pipeline.bat").read_text()
    assert "one_click.py" in text
    assert "%*" in text, "arguments must be forwarded (--token, --ui, --skip-checks)"


def test_emergency_stop_needs_no_python():
    """It must work when the venv is broken — that is when you need it most."""
    code = " ".join(line for _, line in _code_lines(ROOT / "EMERGENCY_STOP.bat")).lower()
    assert "python" not in code
    assert "_env.bat" not in code
    assert "kill_switch" in code


def test_emergency_stop_writes_the_file_the_killswitch_reads():
    from app.config import Settings

    default_path = Settings(_env_file=None).kill_switch_file
    assert Path(default_path).name == "KILL_SWITCH"
