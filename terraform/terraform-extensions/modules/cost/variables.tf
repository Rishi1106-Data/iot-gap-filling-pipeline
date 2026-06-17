# modules/cost/variables.tf

variable "name_prefix" {
  type        = string
  description = "Resource name prefix matching other modules (e.g. annam-prod)"
}

variable "environment" {
  type        = string
  description = "Environment label shown on the dashboard header"
}

variable "cw_namespace" {
  type        = string
  default     = "AnnamAI/GapFilling"
}

variable "failure_topic_arn" {
  type        = string
  description = "SNS topic ARN that cost alarms and budget notifications publish to"
}

variable "run_batch_log_group_name" {
  type        = string
  description = "CloudWatch log group for run-batch tasks — used in Logs Insights query. From ecs module output run_batch_log_group_name."
}

variable "monthly_budget_usd" {
  type        = number
  description = "Monthly AWS spend budget in USD. Triggers SNS alert at 80% actual and 100% forecasted."
  default     = 300  # $300 comfortably covers 10k sensors/daily at Spot rates + headroom
}

variable "daily_cost_alarm_usd" {
  type        = number
  description = "Daily TaskCostUSD sum that triggers a P2 alarm. Default covers 1000 sensors."
  default     = 15  # ~$14.3/day for 1000 sensors; $15 gives headroom for occasional retrain spikes
}

variable "cost_per_sensor_alarm_usd" {
  type        = number
  description = "Average CostPerSensorUSD that triggers a P2 alarm. Spike = stuck job or Spot thrash."
  default     = 0.005  # 10x normal cost of ~$0.000477/sensor
}

variable "attribution_retention_days" {
  type        = number
  description = "Days to retain per-sensor cost attribution records (TTL)"
  default     = 90
}

variable "tags" {
  type        = map(string)
  default     = {}
}
