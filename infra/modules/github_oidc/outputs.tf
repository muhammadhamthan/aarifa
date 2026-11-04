output "deploy_role_arn" {
  description = "GitHub Environment variable AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.deploy.arn
}

output "tf_plan_role_arn" {
  description = "Repository variable TF_PLAN_ROLE_<ENV>."
  value       = aws_iam_role.tf_plan.arn
}

output "tf_apply_role_arn" {
  description = "GitHub Environment variable TF_APPLY_ROLE_ARN."
  value       = aws_iam_role.tf_apply.arn
}
