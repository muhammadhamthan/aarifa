"""Outbound kill switch for automation: the D35 ladder in `kx.core.killswitch` (one level per company, set
from the CRM admin screen) plus the older per-channel keys. Automation only reads it, at send time and again
at delivery time, so messages queued before the switch was thrown stay put.
"""

from __future__ import annotations

from kx.core.killswitch import KillSwitchLadder

KillSwitch = KillSwitchLadder

__all__ = ["KillSwitch"]
