"""Monitoring wiring (11 Nov, D29): Better Stack for errors (via the Sentry SDK), the Beat heartbeat, and the
queue-lag metric. AI traces go to Phoenix (kx.agents.tracing), SQL to CloudWatch Database Insights.

PII never leaves AWS unmasked (Recommendation: "Logs must be PII-masked before they leave AWS"): the Sentry
`before_send` hook masks every string with `kx.core.logging.mask_pii`, and request bodies are not sent.
"""

from __future__ import annotations

from typing import Any

import httpx

from arifaa_pack.config import AppSettings
from kx.core.logging import get_logger, mask_pii
from kx.core.metrics import emit_metric

log = get_logger(__name__)


def _mask(value: Any) -> Any:
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_mask(v) for v in value]
    return value


def before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Mask phones and e-mails anywhere in the error event; drop request bodies and cookies."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
    masked: dict[str, Any] = _mask(event)
    return masked


def init_error_tracking(s: AppSettings, environment: str, component: str) -> bool:
    """Sentry SDK pointed at Better Stack error tracking ("Sentry SDK-compatible"). No-op without a DSN."""
    if s.sentry_dsn is None:
        return False
    import sentry_sdk  # noqa: PLC0415 - only when configured

    sentry_sdk.init(
        dsn=s.sentry_dsn.get_secret_value(),
        environment=s.sentry_environment or environment,
        traces_sample_rate=s.sentry_traces_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
        before_send=before_send,  # type: ignore[arg-type]
        server_name=component,
    )
    return True


async def heartbeat(http: httpx.AsyncClient, s: AppSettings, company: str, lag_seconds: float) -> None:
    """Beat's 30-second heartbeat: ping Better Stack (a missing ping for 2 minutes pages on-call, Fallback
    plan "Background jobs") and publish `Kairox/QueueLagSeconds`. Runs on a worker, so a ping proves Beat,
    the broker and a worker are all alive."""
    emit_metric("QueueLagSeconds", lag_seconds, company=company, unit="Seconds")
    if s.betterstack_heartbeat_url is None:
        return
    try:
        (await http.get(s.betterstack_heartbeat_url.get_secret_value(), timeout=5.0)).raise_for_status()
    except httpx.HTTPError:
        log.warning("heartbeat.ping_failed", exc_info=True)  # Better Stack itself will page on the gap
