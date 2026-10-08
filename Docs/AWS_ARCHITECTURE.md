> **Status: earlier design (not the current production path).** This document describes the first AWS design — S3 CSV inputs/outputs, DynamoDB sensor-metadata/results tables and a Step Functions fan-out wrapped around `src/`. The current production implementation lives in [`aws/production/`](../aws/production/DEPLOY.md) (DynamoDB as the source *and* destination, one scheduled ECS Fargate task, no Step Functions/Lambda). This file is kept for reference.

# AWS Architecture — Annam AI IoT Gap-Filling Pipeline

**Version:** 1.0  
**Region:** `ap-south-1` (Mumbai)  
**Last updated:** 2026-06

---

## 1. Architecture Overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│                          AWS ap-south-1                                     │
│                                                                              │
│  ┌──────────────────┐                                                        │
│  │ EventBridge      │                                                        │
│  │ Scheduler        │                                                        │
│  │                  │                                                        │
│  │ daily  02:30 UTC │──train_mode=true──┐                                   │
│  │ hourly (opt)     │──train_mode=false─┤                                   │
│  └──────────────────┘                   │                                   │
│                                          ▼                                   │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                  Step Functions (STANDARD)                             │  │
│  │                                                                        │  │
│  │  ┌─────────────┐   ┌─────────────┐   ┌──────────────────────────┐    │  │
│  │  │Discover     │   │ LoadBatches │   │ ProcessBatches (Map)      │    │  │
│  │  │AndBatch     │──▶│ S3 GetObject│──▶│ MaxConcurrency=20         │    │  │
│  │  │             │   │             │   │ DISTRIBUTED mode           │    │  │
│  │  │ list-sensors│   │             │   │                            │    │  │
│  │  │ Fargate task│   │             │   │  ┌───────────────────┐    │    │  │
│  │  └─────────────┘   └─────────────┘   │  │ RunBatch          │    │    │  │
│  │         │                             │  │ Fargate SPOT task │    │    │  │
│  │         │ writes batches.json         │  │ (1vCPU / 2GB)    │    │    │  │
│  │         ▼                             │  │                   │    │    │  │
│  │  ┌─────────────┐                     │  │ N sensors/task    │    │    │  │
│  │  │   S3        │                     │  │ sequential        │    │    │  │
│  │  │ runs/<exec> │◀────────────────────│  │                   │    │    │  │
│  │  │ /batches    │                     │  └───────────────────┘    │    │  │
│  │  └─────────────┘                     └──────────────────────────┘    │  │
│  │                                                                        │  │
│  │  On failure: ──▶ SNS Notify ──▶ FailState                            │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  Fargate Task: run_batch.py (per batch)                              │    │
│  │                                                                       │    │
│  │  For each sensor in batch:                                            │    │
│  │    ┌──────────────┐  ┌────────────────┐  ┌──────────────────────┐   │    │
│  │    │ DynamoDB     │  │ S3 download    │  │ run_pipeline(config) │   │    │
│  │    │ GetItem      │─▶│ target + nbrs  │─▶│  gap_analysis        │   │    │
│  │    │ (metadata +  │  │ (nbr cached)   │  │  interpolation       │   │    │
│  │    │  neighbours) │  │                │  │  model_selection     │   │    │
│  │    └──────────────┘  └────────────────┘  │  neighbor_recon      │   │    │
│  │                                           │  validation          │   │    │
│  │                                           └──────────┬───────────┘   │    │
│  │                                                      │               │    │
│  │    ┌──────────────────────────────────────────────── │ ──────────┐  │    │
│  │    │ Outputs                                         ▼           │  │    │
│  │    │  S3: filled/<date>/<id>/filled_dataset.csv                  │  │    │
│  │    │  S3: reports/<date>/<id>/{audit,eval,nbr,model,mae}.csv/png │  │    │
│  │    │  S3: models/<id>/model_selection.joblib  (train only)       │  │    │
│  │    │  DynamoDB: annam-gapfill-results (BatchWriteItem)           │  │    │
│  │    │  CloudWatch: EMF metrics via stdout                         │  │    │
│  │    └─────────────────────────────────────────────────────────────┘  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────────────────┐  │
│  │  S3 Bucket   │  │  DynamoDB        │  │  CloudWatch                  │  │
│  │              │  │                  │  │                               │  │
│  │  raw/        │  │  annam-sensor-   │  │  Namespace:                   │  │
│  │  models/     │  │  metadata        │  │  AnnamAI/GapFilling           │  │
│  │  filled/     │  │  (+ gsi_status)  │  │                               │  │
│  │  reports/    │  │                  │  │  Metrics: SensorsProcessed,   │  │
│  │  runs/       │  │  annam-gapfill-  │  │  SensorsSucceeded,            │  │
│  │              │  │  results (TTL)   │  │  SensorsFailed,               │  │
│  │  SSE-AES256  │  │                  │  │  BatchDurationSeconds,        │  │
│  │  Versioned   │  │  On-demand       │  │  TaskCostUSD,                 │  │
│  │              │  │  billing         │  │  Rows_model/neighbor/interp   │  │
│  └──────────────┘  └──────────────────┘  └──────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Service Inventory

