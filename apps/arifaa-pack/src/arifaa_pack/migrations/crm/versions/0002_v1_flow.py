"""v1 flow (5 Nov): salesperson approvals, price list, change requests, assignment, handover.

Revision ID: crm_0002
Revises: crm_0001
Create Date: 2026-11-05

Source: docs/V1_CHANGE_PLAN.md WP1 and docs/BUSINESS_FLOW_V1.md. Each block cites the rule it serves.
Idempotent by construction (IF NOT EXISTS / ON CONFLICT), so a half-applied run can be repeated.
Forward-only like the baseline: restore from a snapshot to go back.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0002"
down_revision = "crm_0001"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;
-- btree_gist: uuid equality inside the tier exclusion constraint. Trusted extension (PG13+), so the DB
-- owner role may create it; infra/scripts/db_bootstrap.sh pre-creates it as well.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------- approvals: D1 the salesperson approves; D3 special price needs the manager ----------
ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_required_role_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_required_role_check
    CHECK (required_role IN ('sales','manager','md'));
-- the 4 Nov rule "final quote = MD" (T2) is gone: §11.3/§14.4 the salesperson approves the final quote
ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_check1;
ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_kind_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_kind_check
    CHECK (kind IN ('initial','revision','counter','final','below_range','special_price'));
-- §12.2 rule 4: a price below the tier minimum is approved by the manager only (D3)
ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_special_price_role_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_special_price_role_check
    CHECK (kind <> 'special_price' OR required_role = 'manager');
-- the deal owner at submit time (D1); a later reassignment does not silently move the approval
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approver_user_id uuid REFERENCES users(id);
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS escalated_at timestamptz;   -- §16 120 min -> manager

ALTER TABLE approval_events DROP CONSTRAINT IF EXISTS approval_events_kind_check;
ALTER TABLE approval_events ADD CONSTRAINT approval_events_kind_check CHECK (kind IN
    ('alert_sent','md_notified','overdue','call_placed','call_result','escalated_to_md','link_opened',
     'decided','reminder_sent','escalated_to_manager','manager_notified'));

-- grants released by a salesperson approval (D1)
ALTER TABLE quote_send_grants DROP CONSTRAINT IF EXISTS quote_send_grants_policy_check;
ALTER TABLE quote_send_grants ADD CONSTRAINT quote_send_grants_policy_check
    CHECK (policy IN ('sales','manager','md','within_range'));

-- ---------- users: D4 alert channels, D5 assignment rules, away/backup, round-robin ----------
ALTER TABLE users ADD COLUMN IF NOT EXISTS languages      text[] NOT NULL DEFAULT ARRAY[]::text[];
ALTER TABLE users ADD COLUMN IF NOT EXISTS countries      text[] NOT NULL DEFAULT ARRAY[]::text[];
ALTER TABLE users ADD COLUMN IF NOT EXISTS category_codes text[] NOT NULL DEFAULT ARRAY[]::text[];
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_away        boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS backup_user_id uuid REFERENCES users(id);
ALTER TABLE users ADD COLUMN IF NOT EXISTS round_robin_seq bigint;
ALTER TABLE users ALTER COLUMN alert_channels SET DEFAULT ARRAY['whatsapp'];
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_backup_not_self_check;
ALTER TABLE users ADD CONSTRAINT users_backup_not_self_check CHECK (backup_user_id IS DISTINCT FROM id);
CREATE SEQUENCE IF NOT EXISTS assignment_seq;
CREATE INDEX IF NOT EXISTS users_round_robin_idx ON users (round_robin_seq NULLS FIRST, id)
    WHERE role = 'sales' AND is_active;

-- ---------- deals: D5 MD takeover, D13 handover, D7/§12.2 rule 4 insisted low asks, §15 LOST ----------
ALTER TABLE deals ADD COLUMN IF NOT EXISTS taken_over_by uuid REFERENCES users(id);
ALTER TABLE deals ADD COLUMN IF NOT EXISTS taken_over_at timestamptz;
ALTER TABLE deals ADD COLUMN IF NOT EXISTS low_ask_count integer NOT NULL DEFAULT 0;
ALTER TABLE deals DROP CONSTRAINT IF EXISTS deals_low_ask_count_check;
ALTER TABLE deals ADD CONSTRAINT deals_low_ask_count_check CHECK (low_ask_count >= 0);
ALTER TABLE deals DROP CONSTRAINT IF EXISTS deals_takeover_check;
ALTER TABLE deals ADD CONSTRAINT deals_takeover_check CHECK ((taken_over_by IS NULL) = (taken_over_at IS NULL));
ALTER TABLE deals DROP CONSTRAINT IF EXISTS deals_ai_paused_reason_check;
ALTER TABLE deals ADD CONSTRAINT deals_ai_paused_reason_check CHECK (ai_paused_reason IN
    ('human_takeover','below_range','exception','md_takeover','opt_out','handover','concessions_exhausted'));
-- §15.4 lost reasons: price, delivery time, spec not available, competitor, no response (existing codes)
-- plus budget, duplicate, other
ALTER TABLE deals DROP CONSTRAINT IF EXISTS deals_lost_reason_check;
ALTER TABLE deals ADD CONSTRAINT deals_lost_reason_check CHECK (lost_reason IN
    ('price_too_high','delivery_not_suitable','bought_competitor','product_unavailable','no_response_postponed',
     'budget','duplicate','other'));
CREATE INDEX IF NOT EXISTS deals_taken_over_idx ON deals (taken_over_by) WHERE taken_over_by IS NOT NULL;

-- §10 "Pricing needed" task (D8) and §16 LOST proposal after silence (D15)
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_kind_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_kind_check CHECK (kind IN
    ('below_range_review','delivery_confirm','price_review','factory_visit','callback','knowledge_gap',
     'manual_followup','lane_risk_review','pricing_needed','lost_proposal'));

-- ---------- catalogue: D6 price list ----------
ALTER TABLE product_categories ADD COLUMN IF NOT EXISTS min_discount_pct numeric(5,2);
ALTER TABLE product_categories DROP CONSTRAINT IF EXISTS product_categories_min_discount_pct_check;
ALTER TABLE product_categories ADD CONSTRAINT product_categories_min_discount_pct_check
    CHECK (min_discount_pct IS NULL OR (min_discount_pct >= 0 AND min_discount_pct < 100));

-- products.moq already exists (baseline). D10 default delivery time; D8 custom/unpriced products.
ALTER TABLE products ADD COLUMN IF NOT EXISTS default_lead_days_min smallint;
ALTER TABLE products ADD COLUMN IF NOT EXISTS default_lead_days_max smallint;
ALTER TABLE products ADD COLUMN IF NOT EXISTS custom_printing boolean NOT NULL DEFAULT false;
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_quotable boolean NOT NULL DEFAULT true;
ALTER TABLE products DROP CONSTRAINT IF EXISTS products_lead_days_check;
ALTER TABLE products ADD CONSTRAINT products_lead_days_check CHECK (
    (default_lead_days_min IS NULL OR default_lead_days_min >= 0)
    AND (default_lead_days_max IS NULL OR default_lead_days_min IS NULL
         OR default_lead_days_max >= default_lead_days_min));

-- One row per product per quantity tier (§10). Tiers of one price-list version (same valid_from) never
-- overlap; a newer valid_from is the next version, so open quotes keep their snapshot (§24).
CREATE TABLE IF NOT EXISTS product_price_tiers (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id  uuid NOT NULL REFERENCES products(id),
    min_qty     numeric(14,2) NOT NULL CHECK (min_qty > 0),
    max_qty     numeric(14,2),                          -- NULL = no upper limit ("50,000+")
    list_price  numeric(14,6) NOT NULL CHECK (list_price > 0),
    min_price   numeric(14,6) CHECK (min_price > 0),    -- NULL = the category minimum % applies
    currency    char(3) NOT NULL DEFAULT 'AED',
    valid_from  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid REFERENCES users(id),
    updated_by  uuid REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (max_qty IS NULL OR max_qty >= min_qty),
    CONSTRAINT product_price_tiers_min_le_list CHECK (min_price IS NULL OR min_price <= list_price),
    CONSTRAINT product_price_tiers_no_overlap EXCLUDE USING gist
        (product_id WITH =, valid_from WITH =, numrange(min_qty, max_qty, '[]') WITH &&)
);
CREATE INDEX IF NOT EXISTS product_price_tiers_lookup_idx ON product_price_tiers (product_id, valid_from DESC);

-- ---------- quotes: §11.2 delivery / terms / FX lines, D10, D11 signature ----------
ALTER TABLE quote_versions ADD COLUMN IF NOT EXISTS delivery_time_text text;
ALTER TABLE quote_versions ADD COLUMN IF NOT EXISTS delivery_place text;
ALTER TABLE quote_versions ADD COLUMN IF NOT EXISTS payment_terms_text text;
ALTER TABLE quote_versions ADD COLUMN IF NOT EXISTS fx_rate numeric(18,8);   -- printed: AED per 1 unit
ALTER TABLE quote_versions ADD COLUMN IF NOT EXISTS fx_rate_date date;
ALTER TABLE quote_versions ADD COLUMN IF NOT EXISTS signed_by_user_id uuid REFERENCES users(id);
-- incoterm already exists (default 'EXW'); D6 makes the default a setting (quote.default_incoterm)

-- §11.3.3 approval locks the version: the new buyer-visible fields freeze with the prices
CREATE OR REPLACE FUNCTION crm.quote_version_freeze() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'draft' AND (
       NEW.subtotal <> OLD.subtotal OR NEW.total <> OLD.total OR NEW.currency <> OLD.currency
    OR NEW.vat_amount <> OLD.vat_amount OR NEW.incoterm <> OLD.incoterm
    OR NEW.validity_days <> OLD.validity_days
    OR NEW.delivery_time_text IS DISTINCT FROM OLD.delivery_time_text
    OR NEW.delivery_place IS DISTINCT FROM OLD.delivery_place
    OR NEW.payment_terms_text IS DISTINCT FROM OLD.payment_terms_text
    OR NEW.fx_rate IS DISTINCT FROM OLD.fx_rate
    OR NEW.fx_rate_date IS DISTINCT FROM OLD.fx_rate_date
    OR NEW.signed_by_user_id IS DISTINCT FROM OLD.signed_by_user_id
    OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256 AND OLD.content_sha256 IS NOT NULL) THEN
    RAISE EXCEPTION 'quote version % is frozen (status %): create a new version', OLD.id, OLD.status
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- Price-list snapshot per quote line (never in buyer/AI payloads, like quote_line_costs): the list price,
-- the minimum and the number of steps at quote time, in the quote currency (§10: "the negotiation range
-- converts at the same rate"; §24: an open quote keeps its prices when the price list changes).
CREATE TABLE IF NOT EXISTS quote_line_tiers (
    quote_line_id  uuid PRIMARY KEY REFERENCES quote_lines(id) ON DELETE CASCADE,
    price_tier_id  uuid REFERENCES product_price_tiers(id),
    list_price     numeric(14,6) NOT NULL CHECK (list_price > 0),
    min_price      numeric(14,6) NOT NULL CHECK (min_price > 0),
    max_steps      smallint NOT NULL CHECK (max_steps BETWEEN 0 AND 10),
    fx_rate_to_aed numeric(18,8) NOT NULL DEFAULT 1 CHECK (fx_rate_to_aed > 0),
    CHECK (min_price <= list_price)
);

-- ---------- change requests (§13, D12) ----------
CREATE TABLE IF NOT EXISTS change_requests (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id             uuid NOT NULL REFERENCES deals(id),
    quote_version_id    uuid REFERENCES quote_versions(id),
    type                text NOT NULL CHECK (type IN
                        ('product','spec','printing','quantity','delivery','samples','payment','other')),
    buyer_text          text NOT NULL,                    -- the buyer's exact words (§13.2)
    buyer_text_en       text,                             -- translation for the card
    fields              jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{field, current, requested}]
    ai_notes            text,
    status              text NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','resolved','declined')),
    assigned_to         uuid REFERENCES users(id),
    sla_due_at          timestamptz NOT NULL,
    escalated_at        timestamptz,
    resolved_version_id uuid REFERENCES quote_versions(id),
    decline_reason      text,
    started_at          timestamptz,
    closed_at           timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(fields) = 'array'),
    CHECK (status <> 'declined' OR decline_reason IS NOT NULL),
    CHECK (status <> 'resolved' OR resolved_version_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS change_requests_deal_idx ON change_requests (deal_id, created_at DESC);
CREATE INDEX IF NOT EXISTS change_requests_open_idx ON change_requests (assigned_to, sla_due_at)
    WHERE status IN ('open','in_progress');

-- ---------- orders: D15 expected re-order date stored at WON (for v2) ----------
ALTER TABLE orders ADD COLUMN IF NOT EXISTS expected_reorder_date date;

-- ---------- contacts: indexed recipient hashes (DDL v1.1 list), source 'phone' (§8 manual entry) ----------
-- SQL twin of kx.core.identity.recipient_hash. IMMUTABLE is correct here: the database encoding is fixed
-- (UTF8), which is all convert_to depends on.
CREATE OR REPLACE FUNCTION crm.recipient_hash_of(ns text, address text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN address IS NULL THEN NULL
              ELSE encode(sha256(convert_to(ns || ':' || address, 'UTF8')), 'hex') END
$$;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS recipient_hashes text[] GENERATED ALWAYS AS (
    array_remove(ARRAY[crm.recipient_hash_of('phone', phone_e164),
                       crm.recipient_hash_of('phone', whatsapp_e164),
                       crm.recipient_hash_of('email', email::text)], NULL)) STORED;
CREATE INDEX IF NOT EXISTS contacts_recipient_hashes_idx ON contacts USING gin (recipient_hashes);
ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_source_check;
ALTER TABLE contacts ADD CONSTRAINT contacts_source_check CHECK (source IN
    ('web_voice','web_chat','whatsapp','email','manual','import','form','phone'));

-- updated_at triggers for the new mutable tables
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['product_price_tiers','change_requests'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = t || '_touch') THEN
      EXECUTE format('CREATE TRIGGER %I_touch BEFORE UPDATE ON crm.%I FOR EACH ROW '
                     'EXECUTE FUNCTION crm.touch_updated_at()', t, t);
    END IF;
  END LOOP;
END $$;

-- ---------- settings (WP1 seed; existing values are kept) ----------
INSERT INTO settings (key, value, description) VALUES
 ('pricing.mode',                    '"price_list"', 'v1: price list (list + minimum per tier). "cost" = v2 cost build-up'),
 ('quote.vat_pct_domestic',          '5',     'VAT % for delivery inside the UAE (§10)'),
 ('quote.vat_pct_export',            '0',     'VAT % for exports (§10)'),
 ('quote.default_incoterm',          '"EXW"', 'Incoterm on new quotes; the salesperson can change it (§11.2)'),
 ('negotiation.max_steps',           '3',     'Equal concession steps from list price to minimum (§12.1)'),
 ('change_request.sla_working_hours','4',     'Promise to the buyer: updated quotation within N working hours (§13)'),
 ('approval.reminder_minutes',       '[30,60,120]', 'WhatsApp reminder, alert call, escalation to the manager (§16)'),
 ('followup.lost_after_days',        '14',    'Days after the last follow-up before LOST is proposed (§16)'),
 ('assignment.rules',                '[]',    'Optional rules in order: language, country, category (§9)'),
 ('features.scraping',               'false', 'v2: raw-material price scraping'),
 ('features.delivery_calc',          'false', 'v2: delivery-time calculation'),
 ('features.news_digest',            'false', 'v2: shipping-risk news digest')
ON CONFLICT (key) DO NOTHING;
UPDATE settings SET description = 'Off (null) in v1. When set, quotes above this AED total also need the MD'
 WHERE key = 'approval.md_value_limit_aed';
UPDATE settings SET description = 'Escalate a pending approval to the manager after N alert-hour minutes (§16)'
 WHERE key = 'approval.window_minutes';
-- the 4 Nov flags are superseded by the features.* settings above (D14: v2 features off in v1)
UPDATE feature_flags SET enabled = false WHERE key IN ('news_digest','price_scraping');
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
