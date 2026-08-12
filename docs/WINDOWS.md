# Windows Runbook

Everything here is double-click. No terminal required, no `make`, no manual
`pip`.

---

## The files you actually use

| Double-click | What it does |
|---|---|
| **`setup.bat`** | Run once. Creates `.venv`, installs everything, writes `.env`, builds the database, runs the tests. |
| **`run_pipeline.bat`** | **The one-click.** Tests the APIs, runs one full screening cycle, prints the results. |
| **`start_ui.bat`** | Opens the control panel in your browser (`http://127.0.0.1:7860`). |
| **`test_connection.bat`** | Connection/API test only. Run after editing `.env`. |
| **`run_api.bat`** | Leave-it-running mode: screens every 5 minutes and alerts. |
| **`EMERGENCY_STOP.bat`** | **Stops all order execution instantly.** Needs no Python. |
| **`resume_trading.bat`** | Clears the emergency stop (asks you to type `RESUME`). |
| **`run_tests.bat`** | Runs the test suite. |

`_env.bat` is shared plumbing — don't run it directly.

---

## First run, start to finish

### 1. Install Python

Get Python 3.11+ from [python.org](https://www.python.org/downloads/).
**Tick "Add python.exe to PATH"** in the installer. That checkbox is the single
most common cause of "nothing happens when I double-click".

### 2. Put the folder somewhere simple

`C:\screener` is ideal. Avoid:

- **OneDrive / Google Drive folders** — file locking breaks virtualenv creation
- paths with accented or non-Latin characters
- very deep paths (Windows still has path-length limits)

### 3. `setup.bat`

Takes a few minutes on first run (it downloads a few hundred MB). It finishes by
running the test suite — if those fail, stop and find out why before going
further, because the tests are what enforce the risk gates.

### 4. Edit `.env`

Right-click → *Open with* → Notepad. The only value you need to start is:

```
RH_NODE_RPC_URL=https://your-rpc-endpoint
```

Leave `RH_CHAIN_ID` blank — the connection test reads it from the node and tells
you what to put there.

Leave `RUN_MODE=ALERT_ONLY`. Nothing can be traded in that mode.

### 5. `test_connection.bat`

Until this reports green, treat every number the screener prints as unreliable.
It tells you exactly what to fix for each failure.

### 6. `run_pipeline.bat`

The one-click. Five steps, all visible in the window:

```
[1/5] Checking configuration
[2/5] Preparing database
[3/5] Testing connections and APIs
[4/5] Running one screening cycle
[5/5] Results
```

Useful arguments — drag the `.bat` into a `cmd` window, or make a shortcut and
append them to the Target field:

```bat
run_pipeline.bat --token 0xYourContract   ..screen a specific contract
run_pipeline.bat --skip-checks            ..skip the API tests (faster)
run_pipeline.bat --ui                     ..open the panel when it finishes
```

### 7. `start_ui.bat`

The control panel. Six tabs: connection tests, screener results, token
inspector, threshold lab, risk/kill switch, configuration.

---

## Emergency stop

**`EMERGENCY_STOP.bat` — double-click it.**

Make a desktop shortcut now, before you need it. It writes a `KILL_SWITCH`
sentinel file and deliberately uses **no Python, no virtualenv and no running
service**, so it works when everything else is broken — which is exactly when
you'll reach for it.

Screening and alerting continue. Only order execution stops. A running service
notices on its next check; no restart needed.

To resume: `resume_trading.bat`, which makes you type `RESUME`.

Note there are **three independent kill-switch triggers** — the sentinel file,
`KILL_SWITCH=true` in `.env`, and the control panel's button. Clearing one does
not clear the others. The panel's "Risiko & Order" tab shows the live state.

---

## Windows-specific behaviour

**Console output.** The launchers switch the code page to UTF-8. Emoji status
icons are used in Windows Terminal and VS Code; plain `cmd.exe` gets ASCII
markers (`[+] [!] [x] [-]`) instead, because the classic console renders emoji
as boxes even at code page 65001. Force either mode with `SCREENER_UNICODE=1` or
`SCREENER_ASCII=1`.

**Line endings.** The `.bat` files are stored with CRLF and pinned that way in
`.gitattributes`. `cmd.exe` can mis-parse `goto` labels in LF-only batch files,
so don't let an editor "helpfully" convert them.

**Antivirus.** Some scanners flag new `.venv` directories or block `pip`. If
`setup.bat` fails at the install step, that's the first thing to check.

**Closing a window stops the service.** `run_api.bat` and `start_ui.bat` run in
the foreground. Closing the window ends them. Use `EMERGENCY_STOP.bat` to stop
*trading* without stopping the *service*.

---

## When something goes wrong

| Symptom | Cause and fix |
|---|---|
| Window flashes and closes | Every launcher ends in `pause`, so this means the `.bat` itself failed to start — usually LF line endings after an editor rewrote it. Re-clone. |
| "Python was not found" | Python isn't on PATH. Reinstall with the PATH checkbox ticked. |
| "Could not create the virtual environment" | Folder is in OneDrive, or antivirus blocked it. Move to `C:\screener`. |
| Dependency install fails | Corporate proxy or no internet. Run `.venv\Scripts\python.exe -m pip install -r requirements.txt` by hand to see the real error. |
| ".env could not be read" | The message names the offending setting. Fix it, or delete `.env` and re-run to regenerate safe defaults. |
| Everything says SKIP / FAIL | `RH_NODE_RPC_URL` isn't set. Tokens will sit in WATCH — that's the safe degradation, not a crash. |
| Panel stuck on "Loading…" | A browser extension blocking local scripts, or a stale tab. Hard-refresh with Ctrl+F5. |

---

## Before you ever set `RUN_MODE=LIVE`

Work through [`SECURITY_CHECKLIST.md`](SECURITY_CHECKLIST.md) first.

`run_pipeline.bat` will refuse to run unattended when `RUN_MODE=LIVE` and
`OKX_SIMULATED=false` — it demands you type `LIVE` into the console. That guard
exists because a double-clicked icon must never be one click away from spending
real money.

There is **no sell logic in this system**. It can enter a position and cannot
exit one. Exits are manual. Do not leave it running unattended with money you
would miss.
