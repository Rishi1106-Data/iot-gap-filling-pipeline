# =============================================================================
# modules/s3/main.tf
#
# Single S3 bucket with:
#   - Versioning (model artifact rollback)
#   - SSE-AES256 encryption
#   - Public access block
#   - Lifecycle rules per prefix (expire outputs, cap model versions)
#   - Intelligent-Tiering on raw/ (sensors written once, rarely re-read)
# =============================================================================

resource "aws_s3_bucket" "data" {
  bucket        = var.bucket_name
  force_destroy = var.force_destroy

  tags = merge(var.tags, { Name = var.bucket_name })
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  # Expire old filled outputs
  rule {
    id     = "expire-filled"
    status = "Enabled"
    filter { prefix = "filled/" }
    expiration { days = var.filled_retention_days }
    noncurrent_version_expiration { noncurrent_days = 7 }
  }

  # Expire old reports
  rule {
    id     = "expire-reports"
    status = "Enabled"
    filter { prefix = "reports/" }
    expiration { days = var.reports_retention_days }
    noncurrent_version_expiration { noncurrent_days = 7 }
  }

  # Step Functions batch manifests — ephemeral
  rule {
    id     = "expire-runs"
    status = "Enabled"
    filter { prefix = "runs/" }
    expiration { days = var.runs_retention_days }
  }

  # Cap old model versions (keep current + rollback window)
  rule {
    id     = "expire-old-model-versions"
    status = "Enabled"
    filter { prefix = "models/" }
    noncurrent_version_expiration { noncurrent_days = var.model_version_retention_days }
  }
}

# Intelligent-Tiering for raw/ sensor CSVs.
# ARCHIVE_ACCESS (90d) must be declared before DEEP_ARCHIVE_ACCESS (180d) —
# AWS requires ascending day order within the same configuration.
resource "aws_s3_bucket_intelligent_tiering_configuration" "raw" {
  bucket = aws_s3_bucket.data.id
  name   = "raw-auto-tier"
  filter { prefix = "raw/" }

  tiering {
    access_tier = "ARCHIVE_ACCESS"
    days        = 90
  }

  tiering {
    access_tier = "DEEP_ARCHIVE_ACCESS"
    days        = 180
  }
}
