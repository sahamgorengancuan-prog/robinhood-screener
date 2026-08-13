"""Read and write `.env` from the control panel.

Writing configuration from a browser form is a deliberate trade-off. It is what
makes one-click setup possible, and it is also the only place in this project
where the UI can change behaviour. Three rules keep it safe:

1. **Comments and ordering survive.** `.env.example` is heavily annotated and
   those annotations are the documentation for every risk threshold. A naive
   `dump(dict)` would delete them, so this rewrites values line by line and only
   appends genuinely new keys.
2. **Blank means "leave alone", not "erase".** An empty box in the form never
   wipes an existing secret — otherwise re-saving after a page reload would
   silently clear your API keys.
3. **Secrets are never logged or echoed back.** Values go in; only key names
   come out.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

SECRET_KEYS = {
    "OKX_API_SECRET", "OKX_API_PASSPHRASE", "OKX_API_KEY", "OKX_PROJECT_ID",
    "OKX_TRADE_API_SECRET", "OKX_TRADE_API_PASSPHRASE", "OKX_TRADE_API_KEY",
    "RH_DATA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
}

_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")


def read_env(path: str | Path) -> dict[str, str]:
    """Parse a .env into {KEY: value}. Missing file yields {}."""
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(raw)
        if m:
            out[m.group(2)] = m.group(4).strip().strip('"').strip("'")
    return out


def write_env(path: str | Path, updates: dict[str, str], *, backup: bool = True) -> list[str]:
    """Apply `updates` in place. Returns the list of key names changed.

    Keys already present are rewritten where they sit, so surrounding comments
    stay attached to the setting they describe. New keys are appended in a
    clearly marked block.
    """
    p = Path(path)
    changed: list[str] = []

    clean = {k: v for k, v in updates.items() if v is not None}

    if not p.exists():
        example = p.parent / ".env.example"
        if example.exists():
            shutil.copyfile(example, p)
        else:
            p.write_text("", encoding="utf-8")

    if backup and p.exists():
        shutil.copyfile(p, p.with_suffix(p.suffix + ".bak"))

    lines = p.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()

    for i, raw in enumerate(lines):
        m = _LINE.match(raw)
        if not m:
            continue
        indent, key, sep, old = m.groups()
        if key not in clean:
            continue
        seen.add(key)
        new = str(clean[key])
        if old.strip().strip('"').strip("'") != new:
            lines[i] = f"{indent}{key}{sep}{new}"
            changed.append(key)

    missing = [k for k in clean if k not in seen]
    if missing:
        lines.append("")
        lines.append("# ---- added by the control panel ----")
        for k in missing:
            lines.append(f"{k}={clean[k]}")
            changed.append(k)

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def merge_form(current: dict[str, str], form: dict[str, str]) -> dict[str, str]:
    """Build the update set from a form submission.

    A blank secret field means "keep what is already there". A blank non-secret
    field is treated the same way, because the form cannot distinguish "cleared
    on purpose" from "never loaded".
    """
    updates: dict[str, str] = {}
    for key, value in form.items():
        value = "" if value is None else str(value).strip()
        if not value:
            continue
        if key in SECRET_KEYS and value.startswith("***"):
            continue  # the redacted placeholder was sent back unchanged
        if current.get(key, "") != value:
            updates[key] = value
    return updates


def redact(values: dict[str, str]) -> dict[str, str]:
    """Mask secrets for display. Shows whether a value is set, never what it is."""
    return {
        k: ("***set***" if v else "") if k in SECRET_KEYS else v
        for k, v in values.items()
    }
