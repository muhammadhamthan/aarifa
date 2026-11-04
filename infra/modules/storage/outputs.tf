output "bucket_names" {
  value = { for k, b in aws_s3_bucket.this : k => b.id }
}

output "bucket_arns" {
  value = { for k, b in aws_s3_bucket.this : k => b.arn }
}

output "media_cdn_domain" {
  value = local.cdn ? aws_cloudfront_distribution.media[0].domain_name : null
}
