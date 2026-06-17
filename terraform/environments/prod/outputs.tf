# environments/prod/outputs.tf
# All values needed for post-deploy verification and CI/CD pipeline integration.

output "data_bucket_name" {
  value       = module.s3.bucket_id
  description = "S3 data bucket name"
}

output "ecr_repository_url" {
  value       = module.ecr.repository_url
  description = "ECR image URI — use in docker push commands"
}

output "state_machine_arn" {
  value       = module.stepfunctions.state_machine_arn
  description = "Step Functions ARN — use in manual execution commands"
}

output "failure_topic_arn" {
  value       = module.stepfunctions.failure_topic_arn
  description = "SNS failure topic ARN"
}

output "ecs_cluster_arn" {
  value       = module.ecs.cluster_arn
}

output "sensor_table_name" {
  value       = module.dynamodb.sensor_table_name
  description = "DynamoDB sensor metadata table name"
}

output "results_table_name" {
  value       = module.dynamodb.results_table_name
  description = "DynamoDB results table name"
}

output "task_security_group_id" {
  value       = module.networking.task_security_group_id
  description = "Security group ID attached to Fargate tasks"
}

output "task_role_arn" {
  value       = module.iam.task_role_arn
}

output "dashboard_url" {
  value       = "https://console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${module.cloudwatch.dashboard_name}"
  description = "Direct link to the CloudWatch operational dashboard"
}

output "run_batch_task_definition_arn" {
  value       = module.ecs.run_batch_task_definition_arn
}

output "daily_training_schedule_arn" {
  value       = module.eventbridge.daily_training_schedule_arn
}
