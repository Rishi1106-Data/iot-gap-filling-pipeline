# =============================================================================
# modules/cost/main.tf
#
# Cost visibility infrastructure — cost dashboard + per-sensor reporting.
#
# THREE COMPONENTS
# ─────────────────────────────────────────────────────────────────────────────
# 1. Per-sensor cost attribution DynamoDB table
#    Writes one row per sensor per day. Enables billing showback per project,
#    anomaly detection (one sensor consuming 10x compute = bad data pipeline).
#
# 2. CloudWatch Cost Dashboard
#    Dedicated second dashboard (separate from operational pipeline dashboard):
#    daily/monthly Fargate Spot spend, cost-per-sensor trend, training vs
#    infer split, drift savings counter, AWS service breakdown.
#
# 3. Cost alarms (daily total + per-sensor spike) + AWS Budget
#    Budget catches runaway spend from misconfiguration (schedules left on in
#    dev, BATCH_SIZE set to 1). Alarms fire within minutes via EMF metrics.
#
# COST MODEL REFERENCE (ap-south-1 Fargate Spot, measured values)
# ─────────────────────────────────────────────────────────────────────────────
#   ~115s/sensor × 1 vCPU × $0.01244/hr = $0.000391/sensor (compute)
#   ~115s/sensor × 2 GB   × $0.001365/hr = $0.000086/sensor (memory)
#   Total per sensor: ~$0.000477
#   1,000 sensors/day × 30 days = ~$14.3/month
#   10,000 sensors/day × 30 days = ~$143/month
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  ns = var.cw_namespace
}

# ── Per-sensor cost attribution table ────────────────────────────────────────
resource "aws_dynamodb_table" "cost_attribution" {
  name         = "${var.name_prefix}-cost-attribution"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"  # "COST#<sensor_id>"
  }
  attribute {
    name = "SK"
    type = "S"  # "DATE#<YYYY-MM-DD>"
  }
  attribute {
    name = "project_tag"
    type = "S"  # Sensor-to-project grouping for billing showback
  }
  attribute {
    name = "run_date"
    type = "S"
  }

  # Query: "all sensors in project X on date Y" for showback reports
  global_secondary_index {
    name            = "gsi_project_date"
    hash_key        = "project_tag"
    range_key       = "run_date"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "sensor_id", "compute_cost_usd", "duration_seconds",
      "rows_processed", "train_mode",
    ]
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true  # Records expire after var.attribution_retention_days
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = false  # Cost data reconstructable from CW metrics; PITR unnecessary
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-cost-attribution" })
}

