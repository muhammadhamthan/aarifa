# Arifaa Pack production (AWS account: Kairox Organization member "kx-pack-prod").
company     = "pack"
account_id  = "000000000000" # REPLACE
cost_center = "arifaa-pack"
vpc_cidr    = "10.41.0.0/16"

api_host              = "api.arifapack.com"
cms_host              = "cms.arifapack.com"
route53_zone_id       = null # client DNS: records are output and added by hand
ses_domain            = "arifapack.com"
inbound_mail_enabled  = false # flip after SES Mail Manager is confirmed in me-central-1
crm_urls              = ["https://crm.arifapack.com"]
cognito_domain_prefix = "kx-arifaa-pack"

db_instance_class       = "db.m6g.large"
db_engine_version       = "18"
db_allocated_storage_gb = 100
dr_copy_region          = null # OFF: only with the client's written consent (e.g. "eu-central-1")
cache_node_type         = "cache.t4g.small"
bedrock_region          = null # Anthropic API direct; set only if Bedrock + data-transfer consent

alarm_emails       = ["ops@kairox.ai"] # REPLACE
monthly_budget_usd = 1570              # "medium" ceiling from the 4 Nov budget

github_repository           = "kairox-ai/kairox-platform" # REPLACE with the real owner/repo
staging_account_id          = "000000000000"              # REPLACE (image promotion source)
github_environment          = "pack-prod"
create_github_oidc_provider = true
state_bucket_name           = "kx-tfstate-pack-prod-000000000000" # REPLACE (from infra/bootstrap)
