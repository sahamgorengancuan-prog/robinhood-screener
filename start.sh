#!/usr/bin/env bash
# =============================================================================
#
#   ROBINHOOD CHAIN TOKEN SCREENER  —  START HERE (Linux)
#
#       ./start.sh
#
#   This is the only file you need after installing. If the environment is not
#   built yet it runs install.sh for you, so a fresh copy needs one command.
#
#   Everything else — setup, connection tests, running the pipeline, thresholds,
#   the kill switch — happens in the browser at http://127.0.0.1:7860
#
#   Press Ctrl+C to stop the panel.
#
# =============================================================================

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VENV_PY=".venv/bin/python"

if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; CYAN=$'\033[36m'; R=$'\033[0m'
else
    B=""; DIM=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; R=""
fi

say() { printf '%s\n' "$*"; }
die() { printf '\n  %s[x] %s%s\n\n' "$RED" "$*" "$R" >&2; exit 1; }

# ------------------------------------------------------- bootstrap if needed
if [ ! -x "$VENV_PY" ]; then
    say ""
    printf '  %s[!]%s Not installed yet — running install.sh first.\n' "$YELLOW" "$R"
    bash ./install.sh --no-tests || die "install.sh failed; fix the error above and try again"
fi

# A venv that exists but cannot import the app is worse than no venv, because
# the failure surfaces as a stack trace minutes later. Catch it here.
if ! "$VENV_PY" -c "import gradio, app.ui.gradio_app" >/dev/null 2>&1; then
    printf '  %s[!]%s Environment is incomplete — repairing.\n' "$YELLOW" "$R"
    bash ./install.sh --no-tests || die "repair failed"
fi

[ -f .env ] || { cp .env.example .env; chmod 600 .env 2>/dev/null || true; }
mkdir -p data

# ------------------------------------------------------------------ address
HOST="${GRADIO_HOST:-127.0.0.1}"
PORT="${GRADIO_PORT:-7860}"

# Only ask for a browser when there is a desktop to open one on. On a headless
# server the webbrowser module fails silently and the log looks broken.
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    export GRADIO_INBROWSER="${GRADIO_INBROWSER:-true}"
else
    export GRADIO_INBROWSER="${GRADIO_INBROWSER:-false}"
fi

# Check the port ourselves so a second launch says what is wrong, instead of
# ending in a 30-line Gradio traceback. A bind test is used rather than `ss`,
# which is absent from minimal images and would skip the check silently.
if ! "$VENV_PY" - "$HOST" "$PORT" <<'PY' >/dev/null 2>&1
import socket, sys
sock = socket.socket()
try:
    sock.bind((sys.argv[1], int(sys.argv[2])))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
then
    die "port $PORT on $HOST is already in use — another panel is probably running.
      Stop it with Ctrl+C in its window, or pick another port:

          GRADIO_PORT=7861 ./start.sh"
fi

say ""
printf '%s==============================================================================%s\n' "$DIM" "$R"
printf '  %sROBINHOOD CHAIN TOKEN SCREENER%s\n' "$B" "$R"
printf '%s==============================================================================%s\n' "$DIM" "$R"
say ""
printf '   Panel  :  %s%shttp://%s:%s%s\n' "$B" "$CYAN" "$HOST" "$PORT" "$R"
say ""
if [ "$GRADIO_INBROWSER" = "true" ]; then
    say "   Your browser should open by itself. If not, paste that address in."
else
    say "   No desktop session detected — open that address yourself."
    say "   ${DIM}Remote server? Forward it instead of exposing it:${R}"
    say "   ${DIM}    ssh -L $PORT:127.0.0.1:$PORT user@this-host${R}"
fi
say ""
say "   Configure everything on the ${B}Setup${R} tab, then run"
say "   ${B}Koneksi & API Test${R} before trusting any number the screener prints."
say ""
say "   Press ${B}Ctrl+C${R} to stop."
printf '%s==============================================================================%s\n' "$DIM" "$R"
say ""

if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ]; then
    printf '  %s[!] %s is not loopback. This panel has NO authentication and can%s\n' "$YELLOW" "$HOST" "$R"
    printf '  %s    engage or release the kill switch. Put it behind an authenticating%s\n' "$YELLOW" "$R"
    printf '  %s    reverse proxy, or use the ssh -L tunnel above instead.%s\n\n' "$YELLOW" "$R"
fi

# exec so Ctrl+C and any service manager signal the interpreter directly
# instead of this wrapper.
exec "$VENV_PY" -m app.ui.gradio_app
