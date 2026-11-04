# SES (v2) sending: domain identity with Easy DKIM, custom MAIL FROM, a configuration set whose
# delivery / bounce / complaint events go to an encrypted SNS topic consumed by the automation API.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id       = data.aws_caller_identity.current.account_id
  region           = data.aws_region.current.region
  mail_from_domain = var.mail_from_subdomain == null ? null : "${var.mail_from_subdomain}.${var.domain}"
  manage_dns       = var.route53_zone_id != null
}

resource "aws_sesv2_configuration_set" "this" {
  configuration_set_name = var.name

  delivery_options {
    tls_policy = "REQUIRE"
  }
  reputation_options {
    reputation_metrics_enabled = true
  }
  sending_options {
    sending_enabled = true
  }
  suppression_options {
    suppressed_reasons = ["BOUNCE", "COMPLAINT"]
  }

  tags = var.tags
}

resource "aws_sesv2_email_identity" "this" {
  email_identity         = var.domain
  configuration_set_name = aws_sesv2_configuration_set.this.configuration_set_name

  dkim_signing_attributes {
    next_signing_key_length = "RSA_2048_BIT"
  }

  tags = var.tags
}

resource "aws_sesv2_email_identity_mail_from_attributes" "this" {
  count                  = local.mail_from_domain == null ? 0 : 1
  email_identity         = aws_sesv2_email_identity.this.email_identity
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
  mail_from_domain       = local.mail_from_domain
}

# ---- events -> SNS -> API webhook -------------------------------------------
resource "aws_sns_topic" "events" {
  name              = "${var.name}-ses-events"
  kms_master_key_id = var.kms_key_arn
  tags              = var.tags
}

data "aws_iam_policy_document" "events_topic" {
  statement {
    sid       = "SesPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.events.arn]
    principals {
      type        = "Service"
      identifiers = ["ses.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "events" {
  arn    = aws_sns_topic.events.arn
  policy = data.aws_iam_policy_document.events_topic.json
}

resource "aws_sesv2_configuration_set_event_destination" "sns" {
  configuration_set_name = aws_sesv2_configuration_set.this.configuration_set_name
  event_destination_name = "sns"

  event_destination {
    enabled              = true
    matching_event_types = ["SEND", "DELIVERY", "BOUNCE", "COMPLAINT", "REJECT", "DELIVERY_DELAY", "RENDERING_FAILURE"]
    sns_destination {
      topic_arn = aws_sns_topic.events.arn
    }
  }

  depends_on = [aws_sns_topic_policy.events]
}

resource "aws_sns_topic_subscription" "webhook" {
  count                  = var.event_webhook_url == null ? 0 : 1
  topic_arn              = aws_sns_topic.events.arn
  protocol               = "https"
  endpoint               = var.event_webhook_url
  raw_message_delivery   = false # keep the SNS envelope so the API can verify the SNS signature
  endpoint_auto_confirms = false
}

# ---- DNS (only when the zone is in this account) -----------------------------
resource "aws_route53_record" "dkim" {
  count   = local.manage_dns ? 3 : 0
  zone_id = var.route53_zone_id
  name    = "${aws_sesv2_email_identity.this.dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.domain}"
  type    = "CNAME"
  ttl     = 1800
  records = ["${aws_sesv2_email_identity.this.dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"]
}

resource "aws_route53_record" "mail_from_mx" {
  count   = local.manage_dns && local.mail_from_domain != null ? 1 : 0
  zone_id = var.route53_zone_id
  name    = local.mail_from_domain
  type    = "MX"
  ttl     = 1800
  records = ["10 feedback-smtp.${local.region}.amazonses.com"]
}

resource "aws_route53_record" "mail_from_spf" {
  count   = local.manage_dns && local.mail_from_domain != null ? 1 : 0
  zone_id = var.route53_zone_id
  name    = local.mail_from_domain
  type    = "TXT"
  ttl     = 1800
  records = ["v=spf1 include:amazonses.com ~all"]
}

# ---- inbound (toggle; see variable description) ------------------------------
resource "aws_s3_bucket" "inbound" {
  # checkov:skip=CKV_AWS_144: no cross-region replication (UAE residency)
  # checkov:skip=CKV_AWS_18: access logging via CloudTrail data events
  # checkov:skip=CKV2_AWS_62: the API is notified through SNS by Mail Manager, not S3 events
  # checkov:skip=CKV_AWS_21: raw MIME is write-once, expired after inbound_retention_days
  count  = var.inbound_mail_enabled ? 1 : 0
  bucket = "${var.name}-inbound-mail-${local.account_id}"
  tags   = merge(var.tags, { purpose = "inbound-mail" })
}

resource "aws_s3_bucket_public_access_block" "inbound" {
  count                   = var.inbound_mail_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.inbound[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "inbound" {
  count  = var.inbound_mail_enabled ? 1 : 0
  bucket = aws_s3_bucket.inbound[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "inbound" {
  count  = var.inbound_mail_enabled ? 1 : 0
  bucket = aws_s3_bucket.inbound[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "inbound" {
  count  = var.inbound_mail_enabled ? 1 : 0
  bucket = aws_s3_bucket.inbound[0].id
  rule {
    id     = "expire-raw-mime"
    status = "Enabled"
    filter {}
    expiration {
      days = var.inbound_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "inbound_bucket" {
  count = var.inbound_mail_enabled ? 1 : 0
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.inbound[0].arn, "${aws_s3_bucket.inbound[0].arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "inbound" {
  count  = var.inbound_mail_enabled ? 1 : 0
  bucket = aws_s3_bucket.inbound[0].id
  policy = data.aws_iam_policy_document.inbound_bucket[0].json
}

resource "aws_sns_topic" "inbound" {
  count             = var.inbound_mail_enabled ? 1 : 0
  name              = "${var.name}-inbound-mail"
  kms_master_key_id = var.kms_key_arn
  tags              = var.tags
}

data "aws_iam_policy_document" "mail_manager_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ses.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "mail_manager" {
  count              = var.inbound_mail_enabled ? 1 : 0
  name               = "${var.name}-mail-manager"
  assume_role_policy = data.aws_iam_policy_document.mail_manager_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "mail_manager" {
  count = var.inbound_mail_enabled ? 1 : 0
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.inbound[0].arn}/*"]
  }
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.inbound[0].arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "mail_manager" {
  count  = var.inbound_mail_enabled ? 1 : 0
  name   = "write-inbound"
  role   = aws_iam_role.mail_manager[0].id
  policy = data.aws_iam_policy_document.mail_manager[0].json
}
