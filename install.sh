#!/usr/bin/env bash
# =============================================================================
#
#   ROBINHOOD CHAIN TOKEN SCREENER  —  Linux installer (Ubuntu 22.04 / 24.04)
#
#   Run this once:
#
#       bash install.sh
#
#   It finds or provisions a suitable Python, builds a private virtualenv in
#   ./.venv, installs the pinned dependencies, creates .env with safe defaults
#   (ALERT_ONLY, no keys, no trading), builds the database, and then runs the
#   test suite so you know the install actually works before you trust it.
#
#   Nothing here touches the system Python or installs anything globally,
#   unless you explicitly approve the apt route when no modern Python exists.
#
# =============================================================================

set -euo pipefail

# Always operate from this file's directory, whatever the caller's cwd is.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------------------------- policy
# Must match `requires-python` in pyproject.toml. tests/test_linux.py asserts
# the two never drift apart.
MIN_PY_MAJOR=3
MIN_PY_MINOR=11
# Newest first. The project is verified on 3.14.7 and 3.11; anything between is
# fine. The list is bounded on purpose so a future 3.15 alpha is never picked.
PY_CANDIDATES=(python3.14 python3.13 python3.12 python3.11 python3)
# What the apt / uv routes provision when nothing suitable is installed. 3.12 is
# the newest interpreter available from the Ubuntu 24.04 archive itself.
PROVISION_VERSION=3.12

VENV_DIR=".venv"
VENV_PY="$VENV_DIR/bin/python"

ASSUME_YES=0
RUN_TESTS=1
FORCED_PYTHON=""

# ------------------------------------------------------------------- output
if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; CYAN=$'\033[36m'; R=$'\033[0m'
else
    B=""; DIM=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; R=""
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s[+]%s %s\n' "$GREEN" "$R" "$*"; }
warn() { printf '  %s[!]%s %s\n' "$YELLOW" "$R" "$*"; }
die()  { printf '\n  %s[x] %s%s\n\n' "$RED" "$*" "$R" >&2; exit 1; }
step() { printf '\n%s[%s/%s]%s %s\n' "$B" "$1" "$TOTAL_STEPS" "$R" "$2"; }
rule() { printf '%s\n' "$DIM==============================================================================$R"; }

TOTAL_STEPS=6

usage() {
    cat <<'EOF'
Usage: bash install.sh [options]

  -y, --yes           Non-interactive. Approve provisioning a Python without
                      asking (apt when sudo works, otherwise uv).
      --no-tests      Skip the post-install test suite (not recommended).
      --python PATH   Use this interpreter instead of searching for one.
  -h, --help          Show this message.

After a successful install, start the control panel with:  ./start.sh
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)    ASSUME_YES=1 ;;
        --no-tests)  RUN_TESTS=0 ;;
        --python)    shift; FORCED_PYTHON="${1:-}" ;;
        -h|--help)   usage; exit 0 ;;
        *)           usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

confirm() {
    # Returns 0 on approval. Fails closed when there is no terminal to ask.
    [ "$ASSUME_YES" = "1" ] && return 0
    [ -t 0 ] || return 1
    local reply
    printf '  %s%s%s [y/N] ' "$B" "$1" "$R"
    read -r reply || return 1
    case "$reply" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------------- banner
say ""
rule
printf '  %sROBINHOOD CHAIN TOKEN SCREENER%s  —  Linux installer\n' "$B" "$R"
rule

# =============================================================== 1. platform
step 1 "Checking the platform"

[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux. On Windows use START.bat."

DISTRO_ID=""; DISTRO_VER=""; DISTRO_NAME="unknown Linux"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-}"; DISTRO_VER="${VERSION_ID:-}"; DISTRO_NAME="${PRETTY_NAME:-$DISTRO_ID}"
fi

case "$DISTRO_ID:$DISTRO_VER" in
    ubuntu:22.04|ubuntu:24.04)
        ok "$DISTRO_NAME (tested target)" ;;
    ubuntu:*|debian:*|linuxmint:*|pop:*)
        ok "$DISTRO_NAME"
        warn "tested on Ubuntu 22.04 and 24.04; this release is close enough to proceed" ;;
    *)
        warn "$DISTRO_NAME is outside the tested set (Ubuntu 22.04 / 24.04)"
        warn "continuing — nothing below is Ubuntu-specific except the apt fallback" ;;
esac

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|aarch64) ok "architecture $ARCH" ;;
    *) warn "architecture $ARCH has no prebuilt wheels for some dependencies; pip may compile" ;;
esac

# ================================================================= 2. python
step 2 "Finding a Python interpreter (need >= $MIN_PY_MAJOR.$MIN_PY_MINOR)"

py_version_ok() {
    "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" \
        >/dev/null 2>&1
}

