# One customer-managed KMS key per account/environment (RDS storage + PI, S3, Secrets Manager,
# CloudWatch Logs, SNS, ElastiCache) and the Secrets Manager shells for every integration, plus the
# staff-login keys (RS256 signing key, TOTP-seed encryption key) for the in-house login (11 Nov).

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  integration_secrets = merge([
    for c in var.companies : {
      for k, keys in var.integrations : "${c}/${k}" => { company = c, short = k, keys = keys }
    }
  ]...)

  generated_secrets = merge([
    for c in var.companies : {
      for k in var.generated : "${c}/${k}" => { company = c, short = k }
    }
  ]...)
}

data "aws_iam_policy_document" "kms" {
  # checkov:skip=CKV_AWS_109: key policy - root account statement is required so IAM policies can grant use
  # checkov:skip=CKV_AWS_111: key policy - resource "*" means "this key" inside a key policy
  # checkov:skip=CKV_AWS_356: key policy - resource "*" means "this key" inside a key policy
  statement {
    sid       = "AccountRoot"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid = "CloudWatchLogs"
    actions = [
      "kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*",
    ]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${data.aws_partition.current.partition}:logs:${local.region}:${local.account_id}:*"]
    }
  }

  # SNS topics are encrypted with this key; CloudWatch alarms and SES events publish to them.
  statement {
    sid       = "AwsServicesPublishToEncryptedSns"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "ses.amazonaws.com", "events.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

data "aws_iam_policy_document" "kms_full" {
  source_policy_documents = [data.aws_iam_policy_document.kms.json]

  dynamic "statement" {
    for_each = length(var.kms_decrypt_account_ids) > 0 ? [1] : []
    content {
      sid       = "CrossAccountEcrPull"
      actions   = ["kms:Decrypt", "kms:DescribeKey"]
      resources = ["*"]
      principals {
        type        = "AWS"
        identifiers = [for a in var.kms_decrypt_account_ids : "arn:${data.aws_partition.current.partition}:iam::${a}:root"]
      }
      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["ecr.${local.region}.amazonaws.com"]
      }
    }
  }
}

resource "aws_kms_key" "main" {
  description             = "${var.name} platform key (RDS, S3, secrets, logs, SNS, Redis)"
  enable_key_rotation     = true
  deletion_window_in_days = var.kms_deletion_window_days
  policy                  = data.aws_iam_policy_document.kms_full.json
  tags                    = var.tags
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.main.key_id
}

# ---- integration shells -------------------------------------------------
resource "aws_secretsmanager_secret" "integration" {
  # checkov:skip=CKV2_AWS_57: third-party API keys are rotated by hand at the provider, not by Lambda
  for_each                = local.integration_secrets
  name                    = "kx/${var.env}/${each.value.company}/${each.value.short}"
  description             = "${each.value.short} credentials for ${each.value.company} (${var.env}); set by hand"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.recovery_window_days
  tags                    = merge(var.tags, { company = each.value.company })
}

resource "aws_secretsmanager_secret_version" "integration" {
  for_each      = local.integration_secrets
  secret_id     = aws_secretsmanager_secret.integration[each.key].id
  secret_string = jsonencode({ for k in each.value.keys : k => "CHANGE_ME" })

  lifecycle {
    # Real values are put by a human; never overwrite them.
    ignore_changes = [secret_string]
  }
}

# ---- generated ----------------------------------------------------------
resource "random_password" "generated" {
  for_each = local.generated_secrets
  length   = 48
  special  = false
}

resource "aws_secretsmanager_secret" "generated" {
  # checkov:skip=CKV2_AWS_57: rotated by re-creating the random_password (taint) and redeploying
  for_each                = local.generated_secrets
  name                    = "kx/${var.env}/${each.value.company}/${each.value.short}"
  description             = "Generated ${each.value.short} for ${each.value.company} (${var.env})"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.recovery_window_days
  tags                    = merge(var.tags, { company = each.value.company })
}

resource "aws_secretsmanager_secret_version" "generated" {
  for_each      = local.generated_secrets
  secret_id     = aws_secretsmanager_secret.generated[each.key].id
  secret_string = jsonencode({ value = random_password.generated[each.key].result })
}

# ---- staff login (in-house, 11 Nov): RS256 signing key + TOTP-seed encryption key -------------
resource "tls_private_key" "jwt" {
  # checkov:skip=CKV_SECRET_6: generated key, stored only in encrypted state and Secrets Manager
  for_each  = toset(var.companies)
  algorithm = "RSA"
  rsa_bits  = 3072
}

resource "aws_secretsmanager_secret" "jwt" {
  # checkov:skip=CKV2_AWS_57: rotated by tainting tls_private_key.jwt and bumping jwt_key_generation
  for_each                = toset(var.companies)
  name                    = "kx/${var.env}/${each.key}/jwt"
  description             = "RS256 key that signs staff access tokens for ${each.key} (KX_JWT_PRIVATE_KEY, KX_JWT_KEY_ID)"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.recovery_window_days
  tags                    = merge(var.tags, { company = each.key })
}

resource "aws_secretsmanager_secret_version" "jwt" {
  for_each  = toset(var.companies)
  secret_id = aws_secretsmanager_secret.jwt[each.key].id
  secret_string = jsonencode({
    private_key_pem = tls_private_key.jwt[each.key].private_key_pem
    public_key_pem  = tls_private_key.jwt[each.key].public_key_pem
    key_id          = "${var.jwt_key_id_prefix}-${each.key}-${var.jwt_key_generation}"
  })
}

# Fernet-format key (url-safe base64 of 32 random bytes) that encrypts TOTP seeds at rest.
resource "random_bytes" "auth_encryption" {
  for_each = toset(var.companies)
  length   = 32
}

resource "aws_secretsmanager_secret" "auth" {
  # checkov:skip=CKV2_AWS_57: rotating it means re-enrolling every authenticator app; done by hand
  for_each                = toset(var.companies)
  name                    = "kx/${var.env}/${each.key}/auth"
  description             = "Staff-login encryption key for ${each.key} (KX_AUTH__ENCRYPTION_KEY)"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.recovery_window_days
  tags                    = merge(var.tags, { company = each.key })
}

resource "aws_secretsmanager_secret_version" "auth" {
  for_each  = toset(var.companies)
  secret_id = aws_secretsmanager_secret.auth[each.key].id
  secret_string = jsonencode({
    encryption_key = replace(replace(random_bytes.auth_encryption[each.key].base64, "+", "-"), "/", "_")
  })

  lifecycle {
    # A new key would make every enrolled TOTP seed unreadable: never replace it silently.
    ignore_changes = [secret_string]
  }
}
