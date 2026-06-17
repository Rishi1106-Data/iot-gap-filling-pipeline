# modules/drift_detection/variables.tf

variable "name_prefix" {
  type        = string
  description = "Resource name prefix matching other modules (e.g. annam-prod)"
}

variable "model_registry_table_name" {
  type        = string
  description = "DynamoDB model registry table name (from model_registry module output)"
}

variable "model_registry_table_arn" {
  type        = string
  description = "DynamoDB model registry table ARN (from model_registry module output)"
}

variable "model_registry_gsi_status_arn" {
  type        = string
  description = "gsi_status_trained GSI ARN (from model_registry module output)"
}

variable "model_registry_gsi_next_train_arn" {
  type        = string
  description = "gsi_next_train GSI ARN (from model_registry module output)"
}

variable "main_pipeline_state_machine_arn" {
  type        = string
  description = "ARN of the main gap-filling pipeline state machine (from stepfunctions module)"
}

variable "failure_topic_arn" {
  type        = string
  description = "SNS topic ARN for alarm notifications (from stepfunctions module)"
}

variable "schedule_group_name" {
  type        = string
  description = "EventBridge Scheduler group name to add the drift check schedule to (from eventbridge module)"
}

variable "drift_scheduler_role_arn" {
  type        = string
  description = "IAM role ARN that EventBridge Scheduler assumes to start the drift-check state machine"
}

variable "schedules_enabled" {
  type        = bool
  description = "Master switch — set false in dev/staging to prevent automatic drift checks"
  default     = true
}

variable "cw_namespace" {
  type        = string
  description = "CloudWatch metrics namespace for drift metrics"
  default     = "AnnamAI/GapFilling"
}

variable "forced_retrain_spike_threshold" {
  type        = number
  description = "Number of sensors flagged for forced retraining in one day that triggers a P2 alarm"
  default     = 200  # 20% of a 1000-sensor fleet
}

variable "log_retention_days" {
  type        = number
  default     = 30
}

variable "tags" {
  type        = map(string)
  default     = {}
}
