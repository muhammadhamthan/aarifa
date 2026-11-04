# Production for ONE company in its own AWS account (me-central-1, AZs az1 + az3).
# This file is identical in every <company>-prod folder; only terraform.tfvars and backend.hcl differ.

locals {
  name = "kx-${var.company}-prod"
  env  = "prod"

  tags = {
    company       = var.company
    env           = "prod"
    owner         = var.owner
    "cost-center" = var.cost_center
    managed-by    = "terraform"
  }

  integration_secret_arns = { for k, v in module.secrets.integration_secret_arns : split("/", k)[1] => v }
  generated_secret_arns   = { for k, v in module.secrets.generated_secret_arns : split("/", k)[1] => v }
  db_secret_arns          = { for k, v in module.database.role_secret_arns : split("/", k)[1] => v }
}

module "secrets" {
  source    = "../../modules/secrets"
  name      = local.name
  env       = local.env
  companies = [var.company]
  tags      = local.tags
}

module "network" {
  source             = "../../modules/network"
  name               = local.name
  cidr_block         = var.vpc_cidr
  az_ids             = var.az_ids
  single_nat_gateway = false
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
  tenants                   = { (var.company) = "" }
  engine_version            = var.db_engine_version
  instance_class            = var.db_instance_class
  allocated_storage_gb      = var.db_allocated_storage_gb
  max_allocated_storage_gb  = var.db_allocated_storage_gb * 4
  multi_az                  = true
  deletion_protection       = true
  backup_retention_days     = 14
  subnet_ids                = module.network.data_subnet_ids
  vpc_id                    = module.network.vpc_id
  client_security_group_ids = { app = module.network.app_security_group_id }
  kms_key_arn               = module.secrets.kms_key_arn
  dr_copy_region            = var.dr_copy_region
  tags                      = local.tags
}

module "cache" {
  source                    = "../../modules/cache"
  name                      = local.name
  env                       = local.env
  tenants                   = { (var.company) = 0 }
  node_type                 = var.cache_node_type
  replicas                  = 1
  subnet_ids                = module.network.data_subnet_ids
  vpc_id                    = module.network.vpc_id
  client_security_group_ids = { app = module.network.app_security_group_id }
  kms_key_arn               = module.secrets.kms_key_arn
  tags                      = local.tags
}

module "storage" {
  source               = "../../modules/storage"
  name                 = local.name
  kms_key_arn          = module.secrets.kms_key_arn
  cors_allowed_origins = var.crm_urls
  tags                 = local.tags
}

module "email" {
  source               = "../../modules/email"
  name                 = local.name
  domain               = var.ses_domain
  route53_zone_id      = var.route53_zone_id
  event_webhook_url    = "https://${var.api_host}/v1/webhooks/ses"
  inbound_mail_enabled = var.inbound_mail_enabled
  kms_key_arn          = module.secrets.kms_key_arn
  tags                 = local.tags
}

module "auth" {
  source              = "../../modules/auth"
  name                = local.name
  callback_urls       = [for u in var.crm_urls : "${u}/auth/callback"]
  logout_urls         = var.crm_urls
  domain_prefix       = var.cognito_domain_prefix
  deletion_protection = true
  tags                = local.tags
}

module "cluster" {
  source      = "../../modules/ecs_cluster"
  name        = local.name
  kms_key_arn = module.secrets.kms_key_arn
  tags        = local.tags
}

module "alb" {
  source                    = "../../modules/alb_waf"
  name                      = local.name
  vpc_id                    = module.network.vpc_id
  public_subnet_ids         = module.network.public_subnet_ids
  app_security_group_id     = module.network.app_security_group_id
  domain_name               = var.api_host
  subject_alternative_names = [var.cms_host]
  route53_zone_id           = var.route53_zone_id
  deletion_protection       = true
  kms_key_arn               = module.secrets.kms_key_arn
  tags                      = local.tags

  targets = {
    api = { host = var.api_host, port = 8000, health_path = var.api_health_path, priority = 10 }
    cms = { host = var.cms_host, port = 3000, health_path = var.cms_health_path, priority = 20 }
  }
}

module "app" {
  source = "../../modules/app_services"

