output "scp_id" {
  value = aws_organizations_policy.region_lock.id
}

output "effective_regions" {
  value = local.regions
}
