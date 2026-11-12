"""D30 "every field has primary -> backup -> last resort" (Fallback plan, "How switching works").

* `CircuitBreaker`: "a circuit breaker skips a vendor for 5 minutes after 3 failures in 60 seconds, then one
  test call decides". In-process (each worker learns on its own; no shared state to fail).
* `FallbackChain`: tries the vendors of one field in order, skipping open breakers; every switch writes the
  `VendorSwitch` metric (namespace `Kairox`; the CloudWatch alarm pages on-call) and calls the optional
  alert hook. The order comes from the caller (Parameter Store via `VendorOrder`), so it changes without
  a deploy.

The safety gates (price guard, approval gate, consent, quiet hours) are not here: they run after every switch
because they sit in front of every vendor (`OutboundService`, `PriceGuard`), whichever one answers.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from kx.core.logging import get_logger
from kx.core.metrics import emit_metric

log = get_logger(__name__)
FAILURES: Final = 3
WINDOW_SECONDS: Final = 60.0
OPEN_SECONDS: Final = 300.0


class AllVendorsFailed(Exception):
    """Every vendor of the field failed or is cooling down: the caller takes its last resort."""

    def __init__(self, field_name: str, errors: dict[str, str]) -> None:
        super().__init__(f"{field_name}: every vendor failed ({errors})")
        self.field_name = field_name
        self.errors = errors


@dataclass
class CircuitBreaker:
    failures: int = FAILURES
    window_seconds: float = WINDOW_SECONDS
    open_seconds: float = OPEN_SECONDS
    clock: Callable[[], float] = time.monotonic
    _recent: deque[float] = field(default_factory=deque)
    _opened_at: float | None = None
    _trial_running: bool = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self._opened_at >= self.open_seconds else "open"

    def allow(self) -> bool:
        """True when a call may go: closed, or the single trial call after the cool-down."""
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._trial_running:
            self._trial_running = True
            return True
        return False

    def release(self) -> None:
        """The call ended for a reason that says nothing about the vendor (our own bug): free the trial."""
        self._trial_running = False

    def success(self) -> None:
        self._recent.clear()
        self._opened_at, self._trial_running = None, False

    def failure(self) -> None:
        now = self.clock()
        if self._trial_running:  # the test call failed: cool down again
            self._opened_at, self._trial_running = now, False
            return
        self._recent.append(now)
        while self._recent and now - self._recent[0] > self.window_seconds:
            self._recent.popleft()
        if len(self._recent) >= self.failures:
            self._opened_at = now
            self._recent.clear()
            log.warning("circuit.open", open_seconds=self.open_seconds)


Alert = Callable[[str, str, str], Awaitable[None]]  # field, from vendor, to vendor


@dataclass
class FallbackChain[T]:
    """One field's vendors in order. `call(vendor)` does the work; an exception moves to the next vendor."""

    field_name: str
    company: str
    alert: Alert | None = None
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)

    def breaker(self, vendor: str) -> CircuitBreaker:
        return self.breakers.setdefault(vendor, CircuitBreaker())

    async def run(self, order: Sequence[str], call: Callable[[str], Awaitable[T]]) -> tuple[str, T]:
        errors: dict[str, str] = {}
        first = order[0] if order else ""
        for vendor in order:
            breaker = self.breaker(vendor)
            if not breaker.allow():
                errors[vendor] = "circuit open"
                continue
            try:
                result = await call(vendor)
            except Exception as exc:
                breaker.failure()
                errors[vendor] = type(exc).__name__
                log.warning(
                    "fallback.vendor_failed", field=self.field_name, vendor=vendor, error=errors[vendor]
                )
                continue
            breaker.success()
            if vendor != first:
                await self.switched(first, vendor)
            return vendor, result
        await self.switched(first, "last_resort")
        raise AllVendorsFailed(self.field_name, errors)

    async def switched(self, from_vendor: str, to_vendor: str) -> None:
        emit_metric(
            "VendorSwitch",
            1,
            company=self.company,
            field=self.field_name,
            from_vendor=from_vendor,
            to_vendor=to_vendor,
        )
        log.warning("fallback.switched", field=self.field_name, from_vendor=from_vendor, to_vendor=to_vendor)
        if self.alert is not None:
            await self.alert(self.field_name, from_vendor, to_vendor)