| Service | Resource name | Purpose | Billing model |
|---|---|---|---|
| S3 | `annam-gapfill-prod-<acct>` | Raw CSVs, models, outputs | Pay per GB stored + requests |
| DynamoDB | `annam-sensor-metadata` | Sensor topology + config | On-demand (PAY_PER_REQUEST) |
| DynamoDB | `annam-gapfill-results` | Per-run audit + TTL 180d | On-demand (PAY_PER_REQUEST) |
| ECR | `annam-gapfill` | Docker image registry | Pay per GB stored |
| ECS | `annam-gapfill` cluster | Hosts Fargate tasks | No charge for cluster itself |
| Fargate | `annam-run-batch` task def | Gap-filling workload | Per vCPU-sec + GB-sec (Spot) |
| Fargate | `annam-list-sensors` task def | Sensor discovery | Per vCPU-sec (Spot) |
| Step Functions | `annam-gapfill` (STANDARD) | Orchestration + fan-out | Per state transition |
| EventBridge Scheduler | 2 schedules | Timed triggers | Per invocation |
| CloudWatch Logs | `/ecs/annam-run-batch`, `/ecs/annam-list-sensors` | Structured logs | Pay per GB ingested |
| CloudWatch Metrics | `AnnamAI/GapFilling` namespace | EMF (free extraction) | Free via log metric filter |
| CloudWatch Dashboard | `AnnamAI-GapFilling` | Operational visibility | Per dashboard |
| SNS | `annam-gapfill-failures` | Hard-failure alerts | Per notification |

---

## 3. Network Architecture

```
VPC (your existing VPC)
│
├── Private Subnet AZ-a (subnet-xxxxxxxxx)
│   └── Fargate Tasks (no public IP)
│       └── Security Group: annam-task-sg
│           Egress: 443 to VPC endpoints only
│
├── Private Subnet AZ-b (subnet-yyyyyyyyy)
│   └── Fargate Tasks (failover + Spot diversity)
│
└── VPC Endpoints (mandatory — no NAT gateway)
    ├── S3 Gateway Endpoint          (free)
    ├── DynamoDB Gateway Endpoint    (free)
    ├── ECR API Interface Endpoint   (paid)
    ├── ECR DKR Interface Endpoint   (paid)
    ├── CloudWatch Logs Interface    (paid)
    └── Step Functions Interface     (paid, needed for .sync callbacks)
```

### Why no NAT gateway?

At 10,000 sensors × daily run, NAT data-processing charges would be the single largest line item (~$180/month for ~180 GB of S3 traffic). VPC gateway endpoints for S3 and DynamoDB are free and eliminate this entirely. Interface endpoints for ECR and CloudWatch cost ~$15/month but avoid NAT costs that would otherwise be ~10× higher.

### Security group rules

```
annam-task-sg (tasks):
  Ingress: None (tasks never receive inbound connections)
  Egress:  TCP 443 → pl-xxxxxxxx (S3 prefix list, gateway EP)
           TCP 443 → pl-yyyyyyyy (DynamoDB prefix list, gateway EP)
           TCP 443 → sg-zzz (ECR VPC endpoint ENI)
           TCP 443 → sg-aaa (CW Logs VPC endpoint ENI)
```

---

## 4. Data Flow

### 4a. Training run (daily, `TRAIN_MODE=true`)

```
1. EventBridge fires at 02:30 UTC
2. Step Functions starts execution
3. DiscoverAndBatch task:
   a. Query DynamoDB gsi_status = ACTIVE  →  list of sensor metadata
   b. Cluster by shared neighbours, chunk into batches of 25
   c. Write batches.json to s3://bucket/runs/<exec_name>/batches.json
4. LoadBatches: Step Functions reads batches.json via S3 SDK integration
5. ProcessBatches Map (MaxConcurrency=20):
   For each batch:
   a. Fargate Spot task starts (1 vCPU / 2 GB / 30 GB ephemeral)
   b. For each of 25 sensors:
      - GetItem: sensor metadata + neighbour list (1 read)
      - S3 download: target CSV  →  /tmp/iot/sensors/<id>/input/
      - S3 download: neighbour CSVs  →  /tmp/iot/cache/neighbors/ (cached)
      - run_pipeline(config)  →  filled_df, eval_df, summary
      - S3 upload: filled_dataset.csv, reports/, models/  (train = save models)
      - Per-sensor dir deleted  →  disk freed for next sensor
   c. BatchWriteItem all 25 results to DynamoDB
   d. Emit EMF metrics to CloudWatch via stdout
6. Summarize: execution completes
```