py_has_venv() {
    # Ubuntu splits the stdlib: `python3` alone cannot create a virtualenv
    # without python3.X-venv, and the failure message is famously unhelpful.
    "$1" -c "import ensurepip, venv" >/dev/null 2>&1
}

py_label() { "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "?"; }

PY=""
MISSING_VENV_FOR=""

find_python() {
    local cand path
    for cand in "${PY_CANDIDATES[@]}"; do
        path="$(command -v "$cand" 2>/dev/null)" || continue
        py_version_ok "$path" || continue
        if py_has_venv "$path"; then
            PY="$path"
            return 0
        fi
        # Remember the first one that is new enough but cannot make a venv, so
        # we can name the exact apt package instead of a generic error.
        [ -n "$MISSING_VENV_FOR" ] || MISSING_VENV_FOR="$path"
    done
    return 1
}

if [ -n "$FORCED_PYTHON" ]; then
    command -v "$FORCED_PYTHON" >/dev/null 2>&1 || die "--python $FORCED_PYTHON not found"
    py_version_ok "$FORCED_PYTHON" \
        || die "--python $FORCED_PYTHON is $(py_label "$FORCED_PYTHON"), need >= $MIN_PY_MAJOR.$MIN_PY_MINOR"
    py_has_venv "$FORCED_PYTHON" \
        || die "$FORCED_PYTHON cannot create virtualenvs (install its -venv package)"
    PY="$(command -v "$FORCED_PYTHON")"
    ok "using $PY ($(py_label "$PY")) as requested"
else
    find_python || true
fi

# ---------------------------------------------------- 2a. repair a broken venv
if [ -z "$PY" ] && [ -n "$MISSING_VENV_FOR" ]; then
    PKG="python$("$MISSING_VENV_FOR" -c 'import sys; print("%d.%d" % sys.version_info[:2])')-venv"
    warn "$MISSING_VENV_FOR is new enough but the venv module is missing"
    say "      Ubuntu ships that separately as: ${B}$PKG${R}"
    if command -v sudo >/dev/null 2>&1 && confirm "Install $PKG with apt now?"; then
        sudo apt-get update -qq || true
        if sudo apt-get install -y "$PKG"; then
            py_has_venv "$MISSING_VENV_FOR" && PY="$MISSING_VENV_FOR"
        fi
    fi
    [ -n "$PY" ] || warn "still unusable; falling through to provisioning"
fi

# ------------------------------------------------------- 2b. provision python
provision_with_apt() {
    command -v sudo >/dev/null 2>&1 || return 1
    say "      ${DIM}apt route: installs python$PROVISION_VERSION from the Ubuntu archive${R}"
    confirm "Install python$PROVISION_VERSION system-wide with apt (needs sudo)?" || return 1
    sudo apt-get update -qq || return 1
    # The archive first. On 24.04 python3.12 is in main; on 22.04 python3.11 and
    # python3.12 come from universe. Only if both are absent do we ask about a PPA.
    local v
    for v in "$PROVISION_VERSION" 3.11; do
        if sudo apt-get install -y "python$v" "python$v-venv"; then
            command -v "python$v" >/dev/null 2>&1 && { PY="$(command -v "python$v")"; return 0; }
        fi
    done
    warn "no suitable Python in this release's archive"
    confirm "Add the deadsnakes PPA (third-party) and retry?" || return 1
    sudo apt-get install -y software-properties-common || return 1
    sudo add-apt-repository -y ppa:deadsnakes/ppa || return 1
    sudo apt-get update -qq || return 1
    sudo apt-get install -y "python$PROVISION_VERSION" "python$PROVISION_VERSION-venv" || return 1
    PY="$(command -v "python$PROVISION_VERSION")" || return 1
    return 0
}

UV=""
provision_with_uv() {
    UV="$(command -v uv 2>/dev/null || true)"
    if [ -z "$UV" ]; then
        [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
    fi
    if [ -z "$UV" ]; then
        command -v curl >/dev/null 2>&1 || return 1
        say "      ${DIM}uv route: downloads a standalone CPython into your home directory${R}"
        confirm "Install uv from https://astral.sh/uv (no root needed)?" || return 1
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || return 1
        UV="$HOME/.local/bin/uv"
        [ -x "$UV" ] || return 1
    fi
    ok "uv found at $UV"
    "$UV" python install "$PROVISION_VERSION" || return 1
    PY="$("$UV" python find "$PROVISION_VERSION" 2>/dev/null)" || return 1
    [ -x "$PY" ] || return 1
    return 0
}

if [ -z "$PY" ]; then
    warn "no Python >= $MIN_PY_MAJOR.$MIN_PY_MINOR found"
    say ""
    say "      Ubuntu 22.04 ships Python 3.10 by default, which is below this"
    say "      project's floor. One of two routes will fix that:"
    say ""
    say "        apt  — installs python$PROVISION_VERSION system-wide (needs sudo)"
    say "        uv   — downloads a private CPython into ~/.local (no root)"
    say ""
    if command -v sudo >/dev/null 2>&1; then
        provision_with_apt || provision_with_uv || true
    else
        provision_with_uv || provision_with_apt || true
    fi
fi

if [ -z "$PY" ]; then
    die "could not find or install Python >= $MIN_PY_MAJOR.$MIN_PY_MINOR.
      Fix it manually, then re-run this script:

          sudo apt update
          sudo apt install -y python$PROVISION_VERSION python$PROVISION_VERSION-venv

      or pass an interpreter you already have:

          bash install.sh --python /path/to/python3.12"
fi

ok "using $PY (Python $(py_label "$PY"))"

# ================================================================== 3. venv
step 3 "Building the private virtualenv in ./$VENV_DIR"

if [ -x "$VENV_PY" ] && py_version_ok "$VENV_PY"; then
    ok "reusing the existing venv (Python $(py_label "$VENV_PY"))"
else
    if [ -e "$VENV_DIR" ]; then
        warn "existing ./$VENV_DIR is unusable — rebuilding it"
        rm -rf "$VENV_DIR"
    fi
    if [ -n "$UV" ] && [ -x "$UV" ]; then
        # --seed puts pip in the venv so every step below is identical to the
        # stdlib route; without it `uv venv` produces a pip-less environment.
        "$UV" venv --python "$PY" --seed "$VENV_DIR" >/dev/null \
            || die "uv could not create $VENV_DIR"
    else
        "$PY" -m venv "$VENV_DIR" \
            || die "could not create $VENV_DIR — check disk space and permissions on $(pwd)"
    fi
    [ -x "$VENV_PY" ] || die "$VENV_DIR was created but has no python binary"
    ok "created (Python $(py_label "$VENV_PY"))"
fi

# ========================================================== 4. dependencies
step 4 "Installing dependencies"

IMPORT_CHECK='import fastapi, sqlalchemy, httpx, apscheduler, pydantic_settings, gradio, pytest'

if "$VENV_PY" -c "$IMPORT_CHECK" >/dev/null 2>&1; then
    ok "already installed — nothing to download"
else
    say "      ${DIM}First run downloads a few hundred MB. Later runs skip this.${R}"
    "$VENV_PY" -m pip install --upgrade pip --quiet \
        || warn "could not upgrade pip; continuing with the bundled version"
    "$VENV_PY" -m pip install -r requirements.txt \
        || die "dependency installation failed.
      Check your connection or proxy, then re-run. To see the full error:
          $VENV_PY -m pip install -r requirements.txt"
    "$VENV_PY" -c "$IMPORT_CHECK" \
        || die "dependencies installed but do not import — the output above says why"
    ok "installed"
fi

# ================================================================ 5. config
step 5 "Preparing configuration and database"

if [ -f .env ]; then
    ok "using the existing .env (left untouched)"
else
    cp .env.example .env
    ok "created .env with safe defaults — ALERT_ONLY, no keys, no trading"
fi
chmod 600 .env 2>/dev/null || true

mkdir -p data
"$VENV_PY" scripts/init_db.py >/dev/null || die "could not build the database in ./data"
ok "database ready"

# Executable bits do not survive every download path (browser + re-zip on
# Windows, some cloud sync clients). Restore them so ./start.sh just works.
chmod +x ./*.sh 2>/dev/null || true
ok "launchers are executable"

# ================================================================= 6. verify
step 6 "Verifying the install"

if [ "$RUN_TESTS" = "1" ]; then
    if "$VENV_PY" -m pytest -q; then
        ok "test suite passed"
    else
        die "the test suite failed. Do not run the screener until this is green —
      a failing suite means a risk gate is not behaving as specified."
    fi
else
    warn "tests skipped (--no-tests); the install is unverified"
fi

"$VENV_PY" -c "import app.ui.gradio_app" >/dev/null 2>&1 \
    || die "the control panel module does not import"
ok "control panel imports cleanly"

# ================================================================== done
say ""
rule
printf '  %sINSTALL COMPLETE%s\n' "$GREEN$B" "$R"
rule
say ""
say "  Start the control panel:"
printf '      %s./start.sh%s\n' "$B$CYAN" "$R"
say ""
say "  Then open  http://127.0.0.1:7860  and use the Setup tab to enter your"
say "  API keys, and the \"Koneksi & API Test\" tab to prove they work."
say ""
say "  Emergency stop (needs no Python, works when everything else is broken):"
printf '      %s./emergency-stop.sh%s\n' "$B" "$R"
say ""
printf '  %sMode is ALERT_ONLY. This install cannot place an order.%s\n' "$DIM" "$R"
say ""
