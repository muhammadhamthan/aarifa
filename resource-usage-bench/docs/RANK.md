# RANK: free platforms against the paid services, per service (measured 14 Nov 2026)

For every service the platform needs, this page ranks the free self-hosted platforms that were **actually run
and measured** (one or two per service). For each one it says what it is, why the project needs it, what was measured,
the paid service it would replace, and what replacing it costs or saves.

**Rules used**
- **Only measured results or official prices.** Every number is from a bench run (`results/`), AWS's official price
  list, or the repo's own price documents. When something was not measured, it says "not measured".
- **Paid prices**: `docs/Arifaa Platform — Platforms, Pricing & Fallbacks/` (`Recommendation.md`,
  `Options by field.md`; official vendor pages checked 7–11 Nov 2026), US$ a month at the three usage levels:

  | Per month | Light | Normal | Heavy |
  |---|---|---|---|
  | AI turns | 14,000 | 65,000 | 210,000 |
  | Website voice minutes | 1,500 | 7,500 | 25,000 |
  | Emails out / in | 2,000 / 500 | 10,000 / 2,500 | 40,000 / 8,000 |
  | Staff alert / call-back minutes | 36 | 180 | 600 |

- **Free platforms on AWS (UAE)**, computed by `scripts/rank_costs.py` (`results/rank_costs.json`):
  - **Containers:** the smallest valid Fargate size with memory at least the measured peak RAM and vCPU at least the
    measured average CPU under the test load. Priced at AWS's UAE x86 Fargate price ($0.0526 per vCPU-hour +
    $0.0058 per GB-hour), because the measured images were amd64. Source:
    `results/aws_fargate_me-central-1_prices.json`.
  - **GPU work:** the smallest GPU server in the UAE region, from AWS's price list of 14 Nov 2026
    (`results/aws_ec2_me-central-1_prices.json`).
  - **Hours:** 730 a month, always on.
- **Test machine**: i5-13400F (16 threads), 15.8 GB RAM, RTX 5060 8 GB. Load: 10 fake users / 10 callers /
  1,000 emails / 10 SIP calls, with the paid vendors replaced by local fakes (nothing was sent outside the PC).
- Saving = paid line − free cost. A **negative saving means the free platform costs more** on AWS.

---

## Summary

