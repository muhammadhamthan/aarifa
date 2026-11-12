# Official sources

Every price in this doc was read on the vendor's own page. **VERIFIED** = read 7–11 Nov 2026. **READ 5 NOV** = AWS page with region Middle East (UAE). **FLAG** = official but not a pricing page, or a detail the page does not fully show. AWS pages: pick **Middle East (UAE)** in the Region box.

## Added 11 Nov 2026

| Item | Price or fact | Official URL | Status |
| --- | --- | --- | --- |
| Better Stack Free | "Free for personal projects": 10 monitors, 100,000 exceptions, 3 GB logs (3 days) | [betterstack.com/pricing](https://betterstack.com/pricing) | VERIFIED — not for commercial use |
| Better Stack responder | $34 / $29 per licence a month (page does not label which is annual; meta text says "Starts at $29"); includes unlimited phone and SMS alerts | same | VERIFIED / FLAG on monthly vs annual |
| Better Stack Telemetry bundles | Nano 40 GB $30 a month ($25 yearly); Micro 160 GB $120 ($100); Mega 340 GB $250; Tera 700 GB $500; 30-day log retention. Two dearer price sets shown for other data locations | same | VERIFIED / FLAG on which set is EU |
| Better Stack error tracking | 100,000 exceptions included, then $0.00005–$0.000175 each; 90-day retention; "Sentry SDK-compatible" | [betterstack.com/error-tracking](https://betterstack.com/error-tracking) | VERIFIED |
| Better Stack data location | "By default, all data is stored in the EU regions"; quote form offers US, Europe, Singapore; custom locations for enterprise; own S3 bucket $250 a month | [betterstack.com/pricing](https://betterstack.com/pricing) | VERIFIED — no UAE region |
| Better Stack integrations | Logs from Amazon ECS, PostgreSQL, Vercel; incidents from CloudWatch alarms; OpenTelemetry and Prometheus metrics; AWS billing ingestion not stated | [betterstack.com/integrations](https://betterstack.com/integrations) | VERIFIED |
| Better Stack compliance | SOC 2 Type 2 (attestation under NDA); GDPR with DPA; not HIPAA | [betterstack.com/pricing](https://betterstack.com/pricing) | VERIFIED |
| Qwen3-Embedding-0.6B | Apache 2.0; 0.6B parameters; 32k context; 32–1,024 dims (MRL); 100+ languages; transformers ≥ 4.51; MMTEB mean 64.33 vs multilingual-e5-large-instruct 63.22, BGE-M3 59.56, Gemini Embedding 68.37 (leaderboard 24 May 2025) | [huggingface.co/Qwen/Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | VERIFIED |
| BAAI/bge-m3 | MIT; 1,024 dims; 8,192 tokens; 100+ languages; dense, sparse and multi-vector; no query instruction needed | [huggingface.co/BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | VERIFIED |
| intfloat/multilingual-e5-large | MIT; 0.6B; 1,024 dims; 512 tokens; "query: " / "passage: " prefixes required | [huggingface.co/intfloat/multilingual-e5-large](https://huggingface.co/intfloat/multilingual-e5-large) | VERIFIED |
| all-MiniLM-L6-v2 | Apache 2.0; "language: en"; 384 dims; truncates above 256 word pieces | [huggingface.co/sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | VERIFIED — English only |
| Gemini embeddings | gemini-embedding-2 (latest) and gemini-embedding-001; 3,072 dims by default, MRL down to smaller sizes; 8,192 input tokens (embedding-2); $0.20 per 1M text tokens; paid-tier content "not used to improve our products" | [ai.google.dev/gemini-api/docs/embeddings](https://ai.google.dev/gemini-api/docs/embeddings); [aistudio.google.com/docs/pricing](https://aistudio.google.com/docs/pricing) | VERIFIED |
| Celery | 5.7.0 released 5 Nov 2026; docs: runs on Python 3.11+; "ensure only a single scheduler is running"; schedules in seconds allowed | [docs.celeryq.dev/en/stable](https://docs.celeryq.dev/en/stable/); [periodic tasks](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html); [pypi.org/project/celery](https://pypi.org/project/celery/) | VERIFIED |
| Celery on Redis | Default visibility timeout 1 hour; ETA tasks longer than it are "executed again, and again in a loop"; "Broker is not a database"; keys must not be evicted | [Redis broker guide](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html) | VERIFIED |
| Celery acks\_late | "the task may be executed multiple times should the worker crash … Make sure your tasks are idempotent" | [Tasks guide](https://docs.celeryq.dev/en/stable/userguide/tasks.html) | VERIFIED |
| Procrastinate | PostgreSQL 13+ task queue, sync and async APIs, periodic tasks | [procrastinate.readthedocs.io](https://procrastinate.readthedocs.io/en/stable/) | VERIFIED |
| NIST SP 800-63B-4 | "Email SHALL NOT be used for out-of-band authentication"; email codes for address confirmation and recovery are exempt; AAL2 verifiers must offer a phishing-resistant option | [pages.nist.gov/800-63-4/sp800-63b.html](https://pages.nist.gov/800-63-4/sp800-63b.html) | VERIFIED |
| OWASP MFA cheat sheet | "Any MFA is better than no MFA"; "Provide the option … to enable MFA … using TOTP"; email codes rely entirely on the email account's security | [cheatsheetseries.owasp.org MFA](https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html) | VERIFIED |
| Amazon Cognito | Lite and Essentials 10,000 MAU free, no expiry; Essentials includes TOTP, SMS and email OTP MFA | [aws.amazon.com/cognito/pricing](https://aws.amazon.com/cognito/pricing/) | VERIFIED |
| Amazon SES | Essentials $0.16 per 1,000 (default for new accounts from 21 Jul 2026); à la carte $0.10 per 1,000 + $0.12 per GB attachments | [aws.amazon.com/ses/pricing](https://aws.amazon.com/ses/pricing/) | VERIFIED |
| Sentry | Developer free: 1 user, 5k errors; Team $29 monthly ($26 annual), 50k errors | [sentry.io/pricing](https://sentry.io/pricing/) | VERIFIED |
| LiveKit Build | $0, no card: 1,000 agent minutes, 5 concurrent sessions; Ship from $50 | [livekit.com/pricing](https://livekit.com/pricing) | VERIFIED |
| Postmark | Free: 100 emails a month, "never expires"; Basic $15; Pro $16.50 with inbound | [postmarkapp.com/pricing](https://postmarkapp.com/pricing) | VERIFIED |
| Vercel Hobby | "restricts users to non-commercial, personal use only"; Pro seats $20 per user, viewer seats free | [vercel.com/docs/plans/hobby](https://vercel.com/docs/plans/hobby) | VERIFIED |

**Claude API, direct (replaces Bedrock, 11 Nov):**

| Item | Price or fact | Official URL | Status |
| --- | --- | --- | --- |
| Claude API prices | Sonnet 5.5: input 2, 5-min cache write 2.50, 1-h write 4, cache read 0.10, output 10 USD per 1M. Haiku 5.5 (prompts up to 100K): 0.10 / 0.125 / 0.20 / 0.01 / 0.50; over 100K: 0.50 / 0.625 / 1 / 0.05 / 2.50. Batch API 50% off. US-only inference (inference\_geo) 1.1× | [platform.claude.com pricing](https://platform.claude.com/docs/en/about-claude/pricing) | VERIFIED 11 Nov |
| Claude API billing | Prepaid credits, expire one year after purchase, non-refundable, optional auto-reload; minimum purchase not stated; invoicing only via Anthropic Sales | [support.claude.com](https://support.claude.com/en/articles/8977456-how-do-i-pay-for-my-api-usage) | VERIFIED 11 Nov |
| Claude API limits | Tiers Start / Build / Scale / Custom; monthly spend cap 500 / 1,000 / 200,000 USD; Start: 4,000 requests, 2M input and 400K output tokens a minute for Sonnet 5.5 and Haiku 5.5 | [platform.claude.com rate limits](https://platform.claude.com/docs/en/api/rate-limits) | VERIFIED 11 Nov |
| Claude API data | Default routing "may run in any available geography"; only "us" offered as a restricted geo; inputs and outputs deleted within 30 days by default | [data residency](https://platform.claude.com/docs/en/manage-claude/data-residency); [retention](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data) | VERIFIED 11 Nov — no UAE option |

## AI models (7 Nov)

| Item | Price | Official URL | Status |
| --- | --- | --- | --- |
| Claude Sonnet 5.5 | in $2 / cache read $0.10 / 5-min write $2.50 / out $10 per 1M | [platform.claude.com pricing](https://platform.claude.com/docs/en/about-claude/pricing#model-pricing) | VERIFIED |
| Claude Haiku 5.5 | in $0.10 / cache read $0.01 / 5-min write $0.125 / out $0.50 per 1M | same | VERIFIED |
| Claude on Bedrock from UAE | me-central-1: Global cross-region inference only | [aws.amazon.com/bedrock/pricing](https://aws.amazon.com/bedrock/pricing/) | VERIFIED |
| Titan Text Embeddings V2 | Not offered in me-central-1 | [Bedrock regions](https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html) | VERIFIED — removed |
| Gemini 3.8 Flash | $0.75 / $3.75 to 31 Dec 2026; $1.50 / $7.50 from 1 Jan 2027 | [aistudio.google.com/docs/pricing](https://aistudio.google.com/docs/pricing) | VERIFIED |
| Gemini 3.5 Flash-Lite | $0.30 in / $2.50 out | same | VERIFIED |
| Groq gpt-oss-120b | base limit 8K tokens a minute | [console.groq.com/docs/rate-limits](https://console.groq.com/docs/rate-limits) | VERIFIED — removed |

## Speech, voice, phone, messaging (7 Nov)

| Item | Price | Official URL | Status |
| --- | --- | --- | --- |
| Deepgram Nova-3 Multilingual | $0.0092/min; $200 free credit; no Sinhala | [deepgram.com/pricing](https://deepgram.com/pricing) | VERIFIED |
| Soniox real-time | $0.12/hour | [soniox.com/pricing](https://soniox.com/pricing) | VERIFIED |
| ElevenLabs Flash | $0.04 per 1K characters; PAYG minimum top-up $5 | [elevenlabs.io/pricing/api](https://elevenlabs.io/pricing/api) | VERIFIED |
| Google Chirp 3 HD | 1M chars free, then $30 per 1M | [cloud.google.com/text-to-speech/pricing](https://cloud.google.com/text-to-speech/pricing) | VERIFIED |
| Telnyx to UAE | mobile $0.25/min + $0.002 Voice API; minimum payment $10 | [telnyx.com/pricing/elastic-sip](https://telnyx.com/pricing/elastic-sip) | VERIFIED |
| Twilio to UAE | mobile $0.2995/min | [twilio.com/en-us/voice/pricing/ae](https://www.twilio.com/en-us/voice/pricing/ae) | VERIFIED |
| Meta WhatsApp | service replies charged since 1 Oct 2026; 1,000 free per number a month; UAE marketing $0.0576, utility/service $0.0157 | [Meta developer docs](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing); [rates](https://business.whatsapp.com/products/platform-pricing#rates) | VERIFIED |

## AWS, Middle East (UAE) me-central-1

| Item | Price | Official URL | Status |
| --- | --- | --- | --- |
| Fargate ARM | $0.04208 per vCPU-h, $0.00464 per GB-h | [aws.amazon.com/fargate/pricing](https://aws.amazon.com/fargate/pricing/) | VERIFIED |
| RDS PostgreSQL | db.t4g.medium $0.076/h single-AZ, $0.152/h Multi-AZ; db.m6g.large $0.386/h Multi-AZ; gp3 $0.14 per GB-month | [aws.amazon.com/rds/postgresql/pricing](https://aws.amazon.com/rds/postgresql/pricing/) | VERIFIED |
| ElastiCache Redis OSS | cache.t3.micro $0.0198/h, small $0.0396/h, medium $0.0792/h | [aws.amazon.com/elasticache/pricing](https://aws.amazon.com/elasticache/pricing/) | VERIFIED |
| NAT gateway | $0.0528/h + $0.0528/GB | [aws.amazon.com/vpc/pricing](https://aws.amazon.com/vpc/pricing/) | VERIFIED |
| Public IPv4 | $0.005 per hour | same | VERIFIED |
| ALB | $0.028224/h + $0.00896 per LCU-h | [aws.amazon.com/elasticloadbalancing/pricing](https://aws.amazon.com/elasticloadbalancing/pricing/) | VERIFIED |
| WAF | $5 per web ACL, $1 per rule, $0.60 per 1M | [aws.amazon.com/waf/pricing](https://aws.amazon.com/waf/pricing/) | VERIFIED |
| Secrets Manager | $0.40 per secret + $0.05 per 10,000 calls | [aws.amazon.com/secrets-manager/pricing](https://aws.amazon.com/secrets-manager/pricing/) | VERIFIED |
| CloudWatch | logs $0.63 per GB ingested; Database Insights Advanced $0.0125 per vCPU-hour | [aws.amazon.com/cloudwatch/pricing](https://aws.amazon.com/cloudwatch/pricing/) | READ 5 NOV / VERIFIED |
| Business Support+ | greater of $29 or 9% of AWS charges | [aws.amazon.com/premiumsupport/pricing](https://aws.amazon.com/premiumsupport/pricing/) | VERIFIED |
| UAE dirham | USD 1 = AED 3.6725 | [centralbank.ae](https://www.centralbank.ae/en/forex-eibor/exchange-rates/) | VERIFIED |

Other vendor rows (Twilio SMS, Plivo, 360dialog, Netlify, Cloudflare, Langfuse, GlitchTip, Datadog, Grafana, GitHub) are unchanged from the 7–10 Nov ledger.
