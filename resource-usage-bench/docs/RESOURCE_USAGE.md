# Arifa Pack platform: what uses RAM, disk, CPU, network and money — measured

Measured 14 Nov 2026 on branch `v1-11nov-work-and-architecture-guide`, with 10 fake users on a test PC. Every number
marked **(M)** comes from a run whose raw data is in `resource-usage-bench/results/`; the round write-ups are next
to this file (`R1_RESULTS.md`, `R2_R3_RESULTS.md`, `R4_RESULTS.md`, `R5_RESULTS.md`, `R6_R8_RESULTS.md`).

**How to read the numbers**
- **(M) measured** with the production image built from the repo's `Dockerfile` and real upstream software (LiveKit
  server built from its source, official images and compose files for the rest). Paid services (Claude, WhatsApp,
  SES, Twilio, the CMS) were replaced by local fakes, so no vendor was called and nothing was spent.
- **(C) read from the code**, **(D) from the repo docs** (`Recommendation.md` usage levels and prices,
  `infra/README.md` sizes).
- **(E) estimate**, kept only where nothing could be measured; the reason is given.

Test machine: i5-13400F (10 cores / 16 threads), 15.8 GB RAM, Docker VM 8.2 GB, RTX 5060 8 GB. A cloud vCPU is
usually slower than one of these threads, so treat CPU figures as a floor.

Usage levels used throughout (D, `Recommendation.md`):

| Per month | Light | Normal | Heavy |
|---|---|---|---|
| AI turns | 14,000 | 65,000 | 210,000 |
| Website voice minutes | 1,500 | 7,500 | 25,000 |
| WhatsApp messages | 3,900 | 19,500 | 65,000 |
| Emails out / in | 2,000 / 500 | 10,000 / 2,500 | 40,000 / 8,000 |

---

## 0. Read this first: problems the measurement found

The measurement ran the real code under load, and some of it does not work as shipped. Nothing was changed in the
source; each item says where it is.

| # | Problem | Effect | Where |
|---|---|---|---|
| 1 | Every Claude **Sonnet** call fails before it is sent: `temperature=0.2` is rejected by the installed `langchain_anthropic` for `claude-sonnet-5-5` | Pricing and negotiation turns fail; website buyers get HTTP 500 (12 of 299 messages in Round 1). Tests miss it (fake model) | `kx/agents/models.py:35` |
| 2 | **`kx voice` cannot start a job**: the job function is defined inside `_voice`; LiveKit pickles it ("forkserver" on Linux) | 3,380 start failures in 90 s, no call ever answered | `arifaa_pack/cli.py:_voice` |
| 3 | **The voice agent never replies**: the session has no LLM, and LiveKit 1.8.10 skips the reply before `llm_node` | Caller hears the recording notice, then silence, no error | `kx/agents/voice.py` (`AgentSession(...)`) |
| 4 | Every voice call builds the whole app container at its start (not in prewarm) | Under 10 simultaneous calls, 7 calls lost their first 1–2 turns; replacement processes hit LiveKit's 10 s start timeout | `arifaa_pack/cli.py:_voice` |
| 5 | WhatsApp message from a number the app cannot parse returns **HTTP 500** | Meta retries failed webhooks, so the message keeps coming back | WhatsApp webhook handler (`ValueError: invalid phone number`) |
| 6 | Heartbeat task fails every 30 s (`sys.stdout.buffer` under Celery) | Beat heartbeat and queue-lag metric never sent | `kx/core/metrics.py:46` |
| 7 | Embedding timeout default 2 s, but CPU re-index requests take ≈ 26 s | Catalogue re-indexing fails with the default | `KX_APP_EMBEDDING_TIMEOUT_SECONDS` |
| 8 | Postgres connections reach **106** at default worker concurrency (limit 100) | "Too many clients" once voice, CMS or a second worker is added | `KX_APP_WORKER_CONCURRENCY`, pool sizes |
| 9 | Worker at concurrency 10 needs ≈ 2.6 GiB; the AWS plan gives it 2 GB | Out-of-memory restarts on AWS | `infra/README.md` sizes |
| 10 | Repo-pinned TEI `cpu-1.7.2` crashes with Qwen3 on CPU; `cpu-1.8` works with `--auto-truncate` and `--max-batch-tokens 1024`; the TEI CPU image is amd64-only (AWS plan uses ARM) | Embedding server does not start as pinned | `docker-compose.yml`, infra |
| 11 | Tables never pruned: `outbox_events`, `inbox_events`, `webhook_receipts`, `idempotency_keys`, finished `scheduled_jobs` (C) | `scheduled_jobs` alone grows ≈ 1.7 rows / 1.9 KB per buyer turn (M) | no retention job |
| 12 | A WAF with default rules in front of the API (BunkerWeb + OWASP CRS, as `free version.docx` proposes) **blocks every WhatsApp webhook** (89 of 89 → 403) and 2 of 93 website chat messages. Cause (BunkerWeb log): CRS 4.20.0 rule 930120 matches `.profile` in Meta's field `contacts[0].profile.name`. SafeLine with default protection passed every webhook and still blocked the same attacks | WhatsApp silently stops working | WAF config: exclude rule 930120 (`ARGS_NAMES`) on `/webhooks/whatsapp`, or use SafeLine |

