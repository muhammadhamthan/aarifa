output "service_names" {
  value = {
    api    = module.api.service_name
    worker = module.worker.service_name
    voice  = module.voice.service_name
    cms    = module.cms.service_name
  }
}

output "migrate_task_family" {
  value = module.migrate.task_definition_family
}

output "image_tag_parameters" {
  value = {
    app = aws_ssm_parameter.app_tag.name
    cms = aws_ssm_parameter.cms_tag.name
  }
}

output "deploy_manifest" {
  description = "What deploy.yml needs (also written to SSM /kx/<env>/<company>/deploy)."
  value       = local.deploy_manifest
}

output "image_tag" {
  description = "Currently deployed app image tag (from SSM)."
  value       = local.app_tag
}
