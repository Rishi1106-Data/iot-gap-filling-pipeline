> **Status: earlier design (not the current production path).** This document describes the first AWS design — S3 CSV inputs/outputs, DynamoDB sensor-metadata/results tables and a Step Functions fan-out wrapped around `src/`. The current production implementation lives in [`aws/production/`](../aws/production/DEPLOY.md) (DynamoDB as the source *and* destination, one scheduled ECS Fargate task, no Step Functions/Lambda). This file is kept for reference.

# Annam AI — IoT Gap-Filling Pipeline: Deployment Guide

**Audience:** Any engineer deploying this system from scratch.  
**Region:** `ap-south-1` (Mumbai) — change `AWS_REGION` throughout if different.  
**Estimated time:** 45–90 minutes for a first-time deploy; ~10 minutes for subsequent image updates.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Repository Setup](#2-repository-setup)
3. [AWS Account Bootstrap](#3-aws-account-bootstrap)
4. [Environment Variables Reference](#4-environment-variables-reference)
5. [Step-by-Step Deploy Order](#5-step-by-step-deploy-order)
6. [Smoke Testing](#6-smoke-testing)
7. [Updating the System](#7-updating-the-system)
8. [Rollback Procedures](#8-rollback-procedures)
9. [Teardown](#9-teardown)

---

## 1. Prerequisites

### Local tools (all must be on PATH)

| Tool | Minimum version | Install |
|---|---|---|
| AWS CLI | v2.15+ | `https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html` |
| Docker | 24+ | `https://docs.docker.com/get-docker/` |
| Python | 3.11+ | `https://www.python.org/downloads/` |
| jq | 1.6+ | `brew install jq` / `apt install jq` |

### AWS account requirements

- IAM user or role with **AdministratorAccess** for the initial deploy (you tighten this after).
- A VPC with **at least two private subnets** in different AZs. If your account has no VPC, run `scripts/create_vpc.sh` from this repo before proceeding.
- An **ECR repository** named `annam-gapfill`. Create it once:

```bash
aws ecr create-repository \
  --repository-name annam-gapfill \
  --image-scanning-configuration scanOnPush=true \
  --region "$AWS_REGION"
```

### Credentials

Configure a named profile so nothing is hardcoded:

```bash
aws configure --profile annam
export AWS_PROFILE=annam
export AWS_REGION=ap-south-1
```

---

## 2. Repository Setup

```bash
git clone <your-repo-url> iot-gap-filling
cd iot-gap-filling

# Confirm Python environment works locally before deploying
pip install -r requirements.txt
python run_pipeline.py --help
```

Copy the environment template and fill in your values (see Section 4):

```bash
cp infrastructure/env.template .env.production
# Edit .env.production — never commit this file
```

Source the file for all subsequent shell commands:

```bash
set -a; source .env.production; set +a
```

---

## 3. AWS Account Bootstrap

These steps are **one-time only** per AWS account.

### 3a. S3 Bucket

```bash
# Single bucket, prefix-separated. Replace with your chosen name.
export DATA_BUCKET=annam-gapfill-prod-$(aws sts get-caller-identity \
  --query Account --output text)

aws s3api create-bucket \
  --bucket "$DATA_BUCKET" \
  --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"

# Block all public access
aws s3api put-public-access-block \
  --bucket "$DATA_BUCKET" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,\
BlockPublicPolicy=true,RestrictPublicBuckets=true

# Versioning (protects against accidental overwrites of model artifacts)
aws s3api put-bucket-versioning \
  --bucket "$DATA_BUCKET" \
  --versioning-configuration Status=Enabled

# Server-side encryption default
aws s3api put-bucket-encryption \
  --bucket "$DATA_BUCKET" \
  --server-side-encryption-configuration '{
    "Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]
  }'

echo "Bucket ready: s3://$DATA_BUCKET"
```

### 3b. DynamoDB Tables

```bash
bash infrastructure/dynamodb/create_tables.sh
```

Expected output:
```
>> Creating annam-sensor-metadata ...
>> Creating annam-gapfill-results ...
>> Waiting for tables to become ACTIVE ...
>> Enabling TTL on annam-gapfill-results (attribute: ttl) ...
>> Done. Tables ready.
```

### 3c. IAM Roles

```bash
bash infrastructure/iam/create_roles.sh
```

This creates four roles:
- `annam-gapfill-task-role` — task permissions (S3, DynamoDB, CloudWatch)
- `annam-ecs-execution-role` — ECR pull + log write
- `annam-stepfunctions-role` — run ECS tasks, read S3, publish SNS
- `annam-scheduler-role` — start Step Functions executions

### 3d. SNS Failure Topic

```bash
export FAILURE_TOPIC_ARN=$(aws sns create-topic \
  --name annam-gapfill-failures \
  --region "$AWS_REGION" \
  --query TopicArn --output text)

# Subscribe your ops email
aws sns subscribe \
  --topic-arn "$FAILURE_TOPIC_ARN" \
  --protocol email \
  --notification-endpoint "ops@your-company.com"

echo "Confirm the subscription in your email inbox before continuing."
```

### 3e. VPC Endpoints (mandatory for private subnets)

```bash
# Edit the script to supply your VPC ID and subnet IDs first
bash infrastructure/network/create_vpc_endpoints.sh
```

This creates interface endpoints for ECR, CloudWatch Logs, and gateway endpoints for S3 and DynamoDB. Without these, tasks in private subnets cannot reach AWS APIs and will time out.

---

## 4. Environment Variables Reference

All runtime configuration is injected via environment variables. No secrets are baked into the image.

### Core (required)

| Variable | Example | Description |
|---|---|---|
| `AWS_REGION` | `ap-south-1` | AWS region for all resources |
| `DATA_BUCKET` | `annam-gapfill-prod-123456789` | S3 bucket name |
| `SENSOR_TABLE` | `annam-sensor-metadata` | DynamoDB metadata table |
| `RESULTS_TABLE` | `annam-gapfill-results` | DynamoDB results table |

### Execution mode

| Variable | Values | Default | Description |
|---|---|---|---|
| `TRAIN_MODE` | `true` / `false` | `false` | Run tournament + persist models (`true`) or load saved models (`false`) |
| `BATCH_SIZE` | integer | `25` | Sensors processed per Fargate task |
| `N_SPLITS` | integer | `5` | TimeSeriesSplit folds for tournament |
| `VAL_N_GAPS` | integer | `150` | Synthetic gaps for evaluation (lower = faster inference) |
| `VAL_SEED` | integer | `42` | Reproducibility seed |

### Infrastructure references (injected by task definition)

| Variable | Description |
|---|---|
| `RUN_ID` | Set by Step Functions execution name; identifies the run |
| `RUN_DATE` | ISO date string (`YYYY-MM-DD`) for S3 output partitioning |
| `WORK_DIR` | Local scratch dir inside container (default `/tmp/iot`) |
| `MPLBACKEND` | Must be `Agg` (no display in container) |

### S3 prefix layout (optional overrides)

| Variable | Default | Description |
|---|---|---|
| `RAW_PREFIX` | `raw` | Where raw sensor CSVs live |
| `MODELS_PREFIX` | `models` | Where trained model bundles are stored |
| `FILLED_PREFIX` | `filled` | Reconstructed output CSVs |
| `REPORTS_PREFIX` | `reports` | Audit, evaluation, MAE charts |

### Observability

| Variable | Default | Description |
|---|---|---|
| `CW_NAMESPACE` | `AnnamAI/GapFilling` | CloudWatch metrics namespace |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `METRICS_MODE` | `emf` | `emf` (free, via logs) or `api` (PutMetricData) |

### Cost tuning (Fargate Spot rate display only)

| Variable | Default | Description |
|---|---|---|
| `FARGATE_SPOT_VCPU_HOUR` | `0.01244` | USD/vCPU-hour for cost metric display |
| `FARGATE_SPOT_GB_HOUR` | `0.001365` | USD/GB-hour for cost metric display |

---

## 5. Step-by-Step Deploy Order

Follow this order strictly. Later steps depend on earlier ones.

```
[1] S3 bucket              (Section 3a)
[2] DynamoDB tables        (Section 3b)
[3] IAM roles              (Section 3c)
[4] SNS topic              (Section 3d)
[5] VPC endpoints          (Section 3e)
[6] Build + push image     (below)
[7] ECS cluster + taskdefs (below)
[8] Step Functions SM      (below)
[9] EventBridge schedules  (below)
[10] Seed sensor data      (below)
[11] Upload raw CSVs       (below)
[12] CloudWatch dashboard  (below)
[13] Smoke test            (Section 6)
```

### Step 6 — Build and push the Docker image

```bash
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export IMAGE_URI=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/annam-gapfill

# Authenticate Docker to ECR
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin \
  "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

# Build from repo root (Dockerfile uses multi-stage; context = .)
docker build -f aws/Dockerfile -t annam-gapfill:latest .

# Tag and push
docker tag annam-gapfill:latest "$IMAGE_URI:latest"
docker tag annam-gapfill:latest "$IMAGE_URI:$(git rev-parse --short HEAD)"
docker push "$IMAGE_URI:latest"
docker push "$IMAGE_URI:$(git rev-parse --short HEAD)"

echo "Image pushed: $IMAGE_URI:latest"
```

### Step 7 — ECS cluster and task definitions

```bash
export ECS_CLUSTER=annam-gapfill
export IMAGE_TAG=latest

bash infrastructure/ecs/deploy_cluster.sh
```

Verify:
```bash
aws ecs describe-clusters --clusters "$ECS_CLUSTER" \
  --query 'clusters[0].status' --output text
# Expected: ACTIVE
```

### Step 8 — Step Functions state machine

Export the ARNs gathered in previous steps:

```bash
export ECS_CLUSTER_ARN=$(aws ecs describe-clusters \
  --clusters "$ECS_CLUSTER" \
  --query 'clusters[0].clusterArn' --output text)

export RUN_BATCH_TASKDEF_ARN=$(aws ecs describe-task-definition \
  --task-definition annam-run-batch \
  --query 'taskDefinition.taskDefinitionArn' --output text)

export LIST_SENSORS_TASKDEF_ARN=$(aws ecs describe-task-definition \
  --task-definition annam-list-sensors \
  --query 'taskDefinition.taskDefinitionArn' --output text)

export SFN_ROLE_ARN=$(aws iam get-role \
  --role-name annam-stepfunctions-role \
  --query 'Role.Arn' --output text)

# Supply your private subnet IDs and security group ID
export PRIVATE_SUBNET_1=subnet-xxxxxxxxxxxxxxxxx
export PRIVATE_SUBNET_2=subnet-yyyyyyyyyyyyyyyyy
export TASK_SG=sg-zzzzzzzzzzzzzzzzz

bash infrastructure/stepfunctions/deploy_statemachine.sh
```

Capture the state machine ARN for the next step:
```bash
export SFN_ARN=$(aws stepfunctions list-state-machines \
  --query "stateMachines[?name=='annam-gapfill'].stateMachineArn" \
  --output text)
echo "SFN ARN: $SFN_ARN"
```

### Step 9 — EventBridge schedules

```bash
export SCHEDULER_ROLE_ARN=$(aws iam get-role \
  --role-name annam-scheduler-role \
  --query 'Role.Arn' --output text)

bash infrastructure/eventbridge/deploy_schedules.sh
```

### Step 10 — Seed sensor metadata

Prepare a CSV with your device coordinates:

```
device_id,latitude,longitude
216,31.274033,74.849239
255,31.400000,74.950000
201,31.105000,74.700000
```

```bash
python infrastructure/dynamodb/seed_sensors.py \
  --from-coords sensors.csv \
  --k 2 \
  --max-km 30 \
  --region "$AWS_REGION"
```

Verify one record:
```bash
aws dynamodb get-item \
  --table-name annam-sensor-metadata \
  --key '{"PK":{"S":"SENSOR#216"},"SK":{"S":"META"}}' \
  --region "$AWS_REGION"
```

### Step 11 — Upload raw sensor CSVs

```bash
# Each device gets its own prefix: raw/<device_id>/<device_id>.csv
for device_id in 216 255 201; do
  aws s3 cp "data/Annam_${device_id}.csv" \
    "s3://$DATA_BUCKET/raw/$device_id/$device_id.csv"
done
```

### Step 12 — Deploy CloudWatch dashboard

```bash
bash infrastructure/cloudwatch/deploy_dashboard.sh
```

---

## 6. Smoke Testing

### Manual single-sensor run (fastest validation)

```bash
# Run one sensor in training mode via Docker locally (uses real AWS)
docker run --rm \
  -e AWS_REGION="$AWS_REGION" \
  -e DATA_BUCKET="$DATA_BUCKET" \
  -e SENSOR_TABLE=annam-sensor-metadata \
  -e RESULTS_TABLE=annam-gapfill-results \
  -e TRAIN_MODE=true \
  -e N_SPLITS=3 \
  -e VAL_N_GAPS=20 \
  -e RUN_ID=smoke-test-001 \
  -e RUN_DATE=$(date +%Y-%m-%d) \
  -e METRICS_MODE=emf \
  -v ~/.aws:/home/appuser/.aws:ro \
  annam-gapfill:latest \
  python aws/run_batch.py --device-ids 216
```

Expected exit code: `0`  
Expected stdout last line: `{"succeeded": 1, "failed": 0, ...}`

### Verify outputs in S3

```bash
aws s3 ls "s3://$DATA_BUCKET/filled/$(date +%Y-%m-%d)/216/" --recursive
# Should show filled_dataset.csv, audit_report.csv, evaluation_report.csv, etc.
```

### Trigger a full Step Functions execution manually

```bash
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --name "manual-smoke-$(date +%s)" \
  --input '{"train_mode": true}' \
  --region "$AWS_REGION"
```

Monitor in the AWS Console → Step Functions → annam-gapfill → Executions.

### Verify CloudWatch metrics appear

```bash
aws cloudwatch list-metrics \
  --namespace AnnamAI/GapFilling \
  --region "$AWS_REGION" \
  --query 'Metrics[*].MetricName'
# Expected: SensorsProcessed, SensorsSucceeded, SensorsFailed, BatchDurationSeconds, etc.
```

---

## 7. Updating the System

### Image-only update (most common — bug fixes, dependency bumps)

```bash
docker build -f aws/Dockerfile -t annam-gapfill:latest .
docker tag annam-gapfill:latest "$IMAGE_URI:latest"
docker push "$IMAGE_URI:latest"
# No infrastructure changes needed; next scheduled run picks up the new image.
```

### Config-only update (schedule, batch size, etc.)

Edit the relevant environment variable in the task definition JSON and re-register:

```bash
# Edit infrastructure/ecs/taskdef-run-batch.json, then:
bash infrastructure/ecs/deploy_cluster.sh
# The new task def revision is used by the next Step Functions execution.
```

### Sensor topology update (add/remove sensors)

```bash
# Add a new sensor
python infrastructure/dynamodb/seed_sensors.py \
  --item '{"device_id":"300","latitude":31.5,"longitude":75.1,"status":"ACTIVE"}'

# Deactivate a sensor (stops it being batched, keeps history)
aws dynamodb update-item \
  --table-name annam-sensor-metadata \
  --key '{"PK":{"S":"SENSOR#300"},"SK":{"S":"META"}}' \
  --update-expression "SET #s = :inactive" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":inactive":{"S":"INACTIVE"}}'
```

### Model refresh (force retraining)

```bash
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --name "manual-retrain-$(date +%s)" \
  --input '{"train_mode": true}'
```

---

## 8. Rollback Procedures

### Image rollback

```bash
# List available tags
aws ecr list-images --repository-name annam-gapfill \
  --query 'imageIds[*].imageTag' --output table

# Re-tag a previous git SHA as latest
PREVIOUS_SHA=abc1234
docker pull "$IMAGE_URI:$PREVIOUS_SHA"
docker tag "$IMAGE_URI:$PREVIOUS_SHA" "$IMAGE_URI:latest"
docker push "$IMAGE_URI:latest"
```

### Task definition rollback

```bash
# List all revisions
aws ecs list-task-definitions --family-prefix annam-run-batch --output table

# Force the state machine to use a specific revision by updating the ARN
# in infrastructure/stepfunctions/deploy_statemachine.sh, then re-deploy.
```

### Model rollback (S3 versioning)

```bash
# List previous versions of a model bundle
aws s3api list-object-versions \
  --bucket "$DATA_BUCKET" \
  --prefix "models/216/model_selection.joblib" \
  --query 'Versions[*].{VersionId:VersionId,LastModified:LastModified}'

# Restore a specific version
aws s3api copy-object \
  --bucket "$DATA_BUCKET" \
  --copy-source "$DATA_BUCKET/models/216/model_selection.joblib?versionId=<VERSION_ID>" \
  --key "models/216/model_selection.joblib"
```

---

## 9. Teardown

**Warning: this is destructive.** Only run in non-production accounts or when decommissioning.

```bash
# 1. Disable schedules (stops future triggers immediately)
aws scheduler delete-schedule --name annam-gapfill-daily-training
aws scheduler delete-schedule --name annam-gapfill-hourly-inference

# 2. Stop any running executions
aws stepfunctions list-executions \
  --state-machine-arn "$SFN_ARN" \
  --status-filter RUNNING \
  --query 'executions[*].executionArn' --output text | \
  xargs -I {} aws stepfunctions stop-execution --execution-arn {}

# 3. Delete state machine
aws stepfunctions delete-state-machine --state-machine-arn "$SFN_ARN"

# 4. Delete ECS cluster (tasks must all be stopped first)
aws ecs delete-cluster --cluster "$ECS_CLUSTER"

# 5. Delete DynamoDB tables (data is gone)
aws dynamodb delete-table --table-name annam-sensor-metadata
aws dynamodb delete-table --table-name annam-gapfill-results

# 6. Empty and delete S3 bucket
aws s3 rm "s3://$DATA_BUCKET" --recursive
aws s3api delete-bucket --bucket "$DATA_BUCKET"

# 7. Delete ECR images + repo
aws ecr batch-delete-image \
  --repository-name annam-gapfill \
  --image-ids imageTag=latest
aws ecr delete-repository --repository-name annam-gapfill --force
```
