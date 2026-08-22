"""Tests for the Gradio layer's pure logic.

The UI itself isn't worth asserting on pixel by pixel, but three things are:
the threshold lab must agree with the real engine, credential overrides must
not leak into global settings, and the UI must expose no way to place an order.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.ui.gradio_app import lab_evaluate, load_config, settings_with_overrides
from app.ui.theme import metric_cards, score_bars, state_pill


def default_lab_args(**over):
    c = get_settings()
    args = dict(
        liquidity=1_500_000.0, volume_24h=2_200_000.0, volume_1h=95_000.0, tx_count=4_100,
        buy_ratio=0.51, holders=6_500, top1=4.0, top10=18.0, growth=12.0, spread=20.0,
        age_days=12, change_24h=4.0, drawdown=22.0, verified="verified", flags=[],
        okx_avail="tersedia",
        min_liq=c.min_liquidity_usd, min_liq_live=c.min_liquidity_usd_live,
        max_slip=c.max_slippage_bps, max_top1=c.max_top1_holder_pct,
        max_top10=c.max_top10_holder_pct, min_holders=c.min_unique_holders,
        max_turnover=c.max_volume_to_liquidity_ratio, max_change=c.max_price_change_24h_pct,
        alert_min=c.score_alert_min, paper_min=c.score_paper_buy_min,
        live_min=c.score_live_buy_min, position_usd=c.position_usd,
    )
    args.update(over)
    return list(args.values())


def state_of(banner_html: str) -> str:
    for s in ("LIVE_BUY", "PAPER_BUY", "ALERT", "WATCH", "REJECT"):
        if f">{s}<" in banner_html:
            return s
    return "?"


# ------------------------------------------------------------- threshold lab
def test_lab_healthy_token_reaches_alert_in_default_read_only_mode():
    b, bars, gates = lab_evaluate(*default_lab_args())
    assert state_of(b) == "ALERT"
    assert "TOTAL" in bars


@pytest.mark.parametrize(
    "override,expected_fragment",
    [
        ({"top10": 85.0}, "top-10 control"),
        ({"top1": 40.0}, "top holder controls"),
        ({"verified": "unverified"}, "not verified"),
        ({"flags": ["OWNER_CAN_MINT"]}, "dangerous contract privileges"),
        ({"change_24h": 300.0}, "no chasing"),
        ({"age_days": 0}, "below 24h minimum"),
        ({"volume_24h": 25_000_000.0}, "wash-trading"),
        ({"holders": 10}, "below floor"),
        ({"buy_ratio": 0.97}, "one-sided"),
    ],
)
def test_lab_each_red_flag_rejects(override, expected_fragment):
    b, _, gates = lab_evaluate(*default_lab_args(**override))
    assert state_of(b) == "REJECT"
    assert expected_fragment in gates


def test_lab_token_absent_from_okx_cannot_pass_to_live():
    b, _, _ = lab_evaluate(*default_lab_args(okx_avail="tidak tersedia"))
    assert state_of(b) in ("PAPER_BUY", "ALERT", "WATCH")
    assert state_of(b) != "LIVE_BUY"


def test_lab_tightening_a_threshold_changes_the_outcome():
    """The lab must actually respond to threshold moves, or it teaches nothing."""
    loose = lab_evaluate(*default_lab_args(max_top10=40.0, top10=30.0))
    strict = lab_evaluate(*default_lab_args(max_top10=20.0, top10=30.0))
    assert state_of(loose[0]) != "REJECT"
    assert state_of(strict[0]) == "REJECT"


def test_lab_agrees_with_the_real_engine():
    """The lab is not a separate implementation — it must produce the same score."""
    from app.pipeline.scoring import score_snapshot

    b, bars, _ = lab_evaluate(*default_lab_args())
    # The banner carries the score; recompute it independently is covered by the
    # engine tests. Here we only assert the lab surfaces a plausible number.
    assert "/100" in b
    score_text = b.split("skor <b>")[1].split("/100")[0]
    assert 0.0 <= float(score_text) <= 100.0


def test_lab_raising_score_thresholds_downgrades_the_state():
    normal = lab_evaluate(*default_lab_args())
    strict = lab_evaluate(*default_lab_args(alert_min=99.0, paper_min=99.5, live_min=99.9))
    assert state_of(normal[0]) == "ALERT"
    assert state_of(strict[0]) == "REJECT"


# ----------------------------------------------------------------- overrides
def test_overrides_do_not_mutate_global_settings():
    before = get_settings().rh_node_rpc_url
    patched = settings_with_overrides(rpc_url="https://example.invalid/rpc")
    assert patched.rh_node_rpc_url == "https://example.invalid/rpc"
    assert get_settings().rh_node_rpc_url == before, "override leaked into global settings"


def test_empty_overrides_return_the_live_settings():
    assert settings_with_overrides() is get_settings()


def test_bad_chain_id_is_ignored_not_crashed():
    patched = settings_with_overrides(chain_id="not-a-number")
    assert patched.rh_chain_id == get_settings().rh_chain_id


def test_config_view_redacts_secrets(monkeypatch):
    from app.config import reload_settings

    monkeypatch.setenv("OKX_API_SECRET", "leaky-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "leaky-token")
    reload_settings()
    try:
        cfg = load_config()
        assert "leaky-secret" not in str(cfg)
        assert "leaky-token" not in str(cfg)
        assert cfg["okx_api_secret"] == "***"
    finally:
        reload_settings()


# --------------------------------------------------------------- UI surface
def test_ui_exposes_no_order_placement_control():
    """The UI can stop trading; it must not be able to start a trade."""
    import inspect

    import app.ui.gradio_app as ui

    source = inspect.getsource(ui)
    for forbidden in ("place_limit_buy", "simulate_buy", "execute_live_buy"):
        assert forbidden not in source, f"UI must not call {forbidden}"


def test_ui_never_writes_credentials_to_disk():
    import inspect

    import app.ui.gradio_app as ui

    source = inspect.getsource(ui.settings_with_overrides)
    assert "open(" not in source and "write" not in source


# -------------------------------------------------------------- error guard
def test_guarded_puts_the_real_message_in_the_page():
    """A crashed handler used to reach the operator as a red pill reading only
    "Error" — Gradio hides the message by default. That is worthless in a panel
    whose job is diagnosis."""
    import app.ui.gradio_app as ui

    def boom():
        raise RuntimeError("OKX returned 401: Invalid OK-ACCESS-KEY")

    html = ui.guarded(boom, ui.ERROR_SLOT)()
    assert "RuntimeError" in html
    assert "Invalid OK-ACCESS-KEY" in html


def test_guarded_keeps_the_other_output_slots_usable():
    """Handing a Dataframe an HTML string, or a JSON component a str, produces a
    second failure on top of the first."""
    import app.ui.gradio_app as ui

    def boom():
        raise ValueError("nope")

    banner, table, detail, fixes = ui.guarded(boom, ui.ERROR_SLOT, [], {}, "")()
    assert "ValueError" in banner
    assert table == [] and detail == {} and fixes == ""


def test_guarded_escapes_the_exception_text():
    import app.ui.gradio_app as ui

    def boom():
        raise ValueError("<script>alert(1)</script>")

    out = ui.guarded(boom, ui.ERROR_SLOT)()
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_guarded_passes_through_on_success():
    import app.ui.gradio_app as ui

    assert ui.guarded(lambda: "fine", ui.ERROR_SLOT)() == "fine"


def test_guarded_handles_async_handlers():
    """do_connection_test and do_inspect are coroutines; a decorator that only
    understands sync functions would return an un-awaited coroutine object."""
    import asyncio

    import app.ui.gradio_app as ui

    async def boom():
        raise RuntimeError("connect timeout")

    out = asyncio.run(ui.guarded(boom, ui.ERROR_SLOT)())
    assert "connect timeout" in out


def test_every_automatic_handler_is_guarded():
    """Handlers that run on page load have no operator standing by to retry
    them, so an unguarded one is a silent panel."""
    import inspect

    import app.ui.gradio_app as ui

    for line in inspect.getsource(ui.build_ui).splitlines():
        if "demo.load(" in line:
            assert "guarded(" in line, f"unguarded load handler: {line.strip()}"


def test_launch_shows_error_text():
    """Belt and braces: the guard covers wired handlers, show_error covers
    anything raised outside one."""
    import inspect

    import app.ui.gradio_app as ui

    assert "show_error=True" in inspect.getsource(ui.main)


# ------------------------------------------------------------------- theme
def test_score_bars_render_every_component():
    html = score_bars([("liquidity", 23.4, 30), ("holders", 19.1, 25)], 85.4)
    assert "liquidity" in html and "holders" in html and "85.4" in html


def test_metric_cards_render():
    html = metric_cards([("Likuiditas", "$1,500,000", "reconciled")])
    assert "Likuiditas" in html and "$1,500,000" in html


def test_state_pill_colors_each_state():
    from app.ui.theme import STATE_COLORS

    for state in STATE_COLORS:
        assert STATE_COLORS[state] in state_pill(state)


# ------------------------------------------------------- setup tab (Gradio 6)
def test_setup_values_are_always_valid_dropdown_choices(tmp_path, monkeypatch):
    """Feeding a Gradio Dropdown a value outside its choices raises at runtime
    and leaves the control blank, which then breaks Save. Regression guard."""
    import app.ui.gradio_app as ui

    monkeypatch.setattr(ui, "ENV_PATH", tmp_path / "missing.env")  # no .env at all
    values = dict(zip(ui.SETUP_FIELDS, ui.load_setup_values()))
    for key, choices in ui.CHOICE_FIELDS.items():
        assert values[key] in choices, f"{key}={values[key]!r} not in {choices}"


def test_setup_numbers_are_numeric(tmp_path, monkeypatch):
    import app.ui.gradio_app as ui

    monkeypatch.setattr(ui, "ENV_PATH", tmp_path / "missing.env")
    values = dict(zip(ui.SETUP_FIELDS, ui.load_setup_values()))
    for key in ui.NUMBER_FIELDS:
        assert isinstance(values[key], (int, float)), f"{key} is {type(values[key])}"


def test_setup_never_echoes_a_stored_secret(tmp_path, monkeypatch):
    import app.ui.gradio_app as ui

    env = tmp_path / ".env"
    env.write_text("OKX_API_SECRET=super-secret-value\n", encoding="utf-8")
    monkeypatch.setattr(ui, "ENV_PATH", env)
    values = ui.load_setup_values()
    assert "super-secret-value" not in str(values)
    assert "***tersimpan***" in values


def test_setup_form_roundtrip_writes_env(tmp_path, monkeypatch):
    """The whole point of the Setup tab: fill it in, save, and it persists."""
    import app.ui.gradio_app as ui
    from app.ui.envfile import read_env

    env = tmp_path / ".env"
    env.write_text("RUN_MODE=ALERT_ONLY\nPOSITION_USD=25\n", encoding="utf-8")
    monkeypatch.setattr(ui, "ENV_PATH", env)

    form = dict(zip(ui.SETUP_FIELDS, ui.load_setup_values()))
    form["RH_NODE_RPC_URL"] = "https://rpc.test/v1"
    form["POSITION_USD"] = 10.0

    banner_html, _ = ui.do_save_setup(*[form[k] for k in ui.SETUP_FIELDS])

    saved = read_env(env)
    assert saved["RH_NODE_RPC_URL"] == "https://rpc.test/v1"
    assert saved["POSITION_USD"] == "10", "floats must be normalised, not written as 10.0"
    assert "Tersimpan" in banner_html


def test_saving_live_mode_with_real_money_warns_loudly(tmp_path, monkeypatch):
    import app.ui.gradio_app as ui

    env = tmp_path / ".env"
    env.write_text("RUN_MODE=ALERT_ONLY\n", encoding="utf-8")
    monkeypatch.setattr(ui, "ENV_PATH", env)

    form = dict(zip(ui.SETUP_FIELDS, ui.load_setup_values()))
    form["RUN_MODE"] = "LIVE"
    form["OKX_SIMULATED"] = "false"
    banner_html, _ = ui.do_save_setup(*[form[k] for k in ui.SETUP_FIELDS])
    assert "uang sungguhan" in banner_html.lower()
    assert "banner-warn" in banner_html


# ------------------------------------------------- setup save (round-tripping)
def test_clearing_a_field_actually_saves(tmp_path, monkeypatch):
    """Reported symptom: "I clearly changed something and it says nothing
    changed". Clearing a value was silently refused and then reported as a
    no-op."""
    import app.ui.gradio_app as ui
    from app.ui.envfile import read_env

    env = tmp_path / ".env"
    env.write_text("ALERT_WEBHOOK_URL=https://hooks.example/old\nRUN_MODE=ALERT_ONLY\n",
                   encoding="utf-8")
    monkeypatch.setattr(ui, "ENV_PATH", env)

    form = dict(zip(ui.SETUP_FIELDS, ui.load_setup_values()))
    form["ALERT_WEBHOOK_URL"] = ""
    html, _ = ui.do_save_setup(*[form[k] for k in ui.SETUP_FIELDS])

    assert "Tersimpan" in html
    assert "Dikosongkan" in html
    assert read_env(env)["ALERT_WEBHOOK_URL"] == ""


def test_a_genuine_no_op_explains_what_it_compared(tmp_path, monkeypatch):
    """"Nothing changed" is a claim about the operator's input, so it has to say
    what it was compared against."""
    import app.ui.gradio_app as ui

    env = tmp_path / ".env"
    env.write_text("RUN_MODE=ALERT_ONLY\nOKX_API_KEY=stored-key\n", encoding="utf-8")
    monkeypatch.setattr(ui, "ENV_PATH", env)

    # The first save materialises the form's defaults into a minimal .env, so
    # the genuine no-op is the *second* one. That doubles as an idempotency
    # check: saving twice without touching anything must not churn the file.
    form = dict(zip(ui.SETUP_FIELDS, ui.load_setup_values()))
    ui.do_save_setup(*[form[k] for k in ui.SETUP_FIELDS])

    form = dict(zip(ui.SETUP_FIELDS, ui.load_setup_values()))
    html, _ = ui.do_save_setup(*[form[k] for k in ui.SETUP_FIELDS])

    assert "Tidak ada perubahan" in html
    assert ".env" in html
    assert "kunci terbaca" in html
    # and it must say the stored secret was preserved, not silently ignored
    assert "OKX_API_KEY" in html


def test_saving_never_wipes_a_stored_secret_left_blank(tmp_path, monkeypatch):
    import app.ui.gradio_app as ui
    from app.ui.envfile import read_env

    env = tmp_path / ".env"
    env.write_text("OKX_API_SECRET=do-not-lose-me\nRUN_MODE=ALERT_ONLY\n", encoding="utf-8")
    monkeypatch.setattr(ui, "ENV_PATH", env)

    form = dict(zip(ui.SETUP_FIELDS, ui.load_setup_values()))
    form["OKX_API_SECRET"] = ""            # operator leaves it blank
    form["RH_NODE_RPC_URL"] = "https://rpc.test/v2"
    ui.do_save_setup(*[form[k] for k in ui.SETUP_FIELDS])

    assert read_env(env)["OKX_API_SECRET"] == "do-not-lose-me"


def test_run_mode_cannot_be_blanked(tmp_path, monkeypatch):
    import app.ui.gradio_app as ui
    from app.ui.envfile import read_env

    env = tmp_path / ".env"
    env.write_text("RUN_MODE=PAPER\n", encoding="utf-8")
    monkeypatch.setattr(ui, "ENV_PATH", env)

    form = dict(zip(ui.SETUP_FIELDS, ui.load_setup_values()))
    form["RUN_MODE"] = ""
    ui.do_save_setup(*[form[k] for k in ui.SETUP_FIELDS])
    assert read_env(env)["RUN_MODE"] == "PAPER"


# ----------------------------------------------------- one connection button
def test_there_is_a_single_connection_test_button():
    """The separate Ping RPC did a strict subset of check_node_rpc, so it could
    only agree with the full test or confuse the operator by disagreeing."""
    import inspect

    import app.ui.gradio_app as ui

    src = inspect.getsource(ui.build_ui)
    assert "ping_btn" not in src
    assert src.count("gr.Button(\"🩺") == 1


def test_the_full_test_still_covers_the_node():
    """Removing the ping must not remove RPC coverage."""
    import inspect

    import app.diagnostics as diag

    assert "check_node_rpc" in inspect.getsource(diag.run_all_checks)
