# =============================================================================
# modules/iam/main.tf
#
# Four IAM roles, each with the minimum permissions verified against the
# pipeline's source files (io_s3.py, io_dynamo.py, run_batch.py, list_sensors.py).
#
# CIRCULAR DEPENDENCY RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────
# The IAM scheduler role needs the state machine ARN (from the stepfunctions
# module), and the stepfunctions module needs the SFN role ARN (from here).
# We break this by splitting the scheduler policy into two parts:
#   Part 1 (this module): role skeleton with a placeholder deny-all policy
#   Part 2 (environments/*/main.tf): a separate aws_iam_role_policy resource
#            applied AFTER stepfunctions outputs are available
#
# The task role similarly needs the SNS topic ARN and state machine ARN for
# its policy, but those are only needed by the SCHEDULER and SFN roles, not
# the task role itself. The task role only needs S3, DDB, and CW — no cycle.
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
}

# ── 1. Fargate Task Role ──────────────────────────────────────────────────────
resource "aws_iam_role" "task" {
  name        = "${var.name_prefix}-task-role"
  description = "Fargate gap-filling task: S3 R/W, DynamoDB read/write, CloudWatch metrics"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        ArnLike = {
          "aws:SourceArn" = "arn:aws:ecs:${local.region}:${local.account_id}:*"
        }
      }
    }]
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-task-role" })
}

resource "aws_iam_role_policy" "task_s3" {
  name = "s3-data-access"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ObjectAccess"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${var.data_bucket_arn}/raw/*",
          "${var.data_bucket_arn}/models/*",
          "${var.data_bucket_arn}/filled/*",
          "${var.data_bucket_arn}/reports/*",
          "${var.data_bucket_arn}/runs/*",
        ]
      },
      {
        Sid      = "S3ListBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [var.data_bucket_arn]
        Condition = {
          StringLike = {
            "s3:prefix" = ["raw/*", "models/*", "filled/*", "reports/*", "runs/*"]
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_dynamodb" {
  name = "dynamodb-access"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "MetadataRead"
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = [
          var.sensor_table_arn,
          var.sensor_table_gsi_arn,
        ]
      },
      {
        Sid      = "ResultsWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:BatchWriteItem"]
        Resource = [var.results_table_arn]
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_cloudwatch" {
  name = "cloudwatch-metrics"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "PutMetricsNamespaceScoped"
      Effect   = "Allow"
      Action   = ["cloudwatch:PutMetricData"]
      Resource = ["*"]
      Condition = {
        StringEquals = { "cloudwatch:namespace" = var.cw_namespace }
      }
    }]
  })
}

# ── 2. ECS Execution Role ─────────────────────────────────────────────────────
resource "aws_iam_role" "execution" {
  name        = "${var.name_prefix}-execution-role"
  description = "ECS control plane: pull ECR image, write CloudWatch Logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-execution-role" })
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_ecr_scoped" {
  name = "ecr-repo-scoped"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ECRRepositoryAccess"
      Effect = "Allow"
      Action = [
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:BatchCheckLayerAvailability",
      ]
      Resource = [var.ecr_repository_arn]
    }]
  })
}

# ── 3. Step Functions Role ────────────────────────────────────────────────────
resource "aws_iam_role" "sfn" {
  name        = "${var.name_prefix}-sfn-role"
  description = "Step Functions: run ECS tasks, read S3 batch manifests, publish SNS failures"

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

  tags = merge(var.tags, { Name = "${var.name_prefix}-sfn-role" })
}

resource "aws_iam_role_policy" "sfn_ecs" {
  name = "ecs-run-tasks"
  role = aws_iam_role.sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RunFargateTasks"
        Effect   = "Allow"
        Action   = ["ecs:RunTask", "ecs:StopTask", "ecs:DescribeTasks"]
        Resource = ["*"]
      },
      {
        Sid    = "PassRolesToECS"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.task.arn,
          aws_iam_role.execution.arn,
        ]
      },
      {
        Sid    = "ECSTaskSync"
        Effect = "Allow"
        Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = [
          "arn:aws:events:${local.region}:${local.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "sfn_s3" {
  name = "s3-read-batches"
  role = aws_iam_role.sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadBatchManifests"
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = ["${var.data_bucket_arn}/runs/*"]
    }]
  })
}

# sfn_sns policy is applied via a SEPARATE resource in the environment root
# (environments/*/main.tf) AFTER the stepfunctions module has been applied
# and the SNS topic ARN is known. This avoids the IAM ↔ SFN dependency cycle.

# ── 4. EventBridge Scheduler Role ────────────────────────────────────────────
resource "aws_iam_role" "scheduler" {
  name        = "${var.name_prefix}-scheduler-role"
  description = "EventBridge Scheduler: start the Step Functions pipeline execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-scheduler-role" })
}

# Scheduler policy is applied via a SEPARATE resource in the environment root
# AFTER the stepfunctions module has been applied and state_machine_arn is known.
