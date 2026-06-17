# modules/ecs/outputs.tf

output "cluster_arn" {
  value       = aws_ecs_cluster.this.arn
  description = "ECS cluster ARN — referenced in Step Functions task runner"
}

output "cluster_name" {
  value       = aws_ecs_cluster.this.name
}

output "run_batch_task_definition_arn" {
  value       = aws_ecs_task_definition.run_batch.arn
  description = "run-batch task definition ARN (latest revision)"
}

output "list_sensors_task_definition_arn" {
  value       = aws_ecs_task_definition.list_sensors.arn
  description = "list-sensors task definition ARN (latest revision)"
}

output "run_batch_log_group_name" {
  value       = aws_cloudwatch_log_group.run_batch.name
}

output "list_sensors_log_group_name" {
  value       = aws_cloudwatch_log_group.list_sensors.name
}
