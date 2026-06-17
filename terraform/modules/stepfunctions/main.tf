# =============================================================================
# modules/stepfunctions/main.tf
#
# Creates:
#   - SNS failure notification topic (+ email subscription if provided)
#   - Step Functions STANDARD state machine (ASL from template file)
#
# The state machine definition references ECS ARNs, subnet IDs, SG IDs,
# S3 bucket name, and SNS topic ARN — all injected via templatefile().
# =============================================================================

# ── SNS failure topic ─────────────────────────────────────────────────────────
resource "aws_sns_topic" "failures" {
  name              = "${var.name_prefix}-failures"
  kms_master_key_id = "alias/aws/sns"

  tags = merge(var.tags, { Name = "${var.name_prefix}-failures" })
}

resource "aws_sns_topic_subscription" "ops_email" {
  count     = var.ops_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.failures.arn
  protocol  = "email"
  endpoint  = var.ops_email
}

# ── State machine ─────────────────────────────────────────────────────────────
resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.name_prefix}-pipeline"
  role_arn = var.sfn_role_arn
  type     = "STANDARD"

  definition = templatefile("${path.module}/definition.json.tftpl", {
    ecs_cluster_arn             = var.ecs_cluster_arn
    run_batch_taskdef_arn       = var.run_batch_taskdef_arn
    list_sensors_taskdef_arn    = var.list_sensors_taskdef_arn
    private_subnet_1            = var.private_subnet_ids[0]
    private_subnet_2            = var.private_subnet_ids[1]
    task_security_group         = var.task_security_group_id
    data_bucket                 = var.data_bucket_name
    failure_topic_arn           = aws_sns_topic.failures.arn
    map_max_concurrency         = var.map_max_concurrency
    tolerated_failure_percentage = var.tolerated_failure_percentage
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = false
    level                  = "ERROR"
  }

  tracing_configuration {
    enabled = var.enable_xray_tracing
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-pipeline" })
}

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/states/${var.name_prefix}-pipeline"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}
