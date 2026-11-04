# Public ALB (HTTPS only, host-based routing to api / cms) protected by a regional WAFv2 web ACL:
# AWS managed rule groups + a global per-IP rate limit + a tighter limit on the AI endpoints.

data "aws_caller_identity" "current" {}

locals {
  manage_dns = var.route53_zone_id != null
}

# ---- security group ------------------------------------------------------------
resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Public HTTPS to the ALB"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-alb" })
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet (Vercel frontends, webhooks, browsers)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  # checkov:skip=CKV_AWS_260: port 80 only answers with a 301 redirect to HTTPS
  security_group_id = aws_security_group.alb.id
  description       = "HTTP, redirected to HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "to_targets" {
  for_each                     = { for k, t in var.targets : k => t.port }
  security_group_id            = aws_security_group.alb.id
  description                  = "ALB to ${each.key} tasks"
  referenced_security_group_id = var.app_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
}

resource "aws_vpc_security_group_ingress_rule" "targets_from_alb" {
  for_each                     = { for k, t in var.targets : k => t.port }
  security_group_id            = var.app_security_group_id
  description                  = "${each.key} from the ALB"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
}

# ---- access logs (ELB log delivery cannot write with a CMK: SSE-S3) ---------------
resource "aws_s3_bucket" "logs" {
  # checkov:skip=CKV_AWS_144: no cross-region replication (UAE residency)
  # checkov:skip=CKV_AWS_145: ELB access-log delivery supports SSE-S3 only
  # checkov:skip=CKV_AWS_18: this is the log bucket
  # checkov:skip=CKV_AWS_21: logs are write-once and expire
  # checkov:skip=CKV2_AWS_62: no notifications needed
  bucket        = "${var.name}-alb-logs-${data.aws_caller_identity.current.account_id}"
  force_destroy = !var.deletion_protection
  tags          = merge(var.tags, { purpose = "alb-logs" })
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire-90-days"
    status = "Enabled"
    filter {}
    expiration {
      days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# me-central-1 is a post-2022 region: ELB delivers logs with the service principal.
data "aws_iam_policy_document" "logs" {
  statement {
    sid       = "ElbLogDelivery"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/alb/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }
  }
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]
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

resource "aws_s3_bucket_policy" "logs" {
  bucket     = aws_s3_bucket.logs.id
  policy     = data.aws_iam_policy_document.logs.json
  depends_on = [aws_s3_bucket_public_access_block.logs]
}

# ---- load balancer -----------------------------------------------------------
resource "aws_lb" "this" {
  # checkov:skip=CKV_AWS_150: deletion protection is var.deletion_protection (true in prod, false in staging)
  name                       = var.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = var.public_subnet_ids
  security_groups            = [aws_security_group.alb.id]
  idle_timeout               = var.idle_timeout_seconds
  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
  desync_mitigation_mode     = "defensive"

  access_logs {
    bucket  = aws_s3_bucket.logs.id
    prefix  = "alb"
    enabled = true
  }

  tags       = var.tags
  depends_on = [aws_s3_bucket_policy.logs]
}

resource "aws_lb_target_group" "this" {
  # checkov:skip=CKV_AWS_378: TLS terminates at the ALB; ALB -> task traffic stays inside the VPC
  for_each                      = var.targets
  name                          = substr("${var.name}-${each.key}", 0, 32)
  port                          = each.value.port
  protocol                      = "HTTP"
  target_type                   = "ip"
  vpc_id                        = var.vpc_id
  deregistration_delay          = 30
  load_balancing_algorithm_type = "least_outstanding_requests"

  health_check {
    path                = each.value.health_path
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = var.tags
}

# ---- certificate ---------------------------------------------------------------
resource "aws_acm_certificate" "this" {
  # checkov:skip=CKV2_AWS_71: staging uses one *.staging certificate; prod uses exact names
  domain_name               = var.domain_name
  subject_alternative_names = var.subject_alternative_names
  validation_method         = "DNS"
  key_algorithm             = "RSA_2048"
  tags                      = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "validation" {
  for_each = local.manage_dns ? {
    for o in aws_acm_certificate.this.domain_validation_options : o.domain_name => o
  } : {}
  zone_id         = var.route53_zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  count                   = local.manage_dns ? 1 : 0
  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = [for r in aws_route53_record.validation : r.fqdn]
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.manage_dns ? aws_acm_certificate_validation.this[0].certificate_arn : aws_acm_certificate.this.arn

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      message_body = "{\"detail\":\"not found\"}"
      status_code  = "404"
    }
  }
}

resource "aws_lb_listener_rule" "host" {
  for_each     = var.targets
  listener_arn = aws_lb_listener.https.arn
  priority     = each.value.priority

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this[each.key].arn
  }

  condition {
    host_header {
      values = [each.value.host]
    }
  }

  tags = var.tags
}

