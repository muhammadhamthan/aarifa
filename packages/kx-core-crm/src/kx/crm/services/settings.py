"""MD-editable business settings (crm.settings + crm.feature_flags) as one typed, cached snapshot.

Why a snapshot: every rule (approval window, MD limit, staleness, grant TTL ...) reads several keys
in one request; one query + one pydantic parse per TTL keeps that cheap, and parsing the whole table
on every *write* guarantees the MD can never save a value that breaks another rule. Other processes
see a change within `ttl_seconds`; SETTINGS_CHANGED lets consumers (automation, agents) refresh too.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import time, timedelta
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.errors import NotFound, RuleViolation
from kx.core.event_payloads import SettingsChanged
from kx.core.timewindow import Window
from kx.crm.models import FeatureFlag, Setting, SettingHistory
from kx.crm.services.journal import Actor, Emit, record


class Hours(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: time
    end: time
    tz: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


def _alias(key: str, default: Any, **kw: Any) -> Any:
    return Field(default, alias=key, **kw)


Incoterm = Literal["EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DDP"]
# §9.2 optional assignment rules, checked in this order when listed: language, country, category
AssignmentRule = Literal["language", "country", "category"]
# §21: who receives the scheduled reports (CRM roles; every active user with the role)
ReportRecipient = Literal["manager", "md", "admin"]


class BusinessSettings(BaseModel):
    """Typed view of crm.settings. Defaults mirror the DDL seed so a missing row never breaks a rule."""

    model_config = ConfigDict(frozen=True, extra="ignore", validate_by_alias=True, validate_by_name=True)

    approval_window_minutes: int = _alias("approval.window_minutes", 120, gt=0)
    md_value_limit_aed: Decimal | None = _alias("approval.md_value_limit_aed", None, gt=0)
    call_attempts: int = _alias("approval.call_attempts", 3, ge=0)
    call_gap_minutes: int = _alias("approval.call_gap_minutes", 20, gt=0)
    alert_hours: Hours = _alias("approval.alert_hours", Hours(start=time(8), end=time(22), tz="Asia/Dubai"))
    below_range_reply_minutes: int = _alias("negotiation.below_range_reply_minutes", 60, gt=0)
    contact_hours: Hours = _alias("contact.hours", Hours(start=time(9), end=time(18)))
    followup_cadence_days: tuple[int, ...] = _alias("followup.cadence_days", (2, 5, 10))
    followup_max_per_week: int = _alias("followup.max_per_week", 2, ge=0)
    quote_validity_days: int = _alias("quote.validity_days", 7, ge=1, le=60)
    vat_pct_domestic: Decimal = _alias("quote.vat_pct_domestic", Decimal(5), ge=0)
    stale_warn_days: int = _alias("pricing.stale_warn_days", 3, ge=0)
    stale_block_days: int = _alias("pricing.stale_block_days", 7, gt=0)
    digest_time: time = _alias("pricing.digest_time", time(9))
    grant_ttl_hours: int = _alias("grant.ttl_hours", 48, gt=0)
    assistant_name: str = _alias("ai.assistant_name", "Sara", min_length=1)
    max_concessions_default: int = _alias("ai.max_concessions_default", 3, ge=0, le=10)
    retention_audio_days: int = _alias("retention.audio_days", 90, gt=0)
    retention_open_lead_chat_months: int = _alias("retention.open_lead_chat_months", 24, gt=0)
    retention_won_customer_years: int = _alias("retention.won_customer_years", 5, gt=0)
    retention_lost_lead_months: int = _alias("retention.lost_lead_months", 12, gt=0)
    # ---- 5 Nov flow (docs/V1_CHANGE_PLAN.md WP1 seed); defaults mirror the seed
    pricing_mode: Literal["price_list", "cost"] = _alias("pricing.mode", "price_list")  # D6; cost = v2
    vat_pct_export: Decimal = _alias("quote.vat_pct_export", Decimal(0), ge=0)  # §10
    default_incoterm: Incoterm = _alias("quote.default_incoterm", "EXW")  # §11.2, D6
    # §11.2 "Terms: payment terms (advance %, balance)... default terms; salesperson can edit". The advance
    # % per customer type (§15.1) is still to confirm, so the default names no % (docs/OPEN_QUESTIONS.md).
    payment_terms_text: str = _alias(
        "quote.payment_terms_text", "Advance payment is required before production starts.", min_length=1
    )
    # §8 "delivery country ... sets currency": ISO-2 country -> ISO currency; unlisted countries quote in AED
    currency_by_country: dict[str, str] = _alias("quote.currency_by_country", {})
    max_steps: int = _alias("negotiation.max_steps", 3, ge=0, le=10)  # §12.1, D7
    free_samples_allowed: bool = _alias("negotiation.free_samples_allowed", False)  # §12.2 rule 6
    change_request_sla_working_hours: int = _alias("change_request.sla_working_hours", 4, gt=0)  # §13, D12
    reminder_minutes: tuple[int, int, int] = _alias("approval.reminder_minutes", (30, 60, 120))  # §16, D4
    followup_lost_after_days: int = _alias("followup.lost_after_days", 14, gt=0)  # §16, D15
    assignment_rules: tuple[AssignmentRule, ...] = _alias("assignment.rules", ())  # §9, D5
    feature_scraping: bool = _alias("features.scraping", False)  # D14 (v2)
    feature_delivery_calc: bool = _alias("features.delivery_calc", False)  # D14 (v2)
    feature_news_digest: bool = _alias("features.news_digest", False)  # D14 (v2)
    order_status_to_buyer: bool = _alias("notifications.order_status_to_buyer", False)  # §15.3 client choice
    # §16 / §23 scenario 9: ISO weekdays off per buyer country (only the Saudi weekend is in the documents)
    weekend_days_by_country: dict[str, tuple[int, ...]] = _alias(
        "followup.weekend_days_by_country", {"SA": (5, 6)}
    )
    # ---- 11 Nov SRS §8.1 (D18): crm_0009 seed
    optout_ask_reason: bool = _alias("optout.ask_reason", False)  # off until a lawyer confirms
    optout_live_minutes: int = _alias("optout.live_minutes", 10, ge=1)
    # §14 practical notes: after 24 h without a buyer message only an approved template restarts the chat,
    # "Sara from Arifa Pack here about your order - reply to continue." ({{1}} salesperson, {{2}} company)
    handover_reopen_template: str = _alias("handover.reopen_template", "staff_reopen", min_length=1)
    optout_unclear_answer_hours: int = _alias("optout.unclear_answer_hours", 24, ge=1)
    # §21 scheduled reports: a daily summary and a weekly report to the manager and MD (company time, the
    # alert-hours zone). Times and the weekday are proposals in OPEN_QUESTIONS #67; crm_0011 seed.
    report_daily_time: time = _alias("reports.daily_time", time(8))
    report_weekly_day: int = _alias("reports.weekly_day", 1, ge=1, le=7)  # ISO weekday, 1 = Monday
    report_weekly_time: time = _alias("reports.weekly_time", time(8))
    report_recipients: tuple[ReportRecipient, ...] = _alias("reports.recipients", ("manager", "md"))
    flags: dict[str, bool] = Field(default_factory=dict)

    @property
    def optout_unclear_answer(self) -> timedelta:
        return timedelta(hours=self.optout_unclear_answer_hours)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.stale_block_days < self.stale_warn_days:
            raise ValueError("pricing.stale_block_days must be >= pricing.stale_warn_days")
        days = self.followup_cadence_days
        if any(d <= 0 for d in days) or list(days) != sorted(set(days)):
            raise ValueError("followup.cadence_days must be increasing positive days")
        if self.alert_hours.tz is None:
            raise ValueError("approval.alert_hours needs a tz")
        mins = self.reminder_minutes
        if any(m <= 0 for m in mins) or list(mins) != sorted(set(mins)):
            raise ValueError("approval.reminder_minutes must be 3 increasing positive minutes")
        if len(set(self.assignment_rules)) != len(self.assignment_rules):
            raise ValueError("assignment.rules lists a rule twice")
        if any(len(k) != 2 or len(v) != 3 for k, v in self.currency_by_country.items()):
            raise ValueError("quote.currency_by_country maps ISO-2 countries to ISO-3 currencies")
        return self

    def quote_currency(self, country: str | None, fallback: str = "AED") -> str:
        """§8: the delivery country sets the quote currency (AED unless the setting maps it)."""
        return self.currency_by_country.get((country or "").upper(), fallback).upper()

    def vat_for(self, country: str | None) -> Decimal:
        """§10: 5 % inside the UAE, 0 % for exports (both settings). No country yet = domestic."""
        return self.vat_pct_domestic if (country or "AE").upper() == "AE" else self.vat_pct_export

    def escalate_after_minutes(self) -> int:
        """§16: the last reminder step escalates to the manager (120 alert-hour minutes by default)."""
        return self.reminder_minutes[-1]

    def alert_window(self) -> Window:
        """Manager alert hours; the approval timer only runs inside this window."""
        h = self.alert_hours
        return Window(h.start, h.end, h.tz or "Asia/Dubai")

    def buyer_window(self, tz: str) -> Window:
        """Buyer contact hours in the buyer's own time zone."""
        return Window(self.contact_hours.start, self.contact_hours.end, tz)

    def flag(self, key: str, default: bool = False) -> bool:
        return self.flags.get(key, default)


