# One ECS Fargate (Graviton) service or one-off task definition, with its own log group, execution
# role (pulls the image, reads exactly the secrets it is given) and task role.

data "aws_region" "current" {}

locals {
  # valueFrom may carry ":<json-key>:<version-stage>:<version-id>" - IAM needs the bare secret ARN.
  secret_arns = distinct([for v in values(var.secrets) : join(":", slice(split(":", v), 0, 7))])

  container = merge(
    {
      name                   = var.container_name
      image                  = var.image
      essential              = true
      readonlyRootFilesystem = var.readonly_root_filesystem
      stopTimeout            = var.stop_timeout_seconds
      environment            = [for k in sort(keys(var.environment)) : { name = k, value = var.environment[k] }]
      secrets                = [for k in sort(keys(var.secrets)) : { name = k, valueFrom = var.secrets[k] }]
      portMappings           = var.port == null ? [] : [{ containerPort = var.port, protocol = "tcp" }]
      mountPoints            = var.readonly_root_filesystem ? [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }] : []
      linuxParameters        = { initProcessEnabled = true }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = data.aws_region.current.region
          "awslogs-stream-prefix" = var.container_name
          "mode"                  = "non-blocking"
          "max-buffer-size"       = "25m"
        }
      }
    },
    var.command == null ? {} : { command = var.command },
    var.entry_point == null ? {} : { entryPoint = var.entry_point },
    var.container_health_check == null ? {} : {
      healthCheck = { command = var.container_health_check, interval = 30, timeout = 5, retries = 3, startPeriod = 30 }
    },
  )
}

resource "aws_cloudwatch_log_group" "this" {
  # checkov:skip=CKV_AWS_338: 30-day retention decided for operational logs (cost); audit trail is exported to S3
  name              = "/kx/${var.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

# ---- IAM ------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution" {
  count = length(local.secret_arns) > 0 ? 1 : 0
  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.secret_arns
  }
  statement {
    sid       = "DecryptSecrets"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "execution" {
  count  = length(local.secret_arns) > 0 ? 1 : 0
  name   = "read-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution[0].json
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "exec_command" {
  # checkov:skip=CKV_AWS_111: ssmmessages and logs:Describe* do not support resource-level permissions
  # checkov:skip=CKV_AWS_356: ssmmessages and logs:Describe* do not support resource-level permissions
  statement {
    sid = "EcsExec"
    actions = [
      "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "EcsExecKms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
  statement {
    sid       = "EcsExecLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "exec_command" {
  count  = var.enable_execute_command ? 1 : 0
  name   = "ecs-exec"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.exec_command.json
}

resource "aws_iam_role_policy" "task" {
  count  = var.task_role_policy_json == null ? 0 : 1
  name   = "app"
  role   = aws_iam_role.task.id
  policy = var.task_role_policy_json
}

# ---- task definition --------------------------------------------------------
resource "aws_ecs_task_definition" "this" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions    = jsonencode([local.container])

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  dynamic "volume" {
    for_each = var.readonly_root_filesystem ? ["tmp"] : []
    content {
      name = volume.value
    }
  }

  tags = var.tags
}

# ---- service ----------------------------------------------------------------
resource "aws_ecs_service" "this" {
  count                   = var.create_service ? 1 : 0
  name                    = var.name
  cluster                 = var.cluster_arn
  task_definition         = aws_ecs_task_definition.this.arn
  desired_count           = var.desired_count
  enable_execute_command  = var.enable_execute_command
  enable_ecs_managed_tags = true
  propagate_tags          = "SERVICE"
  platform_version        = "LATEST"

  # Fargate spreads tasks across the given subnets/AZs; rebalancing keeps api at one task per AZ.
  availability_zone_rebalancing = "ENABLED"

  capacity_provider_strategy {
    capacity_provider = var.capacity_provider
    weight            = 1
  }

  # Celery Beat runs 0/100 (stop the old task before starting the new one): two Beats must never overlap.
  deployment_minimum_healthy_percent = var.deployment_minimum_healthy_percent
  deployment_maximum_percent         = var.deployment_maximum_percent
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = false
  }

  dynamic "service_registries" {
    for_each = var.service_discovery_namespace_id == null ? [] : [1]
    content {
      registry_arn = aws_service_discovery_service.this[0].arn
    }
  }

  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [var.target_group_arn]
    content {
      target_group_arn = load_balancer.value
      container_name   = var.container_name
      container_port   = var.port
    }
  }

  health_check_grace_period_seconds = var.target_group_arn == null ? null : var.health_check_grace_seconds

  tags = var.tags

  lifecycle {
    ignore_changes = [desired_count]
  }
}

# ---- optional private DNS name (<discovery_name>.<namespace>) for services reached inside the VPC ----
resource "aws_service_discovery_service" "this" {
  count = var.create_service && var.service_discovery_namespace_id != null ? 1 : 0
  name  = coalesce(var.discovery_name, var.container_name)

  dns_config {
    namespace_id   = var.service_discovery_namespace_id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }

  tags = var.tags
}

resource "aws_appautoscaling_target" "this" {
  count              = var.create_service ? 1 : 0
  service_namespace  = "ecs"
  resource_id        = "service/${var.cluster_name}/${aws_ecs_service.this[0].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_count
  max_capacity       = var.max_count
}

resource "aws_appautoscaling_policy" "cpu" {
  count              = var.create_service && var.max_count > var.min_count ? 1 : 0
  name               = "${var.name}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.this[0].service_namespace
  resource_id        = aws_appautoscaling_target.this[0].resource_id
  scalable_dimension = aws_appautoscaling_target.this[0].scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = var.cpu_target_percent
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
