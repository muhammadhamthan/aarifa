"""WP20 (D25): the PostgreSQL schedule table behind Celery, and the TaskScheduler built on it.

Platform contest round 4 conditions checked here: long waits are rows (not ETAs); a job runs once even when
claimed twice (acks_late redelivery); jobs sharing a lock key run one at a time, oldest first; a row left
`running` by a dead worker is recovered; retries back off; cancel-by-key serves the §8.1 opt-out.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from kx.automation.alerts import LadderTicket
from kx.automation.jobs import DELIVER, FOLLOWUP, LADDER, JobScheduler
from kx.core.db import Databases
from kx.core.event_payloads import DealNextActionSet, QuoteSubmitted, StaffContact
from kx.core.jobs import Job, JobRunner, JobSpec, JobStore, RetryLater, backoff
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db
NOW = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Kicks:
    def __init__(self) -> None:
        self.ids: list[uuid.UUID] = []

    async def kick(self, job_ids: Sequence[uuid.UUID]) -> None:
        self.ids.extend(job_ids)


@pytest.fixture
async def store(databases: Databases) -> AsyncIterator[tuple[JobStore, Clock, Kicks]]:
    async with databases.transaction(LogicalDb.AUTOMATION) as s:
        await s.execute(text("DELETE FROM automation.scheduled_jobs"))
    clock, kicks = Clock(), Kicks()
    yield JobStore(databases, kicker=kicks, clock=clock), clock, kicks


async def rows(dbs: Databases, task: str | None = None) -> list[Any]:
    async with dbs.transaction(LogicalDb.AUTOMATION) as s:
        q = "SELECT task, args, status, dedup_key, lock_key, run_at, attempts FROM automation.scheduled_jobs"
        result = await s.execute(
            text(q + (" WHERE task = :t" if task else "") + " ORDER BY created_at, id"), {"t": task}
        )
        return list(result.all())


def test_backoff_is_exponential_and_capped() -> None:
    assert [backoff(n, 4).total_seconds() for n in (1, 2, 3)] == [4, 8, 16]
    assert backoff(30, 6) == timedelta(hours=1)


async def test_enqueue_dedupes_and_kicks_only_due_work(
    store: tuple[JobStore, Clock, Kicks], databases: Databases
) -> None:
    js, clock, kicks = store
    first = await js.enqueue("t.a", {"x": 1}, dedup_key="k1")
    assert first is not None and kicks.ids == [first]  # due now: kicked at once (no 30 s wait for chat)
    assert await js.enqueue("t.a", {"x": 2}, dedup_key="k1") is None  # pending: de-duplicated
    later = await js.enqueue("t.a", {}, dedup_key="k2", run_at=NOW + timedelta(days=2))
    assert later is not None and later not in kicks.ids  # a long wait is a row, never a Celery ETA
    assert await js.due() == []  # k1 was kicked a moment ago; k2 is not due
    clock.now = NOW + timedelta(days=2, seconds=1)
    assert set(await js.due()) == {first, later}  # k1 never claimed -> kicked again after the grace


async def test_two_claims_run_a_job_once(store: tuple[JobStore, Clock, Kicks]) -> None:
    js, _, _ = store
    runs: list[uuid.UUID] = []

    async def handler(job: Job) -> None:
        runs.append(job.id)

    runner = JobRunner(js)
    runner.register("t.once", JobSpec(handler))
    job_id = await js.enqueue("t.once", {})
    assert job_id is not None
    assert await runner.run(job_id) == "done"
    assert await runner.run(job_id) == "skipped"  # acks_late redelivery: a no-op
    assert runs == [job_id]


async def test_lock_key_runs_one_at_a_time_oldest_first(
    store: tuple[JobStore, Clock, Kicks], databases: Databases
) -> None:
    js, clock, kicks = store
    a = await js.enqueue("t.d", {"n": 1}, lock_key="thread:t1")
    clock.now = NOW + timedelta(seconds=1)
    b = await js.enqueue("t.d", {"n": 2}, lock_key="thread:t1")
    assert a is not None and b is not None
    assert await js.claim(b) is None  # an older job on the same thread is still to do
    job_a = await js.claim(a)
    assert job_a is not None and await js.claim(b) is None  # and now it is running
    kicks.ids.clear()
    await js.finish(job_a)
    assert kicks.ids == [b]  # the next on the thread is kicked straight away
    assert await js.claim(b) is not None


async def test_retry_backs_off_then_fails(store: tuple[JobStore, Clock, Kicks], databases: Databases) -> None:
    js, clock, _ = store

    async def flaky(job: Job) -> None:
        raise RetryLater("provider 503")

    async def broken(job: Job) -> None:
        raise ValueError("bad payload")

    runner = JobRunner(js)
    runner.register("t.flaky", JobSpec(flaky, max_attempts=2, backoff_seconds=10))
    runner.register("t.strict", JobSpec(broken, retry_on=(RetryLater,)))
    job_id = await js.enqueue("t.flaky", {})
    assert job_id is not None and await runner.run(job_id) == "todo"
    [(_, _, status, _, _, run_at, attempts)] = await rows(databases, "t.flaky")
    assert (status, attempts, run_at) == ("todo", 1, NOW + timedelta(seconds=10))
    clock.now = run_at
    assert await runner.run(job_id) == "failed"  # attempts exhausted
    strict = await js.enqueue("t.strict", {})
    assert strict is not None and await runner.run(strict) == "failed"  # not a retryable error: no retry


async def test_dead_worker_row_is_recovered(
    store: tuple[JobStore, Clock, Kicks], databases: Databases
) -> None:
    js, clock, _ = store
    job_id = await js.enqueue("t.r", {})
    assert job_id is not None and await js.claim(job_id) is not None  # running... and the worker dies
    clock.now = NOW + timedelta(minutes=16)
    assert await js.recover() == 1
    assert (await rows(databases, "t.r"))[0].status == "todo"
    assert await js.lag_seconds() == pytest.approx(16 * 60)


async def test_scheduler_delivery_dedupes_and_orders_per_thread(
    store: tuple[JobStore, Clock, Kicks], databases: Databases
) -> None:
    js, _, _ = store
    scheduler = JobScheduler(js)
    message = uuid.uuid4()
    await scheduler.enqueue_delivery(message, lock="thread:t1", at=None, template_params=("Q-1",))
    await scheduler.enqueue_delivery(message, lock="thread:t1", at=None)  # requeue sweep: no duplicate
    await scheduler.enqueue_delivery(uuid.uuid4(), lock="thread:t1", at=None)
    jobs = await rows(databases, DELIVER)
    assert len(jobs) == 2 and {j.lock_key for j in jobs} == {"thread:t1"}
    assert jobs[0].args == {"message_id": str(message), "template_params": ["Q-1"]}


async def test_followup_newest_replaces_pending_and_opt_out_cancels(
    store: tuple[JobStore, Clock, Kicks], databases: Databases
) -> None:
    js, _, _ = store
    scheduler = JobScheduler(js)
    deal = uuid.uuid4()
    for reason, days in (("followup_d2", 2), ("followup_d5", 5)):
        action = DealNextActionSet(
            deal_ref=deal,
            contact_ref=uuid.uuid4(),
            due_at=NOW + timedelta(days=days),
            reason=reason,
            buyer_timezone="Asia/Dubai",
        )  # type: ignore[arg-type]
        await scheduler.schedule_followup(action, action.due_at)
    pending = [j for j in await rows(databases, FOLLOWUP) if j.status == "todo"]
    assert [j.args["action"]["reason"] for j in pending] == ["followup_d5"]
    assert await scheduler.cancel_followups(deal) == 1  # §8.1 step 2
    assert all(j.status == "cancelled" for j in await rows(databases, FOLLOWUP))


async def test_press_2_pulls_the_waiting_step_forward(
    store: tuple[JobStore, Clock, Kicks], databases: Databases
) -> None:
    js, _, kicks = store
    scheduler = JobScheduler(js)
    staff = StaffContact(user_ref=uuid.uuid4(), name="Sara", phone="+971501111111", email=None)
    sub = QuoteSubmitted(
        approval_ref=uuid.uuid4(),
        quote_ref=uuid.uuid4(),
        quote_version_ref=uuid.uuid4(),
        deal_ref=uuid.uuid4(),
        kind="commercial",
        required_role="sales",
        approver=staff,
        escalate_to=staff,
        due_at=NOW,
        quote_number="Q-1",
        total_display="AED 1",
    )
    await scheduler.schedule_ladder_step(LadderTicket.for_approval(sub), 3, NOW + timedelta(minutes=60))
    kicks.ids.clear()
    await scheduler.pull_ladder_forward(sub.approval_ref)
    [job] = await rows(databases, LADDER)
    assert job.args["step"] == 3 and job.run_at == NOW and len(kicks.ids) == 1
    await scheduler.pull_ladder_forward(uuid.uuid4())  # nothing waiting: nothing kicked
    assert len(kicks.ids) == 1
