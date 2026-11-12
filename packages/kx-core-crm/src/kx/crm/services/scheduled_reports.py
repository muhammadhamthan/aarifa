"""§21 scheduled reports: "a daily summary and a weekly report to the manager and MD (recipients and times
are settings)".

* Beat asks every 15 minutes (`crm.scheduled_reports`); a report goes once its time has passed in company
  time (the alert-hours zone). Each one is sent once: the audit row `report.sent` for the period's key is
  the marker, and the staff notice carries the same key, so a redelivered job never sends twice.
* The figures are the manager dashboard's (`kx.crm.services.dashboards.manager_board`), so the message and
  the screen never disagree. The daily summary covers yesterday; the weekly report the 7 days before today.
* The message is one line (a WhatsApp template parameter cannot hold line breaks); the detail stays on the
  manager and MD dashboards.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.event_payloads import StaffNotice
from kx.crm.deps import CrmDeps
from kx.crm.models import AuditEvent, User
from kx.crm.services import staff
from kx.crm.services.dashboards import ManagerBoard, manager_board
from kx.crm.services.journal import Actor, Emit, record
from kx.crm.services.settings import BusinessSettings

Kind = Literal["daily", "weekly"]
ACTION = "report.sent"


async def send_due(session: AsyncSession, deps: CrmDeps) -> int:
    """Send whichever report is due now and not sent yet. Returns how many went out (0, 1 or 2)."""
    settings = await deps.settings.get(session)
    zone = ZoneInfo(settings.alert_hours.tz or "Asia/Dubai")
    local = deps.now().astimezone(zone)
    today = local.date()
    sent = 0
    if local.time() >= settings.report_daily_time:
        yesterday = today - timedelta(days=1)
        sent += await _send(session, settings, kind="daily", start=yesterday, end=yesterday, zone=zone)
    if local.isoweekday() == settings.report_weekly_day and local.time() >= settings.report_weekly_time:
        week = today - timedelta(days=7), today - timedelta(days=1)
        sent += await _send(session, settings, kind="weekly", start=week[0], end=week[1], zone=zone)
    return sent


async def _send(
    session: AsyncSession,
    settings: BusinessSettings,
    *,
    kind: Kind,
    start: date,
    end: date,
    zone: ZoneInfo,
) -> int:
    key = f"{kind}:{end.isoformat()}"
    done = await session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.action == ACTION, AuditEvent.entity_type == "report", AuditEvent.entity_id == key
        )
    )
    if done is not None:
        return 0
    users = (
        await session.execute(
            select(User)
            .where(User.role.in_(settings.report_recipients), User.is_active)
            .order_by(User.role, User.full_name)
        )
    ).scalars()
    notify = tuple(staff.staff_contact(u) for u in users)
    if not notify:
        return 0  # nobody holds the roles yet; the report goes once a recipient exists
    since = datetime.combine(start, time.min, zone).astimezone(UTC)
    until = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC)
    board = await manager_board(session, start, end, since, until)
    text = summary_text(kind, start, end, board)
    await record(
        session,
        Actor.system("scheduled_reports"),
        ACTION,
        "report",
        key,
        after={"kind": kind, "from": start, "to": end, "recipients": [str(c.user_ref) for c in notify]},
        emit=Emit(
            ev.STAFF_NOTICE,
            "report",
            key,
            StaffNotice(deal_ref=None, notify=notify, text=text, key=f"report:{key}"),
        ),
    )
    return 1


def summary_text(kind: Kind, start: date, end: date, b: ManagerBoard) -> str:
    """One line with the §21 manager figures for the period."""
    period = end.strftime("%d %b %Y") if start == end else f"{start:%d %b} to {end:%d %b %Y}"
    title = "Daily summary" if kind == "daily" else "Weekly report"
    sent = sum(x.quotes_sent for x in b.salespeople)
    won = sum(x.deals_won for x in b.salespeople)
    waiting = sum(x.waiting_for_me for x in b.salespeople)
    changes = sum(b.change_requests_by_type.values())
    return (
        f"{title} {period}: {sent} quotes sent, {won} deals won, {b.sla_breaches} SLA breaches, "
        f"{b.below_minimum_requests} below-minimum requests, {changes} change requests, "
        f"{b.price_list_changes} price-list changes, {b.opted_out} opt-outs; "
        f"{waiting} items waiting for the sales team now. Details on the CRM dashboards."
    )
