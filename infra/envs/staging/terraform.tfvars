# Shared staging (T21): one account, one RDS instance with pack_* and auto_* databases,
# synthetic data only, smaller sizes, single-AZ RDS, Fargate Spot.
account_id      = "000000000000" # REPLACE
vpc_cidr        = "10.40.0.0/16"
base_domain     = "staging.kairox.ai" # REPLACE
route53_zone_id = null

companies = {
  pack = {
    db_prefix   = "pack_"
    redis_db    = 0
    crm_urls    = ["https://pack-crm-staging.vercel.app"] # REPLACE
    ses_domain  = "pack.staging.kairox.ai"
    cost_center = "arifaa-pack"
  }
  auto = {
    db_prefix   = "auto_"
    redis_db    = 1
    crm_urls    = ["https://auto-crm-staging.vercel.app"] # REPLACE
    ses_domain  = "auto.staging.kairox.ai"
    cost_center = "arifaa-auto"
  }
}

db_instance_class = "db.t4g.medium"
db_engine_version = "18"

alarm_emails       = ["ops@kairox.ai"] # REPLACE
monthly_budget_usd = 600

prod_account_ids = [] # REPLACE with pack-prod and auto-prod account ids (image promotion)

github_repository           = "kairox-ai/kairox-platform" # REPLACE
create_github_oidc_provider = true
state_bucket_name           = "kx-tfstate-staging-000000000000" # REPLACE
