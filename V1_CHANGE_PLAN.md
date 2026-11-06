# v1 change plan: bring the code in line with the 5 Nov business flow

**Status:** the code in this repo (309 tests green) implements the 4 Nov rules. On 5 Nov the business flow changed:
- the salesperson approves quotes, not the manager/MD;
- pricing comes from a manual price list, not cost + markup;
- change requests go to the salesperson;
- delivery calculation and scraping move to v2.

This file is the work list to update the code. Source of truth: `docs/BUSINESS_FLOW_V1.md` (section numbers below are "§" references to it).

Work in the order below. Each work package (WP) ends with **Done when** checks. Keep the rules in `CONTRACTS.md`:
- packages never import each other;
- shared helpers come from `kx.core`;
- money is Decimal only;
- every state change writes audit + timeline + outbox in one transaction (`kx.crm.services.journal.record`).

## 0. Decisions already made (Hamthan, 5 Nov)

| # | Decision |
|---|---|
| D1 | The assigned **salesperson** approves the 1st quote, every revised quote and the final quote. There is no second check. The manager can approve any deal. The MD approves only deals it has taken over |
| D2 | There is no "sales manager" role, only a **manager**. CRM roles: `md`, `manager`, `admin`, `sales` (= salesperson) |
| D3 | When a buyer pushes below the minimum, the **salesperson is alerted and the manager notified**. A special price below the minimum needs the **manager's** approval (proposed in §26; confirm) |
| D4 | Reminders: WhatsApp at 30 min, alert call at 60 min, escalate to the manager at 120 min, counted in alert hours 08:00–22:00 Asia/Dubai. Each staff user's preferred alert channels are a per-user setting (to confirm with the client) |
| D5 | Assignment: round-robin by default. Optional rules match the buyer's language, country and product category. Away status and a backup person are respected. The manager or MD can reassign. The MD can take over and hand back; during a takeover the salesperson is read-only |
| D6 | Price list: per product and quantity tier, a **list price** (top) and a **minimum** (floor). Optional minimum % per category, which a per-product value overrides. Prices are in AED with daily FX conversion (manager override). VAT 5% for the UAE, 0% for export. Validity 7 days; Incoterm EXW by default |
| D7 | Concessions: at most 3 equal steps from list to minimum. Step 1 can be goodwill; steps 2–3 need a bigger quantity. A first ask below the minimum gets the next step; an insisted ask below the minimum pauses the AI (§12.2) |
| D8 | Custom, printed or unpriced products: the AI collects the requirements, and the salesperson prices them by hand. The AI never negotiates them |
| D9 | The "open to discuss" line goes in the **message only** (not the PDF). It must sound natural and vary |
| D10 | Delivery time on the quote = the product's default ("X–Y working days after advance payment"), editable by the salesperson. No calculation in v1 |
| D11 | Email is sent from one company address. **WhatsApp uses one number per region**. Messages are signed with the assigned salesperson's name |
| D12 | Change requests (product, spec, print, quantity outside the tiers or below MOQ, delivery, samples, other) go to the salesperson. The AI tells the buyer the update comes "within 4 working hours" (setting). A quantity change *inside* the tiers is repriced by the AI, but the revised quote still needs the salesperson's approval |
| D13 | Payment is **never** handled by the AI. After the final quote is approved: alert the salesperson, notify the manager, and the AI stops replying on that deal |
| D14 | v2, switched off in v1: price scraping, cost build-up pricing, the 3/7-day staleness block, delivery-time calculation, the news digest, the ERP link, re-order follow-up and the repeat-sales agent. **Keep that code**, behind flags |
| D15 | v1 stores `expected_reorder_date` at WON (for v2). LOST is proposed 14 days after the last follow-up (setting); the salesperson confirms |
| D16 | Effort owner: Hamthan (approvals, change requests, pricing tiers) |

## WP1. Database migration 0002 (Alembic, one revision per DB)

Add `apps/arifaa-pack/src/arifaa_pack/migrations/<db>/versions/0002_v1_flow.py`. **Do not edit the 0001 DDL files.** The baseline is already applied.

**crm**

- **approvals:**
  - `required_role` CHECK becomes `IN ('sales','manager','md')`;
  - **drop** the CHECK `kind <> 'final' OR required_role = 'md'`;
  - add the `kind` value `'special_price'` (manager only);
  - add the column `approver_user_id uuid` (the deal owner at submit time).
- **users:** add
  - `languages text[]`, `countries text[]`, `category_codes text[]`;
  - `is_away bool default false`, `backup_user_id uuid`;
  - `alert_channels text[] default '{whatsapp}'`;
  - `round_robin_seq bigint`.
- **deals:** add
  - `owner_user_id` (if missing);
  - `taken_over_by uuid`, `taken_over_at`;
  - `ai_paused_reason` allows `'handover'` and `'concessions_exhausted'`;
  - `low_ask_count int default 0` per open negotiation.
- **New `product_price_tiers`:**
  - columns: `id`, `product_id`, `min_qty`, `max_qty` (null = no upper limit), `list_price numeric(14,6)`, `min_price numeric(14,6)`, `currency text default 'AED'`, `valid_from timestamptz`, `created_by`;
  - CHECK `min_price <= list_price`;
  - exclusion constraint so a product's tiers never overlap.
