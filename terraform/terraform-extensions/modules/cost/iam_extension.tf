# =============================================================================
# modules/cost/iam_extension.tf
#
# Exports an IAM policy JSON that grants the existing Fargate task role access
# to the cost attribution table. Apply in the environment root:
#
#   resource "aws_iam_role_policy" "task_cost_attribution" {
#     name   = "cost-attribution-write"
#     role   = module.iam.task_role_name
#     policy = module.cost.task_policy_json
#   }
# =============================================================================

data "aws_iam_policy_document" "task_cost_attribution" {
  statement {
    sid    = "CostAttributionWrite"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.cost_attribution.arn]
  }

  statement {
    sid    = "CostAttributionReadForBudgets"
    effect = "Allow"
    actions = [
      "dynamodb:Query",
      "dynamodb:GetItem",
    ]
    resources = [
      aws_dynamodb_table.cost_attribution.arn,
      "${aws_dynamodb_table.cost_attribution.arn}/index/gsi_project_date",
    ]
  }
}

output "task_policy_json" {
  value       = data.aws_iam_policy_document.task_cost_attribution.json
  description = "IAM policy JSON to attach to the existing task role for cost attribution access."
}

# Budgets requires a special IAM permission on the account level (not resource-scoped)
# This output reminds operators to attach the AWS-managed Budgets policy if needed.
output "budgets_iam_note" {
  value = "AWS Budgets requires the 'aws-service-role/budgets.amazonaws.com' service-linked role. It is auto-created when the first budget is made. No manual IAM action needed."
}
