# =============================================================================
# modules/model_registry/iam_extension.tf
#
# Extends the existing task role (from modules/iam) with permissions for
# the model registry table. Applied as a SEPARATE inline policy so it can
# be added without modifying the existing iam module.
#
# Usage in environment root (environments/prod/main.tf extensions):
#   resource "aws_iam_role_policy" "task_model_registry" {
#     name   = "model-registry-access"
#     role   = module.iam.task_role_name
#     policy = module.model_registry.task_policy_json
#   }
# =============================================================================

# This file exports a data source output so the environment root can attach
# the policy without creating a circular dependency.
data "aws_iam_policy_document" "task_model_registry" {
  statement {
    sid    = "ModelRegistryReadWrite"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:BatchWriteItem",
    ]
    resources = [
      aws_dynamodb_table.model_registry.arn,
      # GSI ARNs for Query access
      "${aws_dynamodb_table.model_registry.arn}/index/gsi_status_trained",
      "${aws_dynamodb_table.model_registry.arn}/index/gsi_next_train",
    ]
  }
}

output "task_policy_json" {
  value       = data.aws_iam_policy_document.task_model_registry.json
  description = "IAM policy JSON to attach to the existing task role for model registry access. Apply via aws_iam_role_policy in the environment root."
}