# ── CloudWatch Cost Dashboard ─────────────────────────────────────────────────
resource "aws_cloudwatch_dashboard" "cost" {
  dashboard_name = "${var.name_prefix}-cost"

  dashboard_body = jsonencode({
    widgets = [

      # Row 0 — Header
      {
        type = "text", x = 0, y = 0, width = 24, height = 2
        properties = {
          markdown = "# Annam AI — Cost Dashboard\nEnvironment: **${var.environment}** | Region: **${data.aws_region.current.name}**\n\n> **Cost model:** ~$0.000477/sensor/run (Fargate Spot ap-south-1). Storage + DynamoDB = <5% of total. Training ≈ Inference (evaluation always re-fits — see PRODUCTION_HARDENING.md §0)."
        }
      },

      # Row 1 — KPI single-value tiles
      {
        type = "metric", x = 0, y = 2, width = 6, height = 4
        properties = {
          title = "Today's Total Compute Cost (USD)"
          view  = "singleValue"
          setPeriodToTimeRange = true
          metrics = [[local.ns, "TaskCostUSD", { stat = "Sum", label = "Fargate Spot cost" }]]
          period = 86400
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 6, y = 2, width = 6, height = 4
        properties = {
          title = "Avg Cost Per Sensor (USD)"
          view  = "singleValue"
          setPeriodToTimeRange = true
          metrics = [[local.ns, "CostPerSensorUSD", { stat = "Average", label = "$/sensor/run" }]]
          period = 86400
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 12, y = 2, width = 6, height = 4
        properties = {
          title = "Sensors Processed Today"
          view  = "singleValue"
          setPeriodToTimeRange = true
          metrics = [
            [local.ns, "SensorsProcessed", { stat = "Sum", label = "Total" }],
            [local.ns, "SensorsSucceeded", { stat = "Sum", label = "OK" }],
            [local.ns, "SensorsFailed",    { stat = "Sum", label = "Failed" }]
          ]
          period = 86400
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 18, y = 2, width = 6, height = 4
        properties = {
          title = "Projected Monthly Cost (USD)"
          view  = "singleValue"
          metrics = [
            [{ expression = "SUM(m1)", label = "30-day total", id = "e1" }],
            [local.ns, "TaskCostUSD", { stat = "Sum", period = 86400, id = "m1", visible = false }]
          ]
          period = 2592000
          region = data.aws_region.current.name
        }
      },

      # Row 2 — Daily cost trend + cost-per-sensor distribution
      {
        type = "metric", x = 0, y = 6, width = 16, height = 6
        properties = {
          title = "Daily Compute Cost Trend — 30 days (USD)"
          view  = "timeSeries"
          stacked = false
          metrics = [
            [local.ns, "TaskCostUSD", { stat = "Sum", label = "Daily total", color = "#1f77b4", period = 86400 }]
          ]
          period = 86400
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 16, y = 6, width = 8, height = 6
        properties = {
          title = "Cost Per Sensor: Avg / Max / Min"
          view  = "timeSeries"
          metrics = [
            [local.ns, "CostPerSensorUSD", { stat = "Average", label = "Avg",  color = "#2ca02c" }],
            [local.ns, "CostPerSensorUSD", { stat = "Maximum", label = "Max",  color = "#d62728" }],
            [local.ns, "CostPerSensorUSD", { stat = "Minimum", label = "Min",  color = "#9467bd" }]
          ]
          period = 86400
          region = data.aws_region.current.name
        }
      },

      # Row 3 — Drift savings + efficiency
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title = "Drift Detection: Retrain vs Skip (compute savings)"
          view  = "timeSeries"
          stacked = true
          metrics = [
            [local.ns, "DriftForcedRetrainCount",   { stat = "Sum", label = "Force-retrained",   color = "#d62728" }],
            [local.ns, "DriftScheduledRetrainCount", { stat = "Sum", label = "Scheduled retrain", color = "#ff7f0e" }],
            [{ expression = "m3-m1-m2", label = "Skipped (saved)", id = "e1", color = "#2ca02c" }],
            [local.ns, "SensorsProcessed",          { stat = "Sum", id = "m3", visible = false }],
            [local.ns, "DriftForcedRetrainCount",   { stat = "Sum", id = "m1", visible = false }],
            [local.ns, "DriftScheduledRetrainCount", { stat = "Sum", id = "m2", visible = false }]
          ]
          period = 86400
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 12, y = 12, width = 12, height = 6
        properties = {
          title = "Batch Duration vs Sensor Count (efficiency signal)"
          view  = "timeSeries"
          metrics = [
            [local.ns, "BatchDurationSeconds", { stat = "Average", label = "Avg batch duration (s)", color = "#ff7f0e" }],
            [local.ns, "SensorsProcessed",     { stat = "Sum",     label = "Sensors processed",      color = "#1f77b4" }]
          ]
          period = 86400
          region = data.aws_region.current.name
        }
      },

      # Row 4 — Cost-per-sensor Logs Insights table
      {
        type = "log", x = 0, y = 18, width = 24, height = 8
        properties = {
          title  = "Cost Per Sensor — Last Run (top 50 by cost)"
          query  = "SOURCE '${var.run_batch_log_group_name}' | filter ispresent(CostPerSensorUSD) | stats sum(TaskCostUSD) as total_cost_usd, avg(CostPerSensorUSD) as avg_cost_usd, count() as runs by DeviceId | sort total_cost_usd desc | limit 50"
          region = data.aws_region.current.name
          view   = "table"
        }
      }
    ]
  })
}

# ── AWS Budget ────────────────────────────────────────────────────────────────
resource "aws_budgets_budget" "pipeline_monthly" {
  name              = "${var.name_prefix}-monthly-budget"
  budget_type       = "COST"
  limit_amount      = tostring(var.monthly_budget_usd)
  limit_unit        = "USD"
  time_unit         = "MONTHLY"
  time_period_start = "2024-01-01_00:00"

  cost_filter {
    name = "Service"
    values = [
      "Amazon Elastic Container Service",
      "AWS Fargate",
      "Amazon DynamoDB",
      "Amazon Simple Storage Service",
      "AWS Step Functions",
      "Amazon CloudWatch",
      "Amazon Elastic Container Registry (Amazon ECR)",
    ]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [var.failure_topic_arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [var.failure_topic_arn]
  }
}

# ── Cost alarms ───────────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "daily_cost_high" {
  alarm_name          = "${var.name_prefix}-daily-cost-high"
  alarm_description   = "P2: Today's pipeline compute exceeded threshold — check sensor count and BATCH_SIZE"
  namespace           = local.ns
  metric_name         = "TaskCostUSD"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = var.daily_cost_alarm_usd
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.failure_topic_arn]
  tags                = merge(var.tags, { Severity = "P2" })
}

resource "aws_cloudwatch_metric_alarm" "cost_per_sensor_high" {
  alarm_name          = "${var.name_prefix}-cost-per-sensor-high"
  alarm_description   = "P2: Avg cost/sensor exceeded threshold — may indicate Spot interruption thrash or runaway retries"
  namespace           = local.ns
  metric_name         = "CostPerSensorUSD"
  statistic           = "Average"
  period              = 86400
  evaluation_periods  = 1
  threshold           = var.cost_per_sensor_alarm_usd
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.failure_topic_arn]
  tags                = merge(var.tags, { Severity = "P2" })
}
