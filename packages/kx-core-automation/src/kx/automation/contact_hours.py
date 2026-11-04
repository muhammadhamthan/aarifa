"""When proactive messages may go out (pure functions, no I/O).

Follow-ups go 09:00-18:00 buyer local time and at most `max_per_week` in ANY rolling 7 days
(CONTRACTS §3). A follow-up that would break the cap is moved later, not dropped: day 2/5/10
reminders are part of the agreed sales flow, so the buyer still gets it, just not a third that week.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

from kx.core.errors import RuleViolation
from kx.core.timewindow import Window

WEEK: Final = timedelta(days=7)
_MAX_SHIFTS: Final = 64


def cap_conflict(
    at: datetime, existing: Sequence[datetime], max_n: int, period: timedelta = WEEK
) -> datetime | None:
    """None when sending at `at` keeps every `period`-long span at <= max_n messages.

    Otherwise the earliest instant at which the blocking group's first message stops counting.
    A span containing `at` and max_n others exists iff some max_n consecutive (sorted) neighbours
    together with `at` fit inside one period.
    """
    near = sorted(e for e in existing if abs(e - at) < period)
    for i in range(len(near) - max_n + 1):
        group = near[i : i + max_n]
        if max(group[-1], at) - min(group[0], at) < period:
            return group[0] + period
    return None


def next_followup_slot(due: datetime, existing: Sequence[datetime], window: Window, max_n: int) -> datetime:
    """Earliest time >= due inside the buyer's window that respects the weekly cap."""
    at = window.next_open(due)
    for _ in range(_MAX_SHIFTS):
        blocked_until = cap_conflict(at, existing, max_n)
        if blocked_until is None:
            return at
        at = window.next_open(max(blocked_until, at))
    raise RuleViolation("no follow-up slot found within the weekly cap")  # pragma: no cover - needs >64 rows
