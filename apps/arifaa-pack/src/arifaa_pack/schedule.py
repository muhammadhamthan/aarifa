"""Celery Beat timetable (UTC; Dubai = UTC+4). Run **exactly one** Beat (`kx beat`, ECS desired count 1).

Each entry runs `kx.periodic(<name>)`; the worker maps the name to a package's job body (`periodic_jobs()` in
kx.crm.jobs / kx.automation.jobs, plus the app's own). Long waits are rows in automation.scheduled_jobs,
swept by `jobs.dispatch_due`; Beat itself only sends timers.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

from celery.schedules import crontab

from arifaa_pack.config import app_settings

PERIODIC_TASK: Final = "kx.periodic"


def beat_schedule() -> dict[str, dict[str, Any]]:
    s = app_settings()
    every: dict[str, Any] = {
        # the schedule-table sweep (Recommendation: "Beat sweeps every 30 s"); also recovers stuck rows
        "jobs.dispatch_due": timedelta(seconds=s.beat_sweep_seconds),
        # outbox relays: chat replies must not wait a minute
        "relay.crm": timedelta(seconds=s.relay_seconds),
        "relay.automation": timedelta(seconds=s.relay_seconds),
        "relay.agents": timedelta(seconds=s.relay_seconds),
        # Better Stack heartbeat (missing 2 min = page) + Kairox/QueueLagSeconds
        "app.heartbeat": timedelta(seconds=30),
        "crm.approval_sweep": crontab(minute="*/5"),
        "automation.requeue_outbound": crontab(minute="*/5"),
        "crm.lost_proposals": crontab(minute=7, hour=4),  # 08:07 Dubai, start of staff hours
        "crm.price_staleness": crontab(minute=23, hour=5),  # 09:23 Dubai: quote expiry (v1), staleness (v2)
        "crm.retention": crontab(minute=41, hour=23),  # 03:41 Dubai
        # §21 daily summary / weekly report: the times are settings, so Beat only asks every 15 min
        "crm.scheduled_reports": crontab(minute="*/15"),
        # v2 (D14): runs daily but returns at once while `features.scraping` is off
        "automation.price_scrapes": crontab(minute=0, hour=2),  # 06:00 Dubai
    }
    return {name: {"task": PERIODIC_TASK, "schedule": when, "args": (name,)} for name, when in every.items()}
