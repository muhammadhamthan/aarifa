"""The one Celery app (11 Nov, D25) and the `kx.core.jobs.Kicker` over it.

Imported by every process: the API and the worker kick due jobs through it; Beat sends the timers. It does
not import the container (the worker builds that lazily in `arifaa_pack.celery_app`), so there is no cycle.

Settings from the platform docs and Celery's own guides (Official sources, 11 Nov):
* `acks_late` + `task_reject_on_worker_lost`: a task is re-delivered if a worker dies, so every job body is
  idempotent and every send checks its idempotency key;
* prefetch 1: a long PDF job never holds short chat replies hostage;
* `visibility_timeout` above the longest task; long waits are rows in PostgreSQL, never ETAs;
* Redis `noeviction` is set on the ElastiCache parameter group (infra), not here.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from functools import lru_cache
from typing import Final

from celery import Celery

from arifaa_pack.config import app_settings
from kx.core.settings import get_settings

RUN_JOB: Final = "kx.run_job"
QUEUE: Final = "kx"


@lru_cache(maxsize=1)
def celery_app() -> Celery:
    s = app_settings()
    broker = s.celery_broker_url or str(get_settings().redis_url)
    app = Celery("kairox", broker=broker)
    app.conf.update(
        task_default_queue=QUEUE,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_serializer="json",
        accept_content=["json"],
        result_backend=None,
        task_ignore_result=True,
        timezone="UTC",
        enable_utc=True,
        broker_connection_retry_on_startup=True,
        broker_transport_options={"visibility_timeout": s.celery_visibility_timeout_seconds},
        worker_hijack_root_logger=False,
    )
    return app


class CeleryKicker:
    """Sends `kx.run_job(<id>)` for each due job. Runs the blocking publish in a thread so the API event loop
    never waits on Redis; a broker failure raises and the store leaves the rows for the next sweep."""

    def __init__(self, app: Celery | None = None) -> None:
        self._app = app or celery_app()

    async def kick(self, job_ids: Sequence[uuid.UUID]) -> None:
        ids = [str(i) for i in job_ids]

        def send() -> None:
            for job_id in ids:
                self._app.send_task(
                    RUN_JOB,
                    args=[job_id],
                    queue=QUEUE,
                    retry=True,
                    retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2},
                )

        await asyncio.to_thread(send)
