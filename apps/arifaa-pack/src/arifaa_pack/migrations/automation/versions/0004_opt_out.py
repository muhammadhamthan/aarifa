"""§8.1 STOP flow (11 Nov plan WP14, D18): one suppression list by scope, the permanent opt-out log.

Revision ID: automation_0004
Revises: automation_0003
Create Date: 2026-11-12

Sources: the SRS §8.1 step 2 ("one shared suppression list ... checked before every WhatsApp message, email
and call"), step 6 (channel-only stops), step 9 (permanent logging: exact words, time UTC, channel, the rule
or AI version that decided, what was stopped, the confirmation text and when it was sent).
Idempotent; forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "automation_0004"
down_revision = "automation_0003"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = automation, public;

-- address suppressions now take a scope: all | whatsapp | email | phone (calls)
COMMENT ON COLUMN suppressions.channel IS 'scope: all, whatsapp, email or phone (calls); §8.1 step 2 and 6';

-- a stop also follows the person, whatever address they use next (the CRM knows all of them)
CREATE TABLE IF NOT EXISTS contact_suppressions (
    contact_ref uuid NOT NULL,
    scope       text NOT NULL CHECK (scope IN ('all','whatsapp','email','phone')),
    reason      text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (contact_ref, scope)
);

-- §8.1 step 9: permanent (retention never deletes these rows)
CREATE TABLE IF NOT EXISTS opt_out_log (
    id                       uuid PRIMARY KEY,
    at                       timestamptz NOT NULL DEFAULT now(),
    channel                  text NOT NULL,
    contact_ref              uuid,
    deal_ref                 uuid,
    thread_ref               uuid,
    address_hash             char(64),
    scope                    text NOT NULL CHECK (scope IN ('all','whatsapp','email','phone')),
    source                   text NOT NULL CHECK (source IN ('rule','ai','unsubscribe_link','salesperson','no_answer')),
    exact_words              text,
    volunteered_reason       text,
    decided_by               text NOT NULL,
    stopped                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    confirmation_text        text,
    confirmation_message_ref uuid,
    confirmation_queued_at   timestamptz
);
CREATE INDEX IF NOT EXISTS opt_out_log_contact_idx ON opt_out_log (contact_ref, at DESC);
CREATE INDEX IF NOT EXISTS opt_out_log_hash_idx ON opt_out_log (address_hash, at DESC);

-- the one confirmation after a stop, and the salesperson's own replies from the CRM inbox (§14)
ALTER TABLE outbound_messages DROP CONSTRAINT IF EXISTS outbound_messages_kind_check;
ALTER TABLE outbound_messages ADD CONSTRAINT outbound_messages_kind_check CHECK (kind IN
    ('quote','final_quote','counter_offer','reply','followup','summary_pdf','approval_alert','md_notice',
     'order_update','reorder','notification','opt_out_confirmation','staff_reply'));
CREATE INDEX IF NOT EXISTS outbound_queued_hash_idx ON outbound_messages (recipient_hash) WHERE status = 'queued';
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
