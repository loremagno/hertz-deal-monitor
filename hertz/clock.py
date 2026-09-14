"""One clock for every stamp that crosses machines.

Seeds are produced on a laptop in Ohio and adopted on a runner in UTC; the
cache stamps they are compared with are written by whichever machine ran
last. Naive local time on both sides made a seed refit at 00:23 Eastern
look older than a runner fetch at 03:33 UTC made an hour earlier, so the
committed curve was never adopted. Everything compared across machines is
stamped in naive UTC from here.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    """Naive UTC, so it compares with what the runner writes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")
