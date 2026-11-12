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
from typing import Final, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.event_payloads import QuoteApproved, QuoteNotDelivered, QuoteResendRequested
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
from kx.crm.services import phone, staff
from kx.crm.services.grants import GrantPolicy, SendChannel, issue_grant, recipient
from kx.crm.services.journal import Actor, Emit, Timeline, record
from kx.crm.services.settings import BusinessSettings

_ISSUED: Final = ("approved", "sent")  # = quotes.ISSUED (quotes imports this module)


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


async def _unconfirmed_number_route(
    session: AsyncSession, deal: Deal, contact: Contact
) -> SendChannel | None:
    """§8.2 step 6: "Quotes go only to numbers that are Reachable or better. For Not reached, the
    salesperson decides." A WhatsApp number not yet confirmed: the quote goes by email when we have one;
    otherwise to WhatsApp as the approving salesperson decided (the approval screen shows the status), with a
    task to check the number (OPEN_QUESTIONS #46)."""
    preferred, _ = recipient(contact)
    if preferred != "whatsapp" or phone.reachable(contact):
        return None
    if contact.email:
        await deal_svc.add_note(
            session,
            deal,
            "phone",
            "WhatsApp number not confirmed yet: this quote goes by email (§8.2)",
            Actor.system("phone_status"),
        )
        return "email"
    await phone.tell_salesperson(
        session, deal, "the WhatsApp number is not confirmed; the quote was sent there"
    )
    return None


async def buyer_country(session: AsyncSession, deal: Deal) -> str | None:
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
    salesperson = manager = None
    if kind == "final":  # §14.5 / §17: alert the salesperson, notify the manager
        owner = await staff.approver_for(session, deal)
        salesperson, manager = (
            staff.staff_contact(owner),
            staff.staff_contact(await staff.manager_user(session)),
        )
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
        actor=actor,
        channel_override=await _unconfirmed_number_route(session, deal, contact),
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
                buyer_country=await buyer_country(session, deal),
                language=contact.language,
                salesperson=salesperson,
                manager=manager,
            ),
        ),
    )
    return grant


async def resend_by_email(session: AsyncSession, deps: CrmDeps, p: QuoteNotDelivered) -> bool:
    """D32 "A WhatsApp quote not delivered in 10 minutes goes by email with the same approved quote (its own
    single-use grant for the email channel) -> the salesperson is told to call". The grant binds the exact
    content hash automation reported; the email address comes from the contact record. True when re-sent."""
    old = await session.get(QuoteSendGrant, p.grant_ref)
    if old is None or old.revoked_at is not None or p.deal_ref is None:
        return False
    version = await session.get(QuoteVersion, old.quote_version_id)
    deal = await session.get(Deal, p.deal_ref)
    if version is None or deal is None or version.status not in _ISSUED:
        return False  # superseded meanwhile: the newer version is what the buyer should get
    contact = await deal_svc.contact_of(session, deal)
    if not contact.email:
        await phone.tell_salesperson(
            session, deal, "the WhatsApp quote was not delivered and there is no email"
        )
        return False
    s = await deps.settings.get(session)
    actor = Actor.system("quote_delivery")
    grant, _, address = await issue_grant(
        session,
        version=version,
        approval_id=old.approval_id,
        policy=cast("GrantPolicy", old.policy),
        contact=contact,
        sha=p.content_sha256,
        s=s,
        actor=actor,
        channel_override="email",
    )
    await record(
        session,
        actor,
        "quote.resent_by_email",
        "quote_version",
        version.id,
        after={"grant_id": grant.id, "undelivered_message": p.message_ref},
        timeline=Timeline(
            "quote_resent",
            "The WhatsApp quote was not delivered in time; the same quote was sent by email",
            deal.id,
            contact.id,
            ref_type="quote_version",
            ref_id=str(version.id),
        ),
        emit=Emit(
            ev.QUOTE_RESEND_REQUESTED,
            "quote_version",
            version.id,
            QuoteResendRequested(
                grant_ref=grant.id,
                deal_ref=deal.id,
                contact_ref=contact.id,
                kind=p.kind,
                channel="email",
                to_address=address,
                body=p.body,
                subject=p.subject,
                pdf_s3_key=p.pdf_s3_key,
                content_sha256=p.content_sha256,
                buyer_timezone=contact.timezone,
                language=contact.language,
            ),
        ),
    )
    return True
