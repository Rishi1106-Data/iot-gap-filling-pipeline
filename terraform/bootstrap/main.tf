# =============================================================================
# bootstrap/main.tf
#
# ONE-TIME setup: creates the S3 bucket and DynamoDB table that hold Terraform
# remote state for all environments. Run this before everything else.
#
# Usage:
#   cd terraform/bootstrap
#   terraform init
#   terraform apply -var="aws_region=ap-south-1" -var="project=annam"
#
# After apply, copy the outputs into environments/*/backend.tf
# =============================================================================

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
  # Bootstrap has LOCAL state (it bootstraps remote state for everything else)
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform-bootstrap"
    }
  }
}

variable "aws_region" {
  type        = string
  description = "AWS region for the Terraform state bucket"
}

variable "project" {
  type        = string
  description = "Short project name used as prefix (e.g. annam)"
  default     = "annam"
}

data "aws_caller_identity" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  bucket_name = "${var.project}-tfstate-${local.account_id}"
  lock_table  = "${var.project}-tfstate-lock"
}

# ── S3 bucket for state ───────────────────────────────────────────────────────
resource "aws_s3_bucket" "tfstate" {
  bucket        = local.bucket_name
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── DynamoDB lock table ───────────────────────────────────────────────────────
resource "aws_dynamodb_table" "tfstate_lock" {
  name         = local.lock_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  lifecycle {
    prevent_destroy = true
  }
}

# ── Outputs ───────────────────────────────────────────────────────────────────
output "state_bucket" {
  value       = aws_s3_bucket.tfstate.id
  description = "S3 bucket name — use in environments/*/backend.tf"
}

output "lock_table" {
  value       = aws_dynamodb_table.tfstate_lock.name
  description = "DynamoDB lock table — use in environments/*/backend.tf"
}

output "backend_config_snippet" {
  value = <<-EOT
    # Paste into environments/<env>/backend.tf
    terraform {
      backend "s3" {
        bucket         = "${aws_s3_bucket.tfstate.id}"
        key            = "<env>/terraform.tfstate"
        region         = "${var.aws_region}"
        dynamodb_table = "${aws_dynamodb_table.tfstate_lock.name}"
        encrypt        = true
      }
    }
  EOT
}
