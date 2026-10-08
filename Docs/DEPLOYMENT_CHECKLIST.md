> **Status: earlier design (not the current production path).** This document describes the first AWS design — S3 CSV inputs/outputs, DynamoDB sensor-metadata/results tables and a Step Functions fan-out wrapped around `src/`. The current production implementation lives in [`aws/production/`](../aws/production/DEPLOY.md) (DynamoDB as the source *and* destination, one scheduled ECS Fargate task, no Step Functions/Lambda). This file is kept for reference.

# Deployment Checklist — Annam AI IoT Gap-Filling Pipeline

**Instructions:** Work through each section in order. Check off items as you go.  
Blocked items are marked **[BLOCKER]** — do not proceed past them until resolved.

---

## Phase 0: Pre-Deployment Verification

### Local tools
- [ ] `aws --version` → v2.15 or higher
- [ ] `docker --version` → 24.0 or higher
- [ ] `python --version` → 3.11 or higher
- [ ] `jq --version` → 1.6 or higher
- [ ] `aws sts get-caller-identity` succeeds (credentials configured)

### AWS account
- [ ] Deploying account has AdministratorAccess for this session
- [ ] Target region confirmed: `echo $AWS_REGION`
- [ ] ACCOUNT_ID confirmed: `aws sts get-caller-identity --query Account --output text`
- [ ] VPC exists with at least 2 private subnets in different AZs
  - Subnet 1: `PRIVATE_SUBNET_1=_____________`
  - Subnet 2: `PRIVATE_SUBNET_2=_____________`
  - Security group for tasks: `TASK_SG=_____________`
- [ ] VPC ID confirmed: `VPC_ID=_____________`

### Environment file
- [ ] `.env.production` created from `infrastructure/env.template`
- [ ] All required fields filled in (no blank values for REQUIRED section)
- [ ] File is in `.gitignore` and will NOT be committed

---

## Phase 1: Storage

### S3
- [ ] **[BLOCKER]** Bucket name chosen and unique: `DATA_BUCKET=_____________`
- [ ] Bucket created in correct region
- [ ] Public access blocked on bucket
- [ ] Versioning enabled (protects model artifacts from accidental overwrite)
- [ ] Default SSE-AES256 encryption enabled
- [ ] `echo $DATA_BUCKET` prints the expected bucket name

### DynamoDB
- [ ] `bash infrastructure/dynamodb/create_tables.sh` — exit code 0
- [ ] Table `annam-sensor-metadata` status is ACTIVE:
  `aws dynamodb describe-table --table-name annam-sensor-metadata --query Table.TableStatus`
- [ ] Table `annam-gapfill-results` status is ACTIVE
- [ ] TTL enabled on `annam-gapfill-results` (attribute: `ttl`)
- [ ] GSI `gsi_status` present on `annam-sensor-metadata`

---

## Phase 2: IAM and Security

### IAM roles
- [ ] `bash infrastructure/iam/create_roles.sh` — exit code 0
- [ ] `annam-gapfill-task-role` exists:
  `aws iam get-role --role-name annam-gapfill-task-role --query Role.Arn`
- [ ] `annam-ecs-execution-role` exists
- [ ] `annam-stepfunctions-role` exists
- [ ] `annam-scheduler-role` exists
- [ ] All role ARNs saved to `.env.production`

### SNS
- [ ] **[BLOCKER]** Failure SNS topic created:
  `aws sns create-topic --name annam-gapfill-failures`
- [ ] `FAILURE_TOPIC_ARN` saved to `.env.production`
- [ ] Ops email subscribed to topic
- [ ] **[BLOCKER]** Email subscription confirmed (check inbox for AWS confirmation email)

---

## Phase 3: Networking

### VPC Endpoints
- [ ] **[BLOCKER]** S3 Gateway endpoint created (free — required for S3 access from private subnet)
- [ ] **[BLOCKER]** DynamoDB Gateway endpoint created (free — required for DynamoDB from private subnet)
- [ ] ECR API Interface endpoint created
- [ ] ECR DKR Interface endpoint created
- [ ] CloudWatch Logs Interface endpoint created
- [ ] Step Functions Interface endpoint created (needed for .sync ECS callback)
- [ ] Private DNS enabled on all interface endpoints
- [ ] Task security group allows port 443 egress to endpoint ENIs

---

## Phase 4: Container

### ECR repository
- [ ] Repository `annam-gapfill` exists in target region
- [ ] Image scanning on push enabled
- [ ] `IMAGE_URI=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/annam-gapfill` confirmed

### Docker build
- [ ] `docker build -f aws/Dockerfile -t annam-gapfill:local .` — succeeds
- [ ] Local single-sensor test passes (see DEPLOYMENT_GUIDE.md §6 Docker command)
- [ ] ECR login succeeds: `aws ecr get-login-password | docker login ...`
- [ ] `docker push "$IMAGE_URI:latest"` — succeeds
- [ ] `docker push "$IMAGE_URI:<git-sha>"` — succeeds (traceability)

---

## Phase 5: Compute

### ECS cluster
- [ ] `bash infrastructure/ecs/deploy_cluster.sh` — exit code 0
- [ ] Cluster `annam-gapfill` status is ACTIVE
- [ ] Capacity providers FARGATE and FARGATE_SPOT attached
- [ ] Container Insights enabled
- [ ] Log groups created:
  - `/ecs/annam-run-batch` with 30-day retention
  - `/ecs/annam-list-sensors` with 30-day retention

