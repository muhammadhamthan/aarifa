# Kairox platform

A reusable B2B sales platform (CRM + channel automation + AI agents) by Kairox AI Tech. First client: **Arifa Packing & Packaging LLC (Arifaa Pack)**, Ajman, UAE (food packaging: aluminium and plastic containers, cutlery, custom-branded packaging).

**Status (11 Nov 2026):**
- The 5 Nov business flow is built and tested (WP1–WP12, 6 Nov).
- The SRS was updated (STOP flow §8.1, silent phone confirmation §8.2, CMS→CRM mirror §7, CRM flow §7A, backend-built quotes §11.1, same-number handover §14, optional payment details §15) and the platform changed on 11 Nov (Celery, in-house login, Better Stack, Phoenix, Claude API direct, Qwen3 embeddings, Telnyx, Postmark, fallback chains). The work list is WP13–WP29 in `docs/V1_CHANGE_PLAN.md`; progress in `docs/V1_PROGRESS.md`; open decisions in `docs/OPEN_QUESTIONS.md`.

| Read | For |
|---|---|
| `CLAUDE.md` | How to work in this repo (Claude Code / developers) |
| `docs/Arifaa Pack — Detailed Business Flow v1 (SRS basis) (2).md` | **The SRS**: business flow v1 + v2 (read-only master) |
| `docs/Arifaa Platform — Platforms, Pricing & Fallbacks/` | Vendors per field, fallbacks, monthly cost (read-only master) |
| `docs/V1_CHANGE_PLAN.md` | **Work list:** 11 Nov WP13–WP29 and decisions D17–D40; the finished 5 Nov plan below it |
| `CONTRACTS.md` | Package rules, shared helpers, hard safety rules |
| `docs/REUSE_CATALOGUE.md` | What to import for each capability |
| `docs/DELIVERY.md` | What is built, test results, open items |
| `docs/DEPLOYMENT.md` | Run order, env vars, go-live steps (infra detail: `infra/README.md`) |
| `docs/frontend/` | Hand-offs for the frontend team: CRM screens API, CMS, website API |
| `docs/CONTESTS.md` | Competing builds and why the winners won |
| `docs/HANDOFF_CONTEXT.md` | Full project context for a new session |

Quick start: `uv sync --all-packages --all-extras && cp .env.example .env && make up && make migrate && make api` (then `kx worker` and `kx beat` in two more terminals).
