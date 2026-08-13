# Windows Runbook

**One file. Double-click `START.bat`.** Everything else happens in the browser.

Verified on **Python 3.14.7** (the recommended version) and 3.11.

---

## What START.bat does

```
[1/4] find Python, create a private virtual environment   (first run only)
[2/4] install dependencies                                 (first run only)
[3/4] create .env with safe defaults if missing
[4/4] open the control panel at http://127.0.0.1:7860
```

After that you never touch a terminal. Setup, connection tests, running the
pipeline, tuning thresholds, and the kill switch are all in the panel.

Keep the console window open — closing it stops the panel.

There is exactly one other `.bat`: **`EMERGENCY_STOP.bat`**, kept separate on
purpose because it must work when Python itself is broken. See below.

---

## First run

### 1. Install Python

Get **Python 3.14** from [python.org](https://www.python.org/downloads/).

**Tick "Add python.exe to PATH"** in the installer. That checkbox is the single
most common cause of "nothing happens when I double-click".

### 2. Put the folder somewhere simple

`C:\screener` is ideal. Avoid:

- **OneDrive / Google Drive folders** — file locking breaks virtualenv creation
- paths with accented or non-Latin characters
- very deep paths

### 3. Double-click `START.bat`

First run downloads a few hundred MB and takes several minutes. Later runs skip
straight to opening the browser.

### 4. Work through the panel, left to right

| Tab | What you do there |
|---|---|
| **🚀 Setup** | Paste your RPC URL, click **Deteksi** for the chain ID, click **Simpan**. Optionally add OKX keys. Everything is written to `.env`. |
| **🩺 Koneksi & API Test** | Click **Test Semua Koneksi**. Until this is green, treat every number as unreliable. It tells you exactly what to fix. |
| **📊 Screener** | **Jalankan 1 siklus** for a single pass, or **Mulai otomatis** to keep screening every few minutes. |
| **🔬 Token Inspector** | Paste a contract to evaluate it read-only, with the full gate breakdown. |
| **🎛️ Threshold Lab** | Drag sliders, watch the verdict change instantly. Use it to calibrate before saving thresholds. |
| **🛡️ Risiko & Order** | Kill switch, live exposure counters, order ledger. |
| **⚙️ Konfigurasi** | Effective settings, secrets redacted. |

Only the RPC URL is genuinely required. For which keys to obtain and what
each unlocks, see [`API_SETUP.md`](API_SETUP.md). Everything else degrades safely: an
unreachable source makes its metrics *unavailable*, which routes tokens to
WATCH — never to a buy.

---

## Emergency stop

**Double-click `EMERGENCY_STOP.bat`.** Make a desktop shortcut now, before you
need it.

It writes a `KILL_SWITCH` sentinel file and uses **no Python, no virtualenv and
no running service**, so it works when everything else is broken — which is
exactly when you'll reach for it. That is why it is the one thing not folded
into the panel.

Screening and alerting continue; only order execution stops. A running panel
picks it up on its next check, no restart needed.

To resume: delete the `KILL_SWITCH` file, or use the panel's **Risiko & Order**
tab. Note there are **three independent triggers** — the sentinel file,
`KILL_SWITCH=true` in `.env`, and the panel's button. Clearing one does not
clear the others; the panel shows the live state.

---

## Windows-specific behaviour

**Console output.** `START.bat` switches the code page to UTF-8. Status icons
use emoji in Windows Terminal and VS Code, and ASCII markers (`[+] [!] [x] [-]`)
in plain `cmd.exe`, because the classic console renders emoji as boxes even at
code page 65001. Force either with `SCREENER_UNICODE=1` or `SCREENER_ASCII=1`.

**Line endings.** The `.bat` files are CRLF and pinned that way in
`.gitattributes`; `cmd.exe` mis-parses `goto` labels in LF-only batch files.
Don't let an editor convert them.

**Antivirus.** Some scanners block new `.venv` directories or `pip`. If the
install step fails, check that first.

**The panel is unauthenticated.** It binds to `127.0.0.1` and can engage or
release the kill switch. Never set `GRADIO_SHARE=true` — that publishes a
world-reachable tunnel to your control panel.

---

## Headless / scheduled runs

The panel is the normal path, but a scriptable runner remains for Task
Scheduler:

```bat
.venv\Scripts\python.exe scripts\one_click.py
.venv\Scripts\python.exe scripts\one_click.py --token 0xYourContract
.venv\Scripts\python.exe scripts\one_click.py --skip-checks
```

It refuses to run unattended when `RUN_MODE=LIVE` and `OKX_SIMULATED=false`,
demanding a typed `LIVE` confirmation.

---

## When something goes wrong

| Symptom | Cause and fix |
|---|---|
| Window flashes and closes | `START.bat` ends in `pause`, so this means the file itself failed to start — usually LF line endings after an editor rewrote it. Re-clone. |
| "Python was not found" | Not on PATH. Reinstall with the PATH checkbox ticked. |
| "Could not create the virtual environment" | Folder is in OneDrive, or antivirus blocked it. Move to `C:\screener`. |
| Dependency install fails | Proxy or no internet. Run `.venv\Scripts\python.exe -m pip install -r requirements.txt` by hand to see the real error. |
| Panel opens but says .env could not be read | Delete `.env` and restart — it regenerates with safe defaults. Your previous file is kept as `.env.bak`. |
| Everything SKIP / FAIL in the connection tab | RPC URL not set. Tokens will sit in WATCH — the safe degradation, not a crash. |
| Panel stuck on "Loading…" | Hard-refresh with Ctrl+F5. |
| Browser didn't open | Paste `http://127.0.0.1:7860` in manually. |

---

## Before you ever set `RUN_MODE=LIVE`

Work through [`SECURITY_CHECKLIST.md`](SECURITY_CHECKLIST.md) first.

The Setup tab *can* switch to LIVE with real keys — that is the cost of
one-click convenience — and it warns you in the loudest terms it can when you
do. After that, the kill switch and the exposure caps are the only things
between the bot and your balance.

There is **no sell logic in this system**. It can enter a position and cannot
exit one. Exits are manual. Do not leave it running unattended with money you
would miss.