### 4b. Inference run (hourly optional, `TRAIN_MODE=false`)

Same as training except:
- Models are loaded from S3 at step 5b (not retrained)
- Model bundle is NOT re-uploaded
- Note: evaluation still re-fits internally (see PRODUCTION_HARDENING.md §0)

---

## 5. IAM Permission Model

Principle of least privilege. Four roles, each scoped to exactly what it needs.

### `annam-gapfill-task-role` (used by Fargate tasks)

```
S3:
  GetObject, PutObject on raw/*, models/*, filled/*, reports/*, runs/*
  ListBucket with prefix conditions for the above

DynamoDB:
  GetItem, Query on annam-sensor-metadata (+ gsi_status index)
  PutItem, BatchWriteItem on annam-gapfill-results

CloudWatch:
  PutMetricData on namespace AnnamAI/GapFilling (api mode only;
  EMF mode uses log streams — no CW permission needed)
```

### `annam-ecs-execution-role` (used by ECS control plane)

```
ECR:  GetAuthorizationToken, BatchCheckLayerAvailability,
      GetDownloadUrlForLayer, BatchGetImage
Logs: CreateLogStream, PutLogEvents, CreateLogGroup
```

### `annam-stepfunctions-role` (used by Step Functions)

```
ECS:     RunTask, StopTask, DescribeTasks
IAM:     PassRole (task + execution roles only)
Events:  PutTargets, PutRule, DescribeRule (for .sync ECS integration)
S3:      GetObject on runs/* prefix only
SNS:     Publish on annam-gapfill-failures topic only
```

### `annam-scheduler-role` (used by EventBridge Scheduler)

```
States:  StartExecution on annam-gapfill state machine only
```

---

## 6. Storage Layout

### S3 bucket structure

```
s3://annam-gapfill-prod-<acct>/
│
├── raw/
│   ├── 216/
│   │   └── 216.csv                        ← target sensor CSV (input)
│   ├── 255/
│   │   └── 255.csv
│   └── 201/
│       └── 201.csv
│
├── models/
│   ├── 216/
│   │   ├── model_selection.joblib          ← trained model bundle
│   │   └── model_mapping_report.txt        ← human-readable mapping
│   └── 201/
│       └── ...
│
├── filled/
│   └── 2026-06-15/                         ← partitioned by run date
│       ├── 216/
│       │   └── filled_dataset.csv
│       └── 201/
│           └── filled_dataset.csv
│
├── reports/
│   └── 2026-06-15/
│       ├── 216/
│       │   ├── audit_report.csv
│       │   ├── evaluation_report.csv
│       │   ├── neighbor_quality_report.csv
│       │   ├── model_selection_report.csv
│       │   └── mae_vs_gapsize.png
│       └── 201/
│           └── ...
│
└── runs/
    └── <execution-name>/
        └── batches.json                    ← Step Functions intermediate
```

### DynamoDB schemas

**`annam-sensor-metadata`**
```
PK: "SENSOR#<device_id>"  SK: "META"
Attributes: device_id, status (ACTIVE|INACTIVE), latitude, longitude,
            ref_col, neighbors (list of {id, distance_km}),
            jump_thresholds (optional), clipping_ranges (optional),
            continuous_variables (optional), updated_at
GSI: gsi_status — PK: status, SK: device_id
```

**`annam-gapfill-results`**
```
PK: "SENSOR#<device_id>"  SK: "RUN#<run_date>#<run_id>"
Attributes: device_id, run_id, run_date, status, rows_total,
            method_counts (M), filled_s3_uri, reports_s3_prefix,
            train_mode, duration_seconds, error (on failure), ttl
TTL attribute: ttl (epoch seconds, 180 days)
```

---

## 7. Capacity and Cost Model

### Measured baselines (real runs on Annam_216 + Annam_255)

| Metric | Value |
|---|---|
| Wall time per sensor (train mode) | ~117 s |
| Wall time per sensor (inference mode) | ~113 s |
| Peak RSS per sensor | ~257 MB |
| Filled dataset size | ~3.2 MB |
| Model bundle size | ~4.6 MB |

**Key finding:** Inference and training run times are nearly identical because `evaluate_synthetic_gaps()` re-fits each variable's model internally during evaluation. This single fact drives the cost model — every run costs roughly the same compute regardless of `TRAIN_MODE`.

### Cost estimates (ap-south-1, Fargate Spot ~70% off on-demand)

| Fleet | Daily cadence/mo | Hourly cadence/mo |
|---|---|---|
| 100 sensors | ~$2 | ~$36 |
| 1,000 sensors | ~$15 | ~$360 |
| 10,000 sensors | ~$150 | ~$3,600 |

