# resource-usage-bench

Measured resource use of the Kairox / Arifa Pack platform and of the free self-hosted stack from
`free version.docx`, with 10 fake users. Nothing in the platform's source was changed: every bench file is here.

**Easiest read: [`docs/Kairox_Free_Stack_Guide.docx`](docs/Kairox_Free_Stack_Guide.docx)**: one Word guide to every
platform. For each one: what it is, how it works, how we tested it, full results tables built from `results/*.json`,
real test output, RAM / CPU / disk, problems found, the paid version and alternatives. Then the recommended stack
(chosen on measured performance), the Namecheap + Coolify server, and PAID / FREE / RECOMMENDED costs at
50 / 100 / 150 users a day, with diagrams. Rebuild: `scripts/guide/diagrams.py`, then `build_guide.py`
(python-docx venv on F:, outside the project). If the guide is open in Word, the builder writes
`Kairox_Free_Stack_Guide_v2.docx`, `_v3` and so on instead; the newest number is the latest build.

**Full detail in [`docs/RESOURCE_USAGE.md`](docs/RESOURCE_USAGE.md)**: the final document (every former estimate replaced by
a measured value where one exists, problems found, free-stack tables, server sizes).
**Then [`docs/RANK.md`](docs/RANK.md)**: per service (STT, TTS, voice, calls, email, embeddings, tracing, firewall), the
best one or two free platforms against the paid service, with measured RAM / disk / CPU, results, and the monthly cost
or saving on AWS in the UAE.

| Folder | What |
|---|---|
| `plan/PLAN.md` | the plan these rounds followed, with the draft document it converts |
| `docs/` | `RESOURCE_USAGE.md` (final), `RANK.md` (free vs paid per service) and one write-up per round: `R1_RESULTS.md` (app stack), `R2_R3_RESULTS.md` (voice chain, the platform's real voice worker, speech engines), `R4_RESULTS.md` (embedding models), `R5_RESULTS.md` (trace stores), `R6_R8_RESULTS.md` (email, calls, firewall: all eight servers under load) |
| `scripts/` | load generators, fake vendors, samplers and one driver per round (`r1_driver.py` … `r678_driver.py`); `waf_probe.py` (attacks + real requests through a WAF), `recompute_windows.py` (fix for doubled RAM/CPU totals), `aws_prices.py` + `rank_costs.py` (costs in RANK), `clone_repos.sh` |
| `compose/` | one compose file per round; `r2-app-voice.yml` runs the platform's own voice code, `r5-langfuse-override.yml` only frees host ports |
| `results/` | raw data: docker stats CSVs, database snapshots, per-run JSON, logs, AWS price extracts, `browser_setup_snapshots/` (Stalwart and SafeLine web set-up) |
| `repos/` | source of every tested platform, shallow-cloned at the measured version where a tag exists; list with commit and licence in `repos/SOURCES.md`; git-ignored |

Outside this folder (large, on F:): models in `F:\kx-bench\models` and `F:\kx-bench\hf`, Python environments in
`F:\kx-bench\venv` and `F:\kx-bench\pylib`, test clips in `F:\kx-bench\clips`, other cloned repos in
`F:\kx-bench\repos` (the compose files used to run SafeLine, Langfuse, Opik and Postal), AWS price lists in
`F:\kx-bench\pricing`, Stalwart / FreeSWITCH / Postal bench configs in `F:\kx-bench\email` and `F:\kx-bench\calls`.
The Hugging Face token used for downloads is in `F:\kx-bench\hf\token` (revoke it when done).

Rounds: R1 app stack (10 users, worker concurrency 10 and 3) · R2 free voice chain (1/3/10 callers, CPU and GPU,
plus the platform's real voice worker) · R3 speech engines (CPU and GPU) · R4 Qwen3 vs BGE-M3 vs e5-large ·
R5 Phoenix vs Langfuse vs Opik · R6 Haraka / Stalwart / Postal · R7 Asterisk / FreeSWITCH · R8 CrowdSec /
BunkerWeb / SafeLine.
