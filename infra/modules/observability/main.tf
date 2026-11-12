# CloudWatch alarms -> encrypted SNS topic -> email, plus (optional) Better Stack's CloudWatch integration
# so alarms open incidents there. Better Stack is the primary place for uptime, errors, logs and on-call
# (11 Nov, Recommendation); these alarms + SNS email are the backup path (Fallback plan "Monitoring").
# Log groups live with the resource that writes them (ecs_service, database, alb_waf, network).

data "aws_caller_identity" "current" {}

resource "aws_sns_topic" "alarms" {
  name              = "${var.name}-alarms"
  kms_master_key_id = var.kms_key_arn
  tags              = var.tags
}

data "aws_iam_policy_document" "alarms" {
  statement {
    sid       = "CloudWatchPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alarms.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alarms" {
  arn    = aws_sns_topic.alarms.arn
  policy = data.aws_iam_policy_document.alarms.json
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.alarm_emails)
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_sns_topic_subscription" "webhook" {
  count     = var.alarm_webhook_url == null ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "https"
  endpoint  = var.alarm_webhook_url
}

locals {
  actions = [aws_sns_topic.alarms.arn]
}

# ---- ALB -------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.name}-alb-target-5xx"
  alarm_description   = "API/CMS returned more than 10 5xx responses in 5 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = var.alb_arn_suffix }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "elb_5xx" {
  alarm_name          = "${var.name}-alb-elb-5xx"
  alarm_description   = "ALB itself returned 5xx (no healthy targets, timeouts)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  dimensions          = { LoadBalancer = var.alb_arn_suffix }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_targets" {
  for_each            = var.target_group_arn_suffixes
  alarm_name          = "${var.name}-${each.key}-unhealthy"
  alarm_description   = "${each.key}: at least one unhealthy target for 3 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = var.alb_arn_suffix, TargetGroup = each.value }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

# ---- RDS -------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  for_each            = toset(var.db_instance_ids)
  alarm_name          = "${each.key}-rds-cpu"
  alarm_description   = "RDS CPU above 80% for 15 minutes"
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = each.key }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  for_each            = toset(var.db_instance_ids)
  alarm_name          = "${each.key}-rds-free-storage"
  alarm_description   = "RDS free storage below 15% of allocated (autoscaling may be at its ceiling)"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = each.key }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.db_allocated_storage_gb * 0.15 * 1073741824
  comparison_operator = "LessThanThreshold"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "rds_memory" {
  for_each            = toset(var.db_instance_ids)
  alarm_name          = "${each.key}-rds-freeable-memory"
  alarm_description   = "RDS freeable memory below 256 MB"
  namespace           = "AWS/RDS"
  metric_name         = "FreeableMemory"
  dimensions          = { DBInstanceIdentifier = each.key }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 268435456
  comparison_operator = "LessThanThreshold"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

# ---- ECS -------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "ecs_memory" {
  for_each            = var.ecs_services
  alarm_name          = "${each.value}-memory"
  alarm_description   = "${each.key}: memory above 85% for 10 minutes"
  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  dimensions          = { ClusterName = var.ecs_cluster_name, ServiceName = each.value }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

# ---- queue lag: the app emits Kairox/QueueLagSeconds (Embedded Metric Format, dimension Company) --------
# Approval ladders and follow-ups are rows in automation.scheduled_jobs swept by Celery Beat; a growing lag
# means late reminder calls (or a dead Beat: Better Stack's heartbeat monitor pages first).
resource "aws_cloudwatch_metric_alarm" "queue_lag" {
  for_each            = toset(var.companies)
  alarm_name          = "${var.name}-${each.key}-queue-lag"
  alarm_description   = "${each.key}: oldest due scheduled job older than ${var.queue_lag_threshold_seconds}s"
  namespace           = "Kairox"
  metric_name         = "QueueLagSeconds"
  dimensions          = { Company = each.key }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = var.queue_lag_threshold_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

# ---- vendor switch: the app emits Kairox/VendorSwitch (EMF, dimension Company) on every fallback ------
# Fallback plan: "every switch writes a metric and pages the on-call engineer"; this is the CloudWatch copy.
resource "aws_cloudwatch_metric_alarm" "vendor_switch" {
  for_each            = toset(var.companies)
  alarm_name          = "${var.name}-${each.key}-vendor-switch"
  alarm_description   = "${each.key}: a field switched to its backup vendor in the last 5 minutes (see app logs for which)"
  namespace           = "Kairox"
  metric_name         = "VendorSwitch"
  dimensions          = { Company = each.key }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

# ---- spend cap: the app emits Kairox/SpendCap (value = 80 or 100 percent) when a daily cap is crossed (D34) --
resource "aws_cloudwatch_metric_alarm" "spend_cap" {
  for_each            = toset(var.companies)
  alarm_name          = "${var.name}-${each.key}-spend-cap"
  alarm_description   = "${each.key}: a daily spend cap (AI, voice minutes or WhatsApp templates) reached 80 % or more (see app logs for which)"
  namespace           = "Kairox"
  metric_name         = "SpendCap"
  dimensions          = { Company = each.key }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 80
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
  tags                = var.tags
}

# ---- cost (AWS bill only; vendors billed outside AWS have their own spend caps) ----------------------
resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "ACTUAL"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = var.budget_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "FORECASTED"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = var.budget_emails
  }

  tags = var.tags
}
