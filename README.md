# Kairox platform

Reusable B2B sales platform (CRM + channel automation + AI agents) by Kairox AI Tech. First client: Arifaa Pack.

- `CONTRACTS.md` — package rules and shared helpers (read first)
- `docs/DELIVERY.md` — what is built, test results, open items
- `docs/REUSE_CATALOGUE.md` — what to import for each capability
- `docs/DEPLOYMENT.md` — run order, env vars, go-live steps (infra detail: `infra/README.md`)
- `docs/CONTESTS.md` — competing builds and why the winners won

Quick start: `uv sync --all-packages --all-extras && docker compose up -d && kx migrate && kx api`
