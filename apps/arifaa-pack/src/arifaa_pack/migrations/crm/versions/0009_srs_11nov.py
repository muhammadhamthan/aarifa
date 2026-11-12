"""SRS update (11 Nov plan WP14-WP18): opt-out badge and LOST reason, silent phone confirmation, AI summary,
personal contact shared, CMS product mirror, opt-out settings.

Revision ID: crm_0009
Revises: crm_0008
Create Date: 2026-11-12

Sources: the SRS §7 (CMS master for descriptive content, mirrored by product code), §7A.2 (AI summary on the
deal page), §8.1 (STOP: Opted out badge, LOST proposed with reason *Opted out*, `optout.ask_reason` off),
§8.2 (phone status Verified / Likely / Reachable / Not reached), §14 (record when a personal contact is
shared). Idempotent; forward-only like the baseline.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0009"
down_revision = "crm_0008"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;

-- §8.1 step 8: Opted out badge on the deal; LOST proposed with reason *Opted out* (OPEN_QUESTIONS #59)
ALTER TABLE deals ADD COLUMN IF NOT EXISTS opted_out_at timestamptz;
ALTER TABLE deals DROP CONSTRAINT IF EXISTS deals_lost_reason_check;
ALTER TABLE deals ADD CONSTRAINT deals_lost_reason_check CHECK (lost_reason IN
    ('price_too_high','delivery_not_suitable','bought_competitor','product_unavailable','no_response_postponed',
     'budget','duplicate','other','opted_out'));

-- §7A.2: a short AI summary of the conversation at the top of the deal page
ALTER TABLE deals ADD COLUMN IF NOT EXISTS ai_summary text;
ALTER TABLE deals ADD COLUMN IF NOT EXISTS ai_summary_at timestamptz;

-- §14 practical notes: a salesperson may share a personal contact; the CRM records that it happened
ALTER TABLE deals ADD COLUMN IF NOT EXISTS personal_contact_shared_at timestamptz;
ALTER TABLE deals ADD COLUMN IF NOT EXISTS personal_contact_shared_by uuid REFERENCES users(id);

-- §8.1 step 6 (unclear stop, buyer not live -> the salesperson reviews), §8.2 step 4 (not reached -> the
-- salesperson is told), D32 last resort (quote not delivered -> the salesperson calls the buyer)
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_kind_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_kind_check CHECK (kind IN
    ('below_range_review','delivery_confirm','price_review','factory_visit','callback','knowledge_gap',
     'manual_followup','lane_risk_review','pricing_needed','lost_proposal',
     'optout_review','phone_unreached','call_buyer'));

-- §8.2: the silent phone confirmation status, its time and the WhatsApp profile name
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS phone_status text NOT NULL DEFAULT 'unknown';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS phone_status_at timestamptz;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS whatsapp_profile_name text;
ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_phone_status_check;
ALTER TABLE contacts ADD CONSTRAINT contacts_phone_status_check CHECK (phone_status IN
    ('unknown','not_reached','reachable','likely','verified'));
-- a buyer who wrote to us on WhatsApp, or whose summary was already delivered, is verified (§8.2 table)
UPDATE contacts SET phone_status = 'verified', phone_status_at = COALESCE(phone_verified_at, created_at)
 WHERE phone_status = 'unknown' AND (source = 'whatsapp' OR phone_verified_at IS NOT NULL);

-- §7: product records reach the CRM by product code when published in the CMS. The CMS owns the
-- descriptive content (spec); the CRM keeps prices, tiers, MOQ for quoting and the default delivery time.
ALTER TABLE products ADD COLUMN IF NOT EXISTS spec jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE products ADD COLUMN IF NOT EXISTS cms_published boolean NOT NULL DEFAULT false;
ALTER TABLE products ADD COLUMN IF NOT EXISTS cms_synced_at timestamptz;
CREATE INDEX IF NOT EXISTS products_cms_id_idx ON products (cms_product_id) WHERE cms_product_id IS NOT NULL;

-- §8.1 settings
INSERT INTO settings (key, value, description) VALUES
 ('optout.ask_reason', 'false', '§8.1 step 4: a "reply to tell us why" line in the STOP confirmation. Off until a lawyer confirms it is not marketing (UAE, GCC, EU, India)'),
 ('optout.live_minutes', '10', '§8.1 step 6: a buyer who replied within this many minutes is in a live conversation (the AI may ask one question)'),
 ('optout.unclear_answer_hours', '24', '§8.1 step 6: no answer to the "should we stop?" question within this many hours is an opt-out')
ON CONFLICT (key) DO NOTHING;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
