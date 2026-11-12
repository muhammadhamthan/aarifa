output "alb_dns_name" {
  description = "Point api/cms CNAMEs here when the client manages DNS."
  value       = module.alb.alb_dns_name
}

output "certificate_validation_records" {
  value = module.alb.certificate_validation_records
}

output "ses_dns_records" {
  value = module.email.dns_records_for_client
}

output "nat_public_ips" {
  value = module.network.nat_public_ips
}

output "staff_login" {
  description = "In-house staff login (11 Nov): tokens are RS256, verifiable with the API's public JWKS."
  value = {
    issuer   = "https://${var.api_host}"
    audience = "kx-crm"
    jwks_url = "https://${var.api_host}/.well-known/jwks.json"
  }
}

output "internal_urls" {
  value = module.app.internal_urls
}

output "vendor_order_prefix" {
  description = "Parameter Store path of the vendor order per field (edit to switch vendors without a deploy)."
  value       = module.app.vendor_order_prefix
}

output "ecr_repositories" {
  value = module.cluster.repository_urls
}

output "github" {
  description = "Set these as GitHub Environment variables (see infra/README.md)."
  value = {
    AWS_DEPLOY_ROLE_ARN = module.github_oidc.deploy_role_arn
    TF_APPLY_ROLE_ARN   = module.github_oidc.tf_apply_role_arn
    TF_PLAN_ROLE_ARN    = module.github_oidc.tf_plan_role_arn
  }
}

output "deploy_manifest" {
  value = module.app.deploy_manifest
}

output "db_bootstrap_task_family" {
  value = module.db_bootstrap.task_definition_family
}
