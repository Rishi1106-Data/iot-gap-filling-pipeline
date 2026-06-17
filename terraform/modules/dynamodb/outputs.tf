# modules/dynamodb/outputs.tf

output "sensor_table_name" {
  value       = aws_dynamodb_table.sensor_metadata.name
  description = "Sensor metadata table name — set as SENSOR_TABLE env var on tasks"
}

output "sensor_table_arn" {
  value       = aws_dynamodb_table.sensor_metadata.arn
  description = "Sensor metadata table ARN — used in IAM task role policy"
}

output "sensor_table_gsi_arn" {
  value       = "${aws_dynamodb_table.sensor_metadata.arn}/index/gsi_status"
  description = "GSI ARN — must be granted separately in IAM for Query on the index"
}

output "results_table_name" {
  value       = aws_dynamodb_table.gapfill_results.name
  description = "Results table name — set as RESULTS_TABLE env var on tasks"
}

output "results_table_arn" {
  value       = aws_dynamodb_table.gapfill_results.arn
  description = "Results table ARN — used in IAM task role policy"
}
