"""Background jobs on Celery: the PostgreSQL schedule table Beat sweeps (11 Nov plan WP20, D25).

Revision ID: automation_0003
Revises: automation_0002
Create Date: 2026-11-12

Source: platform docs (Recommendation "Background jobs", Fallback plan "Background jobs" / "Job broker",
Platform contest round 4): one Beat; long waits kept in PostgreSQL and swept every 30 s, never as long Celery
ETAs; tasks idempotent with acks_late; jobs re-created from this table if Redis is lost.
Replaces the procrastinate queue (its tables, if present, are left untouched for a manual drop after
cut-over). Idempotent; forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "automation_0003"
down_revision = "automation_0002"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = automation, public;

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id            uuid PRIMARY KEY,
    task          text NOT NULL,                 -- e.g. automation.deliver_outbound
    args          jsonb NOT NULL DEFAULT '{}'::jsonb,
    dedup_key     text,                          -- one pending job per key (replace / pull forward / cancel)
    lock_key      text,                          -- jobs sharing it run one at a time, oldest first
    run_at        timestamptz NOT NULL,
    status        text NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','running','done','failed','cancelled')),
    attempts      integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts  integer NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),
    last_error    text,
    dispatched_at timestamptz,                   -- last time it was handed to Celery
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);
-- the ON CONFLICT target of JobStore._insert: must match its WHERE exactly
CREATE UNIQUE INDEX IF NOT EXISTS scheduled_jobs_pending_key_uq ON scheduled_jobs (dedup_key)
    WHERE status IN ('todo', 'running');
CREATE INDEX IF NOT EXISTS scheduled_jobs_due_idx ON scheduled_jobs (run_at) WHERE status = 'todo';
CREATE INDEX IF NOT EXISTS scheduled_jobs_lock_idx ON scheduled_jobs (lock_key, created_at)
    WHERE lock_key IS NOT NULL AND status IN ('todo', 'running');
CREATE INDEX IF NOT EXISTS scheduled_jobs_running_idx ON scheduled_jobs (updated_at) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS scheduled_jobs_prefix_idx ON scheduled_jobs (dedup_key text_pattern_ops)
    WHERE status = 'todo';
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
