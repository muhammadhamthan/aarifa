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
