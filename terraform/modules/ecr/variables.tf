# modules/ecr/variables.tf

variable "repository_name" {
  type        = string
  description = "ECR repository name"
  default     = "annam-gapfill"
}

variable "max_image_count" {
  type        = number
  description = "Maximum number of tagged images to retain"
  default     = 20
}

variable "tags" {
  type        = map(string)
  default     = {}
}
