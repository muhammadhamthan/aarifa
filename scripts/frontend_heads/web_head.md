# Website API: frontend hand-off

For the developer building the **public website** (Next.js on Vercel, the 3D "Enter the factory" experience)
against a mock first. Taken from the code (`packages/kx-agent-framework/src/kx/agents/website.py`,
`kx/agents/voice.py`) and the SRS §6 (website flow), §8 (qualification), §8.1 (opt-out), D34/D36/D39. The
endpoint reference in the second half is **generated from the code**.

Companion files: `openapi-website.json` (OpenAPI 3.1 of these endpoints), `CMS.md` (where the page content
comes from), `CRM_API.md` (staff app).

---

## 1. Working against a mock

```bash
npx @stoplight/prism-cli mock docs/frontend/openapi-website.json --port 4011
```

Things a mock cannot do: the Turnstile check, the real AI reply, the LiveKit room. Real API locally:
`make up && kx migrate && kx api` (base URL `http://localhost:8000`). Locally the Turnstile check is skipped
when no secret is configured; staging and production refuse to start without it.

---

## 2. What the website does with the API (core design)

The site shows CMS content (pages, categories, products, FAQs, certificates, the factory tour; see `CMS.md`)
and hosts the AI assistant. The assistant is the **same AI as on WhatsApp and email**: same rules, same price
guard, and it never states a price that is not in an approved quote (prices never appear on the website,
§7). The conversation becomes a deal in the CRM.

SRS §6 journey and the calls behind it:

| Step (§6) | What the site does |
|---|---|
| 1 Landing: one button **Enter the factory** = consent to voice; the chat icon is always available | On *Enter the factory*: `POST /v1/public/sessions` with `voice_consent: true`. On opening the chat: the same call with `voice_consent: false` (unless a session already exists). Both need the Turnstile token. |
| 1 Recording notice | Played by the voice agent itself as the first thing it says ("This conversation is with our AI assistant and may be recorded ..."); the site does not play it. |
| 2 Guided tour, 3 Language | Chat: `POST /v1/public/sessions/{id}/messages`. Voice: `POST /v1/public/sessions/{id}/voice`, then join the LiveKit room. Send the visitor's language when they switch (`language` on a message). |
| 4 Qualification | Happens inside the conversation; no form is required. |
| 5 Requirement Summary, 6 Hand-over | The backend sends the summary PDF to WhatsApp or email. When `handed_over` comes back `true`, show "Our sales team is preparing your quotation; you'll receive it on WhatsApp shortly." |
| 7 Returning visitors | Only after they identify themselves: `POST /v1/public/sessions/{id}/identify` with the phone or email they typed. Never recognise anyone silently (no cookies matching, no pre-filled names). |

Languages: English, Arabic, Hindi, Tamil, Chinese, Sinhala (`en`, `ar`, `hi`, `ta`, `zh`, `si`). **Sinhala is
text-only**: `voice_available` is `false` for `si`, so hide the voice button and keep the chat.

---

## 3. Session and calls

1. `POST /v1/public/sessions` `{turnstile_token, language?, voice_consent, cookie_consent, country_code?}` ->
   `201 {session_id, session_token, expires_at, language, voice_available}`.
   * `turnstile_token`: from the Cloudflare Turnstile widget on the page (D39).
   * Keep `session_token` in memory (or sessionStorage) and send it as `Authorization: Bearer <session_token>`
     on the other calls. It lasts 2 hours; on `401` start a new session.
2. Chat turn: `POST /v1/public/sessions/{id}/messages` `{text, language?}` ->
   `{reply, language, handed_over}`. `reply` can be `null` (the conversation is with a person now): show
   nothing new, or the hand-over line. Show `reply` as plain text (it may contain line breaks).
3. Voice: `POST /v1/public/sessions/{id}/voice` -> `{livekit_url, token, room, language, expires_at}`.
   Join with the LiveKit browser SDK (`livekit-client`): `room.connect(livekit_url, token)`, publish the
   microphone, play the agent's audio track. The token admits this one room only; ask for a new one if it
   expires (30 minutes). Refused with `422`:
   * `reason: "text_only"`: the language has no live voice (Sinhala) or voice is not configured: use chat;
   * `reason: "no_voice_consent"`: the session started without *Enter the factory*: start a new session with
     `voice_consent: true` after the visitor presses it.
4. Identify: `POST /v1/public/sessions/{id}/identify` `{phone?, email?, name?}` -> `{ok: true}` always (it does
   not reveal whether the buyer was known). `422 reason: "invalid"` = the phone or email does not look right.

When the visitor says "stop" in voice, the agent stops talking and listens (§8.1: on voice "stop" means
"wait"); nothing for the site to do.

---

## 4. Limits and failures (D34, D39)

| Status | When | What to show |
|---|---|---|
| `429` | Too many requests from this IP (30 a minute) or session (12 a minute) | "Please slow down" and retry after a few seconds |
| `503` | The daily AI budget is used up, or the assistant is unavailable | Offer **WhatsApp click-to-chat** (the documented last resort) |
| `401` | Session token missing or expired | Start a new session |
| `403` | Turnstile failed, or the token belongs to another session | Reload the widget |
| `422` | See the `reason` values above; field errors come as `{"detail": [...]}` | |

Business errors use `application/problem+json` (`type`, `title`, `status`, `detail`, `reason`), the same as
the CRM API.

---
