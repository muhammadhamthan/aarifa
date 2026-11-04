output "identity_arn" {
  value = aws_sesv2_email_identity.this.arn
}

output "configuration_set_name" {
  value = aws_sesv2_configuration_set.this.configuration_set_name
}

output "events_topic_arn" {
  value = aws_sns_topic.events.arn
}

output "dns_records_for_client" {
  description = "Records to give the client's DNS admin when the zone is not in this account."
  value = {
    dkim = [
      for t in aws_sesv2_email_identity.this.dkim_signing_attributes[0].tokens :
      { name = "${t}._domainkey.${var.domain}", type = "CNAME", value = "${t}.dkim.amazonses.com" }
    ]
    mail_from = local.mail_from_domain == null ? [] : [
      { name = local.mail_from_domain, type = "MX", value = "10 feedback-smtp.${local.region}.amazonses.com" },
      { name = local.mail_from_domain, type = "TXT", value = "v=spf1 include:amazonses.com ~all" },
    ]
    dmarc_suggestion = { name = "_dmarc.${var.domain}", type = "TXT", value = "v=DMARC1; p=quarantine; rua=mailto:dmarc@${var.domain}" }
  }
}

output "inbound_bucket_name" {
  value = var.inbound_mail_enabled ? aws_s3_bucket.inbound[0].id : null
}

output "inbound_bucket_arn" {
  value = var.inbound_mail_enabled ? aws_s3_bucket.inbound[0].arn : null
}

output "inbound_topic_arn" {
  value = var.inbound_mail_enabled ? aws_sns_topic.inbound[0].arn : null
}

output "mail_manager_role_arn" {
  value = var.inbound_mail_enabled ? aws_iam_role.mail_manager[0].arn : null
}
