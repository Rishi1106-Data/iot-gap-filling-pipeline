# =============================================================================
# modules/dynamodb/main.tf
#
# Two tables that mirror io_dynamo.py exactly:
#
#   annam-<env>-sensor-metadata   — topology + config, read-mostly
#     PK: SENSOR#<id>  SK: META
#     GSI: gsi_status (PK: status, SK: device_id)
#
#   annam-<env>-gapfill-results   — per-run audit, write-heavy, TTL 180d
#     PK: SENSOR#<id>  SK: RUN#<date>#<run_id>
# =============================================================================

# ── Table 1: Sensor metadata ──────────────────────────────────────────────────
resource "aws_dynamodb_table" "sensor_metadata" {
  name         = "${var.name_prefix}-sensor-metadata"
  billing_mode = "PAY_PER_REQUEST" # Spiky batch traffic; on-demand is cheaper than provisioned
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "device_id"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_status"
    hash_key        = "status"
    range_key       = "device_id"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = var.enable_pitr
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-sensor-metadata" })
}

# ── Table 2: Gap-fill run results ─────────────────────────────────────────────
resource "aws_dynamodb_table" "gapfill_results" {
  name         = "${var.name_prefix}-gapfill-results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  # TTL auto-deletes old run records — no manual cleanup required
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.enable_pitr
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-gapfill-results" })
}
