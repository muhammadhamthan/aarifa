# One RDS PostgreSQL instance per company (T19) holding the logical databases cms, crm, automation,
# agents and phoenix (AI traces, 11 Nov), each owned by its own login role. Databases and roles are created inside Postgres by the
# one-off `db-bootstrap` ECS task (infra/scripts/db_bootstrap.sh); Terraform only generates the role
# passwords and stores them, with ready-made DSNs, in Secrets Manager.

data "aws_caller_identity" "current" {}

locals {
  master_username = "kx_master"

  # "<company>/<logical>" => { company, logical, db, role }
  app_roles = merge([
    for company, prefix in var.tenants : {
      for l in var.logical_databases : "${company}/${l}" => {
        company = company
        logical = l
        db      = "${prefix}${l}"
        role    = "${prefix}${l}_app"
      }
    }
  ]...)

  reporting_roles = {
    for company, prefix in var.tenants : "${company}/reporting_ro" => {
      company = company
      logical = "reporting_ro"
      db      = "${prefix}crm"
      role    = "${prefix}reporting_ro"
    }
  }

  all_roles = merge(local.app_roles, local.reporting_roles)
}

# ---- networking -----------------------------------------------------------
resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_security_group" "db" {
  name        = "${var.name}-rds"
  description = "PostgreSQL from application tasks only"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-rds" })
}

resource "aws_vpc_security_group_ingress_rule" "db" {
  for_each                     = var.client_security_group_ids
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from ${each.key}"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

# ---- parameters -----------------------------------------------------------
resource "aws_db_parameter_group" "this" {
  name        = var.name
  family      = var.parameter_group_family
  description = "Kairox ${var.name}"
  tags        = var.tags

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
  parameter {
    name  = "log_disconnections"
    value = "1"
  }
  parameter {
    name  = "log_lock_waits"
    value = "1"
  }
  parameter {
    # pgvector HNSW builds; per-session default stays modest
    name  = "maintenance_work_mem"
    value = "262144"
  }
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }
}

