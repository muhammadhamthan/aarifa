"""§6.5 / §8.2 / §17 row 1: the Requirement Summary PDF sent when the deal is qualified (D19).

"At the end of qualification the AI sends a one-page PDF to WhatsApp (or email) listing the products,
quantities and specifications; delivery country and city; needed-by date; next steps. It never includes
prices. Sending it confirms the phone number." The CRM renders and stores it; automation sends it (an
approved WhatsApp template with a document header, since the buyer usually has not written to WhatsApp
yet; else email).

If it cannot be delivered: "If the buyer has left, the summary goes by email (if we have it). Otherwise the
salesperson is told." (§8.2 step 4).
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.event_payloads import RequirementSummary
from kx.core.identity import phone_kind, sha256_hex
from kx.crm.deps import CrmDeps
from kx.crm.models import Company, Contact, Deal, DealRequirement, Product
from kx.crm.pdf import SummaryDocument, SummaryLine, summary_caption, summary_key, summary_phrase
from kx.crm.services import deals as deal_svc
from kx.crm.services import phone
from kx.crm.services.issuing import local_today
from kx.crm.services.journal import Actor, Emit, Timeline, record

Route = Literal["whatsapp", "email"]


async def build_summary(
    session: AsyncSession, deps: CrmDeps, deal: Deal, contact: Contact
) -> SummaryDocument:
    rows = (
        await session.execute(
            select(DealRequirement, Product.name, Product.sku)
            .outerjoin(Product, Product.id == DealRequirement.product_id)
            .where(DealRequirement.deal_id == deal.id)
            .order_by(DealRequirement.created_at)
        )
    ).all()
    company = await session.get(Company, contact.company_id) if contact.company_id else None
    first = rows[0][0] if rows else None
    settings = await deps.settings.get(session)
    return SummaryDocument(
        reference=deal.number,
        issued_on=local_today(deps.now(), settings),
        language=contact.language,
        buyer_name=contact.full_name or "",
        buyer_company=company.name if company else None,
        lines=tuple(
            SummaryLine(
                description=name or req.product_text or "",
                code=sku,
                quantity=req.quantity,
                unit=req.unit,
                spec=tuple((str(k), str(v)) for k, v in (req.spec or {}).items() if v not in (None, "")),
            )
            for req, name, sku in rows
        ),
        delivery_country=first.delivery_country if first else None,
        delivery_city=first.delivery_city if first else None,
        needed_by=first.needed_by if first else None,
        letterhead=deps.letterhead,
    )


def _whatsapp_number(contact: Contact) -> str | None:
    number = contact.whatsapp_e164 or contact.phone_e164
    return number if number and phone_kind(number) != "landline" else None


async def send_requirement_summary(
    session: AsyncSession, deps: CrmDeps, deal: Deal, *, only: Route | None = None
) -> bool:
    """Render, store and announce the summary. False when there is no address: the salesperson is told."""
    contact = await deal_svc.contact_of(session, deal)
    whatsapp = _whatsapp_number(contact) if only in (None, "whatsapp") else None
    email = contact.email if only in (None, "email") else None
    if whatsapp is None and email is None:
        await phone.tell_salesperson(
            session, deal, "no WhatsApp number or email to send the Requirement Summary to"
        )
        return False
    doc = await build_summary(session, deps, deal, contact)
    pdf = await deps.renderer.summary_pdf(doc)
    key = summary_key(deps.company_code, deal.number, sha256_hex(pdf))
    await deps.uploader.upload(key, pdf)
    where = "WhatsApp" if whatsapp else "email"
    await record(
        session,
        Actor.system("requirement_summary"),
        "deal.requirement_summary",
        "deal",
        deal.id,
        after={"pdf_s3_key": key, "route": where},
        timeline=Timeline(
            "summary", f"Requirement Summary sent to the buyer's {where} (no prices)", deal.id, contact.id
        ),
        emit=Emit(
            ev.DEAL_REQUIREMENT_SUMMARY,
            "deal",
            deal.id,
            RequirementSummary(
                deal_ref=deal.id,
                contact_ref=contact.id,
                deal_number=deal.number,
                pdf_s3_key=key,
                caption=summary_caption(doc),
                summary_text=summary_phrase(doc),
                buyer_name=contact.full_name,
                whatsapp=whatsapp,
                email=email,
                language=contact.language,
                buyer_country=doc.delivery_country,
            ),
        ),
    )
    return True


async def summary_not_delivered(session: AsyncSession, deps: CrmDeps, deal: Deal) -> None:
    """The WhatsApp summary failed: email if we have it, else the salesperson is told."""
    contact = await deal_svc.contact_of(session, deal)
    if contact.email:
        await send_requirement_summary(session, deps, deal, only="email")
    else:
        await phone.tell_salesperson(session, deal, "the Requirement Summary did not reach WhatsApp")
