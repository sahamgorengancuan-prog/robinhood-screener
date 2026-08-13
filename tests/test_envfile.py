"""Tests for writing `.env` from the control panel.

The UI owning configuration is what makes one-click setup work, and it is also
the only place the browser can change behaviour. These tests pin the three
properties that keep that safe: comments survive, blanks don't erase, and
secrets don't leak into the returned values.
"""

from __future__ import annotations

import pytest

from app.ui.envfile import SECRET_KEYS, merge_form, read_env, redact, write_env

SAMPLE = """\
# ---- core ----
RUN_MODE=ALERT_ONLY
LOG_LEVEL=INFO

# The chain ID is read from the node, never guessed.
RH_CHAIN_ID=
RH_NODE_RPC_URL=

# ---- secrets ----
OKX_API_SECRET=
POSITION_USD=25
"""


@pytest.fixture
def envfile_path(tmp_path):
    p = tmp_path / ".env"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


# ------------------------------------------------------------------- reading
def test_read_env_parses_values(envfile_path):
    values = read_env(envfile_path)
    assert values["RUN_MODE"] == "ALERT_ONLY"
    assert values["POSITION_USD"] == "25"
    assert values["RH_CHAIN_ID"] == ""


def test_read_env_skips_comments(envfile_path):
    assert not any(k.startswith("#") for k in read_env(envfile_path))


def test_read_missing_file_returns_empty(tmp_path):
    assert read_env(tmp_path / "nope.env") == {}


def test_read_strips_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text('RH_NODE_RPC_URL="https://x/rpc"\n', encoding="utf-8")
    assert read_env(p)["RH_NODE_RPC_URL"] == "https://x/rpc"


# ------------------------------------------------------------------- writing
def test_write_preserves_comments_and_order(envfile_path):
    write_env(envfile_path, {"RUN_MODE": "PAPER"})
    text = envfile_path.read_text()
    assert "# ---- core ----" in text
    assert "# The chain ID is read from the node, never guessed." in text
    assert text.index("RUN_MODE") < text.index("LOG_LEVEL")


def test_write_updates_in_place(envfile_path):
    write_env(envfile_path, {"RUN_MODE": "PAPER", "POSITION_USD": "10"})
    values = read_env(envfile_path)
    assert values["RUN_MODE"] == "PAPER"
    assert values["POSITION_USD"] == "10"
    # untouched keys survive
    assert values["LOG_LEVEL"] == "INFO"


def test_write_returns_only_changed_keys(envfile_path):
    changed = write_env(envfile_path, {"RUN_MODE": "ALERT_ONLY", "POSITION_USD": "50"})
    assert changed == ["POSITION_USD"], "unchanged values must not be reported as changed"


def test_write_appends_unknown_keys(envfile_path):
    write_env(envfile_path, {"BRAND_NEW_KEY": "42"})
    text = envfile_path.read_text()
    assert "BRAND_NEW_KEY=42" in text
    assert "added by the control panel" in text


def test_write_makes_a_backup(envfile_path):
    write_env(envfile_path, {"RUN_MODE": "PAPER"})
    backup = envfile_path.with_suffix(envfile_path.suffix + ".bak")
    assert backup.exists()
    assert "ALERT_ONLY" in backup.read_text(), "backup should hold the previous content"


def test_write_creates_from_example_when_missing(tmp_path):
    (tmp_path / ".env.example").write_text("RUN_MODE=ALERT_ONLY\n", encoding="utf-8")
    target = tmp_path / ".env"
    write_env(target, {"POSITION_USD": "5"})
    assert target.exists()
    assert read_env(target)["RUN_MODE"] == "ALERT_ONLY"


def test_written_file_ends_with_newline(envfile_path):
    write_env(envfile_path, {"RUN_MODE": "PAPER"})
    assert envfile_path.read_text().endswith("\n")


# -------------------------------------------------------------- form merging
def test_blank_field_does_not_erase_existing_value():
    current = {"OKX_API_SECRET": "real-secret", "RUN_MODE": "PAPER"}
    updates = merge_form(current, {"OKX_API_SECRET": "", "RUN_MODE": ""})
    assert updates == {}, "a blank form field must never clear a stored value"


def test_redacted_placeholder_is_not_written_back():
    """The form shows *** for stored secrets; submitting it must be a no-op."""
    current = {"OKX_API_SECRET": "real-secret"}
    updates = merge_form(current, {"OKX_API_SECRET": "***tersimpan***"})
    assert updates == {}


def test_changed_value_is_captured():
    updates = merge_form({"RUN_MODE": "ALERT_ONLY"}, {"RUN_MODE": "PAPER"})
    assert updates == {"RUN_MODE": "PAPER"}


def test_unchanged_value_is_skipped():
    assert merge_form({"RUN_MODE": "PAPER"}, {"RUN_MODE": "PAPER"}) == {}


def test_new_secret_is_captured():
    updates = merge_form({}, {"OKX_API_SECRET": "brand-new"})
    assert updates == {"OKX_API_SECRET": "brand-new"}


def test_whitespace_is_trimmed():
    assert merge_form({}, {"RH_NODE_RPC_URL": "  https://x  "}) == {"RH_NODE_RPC_URL": "https://x"}


# ---------------------------------------------------------------- redaction
def test_redact_masks_every_secret():
    values = {k: "sensitive" for k in SECRET_KEYS}
    values["RUN_MODE"] = "PAPER"
    out = redact(values)
    assert "sensitive" not in str(out)
    assert out["RUN_MODE"] == "PAPER"


def test_redact_distinguishes_set_from_unset():
    out = redact({"OKX_API_SECRET": "", "OKX_API_KEY": "x"})
    assert out["OKX_API_SECRET"] == ""
    assert out["OKX_API_KEY"] == "***set***"


def test_trading_credentials_are_treated_as_secret():
    for key in ("OKX_TRADE_API_KEY", "OKX_TRADE_API_SECRET", "OKX_TRADE_API_PASSPHRASE"):
        assert key in SECRET_KEYS
