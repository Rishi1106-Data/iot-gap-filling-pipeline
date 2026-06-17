# modules/iam/variables.tf

variable "name_prefix" {
  type        = string
  description = "Prefix for all role names (e.g. annam-prod)"
}

variable "data_bucket_arn" {
  type        = string
  description = "ARN of the S3 data bucket — scopes task role S3 permissions"
}

variable "sensor_table_arn" {
  type        = string
  description = "ARN of the sensor metadata DynamoDB table"
}

variable "sensor_table_gsi_arn" {
  type        = string
  description = "ARN of the gsi_status GSI — required separately for Query access"
}

variable "results_table_arn" {
  type        = string
  description = "ARN of the gapfill results DynamoDB table"
}

variable "ecr_repository_arn" {
  type        = string
  description = "ECR repository ARN — scopes execution role image pull permissions"
}

variable "cw_namespace" {
  type        = string
  description = "CloudWatch namespace the task role may publish metrics to"
  default     = "AnnamAI/GapFilling"
}

variable "tags" {
  type        = map(string)
  default     = {}
}
