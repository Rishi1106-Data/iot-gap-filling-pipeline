# modules/ecr/outputs.tf

output "repository_url" {
  value       = aws_ecr_repository.this.repository_url
  description = "Full ECR repository URL (e.g. 123456789.dkr.ecr.ap-south-1.amazonaws.com/annam-gapfill)"
}

output "repository_arn" {
  value       = aws_ecr_repository.this.arn
  description = "ECR repository ARN"
}

output "repository_name" {
  value       = aws_ecr_repository.this.name
  description = "ECR repository name"
}
