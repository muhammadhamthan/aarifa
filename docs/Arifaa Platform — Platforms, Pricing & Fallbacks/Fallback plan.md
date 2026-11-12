# Fallback plan

Every field has a primary, a backup and a last resort, so one vendor failing never stops a sale. Switches are automatic wherever code can detect the failure; the price guard, approval gate, consent and quiet hours run after every switch, whichever vendor answered. Rows changed on 11 Nov are marked **(new)**.

## How a switch works

- **One adapter per field.** The code calls `llm.reply()`, `stt.stream()`, `calls.place()` and so on, never a vendor directly. Each adapter holds an ordered vendor list from Parameter Store, so the order changes without a deploy.
- **Detect failure:** HTTP 5xx, connection lost, HTTP 429, or slower than the trigger time below.
- **Circuit breaker:** after 3 failures in 60 seconds the vendor is skipped for 5 minutes; then one test call decides whether it comes back.
- **Alert:** every switch writes a metric and pages the on-call engineer through **Better Stack** (phone, SMS, email); CloudWatch alarms are the backup path.
- **Manual override:** the admin screen can force a vendor on or off, or pause a whole channel (kill switch).

## Fallback chain per field

| Field | Primary | Backup 1 | Backup 2 / last resort | Switch trigger | What the buyer or staff sees |
| --- | --- | --- | --- | --- | --- |
| AI brain | Money turns: Claude Sonnet 5.5. Other turns: Claude Haiku 5.5 (both via the Claude API, direct) | Money: Gemini 3.8 Flash (only if suite-passed and Arifa agreed). Other: Gemini 3.5 Flash-Lite | Safe reply ("Let me check with our sales team") + hand-over | No first word in 8 s, 5xx or 429, or the price guard rejects twice | A slightly slower answer; never a wrong price |
| Speech-to-text | Deepgram Nova-3 | Soniox | Switch to text chat | No transcript 3 s after speech ends | One "Sorry, could you repeat that?" |
| Text-to-speech | ElevenLabs Flash | Google Chirp 3 HD | Reply shown as text | No audio 1.5 s after the text is ready | A different voice |
| Real-time voice | LiveKit Cloud | Website text chat | WhatsApp click-to-chat | Browser cannot join in 5 s | "Voice is busy, let's chat here" |
| Staff alert calls | Telnyx | Twilio | WhatsApp + push → next person on the ladder | Not connected in 30 s | Phone rings from the backup number |
| WhatsApp to buyers | Meta Cloud API | Email with the same approved quote | SMS (v1.5) → salesperson told to call | Not delivered in 10 min | The quote arrives by email |
| Email | SES | Postmark (send) → Gmail API from the salesperson's Workspace mailbox | WhatsApp → salesperson told | SES error or bounce | No difference |
| Buyer email replies | Postmark inbound | Gmail API reads replies from the salesperson's Workspace mailbox | — | Webhook silent 1 h in business hours | No difference |
| Background jobs **(new)** | Celery workers + one Celery Beat | ECS restarts a failed Beat or worker; due jobs wait in the PostgreSQL schedule table and run on restart | Manager handles reminders by hand from the CRM inbox | Beat heartbeat missing 2 min (Better Stack heartbeat monitor) | Reminders a few minutes late; none lost or doubled (idempotent tasks) |
| Job broker **(new)** | Redis primary (ElastiCache) | Redis replica in the second zone (automatic) | Tasks re-created from the PostgreSQL outbox and schedule table | AWS detects the failure | No difference |
| Servers | Fargate, 2 zones | New containers in the healthy zone | Roll back → static "contact us on WhatsApp" page | Health check fails 3 times | A few seconds' pause |
| Database | RDS primary | Multi-AZ standby (about 1 min) | Point-in-time restore; read-only mode | AWS detects the failure | About a minute of "please wait" |
| Knowledge search **(new)** | Qwen3-Embedding-0.6B in our UAE container + full-text | PostgreSQL full-text only | Hand-over to the salesperson | Embedding call fails or > 2 s | Slightly less precise answers |
| Website | Vercel Pro | Cloudflare static standby page | WhatsApp click-to-chat | Uptime check fails 2 min | A simpler page |
| Staff login **(new)** | In-house login: password + TOTP code | Email code to the staff member's work address | Break-glass admin account (no MFA bypass, every use alerted) | Manual, or TOTP device lost | Approvals wait; nothing is sent unapproved |
| Monitoring **(new)** | Better Stack (uptime, errors, logs, on-call) | CloudWatch logs + alarms emailed via SNS | — | Better Stack ingestion or alert failing | Nothing visible to buyers |
| AI tracing | Phoenix in the UAE | CloudWatch logs | — | Automatic | Nothing visible |

## Rules that never change during a fallback

- **The price guard runs on every reply,** whichever model wrote it. A backup that fails the guard twice triggers the safe reply.
- **The approval gate stays closed.** If the database or login is down, quotes wait; nothing priced is sent without a salesperson's approval and a send grant.
- **No cold calls.** A telephony fallback only reaches staff or a buyer who asked for a call.
- **Consent covers every backup.** Each backup vendor, and Better Stack for logs, is named in the processor list before go-live.
- **A price turn only falls back to a tested model** that passed the 50-scenario negotiation suite in all five languages.
- **No job runs twice with effect.** Every Celery task checks an idempotency key before sending a message or placing a call, because `acks_late` can redeliver.
- **Spend caps per company per day:** alert at 80%, degrade to text-only or hand-over at 100%.
- **Kill-switch ladder:** stop marketing sends → stop outbound calls → text only → hold and hand everything to staff.

## Test before go-live

In staging, switch off each vendor in turn (including Redis, Celery Beat and Better Stack) and record how long the take-over took against the trigger times above.
