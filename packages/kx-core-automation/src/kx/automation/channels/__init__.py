"""Channel adapters (WhatsApp Cloud API, SES email, Twilio IVR) and their shared HTTP retry policy.

Retries are for transport problems only (429, 5xx, timeouts, connection errors). A 4xx means the
request itself is wrong; repeating it would only repeat the error, so it fails at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter
from tenacity.wait import wait_base


class ChannelError(Exception):
    """A provider refused or failed a request. `retryable` tells the delivery job whether to try again."""

    def __init__(self, provider: str, detail: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(f"{provider}: {detail}")
        self.provider = provider
        self.detail = detail
        self.retryable = retryable
        self.status = status


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)  # timeouts, connect/read errors


@dataclass(frozen=True, slots=True)
class HttpRetry:
    attempts: int = 3
    wait: wait_base = field(default_factory=lambda: wait_exponential_jitter(initial=0.5, max=8))

    async def request(
        self, client: httpx.AsyncClient, provider: str, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """Send with retries on transient errors; raise ChannelError with the final outcome."""
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.attempts),
                wait=self.wait,
                retry=retry_if_exception(is_transient),
                reraise=True,
            ):
                with attempt:
                    response = await client.request(method, url, **kwargs)
                    response.raise_for_status()
                    return response
        except httpx.HTTPStatusError as exc:
            raise ChannelError(
                provider,
                _error_detail(exc.response),
                retryable=is_transient(exc),
                status=exc.response.status_code,
            ) from exc
        except httpx.TransportError as exc:
            raise ChannelError(provider, type(exc).__name__, retryable=True) from exc
        raise AssertionError("unreachable")  # pragma: no cover - AsyncRetrying always returns or raises


def _error_detail(response: httpx.Response) -> str:
    """Provider error text without echoing request data (bodies can contain buyer content)."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = body.get("error", body) if isinstance(body, dict) else {}
    if isinstance(error, dict):
        code = error.get("code", "")
        message = error.get("message", "")
        return f"HTTP {response.status_code} {code} {message}".strip()
    return f"HTTP {response.status_code}"