# ---- logs -----------------------------------------------------------------
resource "aws_cloudwatch_log_group" "postgresql" {
  # checkov:skip=CKV_AWS_338: 30-day retention decided for operational logs (cost); audit trail is exported to S3
  name              = "/aws/rds/instance/${var.name}/postgresql"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "upgrade" {
  # checkov:skip=CKV_AWS_338: 30-day retention decided for operational logs (cost); audit trail is exported to S3
  name              = "/aws/rds/instance/${var.name}/upgrade"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

# ---- enhanced monitoring role ---------------------------------------------
data "aws_iam_policy_document" "monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "monitoring" {
  count              = var.monitoring_interval > 0 ? 1 : 0
  name               = "${var.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.monitoring_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "monitoring" {
  count      = var.monitoring_interval > 0 ? 1 : 0
  role       = aws_iam_role.monitoring[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# ---- instance -------------------------------------------------------------
resource "aws_db_instance" "this" {
  # checkov:skip=CKV_AWS_161: IAM DB auth not used; app roles use Secrets Manager passwords
  # checkov:skip=CKV2_AWS_30: query logging is configured via log_min_duration_statement
  # checkov:skip=CKV_AWS_157: multi_az is a variable - true in prod, false in shared staging (T21)
  # checkov:skip=CKV_AWS_293: deletion_protection is a variable - true in prod, false in staging
  # checkov:skip=CKV_AWS_118: monitoring_interval is a variable - 60 s in prod, off in staging
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  # The master user owns nothing but bootstrap. RDS manages its password in Secrets Manager
  # and rotates it (default every 7 days).
  username                      = local.master_username
  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.kms_key_arn
  db_name                       = "postgres"

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.kms_key_arn

  multi_az               = var.multi_az
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  port                   = 5432
  network_type           = "IPV4"
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  parameter_group_name        = aws_db_parameter_group.this.name
  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false
  apply_immediately           = false
  maintenance_window          = "fri:22:00-fri:23:00" # Sat 02:00-03:00 Gulf time
  backup_window               = "20:00-21:00"         # 00:00-01:00 Gulf time
  backup_retention_period     = var.backup_retention_days
  copy_tags_to_snapshot       = true
  delete_automated_backups    = false

  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = !var.deletion_protection
  final_snapshot_identifier = "${var.name}-final"

  # Database Insights Advanced (Recommendation: slow queries, locks) requires 465 days of PI retention.
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = var.kms_key_arn
  performance_insights_retention_period = var.database_insights_mode == "advanced" ? max(var.performance_insights_retention_days, 465) : var.performance_insights_retention_days
  database_insights_mode                = var.database_insights_mode
  monitoring_interval                   = var.monitoring_interval
  monitoring_role_arn                   = var.monitoring_interval > 0 ? aws_iam_role.monitoring[0].arn : null

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.postgresql, aws_cloudwatch_log_group.upgrade]
}

# ---- application role passwords + DSNs -------------------------------------
resource "random_password" "role" {
  for_each = local.all_roles
  length   = 40
  special  = false # URL-safe inside DSNs
}

resource "aws_secretsmanager_secret" "role" {
  # checkov:skip=CKV2_AWS_57: app-role passwords are rotated by re-running db-bootstrap after a taint
  for_each                = local.all_roles
  name                    = "kx/${var.env}/${each.value.company}/db/${each.value.logical}"
  description             = "PostgreSQL login ${each.value.role} for database ${each.value.db}"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
  tags                    = merge(var.tags, { company = each.value.company })
}

resource "aws_secretsmanager_secret_version" "role" {
  for_each  = local.all_roles
  secret_id = aws_secretsmanager_secret.role[each.key].id
  secret_string = jsonencode({
    username = each.value.role
    password = random_password.role[each.key].result
    host     = aws_db_instance.this.address
    port     = aws_db_instance.this.port
    dbname   = each.value.db
    # KX_<DB>_DSN for the app (asyncpg). TLS is forced server-side (rds.force_ssl=1); asyncpg negotiates it.
    dsn = "postgresql+asyncpg://${each.value.role}:${random_password.role[each.key].result}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${each.value.db}"
    # KX_MIGRATE_<DB>_DSN for `kx migrate` (psycopg, libpq sslmode).
    migrate_dsn = "postgresql+psycopg://${each.value.role}:${random_password.role[each.key].result}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${each.value.db}?sslmode=require"
    # Plain URL without driver or query (Arize Phoenix PHOENIX_SQL_DATABASE_URL; TLS is negotiated).
    url = "postgresql://${each.value.role}:${random_password.role[each.key].result}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${each.value.db}"
    # Payload CMS (node-postgres). TODO: switch to verify-full with the RDS CA bundle in the CMS image.
    uri = "postgres://${each.value.role}:${random_password.role[each.key].result}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${each.value.db}?sslmode=no-verify"
  })
}

# ---- optional DR: encrypted automated-backup replication to another region ----
data "aws_iam_policy_document" "dr_key" {
  # checkov:skip=CKV_AWS_109: key policy - root statement lets RDS (via IAM) use the key
  # checkov:skip=CKV_AWS_111: key policy - "*" means this key
  # checkov:skip=CKV_AWS_356: key policy - "*" means this key
  statement {
    sid       = "AccountRoot"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_kms_key" "dr" {
  count                   = var.dr_copy_region == null ? 0 : 1
  provider                = aws.dr
  description             = "${var.name} DR backup copies"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.dr_key.json
  tags                    = var.tags
}

resource "aws_db_instance_automated_backups_replication" "dr" {
  count                  = var.dr_copy_region == null ? 0 : 1
  provider               = aws.dr
  source_db_instance_arn = aws_db_instance.this.arn
  kms_key_id             = aws_kms_key.dr[0].arn
  retention_period       = var.dr_retention_days
}
