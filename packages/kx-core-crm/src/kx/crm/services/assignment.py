"""Deal assignment (docs/BUSINESS_FLOW_V1.md §9, §3, §24; D5).

1. Default: round-robin across active salespersons.
2. Optional rules (setting `assignment.rules`, off by default), checked in the order listed: the
   salesperson's languages contain the buyer's language; their countries contain the delivery country;
   their categories contain the product category. Each rule narrows the pool; a rule nobody matches is
   skipped. If several match, round-robin among them.
3. Away or holiday: an away salesperson is skipped, their backup takes their place; if nobody is left the
   deal goes to the manager (§24 "Several salespersons are away -> deals go to backups; if none, to the
   manager").
4. Reassign: the manager or MD can reassign any deal; both salespersons are notified, the timeline records it.
5. MD takeover: the MD takes the deal (the AI pauses, approvals go to the MD, the salesperson is read-only)
   and hands it back.
6. Returning customer: goes to the salesperson who handled their last deal, if that person is active.

Round-robin is atomic: one transaction-level advisory lock serialises assignments, then the chosen user's
`round_robin_seq` takes the next value of `crm.assignment_seq`, so concurrent assignments never pick the
same "next" salesperson twice.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.auth import Principal, Role
from kx.core.errors import Forbidden, NotFound, RuleViolation
from kx.core.event_payloads import DealAssigned, StaffContact
from kx.crm.deps import CrmDeps
from kx.crm.models import Contact, Deal, DealRequirement, Product, User
from kx.crm.services import approvals, change_requests, staff
from kx.crm.services.journal import Actor, Emit, Timeline, record
from kx.crm.services.settings import AssignmentRule

Reason = Literal[
    "round_robin", "rule", "returning_customer", "backup", "manager", "reassigned", "takeover", "hand_back"
]
_LOCK: Final = "crm.assignment"
_CLOSED: Final = ("WON", "LOST")


# ---------------------------------------------------------------- pure selection (unit-tested)
@dataclass(frozen=True, slots=True)
class Candidate:
    user_id: uuid.UUID
    languages: frozenset[str] = frozenset()
    countries: frozenset[str] = frozenset()
    categories: frozenset[str] = frozenset()
    is_away: bool = False
    backup_id: uuid.UUID | None = None
    seq: int | None = None  # last round-robin turn (None = never assigned)


@dataclass(frozen=True, slots=True)
class BuyerFacts:
    language: str | None = None
    country: str | None = None
    category: str | None = None


def _matches(c: Candidate, rule: AssignmentRule, b: BuyerFacts) -> bool:
    match rule:
        case "language":
            return b.language is not None and b.language.split("-")[0].lower() in c.languages
        case "country":
            return b.country is not None and b.country.upper() in c.countries
        case "category":
            return b.category is not None and b.category in c.categories


def rule_pool(
    candidates: Sequence[Candidate], buyer: BuyerFacts, rules: Iterable[AssignmentRule]
) -> list[Candidate]:
    """§9.2: narrow by each listed rule in order; a rule nobody matches does not empty the pool."""
    pool = list(candidates)
    for rule in rules:
        matched = [c for c in pool if _matches(c, rule, buyer)]
        if matched:
            pool = matched
    return pool


def available(pool: Sequence[Candidate], everyone: dict[uuid.UUID, Candidate]) -> list[Candidate]:
    """§9.3: away salespersons are replaced by their backup (if the backup is there and not away)."""
    out: dict[uuid.UUID, Candidate] = {}
    for c in pool:
        if not c.is_away:
            out.setdefault(c.user_id, c)
        elif c.backup_id is not None and (b := everyone.get(c.backup_id)) is not None and not b.is_away:
            out.setdefault(b.user_id, b)
    return list(out.values())


def next_in_turn(pool: Sequence[Candidate]) -> Candidate | None:
    """Round-robin: whoever had the last turn longest ago (never-assigned first; ties by id)."""
    if not pool:
        return None
    return min(pool, key=lambda c: (c.seq is not None, c.seq or 0, c.user_id))


def choose(
    candidates: Sequence[Candidate], buyer: BuyerFacts, rules: Iterable[AssignmentRule]
) -> tuple[Candidate | None, bool]:
    """The salesperson for a new deal (None = the manager) and whether a rule decided it."""
    everyone = {c.user_id: c for c in candidates}
    rules = tuple(rules)
    pool = rule_pool(candidates, buyer, rules)
    narrowed = len(pool) < len(candidates) and bool(rules)
    return next_in_turn(available(pool, everyone)), narrowed


# ---------------------------------------------------------------- database
def _candidate(u: User) -> Candidate:
    return Candidate(
        user_id=u.id,
        languages=frozenset(x.lower() for x in u.languages),
        countries=frozenset(x.upper() for x in u.countries),
        categories=frozenset(u.category_codes),
        is_away=u.is_away,
        backup_id=u.backup_user_id,
        seq=u.round_robin_seq,
    )


async def _buyer_facts(session: AsyncSession, deal: Deal) -> BuyerFacts:
    contact = await session.get(Contact, deal.primary_contact_id)
    row = (
        await session.execute(
            select(DealRequirement.delivery_country, Product.category_code)
            .outerjoin(Product, Product.id == DealRequirement.product_id)
            .where(DealRequirement.deal_id == deal.id)
            .order_by(DealRequirement.created_at)
            .limit(1)
        )
    ).first()
    return BuyerFacts(
        language=contact.language if contact else None,
        country=row[0] if row else None,
        category=row[1] if row else None,
    )


async def _last_owner(session: AsyncSession, deal: Deal) -> User | None:
    """§9.6: the salesperson of the contact's previous deal, if active."""
    owner_id = await session.scalar(
        select(Deal.owner_user_id)
        .where(
            Deal.primary_contact_id == deal.primary_contact_id,
            Deal.id != deal.id,
            Deal.owner_user_id.is_not(None),
        )
        .order_by(Deal.created_at.desc())
        .limit(1)
    )
    if owner_id is None:
        return None
    user = await session.get(User, owner_id)
    return user if user is not None and user.is_active and user.role == "sales" else None


