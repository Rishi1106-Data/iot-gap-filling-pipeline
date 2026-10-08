> **Status: earlier design (not the current production path).** This document describes the first AWS design — S3 CSV inputs/outputs, DynamoDB sensor-metadata/results tables and a Step Functions fan-out wrapped around `src/`. The current production implementation lives in [`aws/production/`](../aws/production/DEPLOY.md) (DynamoDB as the source *and* destination, one scheduled ECS Fargate task, no Step Functions/Lambda). This file is kept for reference.

# Production Deployment Runbook
## Annam AI — IoT Gap-Filling Pipeline

**Document type:** Operational runbook  
**Maintained by:** Platform / ML Infrastructure team  
**Review cadence:** Quarterly, or after any major infrastructure change

---

## Runbook Index

- [RB-01: Scheduled daily training run](#rb-01-scheduled-daily-training-run)
- [RB-02: Manual ad-hoc execution](#rb-02-manual-ad-hoc-execution)
- [RB-03: New image deployment](#rb-03-new-image-deployment)
- [RB-04: Adding a new sensor](#rb-04-adding-a-new-sensor)
- [RB-05: Sensor failed — investigation](#rb-05-sensor-failed--investigation)
- [RB-06: All sensors failed in a batch](#rb-06-all-sensors-failed-in-a-batch)
- [RB-07: Step Functions execution stuck or failed](#rb-07-step-functions-execution-stuck-or-failed)
- [RB-08: Model drift detected](#rb-08-model-drift-detected)
- [RB-09: S3 storage growing unexpectedly](#rb-09-s3-storage-growing-unexpectedly)
- [RB-10: DynamoDB cost spike](#rb-10-dynamodb-cost-spike)
- [RB-11: Fargate Spot interruptions causing failures](#rb-11-fargate-spot-interruptions-causing-failures)
- [RB-12: Emergency pipeline disable](#rb-12-emergency-pipeline-disable)

---

## RB-01: Scheduled Daily Training Run

**When:** Every day at 02:30 UTC (± 30-minute jitter from EventBridge Scheduler)  
**What happens:** EventBridge triggers Step Functions with `train_mode=true`. Full pipeline runs: sensor discovery → batch creation → Fargate Spot fan-out → model retrain → S3 upload → DynamoDB audit write.

### Verify it ran

```bash
# Check last execution status
aws stepfunctions list-executions \
  --state-machine-arn "$SFN_ARN" \
  --max-results 5 \
  --region "$AWS_REGION" \
  --query 'executions[*].{Name:name,Status:status,Start:startDate}' \
  --output table
```

### Verify outputs in S3

```bash
TODAY=$(date +%Y-%m-%d)
aws s3 ls "s3://$DATA_BUCKET/filled/$TODAY/" --recursive | head -20
```

### Check failure count

```bash
aws cloudwatch get-metric-statistics \
  --namespace AnnamAI/GapFilling \
  --metric-name SensorsFailed \
  --start-time "$(date -u -v-2H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u --date '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 7200 \
  --statistics Sum \
  --region "$AWS_REGION"
```

**Expected:** `SensorsFailed = 0` for a clean run. Up to 10% failures within `ToleratedFailurePercentage` are acceptable.

---

## RB-02: Manual Ad-Hoc Execution

**Use case:** Re-process after a data correction, force model refresh, or test changes.

```bash
# Training run (re-trains and re-saves models)
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --name "manual-train-$(date +%Y%m%d-%H%M%S)" \
  --input '{"train_mode": true}' \
  --region "$AWS_REGION"

# Inference run (uses existing saved models)
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --name "manual-infer-$(date +%Y%m%d-%H%M%S)" \
  --input '{"train_mode": false}' \
  --region "$AWS_REGION"

# Single sensor (Docker, bypasses Step Functions — good for quick debugging)
docker run --rm \
  -e AWS_REGION="$AWS_REGION" \
  -e DATA_BUCKET="$DATA_BUCKET" \
  -e SENSOR_TABLE=annam-sensor-metadata \
  -e RESULTS_TABLE=annam-gapfill-results \
  -e TRAIN_MODE=true \
  -e N_SPLITS=3 \
  -e VAL_N_GAPS=20 \
  -e RUN_ID=debug-$(date +%s) \
  -e RUN_DATE=$(date +%Y-%m-%d) \
  -v ~/.aws:/home/appuser/.aws:ro \
  "$IMAGE_URI:latest" \
  python aws/run_batch.py --device-ids 216
```

---

## RB-03: New Image Deployment

**Use case:** Bug fix, dependency update, or pipeline logic change.

**Pre-deploy checklist:**
- [ ] `pytest tests/` passes locally (all modules)
- [ ] Docker build succeeds locally: `docker build -f aws/Dockerfile -t annam-gapfill:local .`
- [ ] Single-sensor smoke test passes (RB-02 Docker command above)
- [ ] Change is on `main` branch and PR is merged

```bash
# 1. Build and tag with git SHA for traceability
GIT_SHA=$(git rev-parse --short HEAD)
docker build -f aws/Dockerfile -t annam-gapfill:$GIT_SHA .

# 2. Push
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin \
  "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

docker tag annam-gapfill:$GIT_SHA "$IMAGE_URI:$GIT_SHA"
docker tag annam-gapfill:$GIT_SHA "$IMAGE_URI:latest"
docker push "$IMAGE_URI:$GIT_SHA"
docker push "$IMAGE_URI:latest"

# 3. No task def update needed — 'latest' tag is already what the task def uses.
#    The next Step Functions execution will pull the new image.

echo "Deployed: $GIT_SHA. Next scheduled run will use the new image."
```

**Rollback:** See DEPLOYMENT_GUIDE.md §8.

---

## RB-04: Adding a New Sensor

**Use case:** A new physical IoT device is installed and needs to be included in gap-filling.

```bash
# 1. Upload the raw CSV to S3
NEW_ID=300
aws s3 cp "data/Annam_${NEW_ID}.csv" \
  "s3://$DATA_BUCKET/raw/$NEW_ID/$NEW_ID.csv"

# 2. Add its neighbour sensors' CSVs if not already present
# (assuming neighbours 237 and 249 already exist)

# 3. Seed metadata. Compute lat/long from the device spec sheet.
python infrastructure/dynamodb/seed_sensors.py \
  --item '{
    "device_id": "300",
    "latitude": 31.5,
    "longitude": 75.1,
    "status": "ACTIVE",
    "ref_col": "CorrectedTemp"
  }' \
  --region "$AWS_REGION"

# 4. Add neighbour topology. Either re-run from-coords with the full fleet:
python infrastructure/dynamodb/seed_sensors.py \
  --from-coords all_sensors.csv \
  --k 2 --max-km 30

# OR manually add neighbours to the existing item:
aws dynamodb update-item \
  --table-name annam-sensor-metadata \
  --key '{"PK":{"S":"SENSOR#300"},"SK":{"S":"META"}}' \
  --update-expression "SET neighbors = :nbrs" \
  --expression-attribute-values '{":nbrs":{"L":[
    {"M":{"id":{"S":"237"},"distance_km":{"N":"16.75"}}},
    {"M":{"id":{"S":"249"},"distance_km":{"N":"17.5"}}}
  ]}}'

# 5. Trigger a training run to build models for the new sensor
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --name "new-sensor-train-$(date +%s)" \
  --input '{"train_mode": true}'

echo "Sensor $NEW_ID is ACTIVE and will be processed in the next run."
```

---

## RB-05: Sensor Failed — Investigation

**Trigger:** SNS alert `SensorsFailed > 50`, or you noticed a sensor missing from outputs.

```bash
# 1. Find the failing sensor in CloudWatch Logs Insights
aws logs start-query \
  --log-group-name "/ecs/annam-run-batch" \
  --start-time $(date -u -v-24H +%s 2>/dev/null || date -u --date '24 hours ago' +%s) \
  --end-time $(date -u +%s) \
  --query-string "fields ts, device_id, error | filter level = 'ERROR' and msg = 'Sensor failed' | sort ts desc | limit 50" \
  --region "$AWS_REGION"

# Wait for query to complete, then retrieve results
QUERY_ID=<paste query ID from above>
aws logs get-query-results --query-id "$QUERY_ID" --region "$AWS_REGION"
```

**Common failure causes and fixes:**

| Error message | Root cause | Fix |
|---|---|---|
| `No neighbours configured for sensor <id>` | DynamoDB `neighbors` attribute missing | Run `seed_sensors.py` for that sensor |
| `Inference mode but no saved model` | First-ever run or model was deleted | Trigger a training run: `train_mode=true` |
| `NoSuchKey` on S3 download | Raw CSV not uploaded | `aws s3 cp data/<id>.csv s3://$DATA_BUCKET/raw/<id>/<id>.csv` |
| `ClientError: ProvisionedThroughputExceededException` | DynamoDB throttling | Tables are on-demand — this shouldn't happen; check for runaway loops |
| `ValueError: neighbor config mismatch` | Neighbour count in DDB differs from what pipeline built | Re-seed sensor metadata |
| Out of disk | Batch size too large, ephemeral storage filling up | Reduce `BATCH_SIZE` in task definition |

---

## RB-06: All Sensors Failed in a Batch

**Trigger:** `run_batch` raised `RuntimeError: All N sensors in batch failed`.  
This causes Step Functions to retry the batch up to 3 times (30s, 60s, 120s backoff).

```bash
# 1. Identify which batch (batch_id) failed from the execution history
EXEC_ARN=$(aws stepfunctions list-executions \
  --state-machine-arn "$SFN_ARN" \
  --status-filter FAILED \
  --max-results 1 \
  --query 'executions[0].executionArn' --output text)

aws stepfunctions get-execution-history \
  --execution-arn "$EXEC_ARN" \
  --query 'events[?type==`TaskFailed`]' \
  --output json | jq '.[].taskFailedEventDetails'

# 2. Manually re-run the specific device IDs from the failed batch
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --name "retry-batch-$(date +%s)" \
  --input '{"train_mode": false}'
# Or target specific devices via Docker (RB-02)
```

---

## RB-07: Step Functions Execution Stuck or Failed

**Trigger:** SNS alert `ExecutionsFailed >= 1`, or execution shows `RUNNING` for > 3 hours.

```bash
# List active executions
aws stepfunctions list-executions \
  --state-machine-arn "$SFN_ARN" \
  --status-filter RUNNING \
  --region "$AWS_REGION"

# Stop a stuck execution
aws stepfunctions stop-execution \
  --execution-arn "<stuck-execution-arn>" \
  --cause "Manual stop via runbook RB-07" \
  --region "$AWS_REGION"

# Start a fresh execution
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --name "recovery-$(date +%s)" \
  --input '{"train_mode": false}'
```

**Check if DiscoverAndBatch step failed (can't find sensors):**

```bash
aws logs filter-log-events \
  --log-group-name "/ecs/annam-list-sensors" \
  --filter-pattern "ERROR" \
  --start-time $(($(date +%s) - 3600))000 \
  --region "$AWS_REGION"
```

---

## RB-08: Model Drift Detected

**Trigger:** `evaluation_report.csv` shows MAE increasing over time, or data science team flags degrading reconstruction quality.

```bash
# 1. Check recent evaluation reports for a specific sensor
aws s3 ls "s3://$DATA_BUCKET/reports/" --recursive | \
  grep "216/evaluation_report.csv" | sort | tail -7

# 2. Download and inspect the last week of eval reports
for date in $(seq 0 6 | xargs -I{} date -v-{}d +%Y-%m-%d 2>/dev/null || \
             seq 0 6 | xargs -I{} date --date "{} days ago" +%Y-%m-%d); do
  aws s3 cp "s3://$DATA_BUCKET/reports/$date/216/evaluation_report.csv" \
    "/tmp/eval_216_$date.csv" 2>/dev/null || true
done

# 3. Force model retraining with full tournament
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --name "retrain-drift-$(date +%s)" \
  --input '{"train_mode": true}'

# 4. If drift persists, lower N_SPLITS or raise n_estimators in task def env vars,
#    then redeploy task definition (RB-03 image deploy pattern).
```

---

## RB-09: S3 Storage Growing Unexpectedly

**Trigger:** AWS Cost Explorer shows S3 costs increasing, or you notice unexpected bucket growth.

```bash
# Check total bucket size by prefix
aws s3api list-objects-v2 \
  --bucket "$DATA_BUCKET" \
  --query 'sum(Contents[].Size)' \
  --output text

# Size by prefix
for prefix in raw models filled reports runs; do
  SIZE=$(aws s3 ls "s3://$DATA_BUCKET/$prefix/" --recursive --summarize | \
         grep "Total Size" | awk '{print $3}')
  echo "$prefix: $SIZE bytes"
done
```

**Common causes and fixes:**

| Cause | Fix |
|---|---|
| `filled/` and `reports/` accumulating per-day partitions | Add S3 Lifecycle rule: delete `filled/*` and `reports/*` older than 90 days |
| `runs/` batches.json files accumulating | Add S3 Lifecycle rule: delete `runs/*` older than 7 days |
| `models/` growing due to many sensor versions | Models are overwritten per sensor — if unexpectedly large, check for errant prefixes |

```bash
# Add lifecycle rule for filled + reports (90-day retention)
aws s3api put-bucket-lifecycle-configuration \
  --bucket "$DATA_BUCKET" \
  --lifecycle-configuration '{
    "Rules": [
      {"ID":"expire-filled","Status":"Enabled",
       "Filter":{"Prefix":"filled/"},
       "Expiration":{"Days":90}},
      {"ID":"expire-reports","Status":"Enabled",
       "Filter":{"Prefix":"reports/"},
       "Expiration":{"Days":90}},
      {"ID":"expire-runs","Status":"Enabled",
       "Filter":{"Prefix":"runs/"},
       "Expiration":{"Days":7}}
    ]
  }'
```

---

## RB-10: DynamoDB Cost Spike

**Trigger:** AWS Cost Explorer shows DynamoDB costs higher than expected.

Both tables use on-demand (PAY_PER_REQUEST) billing. Unexpected costs mean unexpected traffic.

```bash
# Check consumed read/write capacity units in CloudWatch
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedWriteCapacityUnits \
  --dimensions Name=TableName,Value=annam-gapfill-results \
  --start-time "$(date -u --date '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 \
  --statistics Sum
```

**If `annam-gapfill-results` is the culprit:**
- Check TTL is enabled (items should auto-expire after 180 days)
- Check if a runaway Step Functions execution is writing duplicates

**If `annam-sensor-metadata` is the culprit:**
- Should be read-only in normal operation; only writes happen during seeding
- Check for unexpected scan operations (there should be none — all access is GetItem or GSI Query)

---

## RB-11: Fargate Spot Interruptions Causing Failures

**Trigger:** Batch tasks failing with `States.TaskFailed` followed by retry, more than 3 times in a row.

Spot interruptions are expected; the system is designed to retry. Sustained failures suggest Spot capacity shortage in your region.

```bash
# Check Spot capacity failure rates in CloudWatch
aws cloudwatch get-metric-statistics \
  --namespace AWS/ECS \
  --metric-name CPUReservation \
  --dimensions Name=ClusterName,Value=annam-gapfill \
  --start-time "$(date -u --date '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 \
  --statistics Average
```

**Mitigations (in order of preference):**

1. **Wait** — Spot capacity varies; the daily job has a wide window.
2. **Add a second region's AZ** — if your VPC spans 3 AZs, add `PRIVATE_SUBNET_3` to the task network config.
3. **Increase on-demand fallback weight** — in `stepfunctions_definition.json`, change `FARGATE_SPOT weight=4, FARGATE weight=1` to `FARGATE_SPOT weight=2, FARGATE weight=1`. This costs ~30% more but reduces interruptions.
4. **Reduce MaxConcurrency** — fewer concurrent tasks = less Spot demand = fewer interruptions.

---

## RB-12: Emergency Pipeline Disable

**Use case:** Data quality incident, runaway cost, or accidental misconfiguration.

```bash
# ── FAST STOP (< 30 seconds) ────────────────────────────────────────────────

# 1. Disable both EventBridge schedules (stops future triggers immediately)
aws scheduler update-schedule \
  --name annam-gapfill-daily-training \
  --state DISABLED \
  --flexible-time-window Mode=OFF \
  --schedule-expression "rate(1 hour)" \
  --target Arn="$SFN_ARN",RoleArn="$SCHEDULER_ROLE_ARN"

aws scheduler update-schedule \
  --name annam-gapfill-hourly-inference \
  --state DISABLED \
  --flexible-time-window Mode=OFF \
  --schedule-expression "rate(1 hour)" \
  --target Arn="$SFN_ARN",RoleArn="$SCHEDULER_ROLE_ARN"

# 2. Stop any currently-running Step Functions execution
aws stepfunctions list-executions \
  --state-machine-arn "$SFN_ARN" \
  --status-filter RUNNING \
  --query 'executions[*].executionArn' --output text | \
  tr '\t' '\n' | \
  xargs -I {} aws stepfunctions stop-execution \
    --execution-arn {} \
    --cause "Emergency stop — runbook RB-12"

echo "Pipeline disabled. Re-enable with:"
echo "  aws scheduler update-schedule --name annam-gapfill-daily-training --state ENABLED ..."
```

**To re-enable after incident resolution:**

```bash
bash infrastructure/eventbridge/deploy_schedules.sh
```
