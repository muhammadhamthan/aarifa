# ElastiCache for Redis OSS (11 Nov, Recommendation "Cache + job broker"): cache, rate limits, kill
# switches, daily spend counters and the Celery broker. A broker must never lose keys, so the parameter
# group uses maxmemory-policy = noeviction (Celery's Redis guide). Due work and long timers are NOT here:
# they are rows in automation.scheduled_jobs (PostgreSQL), so a lost broker loses nothing durable.

resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_security_group" "cache" {
  name        = "${var.name}-redis"
  description = "Redis from application tasks only"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-redis" })
}

resource "aws_vpc_security_group_ingress_rule" "cache" {
  for_each                     = var.client_security_group_ids
  security_group_id            = aws_security_group.cache.id
  description                  = "Redis from ${each.key}"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_elasticache_parameter_group" "this" {
  name   = "${var.name}-redis"
  family = var.parameter_group_family
  tags   = var.tags

  parameter {
    # Celery broker keys have no TTL: evicting them would silently drop tasks.
    name  = "maxmemory-policy"
    value = "noeviction"
  }
}

resource "random_password" "auth" {
  length  = 64
  special = false
}

resource "aws_elasticache_replication_group" "this" {
  # checkov:skip=CKV2_AWS_50: failover follows var.replicas (1 in prod = 2 nodes in 2 zones, 0 in staging)
  replication_group_id = var.name
  description          = "Kairox ${var.name} cache / rate limits / kill switches / Celery broker"
  engine               = "redis"
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

locals {
  endpoint = aws_elasticache_replication_group.this.primary_endpoint_address
}

resource "aws_secretsmanager_secret" "url" {
  # checkov:skip=CKV2_AWS_57: rotated by tainting random_password.auth (ROTATE strategy keeps both valid)
  for_each                = var.tenants
  name                    = "kx/${var.env}/${each.key}/redis"
  description             = "Redis URLs for ${each.key}: url = KX_REDIS_URL, broker_url = KX_APP_CELERY_BROKER_URL"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
  tags                    = merge(var.tags, { company = each.key })
}

resource "aws_secretsmanager_secret_version" "url" {
  for_each  = var.tenants
  secret_id = aws_secretsmanager_secret.url[each.key].id
  secret_string = jsonencode({
    # cache, rate limits, kill switches, spend counters (logical DB = tenant index)
    url = "rediss://:${random_password.auth.result}@${local.endpoint}:6379/${each.value}"
    # Celery broker on its own logical DB (tenant index + broker_db_offset) so a cache flush never drops
    # tasks; Celery needs ssl_cert_reqs on rediss:// URLs (ElastiCache certificates are publicly trusted).
    broker_url = "rediss://:${random_password.auth.result}@${local.endpoint}:6379/${each.value + var.broker_db_offset}?ssl_cert_reqs=required"
  })
}
