# modules/ecs/variables.tf

variable "name_prefix" {
  type        = string
  description = "Prefix for all ECS resource names (e.g. annam-prod)"
}

variable "aws_region" {
  type        = string
}

variable "task_role_arn" {
  type        = string
  description = "IAM task role ARN (from iam module)"
}

variable "execution_role_arn" {
  type        = string
  description = "IAM execution role ARN (from iam module)"
}

variable "ecr_repository_url" {
  type        = string
  description = "ECR repository URL without tag (from ecr module)"
}

variable "image_tag" {
  type        = string
  description = "Docker image tag to deploy"
  default     = "latest"
}

variable "data_bucket_name" {
  type        = string
  description = "S3 data bucket name (from s3 module)"
}

variable "sensor_table_name" {
  type        = string
  description = "DynamoDB sensor metadata table name (from dynamodb module)"
}

variable "results_table_name" {
  type        = string
  description = "DynamoDB results table name (from dynamodb module)"
}

variable "cw_namespace" {
  type        = string
  description = "CloudWatch EMF metrics namespace"
  default     = "AnnamAI/GapFilling"
}

variable "log_retention_days" {
  type        = number
  description = "CloudWatch log retention in days"
  default     = 30
}

variable "log_level" {
  type        = string
  description = "Application log level: DEBUG, INFO, WARNING, ERROR"
  default     = "INFO"
}

variable "train_mode" {
  type        = bool
  description = "Default TRAIN_MODE for tasks (overridden per-execution by Step Functions)"
  default     = false
}

variable "batch_size" {
  type        = number
  description = "Sensors processed per Fargate task"
  default     = 25
}

variable "n_splits" {
  type        = number
  description = "TimeSeriesSplit folds for model tournament"
  default     = 5
}

variable "val_n_gaps" {
  type        = number
  description = "Synthetic gaps for evaluation (lower = faster)"
  default     = 150
}

variable "tags" {
  type        = map(string)
  default     = {}
}
