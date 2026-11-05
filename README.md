# Kairox platform

A reusable B2B sales platform (CRM + channel automation + AI agents) by Kairox AI Tech. First client: **Arifa Packing & Packaging LLC (Arifaa Pack)**, Ajman, UAE (food packaging: aluminium and plastic containers, cutlery, custom-branded packaging).

**Status (5 Nov 2026):**
- The platform code is built and tested: 309 tests, lint/type clean.
- It implements the 4 Nov rules. The 5 Nov business flow changes approvals (salesperson), pricing (manual price list) and change requests.
- The code update is planned in `docs/V1_CHANGE_PLAN.md` and has not started yet.

| Read | For |
|---|---|
| `CLAUDE.md` | How to work in this repo (Claude Code / developers) |
| `docs/V1_CHANGE_PLAN.md` | **Next work:** 12 work packages to reach the 5 Nov flow |
| `docs/BUSINESS_FLOW_V1.md` | Detailed business flow v1 + v2 (SRS source), exported from the Claude Doc |
| `CONTRACTS.md` | Package rules, shared helpers, hard safety rules |
| `docs/REUSE_CATALOGUE.md` | What to import for each capability |
| `docs/DELIVERY.md` | What is built, test results, open items |
| `docs/DEPLOYMENT.md` | Run order, env vars, go-live steps (infra detail: `infra/README.md`) |
| `docs/CONTESTS.md` | Competing builds and why the winners won |
| `docs/HANDOFF_CONTEXT.md` | Full project context for a new session |

Quick start: `uv sync --all-packages --all-extras && make up && make migrate && make api`
