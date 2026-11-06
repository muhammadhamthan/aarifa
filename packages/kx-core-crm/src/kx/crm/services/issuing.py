"""Releasing a quote to the buyer after a human approval: freeze the PDF, store it, grant one send,
tell automation. Runs after Approval 1 (first and revised quotes) and Approval 2 (final quote), both by the
deal's salesperson, the manager, or the MD on a takeover (D1); every buyer-facing version gets its own
single-use grant.

Order matters: the PDF is uploaded before the grant row commits. If the transaction then fails, an
orphan PDF is left in S3 (harmless, content-addressed key); the reverse order could leave a grant
pointing at a PDF that does not exist.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.event_payloads import QuoteApproved
from kx.core.identity import priced_content_sha256, sha256_hex
from kx.crm.deps import CrmDeps
from kx.crm.models import (
    Approval,
    Company,
    Contact,
    Deal,
    DealRequirement,
    DeliveryEstimateRow,
    Quote,
    QuoteLine,
    QuoteSendGrant,
    QuoteVersion,
    User,
)
from kx.crm.pdf import DocLine, QuoteDocument, SalesContact, pdf_key, quote_message
from kx.crm.services import deals as deal_svc
from kx.crm.services.grants import GrantPolicy, issue_grant
from kx.crm.services.journal import Actor, Emit, Timeline, record
from kx.crm.services.settings import BusinessSettings


def local_today(now: datetime, s: BusinessSettings) -> date:
    return now.astimezone(ZoneInfo(s.alert_hours.tz or "Asia/Dubai")).date()


async def lines_of(session: AsyncSession, version_id: object) -> list[QuoteLine]:
    stmt = select(QuoteLine).where(QuoteLine.quote_version_id == version_id).order_by(QuoteLine.line_no)
    return list((await session.execute(stmt)).scalars())


async def build_document(
    session: AsyncSession,
    deps: CrmDeps,
    *,
    quote: Quote,
    version: QuoteVersion,
    contact: Contact,
    issued_on: date,
) -> QuoteDocument:
    """Buyer-facing document from frozen rows only (no cost table is read here)."""
    lines = await lines_of(session, version.id)
    company = await session.get(Company, contact.company_id) if contact.company_id else None
    estimate = (
        await session.get(DeliveryEstimateRow, version.delivery_estimate_id)
        if version.delivery_estimate_id
        else None
    )
    assert version.valid_until is not None
    signer = await session.get(User, version.signed_by_user_id) if version.signed_by_user_id else None
    return QuoteDocument(
        number=quote.number,
        version_no=version.version_no,
        issued_on=issued_on,
        valid_until=version.valid_until,
        currency=version.currency,
        language=contact.language,
        buyer_name=contact.full_name or "",
        buyer_company=company.name if company else None,
        lines=tuple(
            DocLine(ln.line_no, ln.description, ln.quantity, ln.unit, ln.unit_price, ln.line_total)
            for ln in lines
        ),
        subtotal=version.subtotal,
        vat_rate=version.vat_rate,
        vat_amount=version.vat_amount,
        total=version.total,
        incoterm=version.incoterm,
        clauses=tuple(version.clauses),
        estimated_delivery=(estimate.confirmed_date or estimate.estimated_delivery) if estimate else None,
        letterhead=deps.letterhead,
        kind=version.kind,
        delivery_time_text=version.delivery_time_text,
        delivery_place=version.delivery_place,
        payment_terms_text=version.payment_terms_text,
        fx_rate=version.fx_rate,
        fx_rate_date=version.fx_rate_date,
        salesperson=None
        if signer is None
        else SalesContact(name=signer.full_name, phone=signer.phone_e164, email=str(signer.email)),
        cover_note=version.cover_note,
        change_summary=version.change_summary,
    )


async def _buyer_country(session: AsyncSession, deal: Deal) -> str | None:
    """§8: the delivery country picks the regional WhatsApp number."""
    return await session.scalar(
        select(DealRequirement.delivery_country)
        .where(DealRequirement.deal_id == deal.id, DealRequirement.delivery_country.is_not(None))
        .order_by(DealRequirement.created_at)
        .limit(1)
    )


async def issue_to_buyer(
    session: AsyncSession,
    deps: CrmDeps,
    s: BusinessSettings,
    *,
    deal: Deal,
    quote: Quote,
    version: QuoteVersion,
    approval: Approval,
    kind: Literal["commercial", "final"],
    actor: Actor,
    now: datetime,
    policy: GrantPolicy = "sales",
) -> QuoteSendGrant:
    """Approved: render + upload the PDF, freeze its hash, issue the single-use grant, emit QUOTE_APPROVED."""
    contact = await deal_svc.contact_of(session, deal)
    today = local_today(now, s)
    version.valid_until = today + timedelta(days=version.validity_days)
    doc = await build_document(session, deps, quote=quote, version=version, contact=contact, issued_on=today)
    pdf = await deps.renderer.pdf(doc)
    pdf_sha = sha256_hex(pdf)
    key = pdf_key(deps.company_code, quote.number, version.version_no, pdf_sha)
    await deps.uploader.upload(key, pdf)
    subject, body = quote_message(doc)
    sha = priced_content_sha256(body, pdf_sha)
    version.status, version.pdf_s3_key, version.content_sha256 = "approved", key, pdf_sha
    deal.low_ask_count = 0  # §12.2 rule 4 counts low asks per open negotiation: a new version starts one
    await session.flush()
    grant, channel, address = await issue_grant(
        session,
        version=version,
        approval_id=approval.id,
        policy=policy,
        contact=contact,
        sha=sha,
        s=s,
        now=now,
        actor=actor,
    )
    await record(
        session,
        actor,
        "quote.approved",
        "quote_version",
        version.id,
        after={"status": "approved", "pdf_s3_key": key, "pdf_sha256": pdf_sha, "grant_id": grant.id},
        timeline=Timeline(
            "quote_approved",
            f"{kind.title()} quote {quote.number} v{version.version_no} approved ({policy}); ready to send",
            deal.id,
            contact.id,
            ref_type="quote_version",
            ref_id=str(version.id),
        ),
        emit=Emit(
            ev.QUOTE_APPROVED,
            "quote",
            quote.id,
            QuoteApproved(
                approval_ref=approval.id,
                quote_ref=quote.id,
                quote_version_ref=version.id,
                deal_ref=deal.id,
                contact_ref=contact.id,
                kind=kind,
                grant_ref=grant.id,
                channel=channel,
                to_address=address,
                pdf_s3_key=key,
                body=body,
                subject=subject,
                content_sha256=sha,
                buyer_timezone=contact.timezone,
                buyer_country=await _buyer_country(session, deal),
                language=contact.language,
            ),
        ),
    )
    return grant
