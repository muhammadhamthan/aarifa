#!/usr/bin/env bash
# =====================================================================================
# Kairox DB bootstrap - idempotent. Run as the RDS master user BEFORE `kx migrate`.
#
# For every tenant prefix ("" in prod; "pack_", "auto_" in staging) it ensures:
#   roles      <p>cms_app <p>crm_app <p>automation_app <p>agents_app <p>reporting_ro
#              (LOGIN, password = $KX_PW_<ROLE_UPPER>, re-synced every run; mirrors db/ddl/00_roles.sql)
#   databases  <p>cms <p>crm <p>automation <p>agents, each OWNED by its app role
#   connect    REVOKE CONNECT/TEMP FROM PUBLIC; each role may connect only to its own DB;
#              reporting_ro may connect to <p>crm
#   extensions crm: citext, pg_trgm, btree_gist | automation: citext | agents: vector, pg_trgm
#              (created here because the app roles are not allowed to)
#   reporting  schema crm pre-created (owner crm_app) + read-only grants and default privileges
#              for reporting_ro, so tables created later by `kx migrate` are readable too
#
# Environment (ECS task definition `<name>-db-bootstrap` injects all of these):
#   PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE=postgres PGSSLMODE=require    (libpq)
#   KX_BOOTSTRAP_PREFIXES   comma-separated prefixes; empty string = one tenant without prefix
#   KX_PW_<ROLE>            e.g. KX_PW_CRM_APP, KX_PW_PACK_CRM_APP, KX_PW_REPORTING_RO
# Local:  PGHOST=localhost PGUSER=postgres KX_PW_CRM_APP=dev ... infra/scripts/db_bootstrap.sh
# =====================================================================================
set -euo pipefail

LOGICALS=(cms crm automation agents)
declare -A EXTENSIONS=(
  [cms]=""
  [crm]="citext pg_trgm btree_gist"
  [automation]="citext"
  [agents]="vector pg_trgm"
)

PSQL=(psql -X -v ON_ERROR_STOP=1 --quiet --no-psqlrc)

log() { printf '%s db-bootstrap: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

if [[ -z "${KX_BOOTSTRAP_PREFIXES:-}" ]]; then
  PREFIXES=("")
else
  IFS=',' read -r -a PREFIXES <<<"${KX_BOOTSTRAP_PREFIXES}"
fi

password_for() {
  local role="$1" var
  var="KX_PW_$(tr '[:lower:]' '[:upper:]' <<<"$role")"
  if [[ -z "${!var:-}" ]]; then
    log "missing password env ${var} for role ${role}"
    exit 2
  fi
  printf '%s' "${!var}"
}

# psql variables (:'name') are interpolated outside dollar-quoted bodies only, so statements are
# built with format(%I, %L) and executed with \gexec.
ensure_role() {
  local role="$1" pw
  pw="$(password_for "$role")"
  "${PSQL[@]}" -d postgres -v role="$role" -v pw="$pw" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOCREATEDB NOCREATEROLE PASSWORD %L', :'role', :'pw') \gexec
-- PG16+: the master (rds_superuser, not superuser) needs SET to create databases owned by the role and
-- INHERIT to pass owner checks (GRANT/REVOKE on the database, CREATE on its public schema).
SELECT format('GRANT %I TO CURRENT_USER WITH INHERIT TRUE, SET TRUE', :'role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper) \gexec
SQL
}

ensure_database() {
  local db="$1" owner="$2"
  "${PSQL[@]}" -d postgres -v db="$db" -v owner="$owner" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8'' TEMPLATE template0', :'db', :'owner')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'db', :'owner') \gexec
SELECT format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', :'db') \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', :'db', :'owner') \gexec
SQL
}

# Extensions are created by the master (CREATE on the public schema via inherited ownership; RDS lets
# rds_superuser create allow-listed extensions such as vector). The app roles could not create
# `vector` themselves.
ensure_extensions() {
  local db="$1" exts="$2" ext
  for ext in $exts; do
    "${PSQL[@]}" -d "$db" -v ext="$ext" <<'SQL'
SELECT format('CREATE EXTENSION IF NOT EXISTS %I', :'ext') \gexec
SQL
  done
}

ensure_reporting() {
  local db="$1" owner="$2" ro="$3"
  "${PSQL[@]}" -d postgres -v db="$db" -v ro="$ro" <<'SQL'
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'ro') \gexec
SQL
  "${PSQL[@]}" -d "$db" -v owner="$owner" -v ro="$ro" <<'SQL'
-- as the owner: the master has no CREATE privilege on a database it does not own
SELECT format('SET ROLE %I', :'owner') \gexec
CREATE SCHEMA IF NOT EXISTS crm;
SELECT format('GRANT USAGE ON SCHEMA crm TO %I', :'ro') \gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA crm TO %I', :'ro') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA crm GRANT SELECT ON TABLES TO %I', :'ro') \gexec
RESET ROLE;
SQL
}

log "server: $("${PSQL[@]}" -d postgres -Atc 'SHOW server_version') as $("${PSQL[@]}" -d postgres -Atc 'SELECT current_user')"

for p in "${PREFIXES[@]}"; do
  label="${p:-<none>}"
  log "tenant prefix ${label}"
  for l in "${LOGICALS[@]}"; do
    ensure_role "${p}${l}_app"
  done
  ensure_role "${p}reporting_ro"

  for l in "${LOGICALS[@]}"; do
    ensure_database "${p}${l}" "${p}${l}_app"
    ensure_extensions "${p}${l}" "${EXTENSIONS[$l]}"
  done
  ensure_reporting "${p}crm" "${p}crm_app" "${p}reporting_ro"
  log "tenant prefix ${label}: ok"
done

log "done"
