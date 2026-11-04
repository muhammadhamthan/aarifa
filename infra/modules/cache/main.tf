# ElastiCache Valkey: cache, rate limits and the ai_enabled / ai_outbound_enabled kill-switch cache only.
# Nothing here is a source of truth (procrastinate in Postgres is the only queue).

resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_security_group" "cache" {
  name        = "${var.name}-valkey"
  description = "Valkey from application tasks only"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-valkey" })
}

resource "aws_vpc_security_group_ingress_rule" "cache" {
  for_each                     = var.client_security_group_ids
  security_group_id            = aws_security_group.cache.id
  description                  = "Valkey from ${each.key}"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_elasticache_parameter_group" "this" {
  name   = var.name
  family = var.parameter_group_family
  tags   = var.tags

  parameter {
    # rate-limit and cache keys all carry TTLs
    name  = "maxmemory-policy"
    value = "volatile-lru"
  }
}

resource "random_password" "auth" {
  length  = 64
  special = false
}

resource "aws_elasticache_replication_group" "this" {
  # checkov:skip=CKV2_AWS_50: failover follows var.replicas (1 in prod, 0 in staging); cache holds no source of truth
  replication_group_id = var.name
  description          = "Kairox ${var.name} cache / rate limits / kill switches"
  engine               = "valkey"
  engine_version       = var.engine_version
  node_type            = var.node_type
  port                 = 6379
  parameter_group_name = aws_elasticache_parameter_group.this.name
  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = [aws_security_group.cache.id]

  num_cache_clusters         = 1 + var.replicas
  automatic_failover_enabled = var.replicas > 0
  multi_az_enabled           = var.replicas > 0

  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  auth_token                 = random_password.auth.result
  auth_token_update_strategy = "ROTATE"

  snapshot_retention_limit   = var.snapshot_retention_days
  snapshot_window            = "21:00-22:00"
  maintenance_window         = "fri:22:30-fri:23:30"
  auto_minor_version_upgrade = true
  apply_immediately          = false

  tags = var.tags
}

resource "aws_secretsmanager_secret" "url" {
  # checkov:skip=CKV2_AWS_57: rotated by tainting random_password.auth (ROTATE strategy keeps both valid)
  for_each                = var.tenants
  name                    = "kx/${var.env}/${each.key}/valkey"
  description             = "Valkey URL for ${each.key} (KX_REDIS_URL)"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
  tags                    = merge(var.tags, { company = each.key })
}

resource "aws_secretsmanager_secret_version" "url" {
  for_each  = var.tenants
  secret_id = aws_secretsmanager_secret.url[each.key].id
  secret_string = jsonencode({
    url = "rediss://:${random_password.auth.result}@${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/${each.value}"
  })
}
