# modules/dynamodb/variables.tf

variable "name_prefix" {
  type        = string
  description = "Prefix for table names (e.g. annam-prod → annam-prod-sensor-metadata)"
}

variable "enable_pitr" {
  type        = bool
  description = "Enable Point-In-Time Recovery. Recommended true for prod."
  default     = true
}

variable "tags" {
  type        = map(string)
  default     = {}
}
