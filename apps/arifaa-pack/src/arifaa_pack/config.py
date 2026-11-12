"""Client app settings (env prefix KX_APP_) + company.yaml. Platform settings stay in each package."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]  # types-PyYAML not in the lock yet
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

COMPANY_YAML = pathlib.Path(__file__).with_name("company.yaml")


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KX_APP_", extra="ignore")

    api_port: int = 8000
    media_bucket: str = "kx-local-media"  # quote PDFs, inbound media
    inbound_mail_bucket: str = "kx-local-inbound-mail"
    s3_kms_key_id: str | None = None
    cms_url: str = "http://localhost:3000"  # Payload CMS (KB source)
    cms_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    cors_origins: tuple[str, ...] = ("http://localhost:3001",)

    # ---- background jobs (D25: Celery 5.7 + Beat, Redis broker, PostgreSQL schedule table)
    celery_broker_url: str | None = None  # default: KX_REDIS_URL; prod: rediss://…/<db index> (infra secret)
    worker_concurrency: int = 10
    beat_sweep_seconds: float = 30.0  # Recommendation: Beat sweeps the schedule table every 30 s
    relay_seconds: float = 5.0  # outbox relays (chat replies must not wait a minute)
    celery_visibility_timeout_seconds: int = 7200  # above the longest task (OPEN_QUESTIONS #60)

    # ---- monitoring (D29: Better Stack via the Sentry SDK; Phoenix for AI traces)
    sentry_dsn: SecretStr | None = None  # Better Stack error-tracking DSN (Sentry SDK compatible)
    sentry_environment: str | None = None
    sentry_traces_sample_rate: float = 0.0
    betterstack_heartbeat_url: SecretStr | None = None  # pinged by the Beat heartbeat task
    phoenix_collector_endpoint: str | None = None  # http://phoenix.<ns>:6006/v1/traces
    phoenix_project: str = "pack"

    # ---- AI (D27: Claude API direct; Gemini backups off until the suite passes and Arifa agrees)
    gemini_api_key: SecretStr | None = None
    gemini_fallback_enabled: bool = False

    # ---- knowledge search (D28: Qwen3-Embedding-0.6B in our UAE container, full-text fallback)
    embedding_url: str | None = (
        None  # base URL of the OpenAI-compatible server (no /v1), e.g. http://embeddings
    )
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_api_key: SecretStr | None = None
    embedding_timeout_seconds: float = 2.0  # Fallback plan: "Embedding call fails or > 2 s" -> full-text

    # ---- website (§6, D39)
    turnstile_secret_key: SecretStr | None = None
    livekit_url: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: SecretStr | None = None
    cms_webhook_secret: SecretStr | None = None  # HMAC Payload signs publish webhooks with (§7, D22)

    # ---- vendors and caps (D30, D34)
    vendor_order_ssm_prefix: str | None = None  # /kx/<env>/<company>/vendors (Parameter Store)
    spend_ai_usd_per_day: Decimal = Decimal(25)  # defaults = Heavy column / 30 (OPEN_QUESTIONS #52)
    spend_voice_minutes_per_day: Decimal = Decimal(840)
    spend_wa_templates_per_day: Decimal = Decimal(870)

    # ---- quote PDFs (D37: headless Chromium, WeasyPrint backup)
    pdf_renderer: Literal["chromium", "weasyprint"] = "chromium"  # D37 primary engine
    chromium_path: str = "/usr/bin/chromium"
    google_credentials_json: SecretStr | None = None  # Chirp TTS backup: written to /tmp at voice start


@dataclass(frozen=True, slots=True)
class Company:
    code: str
    name: str
    letterhead: dict[str, str]
    languages: tuple[str, ...]


@lru_cache(maxsize=1)
def app_settings() -> AppSettings:
    return AppSettings()


@lru_cache(maxsize=1)
def company() -> Company:
    raw: dict[str, Any] = yaml.safe_load(COMPANY_YAML.read_text(encoding="utf-8"))
    return Company(
        code=raw["code"],
        name=raw["name"],
        letterhead=raw.get("letterhead", {}),
        languages=tuple(raw.get("languages", ("en",))),
    )
