# environments/prod/variables_extensions.tf
#
# New variables added by the extensions. Add to terraform.tfvars as needed.
# All have sensible defaults — no values are required to apply extensions.

variable "monthly_budget_usd" {
  type        = number
  description = "Monthly AWS spend budget in USD. SNS alert fires at 80% actual and 100% forecasted."
  default     = 300  # Safe default for 1000 sensors/daily; raise for larger fleets
}

variable "daily_cost_alarm_usd" {
  type        = number
  description = "Daily compute spend (USD) that triggers a P2 CloudWatch alarm"
  default     = 15   # ~1000 sensors/run; raise proportionally with fleet size
}

variable "cost_per_sensor_alarm_usd" {
  type        = number
  description = "Average cost-per-sensor (USD) per run that triggers a P2 alarm. A spike = stuck job or Spot thrash."
  default     = 0.005  # 10x the normal ~$0.000477/sensor
}

variable "drift_forced_retrain_spike_threshold" {
  type        = number
  description = "Count of sensors force-flagged for retraining in one day that triggers a P2 alarm"
  default     = 200  # ~20% of a 1000-sensor fleet — investigate if more than this drift together
}