| Service | Rank 1 (free) | Rank 2 (free) | Paid service now | Paid US$/month L / N / H | Saving with rank 1, US$/month L / N / H | Verdict from the numbers |
|---|---|---|---|---|---|---|
| Speech-to-text | faster-whisper large-v3-turbo (GPU) | whisper.cpp (CPU, not live) | Deepgram Nova-3 | 14 / 69 / 230 | needs a GPU server, see "whole voice chain" | GPU only; CPU too slow for live calls |
| Text-to-speech | Piper (CPU) | Chatterbox (GPU, not live) | ElevenLabs Flash | 27 / 135 / 450 | Light +3.57 (1 voice at once); Heavy +75 (10 at once) | small saving; **no Tamil / Sinhala voice** |
| Real-time voice transport | LiveKit server (self-hosted) | Pipecat: **not tested** | LiveKit Cloud Ship | 50 / 75 / 250 | inside the voice chain | server itself is tiny (≤ 90 MiB) |
| **Whole voice chain** (STT + TTS + transport) | faster-whisper GPU + Piper + LiveKit on one GPU server | — | Deepgram + ElevenLabs + LiveKit Cloud | 91 / 279 / 930 | **−630 / −442 / +209** (g6.xlarge $721) | **costs more at Light and Normal**; saves only at Heavy |
| Staff alert calls | Asterisk 23.5 | FreeSWITCH 1.11.3 | Telnyx (Twilio backup) | 10 / 46 / 152 | **−11.65 / −11.36 / −10.52** | no saving: phone minutes stay paid |
| Email: sending | Postal 3.3.4 | — (Haraka only tested receiving) | Amazon SES + Postmark | 17 / 19 / 22 | **−41.59 / −39.59 / −36.59** (with Stalwart for replies) | costs more; deliverability not testable locally |
| Email: receiving / mailbox | Stalwart v1.1 | Haraka 3 | Postmark (replies) | in the line above | in the line above | Stalwart stores and serves mail; Haraka only receives |
| Knowledge search (embeddings) | BAAI/bge-m3 | Qwen3-Embedding-0.6B (today's default) | (already self-hosted); paid alternative Gemini Embedding 2 | plan 44 / 44 / 44; Gemini 0.28 / 1.30 / 4.20 | BGE-M3: **−13.82** a month (needs an 8 GB container) | same answers; BGE-M3 6× faster, Qwen3 1.9 GiB smaller |
| AI tracing | Arize Phoenix 21.1 (today's plan) | Langfuse v4 (self-hosted) | Langfuse Cloud Core | 29 / 47 / 105 | **+10 / +28 / +86** (Phoenix at the plan's $19) | Phoenix: lightest by far; keep it |
| Web firewall (WAF) | SafeLine 9.2.7 | BunkerWeb 1.6.6 (OWASP CRS) | AWS WAF + Turnstile | 10 / 11 / 14 | **−8.07 / −7.07 / −4.07** | no saving; SafeLine is the one that works out of the box |
| Log-based blocking | CrowdSec 1.7.3 | — | (no line in the plan) | — | — | adds a layer; costs a container |
| Bot challenge | ALTCHA: **not tested** | — | Cloudflare Turnstile (inside the WAF line) | — | — | not measured |

**If every tested paid line above were replaced** (voice chain, calls, email, WAF): **−691.73 / −500.44 / +157.40
US$ a month** (L / N / H). At Arifa's planned volumes the free stack costs more on AWS at Light and Normal and saves
about US$157 a month at Heavy. The biggest single item is the GPU server the free voice chain needs.

---

## 1. Speech-to-text (STT)

**What it is.** It turns the caller's speech into text so the AI can read it.
**Why the project needs it.** The website's "Enter the factory" voice conversation (SRS §6, step 1 "Landing") in the visitor's
language: English, Arabic, Hindi, Tamil, Chinese, Sinhala (SRS §6, step 3 "Language").

| | **Rank 1: faster-whisper 1.2.1** (large-v3-turbo, CTranslate2, GPU float16) | **Rank 2: whisper.cpp** (q5_0 model, CPU) | Also tested |
|---|---|---|---|
| Licence | MIT (repo) | MIT (repo) | sherpa-onnx (Apache-2.0); faster-whisper int8 on CPU is the same package |
| Model on disk | 1.62 GB | 0.57 GB | sherpa 1.04 GB |
| RAM / VRAM | +0.16 GB RAM, ≈ 3.5 GB VRAM | 0.61 GB RAM after load, 0.78 GB peak | sherpa 7.2 GB RAM peak (10 at once); fw-int8 CPU 4.3 GB |
| Speed, one 4–6 s clip | 0.24 s (real-time factor 0.041) | 11.7 s (RTF 1.98, slower than real time) | sherpa RTF 0.86; fw-int8 CPU RTF 0.94 |
| 10 clips at once (59 s of audio) | all done in 2.7 s | 118 s (one at a time) | sherpa 42 s; fw-int8 CPU 39.5 s |
| CPU while doing it | 6–7 cores for audio preparation (10 clips at once) | 8 threads | 16 threads |
| In the live voice test | 10 callers: reply p50 4.7 s, 36 of 40 turns; platform's own worker: 10 of 10 calls, p50 10 s | not usable live | CPU STT in the live chain: one caller already waited 12.8 s; 10 callers got 0 of 40 replies |

**How the test ran.** Ten Piper-made clips with known text (3 en, 3 ar, 2 hi, 2 zh). Each engine ran alone (Round 3).
The whole chain then ran with 1, 3 and 10 simultaneous callers through LiveKit (Round 2), including the platform's own
voice code.

**Accuracy (transcripts read against the reference text).** English and Arabic essentially perfect. Hindi: meaning
intact, small spelling slips. **Chinese: real errors on product words** (铝箔→铝博, 餐盒→餐和, 起订量→企定量), partly
because the test voice was synthetic. Tamil and Sinhala: **not tested**.

**Why rank 1.** It is the only engine fast enough for a live conversation, and only on a GPU. Rank 2 is the
lightest CPU option, for work that is not live.

**Paid version.** Deepgram Nova-3 Multilingual, $0.0092 a minute: **14 / 69 / 230 US$ a month**. Its card lists
ar, hi, ta, zh, and no Sinhala.

**Replacing it.**
- **Cost:** a GPU server. The smallest in the UAE region is **g6.xlarge** (1 × NVIDIA L4 24 GB, 4 vCPU, 16 GiB):
  **$0.98824 an hour, $721.42 a month**. Replacing STT alone would cost more than Deepgram at every level.
- **Why only together:** it pays off only as part of the whole voice chain (below).
- **Not measured:** speed on an L4. The bench GPU was an RTX 5060.

---

## 2. Text-to-speech (TTS)

**What it is.** It turns the AI's written reply into a voice.
**Why the project needs it.** The same website voice conversation (SRS §6, steps 1 and 3), including the recording notice
that must play first.

| | **Rank 1: Piper 1.8.0** (CPU) | **Rank 2: Chatterbox 0.1.7 multilingual** (GPU) |
|---|---|---|
| Licence | GPL-3.0 (`piper1-gpl`) | MIT |
| Model on disk | 63 MB per voice (4 voices: en, ar, hi, zh = 0.25 GB) | 13.9 GB downloaded |
| RAM / VRAM | 0.40 GB with 4 voices loaded; 2.07 GB peak with 10 jobs at once | ≈ 4.0 GB VRAM (torch peak 3.5 GB); 5.2 GB RAM while loading |
| Speed (real-time factor) | 0.027–0.051 (20–37× faster than real time) | 0.84–0.88; Chinese 1.98 |
| First audio | 0.11–0.29 s | 4.7–11.3 s (not streamed) |
| 10 at once | 157 s of speech in 4.3 s; first audio p95 2.1 s | not tested (one at a time was already about real time) |
| CPU | **0.43 core for each voice speaking at the same moment** (67.2 CPU-s for 157.2 s of speech) | GPU |
| Languages | en, ar, hi, zh tested; **no Tamil or Sinhala voice exists** | multilingual; ta / si not tested |

**Why rank 1.** Piper is fast on a plain CPU and was never the bottleneck in any voice run. You chose it
(`free version.docx`: "RANK 1 PIPER BCOZ IT HAVE ACCURATE ARABIC VOICE"), and the Arabic test clips confirm it.
Chatterbox sounds more natural, but at about real-time speed with a 5–11 s wait for the first audio, it is too slow
for a live call on this GPU.

**Paid version.** ElevenLabs Flash, $0.04 per 1,000 characters: **27 / 135 / 450 US$ a month**.

**Replacing it (Piper on Fargate).**
- **One voice at a time** (0.5 vCPU, 1 GB): **$23.43 a month**.
- **Ten at a time** (8 vCPU, 16 GB): **$374.93 a month**.

| Level | Saving |
|---|---|
| Light (peak 1 voice at once) | +$3.57 a month |
| Heavy (peak 10 voices at once) | +$75.07 a month |
| Normal | between the two; the RAM between 1 and 10 voices was not measured |

**Gap:** SRS §6 step 3 needs Tamil and Sinhala, and Piper has neither. A second TTS would be needed for those (not tested).

---

## 3. Real-time voice transport

**What it is.** The media server that carries live audio between the visitor's browser and the AI voice agent.
**Why the project needs it.** Every website voice call goes through it (SRS §6 step 1, §19 "Website voice and chat").

| | **Rank 1: LiveKit server v1.13.11** (built from `repos/livekit-server`) | Rank 2: Pipecat |
|---|---|---|
| Licence | Apache-2.0 | BSD-2-Clause |
| Image | 133 MB | — |
| RAM / CPU with 10 callers | 16–90 MiB, ≤ 1 core | **not tested** |
| Bandwidth at the server (both legs) | 0.3–1.7 MB per call-minute; the caller's internet leg ≈ 0.15–0.85 MB | — |
| Result | carried all calls in every voice run; the platform's own agent code (LiveKit Agents 1.8.10) ran on it | — |

**Why rank 1.** It's the transport the platform's voice code is built on (`kx.agents.voice` uses LiveKit Agents), and
it was measured. Pipecat would mean rewriting the agent (`Options by field.md` §4) and was not run.

**Paid version.** LiveKit Cloud Ship: $50 a month including 5,000 agent minutes, then $0.01 a minute, up to 20
sessions at once: **50 / 75 / 250 US$ a month**.

**Replacing it.**
- **Measured:** the server needs very little, so in the free chain it shares the GPU server (section 4).
- **Repo estimate:** a separate self-hosted LiveKit on EC2 + Fargate is 134 / 267 / 534 a month (`Options by field.md`
  §4). That is a documentation estimate, not a measurement, and it is more than LiveKit Cloud at every level.

**Problems found in the platform's voice code** (they apply to paid and free alike, `RESOURCE_USAGE.md` §0):
1. `kx voice` cannot start a job (pickling).
2. The session has no LLM, so it never replies.
3. Every call builds the whole app at its start, and under 10 calls the first turns are lost.

The platform's voice worker itself used 1.41 GiB idle and 4.21 GiB / 10 cores at 10 calls.

---

## 4. The whole voice chain (what would actually be replaced)

The free voice stack only works as a set: GPU STT, Piper TTS and LiveKit on one GPU server. The same LiveKit
Agents worker runs in both setups.

| | Paid (Deepgram + ElevenLabs + LiveKit Cloud) | Free on **g6.xlarge** (L4 24 GB, 4 vCPU, 16 GiB) | Free on **g6.2xlarge** (L4 24 GB, 8 vCPU, 32 GiB) |
|---|---|---|---|
| US$ a month, Light / Normal / Heavy | 91 / 279 / 930 | 721.42 at every level | 876.31 at every level |
| **Saving** | — | **−630.42 / −442.42 / +208.58** | **−785.31 / −597.31 / +53.69** |

**Which size.** Ten simultaneous transcriptions used 6–7 CPU cores on the bench, so the 8-vCPU g6.2xlarge is the
size that matches the 10-caller test. The 4-vCPU g6.xlarge suits fewer simultaneous callers (not measured).

**Verdict.** At Light and Normal the paid voice vendors are far cheaper than any always-on UAE GPU server. The free
chain saves money only at Heavy, by $54–209 a month, and its speed on an L4 card was not measured.

---

## 5. Staff alert calls and buyer call-backs (SIP)

**What it is.** A phone switch that places and answers phone calls.
**Why the project needs it.**
- scripted alert calls to staff for approvals, change requests and below-minimum offers (SRS §11.3, §16, §19);
- click-to-call from the CRM (SRS §14).

| | **Rank 1: Asterisk 23.5.0** | **Rank 2: FreeSWITCH 1.11.3** |
|---|---|---|
| Licence | GPL-2.0 | MPL-1.1 |
| Image | 485 MB | 352 MB |
| RAM idle → with 10 calls | 49 → 52 MB (≈ 0.3 MB per call) | 41 → 50 MB (≈ 0.9 MB per call) |
| CPU with 10 calls | 0.05 core avg, 0.17 peak | 0.03 core avg, 0.05 peak |
| Result | **10 of 10 calls**, two-way G.711 audio, 0 retransmissions | **10 of 10 calls**, two-way audio verified (76 kB in, 70 kB echoed back) |
| Set-up found | worked with a small `pjsip.conf` / `extensions.conf` | by default it advertises the PC's **public IP** (found by STUN) in its call setup, so audio leaves the network; needs `external_rtp_ip` / `external_sip_ip` set |

**How the test ran.** SIPp placed 10 simultaneous calls with G.711 audio both ways (answer + echo), all inside Docker,
with no phone lines. SIPp's built-in scenario plays 8 s of audio per call.

**Why rank 1.** Both completed every call with tiny RAM. Asterisk worked with its stock image and a small config;
FreeSWITCH needed its NAT settings fixed first.

**Paid version.** Telnyx: $0.25 a minute to UAE mobiles + $0.002 a minute Voice API: **10 / 46 / 152 US$ a month**
(Twilio backup $0.2995 a minute).

**Replacing it.**
- **What changes:** a self-hosted switch replaces only Telnyx's $0.002 a minute Voice API part (0.07 / 0.36 /
  1.20 US$ a month). Reaching real phones still needs a paid SIP trunk for the minutes (`free version.docx`:
  "Software free; phone-line minutes paid").
