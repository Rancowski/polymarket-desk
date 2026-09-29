"""Desk release id: git SHA on a clone, deploy/VERSION or agent/COMMIT if .git is missing."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _file_sha(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except Exception:
        return None
    if text and text.lower() != "unknown":
        return text[:12]
    return None


def release() -> str:
    git = ROOT / ".git"
    if git.exists():
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "--short=7", "HEAD"],
                cwd=str(ROOT),
                timeout=4,
                stderr=subprocess.DEVNULL,
            )
            sha = out.decode("utf-8", errors="replace").strip()
            if sha:
                return sha
        except Exception:
            pass
    for path in (ROOT / "deploy" / "VERSION", Path(__file__).with_name("COMMIT")):
        got = _file_sha(path)
        if got:
            return got
    return "unknown"
