# Private, KMS-encrypted application buckets. All access is via the ECS task roles (presigned URLs
# for browser downloads); nothing is public.

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  names      = { for k, _ in var.buckets : k => "${var.name}-${replace(k, "_", "-")}-${local.account_id}" }
  versioned  = { for k, b in var.buckets : k => b if b.versioning }
  locked     = { for k, b in var.buckets : k => b if b.object_lock_days != null }
  cdn        = var.enable_media_cdn && contains(keys(var.buckets), var.cdn_bucket)
}

resource "aws_s3_bucket" "this" {
  # checkov:skip=CKV_AWS_144: cross-region replication is deliberately off (UAE data residency)
  # checkov:skip=CKV_AWS_18: server access logs are replaced by CloudTrail data events at org level
  # checkov:skip=CKV2_AWS_62: event notifications are not needed for these buckets
  # checkov:skip=CKV2_AWS_6: public access block is aws_s3_bucket_public_access_block.this (for_each, not linked by the graph)
  # checkov:skip=CKV2_AWS_61: lifecycle is aws_s3_bucket_lifecycle_configuration.this (for_each)
  # checkov:skip=CKV_AWS_145: SSE-KMS with the platform CMK is aws_s3_bucket_server_side_encryption_configuration.this
  # checkov:skip=CKV_AWS_21: versioning on except the short-lived exports bucket
  for_each            = var.buckets
  bucket              = local.names[each.key]
  force_destroy       = var.force_destroy
  object_lock_enabled = each.value.object_lock_days != null
  tags                = merge(var.tags, { purpose = each.key })
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = aws_s3_bucket.this
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = local.versioned
  bucket   = aws_s3_bucket.this[each.key].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "this" {
  for_each = local.locked
  bucket   = aws_s3_bucket.this[each.key].id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = each.value.object_lock_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }

  dynamic "rule" {
    for_each = var.buckets[each.key].expiration_days == null ? [] : [var.buckets[each.key].expiration_days]
    content {
      id     = "expire-after-${rule.value}-days"
      status = "Enabled"
      filter {}
      expiration {
        days = rule.value
      }
    }
  }

  dynamic "rule" {
    for_each = var.buckets[each.key].versioning ? [var.buckets[each.key].noncurrent_expiration_days] : []
    content {
      id     = "expire-noncurrent"
      status = "Enabled"
      filter {}
      noncurrent_version_expiration {
        noncurrent_days = rule.value
      }
      expiration {
        expired_object_delete_marker = true
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_cors_configuration" "this" {
  for_each = length(var.cors_allowed_origins) > 0 ? aws_s3_bucket.this : {}
  bucket   = each.value.id
  cors_rule {
    allowed_methods = ["GET", "PUT"]
    allowed_origins = var.cors_allowed_origins
    allowed_headers = ["*"]
    max_age_seconds = 3600
  }
}

data "aws_iam_policy_document" "bucket" {
  for_each = aws_s3_bucket.this

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      each.value.arn,
      "${each.value.arn}/*",
    ]
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

  dynamic "statement" {
    for_each = local.cdn && each.key == var.cdn_bucket ? [1] : []
    content {
      sid       = "CloudFrontOacRead"
      actions   = ["s3:GetObject"]
      resources = ["${each.value.arn}/*"]
      principals {
        type        = "Service"
        identifiers = ["cloudfront.amazonaws.com"]
      }
      condition {
        test     = "StringEquals"
        variable = "AWS:SourceArn"
        values   = [aws_cloudfront_distribution.media[0].arn]
      }
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  for_each   = aws_s3_bucket.this
  bucket     = each.value.id
  policy     = data.aws_iam_policy_document.bucket[each.key].json
  depends_on = [aws_s3_bucket_public_access_block.this]
}

# ---- optional media CDN (OFF by default) -------------------------------------
# When enabling: the CMK key policy must also allow cloudfront.amazonaws.com kms:Decrypt with
# AWS:SourceArn = this distribution (SSE-KMS objects), or keep the media bucket on SSE-S3.
resource "aws_cloudfront_origin_access_control" "media" {
  count                             = local.cdn ? 1 : 0
  name                              = "${var.name}-media"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "media" {
  # checkov:skip=CKV_AWS_68: WAF for CloudFront must live in us-east-1; media is static, read-only
  # checkov:skip=CKV_AWS_86: access logging optional for static media
  # checkov:skip=CKV_AWS_310: single S3 origin, no failover origin
  # checkov:skip=CKV_AWS_374: no geo restriction - buyers are worldwide
  # checkov:skip=CKV2_AWS_32: response headers policy set by the website (Vercel)
  # checkov:skip=CKV2_AWS_42: default certificate on the *.cloudfront.net name
  # checkov:skip=CKV_AWS_174: with the default certificate CloudFront fixes the TLS policy; set a custom cert when enabling
  # checkov:skip=CKV_AWS_305: media objects only, no index document
  # checkov:skip=CKV2_AWS_47: no WAF (see CKV_AWS_68)
  count           = local.cdn ? 1 : 0
  enabled         = true
  comment         = "${var.name} media"
  is_ipv6_enabled = true
  price_class     = "PriceClass_All"

  origin {
    origin_id                = "media"
    domain_name              = aws_s3_bucket.this[var.cdn_bucket].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.media[0].id
  }

  default_cache_behavior {
    target_origin_id       = "media"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1.2_2021"
  }

  tags = var.tags
}
