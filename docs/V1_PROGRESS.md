# v1 flow progress (WP1–WP12 of docs/V1_CHANGE_PLAN.md)

Baseline before the work (5 Nov, commit 6ce4a4c, Linux container with pgvector Postgres 18 + redis-server):
**309 passed**, `make lint` clean.

Environment note: on Windows the DB tests cannot run natively (psycopg's async pool needs a selector event
loop, and WeasyPrint needs GTK/Pango). The gate was run in a `python:3.14-slim-trixie` container with the
same system libraries as the production image, against a `pgvector/pgvector:pg18` container, like CI.

## WP1. Database migration 0002

**Plan.** Files: `migrations/{crm,automation,agents}/versions/0002_v1_flow.py`, a raw-SQL helper in
`arifaa_pack/migrations/__init__.py`, the ORM models of each package, the root `tests/conftest.py` (test DBs
are now built by Alembic: 0001 DDL then 0002), `infra/scripts/db_bootstrap.sh` (btree_gist).
Tests: the three ORM-vs-`information_schema` tests (extended to the new tables/columns), a new
`tests/db/test_migrations_v1.py` (upgrade from the 0001 state with data, run twice, constraints and seeds).

**Done.** Revisions `crm_0002`, `automation_0002`, `agents_0002` (all items of the WP1 list). Additions the
later WPs need, each tied to a rule: `quote_line_tiers` (price-list snapshot per quote line, §24 "an open quote
keeps its prices"), approval kinds `revision`/`counter`, `approvals.escalated_at` (§16 120-min escalation),
grant policy `sales` (D1), task kinds `pricing_needed` (§10, D8) and `lost_proposal` (§16, D15), LOST reasons
`budget`/`duplicate`/`other` (§15.4), call purpose `change_request_alert` (§13.5), `channel_accounts.is_default`
(WP6 fallback region), `change_requests.ai_notes` (§13.2 card), settings `quote.vat_pct_export` (§10). The
freeze trigger now also locks the new buyer-visible quote fields (§11.3.3).
Test DBs are now built by Alembic (0001 then 0002), so every DB test runs on the migrated schema.
Tests added: `tests/db/test_migrations_v1.py` (8 tests: upgrade from a 0001 DB with data, twice; SQL re-run;
seeds kept/added; recipient hashes = Python; approval CHECKs; tier overlap/min ≤ list; freeze; automation
UNIQUE/default; agents columns); the 3 ORM tests now cover the new tables and columns.

## WP2. Pricing: price-list mode

**Plan.** New `kx/crm/pricing/price_list.py` (pure); `ladder_between` + `evaluate_ladder_counter` and
`LadderState` in `pricing/engine.py` / `types.py` (cost functions untouched); price-list branch in
`services/pricing.py`, `services/quotes.py` (`request_quote`, `create_final_version`) and
`services/negotiation.py`; typed settings for the new keys; contract verdict kinds `REQUOTE`/`CHANGE_REQUEST`.
Tests first: `tests/spec/test_spec_price_list.py` (§11-12, §23 numbers, 2 hypothesis properties, 3 benches),
`packages/kx-core-crm/tests/test_price_list_db.py` (DB).

**Done.** Quotes in `pricing.mode='price_list'` are built from the product's current tier version: list
price, FX at the day's rate (the converted line total is the AED line total at the printed rate, so
12,500.00 AED = USD 3,403.68), VAT by country (5 % / export 0 %), Incoterm from `quote.default_incoterm`,
delivery time text from the product default (D10), payment terms from a setting, signed by the owner (D11).
No staleness block, no delivery estimate. Each line keeps a price-list snapshot (`quote_line_tiers`) so an open
quote keeps its prices (§24). Negotiation in price-list mode builds `LadderState` from that snapshot (never
from cost) and applies §12.2 rules 1-5; a tier change drafts a revised quote for approval; quantity outside the
tiers or a hand-priced line returns `CHANGE_REQUEST`. The 4 Nov cost-based DB tests now run with
`pricing.mode='cost'` (fixture `cost_mode`): the v2 path keeps its coverage.
New revision `crm_0003` seeds `quote.payment_terms_text`, `quote.currency_by_country`,
`negotiation.free_samples_allowed`.
Tests added: 36 spec tests (`test_spec_price_list.py`) and 11 DB tests (`test_price_list_db.py`).
Totals: 362 passed.

## WP3. Approvals: the salesperson approves

**Plan.** New `services/access.py` (who may approve / edit / view); `services/approvals.py` (approver,
escalation, special price, sweep to the manager), `services/staff.py` (`approver_for`, `manager_user`),
`services/issuing.py` + `grants.py` (grant policy = who approved), `services/quotes.py` (`revise_quote`, final
quote to the salesperson), API routes (`/approvals/{id}/decision` open to sales, `/deals/{id}/quotes/revise`,
takeover read-only on write routes), `QuoteSubmitted` payload. Tests: `test_approvals_db.py` (DB + HTTP).

