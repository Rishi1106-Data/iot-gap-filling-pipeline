# infrastructure/ — earlier CLI-based deployment scripts

> **Status: earlier design (not the current production path).** These shell scripts and JSON
> definitions deploy the first AWS design (DynamoDB metadata/results tables, ECS task definitions,
> EventBridge schedules and a Step Functions state machine around the S3-based `aws/` layer).
> The current production stack is the single Terraform file
> [`aws/production/main.tf`](../aws/production/main.tf); see [`aws/production/DEPLOY.md`](../aws/production/DEPLOY.md).
> The modular Terraform in [`terraform/`](../terraform/) is likewise part of the earlier design.
> Nothing here is executed automatically.

| Path | Purpose (earlier design) |
|---|---|
| `dynamodb/` | Create sensor-metadata/results tables; seed sensors and neighbour topology |
| `ecs/` | Task definitions and cluster bootstrap |
| `eventbridge/` | Schedules |
| `iam/` | IAM policy documents |
| `stepfunctions/` | State-machine deployment |