---

## 1. The one idea to understand first

The platform uses resources in three ways:

1. **Fixed server resources (mostly RAM).** Programs that run all day. They decide the server size.
2. **Growing resources (disk).** Conversations, messages, quotes and traces add rows and files.
3. **Pay-per-use outside services (money).** Claude, Deepgram, ElevenLabs, Meta. The server sends a request and waits.

The measurements confirm it: with 10 users the app's own CPU stayed under one core on average, the biggest RAM user
is the embedding model, and most "work" is waiting for outside services. The exceptions are the free, self-hosted
replacements for those services (speech-to-text above all), which move that work onto your server (section 9).

---

## 2. Who runs on the server, and what each one consumes (RAM)

| Program | Feature | Idle (M) | With 10 users, avg / peak (M) | Draft estimate |
|---|---|---|---|---|
| Postgres + pgvector (5 databases) | Everything stored | 194 MiB | 397 / 430 MiB | 1.5–3 GB (E) |
| Redis | Celery queue, limits, caps | 10 MiB | 10 / 12 MiB (2 MB of data) | 50–150 MB |
| api (1 uvicorn) | CRM API, website chat, webhooks | 350 MiB | 583 / 817 MiB | 350–500 MB |
| worker, concurrency 10 (default) | WhatsApp/email replies, PDFs, jobs | 0.72 GiB fresh, 2.36 GiB after first jobs | 2.55 / **2.60 GiB** | 2.4–3.5 GB |
| worker, concurrency 3 | same | 0.86 GiB | 0.89 / **0.90 GiB** | 0.8–1.1 GB |
| ↳ Chromium per quote PDF | inside the worker numbers above | — | not isolated (in worker peak) | +150–300 MB (E) |
| beat | timers | 38 MiB | 73 / 85 MiB | 60–100 MB |
| embeddings (TEI CPU, Qwen3-0.6B) | knowledge search, re-index | **2.82–2.86 GiB** | 2.39–2.78 / 3.10 GiB (re-index peak) | 2.5–3 GB |
| Phoenix (traces) | AI trace screen | 0.18 GiB (Round 1) – 0.44 GiB (Round 5) | ≈ 1.0 GiB while ingesting 10 spans/s | 0.3–0.6 GB |
| voice worker (platform code, 4 warm processes) | website voice calls | **1.41 GiB** | 1 call 1.75, 3 calls 2.33, 10 calls **4.21 GiB** | 1.2–2 GB |
| Payload CMS (Node) | website admin | not measured (no CMS in the bench; a fake served its API) | | 0.3–0.5 GB (E) |
| Next.js ×2 (if self-hosted) | CRM + website | not measured | | 0.3–0.6 GB (E) |
| OS + Docker + reverse proxy | | not measured on a server | | 0.5–0.8 GB (E) |

