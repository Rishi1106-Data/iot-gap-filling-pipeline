# modules/s3/variables.tf

variable "bucket_name" {
  type        = string
  description = "Globally unique S3 bucket name. Recommended: annam-gapfill-<env>-<account_id>"
}

variable "force_destroy" {
  type        = bool
  description = "Allow Terraform to delete the bucket even when non-empty. Set true only for dev/test."
  default     = false
}

variable "filled_retention_days" {
  type        = number
  description = "Days to retain filled dataset CSVs before automatic expiry"
  default     = 90
}

variable "reports_retention_days" {
  type        = number
  description = "Days to retain audit/evaluation reports before automatic expiry"
  default     = 90
}

variable "runs_retention_days" {
  type        = number
  description = "Days to retain Step Functions batch manifest files"
  default     = 7
}

variable "model_version_retention_days" {
  type        = number
  description = "Days to retain non-current model versions (enables rollback within this window)"
  default     = 30
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to all resources"
  default     = {}
}
