# Shared staging account hosting every company (T21: keep it simple to operate).
# One VPC / ALB / ECS cluster / RDS instance / Redis node; per company: databases <prefix>cms ...,
# login roles, secrets (incl. the staff-login keys), buckets, SES identity and services.

locals {
  name = "kx-staging"
  env  = "staging"

  tags = {
    company       = "shared"
    env           = "staging"
    owner         = var.owner
    "cost-center" = var.cost_center
    managed-by    = "terraform"
  }

  company_tags = { for c, cfg in var.companies : c => merge(local.tags, { company = c, "cost-center" = cfg.cost_center }) }

  hosts = { for c, _ in var.companies : c => {
    api = "${c}-api.${var.base_domain}"
    cms = "${c}-cms.${var.base_domain}"
  } }

  # "<company>/<short>" maps regrouped per company
  integration_secret_arns = { for c, _ in var.companies : c => {
    for k, v in module.secrets.integration_secret_arns : split("/", k)[1] => v if split("/", k)[0] == c
  } }
  generated_secret_arns = { for c, _ in var.companies : c => {
    for k, v in module.secrets.generated_secret_arns : split("/", k)[1] => v if split("/", k)[0] == c
  } }
  db_secret_arns = { for c, _ in var.companies : c => {
    for k, v in module.database.role_secret_arns : split("/", k)[1] => v if split("/", k)[0] == c
  } }

  # ALB host rules: two per company (api, cms)
  company_keys = sort(keys(var.companies))
  alb_targets = merge([
    for i, c in local.company_keys : {
      "${c}-api" = { host = local.hosts[c].api, port = 8000, health_path = var.api_health_path, priority = 10 + i * 10 }
      "${c}-cms" = { host = local.hosts[c].cms, port = 3000, health_path = var.cms_health_path, priority = 15 + i * 10 }
    }
  ]...)
}

module "secrets" {
  source                  = "../../modules/secrets"
  name                    = local.name
  env                     = local.env
  companies               = keys(var.companies)
  kms_decrypt_account_ids = var.prod_account_ids
  tags                    = local.tags
}

module "network" {
  source             = "../../modules/network"
  name               = local.name
  cidr_block         = var.vpc_cidr
  az_ids             = var.az_ids
  single_nat_gateway = true
  kms_key_arn        = module.secrets.kms_key_arn
  tags               = local.tags
}

module "database" {
  source = "../../modules/database"
  providers = {
    aws    = aws
    aws.dr = aws.dr
  }

  name                      = local.name
  env                       = local.env
  tenants                   = { for c, cfg in var.companies : c => cfg.db_prefix }
  engine_version            = var.db_engine_version
  instance_class            = var.db_instance_class
  allocated_storage_gb      = 30
  max_allocated_storage_gb  = 100
  multi_az                  = false
  deletion_protection       = false
  backup_retention_days     = 7
  monitoring_interval       = 0
  subnet_ids                = module.network.data_subnet_ids
  vpc_id                    = module.network.vpc_id
  client_security_group_ids = { app = module.network.app_security_group_id }
  kms_key_arn               = module.secrets.kms_key_arn
  dr_copy_region            = null
  tags                      = local.tags
}

module "cache" {
  source                    = "../../modules/cache"
  name                      = local.name
  env                       = local.env
  tenants                   = { for c, cfg in var.companies : c => cfg.redis_db }
  node_type                 = "cache.t4g.micro"
  replicas                  = 0
  snapshot_retention_days   = 0
  subnet_ids                = module.network.data_subnet_ids
  vpc_id                    = module.network.vpc_id
  client_security_group_ids = { app = module.network.app_security_group_id }
  kms_key_arn               = module.secrets.kms_key_arn
  tags                      = local.tags
}

module "storage" {
  source               = "../../modules/storage"
  for_each             = var.companies
  name                 = "${local.name}-${each.key}"
  kms_key_arn          = module.secrets.kms_key_arn
  force_destroy        = true
  cors_allowed_origins = each.value.crm_urls
  tags                 = local.company_tags[each.key]
}

module "email" {
  source               = "../../modules/email"
  for_each             = var.companies
  name                 = "${local.name}-${each.key}"
  domain               = each.value.ses_domain
  route53_zone_id      = var.route53_zone_id
  event_webhook_url    = "https://${local.hosts[each.key].api}/webhooks/ses"
  inbound_mail_enabled = false
  kms_key_arn          = module.secrets.kms_key_arn
  tags                 = local.company_tags[each.key]
}

module "cluster" {
  source           = "../../modules/ecs_cluster"
  name             = local.name
  pull_account_ids = var.prod_account_ids
  # Recommendation (11 Nov): Better Stack replaces Container Insights.
  container_insights = "disabled"
  kms_key_arn        = module.secrets.kms_key_arn
  tags               = local.tags
}

module "alb" {
  source                    = "../../modules/alb_waf"
  name                      = local.name
  vpc_id                    = module.network.vpc_id
  public_subnet_ids         = module.network.public_subnet_ids
  app_security_group_id     = module.network.app_security_group_id
  domain_name               = "*.${var.base_domain}"
  subject_alternative_names = []
  route53_zone_id           = var.route53_zone_id
  deletion_protection       = false
  targets                   = local.alb_targets
  kms_key_arn               = module.secrets.kms_key_arn
  tags                      = local.tags
}

