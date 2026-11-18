"""Code identity of the running process, captured ONCE at import time (= process start).

Traces record this so a reviewer can tell which revision actually produced an investigation,
and whether the working tree had uncommitted changes when the process started.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=_ROOT, text=True, stderr=subprocess.DEVNULL, timeout=10
        ).strip()
    except Exception:
        return None


_porcelain = _git("status", "--porcelain")

STARTUP: dict = {
    "commit": _git("rev-parse", "HEAD") or "unknown",
    "dirty": None if _porcelain is None else bool(_porcelain),   # None = git unavailable
    "captured_at": datetime.now(timezone.utc).isoformat(),
}
