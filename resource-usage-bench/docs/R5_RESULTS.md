# Round 5 results: AI-trace stores, Phoenix vs Langfuse vs Opik (14 Nov 2026)

Source data: `results/r5_phoenix.json`, `r5_langfuse.json`, `r5_opik.json`, `r5_*_stats.csv`, `r5_summary.json`.
Script: `scripts/r5_driver.py` (trace stream: `scripts/trace_replay.py`).

Each platform ran **alone** from its official setup:
- Phoenix (`arizephoenix/phoenix` 21.1) on Postgres, as the platform runs it (`r1-app.yml`, `PHOENIX_ENABLE_AUTH=false`
  as in `infra/modules/app_services`).
- Langfuse 4 from its official `docker-compose.yml` (web, worker, AI gateway, ClickHouse, MinIO, Redis, Postgres),
  with `compose/r5-langfuse-override.yml` removing only the internal host ports that clash with the developer's own
  Postgres (5432) and Valkey (6379) on this PC.
- Opik from its official `deployment/docker-compose/docker-compose.yaml --profile opik` (backend, python backend,
  frontend, ClickHouse, ZooKeeper, MySQL, MinIO, Redis).

Stream: 1,000 LLM spans at 10 per second, shaped like `kx/agents/tracing.py` (OpenInference attributes, input up to
8,000 characters, ≈ 2.2 KB of text per span on average). Per platform: 60 s idle → a **control period of the same
length with no spans** (ClickHouse, MySQL and ZooKeeper write logs and metrics on their own) → the 1,000 spans →
45 s to flush → **count what was stored** with the platform's own tables or API.

## Results

| | Phoenix | Langfuse | Opik |
|---|---|---|---|
| Containers | 1 (+ the shared Postgres) | 7 | 8 |
| Stored (checked in its own store) | 1,000 spans (`spans` table) | 1,000 (ClickHouse `events_core` and `events_full`) | 1,000 (API `spans` total) |
| RAM idle | **0.48 GB** (Phoenix 0.44 + Postgres 0.04) | 2.49 GB (web 1.27, worker 0.62, ClickHouse 0.29 …) | 2.61 GB (backend 0.69, MySQL 0.53, ClickHouse 0.45, python backend 0.43 …) |
| RAM over the whole run window (avg / peak) | 0.50 / 0.51 GB | 2.08 / 2.19 GB | 3.11 / 3.32 GB |
| CPU over the whole run window (avg / peak, all containers) | **0.05 / 0.24 core** | 0.22 / 0.74 core | 1.43 / 10.1 cores (short peaks) |
| Disk per 1,000 spans, net of the no-span control | **1.9 MB** | 29.8 MB | 98.3 MB |
| Background disk writes with no traffic (≈ 145 s) | 0 | 19 MB | 30 MB |
| Ready after `up` (images present) | 9 s | 25 s | not measured (my check counted its 404 as ready; spans were accepted after the 60 s idle) |
| Login / auth needed to ingest | none (auth off, private network) | project keys (Basic auth) | none in local mode |

Phoenix in Round 1 (real platform traffic, Haiku calls only): 4.3 KB per model call. The replayed spans carry less
text on average (≈ 2.2 KB), hence 1.9 KB per span here.

> **Corrected 14 Nov.** The first version of this table showed double the RAM and CPU (1.0 / 4.4 / 6.6 GB):
> `docker stats` streams every frame twice and the bench's `window()` summed both copies. The rows above are
> recomputed from the same raw CSVs counting each container once (`scripts/recompute_windows.py`,
> `results/recompute_windows_report.json`). "RAM idle" is a single `docker stats` reading 60 s after start and was
> not affected; Langfuse's web container happened to be larger at that moment (1.27 GB) than during most of the run.

## What the numbers say

- **Phoenix is by far the lightest**: about a quarter to a sixth of the RAM and 1/15 to 1/50 of the disk of the
  ClickHouse-based tools, and it reuses the platform's Postgres (one database, one backup).
- Langfuse and Opik need ClickHouse (+ ZooKeeper for Opik), object storage and their own SQL database: 2.5 GB RAM
  before any traffic, and they write tens of MB on their own.
- **Caveat on the ClickHouse numbers:** ClickHouse stores each insert batch as a small part and merges them later,
  so a 100-second test overstates its long-run size per span. The Phoenix figure is exact (database size).
- At the platform's Normal level (65,000 AI turns ≈ 140,000 model calls a month): Phoenix ≈ 0.3–0.6 GB a month;
  Langfuse ≈ 4 GB and Opik ≈ 14 GB if the short-test rate held (an upper bound, see caveat).

## Bench notes

- First run: Langfuse never started (its compose publishes 5432/6379, already used by the developer's containers);
  fixed with the port override. Storage was then unverified (its public `observations` endpoint returns 404 in v4)
  and disk growth was not net of background writes. Both fixed and all three rerun; the numbers above are the rerun.
