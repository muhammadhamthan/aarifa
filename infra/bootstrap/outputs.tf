output "bucket_name" {
  value = aws_s3_bucket.state.id
}

output "kms_key_arn" {
  value = aws_kms_key.state.arn
}
