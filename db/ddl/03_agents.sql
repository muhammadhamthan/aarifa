-- =====================================================================
-- Arifaa Platform — AGENTS database (owner: agent-framework, role agents_app)
-- Conversations and transcripts, agent runs and tool calls (observability
-- mirror of Langfuse), guardrail events, knowledge base (pgvector), news
-- and lane-risk assessments, eval runs. LangGraph's own checkpoint and
-- store tables are created by PostgresSaver.setup() / PostgresStore.setup().
-- The agents role has NO access to the crm database (floors, costs, margins).
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS agents;
SET search_path = agents, public;

-- Anonymous website visitors (cookie id), merged into a contact once known
CREATE TABLE visitors (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    cookie_consent  boolean NOT NULL DEFAULT false,
    country_code    char(2),
    language        text,
    contact_ref     uuid,                       -- crm.contacts.id after identification
    expires_at      timestamptz NOT NULL DEFAULT now() + interval '90 days'
);

CREATE TABLE conversations (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel            text NOT NULL CHECK (channel IN ('web_voice','web_chat','whatsapp','email','phone')),
    langgraph_thread_id text NOT NULL UNIQUE,   -- checkpointer thread
    visitor_id         uuid REFERENCES visitors(id),
    contact_ref        uuid,
    deal_ref           uuid,
    language           text,
    started_at         timestamptz NOT NULL DEFAULT now(),
    last_activity_at   timestamptz NOT NULL DEFAULT now(),
    ended_at           timestamptz,
    summary            text,                    -- rolling summary pushed to CRM timeline
    recording_consent  boolean NOT NULL DEFAULT false
);
CREATE INDEX conversations_contact_idx ON conversations (contact_ref, last_activity_at DESC);
CREATE INDEX conversations_deal_idx    ON conversations (deal_ref);

CREATE TABLE turns (
    id               bigserial PRIMARY KEY,
    conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_no          integer NOT NULL,
    speaker          text NOT NULL CHECK (speaker IN ('buyer','ai','human','system')),
    ai_role          text,                       -- concierge, product, negotiation ...
    text             text NOT NULL,
    language         text,
    audio_s3_key     text,                       -- deleted after 90 days (retention job)
    stt_confidence   real,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, turn_no)
);

CREATE TABLE agent_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid REFERENCES conversations(id),
    deal_ref        uuid,
    graph           text NOT NULL CHECK (graph IN ('conversation','quotation','deal','digest','eval')),
    role            text NOT NULL,               -- one of the 12 AI roles
    trigger_event   text NOT NULL,               -- message.received.v1, deal.qualified.v1 ...
    trigger_event_id uuid,
    model           text NOT NULL,
    prompt_version  text NOT NULL,
    input_tokens    integer NOT NULL DEFAULT 0,
    output_tokens   integer NOT NULL DEFAULT 0,
    cached_tokens   integer NOT NULL DEFAULT 0,
    cost_usd        numeric(12,6) NOT NULL DEFAULT 0,
    latency_ms      integer,
    status          text NOT NULL CHECK (status IN ('running','ok','failed','blocked','handoff')),
    error           text,
    langfuse_trace_id text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    UNIQUE (trigger_event_id, graph)             -- one run per event per graph (idempotent)
);
CREATE INDEX agent_runs_deal_idx ON agent_runs (deal_ref, started_at DESC);
CREATE INDEX agent_runs_day_idx  ON agent_runs (started_at);

CREATE TABLE tool_calls (
    id           bigserial PRIMARY KEY,
    run_id       uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    tool         text NOT NULL,
    args         jsonb NOT NULL,               -- PII-redacted
    result_summary text,
    status       text NOT NULL CHECK (status IN ('ok','error','denied')),
    duration_ms  integer,
    at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE guard_events (
    id        bigserial PRIMARY KEY,
    run_id    uuid REFERENCES agent_runs(id) ON DELETE CASCADE,
    guard     text NOT NULL CHECK (guard IN
              ('price_number','citation','injection','disclosure','pii','commitment','language','cost_leak')),
    action    text NOT NULL CHECK (action IN ('blocked','rewritten','flagged')),
    detail    jsonb NOT NULL DEFAULT '{}'::jsonb,
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX guard_events_day_idx ON guard_events (at, guard);

-- Questions the AI could not answer from approved content -> CRM task
CREATE TABLE knowledge_gaps (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  uuid REFERENCES conversations(id),
    question         text NOT NULL,
    language         text,
    status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','ignored')),
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------- knowledge base (approved CMS content only) ----------------
CREATE TABLE kb_documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cms_collection  text NOT NULL,          -- products, machines, faqs, certifications, zones, pages
    cms_id          text NOT NULL,
    cms_version     text NOT NULL,
    title           text NOT NULL,
    language        text NOT NULL DEFAULT 'en',
    url_path        text,                   -- for citations and navigation
    content_sha256  char(64) NOT NULL,
    approved_at     timestamptz NOT NULL,
    indexed_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (cms_collection, cms_id, language)
);
CREATE TABLE kb_chunks (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id      uuid NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    chunk_no         integer NOT NULL,
    text             text NOT NULL,
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,   -- category, sku, zone, heading
    embedding        vector(1024) NOT NULL,
    embedding_model  text NOT NULL,
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    UNIQUE (document_id, chunk_no)
);
CREATE INDEX kb_chunks_embedding_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX kb_chunks_tsv_gin        ON kb_chunks USING gin (tsv);
CREATE INDEX kb_chunks_trgm_gin       ON kb_chunks USING gin (text gin_trgm_ops);
CREATE INDEX kb_chunks_meta_gin       ON kb_chunks USING gin (metadata jsonb_path_ops);

-- ---------- shipping-risk news digest ---------------------------------
CREATE TABLE news_items (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source        text NOT NULL,          -- gdelt, carrier_advisory, ukmto, event_registry
    url           text NOT NULL UNIQUE,
    title         text NOT NULL,
    published_at  timestamptz,
    lanes         text[] NOT NULL DEFAULT ARRAY[]::text[],   -- crm.freight_lanes.code
    summary       text,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX news_items_lanes_gin ON news_items USING gin (lanes);
CREATE TABLE lane_risk_assessments (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lane_code   text NOT NULL,
    for_date    date NOT NULL,
    score       smallint NOT NULL CHECK (score BETWEEN 0 AND 3),
    summary     text NOT NULL,
    news_ids    uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
    model       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (lane_code, for_date)
);

CREATE TABLE eval_runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    suite         text NOT NULL,          -- golden, redteam, voice
    git_sha       text NOT NULL,
    passed        integer NOT NULL,
    failed        integer NOT NULL,
    report_s3_key text,
    at            timestamptz NOT NULL DEFAULT now()
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
