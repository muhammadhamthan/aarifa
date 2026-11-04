"""Client app settings (env prefix KX_APP_) + company.yaml. Platform settings stay in each package."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

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
    bedrock_region: str | None = None  # set to enable Bedrock as primary
    bedrock_model_ids: dict[str, str] = {}  # tier -> Bedrock model / inference-profile id
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"  # 1024-dim (kb_chunks vector(1024))
    embedding_region: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str | None = None
    sentry_dsn: SecretStr | None = None
    cors_origins: tuple[str, ...] = ("http://localhost:3001",)
    worker_concurrency: int = 10


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
