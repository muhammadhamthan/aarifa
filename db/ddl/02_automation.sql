-- =====================================================================
-- Arifaa Platform — AUTOMATION database (owner: core-automation, role automation_app)
-- Channels (WhatsApp, email, voice/IVR), inbound/outbound messages,
-- webhook dedupe, calls, price scraping runs. Job queue tables are created
-- by procrastinate (`procrastinate schema --apply`) in this same database.
-- References to CRM rows are plain uuids (no cross-database FKs).
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS citext;
CREATE SCHEMA IF NOT EXISTS automation;
SET search_path = automation, public;

-- One row per connected sending identity (WABA phone, SES identity, Twilio number)
CREATE TABLE channel_accounts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel             text NOT NULL CHECK (channel IN ('whatsapp','email','voice','sms')),
    provider            text NOT NULL CHECK (provider IN ('meta_cloud','ses','postmark','twilio','livekit_sip','telnyx')),
    external_account_id text NOT NULL,      -- phone_number_id / SES identity / Twilio number SID
    display_name        text NOT NULL,
    address             text NOT NULL,      -- +9715..., sales@arifapack.com
    purpose             text NOT NULL CHECK (purpose IN ('customer','internal_alerts')),
    secret_ref          text NOT NULL,      -- Secrets Manager ARN (never the secret)
    status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','pending_verification')),
    config              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, external_account_id)
);

CREATE TABLE message_templates (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel       text NOT NULL CHECK (channel IN ('whatsapp','email')),
    name          text NOT NULL,               -- quote_ready, followup_1, approval_request ...
    language      text NOT NULL DEFAULT 'en',
    category      text NOT NULL CHECK (category IN ('utility','marketing','authentication','internal')),
    provider_status text NOT NULL DEFAULT 'draft' CHECK (provider_status IN ('draft','submitted','approved','rejected','paused')),
    subject       text,                        -- email only
    body          text NOT NULL,               -- with {{1}} / {{name}} placeholders
    variables     text[] NOT NULL DEFAULT ARRAY[]::text[],
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel, name, language)
);

-- A conversation thread on one channel with one external party
CREATE TABLE threads (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_account_id uuid NOT NULL REFERENCES channel_accounts(id),
    external_key       text NOT NULL,          -- wa_id, email address, phone number
    contact_ref        uuid,                   -- crm.contacts.id
    deal_ref           uuid,                   -- crm.deals.id (current)
    last_inbound_at    timestamptz,            -- drives the WhatsApp 24-hour window
    human_owner_ref    uuid,                   -- crm.users.id while a human has taken over
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel_account_id, external_key)
);
CREATE INDEX threads_contact_idx ON threads (contact_ref);

CREATE TABLE inbound_messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id           uuid NOT NULL REFERENCES threads(id),
    provider            text NOT NULL,
    provider_message_id text NOT NULL,
    kind                text NOT NULL CHECK (kind IN ('text','audio','image','document','button','interactive','email','call','reaction','other')),
    text                text,
    media_s3_key        text,
    language            text,
    raw                 jsonb NOT NULL,
    received_at         timestamptz NOT NULL DEFAULT now(),
    processed_at        timestamptz,
    UNIQUE (provider, provider_message_id)
);
CREATE INDEX inbound_unprocessed_idx ON inbound_messages (received_at) WHERE processed_at IS NULL;

