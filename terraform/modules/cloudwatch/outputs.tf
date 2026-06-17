# modules/cloudwatch/outputs.tf

output "dashboard_name" {
  value       = aws_cloudwatch_dashboard.pipeline.dashboard_name
  description = "CloudWatch dashboard name"
}

output "alarm_arns" {
  value = {
    sensors_failed_high  = aws_cloudwatch_metric_alarm.sensors_failed_high.arn
    sfn_executions_failed = aws_cloudwatch_metric_alarm.sfn_executions_failed.arn
    batch_duration_high  = aws_cloudwatch_metric_alarm.batch_duration_high.arn
    unresolved_rows_high = aws_cloudwatch_metric_alarm.unresolved_rows_high.arn
    no_sensors_processed = aws_cloudwatch_metric_alarm.no_sensors_processed.arn
  }
  description = "Map of alarm names to ARNs"
}
