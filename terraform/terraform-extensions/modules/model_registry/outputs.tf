# modules/model_registry/outputs.tf

output "table_name" {
  value       = aws_dynamodb_table.model_registry.name
  description = "Model registry table name — set as MODEL_REGISTRY_TABLE env var on Fargate tasks"
}

output "table_arn" {
  value       = aws_dynamodb_table.model_registry.arn
  description = "Model registry table ARN — used in IAM policy extensions"
}

output "gsi_status_trained_arn" {
  value       = "${aws_dynamodb_table.model_registry.arn}/index/gsi_status_trained"
  description = "GSI ARN for querying active models by training date (stale model detection)"
}

output "gsi_next_train_arn" {
  value       = "${aws_dynamodb_table.model_registry.arn}/index/gsi_next_train"
  description = "GSI ARN for querying sensors due for retraining"
}
