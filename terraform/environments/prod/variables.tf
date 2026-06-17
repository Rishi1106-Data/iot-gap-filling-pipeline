# environments/prod/variables.tf

# ── Required ──────────────────────────────────────────────────────────────────
variable "aws_region" {
  type        = string
  description = "AWS region for all resources"
}

variable "vpc_id" {
  type        = string
  description = "ID of the existing VPC"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs (at least 2, different AZs) for Fargate + VPC endpoints"
}

variable "ops_email" {
  type        = string
  description = "Email address subscribed to the SNS failure topic"
  sensitive   = true
}

# ── Project metadata ──────────────────────────────────────────────────────────
variable "project" {
  type    = string
  default = "annam"
}

variable "owner_team" {
  type        = string
  description = "Team name for resource tagging"
  default     = "ml-infrastructure"
}

# ── Container ─────────────────────────────────────────────────────────────────
variable "image_tag" {
  type        = string
  description = "Docker image tag to deploy. Use git SHA for traceability."
  default     = "latest"
}

# ── Pipeline tuning ───────────────────────────────────────────────────────────
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
  description = "Synthetic gaps used in evaluation (lower = faster inference runs)"
  default     = 150
}

# ── Scheduling ────────────────────────────────────────────────────────────────
variable "enable_hourly_inference" {
  type        = bool
  description = "Enable hourly inference schedule. Costs ~24x more per month than daily-only."
  default     = false
}

# ── Step Functions ────────────────────────────────────────────────────────────
variable "map_max_concurrency" {
  type        = number
  description = "Max concurrent Fargate tasks in the Map state"
  default     = 20
}

variable "tolerated_failure_percentage" {
  type        = number
  description = "% of Map iterations that may fail before aborting the run"
  default     = 10
}

# ── CloudWatch ────────────────────────────────────────────────────────────────
variable "cw_namespace" {
  type    = string
  default = "AnnamAI/GapFilling"
}

variable "sensors_failed_threshold" {
  type        = number
  description = "Sensors-failed count that triggers P1 alarm"
  default     = 50
}

variable "unresolved_rows_threshold" {
  type        = number
  description = "Unresolved row count that triggers P2 alarm"
  default     = 1000
}
