# modules/eventbridge/outputs.tf

output "daily_training_schedule_arn" {
  value       = aws_scheduler_schedule.daily_training.arn
  description = "ARN of the daily training schedule"
}

output "hourly_inference_schedule_arn" {
  value       = aws_scheduler_schedule.hourly_inference.arn
  description = "ARN of the hourly inference schedule (disabled by default)"
}

output "schedule_group_name" {
  value       = aws_scheduler_schedule_group.pipeline.name
}