async def _take_turn(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        text("UPDATE crm.users SET round_robin_seq = nextval('crm.assignment_seq') WHERE id = :u"),
        {"u": user_id},
    )


async def assign(session: AsyncSession, deps: CrmDeps, deal: Deal, actor: Actor) -> User:
    """§5 step 4: give a qualified deal its salesperson and alert them (DEAL_ASSIGNED)."""
    if deal.owner_user_id is not None:
        owner = await session.get(User, deal.owner_user_id)
        assert owner is not None
        return owner
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _LOCK})
    s = await deps.settings.get(session)
    reason: Reason
    user: User | None = None
    returning = await _last_owner(session, deal)
    if returning is not None and not returning.is_away:
        user, reason = returning, "returning_customer"
    elif returning is not None and (backup := await staff.backup_of(session, returning)) is not None:
        user, reason = backup, "backup"
    else:
        rows = list(
            (
                await session.execute(
                    select(User).where(User.role == "sales", User.is_active).order_by(User.id)
                )
            ).scalars()
        )
        picked, by_rule = choose(
            [_candidate(u) for u in rows], await _buyer_facts(session, deal), s.assignment_rules
        )
        if picked is not None:
            user = await session.get(User, picked.user_id)
            reason = "rule" if by_rule else "round_robin"
            if user is not None and user.id not in {u.id for u in rows if not u.is_away}:
                reason = "backup"
    if user is None:
        user, reason = await staff.manager_user(session), "manager"
    else:
        await _take_turn(session, user.id)
    deal.owner_user_id = user.id
    await session.flush()
    await _announce(session, deal, to=user, previous=None, reason=reason, actor=actor, notify=())
    return user


async def _announce(
    session: AsyncSession,
    deal: Deal,
    *,
    to: User,
    previous: User | None,
    reason: Reason,
    actor: Actor,
    notify: Sequence[User],
) -> None:
    contacts: dict[uuid.UUID, StaffContact] = {}
    for u in (*notify, *((previous,) if previous else ())):
        if u.id != to.id:
            contacts.setdefault(u.id, staff.staff_contact(u))
    what = {
        "takeover": f"The MD ({to.full_name}) took the deal over",
        "hand_back": f"Deal handed back to {to.full_name}",
        "reassigned": f"Deal reassigned to {to.full_name}"
        + (f" (was {previous.full_name})" if previous else ""),
    }.get(reason, f"Deal assigned to {to.full_name} ({reason.replace('_', ' ')})")
    await record(
        session,
        actor,
        f"deal.{reason}" if reason in ("takeover", "hand_back", "reassigned") else "deal.assigned",
        "deal",
        deal.id,
        before={"owner_user_id": previous.id if previous else None},
        after={"owner_user_id": deal.owner_user_id, "taken_over_by": deal.taken_over_by, "reason": reason},
        timeline=Timeline("assignment", what, deal.id, deal.primary_contact_id),
        emit=Emit(
            ev.DEAL_ASSIGNED,
            "deal",
            deal.id,
            DealAssigned(
                deal_ref=deal.id,
                deal_number=deal.number,
                assigned_to=staff.staff_contact(to),
                previous=staff.staff_contact(previous) if previous else None,
                notify=tuple(contacts.values()),
                reason=reason,
            ),
        ),
    )


async def _lock_deal(session: AsyncSession, deal_id: uuid.UUID) -> Deal:
    deal = await session.get(Deal, deal_id, with_for_update=True)
    if deal is None:
        raise NotFound(f"deal {deal_id} not found")
    return deal


