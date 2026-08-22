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
def updates_of(current, form):
    return merge_form(current, form)[0]


def test_a_blank_secret_never_erases_a_stored_one():
    """The form shows *** rather than the value, so a reload-then-save must not
    wipe the keys. This is the property the whole blank-handling exists for."""
    current = {"OKX_API_SECRET": "real-secret"}
    updates, kept = merge_form(current, {"OKX_API_SECRET": ""})
    assert updates == {}
    assert kept == ["OKX_API_SECRET"], "the caller must be able to say it was kept"


def test_a_blank_run_mode_is_refused():
    """Clearing RUN_MODE would leave the config unloadable."""
    assert updates_of({"RUN_MODE": "PAPER"}, {"RUN_MODE": ""}) == {}


def test_a_blank_ordinary_field_clears_it():
    """The form is populated from disk before the operator touches it, so a
    field arriving empty was emptied on purpose. Treating that as "no change"
    made clearing impossible and reported it as "nothing changed" — untrue:
    something had changed, it was refused."""
    assert updates_of({"RH_NODE_RPC_URL": "https://old"}, {"RH_NODE_RPC_URL": ""}) \
        == {"RH_NODE_RPC_URL": ""}


def test_clearing_an_already_empty_field_is_not_a_change():
    """Otherwise every save would rewrite every blank key."""
    assert updates_of({"ALERT_WEBHOOK_URL": ""}, {"ALERT_WEBHOOK_URL": ""}) == {}
    assert updates_of({}, {"ALERT_WEBHOOK_URL": ""}) == {}


def test_redacted_placeholder_is_not_written_back():
    current = {"OKX_API_SECRET": "real-secret"}
    updates, kept = merge_form(current, {"OKX_API_SECRET": "***tersimpan***"})
    assert updates == {}
    assert kept == ["OKX_API_SECRET"]


def test_replacing_a_secret_works():
    assert updates_of({"OKX_API_KEY": "old"}, {"OKX_API_KEY": "new-dex-key"}) \
        == {"OKX_API_KEY": "new-dex-key"}


def test_changed_value_is_captured():
    assert updates_of({"RUN_MODE": "ALERT_ONLY"}, {"RUN_MODE": "PAPER"}) == {"RUN_MODE": "PAPER"}


def test_unchanged_value_is_skipped():
    assert updates_of({"RUN_MODE": "PAPER"}, {"RUN_MODE": "PAPER"}) == {}


def test_new_secret_is_captured():
    assert updates_of({}, {"OKX_API_SECRET": "brand-new"}) == {"OKX_API_SECRET": "brand-new"}


def test_whitespace_is_trimmed():
    assert updates_of({}, {"RH_NODE_RPC_URL": "  https://x  "}) == {"RH_NODE_RPC_URL": "https://x"}


def test_no_stored_secret_means_nothing_to_keep():
    """"Kept" must mean a value was actually preserved, not that a field was
    blank — otherwise the banner claims to be protecting something that is not
    there."""
    assert merge_form({}, {"OKX_API_SECRET": ""})[1] == []