CREATE TABLE outbound_messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id           uuid REFERENCES threads(id),
    channel_account_id  uuid NOT NULL REFERENCES channel_accounts(id),
    to_address          text NOT NULL,
    kind                text NOT NULL CHECK (kind IN
                        ('quote','final_quote','counter_offer','reply','followup','summary_pdf',
                         'approval_alert','md_notice','order_update','reorder','notification')),
    template_id         uuid REFERENCES message_templates(id),
    subject             text,
    body                text,
    media_s3_key        text,
    content_sha256      char(64),
    grant_ref           uuid UNIQUE,            -- crm.quote_send_grants.id (one message per grant)
    deal_ref            uuid,
    requested_by        text NOT NULL,           -- ai role, user id, system job
    idempotency_key     text NOT NULL UNIQUE,
    status              text NOT NULL DEFAULT 'queued' CHECK (status IN
                        ('queued','blocked','sending','sent','delivered','read','failed','cancelled')),
    block_reason        text,                    -- no_grant, outside_hours, no_consent, opt_out, window_closed
    provider_message_id text,
    error               text,
    not_before          timestamptz,             -- contact-hours scheduling
    created_at          timestamptz NOT NULL DEFAULT now(),
    sent_at             timestamptz,
    delivered_at        timestamptz,
    read_at             timestamptz,
    -- priced messages can never exist without a grant reference
    CHECK (kind NOT IN ('quote','final_quote','counter_offer') OR grant_ref IS NOT NULL),
    CHECK (kind NOT IN ('quote','final_quote','counter_offer') OR content_sha256 IS NOT NULL)
);
CREATE INDEX outbound_status_idx ON outbound_messages (status, not_before);
CREATE INDEX outbound_deal_idx   ON outbound_messages (deal_ref, created_at DESC);

CREATE TABLE webhook_receipts (
    provider      text NOT NULL,
    external_id   text NOT NULL,         -- message id / event id / CallSid
    signature_ok  boolean NOT NULL,
    received_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, external_id)
);

-- Fast local suppression list mirrored from CRM consents (opt-outs, STOP)
CREATE TABLE suppressions (
    channel       text NOT NULL,
    address_hash  char(64) NOT NULL,
    reason        text NOT NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, address_hash)
);

CREATE TABLE call_attempts (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose            text NOT NULL CHECK (purpose IN ('approval_alert','below_range_alert','customer_callback','inbound')),
    approval_ref       uuid,                    -- crm.approvals.id
    deal_ref           uuid,
    to_e164            text NOT NULL,
    provider           text NOT NULL,
    provider_call_id   text UNIQUE,
    attempt_no         smallint NOT NULL DEFAULT 1,
    status             text NOT NULL DEFAULT 'queued' CHECK (status IN
                       ('queued','ringing','answered','no_answer','busy','failed','completed')),
    digits_pressed     text,
    recording_notice_played boolean NOT NULL DEFAULT false,
    recording_s3_key   text,
    started_at         timestamptz,
    ended_at           timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX call_attempts_approval_idx ON call_attempts (approval_ref, attempt_no);

-- Price scraping (results become proposals in CRM, never prices directly)
CREATE TABLE scrape_runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_code   text NOT NULL,          -- crm.price_sources.code
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','failed','blocked')),
    items         integer NOT NULL DEFAULT 0,
    error         text
);
CREATE TABLE price_observations (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         uuid NOT NULL REFERENCES scrape_runs(id),
    source_code    text NOT NULL,
    material_code  text NOT NULL,
    value          numeric(14,4) NOT NULL CHECK (value > 0),
    currency       char(3) NOT NULL,
    unit           text NOT NULL,          -- per tonne, per kg
    observed_for   date NOT NULL,          -- the date the source states
    source_url     text,
    raw            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_code, material_code, observed_for)
);

CREATE TABLE outbox_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      text NOT NULL,          -- versioned name, e.g. quote.approved.v1
    event_version   smallint NOT NULL DEFAULT 1,
    correlation_id  uuid,                   -- ties a chain of events to one buyer request
    sequence        bigint GENERATED ALWAYS AS IDENTITY,  -- publish order
    aggregate_type  text NOT NULL,
    aggregate_id    text NOT NULL,
    payload         jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz,
    attempts        smallint NOT NULL DEFAULT 0
);
CREATE INDEX outbox_unpublished_idx ON outbox_events (created_at) WHERE published_at IS NULL;
CREATE TABLE inbox_events (
    event_id      uuid NOT NULL,
    handler       text NOT NULL,
    processed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, handler)
);
