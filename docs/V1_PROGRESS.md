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


## Extra testing (after WP12)

* **§23 scenarios 1–10, end to end** (`tests/scenarios/test_scenarios_v1.py`): real CRM and automation on Postgres,
  the real router and graphs with the scripted fake model, the CRM outbox relayed to the real automation handlers and
  §16 ladder, and a sender that consumes each quote's single-use grant like `OutboundService`. Each test checks the
  stage history, who was alerted or notified, which versions were approved and sent, and runs `check_prices` on
  every AI reply against the deal's `allowed_prices`.
* **§17 matrix**, table-driven: `packages/kx-core-automation/tests/test_notifications_matrix_db.py` (11 rows + pause
  split + opt-out confirmation).
* **Safety** (`tests/scenarios/test_safety_v1.py`): the minimum, cost and un-offered steps never in the AI context
  and blocked if said (plus a hypothesis property); payment topics routed to a person in every open stage (24
  cases) and turned into a payment change request; no approving another salesperson's deal; owner read-only during
  an MD takeover; each grant single-use (and automation's send refused once used).
* **Whole-app smoke test** (`tests/integration`): migrate twice, boot the API, fan out an event; now also asserts
  the scraper is not scheduled and repeat sales is off.

Bugs these tests found and fixed: `QuoteSubmitted.remind_at` was never filled (approvals skipped the 30/60-minute
steps); follow-ups ignored the buyer country's weekend (`crm_0008`); no way to price a custom item by hand
(`quotes.manual_quote`); "OK, confirmed" and post-quote change requests did not reach the deal graph (WP10).

## Benchmarks (`make bench`, Linux container, 6 Nov)

| Benchmark | Mean | Budget |
|---|---|---|
| `ladder_between` (lru-cached) | 0.10 µs | µs |
| `resolve_tier` | 0.72 µs | µs |
| `evaluate_ladder_counter` (with tier check) | 2.28 µs | µs |
| `evaluate_counter` (v2) | 1.42 µs | µs |
| `price_line` (v2) | 2.55 µs | µs |
| price guard, 1000 messages | 5.95 ms | < 10 ms |

## Totals

502 passed, 0 skipped (309 before the work): CRM 110, automation 128, agents 64, root spec 131, scenarios + safety
41, unit 14, DB 12, integration 2. `make lint`: ruff, ruff format, mypy --strict (133 files), import-linter 3/3.

---

# 11 Nov plan (WP13–WP29 of docs/V1_CHANGE_PLAN.md)

Sources: the SRS (`docs/Arifaa Pack — Detailed Business Flow v1 (SRS basis) (2).md`) and the platform docs
(`docs/Arifaa Platform — Platforms, Pricing & Fallbacks/`). Gate: the same Linux container + pgvector Postgres 18 as
before (Windows cannot run psycopg's async pool or WeasyPrint natively).

## Baseline (12 Nov, before the work)

502 tests, but **13 failed by date**: the grant was issued on the app clock (fixed test dates of 9 Nov) and consumed
against the database's real `now()`, so every quote send in the DB tests expired on 11 Nov 06:00 UTC. Fixed in the
code, not the tests: `grants.issue_grant` now stamps `issued_at` / `expires_at` with the database clock, the same
clock `crm.consume_send_grant` checks (a security boundary must not depend on an app server's clock). Scenario 9 now
advances from the real send time. Two pure Excel tests that inherited a DB fixture moved to
`test_price_list_rows.py` (they now run on Windows). Result: **502 passed, 0 failed**.

## WP13. Documents

CLAUDE.md, CONTRACTS.md, README.md, `docs/V1_CHANGE_PLAN.md` (11 Nov plan, D17–D40), `docs/OPEN_QUESTIONS.md`
(#40–#60), HANDOFF_CONTEXT.md, DEPLOYMENT.md, CONTESTS.md (§5 platform contest), `db/README.md`, the root
`V1_CHANGE_PLAN.md` (now a pointer). Code comments say "the SRS" instead of the deleted `docs/BUSINESS_FLOW_V1.md`.

## WP16. Payment, WON, LOST reopen, visibility (§15, §7A.1, §3, §14; D20, D24)

* §15.1 / D20: confirming the advance is required, every detail optional (an amount needs its currency);
  timeline "Advance confirmed …". §15.2: WON mandatory, re-order date optional.
* §7A.1 / D24: `POST /v1/deals/{id}/reopen` (manager or MD, MFA, reason): LOST -> the stage it was lost from; the AI
  stays paused. §14: `POST /v1/deals/{id}/personal-contact-shared`.
* §3: a salesperson's `GET /v1/deals` shows only their own deals and those they cover; timeline and quote list check
  view access (before this, any salesperson could read any deal's timeline). §20.3 board filters: salesperson,
  country, product id / code, value in AED, opted out.
* Migration `crm_0009` (also used by WP14/15/17/18): `deals.opted_out_at`, `ai_summary(_at)`,
  `personal_contact_shared_at/_by`, LOST reason `opted_out`, task kinds `optout_review` / `phone_unreached` /
  `call_buyer`, `contacts.phone_status(_at)` + `whatsapp_profile_name` (back-filled `verified` for WhatsApp contacts),
  `products.spec` / `cms_published` / `cms_synced_at`, settings `optout.*`.
* After-the-deal routes (payment, WON, order status) moved to `kx.crm.api.deal_actions` (same paths).
* Tests: 2 DB (details-free payment + WON without a date; reopen) and 3 HTTP (§3 lists, reopen needs manager + MFA,
  personal contact).

## WP20. Background jobs on Celery (D25)

* `kx.core.jobs`: `JobStore` over `automation.scheduled_jobs` (enqueue with de-duplication key, replace, pull forward,
  cancel by key or prefix, claim with per-lock ordering under an advisory lock, finish kicks the next job on the same
  lock, retry with exponential back-off, recover rows left `running` by a dead worker, queue lag) and `JobRunner`
  (the task type's `JobSpec` owns its retry policy). Migration `automation_0003`.
* `kx.automation.jobs`: same task bodies; `JobScheduler` implements `TaskScheduler` (+ `cancel_followups`,
  `schedule_optout_check`, `cancel_optout_check`); `periodic_jobs()`. `kx.crm.jobs.periodic_jobs()`. Procrastinate is
  gone from both packages and the lock.
* App: `arifaa_pack.jobqueue` (the one Celery app: acks_late, reject on worker lost, prefetch 1, visibility timeout
  2 h; `CeleryKicker` publishes in a thread), `arifaa_pack.schedule` (Beat timetable: sweep 30 s, relays 5 s,
  heartbeat 30 s, the 4 Nov crons), `arifaa_pack.celery_app` (`kx.run_job`, `kx.periodic`; one event loop + container
  per worker process), `kx worker` / `kx beat`. The container's `EventBus` writes one job per subscribed package.
* The scraper's daily job returns at once while `features.scraping` is off (D14).
* Tests: 9 DB (store and scheduler); the smoke test boots the app, fans an event out to `scheduled_jobs`, runs it once
  and shows a redelivery is a no-op; Beat's timetable equals the worker's periodic map.

## WP14. STOP flow (§8.1, D18)

* `kx.core.optout`: the rules (whole-message words, clear phrases, channel phrases, stop word in a sentence -> the AI;
  voice: "stop" = wait), the confirmation texts (EN from the SRS; AR/HI/TA/ZH/SI drafts for review, OPEN #41).
* Automation: suppression by scope (`all`/`whatsapp`/`email`/`phone`) and by person (`contact_suppressions`); the
  permanent `opt_out_log`; `apply_stop` (suppress, cancel the queue, log, announce `consent.opted_out.v1`) inside the
  webhook's transaction; the one confirmation as free text in the buyer's language inside the 24-hour window;
  follow-ups cancelled; the 24-hour "no answer" timer; one-click email unsubscribe (RFC 8058 headers, page with an
  optional reason; a GET changes nothing). `OutboundService` now stores `contact_ref` and `recipient_hash` on every
  row (the columns existed since 0002 but were never filled). Migration `automation_0004`.
* CRM: `kx.crm.services.consent` (consent rows, Opted out badge, salesperson + manager notice via the new generic
  `staff.notice_requested.v1`, LOST proposed with reason `opted_out` or the buyer's own, channel switch for
  "use email instead"); a stop automation knew by one address is handed back with every address of the person.
* Agents: no AI reply to a message that already stopped; a stop word in a sentence is read by the fast model
  (opt-out / not / unclear; a failure counts as unclear); unclear -> the SRS question if the buyer was active in the
  last 10 minutes, else a review task; voice "stop" = listen, "stop calling me" = calls stopped.
* Tests: 29 spec (every example sentence in §8.1, confirmations, a hypothesis property), 5 automation DB, 5 agents DB,
  2 CRM DB, and the 5 earlier opt-out tests rewritten to the new rules.

Totals after WP14: **554 passed** (root 229, CRM 117, automation 139, agents 69); ruff, format, mypy --strict
(147 files) and import-linter clean.

## WP15. Phone confirmation and Requirement Summary (§8.2, §6.5, D19)

* `contacts.phone_status` moves forward only on WhatsApp's own signals (delivered = reachable, read = likely, buyer
  reply = verified, failed = not reached); `phone_kind` (mobile / landline / invalid) makes the AI ask the §8.2 line.
* On QUALIFIED the CRM renders the Requirement Summary PDF (no prices; headless Chromium, WeasyPrint fallback) and
  automation sends it as an approved template with a document header. A failed summary blocks the WhatsApp quote:
  email when the buyer gave one, else WhatsApp as the approving salesperson decides, with a task (OPEN #46).
* Tests: 12 CRM DB, 1 automation DB.

## WP21. In-house staff login + MFA (D26)

* `kx.core.passwords` (Argon2id, TOTP with replay protection, Fernet-sealed seeds), `kx.core.auth.TokenIssuer` (RS256,
  15-minute access tokens, `amr`), JWKS at `/.well-known/jwks.json`. Migration `crm_0010` (credentials, sessions,
  challenges).
* `/v1/auth/*`: login -> challenge; TOTP (`mfa=true`) or email code (`mfa=false`: no approvals); refresh in an HttpOnly
  cookie, rotated on every use, a replayed old token ends every session; logout, me, TOTP enrolment, password change,
  recovery by email code (always 202). 5 wrong passwords or codes lock for 15 minutes; every step audited. A
  break-glass sign-in notifies the MD and the manager (`staff.notice_requested.v1`).
* `/v1/users` (admin with MFA): list, create with a one-time password, edit (role, active, alert channels, assignment
  fields), reset MFA, unlock. `kx create-user` for the first admin. Staging and production refuse to start without
  `KX_JWT_PRIVATE_KEY` and `KX_AUTH__ENCRYPTION_KEY`.
* Tests: 6 HTTP+DB (email code then TOTP, lock-out, refresh rotation and reuse, single-use codes, break-glass alert,
  reset by code).

## WP17. Salesperson inbox reply, click-to-call, AI summary (§14, §7A.2, D21, D23)

* `POST /v1/deals/{id}/messages`: a staff reply on the buyer's channel and company number through `OutboundService`
  (kind `staff_reply`), signed "— Name, Company" on WhatsApp; email from the company address with the salesperson's
  name as sender (`outbound_messages.sender_name`, migration `automation_0005`). The price guard runs on staff text:
  an amount outside the latest issued quote or offer is refused (`unapproved_price`); percentages (payment terms)
  are for people. WhatsApp after 24 h answers `window_closed`; the screen resends with the approved re-open template
  (`handover.reopen_template`, default `staff_reopen`, params: name, company). The AI pauses (human takeover).
* `POST /v1/deals/{id}/call`: `CallBridge` rings the salesperson, then dials the buyer from the company number
  (`answerOnBridge`), after the §8.1 call stop and the calls kill switch; one ring per click; logged on the deal.
* Agents write the deal-page summary after the first exchange, every third exchange and at hand-over
  (`conversation.summary_updated.v1`); the CRM stores it without bumping the deal version, newest wins.
* Fixed on the way: the migration test's glob skipped revision `0010`.
* Tests: 10 CRM DB, 3 automation DB (bridge, call stop, kill switch), 1 outbound DB, 1 email, 8 agents spec.

Totals after WP17: **596 passed** (root 229, CRM 145, automation 145, agents 77); ruff, format and mypy --strict clean.

## WP18. CMS publish -> AI knowledge + CRM product mirror (§7, D22)

* `POST /webhooks/cms` (`kx.automation.cms`): HMAC-SHA256 over `"<timestamp>." + body` (`X-Kx-Timestamp`,
  `X-Kx-Signature: sha256=...`), 5-minute window, one receipt per `event_id`. Raises `kb.content_published.v1` per
  locale for pages, products, categories, FAQs, certificates, tour hotspots and testimonials, and
  `catalogue.product_published.v1` for products. Price-like spec keys are dropped before anything is stored.
* CRM `services/catalogue.py`: upsert by product code (case-insensitive); only descriptive fields; a new code is
  not quotable until the manager prices it; a new category is created; unpublish only clears the flag.
* Tests: 6 automation DB (signature, replay, stale, locales, no prices), 2 CRM DB.

## WP19. Website API (§6, D34, D39)

* `kx.agents.website`: `POST /v1/public/sessions` (Turnstile, signed session token, voice consent stored with its
  time: migration `agents_0003`), `/messages` (one turn through `AgentService`; the CRM deal is created on the first
  message), `/voice` (LiveKit token with `deal_ref` + `language`; refused for Sinhala and without voice consent),
  `/identify` (handed to the CRM merge; same answer whether the buyer was known). Per-IP and per-session token
  buckets (OPEN #61); the daily AI cap answers 503. `kx.core.signed` for the session token.
* Tests: 8 DB, 1 spec. Staging and production refuse to start without the Turnstile key.

## WP22. AI route, fallbacks and spend caps (D27, D30, D34)

* Bedrock removed (provider, config, client); `ModelRouter` = Claude API only (Haiku 5.5 FAST, Sonnet 5.5 SMART).
  Gemini stays a switch only: no Gemini client library is in the lock (not added without approval).
* `kx.core.fallback`: `CircuitBreaker` (3 failures / 60 s -> 5 minutes -> one trial) and `FallbackChain` (order,
  breakers, `Kairox/VendorSwitch` metric, alert hook, `AllVendorsFailed`).
* Agents: when every model fails, or Claude's breaker is open, the buyer gets "Let me check with our sales team."
  and the CRM pauses the AI with a task for the salesperson. Run cost from the official price table
  (`kx.agents.runs.PRICES`, Haiku's >100K tier included) is stored on `agent_runs.cost_usd` and added to the daily
  AI cap (`kx.core.spend.SpendMeter`: integers in Redis per local day, one alert at 80 % and at 100 %).
* Tests: 3 spec (breaker, chain, prices), 1 spec (spend), 1 DB (safe reply + breaker).

## WP23. Knowledge search on Qwen3 (D28)

* `HttpEmbedder` (OpenAI-compatible `/v1/embeddings`, Qwen3 query instruction, 1,024 dims checked, L2-normalised)
  replaces Titan. `KnowledgeBase.search` falls back to full-text + trigram when the embedding fails or takes > 2 s.
* Tests: the KB DB test now also searches with a failing and a slow embedder.

Totals after WP23: **618 passed** (root 234, CRM 147, automation 151, agents 86); ruff, format, mypy --strict and
import-linter clean.

## WP24. Observability (D29)

* Phoenix tracing over OpenTelemetry (`kx.agents.tracing`): one span per model call with OpenInference attribute
  names, PII masked; Langfuse removed from the dependencies (`uv lock`: langfuse and backoff gone, OTel 1.45.2 kept and
  now declared). Sentry SDK (Better Stack) now also starts in the API process.
* Tests: 2 (span content and masking; off without a collector).

## WP25. Channel fallbacks and kill-switch ladder (D30-D35)

* Email: `PostmarkSender` (fields per Postmark's API docs, read 12 Nov) and `EmailChain` (SES -> Postmark in the
  Parameter Store order, breaker, switch metric) used by `OutboundService` and the staff-code mailer.
  `POST /webhooks/postmark/inbound` (Basic auth, Postmark does not sign) records buyer replies like SES mail.
* `VendorOrder`: `<KX_APP_VENDOR_ORDER_SSM_PREFIX>/<field>`, cached 60 s, defaults on error.
* D32: 10 minutes after a priced WhatsApp message is sent, undelivered -> `quote.not_delivered.v1` with the exact
  content; the CRM grants the same hash on email and raises `quote.resend_requested.v1` (automation sends it), or
  tells the salesperson to call when there is no email.
* D35: `kx.core.killswitch` ladder (off / marketing / calls / text_only / hold) with `GET`/`PUT
  /v1/admin/kill-switch` (admin or MD with MFA, audited); follow-ups stop at `marketing`, calls at `calls`.
* Telnyx: not built (OPEN #55 lists the three points the docs did not confirm); calls stay on Twilio.
* Tests: 4 Postmark, 1 chain, 1 ladder, 1 outbound D32, 2 CRM D32.

Totals after WP25: **631 passed** (root 234, CRM 149, automation 160, agents 88); ruff, format, mypy --strict and
import-linter clean.

## WP28. CRM screens API completion (§20, §21)

* `kx.crm.api.records`: contacts (search, §3 visibility, edit non-identity fields), companies (list, create, edit),
  product catalogue (list, detail, manager+MFA edit of the CRM-owned fields only, D22), orders list, FX rates
  (latest per currency, manager+MFA override, D38).
* `kx.crm.api.reports`: `/dashboards/salesperson`, `/dashboards/manager` (incl. opt-out count and the buyers' own
  reasons), `/dashboards/md`; `/exports/{deals|contacts|companies|orders|products}?format=csv|xlsx`.
* `kx.automation.admin_api`: WhatsApp numbers per region (countries, default, status) and message templates
  (WhatsApp ones `submitted` until Meta's approval is recorded), admin with MFA.
* Kill switch: `GET`/`PUT /v1/admin/kill-switch` (WP25). Definitions chosen: OPEN #63, #64, #66.
* Tests: 5 CRM HTTP, 2 automation HTTP.

## WP29. Hand-offs

* `.env.example`: generated from `CoreSettings` (KX_), `AutomationSettings` (KX_AUTO_) and `AppSettings` (KX_APP_)
  = all 123 settings with the code's defaults and empty secrets, plus the voice-plugin, migration and CMS-container
  variables. Checked: all three settings classes load it.
* `docs/frontend/CRM_API.md`, `WEBSITE_API.md`: a written overview (design, roles, screens -> endpoints, sign-in,
  conventions, errors, mock server) + an endpoint reference generated from the code's OpenAPI, with the roles read
  from each route's auth dependency. `openapi-crm.json` / `openapi-website.json` for mock servers.
  `docs/frontend/CMS.md`: the content model (§7), the publish-webhook contract and the REST fields the platform
  reads back, from `kx.automation.cms` and `PayloadContentSource`.
* 46 routes had no docstring: each now states its purpose (shows in OpenAPI and the docs). One docstring was
  corrected after checking (`GET /v1/approvals` is manager / MD only).
* Makefile: `up` starts `postgres redis` (Valkey replaced in WP27); `worker` / `beat` targets for Celery.

Final totals: **638 passed** (root 234, CRM 154, automation 162, agents 88); ruff, ruff format, mypy --strict
(170 files) and import-linter (3 contracts) clean.

## WP26. FX daily rate (§11.2, D38) — blocked

* The manager override path is built and tested: `POST /v1/fx-rates` (manager + MFA) writes `crm.fx_rates`
  with source `manual`, and quoting reads the latest rate per currency; a manual rate always wins.
* The **automatic daily CBUAE feed is not built**: the documents do not give a machine-readable source,
  endpoint or licence for the UAE Central Bank rates (OPEN_QUESTIONS #56). Until that is confirmed, rates are
  entered by the manager. No `cbuae`/FX Beat entry exists, by design, so nothing silently serves a stale rate.

## WP27. Local stack: Redis

* `make up` and `docker-compose` run PostgreSQL (pgvector) + Redis. An earlier Valkey image was replaced with
  Redis so local matches the ElastiCache (Redis) target; the `SpendMeter` and rate limiter use it.

## 12 Nov. Audit pass (post-WP29 review and fixes)

A full audit of the 11 Nov code against the SRS and the platform docs (63 items done, 29 partial, 3 missing)
found and fixed the following, with tests. No SRS or platform doc was edited (read-only, as instructed).

**Access and safety**
* Quote-version read (`GET /v1/quote-versions/{id}`) now checks the caller may see the deal (§3); it leaked
  across salespeople before. Change-request and export lists apply the same `visible_to` filter.
* `handover.reply` / `call_buyer`: the "who may contact the buyer" check (§14 — admin never does, MD only on
  takeover) now runs **before** the "sender/caller configured" check, so authorisation no longer depends on
  infrastructure being present (admin got 503 instead of 403).
* Website `_ip` reads the **last** X-Forwarded-For entry (the ALB's own append); earlier entries are
  client-supplied and were spoofable for the per-IP rate limit.
* Opt-out reason counts on the manager dashboard de-duplicate per stop (one `(contact, at)`), not per consent
  row — a scope-`all` stop writes four rows and was counted 4×.

**Degrade and spend (D34/D35)**
* Model tiers corrected to D27: Haiku (FAST) on non-money roles, Sonnet (SMART) on every price-quoting role.
* AI spend cap: at 100 % of the daily AI budget a turn hands over **before** any model call (`budget_left`).
* WhatsApp-template cap: buyer templates are held at 100 % (`spend_cap`, a transient reason the caller
  retries); staff alerts are never held; each template actually sent is counted.
* Kill-switch `text_only` and an exhausted voice-minute budget now block the website voice token.
* Price-guard block now hands the deal to the salesperson (not just a safe reply).
* Metric names are bare (`VendorSwitch`, `SpendCap`) so the CloudWatch alarms in `infra/modules/observability`
  match what the app emits; a `SpendCap` alarm was added.

**Config and ops**
* PDF renderer: the container wires headless Chromium first, WeasyPrint as the backup (D37); without the
  Chromium binary (a developer laptop) it logs once and uses WeasyPrint only.
* Postmark inbound env key corrected (`inbound_username`); stale Telnyx/audio-bucket env vars removed.
* `.env.example`: added `KX_PROCESS`, `KX_API_PORT`, `KX_HEALTH_PATH`; marked `KX_AUTH__COOKIE_SECURE=false`
  as a local override. `docker/healthcheck.py` falls back to `KX_APP_API_PORT`.

**New features filling §-gaps the audit flagged partial/missing**
* §8.1 step 4: `POST /v1/deals/{id}/opt-out` — a salesperson records a buyer's stop (and their own reason),
  add-only, same effect as a STOP.
* §20.7 change history: `GET /v1/products/{id}/history` and `GET /v1/price-list/history` read the audit rows.
* §7A.3 team board: `unassigned=true` and `escalated=true` filters on the deals board.
* §21 scheduled reports: a daily summary and a weekly report to the manager and MD (`kx.crm.services.
  scheduled_reports`, Beat `crm.scheduled_reports` every 15 min; times/recipients are settings, `crm_0011`
  seed). The manager-board figures moved to `kx.crm.services.dashboards` so the report and the screen agree.

**Tests added:** 27 — root unit 17 (tiers, X-Forwarded-For, VendorOrder, metric/alarm names), CRM 7 (opt-out,
change history, team board, scheduled reports, export visibility, quote-version access, admin-never-contacts),
automation 2 (template cap, staff bypass + count), agents 1 (AI budget hand-over).

Totals after the audit pass: **665 passed** (root 251, CRM 161, automation 164, agents 89); ruff, ruff format,
mypy --strict (per package) and import-linter (3 contracts) clean. New migration `crm_0011` (report settings).
