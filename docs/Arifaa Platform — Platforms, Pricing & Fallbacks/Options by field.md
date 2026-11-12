# Options by field

Each field has a High (premium), Medium (balanced) and Low (budget or open source) option, with the official unit price and the monthly cost per company at Light, Normal and Heavy usage (US$). **Bold** = recommended pick. Prices re-checked on official pages 7–11 Nov 2026 (*Official sources* tab). Fields 13, 15, 16 and 19 changed on 11 Nov.

## 1. AI brain (LLM)

Reads the buyer's message and our rules, and decides what to say and which tool to call. A money turn talks price, discount or terms (assumed 40% of turns).

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | Claude Sonnet 5.5, every turn | [$2 in / $0.10 cache read / $2.50 cache write / $10 out per 1M](https://platform.claude.com/docs/en/about-claude/pricing#model-pricing); called directly via the Claude API (Bedrock dropped 11 Nov) | 113 | 526 | 1,701 | Strongest; signed flow names Claude |
| Medium | Gemini 3.8 Flash, every turn | [$1.50 / $0.15 cached / $7.50 out from 1 Jan 2027](https://aistudio.google.com/docs/pricing) | 98 | 373 | 1,156 | Costed at the 2027 price |
| Low | Claude Haiku 5.5, every turn | [$0.10 in / $0.01 cache read / $0.50 out per 1M](https://platform.claude.com/docs/en/about-claude/pricing#model-pricing) | 6 | 28 | 91 | Quality on price turns must be proven |
| Mix | **Sonnet 5.5 on money turns + Haiku 5.5 on the rest** | rows above | 49 | 228 | 735 | Recommended. Fallbacks: Sonnet → Gemini 3.8 Flash; Haiku → Gemini 3.5 Flash-Lite |
| Mix | Gemini 3.8 Flash + Flash-Lite | rows above | 63 | 212 | 637 | Breaks the signed "LangGraph + Claude" flow |

## 2. Speech-to-text

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | **Deepgram Nova-3 Multilingual** | [$0.0092 per min](https://deepgram.com/pricing) | 14 | 69 | 230 | ar, hi, ta, zh; no Sinhala |
| Medium | ElevenLabs Scribe v2 Realtime | [$0.39 per hour](https://elevenlabs.io/pricing/api) | 10 | 49 | 162 | Languages not itemised |
| Low | Soniox real-time | [$0.12 per hour](https://soniox.com/pricing) | 3 | 15 | 50 | Backup |

## 3. Text-to-speech

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | **ElevenLabs Flash** | [$0.04 per 1K characters](https://elevenlabs.io/pricing/api) | 27 | 135 | 450 | Check plan concurrency |
| Medium | Google Chirp 3 HD | [1M chars free, then $30 per 1M](https://cloud.google.com/text-to-speech/pricing) | 0 | 71 | 308 | Backup |
| Low | Google Standard / WaveNet | [4M free, then $4 per 1M](https://cloud.google.com/text-to-speech/pricing) | 0 | 0 | 29 | Robotic |

## 4. Real-time voice

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | **LiveKit Cloud Ship** | [$50/month incl. 5,000 agent min, then $0.01/min; 20 sessions at once](https://livekit.com/pricing) | 50 | 75 | 250 | Build plan $0 (1,000 min, 5 sessions) is enough for development |
| Medium | Pipecat Cloud | [$0.01 per session-minute](https://www.daily.co/pricing/pipecat-cloud) | 15 | 75 | 250 | Agent rewrite needed |
| Low | Self-hosted LiveKit on EC2 + Fargate | [EC2 c6g](https://aws.amazon.com/ec2/pricing/on-demand/) | 134 | 267 | 534 | Phase 2; about 60 h to build |

## 5. Staff alert calls and buyer call-backs

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | Twilio | [$0.2995 per min to UAE mobile](https://www.twilio.com/en-us/voice/pricing/ae) | 12 | 55 | 181 | Backup; already built |
| Medium | **Telnyx** | [$0.25 per min to UAE mobile](https://telnyx.com/pricing/elastic-sip) + $0.002 Voice API | 10 | 46 | 152 | Live du/Etisalat test first |
| Low | AWS End User Messaging | [$0.269 per min](https://aws.amazon.com/end-user-messaging/pricing) | 10 | 48 | 161 | Scripted only |

## 6. WhatsApp

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | Twilio WhatsApp | Meta fee + [$0.005 per message](https://www.twilio.com/en-us/whatsapp/pricing) | 56 | 359 | 1,243 |  |
| Medium | Telnyx WhatsApp | Meta fee + [$0.004 per message](https://telnyx.com/pricing/whatsapp) | 50 | 330 | 1,146 |  |
| Low | **Meta Cloud API direct** | [Meta fee only](https://business.whatsapp.com/products/platform-pricing#rates); 1,000 service messages free per number a month | 26 | 213 | 756 | Already built |

## 7. SMS fallback (v1.5, not in totals)

| Tier | Platform | Official unit price | Light | Normal | Heavy |
| --- | --- | --- | --- | --- | --- |
| High | Twilio | [UAE $0.1176 per segment](https://www.twilio.com/en-us/sms/pricing/ae) | 20 | 100 | 301 |
| Medium | Telnyx | [UAE $0.104 per part](https://telnyx.com/pricing/messaging) | 21 | 104 | 312 |
| Low | Plivo | [UAE $0.0988–$0.1150](https://www.plivo.com/sms/pricing/ae/) | 19 | 94 | 281 |

## 8. Email

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | Postmark Pro (send + replies) | [$16.50/month for 10,000, then $1.30 per 1,000](https://postmarkapp.com/pricing) | 16 | 20 | 66 | Free plan: 100 emails a month for testing |
| Medium | **Amazon SES Essentials + Postmark Pro for replies** | [SES Essentials $0.16 per 1,000](https://aws.amazon.com/ses/pricing/) + Postmark Pro | 17 | 19 | 22 | Default plan for new SES accounts |
| Low | SES + SES Mail Manager | $50/month per endpoint + $0.15 per 1,000 | 50 | 51 | 55 | UAE availability inferred |
| Fallback now (main route in v2) | Gmail API via each salesperson's own Google Workspace mailbox | [No per-email fee under the daily billing threshold; send = 100 quota units, read = 20–60, limit 6,000 units per user per minute](https://developers.google.com/workspace/gmail/api/reference/quota); needs a [Google Workspace seat per mailbox](https://workspace.google.com/pricing) | 0 + seats | 0 + seats | 0 + seats | Used now only as the email fallback; main route in v2. Emails come from the salesperson's real address and replies land in the deal without Postmark; 0 extra if Arifa already pays for Workspace mailboxes; Gmail's daily sending limits apply; Google joins the processor list |

## 9. Servers

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High / Medium | **ECS Fargate ARM, 2 zones** | [$0.04208 per vCPU-h + $0.00464 per GB-h](https://aws.amazon.com/fargate/pricing/) | 150 | 150 | 188 | Now includes Celery Beat and an OpenTelemetry collector ($9.37 each) |
| Low | EC2 t4g we manage | [t4g.large $0.0816/h](https://aws.amazon.com/ec2/pricing/on-demand/) | 177 | 177 | 239 | No saving at this size |

## 10. Database

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | Aurora Serverless v2 | [$0.15 per ACU-hour](https://aws.amazon.com/rds/aurora/pricing/) | 231 | 450 | 900 | Dearest |
| Step | **db.t4g.medium Multi-AZ at Light, db.m6g.large Multi-AZ from Normal** | [$0.152/h, then $0.386/h](https://aws.amazon.com/rds/postgresql/pricing/) | 139 | 312 | 343 | Recommended |
| Low | db.t4g.medium single zone | [$0.076/h](https://aws.amazon.com/rds/postgresql/pricing/) | 69 | 69 | 83 | Staging only: one zone failure stops sales |

## 11. Cache and job broker

Rate limits, kill switches, sessions and, from 11 Nov, the **Celery broker**. A broker must never lose keys, so the parameter group uses `noeviction` (Celery's Redis guide: "Redis must also be prevented from evicting keys under memory pressure").

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Medium | **ElastiCache for Redis OSS, 2 nodes in 2 zones** | [cache.t3.micro $0.0198/h → medium $0.0792/h](https://aws.amazon.com/elasticache/pricing/) | 29 | 58 | 116 | Replica takes over automatically |
| Medium (alt) | ElastiCache for Valkey | [20% lower](https://aws.amazon.com/elasticache/pricing/) | 23 | 46 | 93 | Same commands; Celery's Redis transport works |
| Low | Redis in one Fargate container | [Fargate ARM](https://aws.amazon.com/fargate/pricing/) | 19 | 19 | 37 | Development and staging only |

## 12. Website and CRM screens

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | **Vercel Pro** | [$20/month incl. 1 seat + $20 usage](https://vercel.com/docs/plans/pro-plan) | 20 | 40 | 40 | Hobby is "non-commercial, personal use only" |
| Medium | Netlify Pro | [$20/month](https://www.netlify.com/pricing/) | 20 | 20 | 20 |  |
| Low | Cloudflare Workers Paid | [$5/month](https://developers.cloudflare.com/workers/platform/pricing/) | 5 | 5 | 5 | Next.js needs an adapter |

## 13. Knowledge search (embeddings) — changed 11 Nov

Turns product and policy text into vectors so the AI finds the right facts in the buyer's language. Self-hosted models are free to download; the cost is the UAE container (1 vCPU + 4 GB, $44.27). Our database column is `Vector(1024)`, so a 1,024-dimension model needs no migration. Full contest: *Platform contest* tab.

| Rank | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| **1** | **[Qwen/Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)** in our UAE container | Free (Apache 2.0) + Fargate | 44 | 44 | 44 | Recommended. MMTEB multilingual 64.33; 32k tokens; 32–1,024 dims (MRL); 100+ languages; needs transformers ≥ 4.51 |
| 2 | [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | Free (MIT) + Fargate | 44 | 44 | 44 | Runner-up. MMTEB 59.56; 8,192 tokens; 1,024 dims; dense + sparse in one model; no prefixes |
| 3 | [intfloat/multilingual-e5-large](https://huggingface.co/intfloat/multilingual-e5-large) | Free (MIT) + Fargate | 44 | 44 | 44 | 10 Nov pick. 512 tokens; 1,024 dims; needs "query:" / "passage:" prefixes; no MMTEB mean on its card |
| Out | [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | Free (Apache 2.0) + small container | 9 | 9 | 9 | English only (card: "language: en"); 384 dims; 256 word pieces. Fails Arabic, Hindi, Tamil, Chinese |
| API alt | [Gemini Embedding 2](https://ai.google.dev/gemini-api/docs/embeddings) | [$0.20 per 1M tokens](https://aistudio.google.com/docs/pricing) | 0.28 | 1.30 | 4.20 | Best quality (Gemini row on the Qwen3 card: 68.37) but buyer questions leave the UAE; set output to 1,024 dims |

Fallback for any model: PostgreSQL full-text search. Vectors from two models cannot be mixed, so a model change means re-indexing.

## 14. AI tracing

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | Langfuse Cloud Pro | [$199/month](https://langfuse.com/pricing) | 199 | 217 | 275 |  |
| Medium | Langfuse Cloud Core | [$29/month](https://langfuse.com/pricing) | 29 | 47 | 105 |  |
| Low | **Arize Phoenix self-hosted in the UAE** | [free (Elastic License 2.0)](https://arize.com/docs/phoenix/self-hosting/license) + Fargate | 19 | 19 | 19 | Prices and chats stay in the UAE; Better Stack does not replace it |

## 15. Monitoring: uptime, errors, logs, on-call — changed 11 Nov

One place to see whether the site is up, what broke, and who gets called. Before 11 Nov this was Sentry + UptimeRobot + CloudWatch Container Insights.

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | Datadog (infrastructure + database monitoring + logs) | [Infra Pro $18/host on demand; DB Monitoring $84/host](https://www.datadoghq.com/pricing/list/) | 93+ | 93+ | 94+ | Strong; data leaves the UAE; logs priced separately |
| Medium | **Better Stack**: 1 responder + Telemetry bundle | [Responder $34/month ($29 annual); Nano $30/month (40 GB); Micro $120 (160 GB); 100k exceptions; Sentry SDK compatible](https://betterstack.com/pricing) | 64 | 64 | 154 | Recommended. Uptime + status page + errors + logs + metrics + traces + phone/SMS on-call in one tool. EU storage by default; no UAE region |
| Medium (alt) | Sentry Team + UptimeRobot + CloudWatch Container Insights | [Sentry $29](https://sentry.io/pricing/); [UptimeRobot $0](https://uptimerobot.com/pricing/); [Container Insights $0.07/metric](https://aws.amazon.com/cloudwatch/pricing/) | 73 | 73 | 76 | The 10 Nov set: three tools, no phone on-call |
| Low | Grafana Cloud Free + CloudWatch | [Free: 10k series, 50 GB logs, 14 days](https://grafana.com/pricing/) | 0 | 0 | 19+ | More set-up work; no on-call phone alerts |

Kept beside Better Stack: CloudWatch Database Insights Advanced, $0.0125 per database vCPU-hour ($18.25 a month) for slow-query and lock analysis inside the UAE.

## 16. Staff login and MFA — changed 11 Nov

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| High | Auth0 Essentials | [$35/month](https://auth0.com/pricing) | 35 | 35 | 35 |  |
| Medium | Amazon Cognito Essentials | [10,000 MAU free; TOTP, SMS and email OTP MFA](https://aws.amazon.com/cognito/pricing/) | 0 | 0 | 0 | 10 Nov pick |
| Low | **In-house (FastAPI)**: password + TOTP authenticator code, email code fallback | Free libraries; email codes via SES ($0.16 per 1,000) | 0 | 0 | 0 | Recommended by Hamthan. NIST 800-63B-4: email "SHALL NOT be used for out-of-band authentication", so TOTP is the main factor |

## 17. Firewall and bots

| Tier | Platform | Official unit price | Light | Normal | Heavy |
| --- | --- | --- | --- | --- | --- |
| High | Cloudflare Business | [$250/month](https://www.cloudflare.com/plans/) | 250 | 250 | 250 |
| Medium | AWS WAF + Bot Control + Turnstile | [WAF pricing](https://aws.amazon.com/waf/pricing/) | 20 | 21 | 24 |
| Low | **AWS WAF basic + Turnstile** | [$5 + $1 per rule + $0.60 per 1M](https://aws.amazon.com/waf/pricing/) | 10 | 11 | 14 |

## 18. Secrets

| Tier | Platform | Official unit price | Light | Normal | Heavy |
| --- | --- | --- | --- | --- | --- |
| High | **Secrets Manager for keys + Parameter Store for settings** | [$0.40 per secret](https://aws.amazon.com/secrets-manager/pricing/) | 8 | 8 | 12 |
| Low | Parameter Store only | [no charge](https://aws.amazon.com/systems-manager/pricing/) | 0 | 0 | 0 |

## 19. Background jobs — changed 11 Nov

Runs follow-ups, reminder ladders, outbox relay, FX updates and retention jobs.

| Tier | Platform | Official unit price | Light | Normal | Heavy | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Recommended | **Celery 5.7 + Celery Beat**, Redis broker | Open source; Beat container $9.37 (in Servers) | 0 | 0 | 0 | Mature, large ecosystem, Flower dashboard. Only one Beat at a time; long delays kept in PostgreSQL |
| Alt | Procrastinate (10 Nov) | Open source; queue in PostgreSQL | 0 | 0 | 0 | Async-native and transactional with the outbox; already in the code |

## Fields every stack shares

| Field | Platform | Official unit price | Light | Normal | Heavy |
| --- | --- | --- | --- | --- | --- |
| DNS + failover | Route 53 | [$0.50 per zone](https://aws.amazon.com/route53/pricing/) | 2 | 2 | 2 |
| 3D models, videos | Cloudflare R2 | [$0.015/GB-month, no egress fee](https://developers.cloudflare.com/r2/pricing/) | 1 | 2 | 5 |
| Quote PDFs in Arabic | Headless Chromium on the worker | [Fargate](https://aws.amazon.com/fargate/pricing/) | 7 | 7 | 7 |
| AWS network, logs, storage | NAT × 2, ALB, IPv4, CloudWatch, S3, KMS, ECR | see AWS pages | 148 | 182 | 284 |
| AWS Business Support+ | greater of $29 or 9% of the AWS bill | [AWS Support](https://aws.amazon.com/premiumsupport/pricing/) | 56 | 94 | 161 |
