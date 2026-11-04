# Cognito user pool for CRM staff: admin-created users only, TOTP MFA REQUIRED (approvals need
# require_human_mfa), groups md/manager/admin/sales. Email uses the Cognito default sender: a
# community report says Cognito in me-central-1 cannot use a UAE SES identity (unconfirmed) - staff
# volume is tiny, so the default sender is fine.

resource "aws_cognito_user_pool" "this" {
  name                = var.name
  deletion_protection = var.deletion_protection ? "ACTIVE" : "INACTIVE"
  user_pool_tier      = var.user_pool_tier

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  mfa_configuration = "ON"
  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  username_configuration {
    case_sensitive = false
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 3
      max_length = 254
    }
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [schema] # schema cannot change after creation
  }
}

resource "aws_cognito_user_group" "this" {
  for_each     = var.groups
  user_pool_id = aws_cognito_user_pool.this.id
  name         = each.key
  precedence   = each.value
  description  = "CRM role ${each.key}"
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

# Browser SPA client (CRM on Vercel): authorization code + PKCE, no client secret.
resource "aws_cognito_user_pool_client" "crm" {
  name         = "${var.name}-crm"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls

  explicit_auth_flows           = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = var.access_token_minutes
  id_token_validity      = var.access_token_minutes
  refresh_token_validity = var.refresh_token_days
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}