async def reassign(
    session: AsyncSession, deps: CrmDeps, deal_id: uuid.UUID, to_user_id: uuid.UUID, p: Principal
) -> Deal:
    """§9.4: the manager or the MD reassigns; both salespersons are notified (and the manager, §17)."""
    if not p.has(Role.MANAGER):
        raise Forbidden("only the manager or the MD can reassign a deal")
    deal = await _lock_deal(session, deal_id)
    if deal.stage in _CLOSED:
        raise RuleViolation("a closed deal cannot be reassigned")
    to = await session.get(User, to_user_id)
    if to is None or not to.is_active or to.role not in ("sales", "manager"):
        raise RuleViolation("reassign to an active salesperson or the manager")
    previous = await session.get(User, deal.owner_user_id) if deal.owner_user_id else None
    if previous is not None and previous.id == to.id:
        return deal
    deal.owner_user_id = to.id
    await session.flush()
    actor = Actor.user(p)
    await _announce(
        session,
        deal,
        to=to,
        previous=previous,
        reason="reassigned",
        actor=actor,
        notify=(await staff.manager_user(session),),
    )
    await approvals.reroute_open(session, deps, deal, actor)
    await change_requests.reroute(session, deps, deal, actor)
    return deal


async def take_over(session: AsyncSession, deps: CrmDeps, deal_id: uuid.UUID, p: Principal) -> Deal:
    """§3 / D5: the MD takes the deal: AI paused, approvals come to the MD, the salesperson keeps view
    access only. The salesperson and the manager are notified (§17)."""
    if p.role is not Role.MD or p.user_id is None:
        raise Forbidden("only the MD can take a deal over")
    deal = await _lock_deal(session, deal_id)
    if deal.taken_over_by is not None:
        return deal
    md = await session.get(User, p.user_id)
    if md is None:
        raise NotFound("MD user not found")
    deal.taken_over_by, deal.taken_over_at = md.id, deps.now()
    if deal.ai_enabled:  # the MD negotiates personally (§23 scenario 8)
        deal.ai_enabled, deal.ai_paused_reason, deal.ai_paused_at = False, "md_takeover", deps.now()
    await session.flush()
    actor = Actor.user(p)
    owner = await session.get(User, deal.owner_user_id) if deal.owner_user_id else None
    await _announce(
        session,
        deal,
        to=md,
        previous=owner,
        reason="takeover",
        actor=actor,
        notify=(await staff.manager_user(session),),
    )
    await approvals.reroute_open(session, deps, deal, actor)
    await change_requests.reroute(session, deps, deal, actor)
    return deal


async def hand_back(session: AsyncSession, deps: CrmDeps, deal_id: uuid.UUID, p: Principal) -> Deal:
    """The MD hands the deal back to the salesperson (for payment and the order, §23 scenario 8). The AI
    stays as it is: the salesperson decides whether it resumes."""
    deal = await _lock_deal(session, deal_id)
    if deal.taken_over_by is None:
        raise RuleViolation("this deal is not taken over")
    if p.role is not Role.MD or p.user_id != deal.taken_over_by:
        raise Forbidden("only the MD who took the deal over can hand it back")
    md = await session.get(User, deal.taken_over_by)
    deal.taken_over_by, deal.taken_over_at = None, None
    await session.flush()
    actor = Actor.user(p)
    owner = await staff.approver_for(session, deal)
    await _announce(
        session,
        deal,
        to=owner,
        previous=md,
        reason="hand_back",
        actor=actor,
        notify=(await staff.manager_user(session),),
    )
    await approvals.reroute_open(session, deps, deal, actor)
    await change_requests.reroute(session, deps, deal, actor)
    return deal


async def set_availability(
    session: AsyncSession,
    deps: CrmDeps,
    user_id: uuid.UUID,
    *,
    is_away: bool,
    backup_user_id: uuid.UUID | None,
    p: Principal,
) -> User:
    """§9.3 away / holiday. While away, new deals skip the salesperson and the backup covers their open
    deals (approvals and change requests move to the backup, or the manager if there is none)."""
    if not (p.has(Role.MANAGER, Role.ADMIN) or p.user_id == user_id):
        raise Forbidden("only the manager, an admin or the user can change availability")
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        raise NotFound(f"user {user_id} not found")
    if backup_user_id is not None and backup_user_id == user_id:
        raise RuleViolation("a user cannot be their own backup")
    before = {"is_away": user.is_away, "backup_user_id": user.backup_user_id}
    user.is_away, user.backup_user_id = is_away, backup_user_id
    await session.flush()
    actor = Actor.user(p)
    await record(
        session,
        actor,
        "user.availability_changed",
        "user",
        user.id,
        before=before,
        after={"is_away": is_away, "backup_user_id": backup_user_id},
    )
    open_deals = list(
        (
            await session.execute(
                select(Deal)
                .where(Deal.owner_user_id == user.id, Deal.stage.not_in(_CLOSED))
                .with_for_update()
            )
        ).scalars()
    )
    for deal in open_deals:
        await approvals.reroute_open(session, deps, deal, actor)
        await change_requests.reroute(session, deps, deal, actor)
    return user


async def load_by_owner(session: AsyncSession) -> dict[uuid.UUID | None, int]:
    """Open deals per owner (dashboard / tests)."""
    rows = await session.execute(
        select(Deal.owner_user_id, func.count())
        .where(Deal.stage.not_in(_CLOSED))
        .group_by(Deal.owner_user_id)
    )
    return {owner: int(n) for owner, n in rows.tuples()}
