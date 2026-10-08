# main.tf — Production-ready weekly-batch infra for the Annam reconstruction job.
#
# Architecture:
#   EventBridge Scheduler (weekly cron) -> ONE Fargate task (run_batch.py)
#   -> reads DynamoDB, writes the reconstructed (gap-filled) rows back into the
#      SAME source DynamoDB tables, logs to CloudWatch. No reconstructed dataset
#      is written to S3 in normal execution (optional debug dump only).
#
# No Step Functions, no Lambda, no per-sensor fan-out. One task loops over all
# ~143 sensors sequentially, so the image pull and heavy library imports
# (xgboost / lightgbm / pyarrow) are paid once per run instead of 143 times.
#
# This file is apply-ready once the four values in `locals` marked REPLACE are
# filled in. Everything else (cluster, roles, log group, ECR, schedule, alarm,
# bucket hardening) is created here.
#
# Deploy:
#   1. terraform init
#   2. terraform apply           (creates ECR repo + all infra)
#   3. build & push the image to the ECR repo this creates (see DEPLOY.md)
#   4. run once manually to validate (see DEPLOY.md), then let the schedule run.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }

  # Recommended for team use: uncomment and point at your state bucket so state
  # is shared and locked. Safe to leave commented for a single-operator deploy.
  # backend "s3" {
  #   bucket         = "annam-terraform-state"
  #   key            = "annam-recon/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "annam-terraform-locks"
  # }
}

provider "aws" {
  region = local.region
}

# Account id comes from the active AWS credentials; nothing account-specific is hard-coded.
data "aws_caller_identity" "current" {}

locals {
  region     = "us-east-1"
  account_id = data.aws_caller_identity.current.account_id
  project    = "annam-recon"

  # ── REPLACE these four before apply ──────────────────────────────────────
  # A subnet + security group with a working egress path to DynamoDB and S3.
  # CHEAPEST CORRECT SETUP: a subnet that has VPC *gateway endpoints* for both
  # S3 and DynamoDB. Gateway endpoints are free and need no NAT gateway. A NAT
  # gateway alone costs ~$32/month — that would be 30x the entire rest of this
  # bill, so do not use one for this workload.
  #
  # If you instead use a public subnet with assign_public_ip = true, traffic
  # goes over the internet gateway (also no NAT). In that case the security
  # group MUST allow outbound 443. The default SG usually allows all egress.
  subnet_ids       = ["subnet-REPLACE_ME"] # REPLACE
  security_groups  = ["sg-REPLACE_ME"]      # REPLACE
  assign_public_ip = true                   # true for public subnet; false if using private subnet + VPC endpoints

  # S3 bucket used ONLY for the optional debug dump (S3_REPORT_ENABLED=true).
  # Must match OUTPUT_BUCKET in config.py. Normal runs write nothing to S3.
  output_bucket = "annam-reconstructed" # REPLACE if your bucket name differs

  # Phase 2 — incremental processing watermark table. Must match METADATA_TABLE
  # in config.py. Created as an optional resource below (see aws_dynamodb_table).
  metadata_table = "WS_Reconstruction_Metadata"

  # Phase 2 — configurable historical window (days). Must match LOOKBACK_DAYS in
  # config.py. Passed to the container as an env var so infra stays the source
  # of truth. Set to "0" to read full history.
  lookback_days = "60"

  # Optional: SNS topic ARN to notify on task failure. Leave "" to skip wiring
  # an alarm action (the alarm is still created and visible in CloudWatch).
  alarm_sns_topic_arn = "" # e.g. "arn:aws:sns:us-east-1:<ACCOUNT_ID>:annam-alerts"
  # ──────────────────────────────────────────────────────────────────────────

  image_tag = "latest"
  image_uri = "${aws_ecr_repository.worker.repository_url}:${local.image_tag}"

  # Weekly: 02:00 UTC every Monday.
  schedule_expression = "cron(0 2 ? * MON *)"

  # Right-sizing. Measured: one sensor with ~16 days of 5-min data reconstructs
  # in ~8s at ~290 MB peak. 143 sensors of that size = a ~40-min batch. If your
  # sensors carry a FULL YEAR of history, a single sensor is ~20x heavier and
  # the batch can approach 12-15 hours — in that case either set START_DATE in
  # config.py to bound history, or raise cpu/memory. 2 vCPU / 8 GB is a safe
  # default with generous memory headroom (measured peak was well under 1 GB).
  task_cpu    = "2048"
  task_memory = "8192"

  # x86 vs ARM64. ARM64 (Graviton) Fargate is ~20% cheaper, BUT the image must
  # be built for arm64 (docker buildx --platform linux/arm64) AND all wheels
  # (xgboost, lightgbm, pyarrow) must resolve for aarch64. If you build on a
  # typical x86 CI/laptop with a plain `docker build`, leave this "X86_64" or
  # the task will fail to start with "exec format error". Flip to "ARM64" only
  # after confirming an arm64 build. See DEPLOY.md.
  cpu_architecture = "X86_64"
}

