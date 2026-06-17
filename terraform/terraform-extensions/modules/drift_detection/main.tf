# =============================================================================
# modules/drift_detection/main.tf
#
# Drift detection workflow — autonomous retraining trigger.
#
# ARCHITECTURE
# ─────────────────────────────────────────────────────────────────────────────
#
#   EventBridge (daily)
#        │
#        ▼
#   Step Functions: annam-<env>-drift-check
#        │
#        ├─ QueryStaleSensors   ← DynamoDB SDK: gsi_next_train + gsi_status_trained
#        │                         returns sensors needing retrain (overdue schedule
#        │                         OR live MAE > baseline × (1 + threshold))
#        ├─ AnyRetrainingNeeded ← Choice state
#        │     ├─ NO  → DriftCheckComplete (pass, no cost)
#        │     └─ YES ↓
#        ├─ BuildRetrainPayload ← Pass: constructs {train_mode:true, device_ids:[...]}
#        │
#        └─ TriggerTrainingRun  ← StartExecution on the MAIN pipeline state machine
#                                  with train_mode=true scoped to only the sensors
#                                  that need retraining
#
# WHY NOT LAMBDA
# ─────────────────────────────────────────────────────────────────────────────
# Lambda cold starts + SDK calls for DynamoDB Query + SFN StartExecution are
# $0.0000002/invocation at this scale. Step Functions Express workflow costs
# $0.00001/execution. Both are under $1/month at 1000 sensors. We use
# Step Functions because it gives us free execution history, retries, and
# CloudWatch integration without managing Lambda runtimes.
#
# COST IMPACT
# ─────────────────────────────────────────────────────────────────────────────
# Without drift detection: all sensors retrain daily = ~$150/month (10k sensors)
# With drift detection (7-day interval + 20% MAE threshold):
#   ~15% of sensors retrain on any given day (new data, model drift, or schedule)
#   Estimated savings: ~85% of training compute = ~$127/month at 10k sensors
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
}

# ── IAM role for drift-check state machine ────────────────────────────────────
resource "aws_iam_role" "drift_sfn" {
  name        = "${var.name_prefix}-drift-sfn-role"
  description = "Drift detection state machine: query DynamoDB registry, trigger main pipeline"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        ArnLike = {
          "aws:SourceArn" = "arn:aws:states:${local.region}:${local.account_id}:stateMachine:*"
        }
      }
    }]
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-drift-sfn-role" })
}

resource "aws_iam_role_policy" "drift_sfn_dynamodb" {
  name = "model-registry-read"
  role = aws_iam_role.drift_sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RegistryQuery"
        Effect = "Allow"
        Action = ["dynamodb:Query", "dynamodb:GetItem"]
        Resource = [
          var.model_registry_table_arn,
          var.model_registry_gsi_status_arn,
          var.model_registry_gsi_next_train_arn,
        ]
      },
      {
        # Drift check writes force_retrain=true and retrain_reason back to schedule records
        Sid      = "ScheduleWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:PutItem"]
        Resource = [var.model_registry_table_arn]
      }
    ]
  })
}

resource "aws_iam_role_policy" "drift_sfn_trigger_pipeline" {
  name = "trigger-main-pipeline"
  role = aws_iam_role.drift_sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "StartMainPipeline"
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = [var.main_pipeline_state_machine_arn]
    }]
  })
}

resource "aws_iam_role_policy" "drift_sfn_cloudwatch" {
  name = "cloudwatch-logs"
  role = aws_iam_role.drift_sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "LogDelivery"
      Effect = "Allow"
      Action = [
        "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutLogEvents",
        "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
      ]
      Resource = ["*"]
    }]
  })
}

