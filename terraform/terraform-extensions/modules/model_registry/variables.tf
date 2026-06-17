# modules/model_registry/variables.tf

variable "name_prefix" {
  type        = string
  description = "Resource name prefix — must match the prefix used by other modules (e.g. annam-prod)"
}

variable "enable_pitr" {
  type        = bool
  description = "Enable DynamoDB Point-In-Time Recovery. Strongly recommended for prod."
  default     = true
}

variable "model_history_ttl_days" {
  type        = number
  description = "Days to retain deprecated model version records before auto-expiry. Active records never expire."
  default     = 90
}

variable "retraining_interval_days" {
  type        = number
  description = "Default interval between scheduled model retraining passes (days). Overridden per-sensor via DynamoDB item update."
  default     = 7
}

variable "drift_mae_threshold_pct" {
  type        = number
  description = "Percentage increase in rolling MAE vs. baseline that flags a sensor for forced retraining. E.g. 20 = retrain if MAE is >20% worse than at training time."
  default     = 20
}

variable "tags" {
  type        = map(string)
  default     = {}
}
