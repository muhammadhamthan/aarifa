# Round 1 results: the app stack, measured with 10 fake users (14 Nov 2026)

Source data: `results/r1_summary.json` (from `scripts/analyze_r1.py`), `results/r1_load_c10.json`,
`results/r1_load_c3.json`, `results/r1_load_phx.json`, snapshots `results/r1_snap_*.json`, stats
`results/r1_stats*.csv` (docker stats every ~2 s).

Machine: i5-13400F (16 threads), Docker VM 8.2 GB RAM. The app ran from the production `Dockerfile` image; Claude,
WhatsApp, SES, Twilio, S3 and the CMS were replaced by local fakes (`scripts/fake_services.py`), so no vendor
was called and no money was spent. CPU % is per core (100 % = one full core).

## Traffic

| | Worker concurrency 10 (default) | Worker concurrency 3 |
|---|---|---|
| Window | 15.2 min | 10.2 min |
| Users | 5 website chat, 4 WhatsApp buyers, 1 staff member | same |
| Buyer turns (web messages + WhatsApp messages) | 555 | 375 |
| Model calls (fake Claude) | 1,237 (2.2 per turn) | 854 (2.3 per turn) |
| Staff actions | 85 × (deals list, deal, timeline, quotes, inbox) + 14 qualify + 14 manual quotes | 57 × same + 8 + 10 |
| Website reply time (includes the ~3 s fake model delay) | p50 5.1 s, p95 9.4 s | p50 5.3 s, p95 9.4 s |
| WhatsApp webhook answer | p50 14 ms, p95 45 ms | p50 13 ms, p95 32 ms |
| CRM screens (deal, list, timeline, inbox) | p50 5–11 ms, p95 ≤ 23 ms | same |

## RAM and CPU per program (M)

| Program | Idle RAM | RAM with 10 users (avg / peak) | CPU with 10 users (avg / peak) |
|---|---|---|---|
| api (1 uvicorn) | 350 MiB | 583 / 817 MiB | 0.14 / 4.9 cores |
| worker, concurrency 10 | 718 MiB (just started) → 2.36 GiB after first jobs | 2.55 / 2.60 GiB | 0.09 / 2.1 cores |
| worker, concurrency 3 | 858 MiB | 886 / 902 MiB | 0.05 / 0.6 cores |
| beat | 38 MiB | 73 / 85 MiB | 0.01 core |
| Postgres (5 databases) | 194 MiB | 397 / 430 MiB | 0.03 / 0.37 cores |
| Redis | 10 MiB | 10 / 12 MiB | < 0.01 core |
| Embeddings (TEI CPU 1.8, Qwen3-0.6B) | 2.86 GiB | 2.39 / 2.54 GiB | 0.8 / 8.6 cores |
| Embeddings while re-indexing the catalogue | | 2.90 / 3.10 GiB | 8.0 / 10.9 cores (all it can get) |
| Phoenix (traces UI) | 176 MiB | 95 / 105 MiB | 0.01 core |

Sum of per-program peaks with 10 users, default settings: ≈ 6.6 GiB (api 0.80 + worker 2.60 + beat 0.08 +
Postgres 0.42 + Redis 0.01 + embeddings 2.48 + Phoenix 0.10). With worker concurrency 3: ≈ 4.5 GiB.
Worker concurrency 3 handled the same traffic with the same reply times and saved ≈ 1.7 GiB.

## Disk growth per business unit (M)

| What | Measured | Draft estimate |
|---|---|---|
| App databases (crm + automation + agents), per buyer turn | 6.5–8.0 KB | ~15 KB |
| Phoenix traces, per model call | 4.3 KB (≈ 9.3 KB per buyer turn) | ~30 KB per turn |
| Quote PDF in object storage | 52 KB each | ~200 KB |
| Biggest growers | `automation.scheduled_jobs` (~1.1 KB per row, ~1.7 rows per turn), `crm.audit_events`, `crm.timeline_events`, outbox tables in 3 databases, `agents.agent_runs` | |

