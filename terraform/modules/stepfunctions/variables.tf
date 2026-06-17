# modules/stepfunctions/variables.tf

variable "name_prefix" {
  type        = string
}

variable "sfn_role_arn" {
  type        = string
  description = "Step Functions IAM role ARN (from iam module)"
}

variable "ecs_cluster_arn" {
  type        = string
  description = "ECS cluster ARN (from ecs module)"
}

variable "run_batch_taskdef_arn" {
  type        = string
  description = "run-batch task definition ARN (from ecs module)"
}

variable "list_sensors_taskdef_arn" {
  type        = string
  description = "list-sensors task definition ARN (from ecs module)"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for Fargate tasks (at least 2)"
}

variable "task_security_group_id" {
  type        = string
  description = "Security group ID for Fargate tasks (from networking module)"
}

variable "data_bucket_name" {
  type        = string
  description = "S3 data bucket name for batch manifests"
}

variable "ops_email" {
  type        = string
  description = "Email address for failure notifications. Empty string disables subscription."
  default     = ""
}

variable "map_max_concurrency" {
  type        = number
  description = "Maximum concurrent Fargate tasks in the Map state"
  default     = 20
}

variable "tolerated_failure_percentage" {
  type        = number
  description = "Percentage of Map iterations that may fail before aborting the whole run"
  default     = 10
}

variable "log_retention_days" {
  type        = number
  default     = 30
}

variable "enable_xray_tracing" {
  type        = bool
  description = "Enable AWS X-Ray tracing on the state machine"
  default     = false
}

variable "tags" {
  type        = map(string)
  default     = {}
}