**Totals (M, sum of per-program peaks, 10 users, no voice / CMS / frontends):**
**6.6 GiB** with today's defaults, **4.5 GiB** with worker concurrency 3 — the same traffic, the same reply times.
Add the voice worker (1.4 GiB idle, ≈ 2–4 GiB in use), the CMS and frontends (≈ 1 GB, E) and the OS (≈ 0.5–0.8 GB, E).

**Server size (M + E):** paid speech services, tuned → ≈ 8–10 GB in use → **16 GB server**. Defaults → 12–14 GB.
The AWS plan reserves ≈ 26 GB (D).

---

## 3. CPU: who uses the processor, and when

| Activity | Measured | Draft estimate |
|---|---|---|
| Background timers (beat) | **0.012 core** average idle (M) | 1–3 % of a core |
| One AI turn, platform side (api + worker + Postgres + beat + Redis) | **≈ 0.46 core-seconds** (M, 555 turns) | 50–150 ms |
| Knowledge-search embedding per turn (Qwen3 on CPU) | **≈ 1.27 core-seconds** (M) | 0.1–0.3 s on 1–2 cores |
| One knowledge query alone / 10 at once | 0.16 s / 1.26 s p50, 1.93 s worst (M); the app falls back to keyword search above 2 s (D28) | |
| Re-index, per catalogue chunk (Qwen3, CPU) | **4.7 s** using 6–9 cores (M) → 2,500 chunks ≈ **3 h 15 min** | 0.3–1 s → 15–40 min |
| Re-index with BGE-M3 or e5-large instead | 0.75–0.88 s per chunk → ≈ 31–37 min (M) | |
| Quote PDF | not isolated (inside worker CPU) | 1–3 s of a core (E) |
| Voice call, platform worker (GPU speech) | 0.3 core avg for 1 call, 1.4 peak; 10 calls 1.9 avg, **10 cores peak** (M) | 0.1–0.2 core per call |
| Postgres | 0.03 core avg with 10 users (M) | low |

At the Normal level the busiest hour has ≈ 0.12 AI turns per second (D-based), ≈ 0.2 core of steady CPU (M per-turn
cost). CPU matters for **bursts**: re-indexing takes every core it can get, and simultaneous voice calls each build a
heavy process (problem 4). Plan **8 vCPUs** if voice and CPU embeddings share the server.

---

## 4. Disk: what is stored and how fast it grows

**Fixed (day one, M unless marked)**

| What | Size |
|---|---|
| Production app image (Dockerfile, with Chromium + fonts) | **2.68 GB** (draft E: 1–1.5 GB) |
| pgvector 631 MB, Redis 204 MB, TEI CPU 938 MB, Phoenix 1.44 GB, LiveKit server 133 MB | |
| Embedding model Qwen3-0.6B 1.2 GB (BGE-M3 / e5 ONNX copy ≈ 2.2–2.3 GB) | |
| Free-stack speech models: Whisper turbo 1.62 GB, Piper 4 voices 0.25 GB, turn detector 0.71 GB | |
| OS | 5–10 GB (E) |
| **Total fixed, paid speech** | ≈ 7 GB of images and models + OS ≈ **15 GB** (keep 2 app versions: +2.7 GB) |

**Growing (M per unit, then × usage level)**

| Data | Per unit (M) | Light | Normal | Heavy | Draft per unit |
|---|---|---|---|---|---|
| App databases (crm + automation + agents), per buyer turn | **6.5–8 KB** | 0.10 GB | 0.49 GB | 1.6 GB | ~15 KB |
| Phoenix traces, per model call (≈ 2.2 calls per turn) | **4.3 KB** (≈ 9.3 KB per turn) | 0.13 GB | 0.60 GB | 2.0 GB | ~30 KB per turn |
| Quote PDF in object storage | **52 KB** each | | | | ~200 KB |
| **Database + Phoenix per month** | ≈ 17 KB per turn | **≈ 0.24 GB** | **≈ 1.1 GB** | **≈ 3.6 GB** | 0.9 / 4.2 / 13 GB |

