output "alb_arn" {
  value = aws_lb.this.arn
}

output "alb_arn_suffix" {
  description = "For CloudWatch alarm dimensions."
  value       = aws_lb.this.arn_suffix
}

output "alb_dns_name" {
  description = "CNAME target when the client manages DNS."
  value       = aws_lb.this.dns_name
}

output "security_group_id" {
  value = aws_security_group.alb.id
}

output "target_group_arns" {
  value = { for k, tg in aws_lb_target_group.this : k => tg.arn }
}

output "target_group_arn_suffixes" {
  value = { for k, tg in aws_lb_target_group.this : k => tg.arn_suffix }
}

output "certificate_arn" {
  value = aws_acm_certificate.this.arn
}

output "certificate_validation_records" {
  description = "Give these to the client's DNS admin when route53_zone_id is null."
  value = [
    for o in aws_acm_certificate.this.domain_validation_options :
    { name = o.resource_record_name, type = o.resource_record_type, value = o.resource_record_value }
  ]
}

output "web_acl_arn" {
  value = aws_wafv2_web_acl.this.arn
}
