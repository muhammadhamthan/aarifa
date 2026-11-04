output "user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.this.arn
}

output "issuer" {
  description = "KX_JWT_ISSUER"
  value       = "https://${aws_cognito_user_pool.this.endpoint}"
}

output "jwks_url" {
  description = "KX_JWKS_URL"
  value       = "https://${aws_cognito_user_pool.this.endpoint}/.well-known/jwks.json"
}

output "crm_client_id" {
  description = "KX_JWT_AUDIENCE. Cognito access tokens carry it in `client_id`, ID tokens in `aud`."
  value       = aws_cognito_user_pool_client.crm.id
}

output "hosted_domain" {
  value = "${aws_cognito_user_pool_domain.this.domain}.auth.${split("_", aws_cognito_user_pool.this.id)[0]}.amazoncognito.com"
}
