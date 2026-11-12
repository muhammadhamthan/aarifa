"""Celery worker tasks (11 Nov, D25). `celery -A arifaa_pack.celery_app worker` / `beat` (via `kx worker`,
`kx beat`).

Platform contest round 4 conditions, as built here:
* **one event loop per worker process** — the async platform code runs on a loop created (with the
  container) on the process's first task and reused for every task after it;
* **idempotent tasks with acks_late** — `kx.run_job` claims its row atomically, so a redelivered message
  for a job already done, running or cancelled is a no-op;
* **exactly one Beat** — enforced by ECS (desired count 1, no overlap on deploy), not by code;
* **long waits in PostgreSQL** — Beat never schedules ETAs; it only sends `kx.periodic(<name>)`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any, TypeVar

from celery import signals

from arifaa_pack.config import app_settings
from arifaa_pack.jobqueue import RUN_JOB, celery_app
from arifaa_pack.schedule import PERIODIC_TASK, beat_schedule
from kx.core.logging import configure_logging, get_logger
from kx.core.settings import get_settings

T = TypeVar("T")
log = get_logger(__name__)
app = celery_app()
app.conf.beat_schedule = beat_schedule()


class _Process:
    """The worker process's loop and container (one each, for the life of the process)."""

    loop: asyncio.AbstractEventLoop | None = None
    stack: AsyncExitStack | None = None
    container: Any = None

    @classmethod
    def run(cls, work: Callable[[Any], Awaitable[T]]) -> T:
        if cls.loop is None:
            from arifaa_pack.container import build  # noqa: PLC0415 - heavy; only in worker processes

            cls.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(cls.loop)
            cls.stack = AsyncExitStack()
            cls.container = cls.loop.run_until_complete(cls.stack.enter_async_context(build()))
        return cls.loop.run_until_complete(work(cls.container))

    @classmethod
    def close(cls) -> None:
        if cls.loop is not None and cls.stack is not None:
            cls.loop.run_until_complete(cls.stack.aclose())
            cls.loop.close()
        cls.loop = cls.stack = cls.container = None


@signals.worker_process_init.connect
def _configure(**_: Any) -> None:
    core = get_settings()
    configure_logging(core.log_level, core.log_json)
    from arifaa_pack.observability import init_error_tracking  # noqa: PLC0415

    init_error_tracking(app_settings(), core.env.value, "worker")


@signals.worker_process_shutdown.connect
def _shutdown(**_: Any) -> None:
    _Process.close()


@app.task(name=RUN_JOB, acks_late=True, ignore_result=True)
def run_job(job_id: str) -> str:
    """Run one row of automation.scheduled_jobs (claim -> handler -> finish / retry)."""
    return str(_Process.run(lambda c: c.runner.run(uuid.UUID(job_id))))


@app.task(name=PERIODIC_TASK, acks_late=True, ignore_result=True)
def periodic(name: str) -> None:
    """One Beat entry. Unknown names are logged, not retried (a renamed job must not loop)."""

    async def work(c: Any) -> None:
        fn = c.periodic.get(name)
        if fn is None:
            log.error("periodic.unknown", name=name)
            return
        await fn(datetime.now(UTC))

    _Process.run(work)
