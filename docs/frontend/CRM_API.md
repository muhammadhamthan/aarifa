# CRM API: frontend hand-off

For the developer building the **CRM screens** (staff web app, desktop and mobile) against a mock first, then
the real API. Everything below is taken from the code (`packages/kx-core-crm/src/kx/crm/api/`,
`kx/automation/admin_api.py`) and the business flow (SRS §3, §7A, §14, §15, §20, §21). The endpoint reference
in the second half is **generated from the code** (FastAPI OpenAPI), so names, types and limits are exact.

Companion files in this folder:

* `openapi-crm.json`: the OpenAPI 3.1 document of every CRM endpoint (import into Postman, or mock it, below).
* `WEBSITE_API.md`: the public website API. `CMS.md`: the content side.

---

## 1. Working against a mock

```bash
# any OpenAPI mock server works; for example Prism:
npx @stoplight/prism-cli mock docs/frontend/openapi-crm.json --port 4010
# then point the app at http://localhost:4010 (the paths below start with /v1)
```

The mock returns example data shaped exactly like the real responses. Two things a mock cannot reproduce:
the login cookie (section 3) and the business refusals (section 6); build those screens from this document.

Real API locally: `make up && kx migrate && kx api` (see `README.md`), base URL `http://localhost:8000`.
Create the first user with `kx create-user --role admin --email you@example.com --name "You"`.

---

## 2. What the CRM is (core design)

The CRM is where Arifa's team works the deals the AI starts. The AI talks to buyers on WhatsApp, email and the
website; **the backend builds every quote** (§11.1) and **a person approves it** before anything priced goes
out. The screens never compute prices: they show what the API returns and send decisions back.

| Role (`role` in the token) | What they do (SRS §3) |
|---|---|
| `sales` | Owns assigned deals. Approves 1st, revised and final quotes on their deals. Handles change requests, payment, PO, order updates. Records WON / LOST. Sees **only their own deals** (and those of an away colleague they cover). |
| `manager` | Price list and catalogue, assigns / reassigns, below-minimum decisions, sees everything. |
| `md` | Sees everything, dashboards, can take over any deal. **The MD passes every check that allows `manager`.** |
| `admin` | Users, roles, settings, channels (WhatsApp numbers, templates), kill switch. |

Screens (SRS §20) and the endpoints behind them:

