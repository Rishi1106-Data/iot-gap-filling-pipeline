# modules/networking/variables.tf

variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "vpc_id" {
  type        = string
  description = "ID of the existing VPC to attach endpoints to"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for interface endpoint ENIs (at least 2, different AZs)"
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "At least two private subnets in different AZs are required for Fargate Spot diversity."
  }
}

variable "name_prefix" {
  type        = string
  description = "Prefix for all resource names (e.g. annam-prod)"
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to all resources"
  default     = {}
}
