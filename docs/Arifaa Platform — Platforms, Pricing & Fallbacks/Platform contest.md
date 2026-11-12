# Platform contest

Round 4 (11 Nov) settled the embedding model and reviewed Hamthan's three platform changes. Qwen3-Embedding-0.6B won with 28 of 30; Celery, in-house login and Better Stack were accepted with the conditions below. Scores are Kairox's engineering judgement from the official facts in the *Official sources* tab; quality claims are tested in the shadow week.

## Round 4 (11 Nov): the embedding fight

Five candidates, six criteria, 0–5 each. **Quality** uses the MMTEB multilingual mean on the Qwen3 card (leaderboard of 24 May 2025). **Fit** = works with our existing `Vector(1024)` column without a migration.

| Model | Quality (MMTEB) | Data stays in UAE | Fit with 1,024 dims | Max input | Running cost | Licence | Total /30 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **[Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)** | 4 (64.33) | 5 | 5 (32–1,024 dims) | 5 (32k) | 4 ($44) | 5 (Apache 2.0) | **28** |
| [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | 3 (59.56) | 5 | 5 | 4 (8,192) | 4 ($44) | 5 (MIT) | 26 |
| [intfloat/multilingual-e5-large](https://huggingface.co/intfloat/multilingual-e5-large) | 3 (no mean on card; its newer -instruct sibling scores 63.22) | 5 | 5 | 2 (512) | 4 ($44) | 5 (MIT) | 24 |
| [Gemini Embedding 2](https://ai.google.dev/gemini-api/docs/embeddings) (Google API) | 5 (68.37 for "Gemini Embedding") | 1 (queries go to Google) | 4 (set to 1,024) | 4 (8,192) | 5 ($0.28–4.20) | 3 (paid API, new processor) | 22 |
| [all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | 1 (English only) | 5 | 1 (384 dims: migration) | 1 (256) | 5 ($9) | 5 (Apache 2.0) | 18 — out |

**Verdict: Qwen3-Embedding-0.6B**, runner-up bge-m3.

- **Why Qwen3:** highest multilingual score of the models we can run in the UAE; the longest input, so a whole product datasheet fits one vector; the same $44 container; no schema change.
- **Why not all-MiniLM-L6-v2:** its card says "language: en". Arabic, Hindi, Tamil and Chinese buyers would get wrong answers. It is only cheap because it is small.
- **Why not Gemini, despite the best score:** every buyer question would go to Google, adding a processor outside the UAE; keep it as an option only if Arifa approves.
- **Condition:** none of the cards names Arabic, Hindi, Tamil or Sinhala explicitly. Before go-live, run 50 real buyer questions per language (en / ar / hi / ta / zh / si) against the catalogue; if Qwen3 misses the pass mark, switch to bge-m3 and re-index.

## Round 4 (11 Nov): Hamthan's platform changes

| Change | Verdict | Conditions | For | Against |
| --- | --- | --- | --- | --- |
| Celery + Celery Beat instead of Procrastinate | **Accepted** | One Beat only; long waits in a PostgreSQL schedule table; idempotent tasks with `acks_late`; Redis `noeviction`; async code bridged with one event loop per worker | Most widely used Python job system; team knows it; Flower dashboard; Redis already in the stack | Procrastinate is async-native and keeps jobs in PostgreSQL with the outbox; 11 tasks and 4 periodic jobs must be ported and re-tested |
| In-house login instead of Cognito | **Accepted** | TOTP authenticator app as the main second step; email code only as fallback and recovery; Argon2 hashing, rate limits, lockout, audit log, RS256 tokens | No third-party login vendor; full control of screens and roles | Saves no money (Cognito Essentials is free for 10,000 users); Kairox owns the security of login code |
| Better Stack for all monitoring | **Accepted for uptime, errors, logs, on-call** | Mask PII before logs leave AWS; Arifa approves EU data location; keep Phoenix for AI traces, Database Insights for SQL, AWS Budgets for bills | One screen and one on-call rota; phone/SMS alerts included; Sentry SDK works unchanged | No UAE region; free plan is personal-use only; does not ingest AWS bills |

## Round 3 (7 Nov evening): official prices only

The Claude mix won: Sonnet 5.5 on money turns, Haiku 5.5 on the rest, with Phoenix tracing in the UAE (24 of 30 across CTO, CFO and client judges).

| Stack (US$ a month, Light / Normal / Heavy, as scored on 7 Nov) | CTO | CFO | Client | Total |
| --- | --- | --- | --- | --- |
| **Claude mix: 787 / 1,684 / 3,848** | 9 | 7 | 8 | **24** |
| All-Haiku brain: 740 / 1,467 / 3,146 | 7 | 8 | 7 | 22 |
| Round-2 mix: 816 / 1,738 / 3,972 | 7 | 5 | 6 | 18 |
| Budget stack: 672 / 1,180 / 2,993 | 5 | 6 | 3 | 14 |
| Gemini mix: 796 / 1,649 / 3,683 | 4 | 6 | 3 | 13 |

| Dispute | Verdict | Vote |
| --- | --- | --- |
| AI brain | Claude mix; All-Haiku is the shadow-week test arm | 3–0 |
| Groq | Removed (8,000 tokens a minute is below one turn) | 3–0 |
| Database | db.t4g.medium Multi-AZ at Light, db.m6g.large from Normal | 3–0 |
| WhatsApp | Meta direct with send caps | 3–0 |
| Alert calls | Telnyx after live du and Etisalat tests; Twilio backup | 2–1 |
| AI tracing | Arize Phoenix self-hosted in the UAE | 3–0 |
| Embeddings | Superseded by Round 4 | — |
| Error alerts | Superseded by Round 4 (Better Stack) | — |

## Round 1 (7 Nov morning)

Fourteen agents (5 researchers, 4 advocates, 2 critics, 3 judges) argued every field. Key findings that still stand:

- **Telnyx** needed a published UAE rate (now read: $0.25/min to mobiles) and a live test call; a Telnyx UAE number cannot place calls.
- **Self-hosted LiveKit** needs EC2, UDP ports and TURN (about 60 build hours), so it moved to phase 2.
- **No option covers Sinhala in live voice;** Sinhala stays text-only unless a vendor adds it.
- **Meta offers no uptime SLA,** so the real WhatsApp backup is email.

The full research notes and score sheets are kept with Kairox's working files.
