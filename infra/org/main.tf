# Region-lock SCP for the Kairox workload accounts.
#  1. Everything regional is denied outside me-central-1 (+ dr_copy_region when a client consented).
#     Global services are exempt via NotAction: IAM, STS, Organizations, CloudFront, Route 53,
#     WAF (classic global), Support, Budgets, Cost Explorer, Health, Account.
#  2. ACM and WAFv2 are additionally allowed in us-east-1 only (CloudFront certificates / web ACLs).
#  3. Bedrock is additionally allowed in var.bedrock_regions (cross-region inference profiles).

locals {
  regions = distinct(compact(concat(var.allowed_regions, [var.dr_copy_region])))

  global_actions = [
    "iam:*", "sts:*", "organizations:*", "account:*",
    "cloudfront:*", "route53:*", "route53domains:*", "waf:*",
    "support:*", "budgets:*", "ce:*", "cur:*", "health:*", "trustedadvisor:*",
    "pricing:*", "tag:GetResources", "sso:*", "identitystore:*",
  ]
  us_east_1_actions = ["acm:*", "wafv2:*"]
  bedrock_actions   = ["bedrock:*"]
}

data "aws_iam_policy_document" "region_lock" {
  statement {
    sid         = "DenyOutsideAllowedRegions"
    effect      = "Deny"
    not_actions = concat(local.global_actions, local.us_east_1_actions, local.bedrock_actions)
    resources   = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = local.regions
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values   = var.exempt_role_arn_patterns
    }
  }

  statement {
    sid       = "AcmAndWafv2OnlyHereOrUsEast1"
    effect    = "Deny"
    actions   = local.us_east_1_actions
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = distinct(concat(local.regions, ["us-east-1"]))
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values   = var.exempt_role_arn_patterns
    }
  }

  statement {
    sid       = "BedrockOnlyInApprovedRegions"
    effect    = "Deny"
    actions   = local.bedrock_actions
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = distinct(concat(local.regions, var.bedrock_regions))
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values   = var.exempt_role_arn_patterns
    }
  }

  # Accounts must not leave the organization (would drop the SCP).
  statement {
    sid       = "DenyLeaveOrganization"
    effect    = "Deny"
    actions   = ["organizations:LeaveOrganization"]
    resources = ["*"]
  }
}

resource "aws_organizations_policy" "region_lock" {
  name        = "kx-region-lock-uae"
  description = "Kairox: deny regional services outside me-central-1 (UAE) except global services"
  type        = "SERVICE_CONTROL_POLICY"
  content     = data.aws_iam_policy_document.region_lock.json
}

resource "aws_organizations_policy_attachment" "region_lock" {
  for_each  = toset(var.target_ids)
  policy_id = aws_organizations_policy.region_lock.id
  target_id = each.value
}
