"""Durable background jobs: a PostgreSQL schedule table that Celery Beat sweeps (11 Nov, D25).

Why a table and not Celery ETAs (Recommendation, Platform contest round 4):
* long waits (day 2/5/10 follow-ups, the 30/60/120-minute staff ladder) must survive a broker failover —
  Celery's Redis guide warns ETAs longer than the visibility timeout are "executed again, and again";
* "the broker is not a database": a lost Redis may cost a few seconds, never a reminder;
* replace / pull-forward / cancel by key are plain SQL.

Write path:  service -> `JobStore.enqueue(...)` (own transaction) -> `Kicker.kick(ids)` for work due now.
Dispatch:    Beat every 30 s -> `dispatch_due()` -> kick; the worker runs `JobRunner.run(job_id)`.
Run:         `claim()` is atomic (one runner per job; one job at a time per `lock_key`, oldest first)
             -> handler -> `finish()` (kicks the next job on the same lock) or `fail()` (back-off retry).
Recovery:    rows left `running` by a dead worker go back to `todo` (`recover()`); every handler is idempotent
             because `acks_late` can redeliver (Fallback plan: "No job runs twice with effect").

Packages never import Celery: they get a `JobStore` (and the `Kicker` the app built over Celery).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

import orjson
from sqlalchemy import TextClause, text

from kx.core.db import Databases
from kx.core.identity import new_id
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)

_SCHEMAS: Final = frozenset({"automation"})
DISPATCH_GRACE: Final = timedelta(seconds=60)  # a kicked job not claimed by then is kicked again
STUCK_AFTER: Final = timedelta(minutes=15)  # longest legitimate task run (PDF + upload + send)

# {t} = <schema>.scheduled_jobs (validated schema name, never user input)
_SQL: Final[dict[str, str]] = {
    "insert": (
        "INSERT INTO {t} (id, task, args, dedup_key, lock_key, run_at, max_attempts, created_at, updated_at) "
        "VALUES (:id, :task, CAST(:args AS jsonb), :dk, :lk, :at, :max, :now, :now) "
        "ON CONFLICT (dedup_key) WHERE status IN ('todo', 'running') DO NOTHING RETURNING id"
    ),
    "cancel_key": (
        "UPDATE {t} SET status = 'cancelled', updated_at = :now WHERE dedup_key = :v AND status = 'todo'"
    ),
    "cancel_prefix": (
        "UPDATE {t} SET status = 'cancelled', updated_at = :now "
        "WHERE starts_with(dedup_key, :v) AND status = 'todo'"
    ),
    "pull": (
        "UPDATE {t} SET run_at = :now, updated_at = :now WHERE dedup_key = :k AND status = 'todo' "
        "RETURNING id"
    ),
    "dispatched": "UPDATE {t} SET dispatched_at = :now WHERE id = ANY(:ids)",
    "due": (
        "SELECT id FROM {t} WHERE status = 'todo' AND run_at <= :now "
        "AND (dispatched_at IS NULL OR dispatched_at < :grace) ORDER BY run_at, created_at LIMIT :n"
    ),
    "recover": (
        "UPDATE {t} SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'todo' END, "
        "last_error = 'recovered: worker lost', updated_at = :now, dispatched_at = NULL "
        "WHERE status = 'running' AND updated_at < :cut"
    ),
    "lag": "SELECT min(run_at) FROM {t} WHERE status = 'todo' AND run_at <= :now",
    "head": "SELECT lock_key, created_at FROM {t} WHERE id = :id AND status = 'todo' AND run_at <= :now",
    "lock": "SELECT pg_advisory_xact_lock(hashtextextended(:k, 7))",
    "blocked": (
        "SELECT EXISTS (SELECT 1 FROM {t} o WHERE o.lock_key = :k AND o.id <> :id AND (o.status = 'running' "
        "OR (o.status = 'todo' AND o.run_at <= :now "
        "AND (o.created_at, o.id) < (CAST(:created AS timestamptz), CAST(:id AS uuid)))))"
    ),
    "take": (
        "UPDATE {t} SET status = 'running', attempts = attempts + 1, updated_at = :now "
        "WHERE id = :id AND status = 'todo' "
        "RETURNING id, task, args, dedup_key, lock_key, run_at, attempts, max_attempts"
    ),
    "finish": (
        "UPDATE {t} SET status = 'done', finished_at = :now, updated_at = :now, last_error = NULL "
        "WHERE id = :id"
    ),
    "next_on_lock": (
        "SELECT id FROM {t} WHERE lock_key = :k AND status = 'todo' AND run_at <= :now "
        "ORDER BY created_at, id LIMIT 1"
    ),
    "fail": (
        "UPDATE {t} SET status = :st, run_at = :at, last_error = :err, updated_at = :now, "
        "dispatched_at = NULL, finished_at = CASE WHEN :st = 'failed' THEN CAST(:now AS timestamptz) END "
        "WHERE id = :id"
    ),
}


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Job:
    id: uuid.UUID
    task: str
    args: dict[str, Any]
    dedup_key: str | None
    lock_key: str | None
    run_at: datetime
    attempts: int
    max_attempts: int

    @property
    def final_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


@dataclass(frozen=True, slots=True)
class NewJob:
    """What `enqueue` inserts."""

    task: str
    args: Mapping[str, Any]
    dedup_key: str | None = None
    lock_key: str | None = None
    run_at: datetime | None = None
    max_attempts: int = 5


class Kicker(Protocol):
    """Hands due job ids to the worker pool (Celery `send_task` in the app). Must not raise for a broker
    outage: the rows stay `todo` and the next Beat sweep sends them again."""

    async def kick(self, job_ids: Sequence[uuid.UUID]) -> None: ...


class RetryLater(Exception):
    """Raised by a handler for a transient failure: retried with back-off until `max_attempts`."""


Handler = Callable[[Job], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JobSpec:
    """One task type. `retry_on=None` retries any exception; otherwise only those (others fail at once)."""

    handler: Handler
    max_attempts: int = 5
    backoff_seconds: int = 6
    retry_on: tuple[type[BaseException], ...] | None = None


def backoff(attempts: int, base_seconds: int, cap: timedelta = timedelta(hours=1)) -> timedelta:
    """Exponential back-off: base, 2 x base, 4 x base ... capped (procrastinate's `exponential_wait` kept)."""
    return min(timedelta(seconds=base_seconds * 2 ** max(attempts - 1, 0)), cap)


def _ident(schema: str) -> str:
    if schema not in _SCHEMAS:
        raise ValueError(f"unknown schema {schema!r}")
    return schema


class JobStore:
    def __init__(
        self,
        databases: Databases,
        *,
        db: LogicalDb = LogicalDb.AUTOMATION,
        schema: str = "automation",
        kicker: Kicker | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._db = databases
        self._logical = db
        table = f"{_ident(schema)}.scheduled_jobs"
        self._q: dict[str, TextClause] = {k: text(v.format(t=table)) for k, v in _SQL.items()}
        self.kicker = kicker
        self._now = clock

    # ------------------------------------------------------------ write
    async def enqueue(
        self,
        task: str,
        args: Mapping[str, Any],
        *,
        dedup_key: str | None = None,
        lock_key: str | None = None,
        run_at: datetime | None = None,
        max_attempts: int = 5,
    ) -> uuid.UUID | None:
        """Insert one job; None when a pending job with the same `dedup_key` exists (procrastinate's
        queueing lock). Due work is kicked at once so chat replies never wait for the 30-second sweep."""
        job = NewJob(task, args, dedup_key, lock_key, run_at, max_attempts)
        async with self._db.transaction(self._logical) as s:
            job_id = await self._insert(s, job)
        await self._kick_if_due(job_id, job.run_at)
        return job_id

    async def replace(
        self,
        task: str,
        args: Mapping[str, Any],
        *,
        dedup_key: str,
        run_at: datetime | None,
        lock_key: str | None = None,
        max_attempts: int = 5,
    ) -> uuid.UUID | None:
        """The newest wins: cancel the pending (not running) job with this key, then insert."""
        job = NewJob(task, args, dedup_key, lock_key, run_at, max_attempts)
        async with self._db.transaction(self._logical) as s:
            await s.execute(self._q["cancel_key"], {"v": dedup_key, "now": self._now()})
            job_id = await self._insert(s, job)
        await self._kick_if_due(job_id, job.run_at)
        return job_id

    async def pull_forward(self, dedup_key: str) -> int:
        """Run the waiting job with this key now (e.g. the approver pressed 2: escalate at once)."""
        async with self._db.transaction(self._logical) as s:
            ids = list((await s.execute(self._q["pull"], {"k": dedup_key, "now": self._now()})).scalars())
        await self._kick(ids)
        return len(ids)

    async def cancel(self, *, dedup_key: str | None = None, dedup_prefix: str | None = None) -> int:
        """Cancel pending jobs by exact key or key prefix (§8.1 step 2: an opt-out cancels every queued
        follow-up, reminder and template for that buyer)."""
        if (dedup_key is None) == (dedup_prefix is None):
            raise ValueError("give exactly one of dedup_key / dedup_prefix")
        query, value = (
            (self._q["cancel_key"], dedup_key) if dedup_key else (self._q["cancel_prefix"], dedup_prefix)
        )
        async with self._db.transaction(self._logical) as s:
            result = await s.execute(query, {"v": value, "now": self._now()})
        return int(getattr(result, "rowcount", 0) or 0)

    async def _insert(self, s: Any, job: NewJob) -> uuid.UUID | None:
        now = self._now()
        params = {
            "id": new_id(),
            "task": job.task,
            "args": orjson.dumps(dict(job.args), default=str).decode(),
            "dk": job.dedup_key,
            "lk": job.lock_key,
            "at": job.run_at or now,
            "max": job.max_attempts,
            "now": now,
        }
        found: uuid.UUID | None = (await s.execute(self._q["insert"], params)).scalar_one_or_none()
        return found

    async def _kick_if_due(self, job_id: uuid.UUID | None, run_at: datetime | None) -> None:
        if job_id is not None and (run_at is None or run_at <= self._now()):
            await self._kick([job_id])

    async def _kick(self, ids: Sequence[uuid.UUID]) -> None:
        if not ids or self.kicker is None:
            return
        try:
            await self.kicker.kick(ids)
        except Exception:  # broker down: the rows stay todo; the next sweep sends them
            log.warning("jobs.kick_failed", count=len(ids), exc_info=True)
            return
        async with self._db.transaction(self._logical) as s:
            await s.execute(self._q["dispatched"], {"ids": list(ids), "now": self._now()})

    # ------------------------------------------------------------ dispatch (Beat)
    async def due(self, limit: int = 500) -> list[uuid.UUID]:
        """Todo jobs whose time has come and that were not kicked in the last minute, oldest first."""
        now = self._now()
        async with self._db.transaction(self._logical) as s:
            rows = await s.execute(self._q["due"], {"now": now, "grace": now - DISPATCH_GRACE, "n": limit})
            return list(rows.scalars())

    async def dispatch_due(self, limit: int = 500) -> int:
        """Beat's 30-second sweep: recover stuck rows, then kick everything due."""
        await self.recover()
        ids = await self.due(limit)
        await self._kick(ids)
        return len(ids)

    async def recover(self, stuck_after: timedelta = STUCK_AFTER) -> int:
        """A worker died mid-job (or acks_late redelivery lost the race): back to todo, attempt counted."""
        now = self._now()
        async with self._db.transaction(self._logical) as s:
            result = await s.execute(self._q["recover"], {"now": now, "cut": now - stuck_after})
        return int(getattr(result, "rowcount", 0) or 0)

    async def lag_seconds(self) -> float:
        """Age of the oldest due job (CloudWatch `Kairox/QueueLagSeconds`)."""
        now = self._now()
        async with self._db.transaction(self._logical) as s:
            oldest: datetime | None = await s.scalar(self._q["lag"], {"now": now})
        return max((now - oldest).total_seconds(), 0.0) if oldest else 0.0

    # ------------------------------------------------------------ run (worker)
    async def claim(self, job_id: uuid.UUID) -> Job | None:
        """Atomically take a due todo job. A job with a `lock_key` waits while another job on that key is
        running or an older one is still to do (per-thread delivery order, as procrastinate's `lock`)."""
        now = self._now()
        async with self._db.transaction(self._logical) as s:
            head = (await s.execute(self._q["head"], {"id": job_id, "now": now})).first()
            if head is None:
                return None
            if head.lock_key is not None:
                await s.execute(self._q["lock"], {"k": head.lock_key})
                blocked = await s.scalar(
                    self._q["blocked"],
                    {"k": head.lock_key, "id": job_id, "now": now, "created": head.created_at},
                )
                if blocked:
                    return None
            row = (await s.execute(self._q["take"], {"id": job_id, "now": now})).first()
        if row is None:
            return None
        return Job(
            id=row.id,
            task=row.task,
            args=dict(row.args),
            dedup_key=row.dedup_key,
            lock_key=row.lock_key,
            run_at=row.run_at,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
        )

    async def finish(self, job: Job) -> None:
        nxt: uuid.UUID | None = None
        async with self._db.transaction(self._logical) as s:
            await s.execute(self._q["finish"], {"id": job.id, "now": self._now()})
            if job.lock_key is not None:  # the next job on the same thread runs straight away
                nxt = await s.scalar(self._q["next_on_lock"], {"k": job.lock_key, "now": self._now()})
        if nxt is not None:
            await self._kick([nxt])

    async def fail(self, job: Job, error: str, *, retry: bool, backoff_seconds: int = 6) -> str:
        """Retry later (back-off) while attempts remain; else `failed`. Returns the new status."""
        now = self._now()
        status = "todo" if retry and not job.final_attempt else "failed"
        run_at = now + backoff(job.attempts, backoff_seconds) if status == "todo" else job.run_at
        async with self._db.transaction(self._logical) as s:
            await s.execute(
                self._q["fail"], {"id": job.id, "st": status, "at": run_at, "err": error[:2000], "now": now}
            )
        return status


@dataclass
class JobRunner:
    """Runs one job id: claim -> handler -> finish / fail. One runner serves every package's task types."""

    store: JobStore
    specs: dict[str, JobSpec] = field(default_factory=dict)

    def register(self, task: str, spec: JobSpec) -> None:
        if task in self.specs:
            raise ValueError(f"task {task!r} registered twice")
        self.specs[task] = spec

    async def run(self, job_id: uuid.UUID) -> str:
        job = await self.store.claim(job_id)
        if job is None:
            return "skipped"  # done, cancelled, not yet due, or another runner has it
        spec = self.specs.get(job.task)
        if spec is None:
            log.error("jobs.unknown_task", task=job.task, job_id=str(job.id))
            return await self.store.fail(job, f"unknown task {job.task}", retry=False)
        job = replace(job, max_attempts=spec.max_attempts)  # the task type owns its retry policy
        try:
            await spec.handler(job)
        except Exception as exc:
            retry = isinstance(exc, RetryLater) or spec.retry_on is None or isinstance(exc, spec.retry_on)
            status = await self.store.fail(
                job, f"{type(exc).__name__}: {exc}", retry=retry, backoff_seconds=spec.backoff_seconds
            )
            log.warning("jobs.failed", task=job.task, job_id=str(job.id), tries=job.attempts, status=status)
            return status
        await self.store.finish(job)
        return "done"
