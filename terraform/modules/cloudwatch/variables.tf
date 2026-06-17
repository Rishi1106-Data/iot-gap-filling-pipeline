# modules/cloudwatch/variables.tf

variable "name_prefix" {
  type        = string
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
  description = "SNS topic ARN that alarms publish to (from stepfunctions module)"
}

variable "state_machine_arn" {
  type        = string
  description = "Step Functions ARN for the SFN ExecutionsFailed alarm dimension"
}

variable "sensors_failed_threshold" {
  type        = number
  description = "Number of failed sensors per hour that triggers a P1 alarm"
  default     = 50
}

variable "batch_duration_threshold_seconds" {
  type        = number
  description = "Batch duration (seconds) that triggers a P2 alarm. Default 3600 = 1 hour"
  default     = 3600
}

variable "unresolved_rows_threshold" {
  type        = number
  description = "Total unresolved rows per hour that triggers a P2 alarm"
  default     = 1000
}

variable "tags" {
  type        = map(string)
  default     = {}
}
