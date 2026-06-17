# =============================================================================
# environments/prod/main.tf
#
# Production environment: wires all nine modules together.
#
# DEPENDENCY ORDER
# ─────────────────────────────────────────────────────────────────────────────
# Terraform resolves the graph automatically from output→input references.
# The only hand-managed dependency is the IAM ↔ StepFunctions cycle:
#
#   - iam module creates the sfn_role and scheduler_role skeletons first.
#   - stepfunctions module uses sfn_role_arn (from iam) to create the SM.
#   - Two post-SFN aws_iam_role_policy resources (in this file) then attach
#     the SNS publish and state-machine StartExecution policies once the ARNs
#     are known. This is the standard Terraform pattern for breaking cycles.
# =============================================================================

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  backend "s3" {
    bucket         = "annam-tfstate-REPLACE_WITH_ACCOUNT_ID"
    key            = "prod/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "annam-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

data "aws_caller_identity" "current" {}

locals {
  env         = "prod"
  name_prefix = "${var.project}-${local.env}"

  common_tags = {
    Project     = var.project
    Environment = local.env
    ManagedBy   = "terraform"
    Owner       = var.owner_team
  }
}

# ── 1. Networking ─────────────────────────────────────────────────────────────
module "networking" {
  source = "../../modules/networking"

  aws_region         = var.aws_region
  vpc_id             = var.vpc_id
  private_subnet_ids = var.private_subnet_ids
  name_prefix        = local.name_prefix
  tags               = local.common_tags
}

# ── 2. S3 ─────────────────────────────────────────────────────────────────────
module "s3" {
  source = "../../modules/s3"

  bucket_name                  = "${local.name_prefix}-data-${data.aws_caller_identity.current.account_id}"
  force_destroy                = false
  filled_retention_days        = 90
  reports_retention_days       = 90
  runs_retention_days          = 7
  model_version_retention_days = 30
  tags                         = local.common_tags
}

# ── 3. ECR ────────────────────────────────────────────────────────────────────
module "ecr" {
  source = "../../modules/ecr"

  repository_name = "${var.project}-gapfill"
  max_image_count = 20
  tags            = local.common_tags
}

# ── 4. DynamoDB ───────────────────────────────────────────────────────────────
module "dynamodb" {
  source = "../../modules/dynamodb"

  name_prefix = local.name_prefix
  enable_pitr = true
  tags        = local.common_tags
}

# ── 5. IAM (role skeletons — cycle-breaking policies added below) ─────────────
module "iam" {
  source = "../../modules/iam"

  name_prefix          = local.name_prefix
  data_bucket_arn      = module.s3.bucket_arn
  sensor_table_arn     = module.dynamodb.sensor_table_arn
  sensor_table_gsi_arn = module.dynamodb.sensor_table_gsi_arn
  results_table_arn    = module.dynamodb.results_table_arn
  ecr_repository_arn   = module.ecr.repository_arn
  cw_namespace         = var.cw_namespace
  tags                 = local.common_tags
}

# ── 6. ECS cluster + task definitions ────────────────────────────────────────
module "ecs" {
  source = "../../modules/ecs"

  name_prefix        = local.name_prefix
  aws_region         = var.aws_region
  task_role_arn      = module.iam.task_role_arn
  execution_role_arn = module.iam.execution_role_arn
  ecr_repository_url = module.ecr.repository_url
  image_tag          = var.image_tag
  data_bucket_name   = module.s3.bucket_id
  sensor_table_name  = module.dynamodb.sensor_table_name
  results_table_name = module.dynamodb.results_table_name
  cw_namespace       = var.cw_namespace
  log_retention_days = 30
  log_level          = "INFO"
  train_mode         = false
  batch_size         = var.batch_size
  n_splits           = var.n_splits
  val_n_gaps         = var.val_n_gaps
  tags               = local.common_tags
}

# ── 7. Step Functions (uses sfn_role_arn from iam module) ─────────────────────
module "stepfunctions" {
  source = "../../modules/stepfunctions"

  name_prefix                  = local.name_prefix
  sfn_role_arn                 = module.iam.sfn_role_arn
  ecs_cluster_arn              = module.ecs.cluster_arn
  run_batch_taskdef_arn        = module.ecs.run_batch_task_definition_arn
  list_sensors_taskdef_arn     = module.ecs.list_sensors_task_definition_arn
  private_subnet_ids           = var.private_subnet_ids
  task_security_group_id       = module.networking.task_security_group_id
  data_bucket_name             = module.s3.bucket_id
  ops_email                    = var.ops_email
  map_max_concurrency          = var.map_max_concurrency
  tolerated_failure_percentage = var.tolerated_failure_percentage
  log_retention_days           = 30
  enable_xray_tracing          = false
  tags                         = local.common_tags
}

# ── 7b. Post-SFN IAM policies (breaks the IAM ↔ SFN cycle) ──────────────────
# These policies need ARNs that only exist after module.stepfunctions is applied.
# Defining them here, outside the iam module, eliminates the circular reference.

resource "aws_iam_role_policy" "sfn_sns_publish" {
  name = "sns-publish-failures"
  role = module.iam.sfn_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "PublishFailureNotifications"
      Effect   = "Allow"
      Action   = ["sns:Publish"]
      Resource = [module.stepfunctions.failure_topic_arn]
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_start_sfn" {
  name = "start-state-machine"
  role = module.iam.scheduler_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "StartGapFillStateMachine"
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = [module.stepfunctions.state_machine_arn]
    }]
  })
}

# ── 8. EventBridge schedules ──────────────────────────────────────────────────
module "eventbridge" {
  source = "../../modules/eventbridge"

  name_prefix             = local.name_prefix
  state_machine_arn       = module.stepfunctions.state_machine_arn
  scheduler_role_arn      = module.iam.scheduler_role_arn
  schedules_enabled       = true
  enable_hourly_inference = var.enable_hourly_inference
  tags                    = local.common_tags

  depends_on = [aws_iam_role_policy.scheduler_start_sfn]
}

# ── 9. CloudWatch alarms + dashboard ─────────────────────────────────────────
module "cloudwatch" {
  source = "../../modules/cloudwatch"

  name_prefix                      = local.name_prefix
  environment                      = local.env
  cw_namespace                     = var.cw_namespace
  failure_topic_arn                = module.stepfunctions.failure_topic_arn
  state_machine_arn                = module.stepfunctions.state_machine_arn
  sensors_failed_threshold         = var.sensors_failed_threshold
  batch_duration_threshold_seconds = 3600
  unresolved_rows_threshold        = var.unresolved_rows_threshold
  tags                             = local.common_tags
}
