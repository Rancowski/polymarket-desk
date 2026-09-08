"""Desk release id: git SHA on a clone, agent/COMMIT after tarball deploy."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def release() -> str:
    commit = Path(__file__).with_name("COMMIT")
    if commit.exists():
        text = commit.read_text(encoding="utf-8").strip()
        if text and text.lower() != "unknown":
            return text[:12]
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
    return "unknown"