- **Cost:** the Asterisk container is $11.72 a month.
- **Saving:** **−11.65 / −11.36 / −10.52 US$ a month**. Not worth it at these volumes.

---

## 6. Email: sending

**What it is.** It sends the quote emails with their PDF attached, from one company address.
**Why the project needs it.** Quote sending and buyer replies captured into the deal (SRS §11.4, §14, §19).

| | **Rank 1: Postal 3.3.4** (+ MariaDB, 4 containers) |
|---|---|
| Licence | MIT |
| Images | Postal 1.9 GB + MariaDB 470 MB |
| RAM idle → peak | 455 MB → 652 MB |
| CPU | 0.71 core avg, 1.32 peak |
| Result | **1,000 authenticated quote emails (72 KB each) accepted at 44 a second; all 1,000 signed (DKIM) and delivered to the relay within 39 s**; 210 ms per message p50 |
| Disk | database grew **97 MB per 1,000 emails** (it keeps the raw message) |

**How the test ran.**
1. Postal was set up as an operator would: admin user, organisation, mail server, verified domain and SMTP
   credential.
2. 1,000 quote emails were sent over 10 connections with SMTP login.
3. Postal delivered each to a local Haraka sink, so nothing left the PC.
4. Every message was counted as "Sent" in Postal's own database.