def parse_settings(raw: dict[str, Any], flags: dict[str, bool] | None = None) -> BusinessSettings:
    """Parse crm.settings rows (key -> jsonb value). Raises RuleViolation on an invalid table."""
    try:
        return BusinessSettings.model_validate({**raw, "flags": flags or {}})
    except ValidationError as exc:
        raise RuleViolation("invalid business settings", errors=exc.errors(include_url=False)) from exc


class SettingsService:
    """Process-wide cache of the settings snapshot (create once, share via CrmDeps)."""

    def __init__(self, ttl_seconds: float = 30.0, clock: Callable[[], float] = _time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._cached: tuple[float, dict[str, Any], BusinessSettings] | None = None

    def invalidate(self) -> None:
        self._cached = None

    async def get(self, session: AsyncSession) -> BusinessSettings:
        return (await self._load(session))[1]

    async def raw(self, session: AsyncSession) -> dict[str, Any]:
        return dict((await self._load(session))[0])

    async def _load(self, session: AsyncSession) -> tuple[dict[str, Any], BusinessSettings]:
        now = self._clock()
        if self._cached is not None and now - self._cached[0] < self._ttl:
            return self._cached[1], self._cached[2]
        raw = {k: v for k, v in (await session.execute(select(Setting.key, Setting.value))).tuples()}
        flags = {
            k: e for k, e in (await session.execute(select(FeatureFlag.key, FeatureFlag.enabled))).tuples()
        }
        parsed = parse_settings(raw, flags)
        self._cached = (now, raw, parsed)
        return raw, parsed

    async def update(self, session: AsyncSession, key: str, value: Any, actor: Actor) -> BusinessSettings:
        """Change one setting after validating the whole table with it; history + audit + event."""
        row = await session.get(Setting, key, with_for_update=True)
        if row is None:
            raise NotFound(f"unknown setting {key!r}")
        self.invalidate()
        raw = await self.raw(session)
        parsed = parse_settings({**raw, key: value})
        old = row.value
        row.value, row.updated_by = value, actor.user_id
        session.add(SettingHistory(key=key, old_value=old, new_value=value, changed_by=actor.user_id))
        await record(
            session,
            actor,
            "settings.changed",
            "setting",
            key,
            before={"value": old},
            after={"value": value},
            emit=Emit(
                ev.SETTINGS_CHANGED, "setting", key, SettingsChanged(key=key, changed_by=actor.user_id)
            ),
        )
        self.invalidate()
        return parsed

    async def set_flag(self, session: AsyncSession, key: str, enabled: bool, actor: Actor) -> None:
        """Kill switches (ai_enabled, ai_outbound_calls_uae ...)."""
        row = await session.get(FeatureFlag, key, with_for_update=True)
        if row is None:
            raise NotFound(f"unknown feature flag {key!r}")
        before = row.enabled
        row.enabled, row.updated_by = enabled, actor.user_id
        await record(
            session,
            actor,
            "flag.changed",
            "feature_flag",
            key,
            before={"enabled": before},
            after={"enabled": enabled},
            emit=Emit(
                ev.SETTINGS_CHANGED,
                "feature_flag",
                key,
                SettingsChanged(key=f"flag:{key}", changed_by=actor.user_id),
            ),
        )
        self.invalidate()
