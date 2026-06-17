# modules/networking/outputs.tf

output "task_security_group_id" {
  value       = aws_security_group.tasks.id
  description = "Security group ID to attach to Fargate run-batch tasks"
}

output "vpc_endpoint_security_group_id" {
  value       = aws_security_group.vpc_endpoints.id
  description = "Security group ID attached to interface endpoint ENIs"
}

output "s3_endpoint_id" {
  value       = aws_vpc_endpoint.s3.id
  description = "S3 gateway endpoint ID"
}

output "dynamodb_endpoint_id" {
  value       = aws_vpc_endpoint.dynamodb.id
  description = "DynamoDB gateway endpoint ID"
}

output "interface_endpoint_ids" {
  value       = { for k, v in aws_vpc_endpoint.interfaces : k => v.id }
  description = "Map of service name to interface endpoint ID"
}