Caveat: Sonnet calls never ran (problem 1), so their larger prompts are not in these traces; index bloat and WAL
come on top (≈ +30 %, E). Even so, growth is about a quarter of the draft.

**Disk to buy, Heavy, first year:** database ≈ 3.6 GB × 1.3 × 12 ≈ 56 GB (M + E overhead) + files (PDFs, media,
recordings) ≈ 24–36 GB at the draft's 2–3 GB a month (E; only the 52 KB PDF size was measured) + images and models
≈ 18 GB + OS ≈ **100–120 GB**. Light / Normal need well under 100 GB. Backups belong off-site, not on this disk.

---

## 5. Network (bandwidth)

| Link | Per unit (M) | Light | Normal | Heavy |
|---|---|---|---|---|
| Server → Claude | 19.4 KB up + 1.1 KB down per turn | 0.29 GB | 1.3 GB | 4.3 GB |
| WhatsApp Graph API | ≈ 2 KB per outbound message | < 0.1 GB | < 0.1 GB | 0.13 GB |
| Voice, caller ↔ LiveKit (free stack: speech engines on your server) | 0.15–0.85 MB per call-minute (≈ 20–110 kbit/s; silence compresses) | 0.2–1.3 GB | 1.1–6.4 GB | 3.8–21 GB |
| Postgres traffic inside the server | ≈ 80 KB per turn (internal, not billed) | | | |

With paid speech vendors the audio also travels to Deepgram / ElevenLabs (not measured: no vendor was called).
**Bandwidth is not a cost concern** on any server plan.

---

## 6. Money: what each feature costs outside the server (D)

Unchanged from the draft (vendor prices, `Recommendation.md`): AI replies ≈ $49 / $228 / $735, WhatsApp
≈ $26 / $213 / $756, voice (Deepgram + ElevenLabs + LiveKit Cloud) ≈ $91 / $279 / $930, alert calls
≈ $10 / $46 / $152, email ≈ $17 / $19 / $22 → **≈ $193 / $785 / $2,595 a month**. The measured traffic per turn
(≈ 2.2 model calls, ≈ 20 KB of prompt) is in line with how those prices were built; Sonnet's share could not be
measured (problem 1).

The free stack (section 9) removes the voice vendor line (≈ $91–930) at the cost of a GPU server.

---

## 7. Settings that change consumption (measured where marked)

| Setting | Default | Change to | Effect |
|---|---|---|---|
| `KX_APP_WORKER_CONCURRENCY` | 10 | 3 | **−1.7 GiB RAM (M)**, same reply times, Postgres connections 106 → 57 (M) |
| `KX_DB_POOL_SIZE` / `MAX_OVERFLOW` | 10 / 10 | 5 / 5 | fewer connections (problem 8) |
| `KX_APP_EMBEDDING_TIMEOUT_SECONDS` | 2.0 | ≥ 60 for re-index | re-indexing works on CPU (problem 7); keep queries at 2 s |
| Embedding model | Qwen3-0.6B (Candle) | BGE-M3 (ONNX) | +1.9 GiB RAM, re-index 6× faster, 10-at-once queries 4× faster, same answer quality (M, Round 4); needs a small code change |
| Phoenix retention | forever | 30–90 days | caps trace growth (≈ 4.3 KB per model call, M) |
| LiveKit `num_idle_processes` | min(cores, 4) | expected peak calls | each warm process ≈ 0.3 GiB (M); with problem 4 fixed |
| Docker log size | unlimited | `max-size 50m`, 3 files | stops logs filling the disk |