# ──────────────────────────────────────────────────────────────────────────────
# ECR — the repository the image is pushed to (created here so infra is one-shot)
# ──────────────────────────────────────────────────────────────────────────────
resource "aws_ecr_repository" "worker" {
  name                 = "${local.project}-worker"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

# Keep only the last few images so ECR storage doesn't grow forever.
resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

# ──────────────────────────────────────────────────────────────────────────────
# S3 — reference the existing output bucket and harden it.
#   The bucket is assumed to already exist (created out-of-band so Terraform
#   never risks destroying data). We attach security + lifecycle to it.
# ──────────────────────────────────────────────────────────────────────────────
data "aws_s3_bucket" "output" {
  bucket = local.output_bucket
}

resource "aws_s3_bucket_public_access_block" "output" {
  bucket                  = data.aws_s3_bucket.output.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "output" {
  bucket = data.aws_s3_bucket.output.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Expire reconstructed output after 180 days so storage stays flat. Adjust or
# remove if you need to retain history longer.
resource "aws_s3_bucket_lifecycle_configuration" "output" {
  bucket = data.aws_s3_bucket.output.id
  rule {
    id     = "expire-reconstructed"
    status = "Enabled"
    filter {
      prefix = "reconstructed/"
    }
    expiration {
      days = 180
    }
  }
}

# ──────────────────────────────────────────────────────────────────────────────
# ECS cluster (no orchestration layer — just a place for the task to run)
# ──────────────────────────────────────────────────────────────────────────────
resource "aws_ecs_cluster" "this" {
  name = local.project
}

# ──────────────────────────────────────────────────────────────────────────────
# CloudWatch log group — retention capped so logs don't accumulate forever.
# ──────────────────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "task" {
  name              = "/ecs/${local.project}"
  retention_in_days = 14
}

# ──────────────────────────────────────────────────────────────────────────────
# IAM — execution role (pull image, write logs) + task role (read/write DynamoDB; optional S3 debug)
# ──────────────────────────────────────────────────────────────────────────────
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.project}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "task" {
  name               = "${local.project}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# Least-privilege task policy. Mirrors iam_task_policy.json exactly:
#   * read the data + neighbor tables (WS_*, SSMet_*, gateway-prediction)
#   * write reconstructed rows back to the source tables; PutObject on the
#     output bucket only for the optional S3 debug dump
# Scan is included because the sensor-list step is folded into the batch task.
data "aws_iam_policy_document" "task" {
  statement {
    sid     = "DynamoReads"
    actions = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"]
    resources = [
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/WS_*",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/SSMet_*",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/gateway-prediction",
    ]
  }
  # Successfully reconstructed rows are written back into the SAME source data
  # table they were read from, one conditional PutItem per row: an item is
  # created only if its key is free or it is itself a previous reconstruction
  # (filled_flag = 1), so a real reading is never overwritten. Unresolved slots
  # are not written. Batch writes are not used, so only PutItem is granted.
  statement {
    sid     = "DynamoWriteBackReconstructed"
    actions = ["dynamodb:PutItem"]
    resources = [
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/WS_*",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/SSMet_*",
    ]
  }
  # Phase 2 (Improvement 2): the incremental-processing watermark table needs
  # write access. Its name matches the WS_* read pattern above, so reads are
  # already covered; this statement adds PutItem for the watermark upsert.
  statement {
    sid     = "DynamoMetadataWrite"
    actions = ["dynamodb:PutItem", "dynamodb:GetItem"]
    resources = [
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${local.metadata_table}",
    ]
  }
  # Only needed when the optional S3 debug output is enabled
  # (S3_REPORT_ENABLED=true). Normal execution writes nothing to S3; kept here
  # so the debug dump works without an IAM change if it is ever switched on.
  statement {
    sid       = "S3WriteOutputOptionalDebug"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::${local.output_bucket}/*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.project}-task-policy"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ──────────────────────────────────────────────────────────────────────────────
# Incremental-processing watermark table (Phase 2, Improvement 2)
#   One tiny item per sensor (DeviceId, LastProcessed, LastDataTimestamp,
#   GapCount). PAY_PER_REQUEST so it costs effectively nothing at ~143 items and
#   a handful of reads/writes per weekly run. If the table already exists in the
#   account, remove this resource and import it, or leave INCREMENTAL_ENABLED
#   pointed at the existing table — the app only needs GetItem/PutItem.
# ──────────────────────────────────────────────────────────────────────────────
resource "aws_dynamodb_table" "reconstruction_metadata" {
  name         = local.metadata_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "DeviceId"

  attribute {
    name = "DeviceId"
    type = "S"
  }

  tags = {
    Project = local.project
    Purpose = "incremental-reconstruction-watermark"
  }
}

# ──────────────────────────────────────────────────────────────────────────────
# Task definition — one container, batch entrypoint
# ──────────────────────────────────────────────────────────────────────────────
resource "aws_ecs_task_definition" "this" {
  family                   = "${local.project}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.task_cpu
  memory                   = local.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = local.cpu_architecture
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = "${local.project}-worker"
      image     = local.image_uri
      essential = true
      environment = [
        { name = "AWS_REGION", value = local.region },
        { name = "OUTPUT_BUCKET", value = local.output_bucket },
        # Phase 2 knobs — infra is the source of truth; config.py reads these.
        { name = "LOOKBACK_DAYS", value = local.lookback_days },
        { name = "INCREMENTAL_ENABLED", value = "true" },
        { name = "METADATA_TABLE", value = local.metadata_table },
        { name = "NEIGHBOR_CACHE_ENABLED", value = "true" },
        { name = "MAX_WORKERS", value = "4" },
        # Reconstructed rows go back into the source DynamoDB table. Keep S3 off
        # in normal execution; set to "true" only for offline debug dumps.
        { name = "S3_REPORT_ENABLED", value = "false" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.task.name
          "awslogs-region"        = local.region
          "awslogs-stream-prefix" = "batch"
        }
      }
    }
  ])
}

# ──────────────────────────────────────────────────────────────────────────────
# EventBridge Scheduler — weekly trigger that runs the task directly.
# ──────────────────────────────────────────────────────────────────────────────
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.project}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

# RunTask on any revision of this family, plus PassRole for the two task roles.
data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "RunTask"
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${local.region}:${local.account_id}:task-definition/${local.project}-worker:*"]
  }
  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.project}-scheduler-policy"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "weekly" {
  name = "${local.project}-weekly"
  flexible_time_window {
    mode = "OFF"
  }
  schedule_expression          = local.schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.this.arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = local.subnet_ids
        security_groups  = local.security_groups
        assign_public_ip = local.assign_public_ip
      }
    }

    # If the whole run fails to launch, retry twice. NOTE: a retry re-runs the
    # ENTIRE batch from sensor 1 (there is no checkpoint); sensors already
    # processed are recomputed; their filled rows are simply re-put (same keys) — wasteful but safe.
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}

# ──────────────────────────────────────────────────────────────────────────────
# Failure visibility — a weekly job that fails SILENTLY is the real danger.
#   run_batch.py exits non-zero only if EVERY sensor failed. This metric filter
#   + alarm surfaces a fully-failed run; for partial failures, inspect the
#   per-sensor status lines in the CloudWatch logs.
# ──────────────────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_log_metric_filter" "batch_all_failed" {
  name           = "${local.project}-all-failed"
  log_group_name = aws_cloudwatch_log_group.task.name
  # Matches the summary line "Batch done ... 0 ok," emitted when nothing succeeded.
  pattern = "\"Batch done\" \"0 ok\""
  metric_transformation {
    name      = "BatchAllFailed"
    namespace = "Annam/Recon"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "batch_all_failed" {
  alarm_name          = "${local.project}-all-failed"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = aws_cloudwatch_log_metric_filter.batch_all_failed.metric_transformation[0].name
  namespace           = "Annam/Recon"
  period              = 86400
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_description    = "Annam reconstruction batch run had zero successful sensors."
  alarm_actions       = local.alarm_sns_topic_arn == "" ? [] : [local.alarm_sns_topic_arn]
}

# ──────────────────────────────────────────────────────────────────────────────
# Outputs
# ──────────────────────────────────────────────────────────────────────────────
output "ecr_repository_url" {
  description = "Push the image here (see DEPLOY.md step 3)."
  value       = aws_ecr_repository.worker.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.this.arn
}

output "manual_run_command" {
  description = "Run the batch once by hand to validate before trusting the schedule."
  value = "aws ecs run-task --cluster ${aws_ecs_cluster.this.name} --launch-type FARGATE --task-definition ${local.project}-worker --network-configuration 'awsvpcConfiguration={subnets=${jsonencode(local.subnet_ids)},securityGroups=${jsonencode(local.security_groups)},assignPublicIp=${local.assign_public_ip ? "ENABLED" : "DISABLED"}}' --region ${local.region}"
}

# ──────────────────────────────────────────────────────────────────────────────
# REMINDER: confirm the WS_* / SSMet_* tables are PAY_PER_REQUEST (on-demand).
# For a once-weekly read burst, provisioned capacity sits idle ~99.4% of the
# week and costs far more than the handful of reads this job does. Check with:
#   aws dynamodb describe-table --table-name WS_Data_Full \
#     --query 'Table.BillingModeSummary.BillingMode'
# ──────────────────────────────────────────────────────────────────────────────
