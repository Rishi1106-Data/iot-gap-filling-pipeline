# modules/eventbridge/variables.tf

variable "name_prefix" {
  type        = string
}

variable "state_machine_arn" {
  type        = string
  description = "Step Functions state machine ARN to trigger"
}

variable "scheduler_role_arn" {
  type        = string
  description = "IAM role ARN that EventBridge Scheduler assumes to start executions"
}

variable "schedules_enabled" {
  type        = bool
  description = "Master switch: set false in dev/staging to avoid accidental scheduled runs"
  default     = true
}

variable "enable_hourly_inference" {
  type        = bool
  description = "Enable hourly inference schedule (costs 24x more than daily — only if downstream needs intra-day freshness)"
  default     = false
}

variable "tags" {
  type        = map(string)
  default     = {}
}
