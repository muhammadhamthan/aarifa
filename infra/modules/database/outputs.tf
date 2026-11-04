output "address" {
  value = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "instance_id" {
  value = aws_db_instance.this.identifier
}

output "instance_arn" {
  value = aws_db_instance.this.arn
}

output "security_group_id" {
  value = aws_security_group.db.id
}

output "master_secret_arn" {
  description = "RDS-managed master secret (JSON keys username, password)."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "role_secret_arns" {
  description = "\"<company>/<logical>\" => secret ARN (keys: username, password, host, port, dbname, dsn, migrate_dsn, uri). Logical includes reporting_ro."
  value       = { for k, s in aws_secretsmanager_secret.role : k => s.arn }
}

output "bootstrap_roles" {
  description = "What db-bootstrap must create: \"<company>/<logical>\" => { db, role }."
  value       = { for k, r in local.all_roles : k => { db = r.db, role = r.role, company = r.company, logical = r.logical } }
}