  name                    = local.name
  env                     = local.env
  kx_env                  = "production"
  company                 = var.company
  cluster_arn             = module.cluster.cluster_arn
  cluster_name            = module.cluster.cluster_name
  app_repository_url      = module.cluster.repository_urls["kairox/app"]
  cms_repository_url      = module.cluster.repository_urls["kairox/cms"]
  subnet_ids              = module.network.private_subnet_ids
  security_group_id       = module.network.app_security_group_id
  api_target_group_arn    = module.alb.target_group_arns["api"]
  cms_target_group_arn    = module.alb.target_group_arns["cms"]
  api_health_path         = var.api_health_path
  public_api_url          = "https://${var.api_host}"
  public_cms_url          = "https://${var.cms_host}"
  db_secret_arns          = local.db_secret_arns
  valkey_secret_arn       = module.cache.url_secret_arns[var.company]
  integration_secret_arns = local.integration_secret_arns
  generated_secret_arns   = local.generated_secret_arns
  bucket_names            = module.storage.bucket_names
  bucket_arns             = module.storage.bucket_arns
  inbound_bucket_arn      = module.email.inbound_bucket_arn
  ses_identity_arn        = module.email.identity_arn
  ses_configuration_set   = module.email.configuration_set_name
  ses_from_domain         = var.ses_domain
  jwt_issuer              = module.auth.issuer
  jwks_url                = module.auth.jwks_url
  jwt_audience            = module.auth.crm_client_id
  bedrock_region          = var.bedrock_region
  sizes                   = var.service_sizes
  kms_key_arn             = module.secrets.kms_key_arn
  tags                    = local.tags

  db_bootstrap_family = "${local.name}-db-bootstrap"
  extra_environment = {
    KX_CORS_ORIGINS = join(",", var.crm_urls)
  }
}

# ---- one-off: create databases, roles and extensions (idempotent) --------------------
module "db_bootstrap" {
  source = "../../modules/ecs_service"

  name                   = "${local.name}-db-bootstrap"
  cluster_arn            = module.cluster.cluster_arn
  cluster_name           = module.cluster.cluster_name
  image                  = "${module.cluster.repository_urls["kairox/app"]}:${module.app.image_tag}"
  entry_point            = ["/app/scripts/db_bootstrap.sh"]
  cpu                    = 256
  memory                 = 512
  subnet_ids             = module.network.private_subnet_ids
  security_group_ids     = [module.network.app_security_group_id]
  create_service         = false
  enable_execute_command = false
  kms_key_arn            = module.secrets.kms_key_arn
  tags                   = merge(local.tags, { service = "db-bootstrap" })

  environment = {
    PGHOST                = module.database.address
    PGPORT                = tostring(module.database.port)
    PGDATABASE            = "postgres"
    PGSSLMODE             = "require"
    KX_BOOTSTRAP_PREFIXES = ""
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
  alarm_webhook_url         = "https://${var.api_host}/v1/webhooks/alarms"
  alb_arn_suffix            = module.alb.alb_arn_suffix
  target_group_arn_suffixes = module.alb.target_group_arn_suffixes
  db_instance_ids           = [module.database.instance_id]
  db_allocated_storage_gb   = var.db_allocated_storage_gb
  ecs_cluster_name          = module.cluster.cluster_name
  companies                 = [var.company]
  monthly_budget_usd        = var.monthly_budget_usd
  budget_emails             = var.alarm_emails
  kms_key_arn               = module.secrets.kms_key_arn
  tags                      = local.tags

  ecs_services = {
    api    = "${local.name}-api"
    worker = "${local.name}-worker"
    voice  = "${local.name}-voice"
  }
}

module "github_oidc" {
  source               = "../../modules/github_oidc"
  name                 = local.name
  repository           = var.github_repository
  environments         = [var.github_environment]
  create_oidc_provider = var.create_github_oidc_provider
  ecr_repository_arns  = values(module.cluster.repository_arns)
  ecs_cluster_arn      = module.cluster.cluster_arn
  passable_role_prefix = "${local.name}-"
  ssm_image_tag_prefix = "/kx/${local.env}"
  state_bucket_name    = var.state_bucket_name

  promotion_source_account_id = var.staging_account_id
  tags                        = local.tags
}