**Recommendation:** Run daily training. Add hourly inference only if a downstream consumer genuinely needs intra-day freshness. Each 24× cadence reduction cuts compute cost 24×.

### Fargate task sizing rationale

| Task | vCPU | Memory | Rationale |
|---|---|---|---|
| `annam-run-batch` | 1024 (1 vCPU) | 2048 MB (2 GB) | 257 MB RSS peak + 30 GB ephemeral for neighbour cache and I/O buffers |
| `annam-list-sensors` | 256 (0.25 vCPU) | 512 MB (0.5 GB) | Pure DynamoDB query + S3 write; no ML deps exercised |

### Throughput ceiling

```
20 concurrent tasks × 25 sensors/task × (3600 s / 115 s per sensor)
= ~15,600 sensors/hour

A 10,000-sensor fleet completes in ~40 minutes wall clock.
```

---

## 8. Reliability Design

| Failure mode | Behaviour | Where handled |
|---|---|---|
| Single sensor bad data | Error logged, `status=failed` in DynamoDB, batch continues | `run_batch.process_sensor` try/except |
| Entire batch fails (all sensors) | Raises → Step Functions retries (3×, exponential backoff, 30s base) | `run_batch.run_batch` all-fail raise |
| Fargate Spot interruption | Task dies → Map retries on new Spot slot | SFN `Retry` on `States.TaskFailed` |
| No saved model in inference mode | Explicit error captured per sensor | `process_sensor` model-existence check |
| DiscoverAndBatch failure | Step Functions retries 2× then SNS alert → FailState | State-level Catch |
| Partial fleet failure | Other batches unaffected; SNS only on complete hard fail | Map `ToleratedFailurePercentage=10` |
| Re-run safety | Idempotent: same S3 keys overwritten, DynamoDB upserts | S3 PutObject + DynamoDB overwrite_by_pkeys |

---

## 9. Security Controls

| Area | Control | Implementation |
|---|---|---|
| Network | No public IP on tasks | `AssignPublicIp=DISABLED` in task network config |
| Network | AWS API access without NAT | VPC gateway + interface endpoints |
| Network | Least-privilege egress | Security group allows 443 to endpoint ENIs only |
| Identity | No static credentials | Task IAM role (instance metadata) |
| Identity | Least-privilege policies | Scoped to exact resource ARNs and prefixes |
| Data at rest | Encryption | S3 SSE-AES256 on every PutObject; DynamoDB default encryption |
| Data in transit | TLS everywhere | All AWS SDK calls use HTTPS |
| Container | Non-root user | `uid 10001` (appuser) in Dockerfile |
| Container | No compiler in runtime image | Multi-stage build strips build-essential |
| Container | Image scanning | ECR `scanOnPush=true` |
| CI/CD | No long-lived keys | GitHub OIDC role assumption |
| Secrets | None needed | Task role provides all credentials at runtime |

---

## 10. Observability

### Log structure (CloudWatch Logs Insights queryable)

Every log line is a JSON object. Example:

```json
{
  "ts": "2026-06-15T02:35:12Z",
  "level": "INFO",
  "msg": "Sensor done",
  "stage": "run_batch",
  "run_id": "annam-gapfill-2026-06-15",
  "device_id": "216",
  "duration_s": 114.3,
  "status": "success"
}
```

Query failed sensors in the last 24 hours:
```
fields ts, device_id, error
| filter level = "ERROR" and msg = "Sensor failed"
| sort ts desc
| limit 50
```

### EMF metrics emitted

| Metric | Unit | Dimensions |
|---|---|---|
| `SensorsProcessed` | Count | stage, run_id |
| `SensorsSucceeded` | Count | stage, run_id |
| `SensorsFailed` | Count | stage, run_id |
| `BatchDurationSeconds` | Seconds | stage, run_id |
| `ActiveSensors` | Count | stage |
| `BatchCount` | Count | stage |
| `Rows_original` | Count | DeviceId |
| `Rows_interpolation` | Count | DeviceId |
| `Rows_model` | Count | DeviceId |
| `Rows_neighbor` | Count | DeviceId |
| `Rows_unresolved` | Count | DeviceId |
| `TaskCostUSD` | None | stage, run_id |
| `CostPerSensorUSD` | None | stage, run_id |

### Alarms (deployed by `infrastructure/cloudwatch/deploy_alarms.sh`)

| Alarm | Threshold | Action |
|---|---|---|
| `SensorsFailed > 50` in 1 hour | p1 alert | SNS → PagerDuty/email |
| `SFN ExecutionsFailed >= 1` | p1 alert | SNS → PagerDuty/email |
| `BatchDurationSeconds > 3600` (1 hour) | p2 warning | SNS → email |
| `Rows_unresolved > 1000` per device | p2 warning | SNS → email |
