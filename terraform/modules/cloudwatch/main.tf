# =============================================================================
# modules/cloudwatch/main.tf
#
# Creates:
#   - 5 metric alarms (2 P1, 3 P2) → SNS failure topic
#   - Operational dashboard with metric widgets + Logs Insights panels
# =============================================================================

locals {
  ns              = var.cw_namespace
  log_group_batch = "/ecs/${var.name_prefix}-run-batch"
  log_group_list  = "/ecs/${var.name_prefix}-list-sensors"
  sfn_arn         = var.state_machine_arn
  account_id      = data.aws_caller_identity.current.account_id
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ── P1: Too many sensors failed ───────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "sensors_failed_high" {
  alarm_name          = "${var.name_prefix}-sensors-failed-high"
  alarm_description   = "P1: >50 sensors failed in the last hour — investigate run_batch logs"
  namespace           = local.ns
  metric_name         = "SensorsFailed"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = var.sensors_failed_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.failure_topic_arn]
  ok_actions          = [var.failure_topic_arn]
  tags                = merge(var.tags, { Severity = "P1" })
}

# ── P1: Step Functions execution failed ───────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "sfn_executions_failed" {
  alarm_name          = "${var.name_prefix}-sfn-executions-failed"
  alarm_description   = "P1: Step Functions execution failed — pipeline did not complete"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.failure_topic_arn]
  ok_actions          = [var.failure_topic_arn]

  dimensions = {
    StateMachineArn = local.sfn_arn
  }

  tags = merge(var.tags, { Severity = "P1" })
}

# ── P2: Batch duration too long ───────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "batch_duration_high" {
  alarm_name          = "${var.name_prefix}-batch-duration-high"
  alarm_description   = "P2: A batch ran >1 hour — may indicate stuck sensors or BATCH_SIZE too large"
  namespace           = local.ns
  metric_name         = "BatchDurationSeconds"
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = var.batch_duration_threshold_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.failure_topic_arn]
  tags                = merge(var.tags, { Severity = "P2" })
}

# ── P2: High unresolved rows (neighbour coverage degraded) ────────────────────
resource "aws_cloudwatch_metric_alarm" "unresolved_rows_high" {
  alarm_name          = "${var.name_prefix}-unresolved-rows-high"
  alarm_description   = "P2: >1000 unresolved rows — neighbour sensor coverage may have degraded"
  namespace           = local.ns
  metric_name         = "Rows_unresolved"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = var.unresolved_rows_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.failure_topic_arn]
  tags                = merge(var.tags, { Severity = "P2" })
}

# ── P2: No sensors processed (pipeline silently not firing) ───────────────────
resource "aws_cloudwatch_metric_alarm" "no_sensors_processed" {
  alarm_name          = "${var.name_prefix}-no-sensors-processed"
  alarm_description   = "P2: Zero sensors processed in 26 hours — pipeline may not have triggered"
  namespace           = local.ns
  metric_name         = "SensorsProcessed"
  statistic           = "Sum"
  period              = 93600 # 26 hours — covers daily run + 2h buffer
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "LessThanOrEqualToThreshold"
  treat_missing_data  = "breaching" # Missing = no run = alarm
  alarm_actions       = [var.failure_topic_arn]
  tags                = merge(var.tags, { Severity = "P2" })
}

