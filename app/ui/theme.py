"""Shared styling and small HTML builders for the Gradio UI.

Colour is used for one thing only: decision state and check status. Everything
else stays monochrome, so a red cell always means the same thing wherever it
appears.
"""

from __future__ import annotations

import gradio as gr

# System font stack, deliberately NOT gr.themes.GoogleFont.
#
# Gradio's stock themes inject a render-blocking <link> to fonts.googleapis.com.
# On a locked-down host — which is exactly where you should be running a trading
# control panel — that request hangs and the whole UI sits on "Loading…"
# forever. Local fonts also mean the panel makes no outbound requests at all,
# so running it does not announce itself to anyone.
FONT = ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "Helvetica", "sans-serif"]
FONT_MONO = ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "Liberation Mono", "monospace"]


def theme() -> gr.themes.Base:
    return gr.themes.Soft(font=FONT, font_mono=FONT_MONO)


STATE_COLORS = {
    "LIVE_BUY": "#a3e635",
    "PAPER_BUY": "#38bdf8",
    "ALERT": "#fbbf24",
    "WATCH": "#94a3b8",
    "REJECT": "#f87171",
    "ERROR": "#f87171",
}

STATUS_COLORS = {"OK": "#22c55e", "WARN": "#f59e0b", "FAIL": "#ef4444", "SKIP": "#6b7280"}

CSS = """
.gradio-container { max-width: 1500px !important; }
/* Banners are tinted rather than solid so they read in both Gradio themes.
   `color: inherit` on bold is required: Gradio's stylesheet forces <b> to the
   body text colour, which is unreadable against a coloured callout. */
#banner { border-radius: 10px; padding: 14px 18px; margin-bottom: 8px; font-size: 15px; }
#banner b, #banner strong { color: inherit; font-weight: 700; }
.banner-ok   { background: rgba(34,197,94,.13);  color: #14663a; border-left: 5px solid #22c55e; }
.banner-warn { background: rgba(245,158,11,.14); color: #7c4a03; border-left: 5px solid #f59e0b; }
.banner-fail { background: rgba(239,68,68,.13);  color: #86181d; border-left: 5px solid #ef4444; }
.dark .banner-ok   { color: #86efac; }
.dark .banner-warn { color: #fcd34d; }
.dark .banner-fail { color: #fca5a5; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.metric-card { background: rgba(127,127,127,.09); border-radius: 10px; padding: 12px 14px; }
.metric-card .label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; opacity: .65; }
.metric-card .value { font-size: 21px; font-weight: 700; margin-top: 3px; font-variant-numeric: tabular-nums; }
.metric-card .sub { font-size: 11px; opacity: .55; margin-top: 2px; }
.scorebar { margin: 7px 0; }
.scorebar .row { display: flex; align-items: center; gap: 10px; font-size: 13px; }
.scorebar .name { width: 110px; opacity: .8; }
.scorebar .track { flex: 1; height: 15px; background: rgba(127,127,127,.16); border-radius: 8px; overflow: hidden; }
.scorebar .fill { height: 100%; border-radius: 8px; transition: width .35s ease; }
.scorebar .num { width: 84px; text-align: right; font-variant-numeric: tabular-nums; opacity: .85; }
.gatelist { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12.5px; line-height: 1.75; }
.gatelist b { color: inherit; }
.gatelist .hard { color: #dc2626; } .gatelist .live { color: #b45309; }
.gatelist .data { color: #64748b; } .gatelist .pass { color: #15803d; }
.dark .gatelist .hard { color: #f87171; } .dark .gatelist .live { color: #fbbf24; }
.dark .gatelist .data { color: #94a3b8; } .dark .gatelist .pass { color: #4ade80; }
.metric-card .value, .scorebar .num, .scorebar .name { color: inherit; }
/* elem_classes lands on the <button> itself, so both selectors are needed to
   survive Gradio DOM changes. */
.big-kill, .big-kill button {
  font-size: 17px !important; font-weight: 800 !important; padding: 16px !important;
  background: #dc2626 !important; color: #fff !important; border-color: #b91c1c !important;
}
#lab-result { position: sticky; top: 0; z-index: 5; padding-top: 6px;
              background: var(--body-background-fill); }
.tight .form { gap: 6px !important; }
footer { display: none !important; }
"""


def banner(kind: str, text: str) -> str:
    cls = {"OK": "banner-ok", "WARN": "banner-warn", "FAIL": "banner-fail"}.get(kind, "banner-warn")
    return f'<div id="banner" class="{cls}">{text}</div>'


def metric_cards(items: list[tuple[str, str, str]]) -> str:
    """items = [(label, value, sub)]"""
    cards = "".join(
        f'<div class="metric-card"><div class="label">{lbl}</div>'
        f'<div class="value">{val}</div><div class="sub">{sub}</div></div>'
        for lbl, val, sub in items
    )
    return f'<div class="metric-grid">{cards}</div>'


def score_bars(components: list[tuple[str, float, float]], total: float) -> str:
    """components = [(name, points, weight)]"""
    rows = []
    for name, points, weight in components:
        pct = (points / weight * 100) if weight else 0
        color = "#22c55e" if pct >= 75 else "#f59e0b" if pct >= 45 else "#ef4444"
        rows.append(
            f'<div class="row"><div class="name">{name}</div>'
            f'<div class="track"><div class="fill" style="width:{pct:.1f}%;background:{color}"></div></div>'
            f'<div class="num">{points:.1f} / {weight:.0f}</div></div>'
        )
    tcolor = "#22c55e" if total >= 78 else "#38bdf8" if total >= 72 else "#f59e0b" if total >= 60 else "#ef4444"
    rows.append(
        f'<div class="row" style="margin-top:8px;font-weight:700">'
        f'<div class="name">TOTAL</div>'
        f'<div class="track"><div class="fill" style="width:{total:.1f}%;background:{tcolor}"></div></div>'
        f'<div class="num">{total:.1f} / 100</div></div>'
    )
    return f'<div class="scorebar">{"".join(rows)}</div>'


def state_pill(state: str) -> str:
    color = STATE_COLORS.get(state, "#94a3b8")
    return (
        f'<span style="background:{color};color:#0b0e13;padding:4px 14px;border-radius:999px;'
        f'font-weight:800;font-size:13px;letter-spacing:.03em">{state}</span>'
    )