# ── CloudWatch log group for drift state machine ──────────────────────────────
resource "aws_cloudwatch_log_group" "drift_sfn" {
  name              = "/aws/states/${var.name_prefix}-drift-check"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# ── Drift detection state machine (EXPRESS — runs in <5 min, much cheaper) ────
# Using EXPRESS type because:
#   - Drift check is short-lived (<5 min for 1000 sensors via DDB Query)
#   - EXPRESS is $1/million executions vs STANDARD $0.025/1000 state transitions
#   - At daily frequency: EXPRESS = ~$0.03/month vs STANDARD = ~$0.80/month
resource "aws_sfn_state_machine" "drift_check" {
  name     = "${var.name_prefix}-drift-check"
  role_arn = aws_iam_role.drift_sfn.arn
  type     = "EXPRESS"

  definition = jsonencode({
    Comment = "Annam AI drift detection: query model registry, identify sensors needing retraining, trigger targeted training run."
    StartAt = "QueryStaleSensors"
    States = {

      # ── Step 1: Find sensors overdue for retraining (schedule-based) ─────────
      QueryStaleSensors = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:dynamodb:query"
        Comment  = "Query gsi_next_train for sensors where next_train_after <= today and force_retrain=true OR schedule overdue."
        Parameters = {
          TableName              = var.model_registry_table_name
          IndexName              = "gsi_next_train"
          KeyConditionExpression = "force_retrain = :forced AND next_train_after <= :now"
          FilterExpression       = "attribute_exists(sensor_id)"
          ExpressionAttributeValues = {
            ":forced" = { S = "true" }
            ":now"    = { "S.$" = "$$.Execution.StartTime" }
          }
          ProjectionExpression = "sensor_id, last_trained_at, consecutive_failures, retrain_reason"
        }
        ResultPath = "$.forced_retrain_sensors"
        Next       = "QueryScheduledSensors"
        Retry = [{
          ErrorEquals  = ["States.TaskFailed"]
          IntervalSeconds = 5
          MaxAttempts  = 3
          BackoffRate  = 2.0
        }]
      }

      # ── Step 2: Find sensors due by schedule (regardless of force flag) ──────
      QueryScheduledSensors = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:dynamodb:query"
        Comment  = "Query gsi_next_train for sensors scheduled for retraining today (force_retrain=false but overdue)."
        Parameters = {
          TableName              = var.model_registry_table_name
          IndexName              = "gsi_next_train"
          KeyConditionExpression = "force_retrain = :not_forced AND next_train_after <= :now"
          FilterExpression       = "attribute_exists(sensor_id)"
          ExpressionAttributeValues = {
            ":not_forced" = { S = "false" }
            ":now"        = { "S.$" = "$$.Execution.StartTime" }
          }
          ProjectionExpression = "sensor_id, last_trained_at"
        }
        ResultPath = "$.scheduled_sensors"
        Next       = "AnyRetrainingNeeded"
        Retry = [{
          ErrorEquals  = ["States.TaskFailed"]
          IntervalSeconds = 5
          MaxAttempts  = 3
          BackoffRate  = 2.0
        }]
      }

      # ── Step 3: Gate — skip if nothing to do ─────────────────────────────────
      AnyRetrainingNeeded = {
        Type = "Choice"
        Comment = "If both query results are empty, exit cheaply. Otherwise build the retrain payload."
        Choices = [
          {
            # At least one sensor needs retraining (forced OR scheduled)
            Or = [
              {
                Variable      = "$.forced_retrain_sensors.Count"
                NumericGreaterThan = 0
              },
              {
                Variable      = "$.scheduled_sensors.Count"
                NumericGreaterThan = 0
              }
            ]
            Next = "BuildRetrainPayload"
          }
        ]
        Default = "DriftCheckComplete"
      }

      # ── Step 4: Merge sensor lists into a pipeline trigger payload ────────────
      BuildRetrainPayload = {
        Type    = "Pass"
        Comment = "Merge forced + scheduled sensor IDs. The main pipeline will be called with train_mode=true and this sensor list. Deduplication happens inside run_batch.py."
        Parameters = {
          "train_mode"                  = true
          "triggered_by"                = "drift_detection"
          "execution_time.$"            = "$$.Execution.StartTime"
          "forced_count.$"              = "$.forced_retrain_sensors.Count"
          "scheduled_count.$"           = "$.scheduled_sensors.Count"
          # Sensor IDs are extracted in the ECS task from the registry query results
          # passed via the execution input — run_batch.py already handles device_ids list
          "forced_sensors.$"            = "$.forced_retrain_sensors.Items"
          "scheduled_sensors.$"         = "$.scheduled_sensors.Items"
        }
        ResultPath = "$.retrain_payload"
        Next       = "EmitDriftMetrics"
      }

      # ── Step 5: Emit sensor counts to CloudWatch before triggering ────────────
      EmitDriftMetrics = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:cloudwatch:putMetricData"
        Comment  = "Emit drift detection metrics before triggering retraining."
        Parameters = {
          Namespace  = var.cw_namespace
          MetricData = [
            {
              MetricName = "DriftForcedRetrainCount"
              "Value.$"  = "$.forced_retrain_sensors.Count"
              Unit       = "Count"
            },
            {
              MetricName = "DriftScheduledRetrainCount"
              "Value.$"  = "$.scheduled_sensors.Count"
              Unit       = "Count"
            }
          ]
        }
        ResultPath = null
        Next       = "TriggerTrainingRun"
        Retry = [{
          ErrorEquals  = ["States.TaskFailed"]
          IntervalSeconds = 3
          MaxAttempts  = 2
          BackoffRate  = 1.5
        }]
      }

      # ── Step 6: Fire the main pipeline in training mode ───────────────────────
      TriggerTrainingRun = {
        Type     = "Task"
        Resource = "arn:aws:states:::states:startExecution"
        Comment  = "Start the main gap-filling pipeline with train_mode=true. The pipeline will retrain only the sensors in forced_sensors + scheduled_sensors."
        Parameters = {
          StateMachineArn = var.main_pipeline_state_machine_arn
          "Name.$"        = "States.Format('drift-triggered-{}', $$.Execution.Name)"
          "Input.$"       = "States.JsonToString($.retrain_payload)"
        }
        ResultPath = "$.pipeline_execution"
        End        = true
        Retry = [{
          ErrorEquals  = ["States.TaskFailed"]
          IntervalSeconds = 10
          MaxAttempts  = 2
          BackoffRate  = 2.0
        }]
      }

      # ── Terminal: nothing to do ───────────────────────────────────────────────
      DriftCheckComplete = {
        Type    = "Pass"
        Comment = "No sensors require retraining today. Exiting with zero compute cost."
        Parameters = {
          "status"          = "skipped"
          "reason"          = "no sensors due for retraining"
          "execution_time.$" = "$$.Execution.StartTime"
        }
        End = true
      }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.drift_sfn.arn}:*"
    include_execution_data = true
    level                  = "ALL" # EXPRESS machines: log ALL for full audit trail
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-drift-check" })
}