- **product_categories:** add `min_discount_pct numeric(5,2)` (the shortcut).
- **products:** add `moq`, `default_lead_days_min`, `default_lead_days_max`, `custom_printing bool`, `is_quotable bool`.
- **New `change_requests`:**
  - columns: `id`, `deal_id`, `quote_version_id`, `type` (CHECK in product/spec/printing/quantity/delivery/samples/payment/other), `buyer_text`, `buyer_text_en`, `fields jsonb` (`[{field, current, requested}]`), `status` (open/in_progress/resolved/declined), `assigned_to`, `sla_due_at`, `resolved_version_id`, `decline_reason`, timestamps.
- **quote_versions:** add `delivery_time_text`, `incoterm`, `delivery_place`, `payment_terms_text`, `fx_rate`, `fx_rate_date`, `signed_by_user_id`.
- **orders:** add `expected_reorder_date date`.
- **contacts:** add a generated `recipient_hash` column plus an index (from the DDL v1.1 list); `source` allows `'phone'`.
- **Settings seed:**
  - `pricing.mode = 'price_list'`;
  - `quote.vat_pct_domestic = 5`, `quote.validity_days = 7`, `quote.default_incoterm = 'EXW'`;
  - `negotiation.max_steps = 3`;
  - `change_request.sla_working_hours = 4`;
  - `approval.reminder_minutes = [30,60,120]`;
  - `approval.md_value_limit_aed = null` (off);
  - `followup.lost_after_days = 14`;
  - `assignment.rules = []`;
  - `features.scraping = false`, `features.delivery_calc = false`, `features.news_digest = false`.

**automation**

- **channel_accounts:** add `region text` and `countries text[]` (WhatsApp number per region).
- **outbound_messages:** add `template_params jsonb`, `contact_ref`, `recipient_hash`; statuses `bounced`, `complained`.
- **call_attempts:** `UNIQUE (approval_ref, attempt_no)`.

**agents**

- **turns:** add `run_id`.
- **conversations:** add `summary_upto_turn`.

**Done when:**
- `kx migrate` runs twice on fresh DBs and on the 0001 state;
- the ORM models in each package match (`kx.crm` / `kx.automation` / `kx.agents` have column-vs-`information_schema` tests; extend them).

## WP2. Pricing: price-list mode (`kx.crm.pricing`)

- **New pure module `pricing/price_list.py`:**
  - `resolve_tier(tiers, qty) -> TierPrice(list_price, min_price, tier_id)`. Raises `LookupError` if no tier fits, which the caller turns into a change request (below MOQ / outside the tiers).
  - `min_price_for(tier, category_min_pct)`: the per-product value wins.
- **Add to `pricing/engine.py`:**
  - `ladder_between(top, floor, steps) -> tuple[Decimal, ...]`: equal steps, 4 dp;
  - an `evaluate_counter` variant that takes that ladder and `low_ask_count`, applying §12.2 rules 1–5.
  - Keep the cost-based functions untouched (they are for v2).
- **`services/pricing.py`:** when `pricing.mode='price_list'`:
  - build the lines from tiers;
  - apply FX (`fx_rates`, manager override) and VAT;
  - skip the staleness block.
- **New spec file `tests/spec/test_spec_price_list.py`, using §11–12 and scenario numbers:**
  - 50,000 pcs at AED 0.25: line 12,500.00, VAT 625.00, total 13,125.00; USD at 3.6725 = 3,403.68;
  - ladder 0.2500 / 0.2433 / 0.2367 / 0.2300;
  - scenario 2: 70,000 × 0.2367 = 16,569.00;
  - first ask below the minimum gets the next step; an insisted ask below the minimum gives NEEDS_MANAGER;
  - moving to a higher tier gives the new list price plus `requires_approval=True`;
  - a property test that no offer ever goes below the minimum.

**Done when:** the new spec file passes, the old pricing specs still pass, and `NegotiationState` is no longer built from cost when in price-list mode.

## WP3. Approvals: salesperson approves (`services/approvals.py`, `quotes.py`, `issuing.py`)

- **Approver:**
  - the approver is the deal owner (role `sales`), for every quote: `initial`, `revision`, `counter` and `final`;
  - `manager` may approve any deal;
  - `md` only when `deals.taken_over_by` is set.
- **Takeover:** while a takeover is active, the owner is read-only (403).
- **MFA:** keep `require_human_mfa`.
- **Special price:** a `special_price` approval (manager) is created when the salesperson proposes below the minimum. Once approved, it unlocks that price for that quote version only.
- **Events:** the `QuoteSubmitted` payload's `approver` = the salesperson and `escalate_to` = the manager. Due time = `Window.add_open_minutes` with the 120-minute escalation.
- **Remove** the MD value-limit routing, unless `approval.md_value_limit_aed` is set.
- **Grants:** still issue a single-use grant for every buyer-facing version (unchanged).

