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
