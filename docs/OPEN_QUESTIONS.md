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
