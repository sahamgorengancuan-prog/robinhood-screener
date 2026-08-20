"""Linux-environment guards.

The Linux launchers have their own failure modes, and every one of them is
silent: a CR in a shebang, a lost executable bit, a `pip install` that escapes
the virtualenv, a secret swept into the distributable zip. None of those show up
as an exception during development — they show up on the user's machine.

Each test here pins one of those down.
"""

from __future__ import annotations

import importlib.util
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL_SCRIPTS = sorted(ROOT.glob("*.sh"))

# Exactly three, on purpose:
#   install.sh          builds everything, verifies it, and stops
#   start.sh            the only file needed afterwards
#   emergency-stop.sh   must work when Python itself is broken
LAUNCHERS = ["install.sh", "start.sh", "emergency-stop.sh"]

BASH_SCRIPTS = ["install.sh", "start.sh"]


def _load_packager():
    path = ROOT / "scripts" / "make_linux_package.py"
    spec = importlib.util.spec_from_file_location("make_linux_package", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> Path:
    """The real distributable, built the way a release builds it."""
    return _load_packager().build(tmp_path_factory.mktemp("dist"))


# ------------------------------------------------------------------ presence
def test_every_launcher_exists():
    missing = [n for n in LAUNCHERS if not (ROOT / n).exists()]
    assert missing == [], f"missing launchers: {missing}"


def test_there_are_only_three_entry_points():
    found = sorted(p.name for p in SHELL_SCRIPTS)
    assert found == sorted(LAUNCHERS), (
        f"unexpected .sh files: {set(found) - set(LAUNCHERS)}. "
        "Everything else belongs in the control panel."
    )


def test_indonesian_quickstart_ships_at_the_root():
    """The operator reads this before anything else; it must not be buried."""
    text = (ROOT / "INSTALL-LINUX.txt").read_text(encoding="utf-8")
    assert "bash install.sh" in text
    assert "./start.sh" in text
    assert "./emergency-stop.sh" in text


# -------------------------------------------------------------- line endings
@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_have_no_carriage_returns(script: Path):
    """A CRLF shebang fails as `bad interpreter: /bin/bash^M`.

    This is the single most common way a shell script breaks after a round trip
    through Windows, and the error message names the wrong thing.
    """
    raw = script.read_bytes()
    assert b"\r" not in raw, f"{script.name} contains CR; must be LF-only"


def test_gitattributes_pins_sh_to_lf():
    """Otherwise a clone with core.autocrlf=true checks out broken launchers."""
    assert "*.sh   text eol=lf" in (ROOT / ".gitattributes").read_text()


# ------------------------------------------------------------- shell syntax
@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shebang_is_present_and_first(script: Path):
    first = script.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("#!"), f"{script.name} has no shebang"
    assert "sh" in first


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script: Path):
    """`sh -n` / `bash -n` catch the paren and quoting mistakes that only bite
    on a branch the developer never took."""
    shell = "bash" if script.name in BASH_SCRIPTS else "sh"
    proc = subprocess.run([shell, "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, f"{script.name}: {proc.stderr.strip()}"


@pytest.mark.parametrize("name", BASH_SCRIPTS)
def test_bash_scripts_fail_fast(name: str):
    """Without `set -e` an installer happily continues past a failed step and
    reports success on a broken environment."""
    assert "set -euo pipefail" in (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_paths_are_anchored_to_the_script_directory(script: Path):
    """A launcher can be invoked from anywhere, including by systemd."""
    text = script.read_text(encoding="utf-8")
    assert 'dirname "${BASH_SOURCE[0]}"' in text or 'dirname "$0"' in text, \
        f"{script.name} assumes the caller's working directory"


# -------------------------------------------------------------- install.sh
def test_installer_floor_matches_pyproject():
    """Two places state the minimum Python. They must not drift apart."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requires = next(
        line for line in pyproject.splitlines() if line.strip().startswith("requires-python")
    )
    declared = requires.split(">=")[1].strip().strip('"').strip("'")
    major, minor = declared.split(".")[:2]

    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert f"MIN_PY_MAJOR={major}" in install
    assert f"MIN_PY_MINOR={minor}" in install


def test_installer_verifies_itself_by_running_the_suite():
    """An install that is not verified is a claim, not a result."""
    assert "-m pytest" in (ROOT / "install.sh").read_text(encoding="utf-8")


def test_installer_never_installs_into_the_system_python():
    """A bare `pip install` on Ubuntu 23.04+ hits externally-managed-environment,
    and on older releases silently pollutes the system site-packages."""
    for line in (ROOT / "install.sh").read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0]
        if "pip install" in code:
            assert "$VENV_PY" in code, f"pip escapes the venv: {line.strip()}"


def test_installer_prefers_a_stable_python_on_jammy():
    """Measured against the real jammy archive: Ubuntu 22.04 offers python3.11
    only as 3.11.0~rc1, a release candidate frozen before years of bugfixes.
    uv installs a proper release, so on 22.04 it must be tried first."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    marker = '= "ubuntu:22.04" ]; then'
    assert marker in text, "the jammy special case was removed"
    first_route = text.split(marker, 1)[1].splitlines()[1].strip()
    assert first_route.startswith("provision_with_uv"), f"jammy leads with {first_route}"


def test_installer_treats_prereleases_as_a_last_resort():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "releaselevel" in text, "no prerelease detection"
    # Stable pass first, prerelease pass only as a fallback.
    assert "find_python no || find_python yes" in text
    assert "PRE-RELEASE" in text, "a prerelease must be reported, not used quietly"


def test_installer_probes_apt_before_installing():
    """Blindly apt-installing python3.12 on 22.04 printed four `E: Unable to
    locate package` lines before silently recovering. It looked broken."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "apt_candidate()" in text
    assert "apt-cache policy" in text


def test_no_pipefail_sigpipe_traps_in_the_launchers():
    """`cmd | grep -q` under `set -o pipefail` reports 141 when grep matches
    early and cmd dies of SIGPIPE — so the test fails exactly when it should
    have succeeded. This silently discarded the python3.11 that a real Ubuntu
    22.04 host was offering."""
    for name in BASH_SCRIPTS:
        text = (ROOT / name).read_text(encoding="utf-8")
        if "set -euo pipefail" not in text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            assert not ("|" in code and "grep -q" in code), \
                f"{name}:{i} pipes into `grep -q` under pipefail: {line.strip()}"


def test_installer_restores_the_executable_bit():
    """Browser downloads and Windows re-zips drop the mode bits."""
    assert "chmod +x ./*.sh" in (ROOT / "install.sh").read_text(encoding="utf-8")


def test_installer_does_not_overwrite_an_existing_env():
    """.env holds live API credentials. Clobbering it is unrecoverable."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "if [ -f .env ]" in text
    assert "cp .env.example .env" in text


def _shell_function(text: str, name: str) -> str:
    """Body of a `name() { ... }` block, delimited by a lone closing brace.

    Splitting on the first `}` does not work: `${DIM}` and friends appear long
    before the function ends.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start + 1:end])


@pytest.mark.parametrize(
    "func", ["provision_with_archive", "provision_with_deadsnakes", "provision_with_uv"]
)
def test_installer_asks_before_changing_the_system(func: str):
    """apt and the uv download are the only steps that touch anything outside
    the project folder, so both must be gated on an explicit answer."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "confirm()" in text
    # Fails closed: no terminal to ask means no system change.
    assert "[ -t 0 ] || return 1" in text
    assert "confirm " in _shell_function(text, func), f"{func} changes the system unasked"


# ---------------------------------------------------------------- start.sh
def test_start_launches_the_ui():
    text = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "app.ui.gradio_app" in text
    assert "exec " in text, "systemd and Ctrl+C must reach the interpreter directly"


def test_start_bootstraps_a_fresh_copy():
    """One command on an unpacked zip, not two, when the user skips the readme."""
    text = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "install.sh" in text


def test_start_checks_the_port_without_depending_on_ss():
    """`ss` is missing from minimal images, and an absent tool made the guard
    skip silently — the user then got a raw Gradio traceback instead."""
    text = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "already in use" in text
    assert "sock.bind" in text
    assert "ss -ltn" not in text


def test_start_warns_when_binding_off_loopback():
    """The panel has no auth and can release the kill switch."""
    text = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert 'HOST" != "127.0.0.1"' in text
    assert "NO authentication" in text


def test_no_launcher_can_place_an_order():
    for name in LAUNCHERS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for forbidden in ("place_limit_buy", "execute_live_buy", "run_mode=LIVE", "RUN_MODE=LIVE"):
            assert forbidden not in text, f"{name} must not be able to start trading"


# --------------------------------------------------------- emergency-stop.sh
def test_emergency_stop_needs_no_python():
    """It must work when the venv is gone — that is when you need it most."""
    text = (ROOT / "emergency-stop.sh").read_text(encoding="utf-8").lower()
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    assert "python" not in code
    assert ".venv" not in code
    assert "KILL_SWITCH".lower() in code


def test_emergency_stop_is_posix_sh():
    """bash is not guaranteed on a minimal image; /bin/sh is."""
    first = (ROOT / "emergency-stop.sh").read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/bin/sh"


def test_emergency_stop_writes_the_file_the_killswitch_reads():
    from app.config import Settings

    default_path = Settings(_env_file=None).kill_switch_file
    assert Path(default_path).name == "KILL_SWITCH"
    assert "> KILL_SWITCH" in (ROOT / "emergency-stop.sh").read_text(encoding="utf-8")


# ------------------------------------------------------------------ package
def test_package_is_built_under_one_top_level_directory(package: Path):
    """Unzipping must never scatter files into the user's current directory."""
    with zipfile.ZipFile(package) as zf:
        roots = {name.split("/", 1)[0] for name in zf.namelist()}
    assert roots == {"robinhood-screener"}


def test_package_keeps_shell_scripts_executable(package: Path):
    """zipfile drops POSIX modes unless external_attr is set; without this the
    user gets 'Permission denied' the moment they follow the instructions."""
    with zipfile.ZipFile(package) as zf:
        for name in LAUNCHERS:
            info = zf.getinfo(f"robinhood-screener/{name}")
            mode = info.external_attr >> 16
            assert mode & stat.S_IXUSR, f"{name} is not executable in the zip ({mode:o})"


def test_package_carries_no_secrets_or_local_state(package: Path):
    """The zip is the thing that gets shared. Nothing private may ride along."""
    with zipfile.ZipFile(package) as zf:
        names = [n.split("/", 1)[1] for n in zf.namelist()]

    for name in names:
        assert name != ".env", "the live .env was packaged"
        assert not name.startswith(".venv/"), "the local virtualenv was packaged"
        assert not name.startswith("data/"), "the local database was packaged"
        assert not name.startswith("dist/"), "the package packaged itself"
        assert "__pycache__" not in name
        assert name != "KILL_SWITCH"
        assert not name.endswith((".db", ".pem", ".key"))


def test_package_excludes_secrets_even_without_git(tmp_path, monkeypatch):
    """Regression: the no-git fallback once relied on .gitignore for safety and
    swept a live .env — real API credentials — into a shareable zip."""
    mod = _load_packager()
    monkeypatch.setattr(mod, "git", lambda *a: (_ for _ in ()).throw(FileNotFoundError()))

    (ROOT / ".env").exists() or (ROOT / ".env").write_text("OKX_API_SECRET=x\n")
    out = mod.build(tmp_path / "nogit")
    with zipfile.ZipFile(out) as zf:
        names = {n.split("/", 1)[1] for n in zf.namelist()}

    assert ".env" not in names
    assert ".env.example" in names, "the template must still ship"
    assert not any(n.startswith((".venv/", "data/", ".git/")) for n in names)


def test_package_contains_what_the_installer_needs(package: Path):
    with zipfile.ZipFile(package) as zf:
        names = {n.split("/", 1)[1] for n in zf.namelist()}

    for required in (
        "install.sh", "start.sh", "emergency-stop.sh", "INSTALL-LINUX.txt",
        "requirements.txt", "pyproject.toml", ".env.example",
        "scripts/init_db.py", "app/ui/gradio_app.py", "docs/LINUX.md",
    ):
        assert required in names, f"{required} missing from the package"

    # The test suite ships too: install.sh runs it to verify the install.
    assert any(n.startswith("tests/") for n in names)


def test_shipped_package_is_not_stale():
    """The committed zip is what people actually download. If a launcher was
    edited and the package never rebuilt, the download is a different program
    from the repository — silently, and only on the user's machine."""
    shipped = sorted((ROOT / "dist").glob("robinhood-screener-linux-*.zip"))
    if not shipped:
        pytest.skip("no package built yet — run `make package`")

    with zipfile.ZipFile(shipped[-1]) as zf:
        for name in (*LAUNCHERS, "requirements.txt", "INSTALL-LINUX.txt"):
            packaged = zf.read(f"robinhood-screener/{name}")
            assert packaged == (ROOT / name).read_bytes(), (
                f"{name} in {shipped[-1].name} differs from the working tree — "
                "rebuild with `make package`"
            )


def test_package_build_is_deterministic(tmp_path):
    """Same commit, same bytes — otherwise the published sha256 proves nothing."""
    mod = _load_packager()
    a = mod.build(tmp_path / "a")
    b = mod.build(tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()


def test_packager_refuses_to_ship_crlf_shell_scripts(monkeypatch, tmp_path):
    mod = _load_packager()
    bad = tmp_path / "broken.sh"
    bad.write_bytes(b"#!/usr/bin/env bash\r\necho hi\r\n")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="bad interpreter"):
        mod.check_line_endings(["broken.sh"])