**Why rank 1.** It is the only tested free platform built for application sending (credentials, DKIM signing,
delivery tracking). Haraka was tested only as a receiver.

**Paid version.** Amazon SES Essentials ($0.16 per 1,000) to send + Postmark Pro ($16.50 a month for 10,000) for
replies: **17 / 19 / 22 US$ a month**.

**Replacing it.**
- **Cost:** Postal 1 vCPU / 2 GB is $46.87, plus Stalwart for replies at $11.72: **$58.59 a month**.
- **Saving:** **−41.59 / −39.59 / −36.59 US$ a month**.
- **Not measured:** whether self-hosted mail reaches buyers' inboxes (sender reputation). It can't be measured on
  a local network.

---

## 7. Email: receiving and mailbox

| | **Rank 1: Stalwart v1.1** (full mail server) | **Rank 2: Haraka 3** (SMTP receiver) |
|---|---|---|
| Licence | AGPL-3.0 (licence obligations apply); its `LICENSES/` folder also holds Stalwart's own enterprise licence (SEL) for paid features | MIT |
| Image | 469 MB | node:22-slim 329 MB + Haraka from npm |
| RAM idle → peak | 85 → 228 MB | 91 → 127 MB |
| CPU | 0.22 core avg, 0.83 peak | 0.18 core avg, 0.32 peak |
| Result | **1,000 emails stored in 11 s (90 a second), counted in the mailbox over JMAP** | **1,000 accepted in 8.4 s (119 a second)**, 0 errors (accepted and discarded) |
| Disk | store grew 142 KB per 72 KB email (measured right after, before compaction; includes its search index) | none (not stored) |
| Set-up found | starts in "bootstrap mode" with no SMTP until its web set-up wizard is finished. Default limits: 25 messages an hour per sender-domain + recipient, 5 connections a second per IP, 10 messages per session | — |

