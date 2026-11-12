"""§8.1 step 10: "Every email has a one-click unsubscribe link; the reason field on that page is optional."

* Every buyer email carries `List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click`
  (RFC 8058). Mail clients show their own "Unsubscribe" button and POST to the link: one click, no page.
  The headers sit outside the body, so a priced email's grant hash is untouched.
* Opening the link in a browser shows a page with one button and an optional reason box. A GET never
  unsubscribes by itself: mail scanners fetch links, and a scanner must not opt a buyer out.
* The token is the address hash signed with the service secret (no address, nothing to guess).

An unsubscribe is a full stop on every channel (§8 "Opt-out words (STOP, unsubscribe ...)"; step 2 "Stop at
once, everywhere"); OPEN_QUESTIONS #61. No message is sent afterwards: the page is the confirmation (#43).
"""

# No `from __future__ import annotations` here: FastAPI resolves the closure-local annotations.

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, Request, Response

from kx.automation import optout
from kx.automation.settings import AutomationSettings
from kx.core.db import Databases
from kx.core.identity import constant_time_equals, hmac_sha256_hex, recipient_hash
from kx.core.settings import LogicalDb

PATH: Final = "/v1/public/unsubscribe"
_HTML: Final = "text/html; charset=utf-8"


def make_token(secret: str, address_hash: str) -> str:
    sig = hmac_sha256_hex(secret.encode(), f"unsubscribe:{address_hash}".encode())[:32]
    return f"{address_hash}.{sig}"


def read_token(secret: str, token: str) -> str | None:
    address_hash, _, sig = token.partition(".")
    if len(address_hash) != 64 or not sig:
        return None
    expected = make_token(secret, address_hash).partition(".")[2]
    return address_hash if constant_time_equals(sig, expected) else None


@dataclass(frozen=True, slots=True)
class UnsubscribeLinks:
    """address -> the one-click link for that buyer email (injected into OutboundService)."""

    secret: str
    public_base_url: str

    def __call__(self, email_address: str) -> str:
        token = make_token(self.secret, recipient_hash("email", email_address))
        return f"{self.public_base_url.rstrip('/')}{PATH}?t={quote(token)}"


@dataclass(frozen=True, slots=True)
class UnsubscribeDeps:
    databases: Databases
    settings: AutomationSettings
    secret: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def _page(company: str, body: str) -> Response:
    name = html.escape(company or "our")
    doc = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{name} emails</title><style>body{{font-family:system-ui,sans-serif;max-width:32rem;"
        "margin:3rem auto;padding:0 1rem;line-height:1.5}textarea{width:100%}button{padding:.6rem 1.2rem}"
        f"</style></head><body>{body}</body></html>"
    )
    return Response(doc, media_type=_HTML)


def build_router(deps: UnsubscribeDeps) -> APIRouter:
    router = APIRouter(tags=["public"], include_in_schema=True)
    company = deps.settings.company_name

    @router.get(PATH, include_in_schema=False)
    async def unsubscribe_page(t: str = "") -> Response:
        """The page behind the link: one button, an optional reason (a GET changes nothing)."""
        if read_token(deps.secret, t) is None:
            return _page(company, "<p>This unsubscribe link is not valid.</p>")
        token = html.escape(t, quote=True)
        return _page(
            company,
            f"<h1>Unsubscribe from {html.escape(company or 'our')} messages</h1>"
            "<p>This stops our emails, WhatsApp messages and calls.</p>"  # OPEN_QUESTIONS #62
            f"<form method='post' action='{PATH}?t={token}'>"
            "<label for='reason'>Reason (optional)</label>"
            "<textarea id='reason' name='reason' rows='3' maxlength='500'></textarea>"
            "<p><button type='submit'>Unsubscribe</button></p></form>",
        )

    @router.post(PATH, summary="One-click unsubscribe (RFC 8058) and the unsubscribe page's form")
    async def unsubscribe(request: Request, t: str = "") -> Response:
        """Stops every channel at once (§8.1 step 2) and records the optional reason (step 4)."""
        address_hash = read_token(deps.secret, t)
        if address_hash is None:
            return Response("invalid link", status_code=400)
        form = dict(parse_qsl((await request.body()).decode(errors="replace"), keep_blank_values=True))
        reason = (form.get("reason") or "").strip()[:500] or None
        async with deps.databases.transaction(LogicalDb.AUTOMATION) as s:
            await optout.apply_stop(
                s,
                optout.StopRecord(
                    scope="all",
                    source="unsubscribe_link",
                    channel="email",
                    address_hashes=(address_hash,),
                    contact_ref=None,
                    deal_ref=None,
                    thread_ref=None,
                    words="unsubscribe (email link)",
                    reason=reason,
                    decided_by="rule:list_unsubscribe",
                    language=None,
                    at=deps.clock(),
                ),
            )
        name = html.escape(company or "our")
        return _page(
            company,
            f"<h1>You're unsubscribed</h1><p>You won't receive messages from {name} again. "
            "To restart, message us any time.</p>",
        )

    return router