# ── Dashboard ─────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_dashboard" "pipeline" {
  dashboard_name = "${var.name_prefix}-pipeline"

  dashboard_body = jsonencode({
    widgets = [
      # ---- Header text ----
      {
        type = "text", x = 0, y = 0, width = 24, height = 2
        properties = {
          markdown = "# Annam AI — IoT Gap-Filling Pipeline\nEnvironment: **${var.environment}** | Namespace: **${local.ns}** | [run-batch logs](https://console.aws.amazon.com/cloudwatch/home#logsV2:log-groups/log-group/${replace(local.log_group_batch, "/", "$252F")}) | [Step Functions](https://console.aws.amazon.com/states)"
        }
      },
      # ---- Sensor throughput ----
      {
        type = "metric", x = 0, y = 2, width = 6, height = 6
        properties = {
          title = "Sensors: Processed / Succeeded / Failed"
          view  = "timeSeries", stacked = false
          metrics = [
            [local.ns, "SensorsProcessed", { label = "Processed", color = "#1f77b4" }],
            [local.ns, "SensorsSucceeded", { label = "Succeeded", color = "#2ca02c" }],
            [local.ns, "SensorsFailed",    { label = "Failed",    color = "#d62728" }]
          ]
          period = 3600, stat = "Sum", region = data.aws_region.current.name
        }
      },
      # ---- Batch duration ----
      {
        type = "metric", x = 6, y = 2, width = 6, height = 6
        properties = {
          title = "Batch Duration (seconds)"
          view  = "timeSeries"
          metrics = [
            [local.ns, "BatchDurationSeconds", { stat = "Average", label = "Avg", color = "#ff7f0e" }],
            [local.ns, "BatchDurationSeconds", { stat = "Maximum", label = "Max", color = "#d62728" }]
          ]
          period = 3600, region = data.aws_region.current.name
        }
      },
      # ---- Cost ----
      {
        type = "metric", x = 12, y = 2, width = 6, height = 6
        properties = {
          title = "Estimated Task Cost (USD)"
          view  = "timeSeries"
          metrics = [
            [local.ns, "TaskCostUSD",      { stat = "Sum",     label = "Total task cost" }],
            [local.ns, "CostPerSensorUSD", { stat = "Average", label = "Avg per sensor" }]
          ]
          period = 86400, region = data.aws_region.current.name
        }
      },
      # ---- Fleet size ----
      {
        type = "metric", x = 18, y = 2, width = 6, height = 6
        properties = {
          title = "Active Sensors & Batch Count"
          view  = "singleValue"
          metrics = [
            [local.ns, "ActiveSensors", { stat = "Maximum", label = "Active Sensors" }],
            [local.ns, "BatchCount",    { stat = "Maximum", label = "Batches per run" }]
          ]
          period = 86400, region = data.aws_region.current.name
        }
      },
      # ---- Imputation breakdown ----
      {
        type = "metric", x = 0, y = 8, width = 12, height = 6
        properties = {
          title = "Imputation Method Breakdown (all sensors)"
          view  = "timeSeries", stacked = true
          metrics = [
            [local.ns, "Rows_original",      { stat = "Sum", label = "Original",     color = "#2ca02c" }],
            [local.ns, "Rows_interpolation", { stat = "Sum", label = "Interpolated", color = "#1f77b4" }],
            [local.ns, "Rows_model",         { stat = "Sum", label = "ML Model",     color = "#ff7f0e" }],
            [local.ns, "Rows_neighbor",      { stat = "Sum", label = "Neighbour",    color = "#9467bd" }],
            [local.ns, "Rows_unresolved",    { stat = "Sum", label = "Unresolved",   color = "#d62728" }]
          ]
          period = 3600, region = data.aws_region.current.name
        }
      },
      # ---- Unresolved rows ----
      {
        type = "metric", x = 12, y = 8, width = 12, height = 6
        properties = {
          title = "Unresolved Rows (data quality signal)"
          view  = "timeSeries"
          annotations = {
            horizontal = [{ value = var.unresolved_rows_threshold, label = "P2 threshold", color = "#ff7f0e" }]
          }
          metrics = [[local.ns, "Rows_unresolved", { stat = "Sum", label = "Total unresolved", color = "#d62728" }]]
          period  = 3600, region = data.aws_region.current.name
        }
      },
      # ---- Sensor failures log table ----
      {
        type = "log", x = 0, y = 14, width = 24, height = 8
        properties = {
          title  = "Recent Sensor Failures"
          query  = "SOURCE '${local.log_group_batch}' | fields ts, device_id, error, duration_s | filter level = 'ERROR' and msg = 'Sensor failed' | sort ts desc | limit 25"
          region = data.aws_region.current.name
          view   = "table"
        }
      },
      # ---- Run summary ----
      {
        type = "log", x = 0, y = 22, width = 24, height = 6
        properties = {
          title  = "Pipeline Run Summary (last 10)"
          query  = "SOURCE '${local.log_group_batch}' | fields ts, total, succeeded, failed, batch_seconds | filter msg = 'Batch complete' | sort ts desc | limit 10"
          region = data.aws_region.current.name
          view   = "table"
        }
      },
      # ---- Alarm status panel ----
      {
        type = "alarm", x = 0, y = 28, width = 24, height = 4
        properties = {
          title = "Active Alarms"
          alarms = [
            aws_cloudwatch_metric_alarm.sensors_failed_high.arn,
            aws_cloudwatch_metric_alarm.sfn_executions_failed.arn,
            aws_cloudwatch_metric_alarm.batch_duration_high.arn,
            aws_cloudwatch_metric_alarm.unresolved_rows_high.arn,
            aws_cloudwatch_metric_alarm.no_sensors_processed.arn,
          ]
        }
      }
    ]
  })
}