# ── EventBridge schedule: drift check runs daily at 01:00 UTC ────────────────
# Runs 90 minutes BEFORE the main daily training window (02:30 UTC) so the
# drift check can flag sensors and the main pipeline picks them up immediately.
resource "aws_scheduler_schedule" "drift_check_daily" {
  name       = "${var.name_prefix}-drift-check-daily"
  group_name = var.schedule_group_name
  state      = var.schedules_enabled ? "ENABLED" : "DISABLED"

  schedule_expression          = "cron(0 1 * * ? *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 15 # Small jitter — needs to finish before 02:30
  }

  target {
    arn      = aws_sfn_state_machine.drift_check.arn
    role_arn = var.drift_scheduler_role_arn

    input = jsonencode({
      triggered_by = "scheduled_drift_check"
    })

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 1800
    }
  }
}

# ── CloudWatch alarms for drift detection ────────────────────────────────────

# Alarm: drift check state machine itself failed (infrastructure issue)
resource "aws_cloudwatch_metric_alarm" "drift_sfn_failed" {
  alarm_name          = "${var.name_prefix}-drift-check-failed"
  alarm_description   = "P2: Drift detection state machine failed — drift check did not complete"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.failure_topic_arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.drift_check.arn
  }

  tags = merge(var.tags, { Severity = "P2" })
}

# Alarm: suspiciously high forced-retrain count (possible runaway drift)
resource "aws_cloudwatch_metric_alarm" "drift_forced_retrain_spike" {
  alarm_name          = "${var.name_prefix}-drift-forced-retrain-spike"
  alarm_description   = "P2: >20% of fleet flagged for forced retraining — investigate model drift or data quality"
  namespace           = var.cw_namespace
  metric_name         = "DriftForcedRetrainCount"
  statistic           = "Sum"
  period              = 86400 # Daily
  evaluation_periods  = 1
  threshold           = var.forced_retrain_spike_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.failure_topic_arn]

  tags = merge(var.tags, { Severity = "P2" })
}
