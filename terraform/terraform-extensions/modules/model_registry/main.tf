# =============================================================================
# modules/model_registry/main.tf
#
# Train-once / infer-many architecture for the gap-filling pipeline.
#
# WHY THIS EXISTS
# ─────────────────────────────────────────────────────────────────────────────
# The current pipeline re-runs the model tournament every training pass, even
# when sensor data hasn't materially changed. At 1000+ sensors that wastes
# ~$150/month in unnecessary retraining compute.
#
# This module adds a DynamoDB Model Registry that:
#   1. Records the active model per sensor + variable, when it was trained,
#      and its MAE — so the orchestrator can decide skip vs. retrain per sensor.
#   2. Tracks model versions explicitly (monotonic counter) enabling rollback.
#   3. Stores evaluation snapshots (MAE/RMSE/R²/Bias per variable per gap-size)
#      as the baseline that drift detection compares against.
#   4. Maintains a per-sensor training schedule (last_trained_at,
#      next_train_after, force_retrain) so the orchestrator can route individual
#      sensors to train or infer without a fleet-wide toggle.
#
# SCHEMA
# ─────────────────────────────────────────────────────────────────────────────
# PK: MODEL#<sensor_id>   SK: VAR#<variable>#ACTIVE        → active model record
# PK: MODEL#<sensor_id>   SK: VAR#<variable>#VER#<version>  → version history (TTL)
# PK: SCHEDULE#<sensor_id> SK: META                          → training schedule
#
# GSIs:
#   gsi_status_trained  — find stale ACTIVE models (status + trained_at)
#   gsi_next_train      — find sensors due for retraining (force_retrain + next_train_after)
# =============================================================================

resource "aws_dynamodb_table" "model_registry" {
  name         = "${var.name_prefix}-model-registry"
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
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "trained_at"
    type = "S"
  }
  # GSI keys must be strings in DynamoDB; "true"/"false" for force_retrain
  attribute {
    name = "force_retrain"
    type = "S"
  }
  attribute {
    name = "next_train_after"
    type = "S"  # ISO8601 — lexicographic == chronological sort
  }

  # Query: "show me all ACTIVE models sorted by how long ago they were trained"
  global_secondary_index {
    name            = "gsi_status_trained"
    hash_key        = "status"
    range_key       = "trained_at"
    projection_type = "ALL"
  }

  # Query: "which sensors need retraining in the next window?"
  global_secondary_index {
    name            = "gsi_next_train"
    hash_key        = "force_retrain"
    range_key       = "next_train_after"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "sensor_id",
      "last_trained_at",
      "consecutive_failures",
      "retrain_reason",
    ]
  }

  # Version history records carry a ttl attribute; active records do not.
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

  tags = merge(var.tags, { Name = "${var.name_prefix}-model-registry" })
}