### Task definitions
- [ ] Task definition `annam-run-batch` registered (1 vCPU / 2 GB / 30 GB ephemeral)
- [ ] Task definition `annam-list-sensors` registered (0.25 vCPU / 0.5 GB)
- [ ] Both task defs reference the correct ECR image URI
- [ ] Both task defs reference `annam-gapfill-task-role` and `annam-ecs-execution-role`
- [ ] `DATA_BUCKET` env var is set in both task defs
- [ ] `MPLBACKEND=Agg` is set in `annam-run-batch` task def

---

## Phase 6: Orchestration

### Step Functions
- [ ] All required ARNs exported:
  - `ECS_CLUSTER_ARN=_____________`
  - `RUN_BATCH_TASKDEF_ARN=_____________`
  - `LIST_SENSORS_TASKDEF_ARN=_____________`
  - `SFN_ROLE_ARN=_____________`
  - `PRIVATE_SUBNET_1`, `PRIVATE_SUBNET_2`, `TASK_SG` set
  - `DATA_BUCKET` set
  - `FAILURE_TOPIC_ARN` set
- [ ] `bash infrastructure/stepfunctions/deploy_statemachine.sh` — exit code 0
- [ ] State machine `annam-gapfill` exists and is ACTIVE
- [ ] `SFN_ARN` captured and saved

### EventBridge Scheduler
- [ ] `SCHEDULER_ROLE_ARN` exported
- [ ] `bash infrastructure/eventbridge/deploy_schedules.sh` — exit code 0
- [ ] Schedule `annam-gapfill-daily-training` is ENABLED (cron 30 2 * * ? *)
- [ ] Schedule `annam-gapfill-hourly-inference` is ENABLED (rate 1 hour) — disable if not needed
- [ ] Flexible time windows set (training: 30 min, inference: 10 min)

---

## Phase 7: Data

### Sensor metadata
- [ ] `sensors.csv` prepared with columns: `device_id, latitude, longitude`
- [ ] `python infrastructure/dynamodb/seed_sensors.py --from-coords sensors.csv --k 2 --max-km 30` — exit code 0
- [ ] Verify one sensor item: `aws dynamodb get-item --table-name annam-sensor-metadata --key '{"PK":{"S":"SENSOR#216"},"SK":{"S":"META"}}'`
- [ ] `neighbors` attribute is non-empty on each sensor item
- [ ] All sensors have `status: ACTIVE`

### Raw CSVs
- [ ] All target sensor CSVs uploaded to `s3://$DATA_BUCKET/raw/<id>/<id>.csv`
- [ ] All neighbour CSVs uploaded to their respective raw prefixes
- [ ] CSVs have `TimeStamp` column and required sensor columns (`CorrectedTemp`, etc.)
- [ ] Verify: `aws s3 ls s3://$DATA_BUCKET/raw/ --recursive | head -20`

---

## Phase 8: Observability

### CloudWatch Dashboard
- [ ] `bash infrastructure/cloudwatch/deploy_dashboard.sh` — exit code 0
- [ ] Dashboard `AnnamAI-GapFilling` visible in CloudWatch console

### CloudWatch Alarms
- [ ] `bash infrastructure/cloudwatch/deploy_alarms.sh` — exit code 0
- [ ] 5 alarms created:
  - `annam-sensors-failed-high` (P1)
  - `annam-sfn-executions-failed` (P1)
  - `annam-batch-duration-high` (P2)
  - `annam-unresolved-rows-high` (P2)
  - `annam-no-sensors-processed` (P2)
- [ ] All alarms point to `FAILURE_TOPIC_ARN`
- [ ] All alarms currently in `OK` or `INSUFFICIENT_DATA` state (not `ALARM`)

---

## Phase 9: Smoke Testing

### Single sensor test
- [ ] Docker single-sensor run completes with exit code 0 (see DEPLOYMENT_GUIDE.md §6)
- [ ] `filled_dataset.csv` appears in `s3://$DATA_BUCKET/filled/<today>/<id>/`
- [ ] `audit_report.csv`, `evaluation_report.csv` also present
- [ ] DynamoDB result record written:
  `aws dynamodb get-item --table-name annam-gapfill-results --key '{"PK":{"S":"SENSOR#216"},"SK":{"S":"RUN#<date>#smoke-test-001"}}'`
- [ ] CloudWatch log entry visible in `/ecs/annam-run-batch` (wait 1 min after run)

### Full pipeline test
- [ ] Manual Step Functions execution started:
  `aws stepfunctions start-execution --state-machine-arn "$SFN_ARN" --name "smoke-full-$(date +%s)" --input '{"train_mode": true}'`
- [ ] DiscoverAndBatch state completes (check console)
- [ ] LoadBatches state completes
- [ ] ProcessBatches Map state completes (all/most tasks succeed)
- [ ] Summarize state reached (execution status: SUCCEEDED)
- [ ] CloudWatch metrics appear in `AnnamAI/GapFilling` namespace:
  `aws cloudwatch list-metrics --namespace AnnamAI/GapFilling --region "$AWS_REGION"`
- [ ] Dashboard shows data

---

## Phase 10: Handover Verification

- [ ] `.env.production` safely stored in secrets manager or team password vault (not in git)
- [ ] `SFN_ARN` documented and shared with team
- [ ] Ops email confirmed receiving SNS test notification
- [ ] DEPLOYMENT_GUIDE.md, AWS_ARCHITECTURE.md, PRODUCTION_RUNBOOK.md reviewed by another engineer
- [ ] On-call runbook link added to team incident management tool
- [ ] Cost alert configured in AWS Billing (e.g., alert if monthly spend > $500)
- [ ] Next scheduled training run time confirmed: `02:30 UTC ± 30 min`

---

## Sign-off

| Role | Name | Date | Initials |
|---|---|---|---|
| Deploying engineer | | | |
| Reviewer | | | |
| Team lead / ML owner | | | |
