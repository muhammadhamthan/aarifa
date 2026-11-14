# Plan: measure everything for real (10 fake users) and test the free stack

## Context

The draft document below estimates RAM, disk, CPU and network for the platform, but most numbers are marked (E). You asked for three things:
- run the section 9 measurement so every (E) becomes (M);
- install and load-test every platform in `free version.docx` (repo root);
- record RAM, disk and bandwidth with 10 fake users, downloading models from Hugging Face and cloning repos where needed.

Decisions you made:
- You move Docker's disk image to F: before the run.
- Claude is replaced by a fake local server.
- All three groups are tested: core picks, voice/tracing alternatives, and email/calls/firewall.
- Speech models are measured on CPU **and** on GPU.

## Machine limits (measured 14 Nov)

| Item | Value |
|---|---|
| CPU | i5-13400F: 10 cores, 16 threads |
| RAM | 15.8 GB (2.1 GB free at check time) |
| Docker VM RAM | ~7.7 GB cap (no `.wslconfig`) |
| GPU | RTX 5060, 8 GB |
| Disk | **C: 1.0 GB free**; F: 196 GB free |
| Already running | `kairox-platform-postgres-1`, `-valkey-1`, `librecrawl` |

So:
- each round must fit in ~7 GB of Docker RAM;
- every download goes to `F:\kx-bench\` (`HF_HOME=F:\kx-bench\hf`);
- the bench uses its own compose project (`kxbench`) and other ports, so your running containers and volumes are never touched.

## Before I start (you)

1. Docker Desktop → Settings → Resources → Advanced → Disk image location → `F:\Docker`. Wait for the move to finish, then confirm `docker info` works.
2. Optional: close heavy apps to free RAM. If you want bigger rounds, add `memory=12GB` to `%USERPROFILE%\.wslconfig`.

## Tools I build (no new project dependencies)

All bench files go in `scripts/bench/` (so the same test can be re-run on the real server). Outputs go to `F:\kx-bench\results\`.

- **`fake_claude.py`**: answers `POST /v1/messages` like the Claude API.
  - ~3 s delay, realistic `usage` token counts, sometimes a `tool_use` block (knowledge search).
  - The app reaches it through `ANTHROPIC_BASE_URL`, so the app's code path is unchanged.
  - Fake Meta Graph and Postmark endpoints are added the same way if their base URLs are configurable; otherwise outbound sends are counted at the mock.
- **`load10.py`** (asyncio + httpx, already in `uv.lock`): 10 users for 15 minutes, 5–15 s pause between actions. The traffic mix:
  - website chat sessions and messages;
  - signed WhatsApp inbound webhooks;
  - staff login + CRM list screens;
  - quote create → approve → PDF;
  - one CMS publish that re-indexes a 2,500-chunk catalogue in 6 languages.
- **`sample.py`**: every 2 s records `docker stats` (CPU, RAM, network I/O, disk I/O) to CSV. Before and after each round it also records:
  - `docker system df -v`;
  - per-table `pg_total_relation_size` + row counts;
  - Valkey `INFO memory`;
  - peak `pg_stat_activity` connections.
- **`voice_callers.py`**: 10 fake callers join LiveKit rooms (the `livekit` rtc SDK is already in the venv) and play pre-made Arabic and English speech clips made with Piper. It records time to first reply audio.
- **Speech-model tests** run in a separate Python 3.12 venv on F: so the project's `uv.lock` is untouched. Torch cu128 is needed for the RTX 5060.

## Rounds (each: start → idle 2 min → 10 users for 15 min → measure → `down -v`)

| Round | What runs | Measures |
|---|---|---|
| R1 App stack (section 9) | Production image built from `Dockerfile` (amd64), Postgres, Valkey, migrate, api, worker, beat, TEI Qwen3-0.6B, Phoenix, fake Claude | RAM/CPU per process idle and busy. Worker at concurrency 10 **and** 3. Bytes per AI turn / message / quote / PDF. Phoenix bytes per turn. Peak DB connections. Network per turn. Image sizes. |
| R2 Free voice chain | LiveKit Server (dev), LiveKit Agents + turn detector + Silero, faster-whisper large-v3-turbo and Piper behind OpenAI-compatible endpoints (bench-only agent; the app's voice code is not changed) | RAM per idle/active call process, CPU per call, time to first audio, bandwidth per call minute. CPU int8 vs GPU fp16. |
| R3 Speech alternatives | Chatterbox TTS, whisper.cpp, sherpa-onnx Whisper turbo, Pipecat bot with the same models | Real-time factor with 10 parallel clips, RAM, model disk, CPU vs GPU |
| R4 Embeddings | TEI Qwen3-0.6B vs BGE-M3 (CPU; GPU if TEI supports the 5060, else sentence-transformers on GPU) | RAM, latency with 10 concurrent queries, catalogue index time, vector disk |
| R5 Tracing | Phoenix vs Langfuse (official compose: Postgres + ClickHouse + Redis + MinIO) vs Opik (official compose); the same recorded R1 trace stream replayed over OTLP | Idle RAM, RAM/CPU under load, disk per 1,000 traces |
| R6 Email | Stalwart, Postal, Haraka; 1,000 test emails to a local sink (nothing leaves the PC) | Idle RAM, RAM/CPU per 1,000 emails, disk per 1,000 |
| R7 Calls | Asterisk, FreeSWITCH; SIPp with 10 calls at once, RTP audio, all local (no phone lines) | Idle RAM, CPU and bandwidth per call |
| R8 Firewall | CrowdSec (parses 100k log lines); BunkerWeb (with OWASP CRS) and SafeLine in front of the api, R1 traffic routed through each; ALTCHA verify cost from its library | RAM, CPU, added latency per request |

If a round does not fit in ~7 GB, it is split, and the result notes it.

## What you get

1. **`docs/RESOURCE_USAGE.md`**: the document below with every (E) replaced by (M) where a round measured it. Anything still unmeasurable stays (E) with the reason.
2. **A new section, "Free stack, measured"**: one table per field with idle RAM, RAM with 10 users, CPU, disk (image + growth), bandwidth, speed or latency, and licence. Also server sizes for "AWS-free paid APIs" vs "everything free/self-hosted".
3. Raw CSVs in `F:\kx-bench\results\`, plus a short note in `docs/V1_PROGRESS.md`. Nothing in the SRS, platform docs or `db/ddl` is touched.

## Verification

- Every number in the final document cites the CSV or command it came from.
- Spot-check: re-run one round (R1, 5 minutes) and confirm RAM figures within ±10%.
- `make test` still passes. The bench files are separate, and app code is not changed.

## Time

About 6–10 hours, mostly downloads and 15-minute load windows. I'll report after each round.

---

# Appendix: draft document (template; numbers get replaced after the rounds)

# Arifa Pack platform: what uses RAM, disk, CPU, network and money

Checked 13 Nov 2026 against the code on branch `v1-11nov-work-and-architecture-guide`.

**How to read the numbers**
- **(M) measured** on the dev PC today: Python processes loading the real code, plus the running Postgres and Valkey containers.
- **(C) read from the code**: a default value or setting in the repo, with the file named.
- **(D) from the repo docs**: `Recommendation.md` usage levels and prices, `infra/README.md` container sizes.
- **(E) estimate**: the basis is given. The local databases are almost empty, so per-row sizes could not be measured yet. Section 9 says how to measure them.

Usage levels used throughout (D, `Recommendation.md`):

| Per month | Light | Normal | Heavy |
|---|---|---|---|
| AI turns | 14,000 | 65,000 | 210,000 |
| Website voice minutes | 1,500 | 7,500 | 25,000 |
| WhatsApp messages | 3,900 | 19,500 | 65,000 |
| Emails out / in | 2,000 / 500 | 10,000 / 2,500 | 40,000 / 8,000 |

---

## 1. The one idea to understand first

The platform uses resources in three different ways:

1. **Fixed server resources (mostly RAM).** These programs run all day whether anyone uses the system or not. They decide the server size.
2. **Growing resources (disk).** Every conversation, message and quote adds rows to the database and files to storage. These decide the disk size and the backup size.
3. **Pay-per-use outside services (money).** The expensive "thinking" happens outside your server: Claude writes the replies, Deepgram turns speech into text, ElevenLabs turns text into speech, Meta delivers WhatsApp. Your server mostly sends a request and waits for the answer.

So the server is the waiter, not the kitchen. That is why a modest server is enough, and why most of the monthly bill is usage, not hosting.

---

## 2. Who runs on the server, and what each one consumes (RAM)

| Program | Which feature needs it | Measured | RAM with today's defaults | RAM tuned (section 7) |
|---|---|---|---|---|
| **Postgres + pgvector** | Stores everything: CRM, automation, AI memory, CMS, Phoenix traces (5 databases) | 138 MB idle with the 128 MB default cache (M) | 1.5–3 GB (E: cache + ~5–10 MB per connection) | ~1.5 GB |
| **Valkey / Redis** | Celery job queue, rate limits, daily spend caps, kill switches | 12 MB idle (M); `noeviction` (M) | 50–150 MB (E) | same |
| **api** (1 uvicorn process) | CRM screens' API, website AI chat, WhatsApp/Postmark/SES webhooks, staff login | 253 MB just loading the code (M) | 350–500 MB (E) | same |
| **worker** (Celery) | WhatsApp/email AI replies (`agents.reply` job), deliveries, follow-ups, approval ladder, quote PDFs, reports | 236 MB **per child process** + 58 MB parent (M) | **10 children** (C: `worker_concurrency = 10` in `config.py`) → **2.4–3.5 GB** | 3 children → 0.8–1.1 GB |
| ↳ Chromium | Draws each quote PDF (Arabic shaping); a new process per PDF (C: `pdf.py:279`) | — | +150–300 MB for 1–3 s per PDF (E) | same |
| **beat** (exactly one) | Timers only: sends ~2,430 small tasks an hour (C: `schedule.py`) | 58 MB (M) | 60–100 MB | same |
| **voice** (LiveKit agent) | Website voice calls: speech in → Claude → speech out | 130 MB for LiveKit + 236 MB for the app, per call process (M) | Keeps **min(cores, 4) warm processes** (C: LiveKit default) + 1 per live call → 1.2–2 GB | 1 warm → 0.4–0.8 GB, +~0.4 GB per live call |
| **embeddings** (Qwen3-0.6B on TEI) | "Search the product knowledge" on every AI question; re-index on CMS publish | — | **2.5–3 GB** (E: 0.6 billion numbers × 4 bytes) | same; this is the biggest fixed user |
| **Phoenix** | Screen to inspect every AI answer (traces) | — | 0.3–0.6 GB (E) | same |
| **Payload CMS** (Node) | Website admin: products, photos, text | — | 0.3–0.5 GB (E) | same |
| **Next.js** ×2 | CRM screens + public website, **only if hosted on your server** | — | 0.3–0.6 GB (E) | 0 on Vercel |
| OS + Docker + reverse proxy | | — | 0.5–0.8 GB (E) | same |
| **Total** | | | **≈ 9.5–15 GB** | **≈ 6.5–10 GB** |

**What this means for server size:** tuned, everything fits a **16 GB** machine with room to spare. 12 GB works but is tight. Left on today's defaults, plan for **24–32 GB**.

**For comparison:** the AWS plan *reserves* about **26 GB** (D, `infra/README.md`): app containers 7.5 vCPU / 17 GB, RDS 8 GB and Redis 1.4 GB. You pay for reserved capacity on Fargate whether it is used or not.

---

## 3. CPU: who uses the processor, and when

| Activity | CPU cost | Notes |
|---|---|---|
| Background timers (Beat) | ~1–3% of one core, all day (E) | ~2,430 tiny tasks an hour: 3 outbox relays every 5 s, schedule sweep every 30 s, heartbeat every 30 s, 5-min sweeps (C) |
| One AI text reply | ~50–150 ms of CPU on our side (E) | Then 2–10 s *waiting* for Claude, which uses no CPU |
| Knowledge search (1 per question) | ~0.1–0.3 s on 1–2 cores (E) | The Qwen3 model runs on the CPU |
| Re-indexing the catalogue | ~0.3–1 s per chunk (E) | Example: 2,500 chunks ≈ 15–40 min of one core, only when CMS content is published |
| Quote PDF | 1–3 s of one core (E) | A short burst |
| Live voice call | ~0.1–0.2 core per call (E) | Voice detection (Silero) + audio handling. LiveKit refuses new calls above **70% CPU** (C: library default) |
| Postgres | Low at these volumes (E) | |

**How many things happen at the same time (E):** busiest hour = twice the average, over 10 working hours a day, ~6 s per AI reply.

| | Light | Normal | Heavy |
|---|---|---|---|
| AI replies running at once | ~0.2 | ~0.7 | ~2–3 |
| Voice calls at once (peak) | ~1 | 2–3 | 3–8 |
| Cores needed | 2–4 | 4–6 | 8 |

WhatsApp and email replies run in worker children, one reply per child at a time. Website chat runs in `api`. So **3 worker children cover Light and Normal**, and 4–6 cover Heavy. The default of 10 only costs RAM.

---

## 4. Disk: what is stored and how fast it grows

**Fixed (day one)**

| What | Size |
|---|---|
| Operating system | 5–10 GB (E) |
| Docker images, measured | pgvector 631 MB (M), Valkey 170 MB (M), local app test images 479–701 MB (M) |
| Docker images, estimated | Production app image with Chromium + Noto CJK fonts ~1–1.5 GB (E); TEI ~1–2 GB (E); Qwen3 model download ~1.2 GB (E); Phoenix ~1 GB (E); CMS / Next.js ~0.5–1 GB each (E). Keep 2 versions for rollback. |
| Empty databases | 8–11 MB each (M) |
| **Total fixed** | **≈ 15–25 GB** |

**Growing (per month, E)**. The per-unit sizes include indexes.

| Data (where) | Per unit | Light | Normal | Heavy |
|---|---|---|---|---|
| AI conversation rows: turns, runs, tool calls, guard events, outbox, audit, timeline, job rows (Postgres) | ~15 KB per turn | 0.2 GB | 1.0 GB | 3.2 GB |
| **Phoenix traces** (Postgres): up to 8,000 chars of prompt + 8,000 of reply per model call (C: `tracing.py:22`) | ~30 KB per turn | **0.4 GB** | **2.0 GB** | **6.3 GB** |
| WhatsApp messages + delivery-status webhooks (Postgres) | ~7 KB per message | 0.03 GB | 0.14 GB | 0.46 GB |
| Emails (Postgres) | ~2 KB out, ~20 KB in | 0.01 GB | 0.07 GB | 0.24 GB |
| Postgres overhead (WAL, dead rows) | +30% | | | |
| **Database total per month** | | **≈ 0.9 GB** | **≈ 4.2 GB** | **≈ 13 GB** |
| Files: quote PDFs (~200 KB each), WhatsApp media buyers send, staff-call recordings (object storage) | | 0.1–0.3 GB | 0.5–1 GB | 2–3 GB |

Website voice calls are **not** recorded (C: no audio upload in the voice path), so voice adds almost nothing to disk.

**After 12 months**

| | Light | Normal | Heavy |
|---|---|---|---|
| Database, no cleanup | ~11 GB | ~50 GB | ~160 GB |
| Database, with a 30-day Phoenix retention | ~4.5 GB | ~21 GB | ~68 GB |

**Phoenix traces are about two-thirds of all database growth.** Backups add roughly 1–2× the database size off-site (30 days kept, compressed).

**Disk to buy:** 100–200 GB SSD covers Light and Normal for the first year. Heavy needs about 500 GB.

---

## 5. Network (bandwidth) per month (E)

| What | Per unit | Light | Normal | Heavy |
|---|---|---|---|---|
| Voice audio (buyer ↔ LiveKit ↔ our agent ↔ Deepgram / ElevenLabs) | ~3–4 MB per call minute | ~5 GB | ~25 GB | ~85 GB |
| Prompts to Claude | ~35 KB per turn | 0.5 GB | 2.3 GB | 7.4 GB |
| WhatsApp, email, webhooks | | <1 GB | <1 GB | ~1 GB |
| Website and CRM pages | depends on visitors; Cloudflare caching cuts most of it | | | |
| **Total** | | **~10 GB** | **~35 GB** | **~110 GB** |

Every server plan includes far more than this (terabytes), so **bandwidth is not a cost concern**.

---

## 6. Money: what each feature costs outside the server (D)

Line costs are from `Recommendation.md`. The per-unit costs are those lines divided by the usage levels at the top.

| Feature | Service | Per unit | Light | Normal | Heavy |
|---|---|---|---|---|---|
| AI replies (all channels) | Claude Sonnet 5.5 (money turns) + Haiku 5.5 (the rest) | ~$0.0035 per turn | $49 | $228 | $735 |
| WhatsApp | Meta Cloud API | ~$0.007–0.012 per message | $26 | $213 | $756 |
| Website voice: speech → text | Deepgram | | $14 | $69 | $230 |
| Website voice: text → speech | ElevenLabs | | $27 | $135 | $450 |
| Website voice: transport | LiveKit Cloud | | $50 | $75 | $250 |
| Voice total | | $0.06/min (Light) → $0.037/min | $91 | $279 | $930 |
| Staff alert calls / call-backs | Telnyx → Twilio | mostly a fixed number fee | $10 | $46 | $152 |
| Email | SES + Postmark | mostly Postmark's fixed fee | $17 | $19 | $22 |
| **Usage total** | | | **≈ $193** | **≈ $785** | **≈ $2,595** |

Biggest money users:
- **Light:** voice (LiveKit's fixed fee) and Claude.
- **Normal and Heavy:** WhatsApp and voice.

On the server side only, the biggest users are:
- **RAM:** embeddings, the worker at its defaults, Postgres.
- **Disk:** Phoenix traces, then conversation and outbox rows.
- **CPU bursts:** catalogue re-indexing, PDFs, live calls.

---

## 7. Settings that change consumption (all exist today)

| Setting | Default | Change to | Effect |
|---|---|---|---|
| `KX_APP_WORKER_CONCURRENCY` | 10 (C) | 3 (Light/Normal), 5 (Heavy) | Saves **1.5–2.5 GB RAM**. Still answers every buyer in time (section 3). |
| `KX_DB_POOL_SIZE` / `KX_DB_MAX_OVERFLOW` | 10 / 10 per database per process (C) | 5 / 5 | Fewer Postgres connections (see gap 3) |
| Postgres `max_connections` | 100 (M) | 200, or keep 100 with smaller pools | Prevents "too many clients" errors |
| Postgres `shared_buffers` | 128 MB (M) | 512 MB–1 GB | Faster queries; uses that much RAM |
| Phoenix trace retention | keeps forever | 30–90 days (a Phoenix setting) | Caps the biggest disk grower |
| `MAX_CHARS` in `kx/agents/tracing.py:22` | 8,000 (C) | 4,000 | Halves Phoenix disk (code change) |
| `KX_APP_RELAY_SECONDS` / `KX_APP_BEAT_SWEEP_SECONDS` | 5 s / 30 s (C) | keep | Lower values = faster replies but more background load |
| LiveKit `num_idle_processes` | not set → min(cores, 4) warm processes (C) | 1 | Saves ~0.5–1 GB RAM (one line in `cli.py`, `WorkerOptions`) |
| Docker log size | unlimited | `max-size 50m`, 3 files | Stops logs from filling the disk |

---

## 8. Problems found in the code that affect consumption

1. **Some tables are never cleaned up.** The nightly `crm.retention` job only *anonymises* people (C: `kx/crm/services/retention.py`). Nothing deletes old rows from `outbox_events` / `inbox_events` (in 4 databases), `webhook_receipts`, `idempotency_keys`, or finished `scheduled_jobs`, so they grow forever. Audit rows must stay; the others can be pruned after 30–90 days. This needs a new work package.
2. **Worker RAM vs the AWS size.** Ten worker children × ~240 MB ≈ 2.4 GB+, but `infra/README.md` gives the worker 2 GB. On AWS that would risk out-of-memory restarts too. Set `KX_APP_WORKER_CONCURRENCY` wherever it is deployed.
3. **Database connections can exceed 100.** Every process that builds the app container opens:
   - a LangGraph pool that starts with 4 connections (C: `container.py:295`, psycopg default `min_size=4`);
   - up to 10 + 10 connections for each of the 3 databases (C).

   With 10 worker children, the API, voice, Phoenix and the CMS, about 150 connections are possible (E). That is above Postgres' default of 100. RDS sets a higher limit automatically; a self-managed Postgres does not.
4. **Voice keeps up to 4 warm processes** of ~250–400 MB each, even with no calls (C: LiveKit default).

---

## 9. How to turn the estimates into measured numbers (next step, ~1 hour)

1. Start the full stack locally: `docker compose up --build` plus `--profile ai` and `--profile voice`. Record `docker stats --no-stream` while idle.
2. Run the scenario tests (`make test`) and a short script that sends ~200 fake buyer messages with Claude mocked. Record `docker stats` again while busy.
3. In Postgres, read `pg_total_relation_size` and row counts per table → real bytes per AI turn and per message.
4. Replace every (E) in this document with (M).

## After you approve

- Save this document as `docs/RESOURCE_USAGE.md` (a new file; the SRS and platform docs stay untouched). Publish it as a shareable page if you want to show the client.
- Optional follow-up work packages, each needing your go-ahead:
  - the cleanup job (gap 1);
  - the LiveKit warm-process setting (gap 4);
  - the measurement run in section 9.