---

## 8. Problems that affect consumption

See section 0. The ones that change server sizing: worker RAM at default concurrency (9), Postgres connections (8),
tables never pruned (11), re-index timeout (7), and the voice worker's per-call build (4).

---

## 9. Free stack, measured

What `free version.docx` proposes, each run alone with its official image or compose file. Full detail in the
round write-ups.

### Embeddings (Round 4, TEI CPU, same server the app uses)

| | Qwen3-0.6B (default) | BGE-M3 | multilingual-e5-large |
|---|---|---|---|
| RAM idle | 2.82 GiB | 4.74 GiB | 4.69 GiB |
| Re-index per chunk | 4.7 s | 0.75 s | 0.88 s |
| 10 queries at once, p50 / max | 1.26 / 1.93 s | 0.30 / 0.42 s | 0.38 / 0.44 s |
| Hard retrieval test (38 questions, en/ar/hi/zh, look-alike facts) | 35/38 | 36/38 | 35/38 |
| Notes | app default | no query prefix needed | reads only 512 tokens; needs `query:`/`passage:` prefixes |

### Voice (Rounds 2–3)

| Part | Choice | RAM / VRAM | Speed | Verdict |
|---|---|---|---|---|
| Transport | LiveKit server (self-hosted, built from source) | 16–90 MiB, ≤ 1 core | | fine |
| Speech-to-text | faster-whisper large-v3-turbo on **GPU** | ≈ 0.6 GB RAM + ≈ 2.7–3.5 GB VRAM | RTF 0.04; 10 utterances at once in 2.7 s | **needed for live calls** |
| Speech-to-text | same on **CPU** (int8, 16 threads) | 1.8–4.3 GB | RTF 0.94; one caller waits ≈ 13 s; 10 callers: 0 of 40 turns answered | **not usable live** |
| Speech-to-text | sherpa-onnx / whisper.cpp on CPU | 1.5–7.2 GB / 0.6–1.8 GB | RTF 0.86 / 2.0 | not usable live |
| Text-to-speech | Piper (en, ar, hi, zh; no Tamil / Sinhala) | 0.4 GB | RTF 0.03–0.05, first audio 0.1–0.3 s | **fine on CPU** |
| Text-to-speech | Chatterbox multilingual (GPU) | ≈ 4 GB VRAM | RTF 0.84–0.88 (zh 2.0), 5–11 s to first audio | too slow live |
| Whole chain, bench agent, GPU STT | 10 callers | agent 1.4 / 2.1 GiB | reply p50 4.7 s, 9 of 10 calls served | works |
| Whole chain, **platform's voice worker**, GPU STT (with problems 2–3 worked around) | 10 callers | 1.4 / 4.2 GiB, 10 cores peak | reply p50 10 s; all 10 calls served, 29 of 40 turns (first turns lost, problem 4) | works only after fixes |
| Accuracy (Whisper turbo, read against reference text) | | | English / Arabic ≈ perfect; Hindi minor spelling; **Chinese mis-hears product words** | test Chinese with real speech |

> **Corrected 14 Nov:** the working RAM / CPU of Rounds 5–8 (traces, email, calls, firewall) were first reported
> double: `docker stats` streams each frame twice and the bench summed both. The tables below are recomputed from
> the raw data (`scripts/recompute_windows.py`). Idle RAM, Rounds 1–4 and every work result were not affected.

### AI traces (Round 5, 1,000 spans each, storage verified)

| | Phoenix | Langfuse | Opik |
|---|---|---|---|
| RAM idle / peak in the run | **0.48 / 0.51 GB** | 2.49 / 2.19 GB | 2.61 / 3.32 GB |
| Disk per 1,000 spans (net) | **1.9 MB** | 29.8 MB | 98.3 MB |
| Containers | 1 (+ shared Postgres) | 7 | 8 |

### Email, calls, firewall (Rounds 6–8)

