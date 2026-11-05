# Open questions and gaps found while building the 5 Nov flow

Each row gives the work package, the question, what was built meanwhile and where. When the documents
were silent, the build takes the option that blocks, alerts or hands over to a human, never the one that
sends, discounts or promises. Decide or ask the client, then change the setting or code named in the last
column.

| # | WP | Question | What I chose for now | Where |
|---|---|---|---|---|
| 1 | WP1 | The plan asks for one generated `contacts.recipient_hash`, but a contact has up to three addresses (phone, WhatsApp, email) and inbound matching needs all three. | Generated `recipient_hashes text[]` (GIN index) holding all three hashes. | `migrations/crm/versions/0002_v1_flow.py`, `kx/crm/models/customers.py` |
| 2 | WP1 | Default staff alert channels (D4, "to confirm with the client"). | Plan default `{whatsapp}` for new users; CRM in-app notifications are always on (§16). Existing users keep their value. | `crm_0002` (`users.alert_channels`) |
| 3 | WP1 | §15.4 lost reasons vs the 4 Nov codes. | Kept the 5 existing codes (price, delivery time, spec not available, competitor, no response) and added `budget`, `duplicate`, `other`. | `crm_0002` (`deals_lost_reason_check`), `kx/crm/rules.py` |
| 4 | WP1 | The 4 Nov feature flags `news_digest` and `price_scraping` were `true`; the plan adds `features.*` settings. | Migration sets both flags to `false`; the `features.*` settings are the switches from now on (D14). | `crm_0002` |
| 5 | WP2 | A tier with no minimum and a category with no minimum %: what is the floor? | The list price itself: no room to negotiate, the AI cannot discount. | `kx/crm/pricing/price_list.py` `min_price_for` |
| 6 | WP2 | §8 says the delivery country sets the currency, but no country→currency list exists (§23 scenario 2 quotes Riyadh in AED, scenario 7 quotes Saudi in SAR). | Setting `quote.currency_by_country`, default `{}`: every quote is in AED until the manager maps a country. | `crm_0003`, `BusinessSettings.quote_currency` |
| 7 | WP2 | Default payment terms text; §15.1 says the advance % is a setting per customer type. | Setting `quote.payment_terms_text` = "Advance payment is required before production starts." (names no %). The per-customer-type % is not built: the salesperson edits the terms on the quote. | `crm_0003`, `services/quotes.py` |
| 8 | WP2 | Converted quotes: §11.1 converts the total (AED 12,500.00 → USD 3,403.68), so the 6-dp unit price × quantity can differ by cents (0.068074 × 50,000 = 3,403.70). | Line total = AED line total ÷ printed rate (matches §11.1); unit price shown to 6 dp. Confirm the client accepts the cent difference. | `price_list.list_line` |
| 9 | WP2 | §12.1 step 2 "only for a bigger quantity (or a firm order today)": the AI cannot verify a firm order. | Steps 2 and 3 need a bigger quantity only. | `engine.evaluate_ladder_counter` |
| 10 | WP2 | §12.2 rule 2 (accept an ask between the next step and the offer) vs "steps 2–3 need a bigger quantity". | The quantity condition applies to rule 2 too: without more quantity the AI asks for it instead of accepting. | `engine.evaluate_ladder_counter` |
| 11 | WP2 | Conflict: §12.2 rule 8 checks replies against "the approved quote and the ladder steps", but §18 says the AI sees no minimums and rule 6 says never reveal the steps. | Followed §18: `allowed_prices` = the issued quote + the steps actually offered. Steps not yet offered and the minimum never reach the AI. | `kx/crm/gateway.py` `allowed_prices` |
| 12 | WP2 | How the AI marks a printed request (§10 printed variants go to the salesperson). | Requirement spec keys `printing`, `print`, `logo`, `branding`, `custom_print(ing)` with a non-"no" value → Pricing needed. | `services/pricing.py` `wants_printing` |
| 13 | WP2 | "low_ask_count per open negotiation" and steps used: per deal or per quote version? | Steps used count per issued version; `deals.low_ask_count` resets when a new version is issued. | `services/negotiation.py`, `services/issuing.py` |
