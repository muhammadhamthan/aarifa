# GitHub Actions -> AWS without long-lived keys. Three roles per account:
#   <name>-gh-deploy     push images, register task defs, run `kx migrate`, roll services (Environments only)
#   <name>-gh-tf-plan    read-only plan on pull requests
#   <name>-gh-tf-apply   apply on main (Environments only, required reviewers; bounded by the org SCP)

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  region       = data.aws_region.current.region
  provider_url = "token.actions.githubusercontent.com"
  provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:aws:iam::${local.account_id}:oidc-provider/${local.provider_url}"
  env_subjects = [for e in var.environments : "repo:${var.repository}:environment:${e}"]
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://${local.provider_url}"
  client_id_list = ["sts.amazonaws.com"]
  tags           = var.tags
}

data "aws_iam_policy_document" "trust_env" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.provider_url}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.provider_url}:sub"
      values   = local.env_subjects
    }
  }
}

data "aws_iam_policy_document" "trust_plan" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.provider_url}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "${local.provider_url}:sub"
      values   = concat(["repo:${var.repository}:pull_request"], local.env_subjects)
    }
  }
}

# ---- deploy -------------------------------------------------------------------------
resource "aws_iam_role" "deploy" {
  name                 = "${var.name}-gh-deploy"
  assume_role_policy   = data.aws_iam_policy_document.trust_env.json
  max_session_duration = 3600
  tags                 = var.tags
}

data "aws_iam_policy_document" "deploy" {
  # checkov:skip=CKV_AWS_111: ecr:GetAuthorizationToken and ecs:RegisterTaskDefinition require resource "*"
  # checkov:skip=CKV_AWS_356: ecr:GetAuthorizationToken and ecs:RegisterTaskDefinition require resource "*"
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
      "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
      "ecr:PutImage", "ecr:UploadLayerPart",
    ]
    resources = var.ecr_repository_arns
  }
  dynamic "statement" {
    for_each = var.promotion_source_account_id == null ? [] : [var.promotion_source_account_id]
    content {
      sid       = "PullFromStagingRegistry"
      actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"]
      resources = ["arn:aws:ecr:${local.region}:${statement.value}:repository/kairox/*"]
    }
  }
  statement {
    sid       = "TaskDefinitions"
    actions   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition", "ecs:ListTaskDefinitions", "ecs:TagResource", "ecs:ListTagsForResource"]
    resources = ["*"]
  }
  statement {
    sid     = "ServicesAndTasks"
    actions = ["ecs:UpdateService", "ecs:DescribeServices", "ecs:RunTask", "ecs:DescribeTasks", "ecs:ListTasks"]
    resources = [
      "arn:aws:ecs:${local.region}:${local.account_id}:service/${split("/", var.ecs_cluster_arn)[1]}/*",
      "arn:aws:ecs:${local.region}:${local.account_id}:task/${split("/", var.ecs_cluster_arn)[1]}/*",
      "arn:aws:ecs:${local.region}:${local.account_id}:task-definition/*",
    ]
  }
  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/${var.passable_role_prefix}*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    sid       = "ImageTagParameters"
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = ["arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.ssm_image_tag_prefix}/*"]
  }
  statement {
    sid       = "ReadMigrateLogs"
    actions   = ["logs:GetLogEvents", "logs:FilterLogEvents"]
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/kx/${var.passable_role_prefix}*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# ---- terraform plan -------------------------------------------------------------------
resource "aws_iam_role" "tf_plan" {
  name               = "${var.name}-gh-tf-plan"
  assume_role_policy = data.aws_iam_policy_document.trust_plan.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "tf_plan_readonly" {
  role       = aws_iam_role.tf_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "tf_plan" {
  # checkov:skip=CKV_AWS_356: kms:Decrypt is limited to keys of this account by kms:CallerAccount
  statement {
    sid       = "StateReadAndLock"
    actions   = ["s3:GetObject", "s3:ListBucket", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.state_bucket_name}", "arn:aws:s3:::${var.state_bucket_name}/*"]
  }
  statement {
    # Refreshing aws_secretsmanager_secret_version reads the value.
    sid       = "RefreshSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:kx/*"]
  }
  statement {
    sid       = "Decrypt"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role_policy" "tf_plan" {
  name   = "plan"
  role   = aws_iam_role.tf_plan.id
  policy = data.aws_iam_policy_document.tf_plan.json
}

# ---- terraform apply -------------------------------------------------------------------
resource "aws_iam_role" "tf_apply" {
  name               = "${var.name}-gh-tf-apply"
  assume_role_policy = data.aws_iam_policy_document.trust_env.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "tf_apply_admin" {
  # checkov:skip=CKV_AWS_274: apply needs admin; bounded by the org region-lock SCP and Environment reviewers
  role       = aws_iam_role.tf_apply.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
