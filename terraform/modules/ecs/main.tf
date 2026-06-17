# =============================================================================
# modules/ecs/main.tf
#
# Creates:
#   - ECS cluster with Fargate + Fargate Spot capacity providers
#   - CloudWatch log groups (configurable retention)
#   - Task definition: run-batch   (1 vCPU / 2 GB / 30 GB ephemeral)
#   - Task definition: list-sensors (0.25 vCPU / 0.5 GB)
#
# Sizing rationale (measured production runs):
#   Peak RSS per sensor: ~257 MB → 2 GB fits with headroom for neighbour cache
#   Runtime per sensor:  ~115 s  → BATCH_SIZE=25 → ~48 min per task
# =============================================================================

# ── Cluster ───────────────────────────────────────────────────────────────────
resource "aws_ecs_cluster" "this" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-cluster" })
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 4
    base              = 0
  }

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 0
  }
}

# ── CloudWatch Log Groups ─────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "run_batch" {
  name              = "/ecs/${var.name_prefix}-run-batch"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "list_sensors" {
  name              = "/ecs/${var.name_prefix}-list-sensors"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# ── Task Definition: run-batch ────────────────────────────────────────────────
resource "aws_ecs_task_definition" "run_batch" {
  family                   = "${var.name_prefix}-run-batch"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  task_role_arn            = var.task_role_arn
  execution_role_arn       = var.execution_role_arn

  ephemeral_storage {
    size_in_gib = 30
  }

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  # Container definition uses jsonencode for type safety and readability.
  # Note: 'user' is a top-level container field in the ECS API, not a JSON
  # sub-object — it must be a string value ("UID" or "UID:GID").
  container_definitions = jsonencode([
    {
      name      = "run-batch"
      image     = "${var.ecr_repository_url}:${var.image_tag}"
      essential = true
      user      = "10001"

      environment = [
        { name = "AWS_REGION",    value = var.aws_region },
        { name = "DATA_BUCKET",   value = var.data_bucket_name },
        { name = "SENSOR_TABLE",  value = var.sensor_table_name },
        { name = "RESULTS_TABLE", value = var.results_table_name },
        { name = "CW_NAMESPACE",  value = var.cw_namespace },
        { name = "METRICS_MODE",  value = "emf" },
        { name = "TRAIN_MODE",    value = tostring(var.train_mode) },
        { name = "BATCH_SIZE",    value = tostring(var.batch_size) },
        { name = "N_SPLITS",      value = tostring(var.n_splits) },
        { name = "VAL_N_GAPS",    value = tostring(var.val_n_gaps) },
        { name = "WORK_DIR",      value = "/tmp/iot" },
        { name = "MPLBACKEND",    value = "Agg" },
        { name = "LOG_LEVEL",     value = var.log_level },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.run_batch.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "batch"
        }
      }

      # 120s stop timeout gives in-flight sensors time to upload partial results
      # before the Fargate task is forcefully terminated (e.g. on Spot reclaim).
      stopTimeout = 120

      readonlyRootFilesystem = false
      privileged             = false
    }
  ])

  tags = merge(var.tags, { Name = "${var.name_prefix}-run-batch" })
}

# ── Task Definition: list-sensors ────────────────────────────────────────────
resource "aws_ecs_task_definition" "list_sensors" {
  family                   = "${var.name_prefix}-list-sensors"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  task_role_arn            = var.task_role_arn
  execution_role_arn       = var.execution_role_arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = "list-sensors"
      image     = "${var.ecr_repository_url}:${var.image_tag}"
      essential = true
      user      = "10001"

      environment = [
        { name = "AWS_REGION",    value = var.aws_region },
        { name = "DATA_BUCKET",   value = var.data_bucket_name },
        { name = "SENSOR_TABLE",  value = var.sensor_table_name },
        { name = "RESULTS_TABLE", value = var.results_table_name },
        { name = "CW_NAMESPACE",  value = var.cw_namespace },
        { name = "METRICS_MODE",  value = "emf" },
        { name = "BATCH_SIZE",    value = tostring(var.batch_size) },
        { name = "LOG_LEVEL",     value = var.log_level },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.list_sensors.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "discover"
        }
      }

      readonlyRootFilesystem = false
      privileged             = false
    }
  ])

  tags = merge(var.tags, { Name = "${var.name_prefix}-list-sensors" })
}
