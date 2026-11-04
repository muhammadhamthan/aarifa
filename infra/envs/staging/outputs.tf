output "alb_dns_name" {
  value = module.alb.alb_dns_name
}

output "certificate_validation_records" {
  value = module.alb.certificate_validation_records
}

output "hosts" {
  value = local.hosts
}

output "ses_dns_records" {
  value = { for c, m in module.email : c => m.dns_records_for_client }
}

output "cognito" {
  value = { for c, m in module.auth : c => {
    user_pool_id  = m.user_pool_id
    client_id     = m.crm_client_id
    issuer        = m.issuer
    hosted_domain = m.hosted_domain
  } }
}

output "ecr_repositories" {
  value = module.cluster.repository_urls
}

output "github" {
  value = {
    AWS_DEPLOY_ROLE_ARN = module.github_oidc.deploy_role_arn
    TF_APPLY_ROLE_ARN   = module.github_oidc.tf_apply_role_arn
    TF_PLAN_ROLE_ARN    = module.github_oidc.tf_plan_role_arn
  }
}

output "deploy_manifests" {
  value = { for c, m in module.app : c => m.deploy_manifest }
}

output "db_bootstrap_task_family" {
  value = module.db_bootstrap.task_definition_family
}
