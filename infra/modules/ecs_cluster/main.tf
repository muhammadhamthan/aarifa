resource "aws_cloudwatch_log_group" "exec" {
  # checkov:skip=CKV_AWS_338: 30-day retention decided for operational logs (cost); audit trail is exported to S3
  name              = "/kx/${var.name}/ecs-exec"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

resource "aws_ecs_cluster" "this" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = var.container_insights
  }

  # `aws ecs execute-command` sessions are encrypted and logged (security audit trail).
  configuration {
    execute_command_configuration {
      kms_key_id = var.kms_key_arn
      logging    = "OVERRIDE"
      log_configuration {
        cloud_watch_encryption_enabled = true
        cloud_watch_log_group_name     = aws_cloudwatch_log_group.exec.name
      }
    }
  }

  tags = var.tags
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 0
  }
}

resource "aws_ecr_repository" "this" {
  for_each             = toset(var.ecr_repositories)
  name                 = each.key
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Drop untagged layers after 7 days"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 7 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the last ${var.keep_images} images"
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = var.keep_images }
        action       = { type = "expire" }
      },
    ]
  })
}

data "aws_iam_policy_document" "pull" {
  count = length(var.pull_account_ids) > 0 ? 1 : 0
  statement {
    sid = "CrossAccountPull"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages",
    ]
    principals {
      type        = "AWS"
      identifiers = [for a in var.pull_account_ids : "arn:aws:iam::${a}:root"]
    }
  }
}

resource "aws_ecr_repository_policy" "pull" {
  for_each   = length(var.pull_account_ids) > 0 ? aws_ecr_repository.this : {}
  repository = each.value.name
  policy     = data.aws_iam_policy_document.pull[0].json
}