resource "aws_route53_record" "alias" {
  for_each = local.manage_dns ? var.targets : {}
  zone_id  = var.route53_zone_id
  name     = each.value.host
  type     = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}

# ---- WAF ---------------------------------------------------------------------------
resource "aws_wafv2_web_acl" "this" {
  name        = var.name
  description = "Kairox ${var.name} API / CMS"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  # 1. Reputation lists first (cheap, blocks known-bad sources)
  rule {
    name     = "aws-ip-reputation"
    priority = 10
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-anonymous-ip"
    priority = 15
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAnonymousIpList"
        # Meta, Twilio, SNS, Vercel and LiveKit call us from cloud IPs: count, never block those.
        rule_action_override {
          name = "HostingProviderIPList"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "anonymous-ip"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-common"
    priority = 20
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
        # Meta/Twilio webhooks, CMS uploads and chat transcripts can exceed 8 KB bodies.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-known-bad-inputs"
    priority = 30
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-sqli"
    priority = 40
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesSQLiRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "sqli"
      sampled_requests_enabled   = true
    }
  }

  # 2. Tight per-IP limit on the endpoints that cost LLM / speech money.
  rule {
    name     = "rate-ai-endpoints"
    priority = 50
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.ai_rate_limit_per_5min
        aggregate_key_type = "IP"
        scope_down_statement {
          or_statement {
            dynamic "statement" {
              # or_statement needs >= 2 statements: the list is padded with a never-matching prefix.
              for_each = length(var.ai_path_prefixes) > 1 ? var.ai_path_prefixes : concat(var.ai_path_prefixes, ["/__never__"])
              content {
                byte_match_statement {
                  search_string         = statement.value
                  positional_constraint = "STARTS_WITH"
                  field_to_match {
                    uri_path {}
                  }
                  text_transformation {
                    priority = 0
                    type     = "LOWERCASE"
                  }
                }
              }
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-ai"
      sampled_requests_enabled   = true
    }
  }

  # 3. Global per-IP limit.
  rule {
    name     = "rate-global"
    priority = 60
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.rate_limit_per_5min
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-global"
      sampled_requests_enabled   = true
    }
  }

  dynamic "rule" {
    for_each = length(var.blocked_country_codes) > 0 ? [1] : []
    content {
      name     = "geo-block"
      priority = 5
      action {
        block {}
      }
      statement {
        geo_match_statement {
          country_codes = var.blocked_country_codes
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "geo-block"
        sampled_requests_enabled   = true
      }
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = var.name
    sampled_requests_enabled   = true
  }

  tags = var.tags
}

resource "aws_wafv2_web_acl_association" "this" {
  resource_arn = aws_lb.this.arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}

# WAF log group names must start with aws-waf-logs-
resource "aws_cloudwatch_log_group" "waf" {
  # checkov:skip=CKV_AWS_338: 30-day retention decided for operational logs (cost); audit trail is exported to S3
  name              = "aws-waf-logs-${var.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

resource "aws_wafv2_web_acl_logging_configuration" "this" {
  resource_arn            = aws_wafv2_web_acl.this.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]

  # Webhook signatures and bearer tokens must never land in logs.
  redacted_fields {
    single_header {
      name = "authorization"
    }
  }
  redacted_fields {
    single_header {
      name = "x-hub-signature-256"
    }
  }
  redacted_fields {
    single_header {
      name = "x-twilio-signature"
    }
  }
}
