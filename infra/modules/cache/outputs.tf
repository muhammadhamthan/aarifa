output "primary_endpoint" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "replication_group_id" {
  value = aws_elasticache_replication_group.this.id
}

output "url_secret_arns" {
  description = "company => secret ARN (JSON keys `url` and `broker_url`: rediss:// URLs with the AUTH token and DB index)."
  value       = { for k, s in aws_secretsmanager_secret.url : k => s.arn }
}
