"""Change requests (§13, D12): anything the buyer asks that the AI may not decide goes to the salesperson."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kx.crm.models.base import TOUCHED, TZ, Base, created_at, ref, updated_at, uuid_pk


class ChangeRequest(Base):
    """One card on the deal: the buyer's exact words, the table of what changes, an SLA timer.
    Status: open -> in_progress -> resolved (a new version sent) | declined (with the reason)."""

    __tablename__ = "change_requests"
    __mapper_args__ = TOUCHED

    id: Mapped[uuid.UUID] = uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(ref("deals.id"))
    quote_version_id: Mapped[uuid.UUID | None] = mapped_column(ref("quote_versions.id"))
    type: Mapped[str] = mapped_column(Text)  # product|spec|printing|quantity|delivery|samples|payment|other
    buyer_text: Mapped[str] = mapped_column(Text)
    buyer_text_en: Mapped[str | None] = mapped_column(Text)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    ai_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="open")
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    sla_due_at: Mapped[datetime] = mapped_column(TZ)
    escalated_at: Mapped[datetime | None] = mapped_column(TZ)
    alert_ref: Mapped[uuid.UUID] = mapped_column()  # the current reminder ladder (new on reassignment)
    resolved_version_id: Mapped[uuid.UUID | None] = mapped_column(ref("quote_versions.id"))
    decline_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    closed_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
