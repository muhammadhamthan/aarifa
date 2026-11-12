output "task_definition_arn" {
  value = aws_ecs_task_definition.this.arn
}

output "task_definition_family" {
  value = aws_ecs_task_definition.this.family
}

output "service_name" {
  value = var.create_service ? aws_ecs_service.this[0].name : null
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.this.name
}

output "discovery_name" {
  description = "Private DNS label when service discovery is on, else null."
  value       = var.create_service && var.service_discovery_namespace_id != null ? aws_service_discovery_service.this[0].name : null
}