| Field | Choice | RAM idle | RAM / CPU working | Measured result |
|---|---|---|---|---|
| Email (receive) | **Haraka** | 91 MB | 127 MB peak, 0.18 core avg / 0.32 peak | 1,000 quote emails (72 KB) in 8.4 s, 0 errors |
| Email (mail server, stores) | Stalwart v1.1 (after its setup wizard) | 85 MB | 228 MB peak, 0.22 core avg / 0.83 peak | 1,000 stored in 11 s (90 per s), counted in the mailbox; store ≈ 142 KB per 72 KB email. Default limit **25 messages/h per sender + recipient** |
| Email (send platform) | Postal 3.3.4 + MariaDB (4 containers) | 455 MB | **652 MB peak**, 0.71 core avg / 1.32 peak | 1,000 authenticated sends at 44 per s, all delivered to the relay within 39 s; database ≈ 97 KB per email |
| Calls (SIP) | **Asterisk** | 49 MB | 52 MB with 10 calls (≈ 0.3 MB per call), 0.17 core peak | 10 of 10 calls with two-way G.711 audio |
| Calls (SIP) | FreeSWITCH 1.11.3 | 41 MB | 50 MB with 10 calls (≈ 0.9 MB per call), 0.05 core peak | 10 of 10 calls with two-way G.711 audio (after setting its external IP; by default it advertises the public IP found by STUN) |
| Log-based blocking | **CrowdSec** | 158 MB after | 159 MB peak, 0.91 core avg / 1.32 peak while parsing | 100,000 log lines in 39 s |
| WAF / reverse proxy | BunkerWeb (ModSecurity + OWASP CRS) | 290 MB | 425 MB avg / 472 MB peak while proxying | +4–6 ms per API request; blocked 3 of 3 test attacks but **also all WhatsApp webhooks** (problem 12) |
| WAF | **SafeLine 9.2.7** (7 containers) | 1,618 MiB | 1,639 MiB peak (≈ 1.6 GiB), 0.03 core avg / 0.12 peak while protecting | no added delay (±1 ms); blocked 3 of 3 test attacks; **0 false blocks** (86/86 webhooks, all chat messages) |

### Server sizes

| Setup | RAM in use (M + E) | Server to buy | GPU |
|---|---|---|---|
| **Paid speech / AI APIs**, app self-hosted, tuned (worker 3, Qwen3 on CPU, Phoenix) | ≈ 4.5 GiB app + 1.4–4.2 GiB voice worker + ≈ 1 GB CMS/web + OS ≈ **8–11 GB** | **16 GB RAM, 8 vCPU, 120 GB SSD** (Heavy; less for Light/Normal) | none |
| **Everything free / self-hosted** (+ LiveKit, GPU speech, Piper, Haraka, Asterisk, CrowdSec, SafeLine) | the above + speech server ≈ 0.7 GB + Haraka 0.13 + Asterisk 0.05 + CrowdSec 0.16 + SafeLine 1.6 GB ≈ **10.5–13.5 GB** (BunkerWeb instead of SafeLine: −1.2 GB but needs the webhook rule exclusion; Postal instead of Haraka: +0.5 GB) | **24 GB RAM (32 for headroom), 8–12 vCPU, 150–200 GB SSD** | **8 GB NVIDIA GPU** (≈ 3–4 GB VRAM used) |

A CPU-only server cannot run the free voice chain for live calls (Round 2). Everything else in the free stack runs
on CPU.

GPU in the UAE region (AWS's official price list, me-central-1, 14 Nov 2026,
`results/aws_ec2_me-central-1_prices.json`): the smallest GPU server is **g6.xlarge** (1 × NVIDIA L4 24 GB,
4 vCPU, 16 GiB) at **US$0.98824 an hour ≈ US$721 a month** always on; g5.xlarge (A10G) US$901. The bench GPU was an
RTX 5060; speed on an L4 was not measured. What this means against the paid voice vendors: `RANK.md`.