**Done.** Every quote kind (initial, revision, final) is approved by the deal's salesperson; the manager may
approve any deal; the MD only a deal it has taken over (approvals then go to the MD); the owner is read-only
during a takeover (403 on quote, revise, stage and decision routes; view stays). `QuoteSubmitted`: approver =
salesperson, escalate_to = manager, due = 120 alert-hour minutes. A revised price below the tier minimum opens
a manager-only `special_price` approval first; once approved, the salesperson's approval opens on the same
version (scenario 3). The MD value limit only applies when `approval.md_value_limit_aed` is set. The sweep
escalates overdue approvals to the manager. Each buyer-facing version still gets a single-use grant
(policy = the approver's role). The 4 Nov tests that asserted MD final approval were updated to the 5 Nov
rules (D1).
Tests added: 11 (`test_approvals_db.py`: own deal vs another salesperson, manager any deal, MD only after
takeover, owner read-only over HTTP, final quote grant single-use, edit within range / above list refused,
scenario 3 special price, special price declined, scenario 6 timer 14:00 → 16:00 and 21:30 → 09:30, MD value
limit). Totals: 373 passed.

## WP4. Change requests

**Plan.** kx.core: events `CHANGE_REQUESTED` / `CHANGE_REQUEST_CLOSED`, payloads, `ChangeField`/`ChangeType`,
`CrmGateway.raise_change_request`. kx.crm: `services/change_requests.py`, `api/change_requests.py`, gateway,
status reader, sweep. kx.agents: tool `raise_change_request`, deal graph `intent=change`, phrases. kx.automation:
the ladder generalised (`LadderTicket`) and moved to the §16 schedule; handler for `CHANGE_REQUESTED`. Tests
first: `kx-agent-framework/tests/test_change_requests.py`, `kx-core-crm/tests/test_change_requests_db.py`,
automation ladder tests.

**Done.** The AI raises a card for any non-price request (product, spec, printing, quantity outside the tiers,
delivery, samples, payment, other) and answers with the fixed §13.1 sentence naming the salesperson and the
`change_request.sla_working_hours` setting; payment wording always becomes a payment card (deterministic check
before the model). The CRM fills the "current value" column from the quote, sets the SLA in alert hours and
alerts the salesperson. Lifecycle open → in progress → resolved (when the revised version is approved and sent)
or declined (reason required). The reminder ladder now follows §16 for approvals and change requests alike:
WhatsApp at once, WhatsApp at 30 min, alert call at 60 min, manager (WhatsApp + call) at 120 min, in alert hours,
stopping as soon as the item is decided. The 4 Nov ladder tests (3 calls / 20 min, press 2 → MD) were rewritten
to the §16 schedule (D4).
Tests added: 6 agents (scenario 4 delivery with no promise, payment ×3, quantity outside tiers, tier change,
no salesperson, tool), 6 CRM DB (card + alerts, SLA over night, scenario 4 lifecycle over HTTP, decline,
resolve rules, 120-minute sweep), 3 automation (§16 schedule, press 2, change-request ladder).
Totals: 389 passed.

## WP5. Assignment, away/backup, reassign, takeover

**Plan.** `services/assignment.py` (pure selection + DB), `services/staff.py` (backup covers), `services/access.py`
(the covering backup may act), approvals/change-request rerouting, `api/assignment.py` (reassign, takeover,
hand-back), `PUT /users/{id}/availability`, event `DealAssigned` + automation notice handler, revision `crm_0004`
(`change_requests.alert_ref`). Tests: `test_assignment_rules.py` (unit + hypothesis), `test_assignment_db.py`.

**Done.** A deal is assigned when it becomes QUALIFIED (AI or staff): returning customer → last active owner;
otherwise round-robin over active salespersons, narrowed by `assignment.rules` in order (language, country,
category; an unmatched rule is skipped); away salespersons are replaced by their backup, or the manager.
Round-robin is serialised by an advisory lock and `crm.assignment_seq`. Reassign (manager/MD), takeover and
hand-back (MD) move pending approvals (cancel + re-open) and open change-request cards (new alert ref) to the
person now responsible; the takeover pauses the AI (`md_takeover`); `DEAL_ASSIGNED` notifies the new person,
the previous one and the manager (automation, alert hours). While a salesperson is away, the backup covers
their deals (approvals move, the backup may approve and edit).
Tests added: 8 unit (rule order, several matches, unmatched rule, scenario 7, away/backup, all away → manager,
property: never picks someone away) and 8 DB (assign on qualify, 10 concurrent assignments = 5/5 with unique
turns, returning customer, language rule + away backup, approvals follow the backup, scenario 6 reassign over
HTTP, scenario 8 takeover/hand-back over HTTP, availability route). Totals: 405 passed.

## WP6. Sending the quote

**Plan.** automation `repo.channel_account` (regional number) + `outbound`/handler pass-through; CRM `pdf.py`
(document fields, labels, message), `quote.html.j2`, `issuing.py` (signer, country, language),
`quotes.set_cover_note`, revision `crm_0005` (`cover_note`, `change_summary`); contracts
(`OutboundRequest.buyer_country`, `CrmGateway.set_cover_note`); agents quotation graph writes the cover line.
Tests: `test_auto_regional_db.py`, `test_quote_document.py`, one DB test in `test_approvals_db.py`.

**Done.** WhatsApp goes from the number the buyer already writes to, else the regional number whose
`countries` list the delivery country, else the default number; email always from the company address. The
message is signed "— Sara, Arifa Pack" (the deal's salesperson, D11), carries the AI-written "open to discuss"
line (or an approved variant picked per quote when the AI wrote none), says what changed on a revised quote,
and has no discuss line on the final quote. The PDF prints delivery time, Incoterm and place, payment terms,
the FX rate and its date, and the salesperson's contact; Arabic is right-to-left; the cover line is never in
the PDF. The guard passes on the generated message against the deal's allowed prices.
Tests added: 3 automation (country routing + default, existing thread wins, email/alerts identity), 6 CRM unit
(PDF fields, no cover line in the PDF, Arabic RTL, PDF bytes, signed message + guard, varied fallback, final /
revision wording), 1 CRM DB (sent message: AI cover line, signature, region, guard). Totals: 415 passed.

## WP7. Final quote and handover

**Plan.** CRM `deals.hand_over` called on final approval, `QuoteApproved.salesperson/manager` (final only),
`DealContext.ai_paused_reason/team_available`; automation final-quote alert + manager notice; agents orchestrator
`acknowledge` route, `Phrase.HANDOVER_ACK`, service delivers it. Tests in agents (`test_db.py`, `test_routing.py`),
automation (`test_auto_db.py`), CRM (`test_approvals_db.py`).

**Done.** Approving the final quote sends it (QUOTE_APPROVED kind final, as before), alerts the salesperson
(`final_quote_alert`) and notifies the manager (`staff_notice`) in alert hours, and sets `ai_enabled=false`,
`ai_paused_reason='handover'`. Buyer messages on a handed-over deal never reach a model: they go to the
salesperson's timeline/inbox; outside staff hours the buyer gets only "Thanks — Sara will reply in the morning."
Tests added: 2 agents DB (no AI reply in hours; acknowledgement only after hours), 1 routing unit, 1 automation
(quote sent, salesperson alert, manager notice), 1 CRM DB (final approval hands over; only the final quote alerts).
Totals: 420 passed.

## WP8. Payment, WON, LOST, order

**Plan.** `services/orders.py` (`record_payment` → PAYMENT, new `mark_won`), `rules.py` (§15 LOST reasons,
`lost_proposal_due`), new `services/lost_proposals.py` + daily job, API (`/payments` for the salesperson with
MFA, `/won`), revision `crm_0006` (`payments.proof_s3_key`). Tests: `test_won_lost_db.py`.

**Done.** Recording the advance needs amount, currency and method (reference, date and proof file are stored)
and moves the deal to PAYMENT; WON is a separate salesperson step that needs the advance and the expected
re-order date and creates the order with that date. LOST reasons are the §15 list. A daily job raises one
`lost_proposal` task for the salesperson when the buyer has not replied 14 days (setting) after the last
follow-up; the deal never moves by itself. The 4 Nov test that went payment → WON in one step now does the two
steps.
Tests added: 7 (scenario 1 advance → PAYMENT → WON with the re-order date → order statuses; WON needs the advance;
§15 reasons; LOST with a new reason; scenario 9 date; silent buyer → one task, never LOST; a reply stops it).
Totals: 427 passed.

## WP9. Switch v2 features off (flags; code kept)

**Done.** `features.scraping=false`: `build_jobs(scraping=...)` keeps the scraper task but schedules it only when
the flag is on; the container reads the flag from `crm.settings` at boot. Staleness: no warning/block in
price-list mode (quote expiry still runs daily). `features.delivery_calc=false`: `delivery_estimate` returns the
quote's (or product's) default delivery text, no calculated date and no estimate row. `features.news_digest=false`:
there is no digest job in v1 (tables only). Repeat sales: `AgentSpec.enabled=False`, the orchestrator sends
won-customer messages to the salesperson. The 4 Nov blueprint test now checks both flag positions.
Tests added: smoke test asserts the scraper is not scheduled and repeat sales is off after boot; routing unit;
2 CRM DB (delivery answer without calculation, staleness job off). Totals: 430 passed.

## WP10. AI behaviour updates

**Done.** Base rules and role prompts follow §12.2 rule 6, §13, §18 and §24: price only; never payment, credit or
bank details; never a longer delivery as a concession, a delivery date, free samples or a discount on custom
items; never say or hint at a minimum, steps or a range; trick prompts get a normal sales answer asking for target
and volume; "are you a bot" → the company's AI assistant, a salesperson reviews every quotation; unsure or upset
buyer → the salesperson. Fixed phrases: "Let me check this with our sales team — you'll hear back within 60
minutes during working hours." (§12.2 rule 4, EN/AR), "within 4 working hours" (§13.1), the after-hours
handover acknowledgement (§14.6). On agreement the AI confirms product, quantity, unit price, total, delivery time
and Incoterm (§14.2); the agreed total is computed in Decimal and is the only new amount allowed. The router now
treats "OK, confirmed / go ahead / PO" as agreement (§14.1), sends post-quote change requests and every payment
topic to the deal graph (deterministic change request). The eval YAML scenarios were replaced by §23 scenarios
1–10 (s01–s10) and 7 trick/edge prompts (t01–t07); the harness now checks change requests and the after-hours
acknowledgement. Totals: 437 passed.

## WP11. CRM API for the screens (§20)

**Done.** New `api/screens.py` (thin) over `services/inbox.py`, `services/price_lists.py` and `orders.set_stock`:
`GET /inbox` (approvals, special prices, change requests, below-minimum / pricing-needed / LOST-proposal tasks,
buyer messages on handed-over deals; due time + overdue; a salesperson sees their own and what they cover, the
manager/MD everything); price-list tier CRUD (manager/admin with MFA, every change audited old/new, a tier used
by quotes cannot be deleted); Excel import (raw .xlsx body, all rows validated first: min ≤ list, no overlaps,
codes exist, AED; any error saves nothing and returns row errors; a valid file is the next price-list version)
and export; stock table (`GET/PUT /stock`, manager sets, salespersons read). Settings are now readable/writable
by the manager too (§20.11); the new keys validate. Change-request card actions, reassign/takeover/hand-back were
built in WP4/WP5. The gateway now turns "the AI may not price this" into a Pricing-needed task (D8, scenario 5)
and a quantity outside the tiers into a change request (D12); the two WP2 tests that expected an error now check
both layers.
Tests added: 2 unit (Excel row validation), 6 HTTP/DB (inbox per person, handed-over buyer messages, tier CRUD +
MFA + audit, Excel export/import, stock, settings). Totals: 445 passed.

## WP12. Notifications matrix (§17)

| §17 row | Event | Handler / where | Recipients and channel |
|---|---|---|---|
| New qualified deal assigned | `deal.assigned.v1` | automation `_deal_assigned` | salesperson WhatsApp notice (+ CRM inbox); buyer Requirement Summary **not built** |
| Quote waiting for approval | `quote.submitted.v1` | automation ladder | salesperson 0/30 min WhatsApp, 60 min call; manager at 120 min |
| Quote sent | `quote.approved.v1` | automation `_quote_approved` | buyer quote PDF + message (salesperson: CRM timeline) |
| Counter-offer handled by the AI | (CRM `negotiation.round` timeline) | CRM | salesperson CRM timeline; buyer AI reply |
| Buyer pushes below the minimum | `negotiation.paused.v1` | automation `PauseAlerts` | salesperson WhatsApp + call; manager WhatsApp; buyer 60-minute sentence (AI) |
| Change request | `change_request.raised.v1` | automation ladder | salesperson at once + reminders; manager at 120 min; buyer 4-hour sentence (AI) |
| Final quote approved and sent | `quote.approved.v1` (final) | automation | buyer final quote; salesperson alert; manager notice |
| Payment recorded / WON | `deal.won.v1` | automation `_deal_won` | manager + MD WhatsApp notice; buyer thank-you template |
| Order status changed | `order.status_changed.v1` | automation `_order_status_changed` | buyer WhatsApp only when `notifications.order_status_to_buyer` (default off) |
| MD takes over / reassigned | `deal.assigned.v1` | automation `_deal_assigned` | salesperson(s) + manager (not the MD on its own takeover) |
| Buyer opts out | `message.received.v1` (`opted_out`) | CRM (AI off, `opt_out`, timeline) + automation | buyer one confirmation template (passes the suppression); salesperson CRM |

The MD "Dashboard" column is the CRM data (no message). Tests added: 11 parametrized matrix rows, the pause
alert split, the opt-out confirmation (automation), the opt-out bypass on the real OutboundService, and 2 CRM DB
tests (WON event recipients + order-update flag; opt-out pauses the deal). The 4 Nov assertion "manager + MD
alerted on a pause" now reads "salesperson alerted, manager notified". New revision `crm_0007`
(`notifications.order_status_to_buyer`). Totals: 461 passed.
