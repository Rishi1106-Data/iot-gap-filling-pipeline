# =============================================================================
# modules/eventbridge/main.tf
#
# Two EventBridge Scheduler schedules:
#   daily-training   — cron 02:30 UTC, TRAIN_MODE=true, 30-min jitter window
#   hourly-inference — rate(1 hour), TRAIN_MODE=false, 10-min jitter window
#
# Jitter prevents thundering-herd Fargate launches at exactly the same second.
# =============================================================================

resource "aws_scheduler_schedule_group" "pipeline" {
  name = "${var.name_prefix}-schedules"
  tags = var.tags
}

# ── Daily training run ────────────────────────────────────────────────────────
resource "aws_scheduler_schedule" "daily_training" {
  name       = "${var.name_prefix}-daily-training"
  group_name = aws_scheduler_schedule_group.pipeline.name
  state      = var.schedules_enabled ? "ENABLED" : "DISABLED"

  # Daily at 02:30 UTC
  schedule_expression          = "cron(30 2 * * ? *)"
  schedule_expression_timezone = "UTC"

  # 30-minute jitter — spreads Fargate launches, reduces Spot contention
  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 30
  }

  target {
    arn      = var.state_machine_arn
    role_arn = var.scheduler_role_arn

    input = jsonencode({
      train_mode = true
    })

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}

# ── Hourly inference run (disabled by default — enable per environment) ───────
resource "aws_scheduler_schedule" "hourly_inference" {
  name       = "${var.name_prefix}-hourly-inference"
  group_name = aws_scheduler_schedule_group.pipeline.name
  state      = var.enable_hourly_inference && var.schedules_enabled ? "ENABLED" : "DISABLED"

  schedule_expression          = "rate(1 hour)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 10
  }

  target {
    arn      = var.state_machine_arn
    role_arn = var.scheduler_role_arn

    input = jsonencode({
      train_mode = false
    })

    retry_policy {
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 1800
    }
  }
}
