-- =====================================================================
-- One RDS instance per company, four logical databases, one login role
-- per application. Run as the RDS master user. Passwords come from
-- Secrets Manager (RDS-managed); placeholders here are for local dev only.
-- =====================================================================
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cms_app')        THEN CREATE ROLE cms_app        LOGIN PASSWORD 'dev'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crm_app')        THEN CREATE ROLE crm_app        LOGIN PASSWORD 'dev'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automation_app') THEN CREATE ROLE automation_app LOGIN PASSWORD 'dev'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agents_app')     THEN CREATE ROLE agents_app     LOGIN PASSWORD 'dev'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reporting_ro')   THEN CREATE ROLE reporting_ro   LOGIN PASSWORD 'dev'; END IF;
END $$;

-- Databases (each owned by its app role). In staging the names carry the
-- company prefix: pack_crm, auto_crm, ...
-- CREATE DATABASE cms        OWNER cms_app;
-- CREATE DATABASE crm        OWNER crm_app;
-- CREATE DATABASE automation OWNER automation_app;
-- CREATE DATABASE agents     OWNER agents_app;

-- Each role may connect ONLY to its own database
-- REVOKE CONNECT ON DATABASE cms, crm, automation, agents FROM PUBLIC;
-- GRANT  CONNECT ON DATABASE cms        TO cms_app;
-- GRANT  CONNECT ON DATABASE crm        TO crm_app, reporting_ro;
-- GRANT  CONNECT ON DATABASE automation TO automation_app;
-- GRANT  CONNECT ON DATABASE agents     TO agents_app;
