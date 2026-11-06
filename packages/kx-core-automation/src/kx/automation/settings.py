"""Deploy-time settings for automation (env `KX_AUTO_*`, nested with `__`, e.g. KX_AUTO_WHATSAPP__APP_SECRET).

Business rules that the MD edits (approval window length, MD threshold ...) live in the CRM; what is
here are channel credentials and the platform-wide contact-hour rules from CONTRACTS.md §3.
"""

from __future__ import annotations

import uuid
from functools import cached_property, lru_cache

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from kx.core.timewindow import Window


class _Group(BaseModel):
    model_config = ConfigDict(frozen=True)


class WhatsAppSettings(_Group):
    graph_base_url: str = "https://graph.facebook.com"
    graph_version: str = "v23.0"
    phone_number_id: str = ""
    access_token: SecretStr = SecretStr("")
    app_secret: SecretStr = SecretStr("")
    verify_token: SecretStr = SecretStr("")
    template_language: str = "en"
    session_hours: int = 24  # Meta customer-service window: free-form messages only inside it


class SesSettings(_Group):
    from_address: str = "sales@example.com"
    configuration_set: str | None = None
    # Only these SNS topics may feed /webhooks/ses. SNS signs messages from *any* topic, so a valid
    # signature alone does not prove the event is ours.
    sns_topic_arns: frozenset[str] = frozenset()
    inbound_bucket: str = ""  # S3 bucket SES Mail Manager writes inbound MIME to


class TwilioSettings(_Group):
    api_base_url: str = "https://api.twilio.com"
    account_sid: str = ""
    auth_token: SecretStr = SecretStr("")
    from_number: str = ""
    # Public https base that Twilio calls back (also the URL it signs): https://api.arifapack.com
    status_callback_base_url: str = "http://localhost:8000"
    ring_timeout_seconds: int = 30
    voice: str = "Polly.Joanna"
    language: str = "en-US"


class AlertSettings(_Group):
    """Staff alerts, only 08:00-22:00 Dubai (§16). The reminder times (30 / 60 / 120 alert-hour minutes)
    come from the CRM setting `approval.reminder_minutes`, inside each QuoteSubmitted / ChangeRequested."""

    window_start: str = "08:00"
    window_end: str = "22:00"
    timezone: str = "Asia/Dubai"
    approval_request_template: str = "approval_request"  # 0 min: WhatsApp to the approver
    approval_reminder_template: str = "approval_reminder"  # 30 min: WhatsApp reminder
    change_request_template: str = "change_request"  # 0 min for a change request
    escalation_template: str = "approval_escalation"  # 120 min: WhatsApp to the manager (+ call)
    negotiation_paused_template: str = "negotiation_paused"
    notice_template: str = "staff_notice"  # one-line notices (manager notified, takeover, handover ...)


class FollowUpSettings(_Group):
    """Buyer follow-ups: 09:00-18:00 buyer local time, at most `max_per_week` in any rolling 7 days."""

    window_start: str = "09:00"
    window_end: str = "18:00"
    max_per_week: int = Field(default=2, ge=1)
    default_timezone: str = "Asia/Dubai"
    templates: dict[str, str] = Field(
        default_factory=lambda: {
            "followup_d2": "followup_1",
            "followup_d5": "followup_2",
            "followup_d10": "followup_3",
            "reorder": "reorder_reminder",
            "custom": "followup_1",
        }
    )


class RateLimitSettings(_Group):
    """Token buckets: protect buyers from loops (per recipient) and providers from bursts (per channel)."""

    recipient_capacity: int = 6
    recipient_refill_per_minute: float = 2.0
    channel_capacity: int = 60
    channel_refill_per_minute: float = 600.0


class PriceSourceConfig(_Group):
    """One official page publishing a raw-material price. Results are *proposals*, never prices."""

    code: str = Field(pattern=r"^[a-z0-9_]+$")
    source_ref: uuid.UUID  # crm.price_sources.id
    url: HttpUrl
    material_code: str
    price_selector: str  # CSS selector for the element holding the number
    currency: str = Field(min_length=3, max_length=3)
    unit: str
    date_selector: str | None = None  # element holding the date the source states (ISO)
    terms_ok: bool = False  # scraping allowed by the site's terms (checked by a human)


class AutomationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KX_AUTO_", env_nested_delimiter="__", extra="ignore", frozen=True
    )

    whatsapp: WhatsAppSettings = WhatsAppSettings()
    ses: SesSettings = SesSettings()
    twilio: TwilioSettings = TwilioSettings()
    alerts: AlertSettings = AlertSettings()
    followup: FollowUpSettings = FollowUpSettings()
    rate_limit: RateLimitSettings = RateLimitSettings()
    price_sources: tuple[PriceSourceConfig, ...] = ()
    opt_out_keywords: frozenset[str] = frozenset({"stop", "unsubscribe", "stop all", "إيقاف", "الغاء"})
    http_timeout_seconds: float = 15.0

    @cached_property
    def alert_window(self) -> Window:
        return Window.parse(self.alerts.window_start, self.alerts.window_end, self.alerts.timezone)

    def followup_window(self, buyer_timezone: str | None) -> Window:
        tz = buyer_timezone or self.followup.default_timezone
        return Window.parse(self.followup.window_start, self.followup.window_end, tz)


@lru_cache(maxsize=1)
def get_automation_settings() -> AutomationSettings:
    return AutomationSettings()