| Screen | Endpoints |
|---|---|
| 1 Login (two-step) | `POST /v1/auth/login`, `/v1/auth/mfa/totp`, `/v1/auth/mfa/email/send`, `/v1/auth/mfa/email/verify`, `/v1/auth/refresh`, `/v1/auth/logout`, `GET /v1/auth/me`, TOTP enrolment `/v1/auth/totp/*`, password `/v1/auth/password*` |
| 2 My inbox (SLA timers) | `GET /v1/inbox` (everyone's own items); managers also `GET /v1/approvals` |
| 3 Deals board | `GET /v1/deals` (filters: stage, owner, country, product, sku, value range, opted_out; cursor pages) |
| 4 Deal page | `GET /v1/deals/{id}` (with `ai_summary` at the top, §7A.2), `/timeline`, `/quotes`, `POST /stage`, `/reopen`, `/ai`, `/reassign`, `/takeover`, `/handback`, `/messages` (reply to the buyer), `/call` (click-to-call), `/payments`, `/won`, `/personal-contact-shared`, `PATCH /v1/deals/orders/{id}` |
| 5 Quote approval (§11.3) | `GET /v1/quote-versions/{id}`, `POST /v1/approvals/{id}/decision`, `POST /v1/deals/{id}/quotes`, `/quotes/revise`, `/quotes/manual` |
| Change requests (§13) | `GET /v1/change-requests`, `/{id}`, `POST /{id}/start`, `/revise`, `/resolve`, `/decline` |
| 6 Contacts and companies | `GET/PATCH /v1/contacts…`, `GET/POST/PATCH /v1/companies…` |
| 7 Catalogue and price list | `GET /v1/products`, `PATCH /v1/products/{id}`, price tiers `/v1/products/{id}/price-tiers`, `/v1/price-tiers/{id}`, Excel `POST /v1/price-list/import`, `GET /v1/price-list/export` |
| 8 Stock | `GET/PUT /v1/stock` |
| 9 Orders | `GET /v1/orders`, `PATCH /v1/deals/orders/{id}` |
| 10 Dashboards (§21) | `GET /v1/dashboards/salesperson`, `/manager`, `/md`, `GET /v1/dashboard/summary` |
| 11 Settings | `GET/PUT /v1/settings…`, `PUT /v1/feature-flags/{key}`, FX `GET/POST /v1/fx-rates`, users `/v1/users…`, availability `PUT /v1/users/{id}/availability`, WhatsApp numbers `/v1/admin/channel-accounts`, templates `/v1/admin/message-templates`, kill switch `/v1/admin/kill-switch`, material prices `/v1/price-proposals…`, `/v1/materials/freshness` |
| 12 Exports | `GET /v1/exports/{deals|contacts|companies|orders|products}?format=csv|xlsx` |

Deal stages (`stage`): `NEW`, `QUALIFIED`, `QUOTATION`, `NEGOTIATION`, `READY_TO_CLOSE`, `PAYMENT`, `WON`, `LOST`.
LOST reasons (`reason` when moving to LOST): `price_too_high`, `delivery_not_suitable`, `bought_competitor`,
`product_unavailable`, `no_response_postponed`, `budget`, `duplicate`, `other`, `opted_out`.

Rules the screens must respect:

* **Approvals, price-list edits, settings, payments, WON, reopen** need a session signed in with the
  authenticator app (`mfa=true` in the login answer). An email-code session gets `403` there: show "sign in
  with your authenticator app to approve".
* **No cost or margin on the v1 screens** (§3: "See cost and margin data: not in v1"). The endpoint
  `GET /v1/quote-versions/{id}/costs` exists for the MD only; leave it out of v1 screens unless asked.
* **Opted out** (`opted_out_at` set on the deal): show the badge; the API refuses messages to that buyer.
* **Optimistic concurrency**: stage moves send the `expected_version` they displayed; `409` = someone changed
  the deal, reload it.
* **After hand-over (§14)** the salesperson replies from the deal page on the same company number:
  `POST /v1/deals/{id}/messages` with a `client_key` (a UUID you generate per message, so a double tap sends
  once). A `422` with `reason: "window_closed"` means WhatsApp's 24-hour window has closed: offer the button
  "Send the re-open message" which resends with `reopen_template: true`. A `422` with
  `reason: "unapproved_price"` means the text names a price that is not in an approved quote (`amounts` lists
  them): tell the salesperson to revise the quote instead.
* **Click-to-call** rings the salesperson's own phone first, then the buyer, from the company number. Needs
  the salesperson's phone in their profile (`422 no_staff_phone`); refused if the buyer said "don't call me"
  (`422 opt_out`).

---

## 3. Sign-in, tokens, sessions (D26)

1. `POST /v1/auth/login {email, password}` -> `{challenge_token, methods, totp_enrolled, expires_at}`.
   `methods` is `["totp","email"]` once the authenticator is set up, `["email"]` before.
2. Second step, either:
   * `POST /v1/auth/mfa/totp {challenge_token, code}` -> tokens with `mfa: true`; or
   * `POST /v1/auth/mfa/email/send {challenge_token}` (204; a 6-digit code goes to the work email, valid
     10 minutes), then `POST /v1/auth/mfa/email/verify {challenge_token, code}` -> tokens with `mfa: false`.
3. Answer (`TokenOut`): `access_token` (RS256 JWT, 15 minutes), `expires_in`, `mfa`, `amr`,
   `must_change_password`, `totp_enrolled`, `user`. Send `Authorization: Bearer <access_token>` on every call.
4. The refresh token is set as an **HttpOnly cookie `kx_refresh`** (path `/v1/auth`, SameSite=Lax, Secure in
   production). Call `POST /v1/auth/refresh` (with `credentials: "include"`) before the access token expires;
   each refresh **rotates** the cookie. A reused old refresh token ends every session of that user: on `401`
   from refresh, send the user to the login screen. Non-browser clients may send `{"refresh_token": ...}`.
5. `must_change_password: true` (a new user's one-time password): force `POST /v1/auth/password` first.
   `totp_enrolled: false`: offer `POST /v1/auth/totp/enrol` (returns `secret` + `otpauth_uri` to show as a QR
   code), then `POST /v1/auth/totp/confirm {code}`; the next sign-in can use the authenticator.
6. 5 wrong passwords or codes lock the account for 15 minutes (`429`). Forgotten password:
   `POST /v1/auth/password/reset/start {email}` (always `202`), then `/reset/finish {email, code, new_password}`.

Public key for verifying tokens elsewhere: `GET /.well-known/jwks.json`.

---

## 4. Conventions

* Base path `/v1`; JSON in and out; UTF-8. IDs are UUIDs. Times are ISO 8601 in UTC (`...Z` / `+00:00`);
  show them in the user's time zone (Arifa: Asia/Dubai). Dates are `YYYY-MM-DD`.
* **Money is a decimal string** (`"12500.00"`), never a float; keep it as a string or use a decimal library.
  Currency codes are ISO 4217 (`AED`, `USD`, `SAR` ...).
* Lists that can grow are **cursor pages**: `{items: [...], next_cursor: "..." | null}`; pass `cursor` and
  `limit` (1-100) to get the next page. Smaller lists are plain arrays.
* There is no push channel to the CRM in v1: refresh the inbox and the open deal by polling (for example every
  30 seconds while visible). Staff also get WhatsApp / email alerts (§16).

---

## 5. Exports and files

* `GET /v1/exports/{list}?format=csv|xlsx` returns the file (`Content-Disposition: attachment`).
* `GET /v1/price-list/export` returns the price list as `.xlsx`. `POST /v1/price-list/import` takes the
  `.xlsx` file itself as the request body (columns `sku, min_qty, max_qty, list_price, min_price, currency`).
  All rows are checked first: any error saves nothing and the answer lists the row errors (§10).
* Quote PDFs are produced and sent by the backend; the deal page shows them through the quote versions.

---

## 6. Errors

Business errors are RFC 9457 `application/problem+json`:

```json
{
  "type": "https://errors.kairox.ai/rule_violation",
  "title": "Business rule violated",
  "status": 422,
  "detail": "a price that is not in an approved quote cannot be sent; revise the quote instead",
  "instance": "/v1/deals/0190.../messages",
  "reason": "unapproved_price",
  "amounts": ["AED 9.50"]
}
```

| Status | `type` ends with | Meaning for the screen |
|---|---|---|
| 401 | `unauthorized` | Not signed in / token expired: refresh, else go to login |
| 403 | `forbidden` | Wrong role, someone else's deal, MFA needed, or the MD has taken the deal over |
| 404 | `not_found` | Gone or not visible to this user |
| 409 | `conflict` | Changed by someone else: reload |
| 422 | `rule_violation` | A business rule refused it: show `detail`; switch on `reason` where listed above |
| 429 | `too_many_requests` | Locked out / rate-limited: wait |
| 503 | `unavailable` | A dependency (email, calls) is down: try again later |

Request-shape errors (a missing or wrong field) come from the framework as `422` with
`{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`: show them next to the fields.

---

## CRM endpoint reference

### admin

#### `GET /v1/price-proposals`

Material price proposals (from the v2 scraper or staff) waiting for the manager's decision.

**Who:** manager, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `status` | query | `proposed` \| `approved` \| `rejected` \| `superseded` | no | default `"proposed"` |
| `cursor` | query | string or null | no |  |
| `limit` | query | integer | no | ≥ 1; ≤ 100; default `50` |

**Response 200:** `Page_RateOut_`

---

#### `POST /v1/price-proposals/{rate_id}/approve`

Manager (MFA): accept a proposed material rate (may be corrected); it becomes the current rate.

**Who:** manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `rate_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `RateDecisionIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `rate_per_unit` | number or string or null | no |  |
| `note` | string or null | no |  |

**Response 200:** `RateOut`

---

#### `POST /v1/price-proposals/{rate_id}/reject`

Manager (MFA): reject a proposed material rate.

**Who:** manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `rate_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `RateDecisionIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `rate_per_unit` | number or string or null | no |  |
| `note` | string or null | no |  |

**Response 200:** `RateOut`

---

#### `GET /v1/materials/freshness`

How old each material's approved rate is (stale rates block pricing).

**Who:** manager, md

**Response 200:** list of `FreshnessOut`

---

#### `GET /v1/settings`

§20.11 business settings (timers, follow-up days, VAT, Incoterm, validity, hours, opt-out rules ...).

**Who:** admin, manager, md

**Response 200:** list of `SettingOut`

---

#### `PUT /v1/settings/{key}`

Manager / admin (MFA): change one business setting (validated; logged).

**Who:** admin, manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `key` | path | string | yes |  |

**Request body** (`application/json`): `SettingIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `value` | any | yes |  |

**Response 200:** `SettingOut`

---

#### `PUT /v1/feature-flags/{key}`

Manager / admin (MFA): switch a feature on or off (v2 features stay off in v1, D14).

**Who:** admin, manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `key` | path | string | yes |  |

**Request body** (`application/json`): `FlagIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `enabled` | boolean | yes |  |

**Response 204:** (no body)

---

#### `PUT /v1/users/{user_id}/availability`

§9.3: away / holiday with a backup (the manager, an admin, or the user themself).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `user_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `AvailabilityIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `is_away` | boolean | yes |  |
| `backup_user_id` | string (uuid) or null | no |  |

**Response 200:** `UserOut`

---

#### `GET /v1/dashboard/summary`

Compact pipeline summary: deals by stage, open pipeline value, approvals pending / overdue, open tasks.

**Who:** manager, md

**Response 200:** `DashboardSummary`

---

#### `GET /v1/admin/kill-switch`

D35 kill switch: the current level and what it stops.

**Who:** admin, manager, md

**Response 200:** `KillSwitchOut`

---

#### `PUT /v1/admin/kill-switch`

Admin or MD (MFA): set the kill-switch level (off, marketing, calls, text_only, hold); logged.

**Who:** admin, md (signed in with the authenticator app: `mfa=true`)

**Request body** (`application/json`): `KillSwitchIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `level` | `off` \| `marketing` \| `calls` \| `text_only` \| `hold` | yes |  |

**Response 200:** `KillSwitchOut`

---

### admin: channels

#### `GET /v1/admin/channel-accounts`

§20.11 company WhatsApp numbers and email addresses, with the countries each number serves.

**Who:** admin, manager, md

**Response 200:** list of `ChannelAccountOut`

---

#### `PATCH /v1/admin/channel-accounts/{account_id}`

Regional routing for WhatsApp numbers: which countries a number serves, the default number.

**Who:** admin (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `account_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `ChannelAccountPatch`

| Field | Type | Required | Notes |
|---|---|---|---|
| `display_name` | string or null | no |  |
| `region` | string or null | no |  |
| `countries` | list of string or null | no |  |
| `is_default` | boolean or null | no |  |
| `status` | `active` \| `disabled` or null | no |  |

**Response 200:** `ChannelAccountOut`

---

#### `GET /v1/admin/message-templates`

§20.11 message templates and Meta's approval state (only approved WhatsApp ones can be sent).

**Who:** admin, manager, md

**Response 200:** list of `TemplateOut`

---

#### `PUT /v1/admin/message-templates`

Add or change a template. A WhatsApp template waits for Meta's approval (`submitted`).

**Who:** admin (signed in with the authenticator app: `mfa=true`)

**Request body** (`application/json`): `TemplateIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `channel` | `whatsapp` \| `email` | yes |  |
| `name` | string | yes | pattern `^[a-z0-9_]{1,64}$` |
| `language` | string | yes | min length 2; max length 16 |
| `category` | `utility` \| `marketing` \| `authentication` \| `internal` | no | default `"utility"` |
| `subject` | string or null | no |  |
| `body` | string | yes | min length 1; max length 4000 |
| `variables` | list of string | no | max items 20 |

**Response 200:** `TemplateOut`

---

#### `POST /v1/admin/message-templates/{template_id}/status`

Record Meta's decision on a WhatsApp template (only `approved` templates can be sent).

**Who:** admin (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `template_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `TemplateStatusIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `provider_status` | `draft` \| `submitted` \| `approved` \| `rejected` \| `paused` | yes |  |

**Response 200:** `TemplateOut`

---

### auth

#### `POST /v1/auth/login`

Step 1: email + password. Answers the second steps on offer (TOTP first; email code fallback).

**Who:** public

**Request body** (`application/json`): `LoginIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | string | yes | min length 3; max length 320 |
| `password` | string | yes | min length 1; max length 256 |

**Response 200:** `ChallengeOut`

---

#### `POST /v1/auth/mfa/totp`

Step 2 with the authenticator app: `mfa=true` (needed for approvals, price edits, settings).

**Who:** public

**Request body** (`application/json`): `CodeIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `challenge_token` | string | yes | min length 10; max length 200 |
| `code` | string | yes | min length 6; max length 10 |

**Response 200:** `TokenOut`

---

#### `POST /v1/auth/mfa/email/send`

Fallback second step: a 6-digit code to the staff member's work address (valid 10 minutes).

**Who:** public

**Request body** (`application/json`): `ChallengeIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `challenge_token` | string | yes | min length 10; max length 200 |

**Response 204:** (no body)

---

#### `POST /v1/auth/mfa/email/verify`

Step 2 with the email code: signed in with `mfa=false` (no approvals until TOTP is used).

**Who:** public

**Request body** (`application/json`): `CodeIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `challenge_token` | string | yes | min length 10; max length 200 |
| `code` | string | yes | min length 6; max length 10 |

**Response 200:** `TokenOut`

---

#### `POST /v1/auth/refresh`

A new access token. The refresh token rotates; a reused old one ends all the user's sessions.

**Who:** public

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `kx_refresh` | cookie | string or null | no |  |

**Request body** (`application/json`): `RefreshIn` or null

**Response 200:** `TokenOut`

---

#### `POST /v1/auth/logout`

End this browser's session: the refresh token is revoked and its cookie cleared.

**Who:** public

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `kx_refresh` | cookie | string or null | no |  |

**Response 204:** (no body)

---

#### `GET /v1/auth/me`

The signed-in staff member (name, role, languages, alert channels) for the screen header.

**Who:** any signed-in staff

**Response 200:** `StaffUserOut`

---

#### `POST /v1/auth/totp/enrol`

A new authenticator seed for the signed-in user (active only after /totp/confirm).

**Who:** any signed-in staff

**Response 200:** `TotpEnrolOut`

---

#### `POST /v1/auth/totp/confirm`

The first code from the app switches TOTP on. Sign in again to get an MFA session.

**Who:** any signed-in staff

**Request body** (`application/json`): `TotpConfirmIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `code` | string | yes | min length 6; max length 10 |

**Response 204:** (no body)

---

#### `POST /v1/auth/password`

Change the password (ends every other session).

**Who:** any signed-in staff

**Request body** (`application/json`): `PasswordChangeIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `current_password` | string | yes | min length 1; max length 256 |
| `new_password` | string | yes | min length 12; max length 256 |

**Response 204:** (no body)

---

#### `POST /v1/auth/password/reset/start`

Recovery: a code to the work address. Always 202 (nobody learns which addresses exist).

**Who:** public

**Request body** (`application/json`): `ResetStartIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | string | yes | min length 3; max length 320 |

**Response 202:** (no body)

---

#### `POST /v1/auth/password/reset/finish`

Recovery step 2: the emailed code + a new password (min 12 characters); every session ends.

**Who:** public

**Request body** (`application/json`): `ResetFinishIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | string | yes | min length 3; max length 320 |
| `code` | string | yes | min length 6; max length 10 |
| `new_password` | string | yes | min length 12; max length 256 |

**Response 204:** (no body)

---

#### `GET /.well-known/jwks.json`

The public key that verifies staff access tokens (RS256, D26).

**Who:** public

**Response 200:** object

---

### change-requests

#### `GET /v1/change-requests`

Open cards with SLA timers, oldest due first. A salesperson sees the cards of their own deals.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `status` | query | `open` \| `closed` | no | default `"open"` |
| `deal_id` | query | string (uuid) or null | no |  |
| `limit` | query | integer | no | ≥ 1; ≤ 200; default `100` |

**Response 200:** list of `ChangeRequestOut`

---

#### `GET /v1/change-requests/{cr_id}`

§13 change request card: what the buyer asked (their words), current vs requested values, SLA.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `cr_id` | path | string (uuid) | yes |  |

**Response 200:** `ChangeRequestOut`

---

#### `POST /v1/change-requests/{cr_id}/start`

Take the change request (starts work; the SLA keeps running).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `cr_id` | path | string (uuid) | yes |  |

**Response 200:** `ChangeRequestOut`

---

#### `POST /v1/change-requests/{cr_id}/revise`

"Revise quote": a new version with the edits; approving it resolves the card (§13.3-4).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `cr_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `QuoteEditsIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `lines` | list of `LineEditIn` | no | default `[]` |
| `delivery_time_text` | string or null | no |  |
| `delivery_place` | string or null | no |  |
| `incoterm` | `EXW` \| `FCA` \| `FOB` \| `CFR` \| `CIF` \| `CPT` \| `CIP` \| `DAP` \| `DDP` or null | no |  |
| `payment_terms_text` | string or null | no |  |
| `note` | string or null | no |  |
| `buyer_note` | string or null | no |  |
| `cover_note` | string or null | no |  |

**Response 201:** `QuoteVersionOut`

---

#### `POST /v1/change-requests/{cr_id}/resolve`

Close the change request as done (a revised quote was sent or the answer given).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `cr_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `ResolveIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `version_id` | string (uuid) | yes |  |

**Response 200:** `ChangeRequestOut`

---

#### `POST /v1/change-requests/{cr_id}/decline`

Decline the change request with a reason the buyer is told.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `cr_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `DeclineIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `reason` | string | yes | min length 1; max length 1000 |

**Response 200:** `ChangeRequestOut`

---

### dashboards

#### `GET /v1/dashboards/salesperson`

§21 salesperson: my open deals by stage, items waiting for me, response times, sent and won.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `user_id` | query | string (uuid) or null | no |  |
| `period_from` | query | string (date) or null | no |  |
| `period_to` | query | string (date) or null | no |  |

**Response 200:** `SalespersonBoard`

---

#### `GET /v1/dashboards/manager`

§21 manager: everyone's board, below-minimum requests, change requests by type, SLA breaches, price-list changes, plus opt-outs and the buyers' own reasons (§8.1).

**Who:** manager, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `period_from` | query | string (date) or null | no |  |
| `period_to` | query | string (date) or null | no |  |

**Response 200:** `ManagerBoard`

---

#### `GET /v1/dashboards/md`

§21 MD: pipeline by stage, win rate, average discount from list, deals by country / category / channel, top buyers, takeover queue.

**Who:** md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `period_from` | query | string (date) or null | no |  |
| `period_to` | query | string (date) or null | no |  |

**Response 200:** `MdBoard`

---

#### `GET /v1/exports/{entity}`

§20.12: a list as CSV or XLSX (a salesperson exports only their own deals and orders).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `entity` | path | `deals` \| `contacts` \| `companies` \| `orders` \| `products` | yes |  |
| `format` | query | `csv` \| `xlsx` | no | default `"csv"` |

**Response 200:** (no body)

---

### deals

#### `GET /v1/deals`

§20.3 deals board: by stage, with filters for salesperson, country, product and value (the current quote's total in AED). §7A.3 team board: `unassigned=true`, `escalated=true`. A salesperson sees only their own (and covered) deals (§3).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `stage` | query | `DealStage` or null | no |  |
| `owner_user_id` | query | string (uuid) or null | no |  |
| `country` | query | string or null | no |  |
| `product_id` | query | string (uuid) or null | no |  |
| `sku` | query | string or null | no |  |
| `min_value_aed` | query | number or string or null | no |  |
| `max_value_aed` | query | number or string or null | no |  |
| `opted_out` | query | boolean or null | no |  |
| `unassigned` | query | boolean or null | no |  |
| `escalated` | query | boolean or null | no |  |
| `cursor` | query | string or null | no |  |
| `limit` | query | integer | no | ≥ 1; ≤ 100; default `50` |

**Response 200:** `Page_DealSummary_`

---

#### `GET /v1/deals/{deal_id}`

§20.4 deal page: buyer, company, requirement, stage, AI state, AI summary (§7A.2), opt-out.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Response 200:** `DealDetail`

---

#### `GET /v1/deals/{deal_id}/timeline`

§20.4 full timeline: every message, call transcript, stage change and AI action (newest first).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |
| `cursor` | query | string or null | no |  |
| `limit` | query | integer | no | ≥ 1; ≤ 100; default `50` |

**Response 200:** `Page_TimelineOut_`

---

#### `GET /v1/deals/{deal_id}/quotes`

§20.4 quote versions, newest first (the screen compares two side by side).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Response 200:** list of `QuoteVersionOut`

---

#### `POST /v1/deals/{deal_id}/quotes`

Ask the backend to build the quote from the price list (§11.1). The AI never prices; staff approve before anything is sent.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Response 201:** `QuoteVersionOut`

---

#### `POST /v1/deals/{deal_id}/stage`

Move the deal to another stage (rules in §5; LOST needs a reason). `expected_version` guards against overwriting a newer change (409).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `StageMoveIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `to` | `DealStage` | yes |  |
| `expected_version` | integer | yes |  |
| `reason` | string or null | no |  |
| `lost_reason` | `price_too_high` \| `delivery_not_suitable` \| `bought_competitor` \| `product_unavailable` \| `no_response_postponed` \| `budget` \| `duplicate` \| `other` \| `opted_out` or null | no |  |
| `lost_note` | string or null | no |  |

**Response 200:** `DealDetail`

---

#### `POST /v1/deals/{deal_id}/quotes/revise`

§11.3 Edit then approve: a new version; below the minimum goes to the manager first (D3).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `QuoteEditsIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `lines` | list of `LineEditIn` | no | default `[]` |
| `delivery_time_text` | string or null | no |  |
| `delivery_place` | string or null | no |  |
| `incoterm` | `EXW` \| `FCA` \| `FOB` \| `CFR` \| `CIF` \| `CPT` \| `CIP` \| `DAP` \| `DDP` or null | no |  |
| `payment_terms_text` | string or null | no |  |
| `note` | string or null | no |  |
| `buyer_note` | string or null | no |  |
| `cover_note` | string or null | no |  |

**Response 201:** `QuoteVersionOut`

---

#### `POST /v1/deals/{deal_id}/ai`

Manager/MD takes over (AI off) or hands the conversation back (AI on).

**Who:** manager, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `AiToggleIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `enabled` | boolean | yes |  |
| `reason` | `human_takeover` \| `md_takeover` \| `opt_out` \| `exception` | no | default `"human_takeover"` |

**Response 200:** `DealDetail`

---

#### `POST /v1/deals/{deal_id}/offer`

§12 below-minimum alert, manager (MFA): set an approved special price (may be below the range, D3); it becomes a price the AI may state, and `resume_ai` hands the chat back to the AI.

**Who:** manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `ManagerOfferIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `unit_price` | number or string | yes |  |
| `resume_ai` | boolean | no | default `true` |

**Response 204:** (no body)

---

#### `POST /v1/deals/{deal_id}/reopen`

§7A.1 / D24: the manager (or MD) reopens a LOST deal to the stage it was lost from; the AI stays paused until a person resumes it.

**Who:** manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `ReopenIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `reason` | string | yes | min length 1; max length 500 |

**Response 200:** `DealDetail`

---

#### `POST /v1/deals/{deal_id}/personal-contact-shared`

§14 practical notes: record that the salesperson shared a personal contact with the buyer.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Response 200:** `DealDetail`

---

#### `POST /v1/deals/{deal_id}/messages`

§14 / D21: reply to the buyer on the same channel and company number, signed with the salesperson's name. Goes through the outbound gate (consent, opt-out, 24-hour rule); a price that is not in an approved quote is refused (422 `unapproved_price`). WhatsApp after 24 h of buyer silence answers 422 `window_closed`: send again with `reopen_template=true`. The AI stops replying.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `StaffMessageIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` | string | no | max length 4000; default `""` |
| `client_key` | string | yes | min length 8; max length 80 |
| `channel` | `whatsapp` \| `email` or null | no |  |
| `reopen_template` | boolean | no | default `false` |

**Response 202:** `StaffMessageOut`

---

#### `POST /v1/deals/{deal_id}/call`

§14: click-to-call through the company number. The salesperson's phone rings first, then the buyer; logged on the deal. Refused (422 `opt_out`) when the buyer asked not to be called.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `CallIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `client_key` | string | yes | min length 8; max length 80 |

**Response 202:** `CallOut`

---

#### `POST /v1/deals/{deal_id}/opt-out`

§8.1 step 4: the salesperson records a stop (and the buyer's own reason, if given) after a conversation the buyer started. Same effect as a STOP: suppression list, queued follow-ups cancelled, Opted out badge, LOST proposed. Add only; a stop is never removed here.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `OptOutIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `scope` | `all` \| `whatsapp` \| `email` \| `phone` | no | default `"all"` |
| `words` | string | yes | what the buyer said, as close as possible min length 1; max length 2000 |
| `reason` | string or null | no | only a reason the buyer gave |

**Response 200:** `DealDetail`

---

#### `POST /v1/deals/{deal_id}/payments`

§15.1 / D20: the salesperson (human, MFA) confirms the advance; every detail is optional. The deal moves to PAYMENT.

**Who:** sales, manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `PaymentIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `amount` | number or string or null | no |  |
| `currency` | string or null | no |  |
| `method` | string or null | no |  |
| `reference` | string or null | no |  |
| `paid_on` | string (date) or null | no |  |
| `note` | string or null | no |  |
| `proof_s3_key` | string or null | no |  |

**Response 201:** `PaymentOut`

---

#### `POST /v1/deals/{deal_id}/won`

§15.2: WON (mandatory) with an optional expected re-order date; the order record is created.

**Who:** sales, manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `WonIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `expected_reorder_date` | string (date) or null | no |  |

**Response 201:** `OrderOut`

---

#### `PATCH /v1/deals/orders/{order_id}`

§15.3 order tracking: in production -> ready -> shipped -> delivered (the buyer is told if configured).

**Who:** manager, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `order_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `OrderStatusIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `status` | `ready` \| `shipped` \| `delivered` \| `cancelled` | yes |  |
| `on_date` | string (date) or null | no |  |
| `tracking_ref` | string or null | no |  |

**Response 200:** `OrderOut`

---

#### `POST /v1/deals/{deal_id}/reassign`

§9.4: the manager or MD reassigns; both salespersons are notified, pending items follow.

**Who:** manager, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `ReassignIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `user_id` | string (uuid) | yes |  |

**Response 200:** `DealDetail`

---

#### `POST /v1/deals/{deal_id}/takeover`

§3: the MD takes the deal over; the salesperson is read-only until hand-back.

**Who:** md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Response 200:** `DealDetail`

---

#### `POST /v1/deals/{deal_id}/handback`

End an MD or manager takeover: the salesperson can edit and approve again (§3).

**Who:** md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Response 200:** `DealDetail`

---

#### `POST /v1/deals/{deal_id}/quotes/manual`

D8 / §23 scenario 5: a quote priced by hand by the salesperson, then approved as usual.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `deal_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `ManualQuoteIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `lines` | list of `ManualLineIn` | yes | min items 1 |
| `delivery_time_text` | string or null | no |  |
| `buyer_note` | string or null | no |  |

**Response 201:** `QuoteVersionOut`

---

### inbox

#### `GET /v1/inbox`

§20.2: approvals, change requests, below-minimum alerts, buyer messages on handed-over deals.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `limit` | query | integer | no | ≥ 1; ≤ 500; default `200` |

**Response 200:** list of `InboxItemOut`

---

### price list

#### `GET /v1/products/{product_id}/price-tiers`

§20.7 price list: the quantity tiers of one product (list price and minimum per tier).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `product_id` | path | string (uuid) | yes |  |

**Response 200:** list of `PriceTierOut`

---

#### `POST /v1/products/{product_id}/price-tiers`

Manager (MFA): add a quantity tier (every change is logged, §3).

**Who:** manager, admin, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `product_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `PriceTierIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `min_qty` | number or string | yes |  |
| `max_qty` | number or string or null | no |  |
| `list_price` | number or string | yes |  |
| `min_price` | number or string or null | no |  |
| `valid_from` | string (date-time) or null | no |  |

**Response 201:** `PriceTierOut`

---

#### `PATCH /v1/price-tiers/{tier_id}`

Manager (MFA): change a tier's prices or quantities (logged with old and new value).

**Who:** manager, admin, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `tier_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `PriceTierPatch`

| Field | Type | Required | Notes |
|---|---|---|---|
| `min_qty` | number or string or null | no |  |
| `max_qty` | number or string or null | no |  |
| `list_price` | number or string or null | no |  |
| `min_price` | number or string or null | no |  |

**Response 200:** `PriceTierOut`

---

#### `DELETE /v1/price-tiers/{tier_id}`

Manager (MFA): delete a tier (logged).

**Who:** manager, admin, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `tier_id` | path | string (uuid) | yes |  |

**Response 204:** (no body)

---

#### `POST /v1/price-list/import`

Body: the .xlsx file (columns sku, min_qty, max_qty, list_price, min_price, currency). All rows are validated first; any error saves nothing and returns the row errors (§10).

**Who:** manager, admin, md (signed in with the authenticator app: `mfa=true`)

**Response 200:** `ImportOut`

---

#### `GET /v1/price-list/export`

Download the price list as Excel (the same layout the import accepts).

**Who:** sales, manager, admin, md

**Response 200:** (no body)

---

#### `GET /v1/products/{product_id}/history`

§20.7 change history of one product: its CRM-owned fields and its price tiers, newest first.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `product_id` | path | string (uuid) | yes |  |
| `limit` | query | integer | no | ≥ 1; ≤ 500; default `100` |

**Response 200:** list of `ChangeOut`

---

#### `GET /v1/price-list/history`

§20.7 change history of the whole price list (tier edits and Excel imports), newest first.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `limit` | query | integer | no | ≥ 1; ≤ 500; default `100` |

**Response 200:** list of `ChangeOut`

---

### quotes

#### `GET /v1/quote-versions/{version_id}`

§11.3 quote approval screen: one quote version with its lines, terms, delivery and totals.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `version_id` | path | string (uuid) | yes |  |

**Response 200:** `QuoteVersionOut`

---

#### `GET /v1/quote-versions/{version_id}/costs`

Cost build-up of each line: MD only (T3).

**Who:** md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `version_id` | path | string (uuid) | yes |  |

**Response 200:** list of `LineCostOut`

---

#### `GET /v1/approvals`

Manager / MD: approvals open (with SLA deadlines, §16) or decided. Salespeople see theirs in the inbox.

**Who:** manager, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `status` | query | `open` \| `decided` | no | default `"open"` |
| `cursor` | query | string or null | no |  |
| `limit` | query | integer | no | ≥ 1; ≤ 100; default `50` |

**Response 200:** `Page_ApprovalOut_`

---

#### `POST /v1/approvals/{approval_id}/decision`

Human + MFA only. Whose deal it is, takeover and special price are checked in the service.

**Who:** sales, manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `approval_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `DecisionIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `decision` | `approve` \| `reject` \| `correct` | yes |  |
| `note` | string or null | no |  |

**Response 200:** `ApprovalOut`

---

### records

#### `GET /v1/contacts`

Search by name, email or phone digits. A salesperson sees the contacts of their own deals.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `q` | query | string or null | no |  |
| `cursor` | query | string or null | no |  |
| `limit` | query | integer | no | ≥ 1; ≤ 100; default `50` |

**Response 200:** `Page_ContactRow_`

---

#### `GET /v1/contacts/{contact_id}`

§20.6 one contact (a salesperson only for contacts of their own deals).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `contact_id` | path | string (uuid) | yes |  |

**Response 200:** `ContactRow`

---

#### `PATCH /v1/contacts/{contact_id}`

Edit name, designation, company, preferred channel, language, time zone (phone / email change through the deal).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `contact_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `ContactEditIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `full_name` | string or null | no |  |
| `designation` | string or null | no |  |
| `company_id` | string (uuid) or null | no |  |
| `preferred_channel` | `whatsapp` \| `email` \| `phone` or null | no |  |
| `language` | string or null | no |  |
| `timezone` | string or null | no |  |

**Response 200:** `ContactRow`

---

#### `GET /v1/companies`

§20.6 companies: search by name, newest first.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `q` | query | string or null | no |  |
| `cursor` | query | string or null | no |  |
| `limit` | query | integer | no | ≥ 1; ≤ 100; default `50` |

**Response 200:** `Page_CompanyRow_`

---

#### `POST /v1/companies`

Create a company record.

**Who:** sales, manager, admin, md

**Request body** (`application/json`): `CompanyIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | min length 1; max length 300 |
| `country_code` | string or null | no |  |
| `city` | string or null | no |  |
| `customer_type` | string or null | no |  |
| `vat_trn` | string or null | no |  |
| `website` | string or null | no |  |
| `notes` | string or null | no |  |

**Response 201:** `CompanyRow`

---

#### `PATCH /v1/companies/{company_id}`

Edit a company record (logged).

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `company_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `CompanyIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | min length 1; max length 300 |
| `country_code` | string or null | no |  |
| `city` | string or null | no |  |
| `customer_type` | string or null | no |  |
| `vat_trn` | string or null | no |  |
| `website` | string or null | no |  |
| `notes` | string or null | no |  |

**Response 200:** `CompanyRow`

---

#### `GET /v1/products`

§20.7 product catalogue with the CRM-owned fields (MOQ, lead time, quotable) and CMS sync state.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `category` | query | string or null | no |  |
| `quotable` | query | boolean or null | no |  |

**Response 200:** list of `ProductRow`

---

#### `GET /v1/products/{product_id}`

One product with its specification (from the CMS) and CRM-owned fields.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `product_id` | path | string (uuid) | yes |  |

**Response 200:** `ProductRow`

---

#### `PATCH /v1/products/{product_id}`

Manager (MFA): MOQ for quoting, default delivery time, quotable/active (D22: CRM-owned fields).

**Who:** manager, md (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `product_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `ProductEditIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `moq` | integer or null | no |  |
| `carton_qty` | integer or null | no |  |
| `unit` | string or null | no |  |
| `is_active` | boolean or null | no |  |
| `is_quotable` | boolean or null | no |  |
| `default_lead_days_min` | integer or null | no |  |
| `default_lead_days_max` | integer or null | no |  |

**Response 200:** `ProductRow`

---

#### `GET /v1/orders`

§20.9 orders (a salesperson: their own deals), newest first.

**Who:** sales, manager, admin, md

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `status` | query | string or null | no |  |
| `cursor` | query | string or null | no |  |
| `limit` | query | integer | no | ≥ 1; ≤ 100; default `50` |

**Response 200:** `Page_OrderRow_`

---

#### `GET /v1/fx-rates`

The latest rate per currency (the one quotes use).

**Who:** manager, md

**Response 200:** list of `FxRow`

---

#### `POST /v1/fx-rates`

D38 "the manager can override": a manual rate for a day (it replaces that day's rate).

**Who:** manager, md (signed in with the authenticator app: `mfa=true`)

**Request body** (`application/json`): `FxIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `currency` | string | yes | pattern `^[A-Z]{3}$` |
| `rate_to_aed` | number or string | yes |  |
| `as_of` | string (date) | yes |  |

**Response 201:** `FxRow`

---

### stock

#### `GET /v1/stock`

§20.8 stock table: quantity on hand per product or material.

**Who:** sales, manager, admin, md

**Response 200:** list of `StockOut`

---

#### `PUT /v1/stock`

Manager or admin: set the quantity on hand of a product or material (logged).

**Who:** manager, admin, md

**Request body** (`application/json`): `StockIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `product_id` | string (uuid) or null | no |  |
| `material_code` | string or null | no |  |
| `quantity` | number or string | yes |  |
| `unit` | string | no | default `"pcs"` |

**Response 200:** `StockOut`

---

### users

#### `GET /v1/users`

§20.11 users and roles: every staff member with MFA enrolment and lock state.

**Who:** admin, manager, md

**Response 200:** list of `UserAdminOut`

---

#### `POST /v1/users`

Admin (MFA): a new staff user; the one-time password in the answer is shown once.

**Who:** admin (signed in with the authenticator app: `mfa=true`)

**Request body** (`application/json`): `UserCreateIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | string | yes | min length 3; max length 320 |
| `full_name` | string | yes | min length 1; max length 200 |
| `role` | `md` \| `manager` \| `admin` \| `sales` | yes |  |
| `phone_e164` | string or null | no |  |
| `break_glass` | boolean | no | default `false` |

**Response 201:** `UserCreatedOut`

---

#### `PATCH /v1/users/{user_id}`

Admin (MFA): role, active flag, phone, languages / countries / categories for assignment (§9), alert channels.

**Who:** admin (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `user_id` | path | string (uuid) | yes |  |

**Request body** (`application/json`): `UserPatchIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `full_name` | string or null | no |  |
| `role` | `md` \| `manager` \| `admin` \| `sales` or null | no |  |
| `phone_e164` | string or null | no |  |
| `is_active` | boolean or null | no |  |
| `languages` | list of string or null | no |  |
| `countries` | list of string or null | no |  |
| `category_codes` | list of string or null | no |  |
| `alert_channels` | list of `whatsapp` \| `email` \| `call` \| `in_app` or null | no |  |

**Response 200:** `UserAdminOut`

---

#### `POST /v1/users/{user_id}/reset-mfa`

A lost authenticator: sessions end; the person signs in with an email code and enrols again.

**Who:** admin (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `user_id` | path | string (uuid) | yes |  |

**Response 200:** `UserAdminOut`

---

#### `POST /v1/users/{user_id}/unlock`

Admin (MFA): lift a lock-out after too many wrong passwords or codes.

**Who:** admin (signed in with the authenticator app: `mfa=true`)

**Parameters**

| Name | In | Type | Required | Notes |
|---|---|---|---|---|
| `user_id` | path | string (uuid) | yes |  |

**Response 200:** `UserAdminOut`

---

## CRM endpoint reference: data shapes

### `AiToggleIn`

Pause the AI (any reason) or resume it. A deal paused for `opt_out` cannot be resumed here: the AI may never remove a stop (§8.1 step 6); only a fresh, logged consent from the buyer lifts it.

| Field | Type | Required | Notes |
|---|---|---|---|
| `enabled` | boolean | yes |  |
| `reason` | `human_takeover` \| `md_takeover` \| `opt_out` \| `exception` | no | default `"human_takeover"` |

### `ApprovalOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `quote_version_id` | string (uuid) | yes |  |
| `kind` | string | yes |  |
| `required_role` | string | yes |  |
| `assigned_user_id` | string (uuid) or null | yes |  |
| `approver_user_id` | string (uuid) or null | no |  |
| `status` | string | yes |  |
| `submitted_at` | string (date-time) | yes |  |
| `deadline_at` | string (date-time) | yes |  |
| `decided_by` | string (uuid) or null | yes |  |
| `decided_at` | string (date-time) or null | yes |  |
| `decision_note` | string or null | yes |  |

### `AvailabilityIn`

§9.3 away / holiday and the backup who covers.

| Field | Type | Required | Notes |
|---|---|---|---|
| `is_away` | boolean | yes |  |
| `backup_user_id` | string (uuid) or null | no |  |

### `CallIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `client_key` | string | yes | min length 8; max length 80 |

### `CallOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `call_id` | string (uuid) | yes |  |

### `ChallengeIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `challenge_token` | string | yes | min length 10; max length 200 |

### `ChallengeOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `challenge_token` | string | yes |  |
| `methods` | list of `totp` \| `email` | yes |  |
| `totp_enrolled` | boolean | yes |  |
| `expires_at` | string (date-time) | yes |  |

### `ChangeOut`

One logged change (§3 "every change is logged"): who, when, what, old and new value.

| Field | Type | Required | Notes |
|---|---|---|---|
| `at` | string (date-time) | yes |  |
| `actor_type` | string | yes |  |
| `actor_id` | string | yes |  |
| `action` | string | yes |  |
| `entity_type` | string | yes |  |
| `entity_id` | string | yes |  |
| `before` | object or null | yes |  |
| `after` | object or null | yes |  |

### `ChangeRequestOut`

The §13.2 card: the buyer's words, what changes, the SLA timer.

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `deal_id` | string (uuid) | yes |  |
| `quote_version_id` | string (uuid) or null | yes |  |
| `type` | string | yes |  |
| `buyer_text` | string | yes |  |
| `buyer_text_en` | string or null | yes |  |
| `fields` | list of object | yes |  |
| `ai_notes` | string or null | yes |  |
| `status` | string | yes |  |
| `assigned_to` | string (uuid) or null | yes |  |
| `sla_due_at` | string (date-time) | yes |  |
| `escalated_at` | string (date-time) or null | yes |  |
| `resolved_version_id` | string (uuid) or null | yes |  |
| `decline_reason` | string or null | yes |  |
| `created_at` | string (date-time) | yes |  |

### `ChannelAccountOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `channel` | string | yes |  |
| `provider` | string | yes |  |
| `display_name` | string | yes |  |
| `address` | string | yes |  |
| `purpose` | string | yes |  |
| `status` | string | yes |  |
| `region` | string or null | yes |  |
| `countries` | list of string | yes |  |
| `is_default` | boolean | yes |  |
| `created_at` | string (date-time) | yes |  |

### `ChannelAccountPatch`

| Field | Type | Required | Notes |
|---|---|---|---|
| `display_name` | string or null | no |  |
| `region` | string or null | no |  |
| `countries` | list of string or null | no |  |
| `is_default` | boolean or null | no |  |
| `status` | `active` \| `disabled` or null | no |  |

### `CodeIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `challenge_token` | string | yes | min length 10; max length 200 |
| `code` | string | yes | min length 6; max length 10 |

### `CompanyIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | min length 1; max length 300 |
| `country_code` | string or null | no |  |
| `city` | string or null | no |  |
| `customer_type` | string or null | no |  |
| `vat_trn` | string or null | no |  |
| `website` | string or null | no |  |
| `notes` | string or null | no |  |

### `CompanyRow`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `name` | string | yes |  |
| `country_code` | string or null | yes |  |
| `city` | string or null | yes |  |
| `customer_type` | string or null | yes |  |
| `vat_trn` | string or null | yes |  |
| `website` | string or null | yes |  |
| `notes` | string or null | yes |  |
| `created_at` | string (date-time) | yes |  |

### `ContactEditIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `full_name` | string or null | no |  |
| `designation` | string or null | no |  |
| `company_id` | string (uuid) or null | no |  |
| `preferred_channel` | `whatsapp` \| `email` \| `phone` or null | no |  |
| `language` | string or null | no |  |
| `timezone` | string or null | no |  |

### `ContactOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `full_name` | string or null | yes |  |
| `phone_e164` | string or null | yes |  |
| `whatsapp_e164` | string or null | yes |  |
| `email` | string or null | yes |  |
| `preferred_channel` | string or null | yes |  |
| `language` | string | yes |  |
| `timezone` | string | yes |  |
| `phone_status` | string | no | default `"unknown"` |
| `phone_status_at` | string (date-time) or null | no |  |
| `whatsapp_profile_name` | string or null | no |  |
| `company_id` | string (uuid) or null | no |  |

### `ContactRow`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `full_name` | string or null | yes |  |
| `designation` | string or null | yes |  |
| `company_id` | string (uuid) or null | yes |  |
| `phone_e164` | string or null | yes |  |
| `whatsapp_e164` | string or null | yes |  |
| `email` | string or null | yes |  |
| `preferred_channel` | string or null | yes |  |
| `language` | string | yes |  |
| `timezone` | string | yes |  |
| `phone_status` | string | yes |  |
| `source` | string | yes |  |
| `created_at` | string (date-time) | yes |  |

### `DashboardSummary`

| Field | Type | Required | Notes |
|---|---|---|---|
| `deals_by_stage` | map of integer | yes |  |
| `open_pipeline_aed` | string | yes |  |
| `approvals_pending` | integer | yes |  |
| `approvals_overdue` | integer | yes |  |
| `open_tasks` | integer | yes |  |
| `ai_paused_deals` | integer | yes |  |
| `materials` | list of `FreshnessOut` | yes |  |

### `DealDetail`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `number` | string | yes |  |
| `stage` | `DealStage` | yes |  |
| `ai_enabled` | boolean | yes |  |
| `ai_paused_reason` | string or null | yes |  |
| `currency` | string | yes |  |
| `concessions_used` | integer | yes |  |
| `next_action_at` | string (date-time) or null | yes |  |
| `owner_user_id` | string (uuid) or null | yes |  |
| `taken_over_by` | string (uuid) or null | no |  |
| `opted_out_at` | string (date-time) or null | no |  |
| `version` | integer | yes |  |
| `updated_at` | string (date-time) | yes |  |
| `contact` | `ContactOut` | yes |  |
| `requirements` | list of `RequirementOut` | yes |  |
| `missing_fields` | list of string | yes |  |
| `lost_reason` | string or null | yes |  |
| `lost_note` | string or null | yes |  |
| `ai_summary` | string or null | no |  |
| `ai_summary_at` | string (date-time) or null | no |  |
| `personal_contact_shared_at` | string (date-time) or null | no |  |

### `DealStage`

One of: `NEW`, `QUALIFIED`, `QUOTATION`, `NEGOTIATION`, `READY_TO_CLOSE`, `PAYMENT`, `WON`, `LOST`

### `DealSummary`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `number` | string | yes |  |
| `stage` | `DealStage` | yes |  |
| `ai_enabled` | boolean | yes |  |
| `ai_paused_reason` | string or null | yes |  |
| `currency` | string | yes |  |
| `concessions_used` | integer | yes |  |
| `next_action_at` | string (date-time) or null | yes |  |
| `owner_user_id` | string (uuid) or null | yes |  |
| `taken_over_by` | string (uuid) or null | no |  |
| `opted_out_at` | string (date-time) or null | no |  |
| `version` | integer | yes |  |
| `updated_at` | string (date-time) | yes |  |

### `DecisionIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `decision` | `approve` \| `reject` \| `correct` | yes |  |
| `note` | string or null | no |  |

### `DeclineIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `reason` | string | yes | min length 1; max length 1000 |

### `FlagIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `enabled` | boolean | yes |  |

### `FreshnessOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `material_code` | string | yes |  |
| `days_since_approval` | integer or null | yes |  |
| `level` | `ok` \| `warn` \| `block` | yes |  |

### `FxIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `currency` | string | yes | pattern `^[A-Z]{3}$` |
| `rate_to_aed` | number or string | yes |  |
| `as_of` | string (date) | yes |  |

### `FxRow`

| Field | Type | Required | Notes |
|---|---|---|---|
| `currency` | string | yes |  |
| `rate_to_aed` | string | yes |  |
| `as_of` | string (date) | yes |  |
| `source` | string | yes |  |

### `ImportOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `saved` | integer | yes |  |
| `errors` | list of `RowErrorOut` | yes |  |

### `InboxItemOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `kind` | string | yes |  |
| `ref_id` | string | yes |  |
| `deal_id` | string (uuid) | yes |  |
| `deal_number` | string | yes |  |
| `title` | string | yes |  |
| `due_at` | string (date-time) or null | yes |  |
| `created_at` | string (date-time) | yes |  |
| `overdue` | boolean | yes |  |

### `KillSwitchIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `level` | `off` \| `marketing` \| `calls` \| `text_only` \| `hold` | yes |  |

### `KillSwitchOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `level` | `off` \| `marketing` \| `calls` \| `text_only` \| `hold` | yes |  |
| `marketing_stopped` | boolean | yes |  |
| `calls_stopped` | boolean | yes |  |
| `voice_stopped` | boolean | yes |  |
| `all_held` | boolean | yes |  |

### `LineCostOut`

MD only.

| Field | Type | Required | Notes |
|---|---|---|---|
| `line_no` | integer | yes |  |
| `material_rate` | string | yes |  |
| `kg_per_1000` | string | yes |  |
| `yield_pct` | string | yes |  |
| `production_per_1000` | string | yes |  |
| `packaging_per_1000` | string | yes |  |
| `freight_per_1000` | string | yes |  |
| `unit_cost` | string | yes |  |
| `markup_pct` | string | yes |  |
| `markup_min_pct` | string | yes |  |
| `markup_max_pct` | string | yes |  |

### `LineEditIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `line_no` | integer | yes | ≥ 1.0 |
| `unit_price` | number or string or null | no |  |
| `quantity` | number or string or null | no |  |

### `LoginIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | string | yes | min length 3; max length 320 |
| `password` | string | yes | min length 1; max length 256 |

### `ManagerBoard`

| Field | Type | Required | Notes |
|---|---|---|---|
| `period_from` | string (date) | yes |  |
| `period_to` | string (date) | yes |  |
| `salespeople` | list of `SalespersonBoard` | yes |  |
| `below_minimum_requests` | integer | yes |  |
| `change_requests_by_type` | map of integer | yes |  |
| `sla_breaches` | integer | yes |  |
| `price_list_changes` | integer | yes |  |
| `opted_out` | integer | yes |  |
| `opt_out_reasons` | map of integer | yes |  |

### `ManagerOfferIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `unit_price` | number or string | yes |  |
| `resume_ai` | boolean | no | default `true` |

### `ManualLineIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `description` | string | yes | min length 1; max length 500 |
| `quantity` | number or string | yes |  |
| `unit_price` | number or string | yes |  |
| `unit` | string | no | default `"pcs"` |
| `product_id` | string (uuid) or null | no |  |

### `ManualQuoteIn`

D8: the salesperson prices a custom, printed or unpriced item by hand.

| Field | Type | Required | Notes |
|---|---|---|---|
| `lines` | list of `ManualLineIn` | yes | min items 1 |
| `delivery_time_text` | string or null | no |  |
| `buyer_note` | string or null | no |  |

### `MdBoard`

| Field | Type | Required | Notes |
|---|---|---|---|
| `period_from` | string (date) | yes |  |
| `period_to` | string (date) | yes |  |
| `pipeline_aed_by_stage` | map of string | yes |  |
| `win_rate` | number or null | yes |  |
| `avg_discount_from_list` | number or null | yes |  |
| `deals_by_country` | map of integer | yes |  |
| `deals_by_category` | map of integer | yes |  |
| `deals_by_channel` | map of integer | yes |  |
| `top_buyers` | list of object | yes |  |
| `takeover_queue` | list of string (uuid) | yes |  |

### `OptOutIn`

§8.1 step 4: a salesperson records a stop the buyer asked for in a conversation the buyer started (e.g. on a call). Add only: nobody removes a stop here (step 6).

| Field | Type | Required | Notes |
|---|---|---|---|
| `scope` | `all` \| `whatsapp` \| `email` \| `phone` | no | default `"all"` |
| `words` | string | yes | what the buyer said, as close as possible min length 1; max length 2000 |
| `reason` | string or null | no | only a reason the buyer gave |

### `OrderOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `number` | string | yes |  |
| `deal_id` | string (uuid) | yes |  |
| `status` | string | yes |  |
| `final_quote_version_id` | string (uuid) | yes |  |
| `expected_reorder_date` | string (date) or null | no |  |

### `OrderRow`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `number` | string | yes |  |
| `deal_id` | string (uuid) | yes |  |
| `status` | string | yes |  |
| `production_started_on` | string (date) or null | yes |  |
| `ready_on` | string (date) or null | yes |  |
| `shipped_on` | string (date) or null | yes |  |
| `delivered_on` | string (date) or null | yes |  |
| `tracking_ref` | string or null | yes |  |
| `created_at` | string (date-time) | yes |  |

### `OrderStatusIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `status` | `ready` \| `shipped` \| `delivered` \| `cancelled` | yes |  |
| `on_date` | string (date) or null | no |  |
| `tracking_ref` | string or null | no |  |

### `Page_ApprovalOut_`

| Field | Type | Required | Notes |
|---|---|---|---|
| `items` | list of `ApprovalOut` | yes |  |
| `next_cursor` | string or null | no |  |

### `Page_CompanyRow_`

| Field | Type | Required | Notes |
|---|---|---|---|
| `items` | list of `CompanyRow` | yes |  |
| `next_cursor` | string or null | no |  |

### `Page_ContactRow_`

| Field | Type | Required | Notes |
|---|---|---|---|
| `items` | list of `ContactRow` | yes |  |
| `next_cursor` | string or null | no |  |

### `Page_DealSummary_`

| Field | Type | Required | Notes |
|---|---|---|---|
| `items` | list of `DealSummary` | yes |  |
| `next_cursor` | string or null | no |  |

### `Page_OrderRow_`

| Field | Type | Required | Notes |
|---|---|---|---|
| `items` | list of `OrderRow` | yes |  |
| `next_cursor` | string or null | no |  |

### `Page_RateOut_`

| Field | Type | Required | Notes |
|---|---|---|---|
| `items` | list of `RateOut` | yes |  |
| `next_cursor` | string or null | no |  |

### `Page_TimelineOut_`

| Field | Type | Required | Notes |
|---|---|---|---|
| `items` | list of `TimelineOut` | yes |  |
| `next_cursor` | string or null | no |  |

### `PasswordChangeIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `current_password` | string | yes | min length 1; max length 256 |
| `new_password` | string | yes | min length 12; max length 256 |

### `PaymentIn`

§15.1 / D20: the salesperson confirms the advance was received. Every detail is optional; an amount needs its currency.

| Field | Type | Required | Notes |
|---|---|---|---|
| `amount` | number or string or null | no |  |
| `currency` | string or null | no |  |
| `method` | string or null | no |  |
| `reference` | string or null | no |  |
| `paid_on` | string (date) or null | no |  |
| `note` | string or null | no |  |
| `proof_s3_key` | string or null | no |  |

### `PaymentOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `deal_id` | string (uuid) | yes |  |
| `amount` | string or null | yes |  |
| `currency` | string or null | yes |  |
| `method` | string or null | yes |  |
| `reference` | string or null | yes |  |
| `paid_on` | string (date) or null | yes |  |

### `PriceTierIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `min_qty` | number or string | yes |  |
| `max_qty` | number or string or null | no |  |
| `list_price` | number or string | yes |  |
| `min_price` | number or string or null | no |  |
| `valid_from` | string (date-time) or null | no |  |

### `PriceTierOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `product_id` | string (uuid) | yes |  |
| `min_qty` | string | yes |  |
| `max_qty` | string or null | yes |  |
| `list_price` | string | yes |  |
| `min_price` | string or null | yes |  |
| `currency` | string | yes |  |
| `valid_from` | string (date-time) | yes |  |

### `PriceTierPatch`

| Field | Type | Required | Notes |
|---|---|---|---|
| `min_qty` | number or string or null | no |  |
| `max_qty` | number or string or null | no |  |
| `list_price` | number or string or null | no |  |
| `min_price` | number or string or null | no |  |

### `ProductEditIn`

CRM-owned fields only (D22): the CMS owns the description.

| Field | Type | Required | Notes |
|---|---|---|---|
| `moq` | integer or null | no |  |
| `carton_qty` | integer or null | no |  |
| `unit` | string or null | no |  |
| `is_active` | boolean or null | no |  |
| `is_quotable` | boolean or null | no |  |
| `default_lead_days_min` | integer or null | no |  |
| `default_lead_days_max` | integer or null | no |  |

### `ProductRow`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `sku` | string | yes |  |
| `name` | string | yes |  |
| `category_code` | string | yes |  |
| `unit` | string | yes |  |
| `moq` | integer or null | yes |  |
| `carton_qty` | integer or null | yes |  |
| `is_active` | boolean | yes |  |
| `is_quotable` | boolean | yes |  |
| `custom_printing` | boolean | yes |  |
| `default_lead_days_min` | integer or null | yes |  |
| `default_lead_days_max` | integer or null | yes |  |
| `spec` | object | yes |  |
| `cms_product_id` | string or null | yes |  |
| `cms_published` | boolean | yes |  |
| `cms_synced_at` | string (date-time) or null | yes |  |

### `QuoteEditsIn`

§11.3 "Edit then approve" / §13.3 "Revise quote": only the fields that change.

| Field | Type | Required | Notes |
|---|---|---|---|
| `lines` | list of `LineEditIn` | no | default `[]` |
| `delivery_time_text` | string or null | no |  |
| `delivery_place` | string or null | no |  |
| `incoterm` | `EXW` \| `FCA` \| `FOB` \| `CFR` \| `CIF` \| `CPT` \| `CIP` \| `DAP` \| `DDP` or null | no |  |
| `payment_terms_text` | string or null | no |  |
| `note` | string or null | no |  |
| `buyer_note` | string or null | no |  |
| `cover_note` | string or null | no |  |

### `QuoteLineOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `line_no` | integer | yes |  |
| `description` | string | yes |  |
| `quantity` | string | yes |  |
| `unit` | string | yes |  |
| `unit_price` | string | yes |  |
| `line_total` | string | yes |  |

### `QuoteVersionOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `quote_id` | string (uuid) | yes |  |
| `version_no` | integer | yes |  |
| `kind` | string | yes |  |
| `status` | string | yes |  |
| `currency` | string | yes |  |
| `subtotal` | string | yes |  |
| `vat_rate` | string | yes |  |
| `vat_amount` | string | yes |  |
| `total` | string | yes |  |
| `total_aed` | string | yes |  |
| `incoterm` | string | yes |  |
| `validity_days` | integer | yes |  |
| `valid_until` | string (date) or null | yes |  |
| `clauses` | list of string | yes |  |
| `approver_summary` | string or null | yes |  |
| `delivery_time_text` | string or null | no |  |
| `delivery_place` | string or null | no |  |
| `payment_terms_text` | string or null | no |  |
| `fx_rate` | string or null | no |  |
| `fx_rate_date` | string (date) or null | no |  |
| `cover_note` | string or null | no |  |
| `change_summary` | string or null | no |  |
| `pdf_s3_key` | string or null | yes |  |
| `created_at` | string (date-time) | yes |  |
| `lines` | list of `QuoteLineOut` | no | default `[]` |

### `RateDecisionIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `rate_per_unit` | number or string or null | no |  |
| `note` | string or null | no |  |

### `RateOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `material_code` | string | yes |  |
| `rate_per_unit` | string | yes |  |
| `currency` | string | yes |  |
| `source` | string | yes |  |
| `source_url` | string or null | yes |  |
| `change_pct` | string or null | yes |  |
| `status` | string | yes |  |
| `proposed_at` | string (date-time) | yes |  |
| `decided_at` | string (date-time) or null | yes |  |
| `note` | string or null | yes |  |

### `ReassignIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `user_id` | string (uuid) | yes |  |

### `ReopenIn`

§7A.1 / D24: the manager reopens a LOST deal (back to the stage it was lost from).

| Field | Type | Required | Notes |
|---|---|---|---|
| `reason` | string | yes | min length 1; max length 500 |

### `RequirementOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `product_id` | string (uuid) or null | yes |  |
| `product_text` | string or null | yes |  |
| `spec` | object | yes |  |
| `quantity` | string or null | yes |  |
| `unit` | string | yes |  |
| `delivery_country` | string or null | yes |  |
| `delivery_city` | string or null | yes |  |
| `needed_by` | string (date) or null | yes |  |

### `ResetFinishIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | string | yes | min length 3; max length 320 |
| `code` | string | yes | min length 6; max length 10 |
| `new_password` | string | yes | min length 12; max length 256 |

### `ResetStartIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | string | yes | min length 3; max length 320 |

### `ResolveIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `version_id` | string (uuid) | yes |  |

### `RowErrorOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `row` | integer | yes |  |
| `field` | string | yes |  |
| `error` | string | yes |  |

### `SalespersonBoard`

| Field | Type | Required | Notes |
|---|---|---|---|
| `user_id` | string (uuid) | yes |  |
| `name` | string | yes |  |
| `open_deals_by_stage` | map of integer | yes |  |
| `waiting_for_me` | integer | yes |  |
| `avg_response_minutes` | number or null | yes |  |
| `quotes_sent` | integer | yes |  |
| `deals_won` | integer | yes |  |

### `SettingIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `value` | any | yes |  |

### `SettingOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `key` | string | yes |  |
| `value` | any | yes |  |
| `description` | string | yes |  |
| `updated_at` | string (date-time) | yes |  |

### `StaffMessageIn`

§14: the salesperson writes to the buyer from the CRM inbox (same channel, same company number).

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` | string | no | max length 4000; default `""` |
| `client_key` | string | yes | min length 8; max length 80 |
| `channel` | `whatsapp` \| `email` or null | no |  |
| `reopen_template` | boolean | no | default `false` |

### `StaffMessageOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `message_id` | string (uuid) | yes |  |

### `StaffUserOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `email` | string | yes |  |
| `full_name` | string | yes |  |
| `role` | string | yes |  |
| `phone_e164` | string or null | yes |  |
| `is_active` | boolean | yes |  |
| `is_away` | boolean | yes |  |
| `languages` | list of string | yes |  |
| `countries` | list of string | yes |  |
| `category_codes` | list of string | yes |  |
| `alert_channels` | list of string | yes |  |

### `StageMoveIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `to` | `DealStage` | yes |  |
| `expected_version` | integer | yes |  |
| `reason` | string or null | no |  |
| `lost_reason` | `price_too_high` \| `delivery_not_suitable` \| `bought_competitor` \| `product_unavailable` \| `no_response_postponed` \| `budget` \| `duplicate` \| `other` \| `opted_out` or null | no |  |
| `lost_note` | string or null | no |  |

### `StockIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `product_id` | string (uuid) or null | no |  |
| `material_code` | string or null | no |  |
| `quantity` | number or string | yes |  |
| `unit` | string | no | default `"pcs"` |

### `StockOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `item_type` | string | yes |  |
| `product_id` | string (uuid) or null | yes |  |
| `material_code` | string or null | yes |  |
| `quantity` | string | yes |  |
| `reserved_quantity` | string | yes |  |
| `unit` | string | yes |  |

### `TemplateIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `channel` | `whatsapp` \| `email` | yes |  |
| `name` | string | yes | pattern `^[a-z0-9_]{1,64}$` |
| `language` | string | yes | min length 2; max length 16 |
| `category` | `utility` \| `marketing` \| `authentication` \| `internal` | no | default `"utility"` |
| `subject` | string or null | no |  |
| `body` | string | yes | min length 1; max length 4000 |
| `variables` | list of string | no | max items 20 |

### `TemplateOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `channel` | string | yes |  |
| `name` | string | yes |  |
| `language` | string | yes |  |
| `category` | string | yes |  |
| `provider_status` | string | yes |  |
| `subject` | string or null | yes |  |
| `body` | string | yes |  |
| `variables` | list of string | yes |  |
| `updated_at` | string (date-time) | yes |  |

### `TemplateStatusIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `provider_status` | `draft` \| `submitted` \| `approved` \| `rejected` \| `paused` | yes |  |

### `TimelineOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | integer | yes |  |
| `kind` | string | yes |  |
| `channel` | string or null | yes |  |
| `direction` | string or null | yes |  |
| `summary` | string | yes |  |
| `actor_type` | string | yes |  |
| `actor_id` | string or null | yes |  |
| `at` | string (date-time) | yes |  |

### `TokenOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `access_token` | string | yes |  |
| `token_type` | `Bearer` | no | default `"Bearer"` |
| `expires_in` | integer | yes |  |
| `mfa` | boolean | yes |  |
| `amr` | list of string | yes |  |
| `must_change_password` | boolean | yes |  |
| `totp_enrolled` | boolean | yes |  |
| `user` | `StaffUserOut` | yes |  |

### `TotpConfirmIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `code` | string | yes | min length 6; max length 10 |

### `TotpEnrolOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `secret` | string | yes |  |
| `otpauth_uri` | string | yes |  |

### `UserAdminOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `email` | string | yes |  |
| `full_name` | string | yes |  |
| `role` | string | yes |  |
| `phone_e164` | string or null | yes |  |
| `is_active` | boolean | yes |  |
| `is_away` | boolean | yes |  |
| `languages` | list of string | yes |  |
| `countries` | list of string | yes |  |
| `category_codes` | list of string | yes |  |
| `alert_channels` | list of string | yes |  |
| `backup_user_id` | string (uuid) or null | yes |  |
| `totp_enrolled` | boolean | yes |  |
| `locked` | boolean | yes |  |

### `UserCreateIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | string | yes | min length 3; max length 320 |
| `full_name` | string | yes | min length 1; max length 200 |
| `role` | `md` \| `manager` \| `admin` \| `sales` | yes |  |
| `phone_e164` | string or null | no |  |
| `break_glass` | boolean | no | default `false` |

### `UserCreatedOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `user` | `StaffUserOut` | yes |  |
| `temporary_password` | string | yes |  |

### `UserOut`

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes |  |
| `full_name` | string | yes |  |
| `role` | string | yes |  |
| `is_active` | boolean | yes |  |
| `is_away` | boolean | yes |  |
| `backup_user_id` | string (uuid) or null | yes |  |
| `languages` | list of string | yes |  |
| `countries` | list of string | yes |  |
| `category_codes` | list of string | yes |  |
| `alert_channels` | list of string | yes |  |

### `UserPatchIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `full_name` | string or null | no |  |
| `role` | `md` \| `manager` \| `admin` \| `sales` or null | no |  |
| `phone_e164` | string or null | no |  |
| `is_active` | boolean or null | no |  |
| `languages` | list of string or null | no |  |
| `countries` | list of string or null | no |  |
| `category_codes` | list of string or null | no |  |
| `alert_channels` | list of `whatsapp` \| `email` \| `call` \| `in_app` or null | no |  |

### `WonIn`

| Field | Type | Required | Notes |
|---|---|---|---|
| `expected_reorder_date` | string (date) or null | no |  |

