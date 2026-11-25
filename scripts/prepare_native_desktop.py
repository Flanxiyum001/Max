"""Prepare the Windows source launcher without reinstalling on every start."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def fingerprint(project: Path) -> str:
    digest = hashlib.sha256()
    digest.update(project.joinpath("pyproject.toml").read_bytes())
    digest.update(os.fsencode(project.resolve()))
    digest.update(sys.version.encode("utf-8"))
    return digest.hexdigest()


def read_stamp(stamp: Path) -> str | None:
    try:
        return stamp.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def desktop_present() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("max_core", "PySide6"))


def write_stamp(stamp: Path, expected: str) -> None:
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=stamp.parent, prefix=".max-desktop-", delete=False, mode="w", encoding="ascii"
        ) as file:
            temp = Path(file.name)
            file.write(expected + "\n")
        temp.replace(stamp)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def prepare(project: Path, stamp: Path, *, repair: bool = False) -> int:
    expected = fingerprint(project)
    if not repair and read_stamp(stamp) == expected and desktop_present():
        return 0

    # If an install is interrupted or fails, never leave a current stamp behind.
    stamp.unlink(missing_ok=True)
    print("[MAX] Preparing desktop dependencies in .venv...", flush=True)
    args = [sys.executable, "-m", "pip", "install"]
    if repair:
        args.append("--force-reinstall")
    args.extend(["-e", ".[desktop]"])
    result = subprocess.run(args, cwd=project, check=False)
    if result.returncode:
        return result.returncode
    write_stamp(stamp, expected)
    return 0


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    stamp = project / ".venv" / ".max-desktop-installed"
    try:
        return prepare(project, stamp, repair=os.environ.get("MAX_DESKTOP_REPAIR") == "1")
    except OSError as exc:
        print(f"[MAX] Desktop setup could not finish: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