Per month, at the usage levels in the draft (14k / 65k / 210k AI turns), app databases + Phoenix ≈
0.24 / 1.1 / 3.6 GB (≈ 17 KB per turn; before index bloat and WAL), about a quarter of the draft estimate
(0.9 / 4.2 / 13 GB). Caveat: Sonnet calls never ran (problem 1), so the larger Sonnet prompts are not in
these traces.

## Network per buyer turn (M)

| Link | Bytes |
|---|---|
| Server → Claude (prompts) | ≈ 19.4 KB up, 1.1 KB down per turn (≈ 8.7 KB up per model call) |
| WhatsApp Graph API | ≈ 2 KB per outbound message |
| Postgres traffic inside the server | ≈ 80 KB per turn (internal only) |

## Connections and queues (M)

| | Concurrency 10 | Concurrency 3 |
|---|---|---|
| Postgres connections after load | 106 (agents 59, automation 17, crm 16, phoenix 5, other 9) | 57 |
| Redis memory | 2.0 MB used (peak 2.9 MB) | same |

With the default concurrency the platform already passes Postgres's default `max_connections = 100` with a single
worker container and no voice worker or CMS.

## Catalogue re-index (M)

One CMS publish of 100 products × 6 languages queued ~600 re-index jobs. On CPU, Qwen3-0.6B embedded 5.5 s per
chunk (10 cores busy). The default `KX_APP_EMBEDDING_TIMEOUT_SECONDS=2.0` is far below the ≈ 26 s each indexing
request took under load, so re-indexing fails with the default setting (the bench used 120 s).

## Problems found (not fixed: the bench never edits source)

1. **Every Claude Sonnet call fails before it leaves the server.** `kx/agents/models.py:35` sends
   `temperature=0.2`; the installed `langchain_anthropic` rejects any non-default temperature for
   `claude-sonnet-5-5` (`ValueError: temperature is not supported for claude-sonnet-5-5 at non-default values`,
   19 times during the two load phases). The money turns (pricing, negotiation) therefore fail; website buyers get HTTP 500
   (12 of 299 and 7 of 200 messages). The fake Claude server saw 0 Sonnet calls. Tests miss it because they use a
   fake model.
2. **Heartbeat task fails every 30 s** (`kx/core/metrics.py:46` uses `sys.stdout.buffer`, which Celery replaces):
   the Beat heartbeat and queue-lag metrics are never sent.
3. **WhatsApp message from a number the app cannot parse → HTTP 500** (`ValueError: invalid phone number`).
   Meta retries failed webhooks, so the same message keeps coming back. It should be logged and acknowledged.
4. **Embedding timeout default (2 s) breaks catalogue re-indexing on CPU** (see above).
5. **Postgres connections exceed 100 at default concurrency** (106 measured).
6. **Worker at concurrency 10 needs ≈ 2.6 GiB**, above the 2 GB the AWS plan gives it.
7. **TEI image `cpu-1.7.2` (pinned in the repo) crashes with Qwen3 on CPU**; `cpu-1.8` works but needs
   `--auto-truncate` and a small `--max-batch-tokens` (1024 here; larger values ran out of memory at warm-up).
   The TEI CPU image is amd64-only, while the AWS plan uses ARM.

## Bench notes (my mistakes, fixed, not app problems)

- The bench first ran without `KX_AUTH__ENCRYPTION_KEY`; after a container restart the staff TOTP seed could not be
  decrypted and the load script retried forever. Fixed with a fixed bench key and a hard stop in the script.
- Random fake numbers with invalid UAE prefixes (+971 51/53/57/59) caused the WhatsApp 500s above; the generator
  now uses real prefixes. The 500 itself is still finding 3.
- Phoenix 21 turns login on by default; the bench rejected traces (401) until `PHOENIX_ENABLE_AUTH=false` was set,
  as production already does. Phoenix numbers above come from the 5-minute rerun after that fix.
- Approve clicks returned 422 "contact has no WhatsApp number or email to send the quote to": correct behaviour
  for website buyers who left no contact details.
