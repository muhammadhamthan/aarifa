output "kms_key_arn" {
  value = aws_kms_key.main.arn
}

output "kms_key_id" {
  value = aws_kms_key.main.key_id
}

output "integration_secret_arns" {
  description = "Map \"<company>/<short>\" => secret ARN."
  value       = { for k, s in aws_secretsmanager_secret.integration : k => s.arn }
}

output "generated_secret_arns" {
  description = "Map \"<company>/<short>\" => secret ARN (JSON key `value`)."
  value       = { for k, s in aws_secretsmanager_secret.generated : k => s.arn }
}
