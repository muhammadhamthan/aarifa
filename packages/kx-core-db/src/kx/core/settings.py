"""Deploy-time settings (env vars / Secrets Manager). Business rules live in crm.settings, not here."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import BaseModel, Field, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# str, not PostgresDsn: unix-socket DSNs (host=/path) are valid for asyncpg but fail URL host checks.
AsyncPgDsn = Annotated[str, Field(pattern=r"^postgresql\+asyncpg://")]


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogicalDb(StrEnum):
    """The four logical databases on one RDS instance (CMS is owned by Payload, not Python)."""

    CRM = "crm"
    AUTOMATION = "automation"
    AGENTS = "agents"


class AuthSettings(BaseModel):
    """In-house staff login (OPEN_QUESTIONS #50 for the values the documents do not give)."""

    access_ttl_seconds: int = Field(default=900, ge=60)  # short-lived access tokens (Recommendation)
    refresh_ttl_seconds: int = Field(default=43_200, ge=600)  # 12 h sliding, rotated on every use
    max_failed_attempts: int = Field(default=5, ge=1)  # then locked
    lockout_minutes: int = Field(default=15, ge=1)
    code_ttl_seconds: int = Field(default=600, ge=60)  # email codes and login challenges
    code_max_tries: int = Field(default=5, ge=1)
    encryption_key: SecretStr | None = None  # Fernet key for TOTP seeds (KX_AUTH__ENCRYPTION_KEY)
    cookie_secure: bool = True
    cookie_domain: str | None = None
    totp_issuer: str = "Kairox CRM"


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KX_", env_nested_delimiter="__", extra="ignore")

    env: Environment = Environment.LOCAL
    company_code: str = Field(default="pack", pattern=r"^[a-z][a-z0-9_]{1,20}$")
    company_timezone: str = "Asia/Dubai"
    base_currency: str = Field(default="AED", min_length=3, max_length=3)

    crm_dsn: AsyncPgDsn = "postgresql+asyncpg://crm_app:dev@localhost:5432/crm"
    automation_dsn: AsyncPgDsn = "postgresql+asyncpg://automation_app:dev@localhost:5432/automation"
    agents_dsn: AsyncPgDsn = "postgresql+asyncpg://agents_app:dev@localhost:5432/agents"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_statement_timeout_ms: int = 15_000

    redis_url: RedisDsn = RedisDsn("redis://localhost:6379/0")

    # Staff login (11 Nov, D26: in-house, RS256 tokens; Cognito removed)
    jwt_issuer: str = "http://localhost:8000"
    jwt_audience: str = "kx-crm"
    jwt_private_key: SecretStr | None = None  # RSA PEM (Secrets Manager); a dev key is generated if unset
    jwt_key_id: str = "kx-dev-1"
    jwks_url: str | None = None  # only to verify tokens from another issuer; default: our own public key
    auth: AuthSettings = AuthSettings()
    service_token_secret: SecretStr = SecretStr("dev-only-change-me")  # signs website + unsubscribe tokens

    aws_region: str = "me-central-1"
    s3_bucket: str = "kx-local"
    log_level: str = "INFO"
    log_json: bool = True

    def dsn_for(self, db: LogicalDb) -> str:
        return str(
            {
                LogicalDb.CRM: self.crm_dsn,
                LogicalDb.AUTOMATION: self.automation_dsn,
                LogicalDb.AGENTS: self.agents_dsn,
            }[db]
        )


@lru_cache(maxsize=1)
def get_settings() -> CoreSettings:
    return CoreSettings()
