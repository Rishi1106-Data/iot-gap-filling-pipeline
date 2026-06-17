# modules/drift_detection/outputs.tf

output "drift_state_machine_arn" {
  value       = aws_sfn_state_machine.drift_check.arn
  description = "Drift detection state machine ARN"
}

output "drift_state_machine_name" {
  value       = aws_sfn_state_machine.drift_check.name
}

output "drift_sfn_role_arn" {
  value       = aws_iam_role.drift_sfn.arn
  description = "IAM role ARN used by the drift check state machine"
}

output "drift_sfn_role_name" {
  value       = aws_iam_role.drift_sfn.name
}

output "drift_schedule_arn" {
  value       = aws_scheduler_schedule.drift_check_daily.arn
  description = "EventBridge schedule ARN for daily drift checks"
}

output "alarm_arns" {
  value = {
    drift_sfn_failed           = aws_cloudwatch_metric_alarm.drift_sfn_failed.arn
    drift_forced_retrain_spike = aws_cloudwatch_metric_alarm.drift_forced_retrain_spike.arn
  }
  description = "Map of drift-related alarm names to ARNs"
}
