# Arifaa Platform — Platforms, Pricing & Fallbacks (11 Nov update)

Nov 11, 2026 · @Hamthan

## The answer

**The 11 Nov stack costs about US$903 a month per company at Light usage, US$1,775 at Normal and US$3,936 at Heavy** (AED 3,317 / 6,518 / 14,454 at the 3.6725 peg), every vendor and AWS included, VAT excluded. Against the 10 Nov plan that is +3 / −10 / +32 USD: Better Stack's bigger log bundle at Heavy, offset by Claude leaving the AWS bill (AWS Support+ is 9% of AWS charges).

- **Four changes from Hamthan (11 Nov):** Celery + Celery Beat replace Procrastinate; in-house staff login replaces Amazon Cognito; Better Stack becomes the one place for uptime, errors, logs and on-call alerts; the embedding contest picks **Qwen3-Embedding-0.6B** in our UAE container.
- **Unchanged:** Claude Sonnet 5.5 on money turns and Haiku 5.5 on the rest (now called directly through the Claude API, not Bedrock), Deepgram, ElevenLabs, LiveKit Cloud, Telnyx → Twilio, Meta WhatsApp direct, SES + Postmark, Fargate, RDS Multi-AZ, ElastiCache Redis, Phoenix AI tracing, Vercel Pro.
- **For the client,** these costs are requested in phases, not all at once: see the separate doc *Arifaa Pack — Platform Resources by Phase*. Development starts at about US$50 a month.
- **Biggest swing** is still the share of money turns and Claude cache rebuilds (*Usage, assumptions and sensitivity* below).
- **Before the first bill:** about US$20 of prepaid usage balance (Telnyx $10, ElevenLabs $5, Gemini $5 only if the Google fallback is approved).

## What changed on 11 Nov

| Change | Decision | Monthly effect (Light / Normal / Heavy) | What it means for the build |
| --- | --- | --- | --- |
| Background jobs | **Celery 5.7 + Celery Beat** with Redis (ElastiCache) as the broker replaces Procrastinate | +US$9.37 / 9.37 / 9.37 (one small Beat container, 0.25 vCPU + 0.5 GB) | Port 11 tasks and 4 periodic jobs. Run exactly one Beat. Keep long waits (day 2/5/10 follow-ups, 30/60/120-min ladder) in a PostgreSQL schedule table that Beat sweeps every 30 s, never as long Celery ETAs. Tasks idempotent with `acks_late`. Redis set to `noeviction` |
| Staff login + MFA | **In-house login** (FastAPI): Argon2 passwords, short-lived RS256 tokens our existing JWT check already accepts, 6-digit second-step codes | US$0 (Cognito was also US$0 for 15 staff; emails cost under US$0.01) | Recommended second step: **authenticator-app code (TOTP)**, with the email code as fallback and for recovery. NIST SP 800-63B-4 says email "SHALL NOT be used for out-of-band authentication"; staff approve real prices, so the stronger factor is worth the same build effort |
| Monitoring | **Better Stack** for uptime checks, status page, error tracking (Sentry SDK), app logs and phone/SMS on-call alerts; replaces Sentry Team, UptimeRobot and CloudWatch Container Insights | +US$0.6 / 0.6 / 53 net (1 responder $34 + log bundle $30, or $120 at Heavy; minus Sentry $29 and Container Insights $44–47; plus a $9.37 OpenTelemetry collector) | Data is stored in the EU by default; no UAE region. Logs must be PII-masked before they leave AWS, and Arifa must approve Better Stack as a processor. CloudWatch Database Insights Advanced (US$18) and Phoenix (AI traces) stay in the UAE |
| Knowledge search | **Qwen3-Embedding-0.6B** (Apache 2.0) in our UAE container wins the 5-model contest; fallback PostgreSQL full-text | US$0 (same 1 vCPU + 4 GB container, US$44.27) | Best multilingual score of the self-hosted models (MMTEB 64.33). Outputs 1,024 dimensions, so the existing `Vector(1024)` table needs no migration. Must pass a retrieval test in en / ar / hi / ta / zh / si before go-live |
| Email price | New SES accounts start on the **Essentials** plan, US$0.16 per 1,000 (was $0.10) | +US$0.12 / 0.60 / 2.40 | None |
| AI route | Claude Sonnet 5.5 and Haiku 5.5 called directly through the Claude API from our application; Amazon Bedrock dropped | Token prices unchanged; AWS Support+ falls about US$4 / 21 / 66 because Claude leaves the AWS bill | Prepaid Claude credits (valid 1 year), monthly spend cap, starting-tier limit 4,000 requests a minute. Data processed by Anthropic outside the UAE (global routing), deleted within 30 days by default; Arifa must approve |

