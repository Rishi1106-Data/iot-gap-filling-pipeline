# modules/iam/outputs.tf

output "task_role_arn" {
  value       = aws_iam_role.task.arn
  description = "Task IAM role ARN — set as taskRoleArn in ECS task definitions"
}

output "task_role_name" {
  value       = aws_iam_role.task.name
}

output "execution_role_arn" {
  value       = aws_iam_role.execution.arn
  description = "Execution IAM role ARN — set as executionRoleArn in ECS task definitions"
}

output "execution_role_name" {
  value       = aws_iam_role.execution.name
}

output "sfn_role_arn" {
  value       = aws_iam_role.sfn.arn
  description = "Step Functions role ARN — pass when creating the state machine"
}

output "sfn_role_name" {
  value       = aws_iam_role.sfn.name
}

output "scheduler_role_arn" {
  value       = aws_iam_role.scheduler.arn
  description = "Scheduler role ARN — pass when creating EventBridge schedules"
}

output "scheduler_role_name" {
  value       = aws_iam_role.scheduler.name
}
