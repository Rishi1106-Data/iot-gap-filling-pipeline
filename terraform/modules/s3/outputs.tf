# modules/s3/outputs.tf

output "bucket_id" {
  value       = aws_s3_bucket.data.id
  description = "S3 bucket name"
}

output "bucket_arn" {
  value       = aws_s3_bucket.data.arn
  description = "S3 bucket ARN — used in IAM policies"
}

output "bucket_regional_domain_name" {
  value       = aws_s3_bucket.data.bucket_regional_domain_name
  description = "Regional domain name (useful for VPC endpoint bucket policies)"
}