**Can Better Stack replace everything?** It replaces Sentry (it accepts the Sentry SDK), UptimeRobot, status pages, and log, metric and trace stores like Loki, Mimir and Tempo, and adds phone on-call. It does not replace three things:

- **AI traces with prices** stay in Phoenix in the UAE.
- **AWS bills** are not ingested by Better Stack; AWS Budgets and cost alerts cover them.
- **Raw AWS logs** still land in CloudWatch first (ECS and RDS write there by default); only the masked app logs are shipped on.

Datadog and Grafana Cloud are alternatives to Better Stack, not tools to connect to it.

## Recommended stack, field by field

US$ per company per month. Every field has a primary, a backup and a last resort (*Fallback plan* tab); the price guard, approval gate, consent and quiet hours run after every switch. Rows are rounded, so they add to the total within US$2. Rows changed on 11 Nov are marked **(new)**.

| Field | Pick | Light | Normal | Heavy | Backup → last resort | Price page |
| --- | --- | --- | --- | --- | --- | --- |
| AI brain — money turns | Claude Sonnet 5.5 via the Claude API, direct (new) | 49 | 228 | 735 | Gemini 3.8 Flash (only if it passed the test suite and Arifa agreed) → hold and hand to the salesperson | [Claude](https://platform.claude.com/docs/en/about-claude/pricing#model-pricing) |
| AI brain — other turns | Claude Haiku 5.5 via the Claude API, direct (new) | (in row above) |  |  | Gemini 3.5 Flash-Lite → safe reply + hand-over | [Claude](https://platform.claude.com/docs/en/about-claude/pricing#model-pricing) |
| Speech-to-text | Deepgram Nova-3 Multilingual | 14 | 69 | 230 | Soniox → website text chat | [Deepgram](https://deepgram.com/pricing) |
| Text-to-speech | ElevenLabs Flash | 27 | 135 | 450 | Google Chirp 3 HD → text on screen | [ElevenLabs](https://elevenlabs.io/pricing/api) |
| Real-time voice | LiveKit Cloud Ship | 50 | 75 | 250 | Website text chat → self-hosted LiveKit (phase 2) | [LiveKit](https://livekit.com/pricing) |
| Staff alert calls, buyer call-backs | Telnyx SIP trunk (after live du and Etisalat test calls) | 10 | 46 | 152 | Twilio → WhatsApp + push → next manager at 2 h | [Telnyx](https://telnyx.com/pricing/elastic-sip) |
| WhatsApp | Meta Cloud API direct, with send caps | 26 | 213 | 756 | Email with the same approved quote → SMS (v1.5) | [Meta](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing) |
| Email **(new price)** | Amazon SES Essentials to send + Postmark Pro for replies | 17 | 19 | 22 | Postmark sends → Gmail API from the salesperson's mailbox → WhatsApp | [SES](https://aws.amazon.com/ses/pricing/), [Postmark](https://postmarkapp.com/pricing) |
| Servers **(new)** | ECS Fargate ARM, 2 zones: API, Celery worker, **Celery Beat**, CMS, voice agent, **OpenTelemetry collector** | 150 | 150 | 188 | New containers in the healthy zone → roll back | [Fargate](https://aws.amazon.com/fargate/pricing/) |
| Background jobs **(new)** | **Celery 5.7 + Celery Beat**, Redis broker, schedules kept in PostgreSQL | 0 | 0 | 0 | Beat restarts by ECS; due jobs are picked up from the PostgreSQL table, nothing lost | [Celery](https://docs.celeryq.dev/en/stable/) |
| Database | RDS PostgreSQL Multi-AZ: db.t4g.medium at Light, db.m6g.large from Normal | 139 | 312 | 343 | Standby takes over (about 1 min) → point-in-time restore | [RDS](https://aws.amazon.com/rds/postgresql/pricing/) |
| Cache + job broker | ElastiCache for Redis OSS, 2 nodes in 2 zones, `noeviction` | 29 | 58 | 116 | Replica in the second zone → settings read from PostgreSQL | [ElastiCache](https://aws.amazon.com/elasticache/pricing/) |
| Website + CRM screens | Vercel Pro | 20 | 40 | 40 | Cloudflare static page → WhatsApp click-to-chat | [Vercel](https://vercel.com/docs/plans/pro-plan) |
| Knowledge search **(new)** | **Qwen3-Embedding-0.6B** (Apache 2.0) in our UAE container + PostgreSQL full-text | 44 | 44 | 44 | Full-text search only | [Model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) |
| AI tracing | Arize Phoenix self-hosted in the UAE | 19 | 19 | 19 | CloudWatch logs | [Phoenix licence](https://arize.com/docs/phoenix/self-hosting/license) |
| Uptime, errors, logs, on-call **(new)** | **Better Stack**: 1 responder licence + Telemetry Nano bundle (40 GB, 30-day logs); Micro (160 GB) at Heavy | 64 | 64 | 154 | CloudWatch alarms + AWS email alerts | [Better Stack](https://betterstack.com/pricing) |
| Database monitoring | CloudWatch Database Insights Advanced (slow queries, locks) | 18 | 18 | 18 | Database Insights Standard (free) | [CloudWatch](https://aws.amazon.com/cloudwatch/pricing/) |
| Staff login + MFA **(new)** | **In-house**: password + authenticator code (TOTP), email code fallback | 0 | 0 | 0 | Break-glass admin account (no MFA bypass) | [NIST 800-63B](https://pages.nist.gov/800-63-4/sp800-63b.html) |
| Firewall + bots | AWS WAF + Cloudflare Turnstile | 10 | 11 | 14 | App rate limits + daily AI spend cap | [WAF](https://aws.amazon.com/waf/pricing/) |
| Secrets | Secrets Manager for keys, Parameter Store for settings | 8 | 8 | 12 | Keys cached at start-up | [Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/) |
| AWS network, logs, storage | NAT × 2, ALB, IPv4, CloudWatch, S3, KMS, ECR, data out | 148 | 182 | 284 | Two zones | [VPC](https://aws.amazon.com/vpc/pricing/) |
| DNS + failover | Route 53, 1 zone + 2 health checks | 2 | 2 | 2 | Manual switch in Cloudflare | [Route 53](https://aws.amazon.com/route53/pricing/) |
| 3D models, videos | Cloudflare R2 | 1 | 2 | 5 | CloudFront flat-rate Free | [R2](https://developers.cloudflare.com/r2/pricing/) |
| Quote PDFs in Arabic | Headless Chromium, +2 GB on the worker | 7 | 7 | 7 | WeasyPrint | [Fargate](https://aws.amazon.com/fargate/pricing/) |
| FX rates, push, web analytics | CBUAE daily rates, Web Push, Cloudflare Web Analytics | 0 | 0 | 0 | Manager override | [CBUAE](https://www.centralbank.ae/en/forex-eibor/exchange-rates/) |
| AWS Business Support+ | greater of $29 or 9% of the AWS bill (Claude is billed by Anthropic, so it no longer counts) | 52 | 73 | 95 | — | [AWS Support](https://aws.amazon.com/premiumsupport/pricing/) |
| **Total** |  | **903** | **1,775** | **3,936** |  |  |

Removed on 11 Nov: Amazon Cognito (US$0), Sentry Team (US$29), UptimeRobot (US$0), CloudWatch Container Insights (US$44–47), Procrastinate (US$0).

## Usage, assumptions and sensitivity

Unit prices are fixed by the vendors; usage levels are our planning estimates, replaced by measured numbers after the shadow week.

| Usage per company per month | Light | Normal | Heavy |
| --- | --- | --- | --- |
| AI turns (2,000 new + 6,000 cached + 200 output tokens each) | 14,000 | 65,000 | 210,000 |
| Website voice minutes | 1,500 | 7,500 | 25,000 |
| WhatsApp messages sent (60% service, 35% utility, 5% marketing) | 3,900 | 19,500 | 65,000 |
| Emails out / in | 2,000 / 500 | 10,000 / 2,500 | 40,000 / 8,000 |
| Staff alert and call-back minutes | 36 | 180 | 600 |
| App logs shipped to Better Stack (after masking) | under 40 GB | under 40 GB | 40–160 GB |

Other assumptions: 2 WhatsApp business numbers; 15 staff logins; 1 Better Stack responder (Kairox on-call); money turns 40% of AI turns; Claude writes its prompt cache once per 10 turns.

| If this changes | Light | Normal | Heavy |
| --- | --- | --- | --- |
| Money turns 20% of AI turns | 882 | 1,675 | 3,614 |
| **Money turns 40% (the plan)** | **903** | **1,775** | **3,936** |
| Money turns 60% | 925 | 1,874 | 4,258 |
| Money turns 80% | 946 | 1,974 | 4,580 |
| Claude rebuilds its cache on every money turn (worst case) | +82 | +377 | +1,219 |
| WhatsApp marketing templates 15% of messages (plan 5%) | +14 | +72 | +241 |
| A second Better Stack responder (two people on call) | +34 | +34 | +34 |
| Haiku 5.5 passes the negotiation test and takes money turns too | −43 | −200 | −644 |
| Valkey instead of Redis on ElastiCache | −6 | −13 | −25 |
| Gemini Embedding 2 instead of the Qwen3 container (queries leave the UAE) | −48 | −47 | −44 |
| Annual billing for Better Stack (responder $29, Nano $25, Micro $100) | −10 | −10 | −25 |

- **A WhatsApp message** costs more than the AI turn that writes it, so fewer, fuller messages save more than any model switch.
- **Spend caps** per company per day (AI, voice minutes, WhatsApp templates): alert at 80%, degrade to text-only or hand-over at 100%.
- **Not in these totals:** the staging environment (about US$310 a month at full size, about US$150 if it sleeps outside working hours), VAT, Kairox team time, GitHub seats, extra Vercel seats (US$20 each), SMS for v1.5, a Telnyx UAE number.

## Prepaid balances and decisions before go-live

No vendor in the plan charges a setup fee on its official pages. About **US$20 of prepaid usage balance** is needed before first use: Telnyx minimum payment $10, ElevenLabs minimum top-up $5, Gemini minimum prepay $5 (only if the Google fallback is approved). Monthly plans billed from the first month: LiveKit Ship, Vercel Pro, Postmark Pro, Better Stack, AWS Business Support+ ($29 minimum).

- [ ] Arifa approves the processor list and consent text in en / ar / hi / ta / zh, now including **Better Stack (EU data location)** for masked logs and errors.
- [ ] Arifa says yes or no to Google Gemini as the standby AI.
- [ ] Staff MFA method: authenticator app (recommended) or email code.
- [ ] Pass mark for Haiku 5.5 on the 50 negotiation scenarios, in all five languages.
- [ ] Qwen3-Embedding-0.6B passes the retrieval test in en / ar / hi / ta / zh / si; bge-m3 is the runner-up if it fails.
- [ ] Celery design review: one Beat, PostgreSQL schedule table, idempotent tasks, Redis `noeviction`.
- [ ] Live test calls on du and Etisalat from the Telnyx caller ID; Twilio stays wired as backup.
- [ ] Open the Claude API account in Arifa's name, buy prepaid credit and set a monthly spend cap (USD 500 maximum on the starting tier).
- [ ] Cross-region backup: only with Arifa's written consent.
- [ ] Update the signed flow: Haiku on non-price turns, Gemini fallbacks, Phoenix, Redis, Qwen3 embeddings, Celery, in-house login, Better Stack.