module "app" {
  source   = "../../modules/app_services"
  for_each = var.companies

  name                    = "${local.name}-${each.key}"
  env                     = local.env
  kx_env                  = "staging"
  company                 = each.key
  cluster_arn             = module.cluster.cluster_arn
  cluster_name            = module.cluster.cluster_name
  app_repository_url      = module.cluster.repository_urls["kairox/app"]
  cms_repository_url      = module.cluster.repository_urls["kairox/cms"]
  vpc_id                  = module.network.vpc_id
  subnet_ids              = module.network.private_subnet_ids
  security_group_id       = module.network.app_security_group_id
  api_target_group_arn    = module.alb.target_group_arns["${each.key}-api"]
  cms_target_group_arn    = module.alb.target_group_arns["${each.key}-cms"]
  api_health_path         = var.api_health_path
  public_api_url          = "https://${local.hosts[each.key].api}"
  public_cms_url          = "https://${local.hosts[each.key].cms}"
  db_secret_arns          = local.db_secret_arns[each.key]
  redis_secret_arn        = module.cache.url_secret_arns[each.key]
  jwt_secret_arn          = module.secrets.jwt_secret_arns[each.key]
  auth_secret_arn         = module.secrets.auth_secret_arns[each.key]
  integration_secret_arns = local.integration_secret_arns[each.key]
  generated_secret_arns   = local.generated_secret_arns[each.key]
  bucket_names            = module.storage[each.key].bucket_names
  bucket_arns             = module.storage[each.key].bucket_arns
  inbound_bucket_arn      = null
  ses_identity_arn        = module.email[each.key].identity_arn
  ses_configuration_set   = module.email[each.key].configuration_set_name
  ses_events_topic_arn    = module.email[each.key].events_topic_arn
  ses_from_domain         = each.value.ses_domain
  cors_origins            = concat(each.value.crm_urls, ["http://localhost:3000", "http://localhost:3001"])
  sizes                   = var.service_sizes
  capacity_provider       = "FARGATE_SPOT"
  db_bootstrap_family     = "${local.name}-db-bootstrap"
  kms_key_arn             = module.secrets.kms_key_arn
  log_retention_days      = 14
  tags                    = local.company_tags[each.key]
}

# ---- one-off: databases, roles and extensions for every company (idempotent) ---------------
module "db_bootstrap" {
  source = "../../modules/ecs_service"

  name                   = "${local.name}-db-bootstrap"
  cluster_arn            = module.cluster.cluster_arn
  cluster_name           = module.cluster.cluster_name
  image                  = "${module.cluster.repository_urls["kairox/app"]}:${module.app[local.company_keys[0]].image_tag}"
  entry_point            = ["/app/scripts/db_bootstrap.sh"]
  cpu                    = 256
  memory                 = 512
  subnet_ids             = module.network.private_subnet_ids
  security_group_ids     = [module.network.app_security_group_id]
  create_service         = false
  enable_execute_command = false
  kms_key_arn            = module.secrets.kms_key_arn
  log_retention_days     = 14
  tags                   = merge(local.tags, { service = "db-bootstrap" })

  environment = {
    PGHOST                = module.database.address
    PGPORT                = tostring(module.database.port)
    PGDATABASE            = "postgres"
    PGSSLMODE             = "require"
    KX_BOOTSTRAP_PREFIXES = join(",", [for c in local.company_keys : var.companies[c].db_prefix])
  }
  secrets = merge(
    {
      PGUSER     = "${module.database.master_secret_arn}:username::"
      PGPASSWORD = "${module.database.master_secret_arn}:password::"
    },
    { for k, r in module.database.bootstrap_roles : "KX_PW_${upper(r.role)}" => "${module.database.role_secret_arns[k]}:password::" },
  )
}

module "observability" {
  source                    = "../../modules/observability"
  name                      = local.name
  alarm_emails              = var.alarm_emails
  alb_arn_suffix            = module.alb.alb_arn_suffix
  target_group_arn_suffixes = module.alb.target_group_arn_suffixes
  db_instance_ids           = [module.database.instance_id]
  db_allocated_storage_gb   = 30
  ecs_cluster_name          = module.cluster.cluster_name
  ecs_services              = merge([for c in local.company_keys : { "${c}-api" = "${local.name}-${c}-api", "${c}-worker" = "${local.name}-${c}-worker" }]...)
  companies                 = local.company_keys
  monthly_budget_usd        = var.monthly_budget_usd
  budget_emails             = var.alarm_emails
  kms_key_arn               = module.secrets.kms_key_arn
  tags                      = local.tags
}

module "github_oidc" {
  source               = "../../modules/github_oidc"
  name                 = local.name
  repository           = var.github_repository
  environments         = ["staging"]
  create_oidc_provider = var.create_github_oidc_provider
  ecr_repository_arns  = values(module.cluster.repository_arns)
  ecs_cluster_arn      = module.cluster.cluster_arn
  passable_role_prefix = "${local.name}-"
  ssm_image_tag_prefix = "/kx/${local.env}"
  state_bucket_name    = var.state_bucket_name
  tags                 = local.tags
}
