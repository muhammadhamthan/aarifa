# Arifaa Auto production (AWS account: Kairox Organization member "kx-auto-prod").
# Auto requirements are signed Sat 14 Nov - hostnames and sizes below are placeholders.
company     = "auto"
account_id  = "000000000000" # REPLACE
cost_center = "arifaa-auto"
vpc_cidr    = "10.42.0.0/16"

api_host              = "api.autotec.ae"
cms_host              = "cms.autotec.ae"
route53_zone_id       = null
ses_domain            = "autotec.ae"
inbound_mail_enabled  = false
crm_urls              = ["https://crm.autotec.ae"]
website_urls          = ["https://www.autotec.ae"] # REPLACE
email_from_address    = "sales@autotec.ae"         # REPLACE

db_instance_class       = "db.m6g.large"
db_engine_version       = "18"
db_allocated_storage_gb = 100
dr_copy_region          = null
cache_node_type         = "cache.t4g.small"
database_insights_mode  = "advanced"

alarm_emails       = ["ops@kairox.ai"] # REPLACE
monthly_budget_usd = 1570

github_repository           = "kairox-ai/kairox-platform" # REPLACE
staging_account_id          = "000000000000"              # REPLACE (image promotion source)
github_environment          = "auto-prod"
create_github_oidc_provider = true
state_bucket_name           = "kx-tfstate-auto-prod-000000000000" # REPLACE
