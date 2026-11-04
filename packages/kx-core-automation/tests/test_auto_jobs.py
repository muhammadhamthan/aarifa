"""Blueprint composition and the procrastinate-backed TaskScheduler (in-memory connector)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from procrastinate import App
from procrastinate.testing import InMemoryConnector

from kx.automation.jobs import ProcrastinateScheduler, build_jobs
from kx.core.event_payloads import DealNextActionSet, QuoteSubmitted, StaffContact

NOW = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)


@pytest.fixture
async def setup() -> AsyncIterator[tuple[InMemoryConnector, ProcrastinateScheduler]]:
    connector = InMemoryConnector()
    app = App(connector=connector)
    jobs = build_jobs()
    app.add_tasks_from(jobs.blueprint, namespace="automation")
    async with app.open_async():
        yield connector, ProcrastinateScheduler(app, jobs)


def todo(connector: InMemoryConnector, task: str) -> list[dict[str, object]]:
    return [
        j for j in connector.jobs.values() if j["task_name"] == f"automation:{task}" and j["status"] == "todo"
    ]


def test_blueprint_names_and_periodic_tasks() -> None:
    app = App(connector=InMemoryConnector())
    app.add_tasks_from(build_jobs().blueprint, namespace="automation")
    assert "automation:deliver_outbound" in app.tasks
    periodic = {p.task.name: p.cron for p in app.periodic_registry.periodic_tasks.values()}
    assert periodic == {
        "automation:relay_outbox": "* * * * * */5",
        "automation:approval_sweep": "*/5 * * * *",
        "automation:run_price_scrapes": "0 2 * * *",
        "automation:requeue_outbound": "*/5 * * * *",
    }


async def test_delivery_dedupes_per_message_and_orders_per_thread(
    setup: tuple[InMemoryConnector, ProcrastinateScheduler],
) -> None:
    connector, scheduler = setup
    message = uuid.uuid4()
    await scheduler.enqueue_delivery(message, lock="thread:t1", at=None, template_params=("Q-1",))
    await scheduler.enqueue_delivery(message, lock="thread:t1", at=None)  # requeue sweep: no duplicate
    await scheduler.enqueue_delivery(uuid.uuid4(), lock="thread:t1", at=None)
    jobs = todo(connector, "deliver_outbound")
    assert len(jobs) == 2 and {j["lock"] for j in jobs} == {"thread:t1"}
    assert jobs[0]["args"] == {"message_id": str(message), "template_params": ["Q-1"]}


async def test_followup_newest_replaces_pending(
    setup: tuple[InMemoryConnector, ProcrastinateScheduler],
) -> None:
    connector, scheduler = setup
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
    (job,) = todo(connector, "send_followup")
    assert job["args"]["action"]["reason"] == "followup_d5"  # type: ignore[index]


async def test_press_2_pulls_waiting_step_forward(
    setup: tuple[InMemoryConnector, ProcrastinateScheduler],
) -> None:
    connector, scheduler = setup
    staff = StaffContact(user_ref=uuid.uuid4(), name="Sara", phone="+971501111111", email=None)
    sub = QuoteSubmitted(
        approval_ref=uuid.uuid4(),
        quote_ref=uuid.uuid4(),
        quote_version_ref=uuid.uuid4(),
        deal_ref=uuid.uuid4(),
        kind="commercial",
        required_role="manager",
        approver=staff,
        escalate_to=staff,
        due_at=NOW,
        quote_number="Q-1",
        total_display="AED 1",
    )
    await scheduler.schedule_ladder_step(sub, 3, NOW + timedelta(minutes=20))
    await scheduler.pull_ladder_forward(sub.approval_ref)
    (job,) = todo(connector, "run_approval_ladder")
    assert job["args"]["attempt_no"] == 3 and job["scheduled_at"] is None  # type: ignore[index]
    await scheduler.pull_ladder_forward(uuid.uuid4())  # nothing waiting: nothing deferred
    assert len(todo(connector, "run_approval_ladder")) == 1
