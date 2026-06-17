# modules/cost/outputs.tf

output "cost_attribution_table_name" {
  value       = aws_dynamodb_table.cost_attribution.name
  description = "Per-sensor cost attribution table name — set as COST_ATTRIBUTION_TABLE env var"
}

output "cost_attribution_table_arn" {
  value       = aws_dynamodb_table.cost_attribution.arn
  description = "Cost attribution table ARN — used in IAM task role policy extension"
}

output "cost_dashboard_name" {
  value       = aws_cloudwatch_dashboard.cost.dashboard_name
  description = "CloudWatch cost dashboard name"
}

output "budget_name" {
  value       = aws_budgets_budget.pipeline_monthly.name
  description = "AWS Budget name"
}

output "alarm_arns" {
  value = {
    daily_cost_high        = aws_cloudwatch_metric_alarm.daily_cost_high.arn
    cost_per_sensor_high   = aws_cloudwatch_metric_alarm.cost_per_sensor_high.arn
  }
}
