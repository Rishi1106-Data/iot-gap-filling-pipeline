# modules/stepfunctions/outputs.tf

output "state_machine_arn" {
  value       = aws_sfn_state_machine.pipeline.arn
  description = "State machine ARN — referenced by EventBridge Scheduler and IAM scheduler role"
}

output "state_machine_name" {
  value       = aws_sfn_state_machine.pipeline.name
}

output "failure_topic_arn" {
  value       = aws_sns_topic.failures.arn
  description = "SNS failure topic ARN — CloudWatch alarms also target this"
}

output "sfn_log_group_name" {
  value       = aws_cloudwatch_log_group.sfn.name
}
