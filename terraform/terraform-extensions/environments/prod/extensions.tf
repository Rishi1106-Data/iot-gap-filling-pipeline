# =============================================================================
# environments/prod/extensions.tf
#
# EXTENDS the existing environments/prod/main.tf with four new capabilities:
#
#   module.model_registry  — DynamoDB model registry (train-once/infer-many)
#   module.drift_detection — Autonomous retraining trigger state machine
#   module.cost            — Cost dashboard, budget, per-sensor attribution
#   Post-module IAM        — Least-privilege policy attachments to existing task role
#
# IMPORTANT: This file ADDS to the existing main.tf. It references outputs
# from modules already declared there (module.dynamodb, module.iam,
# module.stepfunctions, module.eventbridge, module.ecs).
# Do NOT re-declare those modules here.
#
# APPLY ORDER:
#   terraform apply (existing main.tf must have been applied first)
#   terraform apply -target=module.model_registry
#   terraform apply -target=module.cost
#   terraform apply -target=module.drift_detection (needs model_registry outputs)
#   terraform apply  (applies remaining resources: IAM policies, drift scheduler role)
# =============================================================================

# ── 10. Model Registry ────────────────────────────────────────────────────────
module "model_registry" {
  source = "../../modules/model_registry"

  name_prefix              = local.name_prefix
  enable_pitr              = true
  model_history_ttl_days   = 90
  retraining_interval_days = 7
  drift_mae_threshold_pct  = 20
  tags                     = local.common_tags
}

# ── 11. Cost Module ───────────────────────────────────────────────────────────
module "cost" {
  source = "../../modules/cost"

  name_prefix              = local.name_prefix
  environment              = local.env
  cw_namespace             = var.cw_namespace
  failure_topic_arn        = module.stepfunctions.failure_topic_arn
  run_batch_log_group_name = module.ecs.run_batch_log_group_name
  monthly_budget_usd       = var.monthly_budget_usd
  daily_cost_alarm_usd     = var.daily_cost_alarm_usd
  cost_per_sensor_alarm_usd = var.cost_per_sensor_alarm_usd
  attribution_retention_days = 90
  tags                     = local.common_tags
}

# ── 12. IAM role for drift-check EventBridge schedule ────────────────────────
# The existing scheduler role (module.iam.scheduler_role) is scoped to the
# main pipeline SFN. The drift-check has its own Express machine, so it gets
# its own scheduler target role — minimal blast radius.
resource "aws_iam_role" "drift_scheduler" {
  name        = "${local.name_prefix}-drift-scheduler-role"
  description = "EventBridge Scheduler: start the drift-check Express state machine"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })

  tags = local.common_tags
}

# Policy is applied AFTER drift module creates the state machine (cycle-break)
resource "aws_iam_role_policy" "drift_scheduler_start" {
  name = "start-drift-check"
  role = aws_iam_role.drift_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = [module.drift_detection.drift_state_machine_arn]
    }]
  })

  depends_on = [module.drift_detection]
}

# ── 13. Drift Detection Module ────────────────────────────────────────────────
module "drift_detection" {
  source = "../../modules/drift_detection"

  name_prefix                       = local.name_prefix
  model_registry_table_name         = module.model_registry.table_name
  model_registry_table_arn          = module.model_registry.table_arn
  model_registry_gsi_status_arn     = module.model_registry.gsi_status_trained_arn
  model_registry_gsi_next_train_arn = module.model_registry.gsi_next_train_arn
  main_pipeline_state_machine_arn   = module.stepfunctions.state_machine_arn
  failure_topic_arn                 = module.stepfunctions.failure_topic_arn
  schedule_group_name               = module.eventbridge.schedule_group_name
  drift_scheduler_role_arn          = aws_iam_role.drift_scheduler.arn
  schedules_enabled                 = true
  cw_namespace                      = var.cw_namespace
  forced_retrain_spike_threshold    = var.drift_forced_retrain_spike_threshold
  log_retention_days                = 30
  tags                              = local.common_tags

  depends_on = [aws_iam_role_policy.drift_scheduler_start]
}

# ── 14. IAM policy extensions — attach to existing task role ─────────────────
# These extend module.iam.task_role with permissions for the new tables.
# They are separate inline policies (not modifying the iam module) to
# preserve the existing module boundary.

resource "aws_iam_role_policy" "task_model_registry" {
  name   = "model-registry-access"
  role   = module.iam.task_role_name
  policy = module.model_registry.task_policy_json
}

resource "aws_iam_role_policy" "task_cost_attribution" {
  name   = "cost-attribution-write"
  role   = module.iam.task_role_name
  policy = module.cost.task_policy_json
}

# ── 15. ECS environment variable extensions ───────────────────────────────────
# The new tables need to be passed to Fargate tasks. Rather than modifying the
# ecs module (which would force a task def replacement and running-task drain),
# we surface them as SSM parameters. The application reads them at startup via
# the existing config mechanism, or they are injected as container overrides
# in the Step Functions state machine via the model_registry_table_name output.
#
# Alternatively, operators can update terraform.tfvars with the new table names
# and add them to the ECS task definition environment block in the next image
# deploy cycle (zero-downtime — task def update only applies to new tasks).

resource "aws_ssm_parameter" "model_registry_table_name" {
  name        = "/${local.name_prefix}/MODEL_REGISTRY_TABLE"
  type        = "String"
  value       = module.model_registry.table_name
  description = "Model registry DynamoDB table name for Fargate tasks"
  tags        = local.common_tags
}

resource "aws_ssm_parameter" "cost_attribution_table_name" {
  name        = "/${local.name_prefix}/COST_ATTRIBUTION_TABLE"
  type        = "String"
  value       = module.cost.cost_attribution_table_name
  description = "Cost attribution DynamoDB table name for Fargate tasks"
  tags        = local.common_tags
}
