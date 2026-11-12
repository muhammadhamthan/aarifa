"""D33 email field: SES -> Postmark (Gmail API slot off, OPEN_QUESTIONS #54), behind one `send()`.

`OutboundService` and the staff-code mailer call this like a single sender. The order is the field's vendor
order (Parameter Store, D30); a vendor whose breaker is open is skipped; each switch writes the
`Kairox/VendorSwitch` metric. When every vendor fails, the last error is raised as a `ChannelError` so the
delivery job keeps its retry / give-up rules (and the CRM's own fallbacks take over: WhatsApp, the task).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from kx.automation.channels import ChannelError
from kx.automation.channels.email import Attachment
from kx.core.fallback import AllVendorsFailed, FallbackChain


class EmailVendor(Protocol):
    async def send(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
        attachment: Attachment | None = None,
        in_reply_to: str | None = None,
        list_unsubscribe: str | None = None,
        from_name: str | None = None,
    ) -> str: ...


class EmailChain:
    def __init__(
        self,
        vendors: Mapping[str, EmailVendor],
        chain: FallbackChain[str],
        order: Callable[[], Awaitable[Sequence[str]]],
    ) -> None:
        self._vendors = dict(vendors)
        self._chain = chain
        self._order = order

    async def send(self, **kwargs: Any) -> str:
        order = [v for v in await self._order() if v in self._vendors]
        retryable = False

        async def call(name: str) -> str:
            nonlocal retryable
            try:
                return await self._vendors[name].send(**kwargs)
            except ChannelError as exc:
                retryable = retryable or exc.retryable
                raise

        try:
            _, message_id = await self._chain.run(order, call)
        except AllVendorsFailed as exc:
            raise ChannelError("email", str(exc.errors)[:300], retryable=retryable) from exc
        return message_id
