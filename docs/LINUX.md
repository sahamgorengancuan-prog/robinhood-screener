# Linux Runbook (Ubuntu 22.04 / 24.04)

**Two commands.** `bash install.sh`, then `./start.sh`. Everything else happens
in the browser at <http://127.0.0.1:7860>.

An Indonesian quick-start lives at the package root: `INSTALL-LINUX.txt`.

---

## The install package

`dist/robinhood-screener-linux-<version>.zip` is a self-contained copy of the
project. It carries no dependencies — `install.sh` fetches those from PyPI — so
it stays under a megabyte and there is nothing prebuilt to distrust.

Rebuild it from a clean checkout with:

```bash
python3 scripts/make_linux_package.py
```

The build is deterministic: files are sorted and stamped with the HEAD commit
date, so the same commit always produces a byte-identical zip and the printed
`sha256` is a real integrity check. The packager also **refuses to build** if any
`.sh` file contains a CR, because a CRLF shebang fails at runtime with the
famously confusing `bad interpreter: /bin/bash^M`.

---

## From nothing to a running panel

```bash
sudo apt update
sudo apt install -y unzip curl          # a minimal Ubuntu image has neither

unzip robinhood-screener-linux-0.1.0.zip
cd robinhood-screener

bash install.sh
./start.sh
```

`install.sh` prints six steps:

```
[1/6] Checking the platform
[2/6] Finding a Python interpreter (need >= 3.11)
[3/6] Building the private virtualenv in ./.venv
[4/6] Installing dependencies
[5/6] Preparing configuration and database
[6/6] Verifying the install          <- runs the whole test suite
```

Step 6 is the reason to trust the install. If a risk gate is not behaving as
specified, the installer fails loudly rather than handing you a screener that
looks fine.

### Options

| Flag | Effect |
|---|---|
| `-y`, `--yes` | Never prompt. Provisions a Python via apt if `sudo` works, otherwise uv. |
| `--no-tests` | Skip the verification suite. Faster, and unverified. |
| `--python PATH` | Use an interpreter you already have instead of searching. |

---

## The Python problem, and how the installer solves it

This project requires **Python ≥ 3.11** (`requires-python` in `pyproject.toml`;
`tests/test_linux.py` asserts the installer's floor never drifts from it).

| Release | Stock `python3` | Result |
|---|---|---|
| Ubuntu 24.04 | 3.12 | works immediately |
| Ubuntu 22.04 | 3.10 | **too old** — needs provisioning |

`install.sh` resolves this in a fixed order, and never installs anything
system-wide without asking first:

1. **Search** for `python3.14`, `3.13`, `3.12`, `3.11`, then `python3` — taking
   the first that is both new enough *and* able to create a virtualenv.
2. **Repair.** If an interpreter is new enough but `ensurepip`/`venv` is missing
   — the standard Ubuntu split — it names the exact package (`python3.12-venv`)
   and offers to `apt install` it.
3. **apt route.** Install `python3.12`, falling back to `python3.11`, from the
   distribution's own archive. Only if neither exists does it ask about adding
   the third-party deadsnakes PPA.
4. **uv route.** Download a standalone CPython into `~/.local` via
   [uv](https://astral.sh/uv). No root, nothing outside your home directory.
   This is the escape hatch when you have no sudo, or apt is locked down.

If all four fail it stops with the exact commands to run by hand. It never
proceeds on an interpreter that cannot support the code.

### The venv trap

`python3 -m venv` on a stock Ubuntu fails with a message that does not name the
fix. The installer checks `import ensurepip, venv` up front precisely so you get
`python3.12-venv` as an instruction instead of a traceback.

---

## Daily use

```bash
./start.sh                     # control panel
GRADIO_PORT=7861 ./start.sh    # if 7860 is taken
./emergency-stop.sh            # halt execution now
```

`start.sh` bootstraps itself: on a copy that was never installed, or one whose
venv is broken, it runs `install.sh` and continues. It also refuses to start
when the port is already bound, rather than letting Gradio fail obscurely.

Browser opening is conditional on `$DISPLAY`/`$WAYLAND_DISPLAY`. On a headless
box it prints the address instead of silently failing to launch a browser.

### Headless servers

The panel has **no authentication** and can engage or release the kill switch.
Do not bind it to a public interface. Forward it:

```bash
ssh -L 7860:127.0.0.1:7860 user@server
```

`start.sh` warns whenever `GRADIO_HOST` is not loopback.

---

## The emergency stop

```bash
./emergency-stop.sh
```

Writes the `KILL_SWITCH` sentinel that `app/execution` checks before every
order. It is POSIX `/bin/sh` with **no Python, no virtualenv, no network** — it
works when the venv is destroyed, the service is wedged, or PyPI is down, which
is exactly when it matters. `tests/test_linux.py` asserts it never gains a
Python dependency.

Screening and alerting continue; only execution stops. Undo it from the panel's
**Risiko & Order** tab, or `rm KILL_SWITCH`.

---

## Running as a service (optional)

Nothing requires this, and the default `ALERT_ONLY` mode makes it harmless, but
if you want the panel to survive a reboot:

```ini
# ~/.config/systemd/user/screener.service
[Unit]
Description=Robinhood Chain Token Screener control panel
After=network-online.target

[Service]
WorkingDirectory=%h/robinhood-screener
ExecStart=%h/robinhood-screener/start.sh
Restart=on-failure
Environment=GRADIO_HOST=127.0.0.1

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now screener
loginctl enable-linger "$USER"     # so it runs when you are not logged in
```

`start.sh` uses `exec`, so systemd signals the interpreter directly and
`systemctl --user stop screener` is clean.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Permission denied` running `./start.sh` | executable bit lost in transit | `chmod +x *.sh` |
| `bad interpreter: /bin/bash^M` | file passed through Windows, CRLF endings | `sudo apt install -y dos2unix && dos2unix *.sh` |
| `ensurepip is not available` | Ubuntu's split stdlib | `sudo apt install -y python3.12-venv` |
| `install.sh` cannot find Python | Ubuntu 22.04 stock 3.10 | let the installer provision, or `bash install.sh --python /usr/bin/python3.12` |
| pip fails behind a proxy | corporate egress | `export HTTPS_PROXY=...` then re-run |
| port 7860 in use | a panel is already running | `GRADIO_PORT=7861 ./start.sh` |
| `externally-managed-environment` from pip | you ran pip outside the venv | always use `.venv/bin/python -m pip` |

---

## What the installer touches

Inside the project folder only:

```
.venv/      the private environment
.env        created from .env.example, chmod 600, never overwritten
data/       SQLite database and alert log
KILL_SWITCH written only by the emergency stop
```

Outside it, **only** when you approve a provisioning route: apt packages
(`python3.12`, `python3.12-venv`) or `~/.local/bin/uv` plus `~/.local/share/uv`.
Removing the install is `rm -rf` on the folder.

---

## What was verified, and what was not

Verified by execution on **Ubuntu 24.04.4 LTS**: the full install from an
extracted zip, the venv build, dependency resolution, the test suite, the panel
import, the port guard, the emergency stop, and — with `PATH` restricted to
Python 3.10 to reproduce the Ubuntu 22.04 situation — the interpreter rejection
and the uv provisioning route.

Not executed: the apt/deadsnakes branch on a real 22.04 host, since no such
machine or container runtime was available here. Its logic is tested for syntax
and control flow, and it degrades to the uv route on any failure, but treat the
first 22.04 apt run as unproven.

The dependency set itself is version-pinned in `requirements.txt` and resolved
from wheels on both 3.11 and 3.12; no compiler is needed on x86_64 or aarch64.
