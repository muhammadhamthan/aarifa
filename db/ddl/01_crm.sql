-- =====================================================================
-- Arifaa Platform — CRM database  (owner: core-crm, DB role crm_app)
-- Source of truth for customers, deals, pricing, quotes, approvals,
-- send grants, stock, orders, payments, audit and reporting.
-- PostgreSQL 16+ (RDS target 18).  Conventions:
--   * uuid PKs (app generates UUIDv7; default gen_random_uuid() for tools)
--   * timestamptz everywhere, stored UTC
--   * money = numeric(14,4) + ISO currency; never float
--   * closed sets = text + CHECK (easier to migrate than PG enums)
--   * every mutable table has created_at / updated_at (trigger)
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS crm;
SET search_path = crm, public;

-- ---------- helpers ---------------------------------------------------
CREATE OR REPLACE FUNCTION crm.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- ---------- people & settings ----------------------------------------
CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cognito_sub   text UNIQUE,
    email         citext NOT NULL UNIQUE,
    full_name     text NOT NULL,
    phone_e164    text CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
    role          text NOT NULL CHECK (role IN ('md','manager','admin','sales')),
    is_active     boolean NOT NULL DEFAULT true,
    alert_channels text[] NOT NULL DEFAULT ARRAY['whatsapp','email','in_app'],
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- MD-editable business settings (approval window, limits, cadence ...)
CREATE TABLE settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    description text NOT NULL,
    updated_by  uuid REFERENCES users(id),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE settings_history (
    id          bigserial PRIMARY KEY,
    key         text NOT NULL,
    old_value   jsonb,
    new_value   jsonb NOT NULL,
    changed_by  uuid REFERENCES users(id),
    changed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE feature_flags (
    key        text PRIMARY KEY,           -- ai_enabled, ai_outbound_calls, indicative_price_range ...
    enabled    boolean NOT NULL,
    updated_by uuid REFERENCES users(id),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------- customers -------------------------------------------------
CREATE TABLE companies (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text NOT NULL,
    country_code   char(2),
    city           text,
    customer_type  text CHECK (customer_type IN
                   ('distributor','restaurant_chain','caterer','airline','retailer','manufacturer','other')),
    vat_trn        text,
    website        text,
    notes          text,
    anonymised_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX companies_name_trgm ON companies USING gin (name gin_trgm_ops);

CREATE TABLE contacts (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id         uuid REFERENCES companies(id),
    full_name          text,
    designation        text,
    phone_e164         text CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
    whatsapp_e164      text CHECK (whatsapp_e164 ~ '^\+[1-9][0-9]{6,14}$'),
    email              citext,
    preferred_channel  text CHECK (preferred_channel IN ('whatsapp','email','phone')),
    language           text NOT NULL DEFAULT 'en',      -- BCP-47: en, ar, hi, ta, si, zh
    timezone           text NOT NULL DEFAULT 'Asia/Dubai', -- buyer local time for contact hours
    phone_verified_at  timestamptz,                      -- set when WhatsApp summary is delivered
    source             text NOT NULL CHECK (source IN
                       ('web_voice','web_chat','whatsapp','email','manual','import','form')),
    visitor_ref        uuid,                             -- agents.visitors.id (no cross-DB FK)
    anonymised_at      timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
-- same person = same phone or same email (auto-merge key)
CREATE UNIQUE INDEX contacts_phone_uq ON contacts (phone_e164) WHERE phone_e164 IS NOT NULL;
CREATE UNIQUE INDEX contacts_email_uq ON contacts (email)      WHERE email IS NOT NULL;
CREATE INDEX contacts_company_idx ON contacts (company_id);

CREATE TABLE consents (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id    uuid NOT NULL REFERENCES contacts(id),
    channel       text NOT NULL CHECK (channel IN ('whatsapp','email','call','recording','marketing','cookies')),
    status        text NOT NULL CHECK (status IN ('granted','revoked')),
    wording       text NOT NULL,            -- exact text the buyer saw / heard
    evidence      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- page, conversation id, ip hash
    captured_via  text NOT NULL,            -- web_chat, web_voice, whatsapp, email, manual
    at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX consents_contact_channel_idx ON consents (contact_id, channel, at DESC);

-- ---------- catalogue & costing (content lives in CMS) ---------------
CREATE TABLE product_categories (
    code  text PRIMARY KEY,                -- e.g. ALU_CONTAINER, PLASTIC_CUTLERY
    name  text NOT NULL
);
CREATE TABLE materials (
    code      text PRIMARY KEY,            -- AL_COIL_8011, PP, PS, PET, HIPS
    name      text NOT NULL,
    unit      text NOT NULL DEFAULT 'kg',
    currency  char(3) NOT NULL DEFAULT 'AED'
);
CREATE TABLE products (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sku                     text NOT NULL UNIQUE,
    cms_product_id          text,           -- Payload id (no cross-DB FK)
    name                    text NOT NULL,
    category_code           text NOT NULL REFERENCES product_categories(code),
    unit                    text NOT NULL DEFAULT 'pcs',
    moq                     integer CHECK (moq > 0),
    carton_qty              integer CHECK (carton_qty > 0),
    is_active               boolean NOT NULL DEFAULT true,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
-- cost recipe per product (MD/manager maintain; versioned by effective_from)
CREATE TABLE product_costings (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id                uuid NOT NULL REFERENCES products(id),
    material_code             text NOT NULL REFERENCES materials(code),
    kg_per_1000               numeric(12,4) NOT NULL CHECK (kg_per_1000 > 0),
    yield_pct                 numeric(5,2)  NOT NULL CHECK (yield_pct > 0 AND yield_pct <= 100),
    production_cost_per_1000  numeric(14,4) NOT NULL CHECK (production_cost_per_1000 >= 0),
    packaging_cost_per_1000   numeric(14,4) NOT NULL CHECK (packaging_cost_per_1000 >= 0),
    pcs_per_40hc              integer CHECK (pcs_per_40hc > 0),   -- freight share per piece
    pieces_per_day            integer NOT NULL CHECK (pieces_per_day > 0),
    packing_days              smallint NOT NULL DEFAULT 1,
    currency                  char(3) NOT NULL DEFAULT 'AED',
    effective_from            timestamptz NOT NULL DEFAULT now(),
    updated_by                uuid REFERENCES users(id),
    created_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX product_costings_current_idx ON product_costings (product_id, effective_from DESC);

-- raw-material rates: proposed by scraper/supplier, approved by manager
CREATE TABLE material_rates (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    material_code  text NOT NULL REFERENCES materials(code),
    rate_per_unit  numeric(14,4) NOT NULL CHECK (rate_per_unit > 0),
    currency       char(3) NOT NULL DEFAULT 'AED',
    source         text NOT NULL CHECK (source IN ('manual','scrape','supplier_quote','erp')),
    source_ref     text,                 -- price source code or supplier name
    source_url     text,
    observation_ref uuid,                -- automation.price_observations.id
    change_pct     numeric(7,3),         -- vs current approved rate
    status         text NOT NULL CHECK (status IN ('proposed','approved','rejected','superseded')),
    proposed_at    timestamptz NOT NULL DEFAULT now(),
    decided_by     uuid REFERENCES users(id),
    decided_at     timestamptz,
    effective_at   timestamptz,          -- set on approval
    note           text,
    CHECK (status <> 'approved' OR (decided_by IS NOT NULL AND effective_at IS NOT NULL))
);
CREATE INDEX material_rates_current_idx ON material_rates (material_code, effective_at DESC) WHERE status = 'approved';
CREATE INDEX material_rates_pending_idx ON material_rates (proposed_at) WHERE status = 'proposed';

CREATE TABLE price_sources (
    code          text PRIMARY KEY,      -- lme_aluminium, uae_official_x, manual_upload
    name          text NOT NULL,
    url           text,
    material_code text REFERENCES materials(code),
    enabled       boolean NOT NULL DEFAULT true,
    terms_ok      boolean NOT NULL DEFAULT false,   -- automated reading permitted
    last_run_at   timestamptz,
    last_status   text
);

CREATE TABLE supplier_lead_times (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    material_code  text NOT NULL REFERENCES materials(code),
    supplier_name  text NOT NULL,
    lead_time_days smallint NOT NULL CHECK (lead_time_days >= 0),
    updated_by     uuid REFERENCES users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (material_code, supplier_name)
);

CREATE TABLE fx_rates (
    currency     char(3) NOT NULL,
    rate_to_aed  numeric(18,8) NOT NULL CHECK (rate_to_aed > 0),
    as_of        date NOT NULL,
    source       text NOT NULL,
    PRIMARY KEY (currency, as_of)
);

-- shipping lanes from the factory, with war-risk buffers
CREATE TABLE freight_lanes (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code               text NOT NULL UNIQUE,         -- JEA-CMB-SEA
    origin_port        text NOT NULL,                -- Jebel Ali / Khorfakkan
    dest_country       char(2) NOT NULL,
    dest_port          text,
    mode               text NOT NULL CHECK (mode IN ('sea','air','road')),
    container_cost     numeric(14,4),                -- per 40HC (sea/road)
    surcharge          numeric(14,4) NOT NULL DEFAULT 0,  -- war-risk / peak
    currency           char(3) NOT NULL DEFAULT 'USD',
    transit_days       smallint NOT NULL CHECK (transit_days >= 0),
    risk_score         smallint NOT NULL DEFAULT 0 CHECK (risk_score BETWEEN 0 AND 3),
    buffer_pct         numeric(5,2) NOT NULL DEFAULT 0,   -- added to freight cost
    buffer_days        smallint NOT NULL DEFAULT 0,       -- added to delivery estimate
    rate_source        text,
    rate_updated_at    timestamptz,
    risk_updated_at    timestamptz,
    is_active          boolean NOT NULL DEFAULT true,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE lane_risk_history (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lane_id     uuid NOT NULL REFERENCES freight_lanes(id),
    score       smallint NOT NULL CHECK (score BETWEEN 0 AND 3),
    summary     text NOT NULL,
    sources     jsonb NOT NULL DEFAULT '[]'::jsonb,
    assessment_ref uuid,                 -- agents.lane_risk_assessments.id
    status      text NOT NULL CHECK (status IN ('proposed','applied','dismissed')),
    decided_by  uuid REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- markup range per category, optional overrides (customer type, qty band)
CREATE TABLE margin_rules (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    category_code   text NOT NULL REFERENCES product_categories(code),
    customer_type   text,                 -- NULL = any
    min_qty         numeric(14,2),        -- NULL = no lower bound
    max_qty         numeric(14,2),
    markup_min_pct  numeric(6,2) NOT NULL CHECK (markup_min_pct >= 0),
    markup_max_pct  numeric(6,2) NOT NULL,
    max_concessions smallint NOT NULL DEFAULT 3 CHECK (max_concessions BETWEEN 0 AND 10),
    priority        smallint NOT NULL DEFAULT 100,   -- lower wins
    is_active       boolean NOT NULL DEFAULT true,
    updated_by      uuid REFERENCES users(id),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (markup_max_pct >= markup_min_pct),
    CHECK (max_qty IS NULL OR min_qty IS NULL OR max_qty >= min_qty)
);
CREATE INDEX margin_rules_lookup_idx ON margin_rules (category_code, priority) WHERE is_active;

-- ---------- deals -----------------------------------------------------
CREATE SEQUENCE deal_number_seq;
CREATE SEQUENCE quote_number_seq;
CREATE SEQUENCE order_number_seq;

CREATE TABLE deals (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number              text NOT NULL UNIQUE DEFAULT
                        ('D-' || to_char(now(),'YYYY') || '-' || lpad(nextval('crm.deal_number_seq')::text, 5, '0')),
    company_id          uuid REFERENCES companies(id),
    primary_contact_id  uuid NOT NULL REFERENCES contacts(id),
    stage               text NOT NULL DEFAULT 'NEW' CHECK (stage IN
                        ('NEW','QUALIFIED','QUOTATION','NEGOTIATION','READY_TO_CLOSE','PAYMENT','WON','LOST')),
    owner_user_id       uuid REFERENCES users(id),
    ai_enabled          boolean NOT NULL DEFAULT true,     -- AI on/off per deal
    ai_paused_reason    text CHECK (ai_paused_reason IN
                        ('human_takeover','below_range','exception','md_takeover','opt_out')),
    ai_paused_at        timestamptz,
    source              text NOT NULL,
    currency            char(3) NOT NULL DEFAULT 'AED',
    concessions_used    smallint NOT NULL DEFAULT 0,
    lost_reason         text CHECK (lost_reason IN
                        ('price_too_high','delivery_not_suitable','bought_competitor','product_unavailable','no_response_postponed')),
    lost_note           text,
    qualified_at        timestamptz,
    won_at              timestamptz,
    lost_at             timestamptz,
    next_action_at      timestamptz,
    next_action_kind    text,
    next_action_reason  text,
    version             integer NOT NULL DEFAULT 1,        -- optimistic locking
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (stage <> 'LOST' OR lost_reason IS NOT NULL),
    CHECK ((ai_enabled AND ai_paused_reason IS NULL) OR (NOT ai_enabled))
);
CREATE INDEX deals_stage_idx       ON deals (stage, updated_at DESC);
CREATE INDEX deals_owner_idx       ON deals (owner_user_id, stage);
CREATE INDEX deals_next_action_idx ON deals (next_action_at) WHERE next_action_at IS NOT NULL;
CREATE INDEX deals_contact_idx     ON deals (primary_contact_id);

CREATE TABLE deal_requirements (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id         uuid NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    product_id      uuid REFERENCES products(id),
    product_text    text,                 -- what the buyer said, before matching
    spec            jsonb NOT NULL DEFAULT '{}'::jsonb,   -- size, capacity, lid, printing
    quantity        numeric(14,2) CHECK (quantity > 0),
    unit            text NOT NULL DEFAULT 'pcs',
    application     text,
    delivery_country char(2),
    delivery_city   text,
    delivery_port   text,
    needed_by       date,
    incoterm        text CHECK (incoterm IN ('EXW','FCA','FOB','CFR','CIF','CPT','CIP','DAP','DDP')),
    captured_by     text NOT NULL,        -- ai role or user id
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deal_requirements_deal_idx ON deal_requirements (deal_id);

CREATE TABLE stage_history (
    id            bigserial PRIMARY KEY,
    deal_id       uuid NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    from_stage    text,
    to_stage      text NOT NULL,
    actor_type    text NOT NULL CHECK (actor_type IN ('user','ai','system')),
    actor_id      text NOT NULL,          -- user uuid or AI role name
    reason        text,
    at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX stage_history_deal_idx ON stage_history (deal_id, at);

CREATE TABLE timeline_events (
    id          bigserial PRIMARY KEY,
    deal_id     uuid REFERENCES deals(id) ON DELETE CASCADE,
    contact_id  uuid REFERENCES contacts(id),
    kind        text NOT NULL,            -- message_in, message_out, call, note, quote_sent, approval ...
    channel     text,
    direction   text CHECK (direction IN ('in','out','internal')),
    summary     text NOT NULL,
    ref_type    text,                     -- outbound_message, conversation, quote_version ...
    ref_id      text,
    actor_type  text NOT NULL CHECK (actor_type IN ('user','ai','system','buyer')),
    actor_id    text,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX timeline_deal_idx    ON timeline_events (deal_id, at DESC);
CREATE INDEX timeline_contact_idx ON timeline_events (contact_id, at DESC);

CREATE TABLE tasks (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id           uuid REFERENCES deals(id) ON DELETE CASCADE,
    kind              text NOT NULL CHECK (kind IN
                      ('below_range_review','delivery_confirm','price_review','factory_visit',
                       'callback','knowledge_gap','manual_followup','lane_risk_review')),
    assigned_user_id  uuid REFERENCES users(id),
    due_at            timestamptz,
    status            text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
    payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    done_at           timestamptz,
    done_by           uuid REFERENCES users(id)
);
CREATE INDEX tasks_open_idx ON tasks (assigned_user_id, due_at) WHERE status = 'open';

-- ---------- quotes, approvals, grants ---------------------------------
CREATE TABLE quotes (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id             uuid NOT NULL REFERENCES deals(id),
    number              text NOT NULL UNIQUE DEFAULT
                        ('Q-' || to_char(now(),'YYYY') || '-' || lpad(nextval('crm.quote_number_seq')::text, 5, '0')),
    current_version_id  uuid,             -- FK added after quote_versions
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX quotes_deal_idx ON quotes (deal_id);

CREATE TABLE delivery_estimates (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id             uuid NOT NULL REFERENCES deals(id),
    lane_id             uuid REFERENCES freight_lanes(id),
    backlog_days        smallint NOT NULL,
    production_days     smallint NOT NULL,
    material_wait_days  smallint NOT NULL,
    packing_days        smallint NOT NULL,
    transit_days        smallint NOT NULL,
    buffer_days         smallint NOT NULL,
    estimated_ready     date NOT NULL,
    estimated_delivery  date NOT NULL,
    inputs              jsonb NOT NULL,   -- full feature snapshot = training row (T14)
    confirmed_by        uuid REFERENCES users(id),
    confirmed_date      date,             -- manager's edited/confirmed date
    confirmed_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE quote_versions (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    quote_id             uuid NOT NULL REFERENCES quotes(id),
    version_no           integer NOT NULL CHECK (version_no > 0),
    kind                 text NOT NULL CHECK (kind IN ('initial','revision','counter','final')),
    status               text NOT NULL DEFAULT 'draft' CHECK (status IN
                         ('draft','pending_approval','overdue','approved','sent','correction_requested',
                          'rejected','superseded','expired')),
    currency             char(3) NOT NULL,
    fx_rate_to_aed       numeric(18,8) NOT NULL DEFAULT 1,
    subtotal             numeric(14,4) NOT NULL CHECK (subtotal >= 0),
    vat_rate             numeric(5,2)  NOT NULL DEFAULT 0,
    vat_amount           numeric(14,4) NOT NULL DEFAULT 0,
    total                numeric(14,4) NOT NULL CHECK (total >= 0),
    total_aed            numeric(14,4) NOT NULL,
    incoterm             text NOT NULL DEFAULT 'EXW',
    freight_lane_id      uuid REFERENCES freight_lanes(id),
    validity_days        smallint NOT NULL CHECK (validity_days BETWEEN 1 AND 60),
    valid_until          date,
    clauses              text[] NOT NULL DEFAULT ARRAY[]::text[],
    assumptions          text,
    approver_summary     text,            -- AI-written summary for the approver
    delivery_estimate_id uuid REFERENCES delivery_estimates(id),
    created_by_type      text NOT NULL CHECK (created_by_type IN ('user','ai')),
    created_by           text NOT NULL,
    correction_note      text,            -- manager's note that produced this version
    pdf_s3_key           text,
    content_sha256       char(64),        -- hash of the frozen PDF; set at approval
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (quote_id, version_no)
);
ALTER TABLE quotes ADD CONSTRAINT quotes_current_version_fk
    FOREIGN KEY (current_version_id) REFERENCES quote_versions(id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX quote_versions_status_idx ON quote_versions (status, created_at);

CREATE TABLE quote_lines (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    quote_version_id   uuid NOT NULL REFERENCES quote_versions(id) ON DELETE CASCADE,
    line_no            smallint NOT NULL,
    product_id         uuid REFERENCES products(id),
    description        text NOT NULL,
    quantity           numeric(14,2) NOT NULL CHECK (quantity > 0),
    unit               text NOT NULL DEFAULT 'pcs',
    unit_price         numeric(14,6) NOT NULL CHECK (unit_price >= 0),
    line_total         numeric(14,4) NOT NULL CHECK (line_total >= 0),
    UNIQUE (quote_version_id, line_no)
);
-- MD-only cost snapshot per line (exact rates used) — separate table so the
-- API can grant it by role and never leak it in buyer-facing payloads
CREATE TABLE quote_line_costs (
    quote_line_id        uuid PRIMARY KEY REFERENCES quote_lines(id) ON DELETE CASCADE,
    material_rate_id     uuid REFERENCES material_rates(id),
    material_rate        numeric(14,4) NOT NULL,
    kg_per_1000          numeric(12,4) NOT NULL,
    yield_pct            numeric(5,2)  NOT NULL,
    production_per_1000  numeric(14,4) NOT NULL,
    packaging_per_1000   numeric(14,4) NOT NULL,
    freight_per_1000     numeric(14,4) NOT NULL DEFAULT 0,
    unit_cost            numeric(14,6) NOT NULL,
    markup_pct           numeric(6,2)  NOT NULL,
    margin_rule_id       uuid REFERENCES margin_rules(id),
    markup_min_pct       numeric(6,2)  NOT NULL,
    markup_max_pct       numeric(6,2)  NOT NULL
);

-- Price fields of a version are frozen once it leaves 'draft'
CREATE OR REPLACE FUNCTION crm.quote_version_freeze() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'draft' AND (
       NEW.subtotal <> OLD.subtotal OR NEW.total <> OLD.total OR NEW.currency <> OLD.currency
    OR NEW.vat_amount <> OLD.vat_amount OR NEW.incoterm <> OLD.incoterm
    OR NEW.validity_days <> OLD.validity_days
    OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256 AND OLD.content_sha256 IS NOT NULL) THEN
    RAISE EXCEPTION 'quote version % is frozen (status %): create a new version', OLD.id, OLD.status
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER quote_versions_freeze BEFORE UPDATE ON quote_versions
    FOR EACH ROW EXECUTE FUNCTION crm.quote_version_freeze();

CREATE OR REPLACE FUNCTION crm.quote_lines_freeze() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE st text;
BEGIN
  SELECT status INTO st FROM crm.quote_versions
   WHERE id = COALESCE(NEW.quote_version_id, OLD.quote_version_id);
  IF st <> 'draft' THEN
    RAISE EXCEPTION 'quote lines are frozen once the version leaves draft'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN COALESCE(NEW, OLD);
END $$;
CREATE TRIGGER quote_lines_freeze BEFORE INSERT OR UPDATE OR DELETE ON quote_lines
    FOR EACH ROW EXECUTE FUNCTION crm.quote_lines_freeze();

CREATE TABLE approvals (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    quote_version_id    uuid NOT NULL REFERENCES quote_versions(id),
    kind                text NOT NULL CHECK (kind IN ('initial','final','below_range')),
    required_role       text NOT NULL CHECK (required_role IN ('manager','md')),
    assigned_user_id    uuid REFERENCES users(id),
    status              text NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending','overdue','approved','rejected','correction_requested','cancelled')),
    window_minutes      integer NOT NULL CHECK (window_minutes > 0),
    submitted_at        timestamptz NOT NULL DEFAULT now(),
    deadline_at         timestamptz NOT NULL,
    escalation_step     smallint NOT NULL DEFAULT 0,   -- 0 = none, 1..3 calls, 4 = MD
    escalated_to_md_at  timestamptz,
    decided_by          uuid REFERENCES users(id),
    decided_at          timestamptz,
    decision_note       text,
    CHECK (status IN ('pending','overdue','cancelled') OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
    CHECK (kind <> 'final' OR required_role = 'md')          -- T2: MD confirms the final quote
);
-- one open approval per version
CREATE UNIQUE INDEX approvals_open_uq ON approvals (quote_version_id)
    WHERE status IN ('pending','overdue');
CREATE INDEX approvals_due_idx ON approvals (deadline_at) WHERE status IN ('pending','overdue');

CREATE TABLE approval_events (
    id           bigserial PRIMARY KEY,
    approval_id  uuid NOT NULL REFERENCES approvals(id),
    kind         text NOT NULL CHECK (kind IN
                 ('alert_sent','md_notified','overdue','call_placed','call_result','escalated_to_md','link_opened','decided')),
    channel      text,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX approval_events_idx ON approval_events (approval_id, at);

-- Single-use permission to send ONE approved quote content to ONE recipient
CREATE TABLE quote_send_grants (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    quote_version_id       uuid NOT NULL REFERENCES quote_versions(id),
    approval_id            uuid REFERENCES approvals(id),     -- NULL only for within_range counters
    policy                 text NOT NULL CHECK (policy IN ('manager','md','within_range')),
    content_sha256         char(64) NOT NULL,
    channel                text NOT NULL CHECK (channel IN ('whatsapp','email')),
    recipient_hash         char(64) NOT NULL,                 -- sha256 of normalised address
    issued_at              timestamptz NOT NULL DEFAULT now(),
    expires_at             timestamptz NOT NULL,
    consumed_at            timestamptz,
    consumed_by_message_id uuid UNIQUE,                       -- automation.outbound_messages.id
    revoked_at             timestamptz,
    revoked_reason         text,
    CHECK (expires_at > issued_at),
    CHECK (consumed_at IS NULL OR consumed_by_message_id IS NOT NULL),
    CHECK (policy = 'within_range' OR approval_id IS NOT NULL)
);
CREATE INDEX grants_open_idx ON quote_send_grants (quote_version_id)
    WHERE consumed_at IS NULL AND revoked_at IS NULL;

-- Atomic, idempotent consume. Returns true exactly when this message may send.
CREATE OR REPLACE FUNCTION crm.consume_send_grant(p_grant uuid, p_sha char(64), p_message uuid,
                                                  p_recipient_hash char(64))
RETURNS boolean LANGUAGE sql AS $$
  WITH g AS (
    UPDATE crm.quote_send_grants
       SET consumed_at = COALESCE(consumed_at, now()),
           consumed_by_message_id = p_message
     WHERE id = p_grant
       AND content_sha256 = p_sha
       AND recipient_hash = p_recipient_hash
       AND revoked_at IS NULL
       AND expires_at > now()
       AND (consumed_at IS NULL OR consumed_by_message_id = p_message)
    RETURNING 1)
  SELECT EXISTS (SELECT 1 FROM g);
$$;

CREATE TABLE negotiation_rounds (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id           uuid NOT NULL REFERENCES deals(id),
    quote_version_id  uuid NOT NULL REFERENCES quote_versions(id),
    round_no          smallint NOT NULL,
    channel           text NOT NULL,
    buyer_ask_unit_price numeric(14,6),
    buyer_ask_quantity   numeric(14,2),
    verdict           text NOT NULL CHECK (verdict IN ('within_range','concession','below_range','exception','accepted')),
    offered_unit_price numeric(14,6),
    concession_no     smallint CHECK (concession_no BETWEEN 1 AND 10),
    traded_for        text,               -- quantity_increase ... (never longer delivery: T5)
    ai_reasoning      text,
    at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (deal_id, round_no),
    CHECK (traded_for IS NULL OR traded_for <> 'longer_delivery')
);

-- ---------- after the deal --------------------------------------------
CREATE TABLE payments (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id       uuid NOT NULL REFERENCES deals(id),
    confirmed_by  uuid NOT NULL REFERENCES users(id),
    confirmed_at  timestamptz NOT NULL DEFAULT now(),
    amount        numeric(14,4),          -- optional (Q16) but recorded when given
    currency      char(3),
    method        text,
    reference     text,
    paid_on       date,
    note          text
);

CREATE TABLE orders (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number                text NOT NULL UNIQUE DEFAULT
                          ('O-' || to_char(now(),'YYYY') || '-' || lpad(nextval('crm.order_number_seq')::text, 5, '0')),
    deal_id               uuid NOT NULL UNIQUE REFERENCES deals(id),
    final_quote_version_id uuid NOT NULL REFERENCES quote_versions(id),
    delivery_estimate_id  uuid REFERENCES delivery_estimates(id),
    status                text NOT NULL DEFAULT 'in_production' CHECK (status IN
                          ('in_production','ready','shipped','delivered','cancelled')),
    production_started_on date,
    ready_on              date,
    shipped_on            date,
    delivered_on          date,
    tracking_ref          text,
    source                text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','erp')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE order_status_history (
    id        bigserial PRIMARY KEY,
    order_id  uuid NOT NULL REFERENCES orders(id),
    status    text NOT NULL,
    on_date   date NOT NULL,
    by_user   uuid REFERENCES users(id),
    source    text NOT NULL DEFAULT 'manual',
    at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE stock_items (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_type          text NOT NULL CHECK (item_type IN ('raw','finished')),
    material_code      text REFERENCES materials(code),
    product_id         uuid REFERENCES products(id),
    quantity           numeric(16,3) NOT NULL DEFAULT 0,
    reserved_quantity  numeric(16,3) NOT NULL DEFAULT 0 CHECK (reserved_quantity >= 0),
    unit               text NOT NULL,
    source             text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','erp')),
    updated_by         uuid REFERENCES users(id),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CHECK ((item_type = 'raw' AND material_code IS NOT NULL AND product_id IS NULL)
        OR (item_type = 'finished' AND product_id IS NOT NULL AND material_code IS NULL))
);
CREATE UNIQUE INDEX stock_raw_uq      ON stock_items (material_code) WHERE item_type = 'raw';
CREATE UNIQUE INDEX stock_finished_uq ON stock_items (product_id)    WHERE item_type = 'finished';
CREATE TABLE stock_movements (
    id             bigserial PRIMARY KEY,
    stock_item_id  uuid NOT NULL REFERENCES stock_items(id),
    delta          numeric(16,3) NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('adjust','reserve','release','consume','receive')),
    order_id       uuid REFERENCES orders(id),
    by_user        uuid REFERENCES users(id),
    note           text,
    at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE visit_slots (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    weekday     smallint NOT NULL CHECK (weekday BETWEEN 1 AND 7),   -- ISO, 1 = Monday
    starts_at   time NOT NULL,
    ends_at     time NOT NULL,
    capacity    smallint NOT NULL DEFAULT 1,
    is_active   boolean NOT NULL DEFAULT true,
    CHECK (ends_at > starts_at)
);
CREATE TABLE factory_visits (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id       uuid REFERENCES deals(id),
    contact_id    uuid NOT NULL REFERENCES contacts(id),
    slot_id       uuid REFERENCES visit_slots(id),
    visit_at      timestamptz NOT NULL,
    status        text NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','confirmed','cancelled','done')),
    confirmed_by  uuid REFERENCES users(id),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reorder_opportunities (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id         uuid NOT NULL REFERENCES companies(id),
    product_id         uuid NOT NULL REFERENCES products(id),
    last_order_on      date NOT NULL,
    avg_interval_days  integer NOT NULL,
    due_on             date NOT NULL,
    status             text NOT NULL DEFAULT 'open' CHECK (status IN ('open','contacted','converted','dismissed')),
    deal_id            uuid REFERENCES deals(id),
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, product_id, due_on)
);

-- ---------- reporting projections (filled from events) ----------------
CREATE TABLE rpt_daily_metrics (
    day        date NOT NULL,
    metric     text NOT NULL,      -- conversations, messages_sent, ai_tokens, ai_cost_usd, handoffs ...
    dimension  text NOT NULL DEFAULT '',   -- channel / role / product
    value      numeric(18,4) NOT NULL,
    PRIMARY KEY (day, metric, dimension)
);

-- ---------- audit (append-only) & messaging plumbing -------------------
CREATE TABLE audit_events (
    id           bigserial PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    actor_type   text NOT NULL CHECK (actor_type IN ('user','ai','system')),
    actor_id     text NOT NULL,
    action       text NOT NULL,            -- quote.approved, grant.consumed, deal.stage_changed ...
    entity_type  text NOT NULL,
    entity_id    text NOT NULL,
    before       jsonb,
    after        jsonb,
    trace_id     text
);
CREATE INDEX audit_entity_idx ON audit_events (entity_type, entity_id, at);
CREATE OR REPLACE FUNCTION crm.audit_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = 'insufficient_privilege'; END $$;
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION crm.audit_append_only();

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
CREATE TABLE idempotency_keys (
    key          text PRIMARY KEY,
    scope        text NOT NULL,
    response     jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- updated_at triggers
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['users','companies','contacts','products','freight_lanes','deals',
                           'deal_requirements','orders'] LOOP
    EXECUTE format('CREATE TRIGGER %I_touch BEFORE UPDATE ON crm.%I FOR EACH ROW EXECUTE FUNCTION crm.touch_updated_at()', t, t);
  END LOOP;
END $$;

-- ---------- default settings (MD-editable) -----------------------------
INSERT INTO settings (key, value, description) VALUES
 ('approval.window_minutes',          '120',  'Approval 1 and 2 window'),
 ('approval.md_value_limit_aed',      'null', 'First quotes above this total need the MD'),
 ('approval.call_attempts',           '3',    'Scripted calls to the approver before MD escalation'),
 ('approval.call_gap_minutes',        '20',   'Gap between approver calls'),
 ('approval.alert_hours',             '{"start":"08:00","end":"22:00","tz":"Asia/Dubai"}', 'Manager alert/call hours; timer waits outside'),
 ('negotiation.below_range_reply_minutes', '60', 'Promise made to the buyer when the AI pauses'),
 ('contact.hours',                    '{"start":"09:00","end":"18:00"}', 'Buyer contact hours in buyer local time'),
 ('followup.cadence_days',            '[2,5,10]', 'Follow-ups when the buyer gave no date'),
 ('followup.max_per_week',            '2',    'Max follow-ups per buyer per week'),
 ('quote.validity_days',              '7',    'Default validity during the shipping crisis'),
 ('pricing.stale_warn_days',          '3',    'Warn when material rates are not reviewed'),
 ('pricing.stale_block_days',         '7',    'Block new quotes after this many days'),
 ('pricing.digest_time',              '"09:00"', 'Daily price-proposal digest to the manager (UAE)'),
 ('grant.ttl_hours',                  '48',   'Send grant lifetime (separate from quote validity)'),
 ('ai.assistant_name',                '"Sara"', 'Name the AI introduces itself with'),
 ('ai.max_concessions_default',       '3',    'Fallback when a margin rule does not set it'),
 ('retention.audio_days',             '90',   'Delete call/voice audio after'),
 ('retention.open_lead_chat_months',  '24',   'Delete chats of open leads after last contact'),
 ('retention.won_customer_years',     '5',    'Keep won-customer records'),
 ('retention.lost_lead_months',       '12',   'Anonymise lost leads after');

INSERT INTO feature_flags (key, enabled) VALUES
 ('ai_enabled', true), ('ai_outbound_calls_uae', false), ('indicative_price_range', false),
 ('news_digest', true), ('price_scraping', true);
