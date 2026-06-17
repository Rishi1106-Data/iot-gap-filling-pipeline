# environments/prod/outputs_extensions.tf
# New outputs from the three extension modules.

output "model_registry_table_name" {
  value       = module.model_registry.table_name
  description = "Model registry DynamoDB table name"
}

output "cost_attribution_table_name" {
  value       = module.cost.cost_attribution_table_name
  description = "Per-sensor cost attribution DynamoDB table name"
}

output "cost_dashboard_url" {
  value       = "https://console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${module.cost.cost_dashboard_name}"
  description = "Direct link to the CloudWatch cost dashboard"
}

output "drift_state_machine_arn" {
  value       = module.drift_detection.drift_state_machine_arn
  description = "Drift detection state machine ARN"
}

output "drift_schedule_arn" {
  value       = module.drift_detection.drift_schedule_arn
  description = "EventBridge schedule ARN for daily drift checks"
}

output "model_registry_ssm_path" {
  value       = aws_ssm_parameter.model_registry_table_name.name
  description = "SSM parameter path for the model registry table name"
}

output "budget_name" {
  value       = module.cost.budget_name
  description = "AWS Budget name for monthly spend alert"
}
