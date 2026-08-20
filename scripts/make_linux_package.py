#!/usr/bin/env python3
"""Build the Linux install package.

    python3 scripts/make_linux_package.py

Produces `dist/robinhood-screener-linux-<version>.zip`: everything git tracks,
laid out under a single top-level folder so `unzip` never scatters files into
the user's current directory.

Two details matter and are easy to get wrong:

* **Executable bits.** Python's ``zipfile`` drops POSIX permissions unless you
  set ``external_attr`` yourself and mark the archive as Unix-created. Without
  that, ``./install.sh`` after unzip fails with "Permission denied".
* **Line endings.** ``.sh`` files must stay LF. A CRLF shebang produces
  ``bad interpreter: /bin/bash^M``, the single most common way a shell script
  breaks after a round trip through Windows. This script refuses to package a
  ``.sh`` file containing CR.

The build is deterministic: entries are sorted and stamped with the HEAD commit
date, so the same commit always yields a byte-identical zip.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "robinhood-screener"

# Never ship these, whichever way the file list was produced.
#
# .gitignore already covers most of it, but only when git is available — and
# this script has to work from an extracted copy too. Relying on the ignore file
# alone put a live .env, with real API credentials, into a shareable zip during
# testing. The deny-list is therefore applied unconditionally, so a force-added
# secret is dropped as well.
EXCLUDE_NAMES = {".env", ".env.bak", "KILL_SWITCH"}
EXCLUDE_PREFIXES = ("dist/", "data/", ".venv/", "venv/", ".git/")
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv"}
EXCLUDE_SUFFIXES = (".zip", ".pem", ".key", ".db", ".db-wal", ".db-shm", ".pyc", ".pyo")


def excluded(name: str) -> bool:
    parts = Path(name).parts
    return (
        name in EXCLUDE_NAMES
        or name.startswith(EXCLUDE_PREFIXES)
        or name.endswith(EXCLUDE_SUFFIXES)
        or bool(EXCLUDE_PARTS.intersection(parts))
        or any(p.endswith(".egg-info") for p in parts)
    )

EXECUTABLE_SUFFIXES = (".sh",)

# 0o755 / 0o644 shifted into the high 16 bits, where zip keeps Unix mode.
MODE_EXEC = (0o100755) << 16
MODE_FILE = (0o100644) << 16


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("version"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def tracked_files() -> list[str]:
    try:
        # Tracked files plus anything new that git would track. Using `ls-files`
        # alone would silently drop a file that has been written but not yet
        # staged; `--exclude-standard` still honours .gitignore, which is what
        # keeps .env, .venv/ and data/ out of the archive.
        names = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Building from an extracted copy rather than a clone.
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()]

    return sorted(
        name for name in names if not excluded(name) and (ROOT / name).is_file()
    )


def stamp() -> tuple[int, int, int, int, int, int]:
    try:
        iso = git("log", "-1", "--format=%cI")
        dt = datetime.fromisoformat(iso).astimezone(timezone.utc)
    except Exception:
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Zip cannot store years before 1980.
    return (max(dt.year, 1980), dt.month, dt.day, dt.hour, dt.minute, dt.second)


def check_line_endings(names: list[str]) -> None:
    bad = [n for n in names if n.endswith(".sh") and b"\r" in (ROOT / n).read_bytes()]
    if bad:
        raise SystemExit(
            "refusing to package: CR found in shell scripts "
            f"{bad} — they would fail with 'bad interpreter: /bin/bash^M'"
        )


def build(out_dir: Path) -> Path:
    names = tracked_files()
    check_line_endings(names)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{PKG_NAME}-linux-{project_version()}.zip"
    when = stamp()

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in names:
            data = (ROOT / name).read_bytes()
            info = zipfile.ZipInfo(f"{PKG_NAME}/{name}", date_time=when)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix — required for the mode bits to be read back
            info.external_attr = MODE_EXEC if name.endswith(EXECUTABLE_SUFFIXES) else MODE_FILE
            zf.writestr(info, data)

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "dist"), help="output directory")
    args = ap.parse_args()

    out = build(Path(args.out))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    with zipfile.ZipFile(out) as zf:
        count = len(zf.namelist())

    print(f"built    {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    print(f"files    {count}")
    print(f"size     {out.stat().st_size / 1024:.0f} KiB")
    print(f"sha256   {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