**Why rank 1.** Stalwart actually stores mail and serves it (JMAP/IMAP), which reply capture needs. Haraka is
lighter and faster, but in the test it only received; storing or forwarding mail to the app needs plugins (not tested).

**Paid version.** Postmark Pro inbound, included in the email line above.

**Replacing it.** Included in section 6's total ($11.72 a month for Stalwart).

---

## 8. Knowledge search (embeddings)

**What it is.** A model that turns product and policy text into vectors, so the AI finds the right fact in the
buyer's language.
**Why the project needs it.**
- every AI answer searches the product knowledge;
- publishing in the CMS re-indexes it (SRS §7: "The AI's knowledge is re-indexed automatically");
- languages per SRS §6 step 3.

All three ran on the same server the app uses (Hugging Face Text Embeddings Inference 1.8.3, CPU, 16 threads).

| | **Rank 1: BAAI/bge-m3** | **Rank 2: Qwen/Qwen3-Embedding-0.6B** (today's default, D28) | Rank 3: intfloat/multilingual-e5-large |
|---|---|---|---|
| Licence | MIT | Apache-2.0 | MIT |
| RAM idle / peak while indexing | 4.74 / 4.83 GiB | **2.82 / 3.10 GiB** | 4.69 / 4.76 GiB |
| Disk the server needs | 2.3 GB | 1.2 GB | 2.2 GB |
| Indexing speed | **0.75 s per chunk** | 4.7 s per chunk | 0.88 s per chunk |
| 2,500-chunk catalogue, full re-index | **≈ 31 min** | ≈ 3 h 15 min | ≈ 37 min |
| 10 questions at the same moment, p50 / max | **0.30 / 0.42 s** | 1.26 / 1.93 s (app's limit is 2 s) | 0.38 / 0.44 s |
| Hard test, 38 questions with look-alike facts | **36/38** | 35/38 | 35/38 |
| Longest input | 8,192 tokens | 32k tokens | **512 tokens** |
| Works with the current code | needs a small change (no query instruction) | yes | needs query and passage prefixes |

**Why rank 1.**
- **Same answers:** quality is a tie (one question apart).
- **Speed:** BGE-M3 re-indexes 6× faster and answers 10 simultaneous questions in under 0.5 s. Qwen3 came within
  0.07 s of the app's 2-second cut-off, beyond which the app silently falls back to keyword search.
- **Trade-off:** Qwen3 uses 1.9 GiB less RAM and fits the plan's 4 GB container.
- **Not measured:** speed on the plan's container (1 vCPU); the bench had 16 threads.

**Paid version.**
- The plan already self-hosts Qwen3: 1 vCPU + 4 GB on Fargate ARM, **44 a month**.
- The paid API alternative is Gemini Embedding 2 at $0.20 per million tokens: 0.28 / 1.30 / 4.20 a month. It is
  cheaper, but buyer questions leave the UAE (`Options by field.md` §13).

**Replacing it.**
- **Cost:** BGE-M3 needs 1 vCPU + 8 GB (ARM): **$57.82 a month**, so **−13.82 a month** against the Qwen3 container.
- **Also needed:** the catalogue must be re-indexed.

---

## 9. AI tracing

**What it is.** It records every AI call (prompt, reply, tools, time, cost) so staff can see why the AI said
something.
**Why the project needs it.** It is the audit trail of AI answers. In the plan, prices and chats stay in the UAE
(`Options by field.md` §14).

| | **Rank 1: Arize Phoenix 21.1.0** (today's plan) | **Rank 2: Langfuse v4** (self-hosted) | Rank 3: Opik |
|---|---|---|---|
| Licence | Elastic License 2.0 (free for own use; hosted resale restricted) | MIT outside the `ee/` folders (copyright ClickHouse, Inc., per its LICENSE file) | Apache-2.0 |
| Containers | 1 (+ the platform's Postgres) | 7 (web, worker, AI gateway, ClickHouse, MinIO, Redis, Postgres) | 8 (incl. ClickHouse, ZooKeeper, MySQL) |
| Main images | 1.44 GB | web 1.06 GB + worker 0.82 GB + ClickHouse 1.19 GB + others | backend 1.3 GB + Python backend 1.66 GB + others |
| RAM idle / peak in the run | **0.48 / 0.51 GB** | 2.49 / 2.19 GB (the idle figure is one reading, taken while its web container was larger than during the run) | 2.61 / 3.32 GB |
| CPU avg | **0.05 core** | 0.22 core | 1.43 cores |
| Disk per 1,000 spans | **1.9 MB** (real platform calls: 4.3 KB each) | 29.8 MB | 98.3 MB |
| Writes with no traffic (≈ 145 s) | 0 | 19 MB | 30 MB |
| Result | 1,000 of 1,000 spans stored | 1,000 of 1,000 | 1,000 of 1,000 |

**Why rank 1.** It uses a fifth of the RAM and 1/15 to 1/50 of the disk, and keeps traces in the database the
platform already backs up.

**Paid version.** Langfuse Cloud Core $29 a month: **29 / 47 / 105**. Pro: 199 / 217 / 275.

**Saving with Phoenix** (the plan's $19 a month): **+10 / +28 / +86 US$ a month** against Langfuse Cloud Core.
Computed on Fargate x86, Phoenix would cost $11.72.

---

## 10. Web firewall (WAF) in front of the platform API

**What it is.** A reverse proxy that inspects every web request and blocks attacks.
**Why the project needs it.** Security of the CRM, website chat and webhooks (SRS §22).

| | **Rank 1: SafeLine 9.2.7** | **Rank 2: BunkerWeb 1.6.6** (ModSecurity + OWASP CRS 4.20.0, 833 rules) |
|---|---|---|
| Licence | GPL-3.0 | AGPL-3.0 |
| Images | 7 images, ≈ 2.3 GB | 1.68 GB |
| RAM idle → while protecting | 1,618 → 1,639 MiB peak (≈ 1.6 GiB) | 290 MB → 472 MB peak |
| CPU while protecting | 0.03 core avg, 0.12 peak | 0.05 core avg, 0.43 peak |
| Delay added per API request | **none measurable (±1 ms)** | +4–6 ms |
| 3 textbook attacks (SQL injection, script injection, path traversal) | **3 of 3 blocked** | **3 of 3 blocked** |
| Real platform traffic (10 users, 5 min) | **0 false blocks**: 86/86 WhatsApp webhooks, all chat messages | **blocked 89 of 89 WhatsApp webhooks** and 2 of 93 chat messages |

**How the test ran.**
1. The same 10-user load ran for 5 minutes straight to the API, then the same 5 minutes through the WAF.
2. Then the same six requests (3 attacks, 3 normal) went straight and through each WAF (`scripts/waf_probe.py`).
3. SafeLine's site was set up in its own web UI.

**Why rank 1.** Both stop the attacks, but only SafeLine lets the platform's real traffic through with default
settings. BunkerWeb's block comes from CRS rule 930120, which matches `.profile` in the field
`contacts[0].profile.name` that every WhatsApp webhook from Meta contains. With BunkerWeb in front and stock rules,
WhatsApp would silently stop working until that rule is excluded for `/webhooks/whatsapp`. BunkerWeb uses 1.2 GB
less RAM.

**Paid version.** AWS WAF basic ($5 + $1 per rule + $0.60 per million requests) + Cloudflare Turnstile:
**10 / 11 / 14 US$ a month**.

**Replacing it.**
- **SafeLine** (0.25 vCPU / 2 GB) costs $18.07: **−8.07 / −7.07 / −4.07 US$ a month**.
- **BunkerWeb** costs $11.72: −1.72 / −0.72 / +2.28.
- **Not tested:** SafeLine's proxy uses host networking, and it was not run on Fargate.

---

## 11. Log-based blocking

**What it is.** It reads web-server logs, spots attack patterns and bans the source IP.

| | **CrowdSec 1.7.3** |
|---|---|
| Licence | MIT |
| Image | 476 MB |
| RAM | 100 MB avg, 159 MB peak while parsing |
| CPU | 0.91 core avg, 1.32 peak during a 100,000-line burst |
| Result | 100,000 nginx-format lines in 39 s (2,564 a second) |

**Caveats.**
- The test log used private 10.x addresses, which CrowdSec trusts by default (96.9k lines whitelisted), so detection
  work was small.
- No steady low-rate run was measured.
- The plan has no separate paid line for this. Its nearest equivalents are AWS WAF rate rules and the app's own rate
  limits. On the sizing rule it would be a 1 vCPU / 2 GB container ($46.87 a month), but that sizes it on the burst.

---

## 12. Not tested (no ranking possible)

| Platform from `free version.docx` | Field | Why not |
|---|---|---|
| Pipecat | real-time voice framework | the platform's voice code is built on LiveKit Agents; using Pipecat means rewriting the agent |
| ALTCHA | bot challenge | not run; its cost would be inside the API container |
| OpenAI Whisper (reference implementation) | STT | the same model was tested through faster-whisper, whisper.cpp and sherpa-onnx |
| LiveKit turn-detection model | voice turn-taking | used inside every voice run, not measured on its own |

---

## Where the numbers come from

| Topic | Write-up | Raw data |
|---|---|---|
| Speech, voice chain | `R2_R3_RESULTS.md` | `results/r2_*`, `r3_*` |
| Embeddings | `R4_RESULTS.md` | `results/r4_*` |
| Tracing | `R5_RESULTS.md` | `results/r5_*` |
| Email, calls, firewall | `R6_R8_RESULTS.md` | `results/r6_*`, `r7_*`, `r8_*`, `r8_waf_probe_*.json` |
| Costs | this page | `results/rank_costs.json` (`scripts/rank_costs.py`) |
| AWS prices | — | `results/aws_ec2_me-central-1_prices.json`, `aws_fargate_me-central-1_prices.json` |
| Paid prices | `docs/Arifaa Platform — Platforms, Pricing & Fallbacks/` | vendor pages listed there |
| Source code of each platform | `repos/SOURCES.md` | `repos/` (shallow clones at the measured version where a tag exists) |

**Correction (14 Nov).** Rounds 5–8 first reported RAM and CPU totals that were double the real values: `docker stats`
streams each frame twice. All figures on this page use the corrected values (`scripts/recompute_windows.py`).