**Done when:** the DB tests prove each of these:
- the salesperson can approve their own deal, but not another salesperson's;
- the manager can approve any deal;
- the MD can approve only after a takeover;
- the owner can't edit during a takeover;
- a final quote approved by the salesperson issues a grant.

## WP4. Change requests (CRM + contracts + agents)

- **kx.core:**
  - add event `CHANGE_REQUESTED = "change_request.raised.v1"` with an `event_payloads.ChangeRequested` payload (deal_ref, change_request_ref, type, assigned_to as StaffContact, sla_due_at);
  - add `CrmGateway.raise_change_request(deal_id, type, buyer_text, fields, actor) -> UUID`.
- **kx.crm:** `services/change_requests.py`:
  - raise (with SLA due = now + 4 working hours, using `Window`), start, resolve (link the new version), decline;
  - API routes for the inbox and the card;
  - `GatewayImpl` implements the new method.
- **kx.agents:**
  - new tool `raise_change_request`;
  - in `graphs/deal.py`, any non-price intent, or a quantity outside the tiers, raises a change request;
  - the reply uses the fixed phrase "within 4 working hours" (a PromptStore phrase).
- **kx.automation:** a handler for `CHANGE_REQUESTED` sends an immediate alert and starts the reminder ladder (reuse `ApprovalLadder` with a different script).

**Done when:**
- a deal-graph test shows "Can you deliver in 5 days?" creates a change request, with no promise in the reply;
- a CRM DB test covers the full lifecycle;
- an automation test shows the alert and the reminders.

## WP5. Assignment, away/backup, reassign, takeover (`services/assignment.py`, new)

- Round-robin over active `sales` users, with an atomic `round_robin_seq` update.
- Optional rules from `assignment.rules`, checked in order: language → country → category.
- An away user is replaced by their backup, or by the manager if there is no backup.
- A returning customer goes to their last owner, if that person is active.
- APIs: reassign (manager/MD), take over and hand back (MD). Both users are notified, and the timeline records it.

**Done when:** unit tests cover the rule order and away/backup, and DB tests cover concurrent assignment with no duplicates.

## WP6. Sending the quote (automation + CRM PDF)

- Pick the WhatsApp `channel_account` by the buyer's country (`countries[]`), with a fallback to the default region. Email always uses the company address.
- Messages are signed with the owner's name.
- The "open to discuss" line is written by the AI communication agent **in the message**, in natural, varied wording. It is not in the PDF.
- The PDF template shows `delivery_time_text`, Incoterm and place, payment terms, and the FX rate line, with Arabic laid out right-to-left.

**Done when:** a regional routing test passes, the PDF renders the new fields, and the guard still passes on the generated message.

## WP7. Final quote and handover

- On final approval:
  - emit `QUOTE_APPROVED` (kind final, as today);
  - the automation handler also alerts the salesperson and notifies the manager;
  - the CRM sets `ai_enabled=false` with `ai_paused_reason='handover'`.
- After handover the orchestrator routes buyer messages to the salesperson inbox. Outside working hours it sends the automatic acknowledgement only.

**Done when:** an orchestrator test shows a handed-over deal gets no AI reply, and an automation test covers the alert plus the manager notice.

## WP8. Payment, WON, LOST, order

- **WON:** requires a recorded advance and `expected_reorder_date`.
- **LOST:** reasons per §15.
- **Job:** proposes LOST `followup.lost_after_days` after the last follow-up (a task for the salesperson; never automatic).

## WP9. Switch v2 features off (flags; keep the code)

| Feature | What changes in v1 |
|---|---|
| `features.scraping=false` | Do not register the scraper periodic job |
| Staleness check | Skipped in price-list mode |
| `features.delivery_calc=false` | The agents' `delivery_estimate` tool returns the product's default lead-time text |
| `features.news_digest=false` | Off |
| Repeat-sales agent | Disabled in the registry |

**Done when:** the app boots with the flags off, and the smoke test asserts the scraper job is not registered.

## WP10. AI behaviour updates (`kx.agents`)

- **Negotiator prompt and policy:** §12.2 rules. Never payment, never longer delivery, never reveal the minimum. Custom items always go to the salesperson.
- **Fixed phrases:**
  - below the minimum: "within 60 minutes in working hours";
  - change requests: "within 4 working hours";
  - after handover: an outside-hours acknowledgement.
- **Evals:** replace the YAML scenarios with the 10 scenarios in §23, plus the trick-prompt cases.

## WP11. CRM API for the screens (§20)

Endpoints:
- inbox (approvals, change requests, below-minimum, handed-over messages, with SLA timers);
- change-request card actions;
- price list CRUD;
- **Excel import/export** (`openpyxl`; validate min ≤ list, no overlaps, codes exist; return row errors);
- stock table;
- reassign, takeover and hand-back;
- settings for the new keys.

Keep the routers thin; the services do the work.

## WP12. Notifications matrix (§17)

Map every row of §17 to an event plus a handler in `kx.automation.handlers`, and add a table-driven test: event → expected recipients and channels.

## Final check (same as CI)

`make lint && make test`:
- ruff, format, mypy strict and import-linter clean;
- all tests green on Postgres;
- then update `docs/DELIVERY.md` with the new test counts.
